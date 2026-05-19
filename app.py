"""
ColoProperty.com Property Lookup — Flask backend
"""

import re
from flask import Flask, request, jsonify, render_template
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

app = Flask(__name__)


# ─── Field-matching keys ──────────────────────────────────────────────────────

PROPERTY_SIZE_KEYS = [
    'lot size', 'lot sq ft', 'lot sqft', 'lot area', 'total acres', 'acreage',
    'land area', 'land acres', 'land sq ft', 'parcel size', 'lot dimensions',
    'approx lot size', 'approx acreage', 'total lot', 'sq ft lot',
    'living area', 'total sq ft', 'total sqft', 'square feet', 'square footage',
    'bldg sq ft', 'finished sq ft', 'above grade sq ft', 'approx sqft',
    'approx sq ft', 'total finished sqft',
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
    'surface area coverage', 'impervious coverage', 'impervious surface',
    'max coverage', 'building footprint', 'footprint coverage', 'max lot coverage',
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


def _flatten_api_data(data, fields, depth=0):
    """Recursively flatten JSON API response into label/value pairs."""
    if depth > 4:
        return
    if isinstance(data, dict):
        for k, v in data.items():
            label = str(k).lower().replace('_', ' ').replace('-', ' ').strip()
            if isinstance(v, (str, int, float)) and v not in ('', None, 0):
                fields.setdefault(label, str(v))
            elif isinstance(v, (dict, list)):
                _flatten_api_data(v, fields, depth + 1)
    elif isinstance(data, list):
        for item in data[:5]:
            _flatten_api_data(item, fields, depth + 1)


def extract_page_fields(page) -> dict:
    """Pull all label/value pairs from the current page using multiple strategies."""
    fields = {}

    # Strategy A: <table> rows
    for row in page.query_selector_all('table tr'):
        cells = row.query_selector_all('td, th')
        if len(cells) >= 2:
            label = cells[0].inner_text().strip().lower().rstrip(':').strip()
            value = cells[1].inner_text().strip()
            if label and value and len(label) < 80:
                fields[label] = value

    # Strategy B: <dl> definition lists
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

    # Strategy C: generic wrapper elements with exactly 2 child text nodes
    for wrapper in page.query_selector_all(
        '[class*="field"], [class*="detail"], [class*="row"], [class*="item"], [class*="stat"]'
    ):
        try:
            children = wrapper.query_selector_all(':scope > *')
            if len(children) >= 2:
                label = children[0].inner_text().strip().lower().rstrip(':').strip()
                value = children[1].inner_text().strip()
                if label and value and 2 < len(label) < 80 and len(value) < 300:
                    fields.setdefault(label, value)
        except Exception:
            pass

    # Strategy D: elements with "label" class next to sibling "value" class
    for lbl_el in page.query_selector_all('[class*="label"]'):
        try:
            label = lbl_el.inner_text().strip().lower().rstrip(':').strip()
            if not label or len(label) > 80:
                continue
            val_el = lbl_el.evaluate(
                'el => el.nextElementSibling || '
                '(el.parentElement ? el.parentElement.querySelector("[class*=\'value\']") : null)'
            )
            if val_el:
                value = page.evaluate('el => el ? el.innerText : ""', val_el).strip()
                if value and len(value) < 300:
                    fields.setdefault(label, value)
        except Exception:
            pass

    return fields


# ─── Playwright scraper ───────────────────────────────────────────────────────

def scrape_coloproperty(address: str) -> dict:
    result = {
        'address': address,
        'found': False,
        'property_size': None,
        'zoning': None,
        'floor_area_ratio': None,
        'building_coverage': None,
        'raw_fields': {},
        'source_url': None,
        'notes': [],
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            viewport={'width': 1280, 'height': 900},
        )
        page = ctx.new_page()

        # Capture all JSON API responses
        captured_api = []

        def on_response(response):
            try:
                ct = response.headers.get('content-type', '')
                if response.status == 200 and 'json' in ct:
                    url = response.url
                    if any(k in url.lower() for k in [
                        'search', 'listing', 'property', 'map', 'suggest',
                        'auto', 'result', 'find', 'lookup',
                    ]):
                        try:
                            captured_api.append({'url': url, 'data': response.json()})
                        except Exception:
                            pass
            except Exception:
                pass

        page.on('response', on_response)

        try:
            # ── 1. Load homepage ──────────────────────────────────────────────
            page.goto('https://www.coloproperty.com/', timeout=30_000, wait_until='domcontentloaded')
            page.wait_for_timeout(3_000)

            # ── 2. Find the search input ──────────────────────────────────────
            search_el = None
            for sel in [
                'input[placeholder*="ddress"]',
                'input[placeholder*="earch"]',
                'input[placeholder*="MLS"]',
                'input[placeholder*="Zip"]',
                'input[type="search"]',
                'input[type="text"]',
            ]:
                for el in page.query_selector_all(sel):
                    try:
                        if el.is_visible():
                            search_el = el
                            break
                    except Exception:
                        pass
                if search_el:
                    break

            if search_el is None:
                result['notes'].append('Search input not found — site layout may have changed.')
                return result

            # ── 3. Type address slowly to trigger autocomplete ────────────────
            search_el.click()
            search_el.fill('')
            # Type street number + name to get autocomplete hits
            street = address.split(',')[0].strip() if ',' in address else address[:30]
            search_el.type(street, delay=60)
            page.wait_for_timeout(2_500)

            # ── 4. Click first autocomplete suggestion if present ─────────────
            SUGGEST_SELS = [
                '.autocomplete-suggestion',
                '.suggestion-item',
                '[class*="suggest"]',
                '[class*="autocomplete"] li',
                'li[role="option"]',
                '.dropdown-item',
                '.search-result-item',
                '.pac-item',
                'ul.dropdown-menu li a',
                '[class*="result"] li',
                '.tt-suggestion',
                '.ui-menu-item',
            ]
            clicked_suggestion = False
            for sel in SUGGEST_SELS:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        clicked_suggestion = True
                        break
                except Exception:
                    pass

            if not clicked_suggestion:
                page.keyboard.press('Enter')

            page.wait_for_load_state('networkidle', timeout=25_000)
            page.wait_for_timeout(2_000)
            result['source_url'] = page.url

            # ── 5. If we're on a results page, click the first listing ─────────
            current_url = page.url
            on_listing_page = any(k in current_url for k in ['/listing/', '/property/', '/detail'])

            if not on_listing_page:
                listing_href = None
                for lsel in [
                    'a[href*="/listing/view/"]',
                    'a[href*="/listing/"]',
                    'a[href*="/property/"]',
                    '.listing-card a',
                    '.property-card a',
                    '.listing-item a',
                    '.result-item a',
                    'a[href*="detail"]',
                ]:
                    el = page.query_selector(lsel)
                    if el:
                        href = el.get_attribute('href') or ''
                        if href and href not in ('/', '#', ''):
                            listing_href = href
                            break

                if not listing_href:
                    # Check if any API response contains a listing URL
                    for resp in captured_api:
                        text = str(resp['data'])
                        m = re.search(r'(https?://[^\s"\']+(?:listing|property)[^\s"\']+)', text)
                        if m:
                            listing_href = m.group(1)
                            break

                if not listing_href:
                    result['notes'].append(
                        'No listing found for that address. '
                        'Make sure to include city, e.g. "123 Oak St, Boulder, CO".'
                    )
                    return result

                if not listing_href.startswith('http'):
                    listing_href = 'https://www.coloproperty.com' + listing_href

                page.goto(listing_href, wait_until='networkidle', timeout=30_000)
                page.wait_for_timeout(2_000)
                result['source_url'] = page.url

            # ── 6. Extract fields from the detail page ────────────────────────
            fields = extract_page_fields(page)

            # Also flatten any captured API responses into fields
            for resp in captured_api:
                _flatten_api_data(resp['data'], fields)

            if not fields:
                result['notes'].append(f'Reached page but found no data fields. URL: {page.url}')
                return result

            result['found'] = True
            result['raw_fields'] = fields

            # ── 7. Map to the four key fields ─────────────────────────────────
            result['property_size']     = best_match(fields, PROPERTY_SIZE_KEYS)
            result['zoning']            = best_match(fields, ZONING_KEYS)
            result['floor_area_ratio']  = best_match(fields, FAR_KEYS)
            result['building_coverage'] = best_match(fields, COVERAGE_KEYS)

            desc = best_match(fields, [
                'description', 'remarks', 'public remarks',
                'agent remarks', 'comments', 'listing remarks',
            ])
            far_hint, cov_hint = extract_description_hints(desc or '')
            if not result['floor_area_ratio'] and far_hint:
                result['floor_area_ratio'] = far_hint + ' (from listing description)'
            if not result['building_coverage'] and cov_hint:
                result['building_coverage'] = cov_hint + ' (from listing description)'

            if not result['floor_area_ratio']:
                result['notes'].append(
                    'FAR is not a standard MLS field — contact your local '
                    'county/city planning department for this value.'
                )
            if not result['building_coverage']:
                result['notes'].append(
                    'Building/lot coverage % is not a standard MLS field — '
                    'contact your local zoning office for this value.'
                )

        except PWTimeout as exc:
            result['error'] = f'Timed out: {exc}'
        except Exception as exc:
            result['error'] = str(exc)
        finally:
            browser.close()

    return result


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/lookup')
def api_lookup():
    address = request.args.get('address', '').strip()
    if not address:
        return jsonify({'error': 'Address is required'}), 400
    data = scrape_coloproperty(address)
    return jsonify(data)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
