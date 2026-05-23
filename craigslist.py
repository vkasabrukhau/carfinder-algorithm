"""
Craigslist car listing scraper — New England edition.

Reverse-engineered API:
  GET  https://sapi.craigslist.org/web/v8/postings/search/full

Items are returned as compressed arrays and decoded using the `decode`
section of each response.  Pagination uses a `cacheId` + batch offset
for large result sets.
"""

import datetime
import math
import requests
import time
import urllib.parse

# ── New England areas ─────────────────────────────────────────────────────────

# Craigslist subdomain → area ID (needed for the batch parameter)
NEW_ENGLAND_AREAS = {
    # Connecticut
    "hartford":    44,
    "newhaven":    168,
    "newlondon":   281,
    "nwct":        354,
    # Massachusetts
    "boston":      4,
    "worcester":   240,
    "capecod":     239,
    "southcoast":  378,
    "westernmass": 173,
    # Maine
    "maine":       169,
    # New Hampshire
    "nh":          198,
    # Rhode Island
    "providence":  38,
    # Vermont
    "vermont":     93,
}

# ── category constants ────────────────────────────────────────────────────────

# Craigslist categoryId → URL abbreviation (from bundle analysis)
_CAT_ABBR = {
    145: "cto",  # cars & trucks – by owner
    146: "ctd",  # cars & trucks – by dealer
}

_SAPI_BASE = "https://sapi.craigslist.org/web/v8/postings/search/full"

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ── item decoder ──────────────────────────────────────────────────────────────

# Variant type codes from the search bundle (fm() function)
_V_UUID      = 13
_V_IMAGE_IDS = 4
_V_SEO       = 6
_V_MILES     = 9
_V_PRICE_STR = 10


def _decode_items(raw_items, decode):
    """Convert compressed item arrays to dicts using the decode lookup tables."""
    if not isinstance(decode, dict) or not raw_items:
        return []
    min_pid  = decode.get("minPostingId", 0)
    min_date = decode.get("minPostedDate", 0)
    locations       = decode.get("locations", [])
    loc_descs       = decode.get("locationDescriptions", [])
    neighborhoods   = decode.get("neighborhoods", [])

    # Pre-process locations list into dicts
    parsed_locs = []
    for loc in locations:
        if isinstance(loc, list) and loc:
            area_id, hostname = loc[0], loc[1]
            subarea = loc[2] if len(loc) > 2 else None
            parsed_locs.append({"areaId": area_id, "hostname": hostname, "subarea": subarea})
        else:
            parsed_locs.append(None)

    results = []
    for t in raw_items:
        try:
            posting_id  = min_pid + t[0]
            posted_ts   = min_date + t[1]
            category_id = t[2]
            price       = t[3] if t[3] != -1 else None
            geo_str     = t[4]  # "locIdx:descIdx~lat~lon" or ":descIdx:neighIdx~lat~lon"
            first_img   = t[5]

            # Parse location
            geo_parts  = geo_str.split("~")
            loc_parts  = geo_parts[0].split(":")
            loc_idx    = int(loc_parts[0]) if loc_parts[0] else 0
            desc_idx   = int(loc_parts[1]) if len(loc_parts) > 1 and loc_parts[1] else 0
            neigh_idx  = int(loc_parts[2]) if len(loc_parts) > 2 and loc_parts[2] else 0
            lat = float(geo_parts[1]) if len(geo_parts) > 1 else None
            lon = float(geo_parts[2]) if len(geo_parts) > 2 else None

            location_info = parsed_locs[loc_idx] if loc_idx < len(parsed_locs) else None
            hostname = location_info["hostname"] if location_info else "craigslist"
            subarea  = location_info.get("subarea") if location_info else None

            loc_desc = loc_descs[desc_idx] if desc_idx < len(loc_descs) else ""
            neighborhood = neighborhoods[neigh_idx] if neigh_idx and neigh_idx < len(neighborhoods) else ""

            cat_abbr = _CAT_ABBR.get(category_id, "cta")

            item = {
                "postingId":   posting_id,
                "postedDate":  datetime.datetime.fromtimestamp(posted_ts).isoformat(),
                "categoryId":  category_id,
                "categoryAbbr": cat_abbr,
                "price":       price,
                "location":    loc_desc,
                "neighborhood": neighborhood,
                "lat":         lat,
                "lon":         lon,
                "hostname":    hostname,
                "subarea":     subarea,
                "title":       None,
                "mileage":     None,
                "priceStr":    None,
                "uuid":        None,
                "seo":         None,
                "imageIds":    [],
                "url":         None,
                "dedupeKey":   None,
            }

            # Decode variable-length suffix elements
            remaining = t[6:]
            for el in remaining:
                if isinstance(el, str):
                    item["title"] = el
                elif isinstance(el, list) and el:
                    vtype = el[0]
                    if vtype == _V_UUID and len(el) > 1:
                        item["uuid"] = el[1]
                    elif vtype == _V_IMAGE_IDS and len(el) > 1:
                        item["imageIds"] = el[1:]
                    elif vtype == _V_SEO and len(el) > 1:
                        item["seo"] = el[1]
                    elif vtype == _V_MILES and len(el) > 1:
                        item["mileage"] = el[1]
                    elif vtype == _V_PRICE_STR and len(el) > 1:
                        item["priceStr"] = el[1]
                elif isinstance(el, int) and el < 0:
                    # Negative int = dedupeKey: same vehicle posted multiple times
                    item["dedupeKey"] = el

            # Construct listing URL
            seo  = item["seo"] or "listing"
            sub  = f"{subarea}/" if subarea else ""
            item["url"] = (
                f"https://{hostname}.craigslist.org/"
                f"{sub}{cat_abbr}/d/{seo}/{posting_id}.html"
            )

            results.append(item)
        except Exception:
            continue

    return results


