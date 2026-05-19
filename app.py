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
    '08001': ('Adams County',      'https://www.adcogov.org/assessor'),
    '08005': ('Arapahoe County',   'https://www.arapahoegov.com/assessor'),
    '08013': ('Boulder County',    'https://www.bouldercounty.gov/property-and-land/assessor/'),
    '08014': ('Broomfield County', 'https://www.broomfield.org/212/Assessors-Office'),
    '08031': ('Denver County',     'https://www.denvergov.org/Government/Departments/Assessment'),
    '08035': ('Douglas County',    'https://assessor.douglas.co.us/'),
    '08041': ('El Paso County',    'https://www.elpasoco.com/property-assessor/'),
    '08059': ('Jefferson County',  'https://www.jeffco.us/assessor'),
    '08069': ('Larimer County',    'https://www.larimer.gov/assessor/search'),
    '08077': ('Mesa County',       'https://www.mesacounty.us/assessor/'),
    '08101': ('Pueblo County',     'https://www.pueblocounty.us/departments/assessor'),
    '08123': ('Weld County',       'https://www.weldgov.com/departments/assessor'),
}


# ──────────────────────────────────────────────────────────────────────────────
# ColoProperty.com mapsearch API
# ──────────────────────────────────────────────────────────────────────────────

def _cp_cookies() -> str:
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request('https://www.coloproperty.com/', headers={'User-Agent': UA})
    opener.open(req, timeout=12)
    return '; '.join(f'{c.name}={c.value}' for c in cj)


def mapsearch(lat: float, lng: float, delta: float = 0.008) -> list:
    """Find listings near lat/lng. Returns list of listing dicts."""
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
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return data.get('D', {}).get('Results', [])
    except Exception:
        return []


def _addr_similarity(listing: dict, target: str) -> float:
    a1 = (listing.get('addr1') or '').lower()
    nums_t = re.findall(r'\d+', target.lower())
    nums_a = re.findall(r'\d+', a1)
    if nums_t and nums_a and nums_t[0] == nums_a[0]:
        return 1.0
    return 0.0


def find_listing(lat: float, lng: float, address: str) -> dict | None:
    results = mapsearch(lat, lng, delta=0.008)
    if not results:
        results = mapsearch(lat, lng, delta=0.02)
    if not results:
        return None
    scored = [(r, _addr_similarity(r, address)) for r in results]
    best = max(scored, key=lambda x: x[1])
    if best[1] > 0:
        return best[0]
    def dist(r):
        dlat = float(r.get('lat', lat)) - lat
        dlng = float(r.get('lng', lng)) - lng
        return dlat**2 + dlng**2
    return min(results, key=dist)


# ──────────────────────────────────────────────────────────────────────────────
# Parse mapsearch HTML fields (quick + summ)
# ──────────────────────────────────────────────────────────────────────────────

def _strip_tags(html: str) -> str:
    return re.sub(r'<[^>]+>', ' ', html or '')


def _parse_quick(quick_html: str) -> dict:
    """Extract structured fields from the mapsearch 'quick' HTML snippet."""
    fields = {}
    spans = [
        (r'class="price"[^>]*>(.*?)</span>',        'Price'),
        (r'class="card-status[^"]*"[^>]*>(.*?)</span>', 'Status'),
        (r'class="beds"[^>]*>(.*?)</span>',          'Beds'),
        (r'class="baths"[^>]*>(.*?)</span>',         'Baths'),
        (r'class="sqft"[^>]*>(.*?)</span>',          'Square Feet'),
        (r'class="bldg-sqft"[^>]*>(.*?)</span>',     'Building SqFt'),
        (r'class="year-built"[^>]*>(.*?)</span>',    'Year Built'),
        (r'class="zoning"[^>]*>(.*?)</span>',        'Zoning'),
    ]
    for pat, label in spans:
        m = re.search(pat, quick_html, re.I | re.S)
        if m:
            val = _strip_tags(m.group(1)).strip()
            if val:
                fields[label] = val
    return fields


def _parse_summ(summ_html: str) -> dict:
    """Extract additional fields from the mapsearch 'summ' HTML snippet."""
    fields = {}
    text = re.sub(r'\s+', ' ', _strip_tags(summ_html)).strip()

    # Lot size / acreage
    m = re.search(r'on\s+([\d.]+)\s*Acr', text, re.I)
    if m:
        fields['Lot Size'] = m.group(1) + ' Acres'

    # Property type
    m = re.search(r'(Attached|Detached|Single[- ]?Family|Condo|Townhome|Ranch|'
                  r'Multi[- ]?Family|Commercial|Industrial|Office|Retail)\b', text, re.I)
    if m:
        fields['Property Type'] = m.group(1)

    # Price per sqft
    m = re.search(r'\(\$([\d,]+)/SF\)', text)
    if m:
        fields['Price/SqFt'] = '$' + m.group(1) + '/SF'

    # Listing office (inside card-list-office span)
    m = re.search(r'card-list-office[^>]*>(.*?)<', summ_html, re.I | re.S)
    if m:
        office = _strip_tags(m.group(1)).strip()
        if office:
            fields['Listing Office'] = office

    return fields


