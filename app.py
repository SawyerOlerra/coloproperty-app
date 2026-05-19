"""
ColoProperty.com Property Lookup — Flask backend
Zoning · Property Size · Floor Area Ratio · Building Coverage
"""

import re
from flask import Flask, request, jsonify, render_template
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

app = Flask(__name__)


# ─── Field-matching keys ──────────────────────────────────────────────────────

PROPERTY_SIZE_KEYS = [
    'lot size', 'lot sq ft', 'lot sqft', 'lot area', 'total acres', 'acreage',
    'land area', 'land acres', 'land sq ft', 'parcel size', 'lot dimensions',
    'approx lot size', 'approx acreage', 'total lot', 'lot size sq', 'sq ft lot',
    'living area', 'total sq ft', 'total sqft', 'square feet', 'square footage',
    'bldg sq ft', 'finished sq ft', 'above grade sq ft',
]
ZONING_KEYS = [
    'zoning', 'zoning type', 'zone', 'zoning class', 'zoning code',
    'zoning desc', 'land use', 'property use', 'use code', 'land zone',
    'zoning description', 'use type',
]
FAR_KEYS = [
    'floor area ratio', 'far', 'f.a.r', 'floor/area', 'floor-area ratio',
    'max floor area', 'flr area ratio', 'floor area ratio (far)',
]
COVERAGE_KEYS = [
    'building coverage', 'lot coverage', 'coverage ratio', 'coverage %',
    'surface area coverage', 'impervious coverage', 'impervious surface',
    'max coverage', 'building footprint', 'footprint coverage', 'max lot coverage',
]


def best_match(fields: dict, keys: list) -> str | None:
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
    m = re.search(
        r'\b(?:FAR|floor\s*area\s*ratio)\s*[=:of\s]+([0-9]+\.?[0-9]*)',
        description, re.I,
    )
    if m:
        far = m.group(1)
    m = re.search(
        r'\b(?:lot|building|site|impervious)\s*coverage\s*[=:of\s]+([0-9]+\.?[0-9]*\s*%?)',
        description, re.I,
    )
    if m:
        cov = m.group(1)
    return far, cov


# ─── Playwright scraper ───────────────────────────────────────────────────────