def _dedupe_by_key(listings):
    """
    Within a single area's result set, collapse same-vehicle duplicates.

    Craigslist encodes a negative `dedupeKey` on postings that represent the
    same physical vehicle listed multiple times by the same dealer.  Among each
    group we keep only the most recently posted one, matching the browser's own
    bm() deduplication logic.  Listings without a dedupeKey are passed through
    unchanged.
    """
    groups = {}   # dedupeKey → [listing, ...]
    singles = []

    for listing in listings:
        key = listing.get("dedupeKey")
        if key is not None:
            groups.setdefault(key, []).append(listing)
        else:
            singles.append(listing)

    kept = []
    for group in groups.values():
        group.sort(key=lambda x: x["postedDate"], reverse=True)
        kept.append(group[0])

    return singles + kept


# ── public API ────────────────────────────────────────────────────────────────

def fetch_listings(
    areas=None,
    make_model=None,
    min_price=None,
    max_price=None,
    min_year=None,
    max_year=None,
    max_miles=None,
    min_miles=None,
    seller_type=None,   # "owner", "dealer", or None for all
    query=None,
    category="cta",
    max_results=None,
    delay=1.0,
):
    """
    Fetch car listings from Craigslist across New England.

    Args:
        areas:        List of subdomain strings to search, e.g. ["boston", "hartford"].
                      Defaults to all New England areas.
        make_model:   Free-text make/model search (e.g. "tesla model s").
                      Pass None to return ALL cars.
        min_price:    Minimum price filter.
        max_price:    Maximum price filter.
        min_year:     Minimum model year.
        max_year:     Maximum model year.
        max_miles:    Maximum mileage.
        min_miles:    Minimum mileage.
        seller_type:  "owner" / "dealer" / None (all).
        query:        General keyword query (title + body).
        category:     Craigslist category abbreviation ("cta"=all cars, "cto"=owner,
                      "ctd"=dealer, "suvs", "electric-cars", "pickups-trucks",
                      "classic-cars").
        max_results:  Stop after this many total listings (across all areas).
        delay:        Seconds to wait between requests.

    Returns:
        List of listing dicts with: postingId, title, price, priceStr, mileage,
        location, lat, lon, hostname, url, postedDate, categoryAbbr, uuid.
    """
    target_areas = areas or list(NEW_ENGLAND_AREAS.keys())

    # Build search filter params (mirrors Craigslist URL query params)
    filters = {}
    if make_model:
        filters["auto_make_model"] = make_model
    if min_price is not None:
        filters["min_price"] = min_price
    if max_price is not None:
        filters["max_price"] = max_price
    if min_year is not None:
        filters["min_auto_year"] = min_year
    if max_year is not None:
        filters["max_auto_year"] = max_year
    if max_miles is not None:
        filters["max_auto_miles"] = max_miles
    if min_miles is not None:
        filters["min_auto_miles"] = min_miles
    if seller_type == "owner":
        filters["purveyor"] = "owner"
    elif seller_type == "dealer":
        filters["purveyor"] = "dealer"
    if query:
        filters["query"] = query

    session = requests.Session()
    session.headers.update(_HEADERS)

    all_listings = []
    seen_posting_ids = set()   # cross-area deduplication

    for subdomain in target_areas:
        area_id = NEW_ENGLAND_AREAS.get(subdomain)
        if area_id is None:
            print(f"  Unknown area: {subdomain}, skipping")
            continue

        print(f"  Fetching {subdomain}.craigslist.org…")
        session.headers["Referer"] = f"https://{subdomain}.craigslist.org/search/{category}"

        # First batch: areaId-offset-0-batchSortId-bundleDups
        batch = f"{area_id}-0-0-0-0"
        cache_id = None
        batch_sort_id = 0
        bundle_dups = 0
        page = 0

        while True:
            params = {
                "batch":      batch,
                "cc":         "us",
                "lang":       "en",
                "searchPath": category,
                **filters,
            }
            if cache_id:
                params["cacheId"] = cache_id

            try:
                resp = session.get(_SAPI_BASE, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"    Error: {e}")
                break

            d        = data.get("data", {})
            errors   = data.get("errors", [])
            if errors:
                print(f"    API error: {errors}")
                break

            raw_items   = d.get("items", [])
            decode      = d.get("decode", {})
            total       = d.get("totalResultCount", 0)
            new_cache   = d.get("cacheId")
            batch_id    = d.get("batchId")       # batchSortId for next request
            bundle_dups = d.get("bundleDups", 0)
            cache_ts    = d.get("cacheTs", 0)

            listings = _decode_items(raw_items, decode)

            # 1. Collapse same-vehicle duplicates within this batch (dedupeKey)
            listings = _dedupe_by_key(listings)

            # 2. Drop postings already seen from another area (postingId)
            new_listings = []
            for lst in listings:
                pid = lst["postingId"]
                if pid not in seen_posting_ids:
                    seen_posting_ids.add(pid)
                    new_listings.append(lst)

            skipped = len(listings) - len(new_listings)
            all_listings.extend(new_listings)

            fetched = len(new_listings)
            cumulative = len(all_listings)
            print(f"    Page {page}: {fetched} new listings "
                  f"({skipped} duplicates dropped, running total: {cumulative})")

            if max_results and cumulative >= max_results:
                break

            # If this batch returned all results, no need to paginate
            if not new_cache or fetched >= total or not batch_id:
                break

            # Build next-page batch: areaId-offset-count-batchSortId-bundleDups-maxPostedTs-cacheTs
            offset = (page + 1) * fetched
            max_posted = decode.get("maxPostedDate", 0)
            batch = f"{area_id}-{offset}-{fetched}-{batch_id}-{bundle_dups}-{max_posted}-{cache_ts}"
            cache_id = new_cache
            batch_sort_id = batch_id
            page += 1
            time.sleep(delay)

        time.sleep(delay)

    return all_listings


