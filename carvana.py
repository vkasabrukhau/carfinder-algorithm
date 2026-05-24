"""
Carvana car listing scraper.

Reverse-engineered approach:
  GET  https://www.carvana.com/cars/filters?cvnaid={ENCODED_FILTERS}&page={N}

The page uses Next.js App Router with React Server Components.  Listing data
is embedded in a self.__next_f.push([1, "..."]) script tag.

Filter state is encoded as:
    cvnaid = base64( JSON.stringify({filters, sortBy}) ).replace(/=/g, "")

Pagination info lives at:
    forInventoryContext.inventoryData.inventory.pagination
    → { currentPage, pageSize, totalMatchedInventory, totalMatchedPages }

Requires:  pip install cloudscraper
"""

import base64
import json
import re
import time

import cloudscraper

_BASE = "https://www.carvana.com"
_SEARCH_URL = f"{_BASE}/cars/filters"

# Sort keys
SORT_MOST_POPULAR  = "MostPopular"
SORT_PRICE_LOW     = "LowestPrice"
SORT_PRICE_HIGH    = "HighestPrice"
SORT_MILEAGE_LOW   = "LowestMileage"
SORT_NEWEST_YEAR   = "NewestYear"
SORT_NEWEST_LISTED = "NewestInventory"


# ── filter encoding ───────────────────────────────────────────────────────────

def _make_cvnaid(filters: dict, sort_by: str = SORT_MOST_POPULAR) -> str:
    """
    Encode a filter dict to the cvnaid URL parameter.

    The encoding mirrors Carvana's JS:
        btoa(JSON.stringify({filters, sortBy})).replace(/=/g, "")
    """
    payload = {"filters": filters, "sortBy": sort_by}
    return (
        base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        .decode("ascii")
        .replace("=", "")
    )


def build_filters(
    make=None,
    model=None,
    min_year=None,
    max_year=None,
    min_price=None,
    max_price=None,
    max_miles=None,
    min_miles=None,
    body_styles=None,       # list of strings e.g. ["suv", "sedan"]
    fuel_types=None,        # list of strings e.g. ["electric", "hybrid"]
) -> dict:
    """
    Build a Carvana filter dict from human-readable parameters.

    Args:
        make:         Make name (e.g. "Tesla", "Toyota").  Case-sensitive as
                      Carvana returns it — use title-case.
        model:        Model name (e.g. "Model S", "Camry").
        min_year:     Earliest model year.
        max_year:     Latest model year.
        min_price:    Minimum price ($).
        max_price:    Maximum price ($).
        max_miles:    Maximum mileage.
        min_miles:    Minimum mileage.
        body_styles:  List of body style slugs e.g. ["suv", "sedan", "coupe",
                      "pickup-truck", "convertible", "wagon", "minivan"].
        fuel_types:   List of fuel type slugs e.g. ["electric", "gas",
                      "hybrid", "plug-in-hybrid"].

    Returns:
        Filter dict suitable for passing to fetch_listings() or _make_cvnaid().
    """
    f = {}

    if make:
        make_entry = {"name": make}
        if model:
            make_entry["parentModels"] = [{"name": model}]
        f["makes"] = [make_entry]

    if min_year is not None or max_year is not None:
        f["year"] = {
            "min": min_year if min_year is not None else 1900,
            "max": max_year if max_year is not None else 2100,
        }

    if min_price is not None or max_price is not None:
        f["price"] = {
            "min": min_price if min_price is not None else 0,
            "max": max_price if max_price is not None else 999999,
        }

    if min_miles is not None or max_miles is not None:
        f["mileage"] = {
            "min": min_miles if min_miles is not None else 0,
            "max": max_miles if max_miles is not None else 999999,
        }

    if body_styles:
        f["bodyStyles"] = [{"key": s} for s in body_styles]

    if fuel_types:
        f["fuelTypes"] = [{"key": t} for t in fuel_types]

    return f


# ── page parser ───────────────────────────────────────────────────────────────

