"""
AutoTempest car listing scraper.


Reverse-engineered API:
  POST  https://www.autotempest.com/queue-results
  Each source is queried separately. A SHA-256 token is computed from the
  non-default search params + a server-side secret to authenticate requests.
"""

import hashlib
import urllib.parse
import requests
import time

# ── constants ────────────────────────────────────────────────────────────────

_BASE_URL = "https://www.autotempest.com"
_QUEUE_URL = f"{_BASE_URL}/queue-results"
_SECRET = "d8007486d73c168684860aae427ea1f9d74e502b06d94609691f5f4f2704a07f"

# Default param values as shipped in the page JS. Params equal to these are
# stripped before the token is computed (same logic as getNonDefaultArgs).
_DEFAULTS = {
    "alert_id": "", "bodystyle": "any", "city": "", "cylinders": "any",
    "doors": "any", "drive": "any", "exterior_color": "any", "exterior_kw": "",
    "filterDays": "", "fuel": "any", "haspic": "0", "interior_color": "any",
    "interior_kw": "", "keywords": "", "localization": "", "make": "",
    "make_kw": "", "make_model": "", "maxmiles": -1, "maxprice": -1,
    "maxyear": -1, "minmiles": -1, "minprice": -1, "minradius": -1,
    "minyear": -1, "model": "", "model_kw": "", "originalradius": -1,
    "radius": -1, "saleby": "any", "saletype": "any", "title": "any",
    "titlesonly": "0", "transmission": "any", "trim_kw": "", "zip": "",
    "domesticonly": "1", "searchAfter": "[]", "rpp": "", "sites": "",
    "deduplicationSites": "", "sort": "", "token": "", "highlight": "",
    "showPending": "0", "dark_mode": "", "partner": "", "search_origin": "web",
    "simplified": "0",
}

# Source codes → human names (type "mash" sources support the queue-results API)
SOURCES = {
    "te":  "AutoTempest",
    "hem": "Hemmings",
    "cs":  "CarSoup",
    "cv":  "Carvana",
    "cm":  "Cars.com",
    "eb":  "eBay",
    "ot":  "Other",
}

_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ── token computation ─────────────────────────────────────────────────────────

def _build_params(search: dict, sites: str) -> dict:
    """Return the ordered non-default params dict, mirroring getNonDefaultArgs."""
    # searchParams key order as they appear in the page (insertion order matters
    # because jQuery.param serialises in insertion order, and the SHA-256 is
    # computed from that serialised string).
    ordered_keys = [
        "alert_id", "bodystyle", "city", "domesticonly", "doors", "drive",
        "fuel", "exterior_color", "interior_color", "cylinders", "filterDays",
        "haspic", "keywords", "localization", "make_model", "make", "make_kw",
        "model", "model_kw", "minmiles", "maxmiles", "minyear", "maxyear",
        "minradius", "radius", "originalradius", "saleby", "saletype", "title",
        "titlesonly", "transmission", "trim_kw", "exterior_kw", "interior_kw",
        "zip", "minprice", "maxprice", "dark_mode", "search_origin",
        "simplified", "sort",
        # appended last by setSearchParams
        "sites",
    ]

    # Start from defaults, overlay caller-supplied values
    full = {**_DEFAULTS, **search}
    # originalradius mirrors radius (set in setSearchParams before queueResults)
    full["originalradius"] = full.get("radius", -1)
    full["sites"] = sites

    # Keep only non-default params (strict equality, matching JS ===)
    result = {}
    for k in ordered_keys:
        if k not in full:
            continue
        v = full[k]
        if str(v) != str(_DEFAULTS.get(k, object())):
            result[k] = v

    return result


def _compute_token(params: dict) -> str:
    """SHA-256(decodeURIComponent(jQuery.param(params)) + secret)."""
    qs = "&".join(
        f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items()
    )
    decoded = urllib.parse.unquote(qs)
    return hashlib.sha256((decoded + _SECRET).encode()).hexdigest()