# ──────────────────────────────────────────────────────────────────────────────
# Detail page via plain HTTP (no Playwright — Cloudflare blocks headless)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_detail_http(lid, cookie_str: str = '') -> dict:
    """Fetch /listing/details/{lid} with urllib and parse table rows."""
    url = f'https://www.coloproperty.com/listing/details/{lid}'
    try:
        if not cookie_str:
            cookie_str = _cp_cookies()
        req = urllib.request.Request(url, headers={
            'User-Agent': UA,
            'Cookie': cookie_str,
            'Referer': 'https://www.coloproperty.com/',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode('utf-8', errors='replace')

        # If Cloudflare challenge page, bail out
        if '<title>Just a moment' in html or 'cf-browser-verification' in html:
            return {'_blocked': True}

        fields = {}
        # Parse <tr><td>Label</td><td>Value</td></tr>
        for row_m in re.finditer(r'<tr[^>]*>(.*?)</tr>', html, re.I | re.S):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row_m.group(1), re.I | re.S)
            if len(cells) >= 2:
                label = _strip_tags(cells[0]).strip().rstrip(':').strip()
                value = _strip_tags(cells[1]).strip()
                if label and value and 1 < len(label) < 60:
                    if label.lower() not in ('request info', 'compare', 'share', 'action'):
                        fields[label] = value

        # Also grab definition-list style dt/dd pairs
        for dt_m in re.finditer(r'<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>', html, re.I | re.S):
            label = _strip_tags(dt_m.group(1)).strip().rstrip(':').strip()
            value = _strip_tags(dt_m.group(2)).strip()
            if label and value and 1 < len(label) < 60:
                fields.setdefault(label, value)

        fields['_source_url'] = url
        return fields
    except Exception as e:
        return {'_error': str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# Key field extraction
# ──────────────────────────────────────────────────────────────────────────────

SIZE_KEYS    = ['lot size', 'lot sq ft', 'lot sqft', 'lot area', 'total acres', 'acreage',
                'land area', 'land sq ft', 'parcel size', 'approx lot size', 'sq ft lot',
                'total sq ft', 'total sqft', 'square feet', 'square footage', 'bldg sq ft',
                'finished sq ft', 'above grade sq ft', 'approx sqft', 'finished',
                'building sqft', 'sqft', 'beds']
ZONING_KEYS  = ['zoning', 'zoning type', 'zone', 'zoning code', 'land use', 'property use',
                'use code', 'zoning description', 'use type']
FAR_KEYS     = ['floor area ratio', 'far', 'f.a.r', 'floor/area', 'floor-area ratio']
COV_KEYS     = ['building coverage', 'lot coverage', 'coverage ratio', 'coverage %',
                'impervious coverage', 'impervious surface', 'max coverage', 'max lot coverage']

# Better size priority: prefer explicit sqft/lot over beds
SIZE_PRIORITY = ['square feet', 'building sqft', 'lot size', 'total sqft', 'sqft',
                 'lot area', 'acreage', 'total acres', 'finished sq ft', 'above grade sq ft']


def best(fields: dict, keys: list):
    fl = {k.lower(): v for k, v in fields.items()}
    for key in keys:
        for label, val in fl.items():
            if key in label or label in key:
                return val
    return None


def best_size(fields: dict):
    fl = {k.lower(): v for k, v in fields.items()}
    for key in SIZE_PRIORITY:
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
        result['notes'].append(
            'Address not recognized. Try including city and state, '
            'e.g. "123 Main St, Boulder, CO".'
        )
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
    result['source_url'] = f'https://www.coloproperty.com/listing/details/{lid}' if lid else None

    # 4. Core fields from mapsearch listing object
    for k, label in [('addr1', 'Address'), ('addr2', 'City/State/Zip'),
                     ('price', 'Price'), ('status', 'MLS Status'), ('mlsNumber', 'MLS Number')]:
        if listing.get(k):
            result['raw_fields'][label] = str(listing[k])

    # 5. Parse quick + summ HTML (rich data already in mapsearch response)
    result['raw_fields'].update(_parse_quick(listing.get('quick', '')))
    result['raw_fields'].update(_parse_summ(listing.get('summ', '')))

    # 6. Try detail page via plain HTTP
    if lid:
        cookies = ''
        try:
            cookies = _cp_cookies()
        except Exception:
            pass
        detail = fetch_detail_http(lid, cookies)
        blocked = detail.pop('_blocked', False)
        source_url = detail.pop('_source_url', None)
        err = detail.pop('_error', None)
        if source_url:
            result['source_url'] = source_url
        if not blocked and detail:
            result['raw_fields'].update(detail)
        elif blocked:
            result['notes'].append(
                'Full listing details are protected by Cloudflare on ColoProperty.com. '
                'Data shown is from the MLS search index.'
            )

    result['found'] = True

    # 7. Map to headline fields
    fields = result['raw_fields']
    result['property_size']     = best_size(fields)
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
    """Step-by-step diagnostic."""
    address = request.args.get('address', '2420 Windrow Dr, Fort Collins, CO 80525').strip()
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
        except Exception as e:
            out['cookies_error'] = str(e)
            cookie_str = ''
        results = mapsearch(lat, lng, delta=0.02)
        out['mapsearch_count'] = len(results)
        out['mapsearch_sample'] = results[:2]
        listing = find_listing(lat, lng, address)
        out['listing_matched'] = listing
        if listing and listing.get('lid'):
            detail = fetch_detail_http(listing['lid'], cookie_str)
            out['detail'] = detail
            out['quick_parsed'] = _parse_quick(listing.get('quick', ''))
            out['summ_parsed'] = _parse_summ(listing.get('summ', ''))
    except Exception as e:
        out['exception'] = str(e)
    return jsonify(out)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
