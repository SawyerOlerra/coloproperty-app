"""
Colorado Property Lookup — Flask backend
Uses Census geocoder + OSM + ColoProperty.com (MLS)
"""

import re
import math
import json
import urllib.request
import urllib.parse
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────────────────────────

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36'

def http_get(url, headers=None, timeout=10):
    h = {'User-Agent': UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='replace')


def http_get_json(url, headers=None, timeout=10):
    return json.loads(http_get(url, headers, timeout))


# ──────────────────────────────────────────────────────────────────────────────
# Step 1 — Geocode with US Census API
# ──────────────────────────────────────────────────────────────────────────────

def geocode_address(address: str) -> dict:
    """Return {lat, lng, county_fips, county_name, matched_address} or empty dict."""
    try:
        url = (
            'https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress'
            '?benchmark=Public_AR_Current&vintage=Current_Current&layers=10&format=json&address='
            + urllib.parse.quote(address)
        )
        data = http_get_json(url, timeout=12)
        matches = data.get('result', {}).get('addressMatches', [])
        if not matches:
            return {}
        m = matches[0]
        coords = m['coordinates']
        county_info = m.get('geographies', {}).get('Census Block Groups', [{}])[0]
        county_fips = '08' + county_info.get('COUNTY', '')  # CO state FIPS = 08
        return {
            'lat': coords['y'],
            'lng': coords['x'],
            'county_fips': county_fips,
            'county_name': '',
            'matched_address': m.get('matchedAddress', address),
        }
    except Exception:
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Step 2 — OSM building + parcel data (free, no API key)
# ──────────────────────────────────────────────────────────────────────────────

def _polygon_area_m2(geometry: list) -> float:
    """Shoelace formula for polygon area in m² (lat/lng coords → approx metres)."""
    if len(geometry) < 3:
        return 0.0
    lat0 = sum(p['lat'] for p in geometry) / len(geometry)
    m_per_deg_lat = 111_320.0
    m_per_deg_lng = 111_320.0 * math.cos(math.radians(lat0))
    xs = [p['lon'] * m_per_deg_lng for p in geometry]
    ys = [p['lat'] * m_per_deg_lat for p in geometry]
    n = len(xs)
    area = abs(sum(xs[i] * ys[(i + 1) % n] - xs[(i + 1) % n] * ys[i] for i in range(n))) / 2
    return area


def _m2_to_sqft(m2: float) -> str:
    sqft = m2 * 10.7639
    if sqft > 43_560:
        return f'{sqft / 43_560:.2f} acres ({int(sqft):,} sq ft)'
    return f'{int(sqft):,} sq ft'


def osm_lookup(lat: float, lng: float) -> dict:
    """Return building area, lot area, tags from OpenStreetMap."""
    result = {}
    try:
        # Building footprint
        q = f'[out:json];(way[building](around:60,{lat},{lng}););out tags geom;'
        url = 'https://overpass-api.de/api/interpreter?data=' + urllib.parse.quote(q)
        data = http_get_json(url, timeout=15)
        elements = data.get('elements', [])
        if elements:
            el = elements[0]
            geom = el.get('geometry', [])
            tags = el.get('tags', {})
            if geom:
                area_m2 = _polygon_area_m2(geom)
                if area_m2 > 5:
                    result['building_footprint_sqft'] = _m2_to_sqft(area_m2)
            if tags.get('building:levels'):
                result['building_levels'] = tags['building:levels']
            if tags.get('building'):
                result['building_type'] = tags['building']
    except Exception:
        pass
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Step 3 — ColoProperty.com MLS lookup (HTTP session + Playwright detail page)
# ──────────────────────────────────────────────────────────────────────────────

PROPERTY_SIZE_KEYS = [
    'lot size', 'lot sq ft', 'lot sqft', 'lot area', 'total acres', 'acreage',
    'land area', 'land acres', 'land sq ft', 'parcel size', 'lot dimensions',
    'approx lot size', 'approx acreage', 'total lot', 'sq ft lot',
    'living area', 'total sq ft', 'total sqft', 'square feet', 'square footage',
    'bldg sq ft', 'finished sq ft', 'above grade sq ft', 'approx sqft',
]
ZONING_KEYS = [
    'zoning', 'zoning type', 'zone', 'zoning class', 'zoning code',
    'zoning desc', 'land use', 'property use', 'use code', 'land zone',
    'zoning description', 'use type', 'property type',
]
FAR_KEYS = [
    'floor area ratio', 'far', 'f.a.r', 'floor/area', 'floor-area ratio',
    'max floor area', 'flr area ratio',
]
COVERAGE_KEYS = [
    'building coverage', 'lot coverage', 'coverage ratio', 'coverage %',
    'impervious coverage', 'impervious surface', 'max coverage',
    'building footprint coverage', 'max lot coverage',
]