# ── helpers ───────────────────────────────────────────────────────────────────

def listings_to_csv(listings, path="craigslist_listings.csv"):
    """Write listings to a CSV file."""
    import csv
    if not listings:
        print("No listings to write.")
        return
    fields = ["postingId", "title", "price", "priceStr", "mileage",
              "location", "neighborhood", "hostname", "postedDate",
              "categoryAbbr", "lat", "lon", "url", "uuid"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(listings)
    print(f"Wrote {len(listings)} rows to {path}")


# ── CLI demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Searching Craigslist New England for Tesla…\n")
    listings = fetch_listings(
        make_model="tesla",   # CL make/model field; add query="model s" to narrow further
        min_year=2017,
        max_price=30000,
        max_miles=80000,
        areas=["hartford", "boston", "providence", "nh", "vermont", "maine"],
    )

    print(f"\nFound {len(listings)} total listings\n")
    for car in listings[:15]:
        miles = f"{car['mileage']:,}" if car.get("mileage") else "?"
        print(
            f"  {car.get('title', 'Unknown')[:55]:<55}"
            f"  {car.get('priceStr', '?'):>8}"
            f"  {miles:>8} mi"
            f"  {car.get('location', '?'):>20}"
            f"  ({car.get('hostname', '')})"
        )
        print(f"    {car.get('url', '')}")
    print()
    listings_to_csv(listings, "craigslist_ne_tesla.csv")
