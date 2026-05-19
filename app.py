"""
Colorado Property Lookup
Sources: Census geocoder + ColoProperty.com (IRES MLS)
"""

import re
import json
import urllib.request
import urllib.parse
import http.cookiejar
from flask import Flask, request, jsonify, render_template
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

app = Flask(__name__)

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)

# ──────────────────────────────────────────────────────────────────────────────
# Geocoding
# ──────────────────────────────────────────────────────────────────────────────

def geocode(address: str) -> dict:
    """Census geocoder → {lat, lng, county_fips, matched_address}."""
    try:
        url = (
            'https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress'
            '?benchmark=Public_AR_Current&vintage=Current_Current&layers=10&format=json&address='
            + urllib.parse.quote(address)
        )
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        matches = data.get('result', {}).get('addressMatches', [])
        if not matches:
            return {}
        m = matches[0]
        coords = m['coordinates']
        county_fips = '08' + m.get('geographies', {}).get('Census Block Groups', [{}])[0].get('COUNTY', '')
        return {
            'lat': coords['y'],
            'lng': coords['x'],
            'county_fips': county_fips,
            'matched_address': m.get('matchedAddress', address),
        }
    except Exception:
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# County assessor links
# ──────────────────────────────────────────────────────────────────────────────

ASSESSORS = {
    '08001': ('Adams County',     'https://www.adcogov.org/assessor'),
    '08005': ('Arapahoe County',  'https://www.arapahoegov.com/assessor'),
    '08013': ('Boulder County',   'https://www.bouldercounty.gov/property-and-land/assessor/'),
    '08014': ('Broomfield County','https://www.broomfield.org/212/Assessors-Office'),
    '08031': ('Denver County',    'https://www.denvergov.org/Government/Departments/Assessment'),
    '08035': ('Douglas County',   'https://assessor.douglas.co.us/'),
    '08041': ('El Paso County',   'https://www.elpasoco.com/property-assessor/'),
    '08059': ('Jefferson County', 'https://www.jeffco.us/assessor'),
    '08069': ('Larimer County',   'https://www.larimer.gov/assessor/search'),
    '08077': ('Mesa County',      'https://www.mesacounty.us/assessor/'),
    '08101': ('Pueblo County',    'https://www.pueblocounty.us/departments/assessor'),
    '08123': ('Weld County',      'https://www.weldgov.com/departments/assessor'),
}


# ──────────────────────────────────────────────────────────────────────────────
# ColoProperty.com mapsearch API (HTTP, no Playwright)
# ──────────────────────────────────────────────────────────────────────────────

def _cp_cookies() -> str:
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request('https://www.coloproperty.com/', headers={'User-Agent': UA})
    opener.open(req, timeout=12)
    return '; '.join(f'{c.name}={c.value}' for c in cj)


def mapsearch(lat: float, lng: float, delta: float = 0.008) -> list:
    """Find listings near lat/lng. Returns list of {lid, addr1, addr2, ...}."""
    try:
        cookie_str = _cp_cookies()
        params = {
            'latMin': str(lat - delta), 'latMax': str(lat + delta),
            'lngMin': str(lng - delta), 'lngMax': str(lng + delta),
            'showSolds': 'A,AB,AF,AP,C,P,S',
            'typeIds': '1,2,3,4,5,6,7,8,9,10',
            'perPage': '20', 'maxResults': '20',
            'searchFor': 'listing',
        }
        url = 'https://www.coloproperty.com/listing/mapsearch?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            'User-Agent': UA,
            'Cookie': cookie_str,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': 'https://www.coloproperty.com/',
        })
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        return data.get('D', {}).get('Results', [])
    except Exception:
        return []


def _addr_similarity(listing: dict, target: str) -> float:
    """Score how well a listing address matches the target (0–1)."""
    a1 = (listing.get('addr1') or '').lower()
    target_l = target.lower()
    # Extract street number and name from both
    nums_t = re.findall(r'\d+', target_l)
    nums_a = re.findall(r'\d+', a1)
    if nums_t and nums_a and nums_t[0] == nums_a[0]:
        return 1.0
    return 0.0