def best_match(fields: dict, keys: list):
    fl = {k.lower(): v for k, v in fields.items()}
    for key in keys:
        for label, val in fl.items():
            if key in label or label in key:
                return val
    return None


def extract_description_hints(description: str):
    far = cov = None
    if not description:
        return far, cov
    m = re.search(r'\b(?:FAR|floor\s*area\s*ratio)\s*[=:of\s]+([0-9]+\.?[0-9]*)', description, re.I)
    if m:
        far = m.group(1)
    m = re.search(r'\b(?:lot|building|site|impervious)\s*coverage\s*[=:of\s]+([0-9]+\.?[0-9]*\s*%?)', description, re.I)
    if m:
        cov = m.group(1)
    return far, cov


def _get_cp_session_cookies() -> dict:
    """Load ColoProperty.com homepage to get valid session cookies."""
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request('https://www.coloproperty.com/', headers={'User-Agent': UA})
    opener.open(req, timeout=12)
    return {c.name: c.value for c in cj}


def coloproperty_mapsearch(address: str, lat: float, lng: float) -> list:
    """Call ColoProperty /listing/mapsearch and return list of listing dicts."""
    try:
        cookies = _get_cp_session_cookies()
        cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())

        # Parse address components
        parts = address.split(',')
        street = parts[0].strip()
        nums = re.findall(r'^\d+', street)
        st_number = nums[0] if nums else ''
        st_name = re.sub(r'^\d+\s*', '', street).strip()
        # Remove common suffix words
        st_name = re.sub(r'\s+(?:Ave|St|Rd|Blvd|Dr|Pl|Ct|Way|Ln|Circle|Cir|Trail|Trl)\.?$', '', st_name, flags=re.I).strip()

        delta = 0.01  # ~1 km bounding box
        params = {
            'rawLoc': street,
            'stNumber': st_number,
            'stName': st_name,
            'latMin': str(lat - delta),
            'latMax': str(lat + delta),
            'lngMin': str(lng - delta),
            'lngMax': str(lng + delta),
            'showSolds': 'A,AB,AF,AP,C,P,S',
            'typeIds': '1,2,3,4,5,6,7,8,9,10',
            'perPage': '5',
            'maxResults': '5',
            'searchFor': 'listing',
            'exclStatus': '',
        }
        url = 'https://www.coloproperty.com/listing/mapsearch?' + urllib.parse.urlencode(params)
        data = http_get_json(url, headers={
            'Cookie': cookie_str,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': 'https://www.coloproperty.com/',
        }, timeout=12)
        return data.get('D', {}).get('Results', [])
    except Exception:
        return []


