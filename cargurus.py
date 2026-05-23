"""
CarGurus car listing scraper.

Reverse-engineered approach:
  GET  https://www.cargurus.com/Cars/inventorylisting/viewDetailsFilterViewInventoryListing.action
       ?zip=ZIP&distance=MILES&entitySelectingHelper.selectedEntity2=ENTITY_ID&...

CarGurus is protected by DataDome bot detection, which requires a real
browser to execute its JS challenge.  This scraper uses Playwright to
drive a headless Chromium instance and intercepts the AJAX calls that
carry listing JSON.

Requires:
    pip install playwright
    playwright install chromium

Known entity IDs (selectedEntity2):
    Tesla Model S   → d2246
    Tesla Model 3   → d2484
    Tesla Model X   → d2461
    Tesla Model Y   → d2535
    Toyota Camry    → d2171
    Toyota Camry Hybrid → d2172
    BMW 3 Series    → d110
    Honda Accord    → d218
    (look up others via: open the search URL in DevTools → Network →
     copy the entitySelectingHelper param from any XHR request)
"""

import time

_BASE = "https://www.cargurus.com"
_SEARCH_URL = (
    _BASE
    + "/Cars/inventorylisting/viewDetailsFilterViewInventoryListing.action"
)
_AJAX_URL_FRAG = "getInventoryListingResult.action"

# Sort options
SORT_PRICE_ASC   = ("PRICE", "ASC")
SORT_PRICE_DESC  = ("PRICE", "DESC")
SORT_MILEAGE_ASC = ("MILEAGE", "ASC")
SORT_NEWEST      = ("DATE", "DESC")
SORT_BEST_MATCH  = ("PRICE", "ASC")  # default


def _build_url(entity_id, zip_code, radius, min_year, max_year,
               min_price, max_price, max_miles, sort_type, sort_dir,
               offset=0):
    params = {
        "zip": zip_code,
        "distance": radius,
        "sortType": sort_type,
        "sortDir": sort_dir,
        "offset": offset,
        "maxResults": 15,
        "showNegotiable": "true",
        "sourceContext": "carGurusHomePageModel",
    }
    if entity_id:
        params["entitySelectingHelper.selectedEntity2"] = entity_id
    if min_year is not None:
        params["minYear"] = min_year
    if max_year is not None:
        params["maxYear"] = max_year
    if min_price is not None:
        params["minPrice"] = min_price
    if max_price is not None:
        params["maxPrice"] = max_price
    if max_miles is not None:
        params["maxMileage"] = max_miles

    from urllib.parse import urlencode
    return _SEARCH_URL + "?" + urlencode(params)


def _parse_listing(raw: dict) -> dict:
    """Normalise a CarGurus listing dict from the AJAX response."""
    price_raw = raw.get("lowestPrice") or raw.get("price") or raw.get("priceContext", {}).get("currentPrice")
    try:
        price = int(str(price_raw).replace("$", "").replace(",", "").strip()) if price_raw else None
    except (ValueError, TypeError):
        price = None

    mileage_raw = raw.get("mileage")
    try:
        mileage = int(str(mileage_raw).replace(",", "").strip()) if mileage_raw else None
    except (ValueError, TypeError):
        mileage = None

    listing_id = raw.get("id") or raw.get("listingId")
    make = raw.get("makeName", "")
    model = raw.get("modelName", "")
    year = raw.get("year") or raw.get("carYear")
    trim = raw.get("trimName") or raw.get("trim", "")
    title = raw.get("heading") or f"{year} {make} {model} {trim}".strip()

    local_url = raw.get("vdpUrl") or raw.get("url", "")
    url = (_BASE + local_url) if local_url.startswith("/") else local_url

    return {
        "listingId": listing_id,
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "trim": trim,
        "vin": raw.get("vin"),
        "price": price,
        "mileage": mileage,
        "dealerName": raw.get("seller", {}).get("name") if isinstance(raw.get("seller"), dict) else raw.get("sellerName"),
        "city": raw.get("city") or raw.get("localAreaName"),
        "state": raw.get("stateAbbreviation") or raw.get("state"),
        "zip": raw.get("zip") or raw.get("postalCode"),
        "distance": raw.get("distance"),
        "url": url,
        "source": "cargurus",
    }


# ── public API ────────────────────────────────────────────────────────────────