def _parse_page(html: str) -> tuple[list[dict], dict]:
    """
    Extract (vehicles, pagination) from a Carvana search results HTML page.

    Returns:
        vehicles   — list of raw vehicle dicts from the payload
        pagination — {"currentPage", "pageSize", "totalMatchedInventory",
                      "totalMatchedPages"}
    """
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    big = [s for s in scripts if len(s) > 100_000]
    if not big:
        return [], {}

    m = re.match(
        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', big[0], re.DOTALL
    )
    if not m:
        return [], {}

    decoded = json.loads('"' + m.group(1) + '"')

    # Vehicles array
    veh_start = decoded.find('"vehicles":[{')
    if veh_start < 0:
        return [], {}
    br = decoded.find("[{", veh_start)
    depth = 0
    i = br
    while i < len(decoded):
        if decoded[i] == "[":
            depth += 1
        elif decoded[i] == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    try:
        vehicles = json.loads(decoded[br : i + 1])
    except (json.JSONDecodeError, ValueError):
        return [], {}

    # Pagination object
    pag_idx = decoded.find('"pagination":{"currentPage"')
    pagination = {}
    if pag_idx >= 0:
        obj_start = decoded.find("{", pag_idx + len('"pagination":'))
        depth = 0
        j = obj_start
        while j < len(decoded):
            if decoded[j] == "{":
                depth += 1
            elif decoded[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        try:
            pagination = json.loads(decoded[obj_start : j + 1])
        except (json.JSONDecodeError, ValueError):
            pass

    return vehicles, pagination


def _normalise(raw: dict) -> dict:
    """Convert a raw Carvana vehicle dict to a clean listing dict."""
    price_obj = raw.get("price") or {}
    price = price_obj.get("total") or price_obj.get("incentivizedPrice")

    vdp_slug = raw.get("vdpSlug") or ""
    url = f"{_BASE}/vehicle/{vdp_slug}" if vdp_slug else None

    return {
        "listingId":   raw.get("stockNumber"),
        "vehicleId":   raw.get("vehicleId"),
        "title":       f"{raw.get('year', '')} {raw.get('make', '')} {raw.get('model', '')} {raw.get('trim', '')}".strip(),
        "year":        raw.get("year"),
        "make":        raw.get("make"),
        "model":       raw.get("model"),
        "trim":        raw.get("trim"),
        "vin":         raw.get("vin"),
        "price":       price,
        "mileage":     raw.get("mileage"),
        "color":       raw.get("color"),
        "bodyStyle":   raw.get("bodyStyle"),
        "fuelType":    raw.get("fuelType"),
        "storeKey":    raw.get("storeKey"),
        "locationId":  raw.get("locationId"),
        "url":         url,
        "imageUrl":    raw.get("imageUrl"),
        "source":      "carvana",
    }


# ── public API ────────────────────────────────────────────────────────────────

def fetch_listings(
    filters=None,
    make=None,
    model=None,
    min_year=None,
    max_year=None,
    min_price=None,
    max_price=None,
    max_miles=None,
    min_miles=None,
    body_styles=None,
    fuel_types=None,
    sort_by=SORT_MOST_POPULAR,
    max_pages=50,
    delay=1.5,
):
    """
    Fetch car listings from Carvana.

    Can be called in two ways:

    1. Pass a pre-built filter dict:
           fetch_listings(filters=build_filters("Tesla", "Model S", max_price=30000))

    2. Pass filter parameters directly:
           fetch_listings(make="Tesla", model="Model S", max_price=30000)

    Args:
        filters:     Pre-built filter dict from build_filters().  If provided,
                     the individual keyword args below are ignored.
        make:        Make name (title-case, e.g. "Tesla").
        model:       Model name (e.g. "Model S", "Model 3").
        min_year:    Earliest model year.
        max_year:    Latest model year.
        min_price:   Minimum price.
        max_price:   Maximum price.
        max_miles:   Maximum mileage.
        min_miles:   Minimum mileage.
        body_styles: List of body style slugs (e.g. ["suv", "sedan"]).
        fuel_types:  List of fuel type slugs (e.g. ["electric"]).
        sort_by:     Sort order constant (e.g. SORT_PRICE_LOW).
        max_pages:   Maximum pages to fetch (~24 results per page).
        delay:       Seconds between requests.

    Returns:
        List of listing dicts with: listingId, vehicleId, title, year, make,
        model, trim, vin, price, mileage, color, bodyStyle, fuelType,
        storeKey, locationId, url, imageUrl, source.

    Note:
        Carvana is a nationwide online retailer — all cars can be delivered
        anywhere in the US.  There is no "distance" filter; the storeKey /
        locationId fields identify the Carvana hub the car ships from.
    """
    if filters is None:
        filters = build_filters(
            make=make, model=model,
            min_year=min_year, max_year=max_year,
            min_price=min_price, max_price=max_price,
            max_miles=max_miles, min_miles=min_miles,
            body_styles=body_styles, fuel_types=fuel_types,
        )

    cvnaid = _make_cvnaid(filters, sort_by)

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "desktop": True},
        delay=10,
    )
    scraper.get(_BASE + "/", timeout=30)
    time.sleep(1)

    all_listings = []
    seen_ids = set()
    total_pages = None

    for page in range(1, max_pages + 1):
        params = {"cvnaid": cvnaid, "page": page}
        print(f"  Carvana page {page}{f' / {total_pages}' if total_pages else ''}…")

        try:
            resp = scraper.get(_SEARCH_URL, params=params, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"    Error: {e}")
            break

        vehicles, pagination = _parse_page(resp.text)

        if not vehicles:
            print("    No vehicles on this page — stopping.")
            break

        if not total_pages and pagination:
            total_pages = pagination.get("totalMatchedPages")
            total_inv   = pagination.get("totalMatchedInventory")
            if total_inv is not None:
                print(f"    Total matching inventory: {total_inv} across {total_pages} pages")

        new = []
        for raw in vehicles:
            lid = raw.get("stockNumber")
            if lid and lid not in seen_ids:
                seen_ids.add(lid)
                new.append(_normalise(raw))

        all_listings.extend(new)
        print(f"    {len(new)} new listings (running total: {len(all_listings)})")

        if total_pages and page >= total_pages:
            break

        time.sleep(delay)

    return all_listings


# ── CLI demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Searching Carvana for Tesla under $30k, 2017+, under 80k miles…\n")
    listings = fetch_listings(
        make="Tesla",
        min_year=2017,
        max_price=30000,
        max_miles=80000,
        sort_by=SORT_PRICE_LOW,
        max_pages=3,
    )

    print(f"\nFound {len(listings)} listings\n")
    for car in listings[:10]:
        miles = f"{car['mileage']:,}" if car.get("mileage") else "?"
        price = f"${car['price']:,}" if car.get("price") else "?"
        print(
            f"  {car.get('year','')} {car.get('make','')} {car.get('model','')} {car.get('trim','')}"
            f" – {price} – {miles} mi – {car.get('color','')}"
        )
        print(f"    {car.get('url', '')}")
