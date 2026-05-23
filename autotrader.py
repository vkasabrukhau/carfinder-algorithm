"""
AutoTrader car listing scraper.

Reverse-engineered approach:
  GET  https://www.autotrader.com/cars-for-sale/used-cars/{make}/{model}
       ?zip=ZIP&searchRadius=MILES&numRecords=25&firstRecord=0&...

The page is a Next.js SSR app.  All listing data is embedded in
  <script id="__NEXT_DATA__" type="application/json">...</script>
under props.pageProps.__eggsState.inventory (keyed by listing ID)
and props.pageProps.__eggsState.owners (keyed by owner/dealer ID).

Requires:  pip install cloudscraper
"""

import json
import re
import time

import cloudscraper

_BASE = "https://www.autotrader.com"
_SEARCH_PATH = "/cars-for-sale/used-cars/{make}/{model}"
_RECORDS_PER_PAGE = 25


def _make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "desktop": True}
    )


def _parse_page(html: str) -> tuple[list[dict], int]:
    """
    Extract listings and total result count from an AutoTrader search HTML page.

    Returns (listings, total_count).
    """
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return [], 0

    data = json.loads(m.group(1))
    eggs = data.get("props", {}).get("pageProps", {}).get("__eggsState", {})

    srp = eggs.get("srp_results", {})
    total = srp.get("count", 0) or 0
    active_ids = srp.get("activeResults", [])

    inventory = eggs.get("inventory", {})
    owners = eggs.get("owners", {})

    listings = []
    for lid in active_ids:
        raw = inventory.get(str(lid))
        if not raw:
            continue

        # Price
        pd = raw.get("pricingDetail") or {}
        price = pd.get("salePrice") or pd.get("incentive")

        # Mileage (stored as dict with 'value' string like "67,373")
        mil_obj = raw.get("mileage") or {}
        mileage_str = mil_obj.get("value", "") if isinstance(mil_obj, dict) else ""
        try:
            mileage = int(mileage_str.replace(",", ""))
        except (ValueError, AttributeError):
            mileage = None

        # Make / model / year
        make_obj = raw.get("make") or {}
        model_obj = raw.get("model") or {}
        trim_obj = raw.get("trim") or {}
        make_name = make_obj.get("name", "") if isinstance(make_obj, dict) else str(make_obj)
        model_name = model_obj.get("name", "") if isinstance(model_obj, dict) else str(model_obj)
        trim_name = trim_obj.get("name", "") if isinstance(trim_obj, dict) else str(trim_obj)
        year = raw.get("year")
        title = raw.get("titleLong") or raw.get("title") or f"{year} {make_name} {model_name} {trim_name}".strip()

        # Dealer / location
        owner_id = str(raw.get("ownerId", ""))
        owner = owners.get(owner_id, {})
        loc = (owner.get("location") or {}).get("address") or {}
        city = loc.get("city")
        state = loc.get("state")
        zip_code = loc.get("zip")
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        distance = (raw.get("marketExtension") or {}).get("distance")
        dealer_name = owner.get("name") or raw.get("ownerName")

        # URL
        url = f"{_BASE}/cars-for-sale/vehicle/{lid}"

        listings.append(
            {
                "listingId": lid,
                "title": title,
                "year": year,
                "make": make_name,
                "model": model_name,
                "trim": trim_name,
                "vin": raw.get("vin"),
                "price": price,
                "mileage": mileage,
                "city": city,
                "state": state,
                "zip": zip_code,
                "lat": lat,
                "lon": lon,
                "distance": distance,
                "dealerName": dealer_name,
                "listingType": raw.get("listingType"),
                "url": url,
                "source": "autotrader",
            }
        )

    return listings, total


# ── public API ────────────────────────────────────────────────────────────────

def fetch_listings(
    make,
    model,
    zip_code,
    radius=500,
    min_year=None,
    max_year=None,
    min_price=None,
    max_price=None,
    max_miles=None,
    condition="used",       # "used" | "new" | "certified"
    max_pages=10,
    delay=1.5,
):
    """
    Fetch car listings from AutoTrader.

    Args:
        make:       Make slug (e.g. "tesla", "toyota").
        model:      Model slug (e.g. "model-s", "camry").
        zip_code:   US ZIP code.
        radius:     Search radius in miles.
        min_year:   Earliest model year.
        max_year:   Latest model year.
        min_price:  Minimum price filter.
        max_price:  Maximum price filter.
        max_miles:  Maximum mileage.
        condition:  "used", "new", or "certified".
        max_pages:  Maximum pages to fetch.
        delay:      Seconds between requests.

    Returns:
        List of listing dicts with: listingId, title, year, make, model, trim,
        vin, price, mileage, city, state, zip, lat, lon, distance, dealerName,
        listingType, url, source.
    """
    scraper = _make_scraper()
    base_params = {
        "zip": zip_code,
        "searchRadius": radius,
        "numRecords": _RECORDS_PER_PAGE,
    }
    if min_year is not None:
        base_params["startYear"] = min_year
    if max_year is not None:
        base_params["endYear"] = max_year
    if min_price is not None:
        base_params["minPrice"] = min_price
    if max_price is not None:
        base_params["maxPrice"] = max_price
    if max_miles is not None:
        base_params["maxMileage"] = max_miles

    search_url = _BASE + _SEARCH_PATH.format(make=make.lower(), model=model.lower())

    all_listings = []
    seen_ids = set()
    first_record = 0

    for page in range(max_pages):
        params = {**base_params, "firstRecord": first_record}
        print(f"  AutoTrader page {page + 1} (firstRecord={first_record})…")

        try:
            resp = scraper.get(search_url, params=params, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"    Error: {e}")
            break

        page_listings, total = _parse_page(resp.text)

        if not page_listings:
            if page == 0:
                print("    No listings found (page may have been blocked).")
            break

        new = [x for x in page_listings if x["listingId"] not in seen_ids]
        for x in new:
            seen_ids.add(x["listingId"])
        all_listings.extend(new)

        print(f"    {len(new)} new listings (total so far: {len(all_listings)} / {total})")

        if len(all_listings) >= total or len(page_listings) < _RECORDS_PER_PAGE:
            break

        first_record += _RECORDS_PER_PAGE
        time.sleep(delay)

    return all_listings


# ── CLI demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Searching AutoTrader for Tesla Model S…\n")
    listings = fetch_listings(
        make="tesla",
        model="model-s",
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
        loc = f"{car.get('city') or '?'}, {car.get('state') or '?'}"
        dist = f"{car['distance']:.0f} mi away" if car.get("distance") else ""
        price_str = f"${car['price']:,}" if car.get("price") else "?"
        print(
            f"  {str(car.get('year','')):<4} {car.get('make',''):<8} {car.get('model',''):<12}"
            f"  {price_str:>10}  {miles:>8} mi  {loc}  {dist}"
        )
        print(f"    {car.get('url', '')}")