def find_listing(lat: float, lng: float, address: str) -> dict | None:
    """Return the mapsearch result that best matches the address."""
    results = mapsearch(lat, lng, delta=0.008)
    if not results:
        results = mapsearch(lat, lng, delta=0.02)
    if not results:
        return None
    # Score each listing
    scored = [(r, _addr_similarity(r, address)) for r in results]
    best = max(scored, key=lambda x: x[1])
    if best[1] > 0:
        return best[0]
    # No street-number match — return the closest by lat/lng
    def dist(r):
        dlat = float(r.get('lat', lat)) - lat
        dlng = float(r.get('lng', lng)) - lng
        return dlat**2 + dlng**2
    return min(results, key=dist)


# ──────────────────────────────────────────────────────────────────────────────
# Listing detail page scraper (Playwright)
# ──────────────────────────────────────────────────────────────────────────────

def scrape_detail(lid) -> dict:
    """Scrape /listing/details/{lid} and return all label→value pairs."""
    fields = {}
    source_url = f'https://www.coloproperty.com/listing/details/{lid}'

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
        )
        ctx = browser.new_context(user_agent=UA, viewport={'width': 1280, 'height': 900})
        page = ctx.new_page()
        try:
            page.goto(source_url, wait_until='networkidle', timeout=30_000)
            page.wait_for_timeout(1_500)

            if '404' in page.title() or page.url == 'https://www.coloproperty.com/':
                return {}

            # Primary: table rows (most MLS fields live here)
            for row in page.query_selector_all('table tr'):
                cells = row.query_selector_all('td')
                if len(cells) >= 2:
                    label = cells[0].inner_text().strip().rstrip(':').strip()
                    value = cells[1].inner_text().strip()
                    if label and value and 1 < len(label) < 60:
                        # Skip navigation/action rows
                        if label.lower() not in ('request info', 'compare', 'share'):
                            fields[label] = value

            # Secondary: any remaining label/value divs
            for wrapper in page.query_selector_all('[class*="detail"],[class*="field"],[class*="row"],[class*="item"]'):
                try:
                    children = wrapper.query_selector_all(':scope > *')
                    if len(children) == 2:
                        label = children[0].inner_text().strip().rstrip(':').strip()
                        value = children[1].inner_text().strip()
                        if label and value and 1 < len(label) < 60 and len(value) < 200:
                            fields.setdefault(label, value)
                except Exception:
                    pass

            fields['_source_url'] = page.url
        except PWTimeout:
            fields['_error'] = 'timeout'
        except Exception as e:
            fields['_error'] = str(e)
        finally:
            browser.close()

    return fields


# ──────────────────────────────────────────────────────────────────────────────
# Key field extraction
# ──────────────────────────────────────────────────────────────────────────────

SIZE_KEYS    = ['lot size','lot sq ft','lot sqft','lot area','total acres','acreage',
                'land area','land sq ft','parcel size','approx lot size','sq ft lot',
                'total sq ft','total sqft','square feet','square footage','bldg sq ft',
                'finished sq ft','above grade sq ft','approx sqft','total','finished']
ZONING_KEYS  = ['zoning','zoning type','zone','zoning code','land use','property use',
                'use code','zoning description','use type']
FAR_KEYS     = ['floor area ratio','far','f.a.r','floor/area','floor-area ratio']
COV_KEYS     = ['building coverage','lot coverage','coverage ratio','coverage %',
                'impervious coverage','impervious surface','max coverage','max lot coverage']


def best(fields: dict, keys: list):
    fl = {k.lower(): v for k, v in fields.items()}
    for key in keys:
        for label, val in fl.items():
            if key in label or label in key:
                return val
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Main lookup
# ──────────────────────────────────────────────────────────────────────────────