def fetch_listings(
    entity_id,
    zip_code,
    radius=500,
    min_year=None,
    max_year=None,
    min_price=None,
    max_price=None,
    max_miles=None,
    sort=SORT_PRICE_ASC,
    max_pages=10,
    delay=1.5,
):
    """
    Fetch car listings from CarGurus using a headless browser.

    Args:
        entity_id:   CarGurus vehicle entity ID (selectedEntity2), e.g. "d2246"
                     for Tesla Model S.  See module docstring for known IDs.
        zip_code:    US ZIP code.
        radius:      Search radius in miles (15 / 25 / 50 / 100 / 200 / 500).
        min_year:    Earliest model year.
        max_year:    Latest model year.
        min_price:   Minimum price.
        max_price:   Maximum price.
        max_miles:   Maximum mileage.
        sort:        Tuple of (sortType, sortDir), e.g. SORT_PRICE_ASC.
        max_pages:   Maximum pages to fetch (15 results per page).
        delay:       Seconds between page navigations.

    Returns:
        List of listing dicts with: listingId, title, year, make, model, trim,
        vin, price, mileage, dealerName, city, state, zip, distance, url, source.

    Raises:
        ImportError if playwright is not installed.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeoutError
    except ImportError:
        raise ImportError(
            "playwright is required for CarGurus scraping.\n"
            "Install it with:\n"
            "    pip install playwright\n"
            "    playwright install chromium"
        )

    sort_type, sort_dir = sort
    all_listings = []
    seen_ids = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = context.new_page()

        for pg in range(max_pages):
            offset = pg * 15
            url = _build_url(
                entity_id, zip_code, radius,
                min_year, max_year, min_price, max_price, max_miles,
                sort_type, sort_dir, offset,
            )
            print(f"  CarGurus page {pg + 1} (offset={offset})…")

            # Collect AJAX responses for this page load
            ajax_results = []

            def on_response(response):
                if _AJAX_URL_FRAG in response.url:
                    try:
                        data = response.json()
                        if isinstance(data, dict) and "listings" in data:
                            ajax_results.extend(data["listings"])
                        elif isinstance(data, list):
                            ajax_results.extend(data)
                    except Exception:
                        pass

            page.on("response", on_response)

            try:
                page.goto(url, wait_until="networkidle", timeout=30_000)
            except PwTimeoutError:
                pass  # networkidle can time out on slow pages — that's OK
            finally:
                page.remove_listener("response", on_response)

            # If AJAX interception produced nothing, fall back to HTML parsing
            if not ajax_results:
                ajax_results = _parse_from_html(page.content())

            if not ajax_results:
                print("    No listings on this page.")
                break

            new = []
            for raw in ajax_results:
                listing = _parse_listing(raw)
                lid = listing["listingId"]
                if lid and lid not in seen_ids:
                    seen_ids.add(lid)
                    new.append(listing)

            all_listings.extend(new)
            print(f"    {len(new)} new listings (running total: {len(all_listings)})")

            if len(ajax_results) < 15:
                break

            time.sleep(delay)

        browser.close()

    return all_listings


def _parse_from_html(html: str) -> list[dict]:
    """
    Fallback HTML parser for when AJAX interception misses the response.
    Extracts what it can from CarGurus' server-rendered listing markup.
    """
    import re
    listings = []

    # CarGurus embeds listing data in data-listing-id and nearby spans
    blocks = re.findall(
        r'data-listing-id="(\d+)"(.*?)(?=data-listing-id=|\Z)',
        html,
        re.DOTALL,
    )
    for lid, block in blocks:
        price_m = re.search(r'\$([0-9,]+)', block)
        miles_m = re.search(r'([0-9,]+)\s*mi', block, re.IGNORECASE)
        title_m = re.search(r'<h4[^>]*>(.*?)</h4>', block, re.DOTALL)
        title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ""

        listings.append({
            "id": lid,
            "heading": title,
            "lowestPrice": price_m.group(1).replace(",", "") if price_m else None,
            "mileage": miles_m.group(1).replace(",", "") if miles_m else None,
        })

    return listings


# ── CLI demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Searching CarGurus for Tesla Model S…\n")
    listings = fetch_listings(
        entity_id="d2246",      # Tesla Model S
        zip_code="06062",
        radius=500,
        min_year=2017,
        max_price=30000,
        max_miles=80000,
        max_pages=3,
    )

    print(f"\nFound {len(listings)} listings\n")
    for car in listings[:10]:
        miles = f"{car['mileage']:,}" if car.get("mileage") else "?"
        price = f"${car['price']:,}" if car.get("price") else "?"
        loc = f"{car.get('city') or '?'}, {car.get('state') or '?'}"
        print(
            f"  {car.get('year','')} {car.get('make','')} {car.get('model','')} "
            f"– {price} – {miles} mi – {loc}"
        )
        print(f"    {car.get('url', '')}")