def coloproperty_detail(listing_id) -> dict:
    """Scrape the ColoProperty listing detail page for a given listing ID."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    fields = {}
    source_url = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={'width': 1280, 'height': 900})
        page = ctx.new_page()
        try:
            # Try common URL patterns for the detail page
            for url_pattern in [
                f'https://www.coloproperty.com/listing/view/{listing_id}',
                f'https://www.coloproperty.com/listing/detail/{listing_id}',
            ]:
                try:
                    page.goto(url_pattern, wait_until='networkidle', timeout=25_000)
                    page.wait_for_timeout(1_500)
                    if page.url != 'https://www.coloproperty.com/' and '404' not in page.title().lower():
                        source_url = page.url
                        break
                except PWTimeout:
                    continue

            if not source_url:
                return {}

            # Extract table rows
            for row in page.query_selector_all('table tr'):
                cells = row.query_selector_all('td, th')
                if len(cells) >= 2:
                    label = cells[0].inner_text().strip().lower().rstrip(':').strip()
                    value = cells[1].inner_text().strip()
                    if label and value and len(label) < 80:
                        fields[label] = value

            # Extract dl lists
            for dt in page.query_selector_all('dl dt'):
                try:
                    label = dt.inner_text().strip().lower().rstrip(':').strip()
                    dd = dt.evaluate('el => el.nextElementSibling')
                    if dd:
                        value = page.evaluate('el => el ? el.innerText : ""', dd).strip()
                        if label and value and len(label) < 80:
                            fields.setdefault(label, value)
                except Exception:
                    pass

            # Generic wrappers
            for wrapper in page.query_selector_all('[class*="field"],[class*="detail"],[class*="row"],[class*="item"]'):
                try:
                    children = wrapper.query_selector_all(':scope > *')
                    if len(children) >= 2:
                        label = children[0].inner_text().strip().lower().rstrip(':').strip()
                        value = children[1].inner_text().strip()
                        if label and value and 2 < len(label) < 80 and len(value) < 300:
                            fields.setdefault(label, value)
                except Exception:
                    pass

        except Exception:
            pass
        finally:
            browser.close()

    fields['_source_url'] = source_url or ''
    return fields


# ──────────────────────────────────────────────────────────────────────────────
# County assessor links
# ──────────────────────────────────────────────────────────────────────────────

COUNTY_ASSESSOR_URLS = {
    '08001': ('Adams', 'https://www.adcogov.org/assessor'),
    '08005': ('Arapahoe', 'https://www.arapahoegov.com/assessor'),
    '08013': ('Boulder', 'https://www.bouldercounty.gov/property-and-land/assessor/'),
    '08031': ('Denver', 'https://www.denvergov.org/Government/Departments/Assessment'),
    '08035': ('Douglas', 'https://assessor.douglas.co.us/'),
    '08041': ('El Paso', 'https://www.elpasoco.com/property-assessor/'),
    '08059': ('Jefferson', 'https://www.jeffco.us/assessor'),
    '08069': ('Larimer', 'https://www.larimer.gov/assessor/search'),
    '08077': ('Mesa', 'https://www.mesacounty.us/assessor/'),
    '08101': ('Pueblo', 'https://www.pueblocounty.us/departments/assessor'),
    '08123': ('Weld', 'https://www.weldgov.com/departments/assessor'),
}


# ──────────────────────────────────────────────────────────────────────────────
# Main lookup
# ──────────────────────────────────────────────────────────────────────────────

def lookup_property(address: str) -> dict:
    result = {
        'address': address,
        'found': False,
        'property_size': None,
        'zoning': None,
        'floor_area_ratio': None,
        'building_coverage': None,
        'raw_fields': {},
        'source_url': None,
        'assessor_url': None,
        'notes': [],
    }

    # ── 1. Geocode ──────────────────────────────────────────────────────────
    geo = geocode_address(address)
    if not geo:
        result['notes'].append('Address not found. Include full address with city and state.')
        return result

    lat, lng = geo['lat'], geo['lng']
    result['address'] = geo.get('matched_address', address)

    # ── 2. County assessor link ─────────────────────────────────────────────
    fips = geo.get('county_fips', '')
    if fips in COUNTY_ASSESSOR_URLS:
        county_name, assessor_url = COUNTY_ASSESSOR_URLS[fips]
        result['assessor_url'] = assessor_url
        result['raw_fields']['county'] = county_name + ' County'

    # ── 3. OSM building data ────────────────────────────────────────────────
    osm = osm_lookup(lat, lng)
    if osm.get('building_footprint_sqft'):
        result['raw_fields']['building footprint (osm)'] = osm['building_footprint_sqft']
    if osm.get('building_levels'):
        result['raw_fields']['building levels (osm)'] = osm['building_levels']
    if osm.get('building_type'):
        result['raw_fields']['building type (osm)'] = osm['building_type']

    # ── 4. ColoProperty MLS search ──────────────────────────────────────────
    listings = coloproperty_mapsearch(address, lat, lng)

    if listings:
        listing = listings[0]
        listing_id = listing.get('lid') or listing.get('listingId')

        # Populate from mapsearch response fields
        for k, v in listing.items():
            if v not in (None, '', 0, -1):
                label = str(k).lower().replace('_', ' ').replace('-', ' ').strip()
                result['raw_fields'].setdefault(label, str(v))

        # Try to get full detail page via Playwright
        if listing_id and listing_id != -1:
            detail = coloproperty_detail(listing_id)
            source_url = detail.pop('_source_url', None)
            if source_url:
                result['source_url'] = source_url
            result['raw_fields'].update(detail)

        result['found'] = True
    else:
        result['notes'].append(
            'No active or recent MLS listing found on ColoProperty.com for this address. '
            'Showing available data from public sources below.'
        )
        result['found'] = bool(result['raw_fields'])

    # ── 5. Map key fields ───────────────────────────────────────────────────
    fields = result['raw_fields']
    result['property_size']     = best_match(fields, PROPERTY_SIZE_KEYS)
    result['zoning']            = best_match(fields, ZONING_KEYS)
    result['floor_area_ratio']  = best_match(fields, FAR_KEYS)
    result['building_coverage'] = best_match(fields, COVERAGE_KEYS)

    desc = best_match(fields, ['description', 'remarks', 'public remarks', 'agent remarks'])
    far_hint, cov_hint = extract_description_hints(desc or '')
    if not result['floor_area_ratio'] and far_hint:
        result['floor_area_ratio'] = far_hint + ' (from listing description)'
    if not result['building_coverage'] and cov_hint:
        result['building_coverage'] = cov_hint + ' (from listing description)'

    if not result['floor_area_ratio']:
        result['notes'].append(
            'FAR is a zoning regulation, not a property record — '
            'contact your local city/county planning department.'
        )
    if not result['building_coverage']:
        result['notes'].append(
            'Building/lot coverage limits are set by the zoning code — '
            'contact your local zoning office.'
        )

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Flask routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/lookup')
def api_lookup():
    address = request.args.get('address', '').strip()
    if not address:
        return jsonify({'error': 'Address is required'}), 400
    data = lookup_property(address)
    return jsonify(data)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