def lookup(address: str) -> dict:
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

    # 1. Geocode
    geo = geocode(address)
    if not geo:
        result['notes'].append('Address not recognized. Try including city and state, e.g. "123 Main St, Boulder, CO".')
        return result

    lat, lng = geo['lat'], geo['lng']
    result['address'] = geo['matched_address']

    # 2. County assessor link
    fips = geo.get('county_fips', '')
    if fips in ASSESSORS:
        county_name, assessor_url = ASSESSORS[fips]
        result['assessor_url'] = assessor_url
        result['raw_fields']['County'] = county_name

    # 3. Find MLS listing via mapsearch
    listing = find_listing(lat, lng, address)

    if not listing:
        result['notes'].append(
            'No MLS listing found on ColoProperty.com for this address. '
            'The property may not be currently or recently listed.'
        )
        result['found'] = bool(result['raw_fields'])
        result['notes'] += [
            'Floor Area Ratio is a zoning regulation — check with your local planning department.',
            'Building coverage limits come from the zoning code — check with your local zoning office.',
        ]
        return result

    lid = listing.get('lid')

    # 4. Add mapsearch summary fields
    for k in ('addr1', 'addr2', 'price', 'status', 'mlsNumber'):
        if listing.get(k):
            result['raw_fields'][k.replace('addr', 'Address').replace('mls', 'MLS ')] = str(listing[k])

    # Parse quick summary (beds/baths/sqft)
    quick_html = listing.get('quick', '')
    for pat, label in [
        (r'(\d+)\s*bd', 'Beds'),
        (r'(\d+)\s*ba', 'Baths'),
        (r'([\d,]+)\s*sqft', 'SqFt'),
    ]:
        m = re.search(pat, quick_html, re.I)
        if m:
            result['raw_fields'][label] = m.group(1).replace(',', '')

    # 5. Scrape full detail page
    if lid:
        detail = scrape_detail(lid)
        source_url = detail.pop('_source_url', None)
        scrape_error = detail.pop('_error', None)
        if scrape_error:
            result['notes'].append(f'Detail page scrape note: {scrape_error}')
        if source_url:
            result['source_url'] = source_url
        # Merge — detail page takes priority
        result['raw_fields'].update(detail)

    result['found'] = True

    # 6. Map to the four headline fields
    fields = result['raw_fields']
    result['property_size']     = best(fields, SIZE_KEYS)
    result['zoning']            = best(fields, ZONING_KEYS)
    result['floor_area_ratio']  = best(fields, FAR_KEYS)
    result['building_coverage'] = best(fields, COV_KEYS)

    if not result['floor_area_ratio']:
        result['notes'].append(
            'Floor Area Ratio is a zoning regulation, not an MLS field. '
            'Look it up using the Zoning code above at your city/county planning department.'
        )
    if not result['building_coverage']:
        result['notes'].append(
            'Building coverage limits are set by the zoning code. '
            'Contact your local zoning office with the Zoning code shown above.'
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
    return jsonify(lookup(address))


@app.route('/api/debug')
def api_debug():
    """Step-by-step diagnostic — use ?address=... to trace what each stage returns."""
    address = request.args.get('address', '').strip()
    if not address:
        address = '2420 Windrow Dr, Fort Collins, CO 80525'
    out = {'address': address}
    try:
        geo = geocode(address)
        out['geocode'] = geo
        if not geo:
            out['stop'] = 'geocode failed'
            return jsonify(out)
        lat, lng = geo['lat'], geo['lng']
        try:
            cookie_str = _cp_cookies()
            out['cookies_ok'] = bool(cookie_str)
            out['cookie_str'] = cookie_str[:80]
        except Exception as e:
            out['cookies_error'] = str(e)
        # Raw mapsearch response
        try:
            delta = 0.02
            params = {
                'latMin': str(lat - delta), 'latMax': str(lat + delta),
                'lngMin': str(lng - delta), 'lngMax': str(lng + delta),
                'showSolds': 'A,AB,AF,AP,C,P,S',
                'typeIds': '1,2,3,4,5,6,7,8,9,10',
                'perPage': '20', 'maxResults': '20',
                'searchFor': 'listing',
            }
            url = 'https://www.coloproperty.com/listing/mapsearch?' + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={
                'User-Agent': UA, 'Cookie': cookie_str,
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': 'https://www.coloproperty.com/',
            })
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = json.loads(r.read())
            out['mapsearch_raw_keys'] = list(raw.keys()) if isinstance(raw, dict) else str(raw)[:200]
            d = raw.get('D', {})
            out['mapsearch_D_keys'] = list(d.keys()) if isinstance(d, dict) else str(d)[:200]
            results_raw = d.get('Results', [])
            out['mapsearch_count'] = len(results_raw)
            out['mapsearch_sample'] = results_raw[:2]
        except Exception as e:
            out['mapsearch_error'] = str(e)
        listing = find_listing(lat, lng, address)
        out['listing'] = listing
        if listing and listing.get('lid'):
            detail = scrape_detail(listing['lid'])
            out['detail_keys'] = list(detail.keys())
            out['detail_sample'] = {k: v for k, v in list(detail.items())[:10]}
    except Exception as e:
        out['exception'] = str(e)
    return jsonify(out)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