# ── public API ────────────────────────────────────────────────────────────────

def fetch_listings(
    make,           # str
    model,          # str
    zip_code,       # str
    radius=500,
    min_year=None,
    max_year=None,
    min_price=None,
    max_price=None,
    min_miles=None,
    max_miles=None,
    domestic_only=False,
    sort="best_match",
    sources=None,
    max_pages=3,
    delay=0.5,
):
    """
    Fetch car listings from AutoTempest.

    Args:
        make:          Car make slug (e.g. "tesla", "toyota").
        model:         Model slug as AutoTempest expects (e.g. "models", "camry").
        zip_code:      US ZIP code for the search centre.
        radius:        Search radius in miles.
        min_year:      Earliest model year to include.
        max_year:      Latest model year to include.
        min_price:     Minimum price filter.
        max_price:     Maximum price filter.
        min_miles:     Minimum mileage filter.
        max_miles:     Maximum mileage filter.
        domestic_only: Restrict to US listings only.
        sort:          Sort order ("best_match", "price_asc", "price_desc",
                       "mileage_asc", "date_desc").
        sources:       List of source codes to query; defaults to all SOURCES.
        max_pages:     Maximum pages to fetch per source (25 results/page).
        delay:         Seconds to wait between requests.

    Returns:
        Flat list of listing dicts. Each dict includes at minimum:
          title, price, mileage, year, make, model, location, distance,
          url, vin, sitecode, sourceName, date.
    """
    search = {
        "make": make,
        "model": model,
        "zip": zip_code,
        "radius": radius,
        "domesticonly": "0" if not domestic_only else "1",
        "sort": sort,
    }
    if min_year is not None:
        search["minyear"] = min_year
    if max_year is not None:
        search["maxyear"] = max_year
    if min_price is not None:
        search["minprice"] = min_price
    if max_price is not None:
        search["maxprice"] = max_price
    if min_miles is not None:
        search["minmiles"] = min_miles
    if max_miles is not None:
        search["maxmiles"] = max_miles

    target_sources = sources or list(SOURCES.keys())
    referer = (
        f"{_BASE_URL}/results?"
        + urllib.parse.urlencode({k: v for k, v in search.items()})
    )
    session = requests.Session()
    session.headers.update({**_HEADERS, "Referer": referer})

    all_listings: list[dict] = []

    for site_code in target_sources:
        print(f"  Fetching {SOURCES.get(site_code, site_code)} ({site_code})…")
        page = 1
        while page <= max_pages:
            params = _build_params({**search, "page": page}, site_code)
            params["token"] = _compute_token(
                {k: v for k, v in params.items() if k != "token"}
            )

            resp = session.get(_QUEUE_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            status = data.get("status")
            results = data.get("results", [])

            if status == -2:
                print(f"    Token error: {data.get('errors')}")
                break
            if status == -1 or not results:
                break

            all_listings.extend(results)
            print(f"    Page {page}: {len(results)} listings (total {len(all_listings)})")

            # status 1 = last page
            if status == 1:
                break

            page += 1
            time.sleep(delay)

        time.sleep(delay)

    return all_listings


# ── CLI demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Searching for Tesla Model S listings…\n")
    listings = fetch_listings(
        make="tesla",
        model="models",
        zip_code="06062",
        radius=500,
        min_year=2017,
        max_price=30000,
        max_miles=80000,
        domestic_only=False,
        sources=["cm", "te", "eb"],  # Cars.com, AutoTempest, eBay
        max_pages=2,
    )

    print(f"\nFound {len(listings)} total listings\n")
    for car in listings[:10]:
        print(
            f"  {car.get('year')} {car.get('make')} {car.get('model')} "
            f"– {car.get('price')} – {car.get('mileage')} mi "
            f"– {car.get('location')} ({car.get('distance', '?')} mi away)"
        )
        print(f"    {car.get('url', '')[:80]}")