def scrape_coloproperty(address: str) -> dict:
    result: dict = {
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
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            viewport={'width': 1280, 'height': 900},
        )
        page = ctx.new_page()

        try:
            page.goto(
                'https://www.coloproperty.com/',
                timeout=30_000,
                wait_until='domcontentloaded',
            )
            page.wait_for_timeout(3_500)

            SEARCH_SELS = [
                'input[placeholder*="Address"]',
                'input[placeholder*="City"]',
                'input[placeholder*="MLS"]',
                'input[placeholder*="Zip"]',
                'input[placeholder*="address"]',
                'input[placeholder*="city"]',
                'input[type="search"]',
                'form input[type="text"]',
                'input[name="q"]',
                'input[id*="search"]',
                '#searchInput',
            ]
            search_el = None
            for sel in SEARCH_SELS:
                el = page.query_selector(sel)
                if el:
                    try:
                        if el.is_visible():
                            search_el = el
                            break
                    except Exception:
                        pass

            if search_el is None:
                for el in page.query_selector_all('input[type="text"]'):
                    try:
                        if el.is_visible():
                            search_el = el
                            break
                    except Exception:
                        pass

            if search_el is None:
                result['notes'].append(
                    'Could not find the search input on ColoProperty.com. '
                    'The site layout may have changed.'
                )
                return result

            search_el.click()
            search_el.fill(address)
            page.wait_for_timeout(700)

            btn = None
            for bsel in [
                'button[type="submit"]',
                'button:has-text("Search")',
                'input[type="submit"]',
                '.btn-search', '#search-btn', '.search-button',
                'button:has-text("Go")',
            ]:
                b = page.query_selector(bsel)
                if b:
                    try:
                        if b.is_visible():
                            btn = b
                            break
                    except Exception:
                        pass

            if btn:
                btn.click()
            else:
                page.keyboard.press('Enter')

            page.wait_for_load_state('networkidle', timeout=20_000)
            result['source_url'] = page.url

            listing_href = None
            for lsel in [
                'a[href*="/listing/view/"]',
                'a[href*="/listing/"]',
                'a[href*="/property/"]',
                '.listing-card a',
                '.property-card a',
                '.listing-item a',
                '.result-item a',
                '.results a',
                '.listing a',
            ]:
                el = page.query_selector(lsel)
                if el:
                    href = el.get_attribute('href') or ''
                    if href and href not in ('/', '#', ''):
                        listing_href = href
                        break

            if not listing_href:
                result['notes'].append(
                    'No MLS listings found for that address. '
                    'Try the full address with city, '
                    'e.g. "123 Main St Fort Collins CO".'
                )
                return result

            if not listing_href.startswith('http'):
                listing_href = 'https://www.coloproperty.com' + listing_href

            page.goto(listing_href, wait_until='networkidle', timeout=30_000)
            page.wait_for_timeout(2_000)
            result['source_url'] = page.url

            fields: dict[str, str] = {}

            for row in page.query_selector_all('table tr'):
                cells = row.query_selector_all('td, th')
                if len(cells) >= 2:
                    label = cells[0].inner_text().strip().lower().rstrip(':').strip()
                    value = cells[1].inner_text().strip()
                    if label and value and len(label) < 60:
                        fields[label] = value

            for dt in page.query_selector_all('dl dt'):
                try:
                    label = dt.inner_text().strip().lower().rstrip(':').strip()
                    dd = dt.evaluate('el => el.nextElementSibling')
                    if dd:
                        value = page.evaluate('el => el ? el.innerText : ""', dd).strip()
                        if label and value and len(label) < 60:
                            fields.setdefault(label, value)
                except Exception:
                    pass

            for wrapper in page.query_selector_all(
                '.field, .detail-field, .property-field, .listing-field, '
                '.data-row, .info-row, [class*="field-row"], [class*="detail-row"]'
            ):
                try:
                    children = wrapper.query_selector_all('*')
                    texts = []
                    for c in children:
                        t = c.inner_text().strip()
                        if t:
                            texts.append(t)
                    if len(texts) >= 2:
                        label = texts[0].lower().rstrip(':').strip()
                        value = texts[1]
                        if label and value and len(label) < 60:
                            fields.setdefault(label, value)
                except Exception:
                    pass

            for lbl_el in page.query_selector_all(
                '.label, .field-label, [class*="-label"], [class*="label-"]'
            ):
                try:
                    label = lbl_el.inner_text().strip().lower().rstrip(':').strip()
                    if not label or len(label) > 60:
                        continue
                    val_el = lbl_el.evaluate(
                        'el => el.nextElementSibling || '
                        '(el.parentElement && el.parentElement.querySelector('
                        '".value, [class*=\\"-value\\"]"))'
                    )
                    if val_el:
                        value = page.evaluate(
                            'el => el ? el.innerText : ""', val_el
                        ).strip()
                        if value:
                            fields.setdefault(label, value)
                except Exception:
                    pass

            result['raw_fields'] = fields
            result['found'] = True

            result['property_size']     = best_match(fields, PROPERTY_SIZE_KEYS)
            result['zoning']            = best_match(fields, ZONING_KEYS)
            result['floor_area_ratio']  = best_match(fields, FAR_KEYS)
            result['building_coverage'] = best_match(fields, COVERAGE_KEYS)

            desc = best_match(
                fields,
                ['description', 'remarks', 'public remarks', 'agent remarks',
                 'comments', 'notes', 'listing remarks'],
            )
            far_hint, cov_hint = extract_description_hints(desc or '')
            if not result['floor_area_ratio'] and far_hint:
                result['floor_area_ratio'] = far_hint + ' (found in listing description)'
            if not result['building_coverage'] and cov_hint:
                result['building_coverage'] = cov_hint + ' (found in listing description)'

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
            result['error'] = f'Timed out while loading ColoProperty.com: {exc}'
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
