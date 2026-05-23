"""
Cars.com car listing scraper.

Reverse-engineered approach:
  GET  https://www.cars.com/shopping/results/
       ?makes[]={make}&models[]={make_model}&...

The page is server-side rendered HTML.  Each listing is a <fuse-card>
web component with a data-vehicle-details JSON attribute containing all
listing fields.  Pagination uses the `page` query parameter.

Requires:  pip install cloudscraper
"""

import json
import re
import time
import html as _html

import cloudscraper

_BASE = "https://www.cars.com"
_SEARCH_URL = f"{_BASE}/shopping/results/"


def _make_scraper():
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "desktop": True},
        delay=10,
    )
    # Warm up session — Cars.com needs a prior visit to the homepage to set
    # cookies that satisfy the Cloudflare challenge on the search page.
    scraper.get(_BASE + "/", timeout=30)
    return scraper


def _parse_page(html: str) -> list[dict]:
    """Extract listings from a Cars.com search results HTML page."""
    # Each listing is a <fuse-card data-listing-id="..." data-vehicle-details="...">
    pattern = re.compile(
        r'data-listing-id="([^"]+)"[^>]*data-vehicle-details="([^"]+)"'
    )
    # The vehicledetail href is in a card-gallery element nearby
    href_pattern = re.compile(r'href="(/vehicledetail/([^/?]+)/[^"]*)"')

    # Build id → href map first
    id_to_href = {}
    for href_m in href_pattern.finditer(html):
        path = href_m.group(1)
        vid = href_m.group(2)
        if vid not in id_to_href:
            id_to_href[vid] = path

    listings = []
    seen = set()

    for m in pattern.finditer(html):
        listing_id = m.group(1)
        if listing_id in seen:
            continue
        seen.add(listing_id)

        try:
            details = json.loads(_html.unescape(m.group(2)))
        except (json.JSONDecodeError, ValueError):
            continue

        seller = details.get("seller") or {}
        href = id_to_href.get(listing_id, f"/vehicledetail/{listing_id}/")
        url = _BASE + href.split("?")[0].rstrip("/") + "/"

        price_raw = details.get("price")
        try:
            price = int(str(price_raw).replace(",", "")) if price_raw and str(price_raw) != "0" else None
        except (ValueError, TypeError):
            price = None

        mileage_raw = details.get("mileage")
        try:
            mileage = int(str(mileage_raw).replace(",", "")) if mileage_raw else None
        except (ValueError, TypeError):
            mileage = None

        year_raw = details.get("year")
        try:
            year = int(year_raw) if year_raw else None
        except (ValueError, TypeError):
            year = None

        listings.append(
            {
                "listingId": listing_id,
                "title": f"{year or ''} {details.get('make', '')} {details.get('model', '')} {details.get('trim', '')}".strip(),
                "year": year,
                "make": details.get("make"),
                "model": details.get("model"),
                "trim": details.get("trim"),
                "vin": details.get("vin"),
                "price": price,
                "mileage": mileage,
                "sellerZip": seller.get("zip"),
                "stockType": details.get("stockType"),
                "bodyStyle": details.get("bodyStyle"),
                "fuelType": details.get("fuelType"),
                "url": url,
                "thumbnailUrl": details.get("primaryThumbnail"),
                "source": "cars.com",
            }
        )

    return listings


# ── public API ────────────────────────────────────────────────────────────────

def fetch_listings(
    make,
    model_slug=None,
    zip_code=None,
    radius=500,
    min_year=None,
    max_year=None,
    min_price=None,
    max_price=None,
    max_miles=None,
    stock_type="used",      # "used" | "new" | "certified"
    page_size=20,
    max_pages=10,
    delay=2.0,
):
    """
    Fetch car listings from Cars.com.

    Args:
        make:        Make slug as Cars.com expects (e.g. "tesla", "toyota").
        model_slug:  Model slug in Cars.com format (e.g. "tesla_model_s",
                     "tesla_model_3").  Pass None to search all models.
        zip_code:    US ZIP code for the search centre.
        radius:      Search radius in miles.
        min_year:    Earliest model year.
        max_year:    Latest model year (not currently applied by Cars.com
                     search but included for completeness).
        min_price:   Minimum list price.
        max_price:   Maximum list price.
        max_miles:   Maximum mileage.
        stock_type:  "used", "new", or "certified".
        page_size:   Results per page (max ~100).
        max_pages:   Maximum pages to fetch.
        delay:       Seconds between requests.

    Returns:
        List of listing dicts with: listingId, title, year, make, model, trim,
        vin, price, mileage, sellerZip, stockType, bodyStyle, fuelType, url,
        thumbnailUrl, source.

    Note:
        Cars.com does not expose the dealer city/state in its SSR HTML; only the
        seller's ZIP code is available from the embedded data.
    """
    params = {
        "makes[]": make,
        "stock_type": stock_type,
        "page_size": page_size,
        "maximum_distance": radius,
    }
    if model_slug:
        params["models[]"] = model_slug
    if zip_code:
        params["zip"] = zip_code
    if min_year is not None:
        params["year_min"] = min_year
    if max_year is not None:
        params["year_max"] = max_year
    if min_price is not None:
        params["list_price_min"] = min_price
    if max_price is not None:
        params["list_price_max"] = max_price
    if max_miles is not None:
        params["mileage_max"] = max_miles

    print("  Initialising Cars.com session…")
    scraper = _make_scraper()
    time.sleep(1)

    all_listings = []
    seen_ids = set()

    for page in range(1, max_pages + 1):
        params["page"] = page
        print(f"  Cars.com page {page}…")

        try:
            resp = scraper.get(_SEARCH_URL, params=params, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"    Error: {e}")
            break

        if "_cf_chl" in resp.text or "just a moment" in resp.text.lower():
            print("    Cloudflare challenge hit — stopping.")
            break

        page_listings = _parse_page(resp.text)
        if not page_listings:
            if page == 1:
                print("    No listings found on page 1.")
            break

        new = [x for x in page_listings if x["listingId"] not in seen_ids]
        for x in new:
            seen_ids.add(x["listingId"])
        all_listings.extend(new)

        print(f"    {len(new)} new listings (running total: {len(all_listings)})")

        if len(page_listings) < page_size:
            break

        time.sleep(delay)

    return all_listings


# ── CLI demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Searching Cars.com for Tesla Model S…\n")
    listings = fetch_listings(
        make="tesla",
        model_slug="tesla_model_s",
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
        print(
            f"  {car.get('year','')} {car.get('make','')} {car.get('model','')} "
            f"– {price} – {miles} mi – ZIP {car.get('sellerZip','?')}"
        )
        print(f"    {car.get('url', '')}")
