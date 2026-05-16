#!/usr/bin/env python3
"""
Hotel finder: searches for hotels near a location within a price cap.

Usage:
    python3 hotel_finder.py \
        --checkin 2026-07-12 \
        --checkout 2026-07-19 \
        --location "111 8th Avenue, Chelsea, New York" \
        --cap 470

Live price sources (tried in order):
  1. RapidAPI — Booking.com  — set RAPIDAPI_KEY env var (free tier at rapidapi.com)
                               Uses the real Booking.com inventory, only needs `requests`
  2. Amadeus Production API  — set AMADEUS_CLIENT_ID + AMADEUS_CLIENT_SECRET env vars
  3. Booking.com scraper     — uses curl_cffi Chrome TLS impersonation to bypass bot checks
  4. Hotels.com scraper      — same technique
  5. Curated estimates       — July 2026 peak-season estimates with direct booking links

Options:
  --lat / --lon    Skip geocoding by providing explicit coordinates
  --json           Output JSON instead of human-readable text
  --quiet          Suppress progress output
  --source NAME    Force a specific source: rapidapi | amadeus | booking | hotelsdotcom | curated
"""

import argparse
import json
import math
import os
import re
import sys
import urllib.parse
from datetime import datetime
from typing import Optional

import requests as std_requests

try:
    from curl_cffi import requests as cffi_requests
    CFFI_AVAILABLE = True
except ImportError:
    CFFI_AVAILABLE = False

# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

def geocode(location: str) -> tuple[float, float]:
    loc_lower = location.lower()
    if "111 8th" in loc_lower or (
        "google" in loc_lower and any(k in loc_lower for k in ("nyc", "new york", "chelsea"))
    ):
        return 40.7422, -74.0041

    for fn in [_geocode_photon, _geocode_nominatim]:
        result = fn(location)
        if result:
            return result

    raise ValueError(
        f"Could not geocode {location!r}. Use --lat / --lon for explicit coordinates."
    )


def _geocode_photon(location: str) -> Optional[tuple[float, float]]:
    try:
        r = std_requests.get(
            "https://photon.komoot.io/api/",
            params={"q": location, "limit": 1, "lang": "en"},
            headers={"User-Agent": "hotel-finder/1.0"},
            timeout=10,
        )
        if r.ok:
            features = r.json().get("features", [])
            if features:
                c = features[0]["geometry"]["coordinates"]
                return float(c[1]), float(c[0])
    except Exception:
        pass
    return None


def _geocode_nominatim(location: str) -> Optional[tuple[float, float]]:
    try:
        r = std_requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": location, "format": "json", "limit": 1},
            headers={"User-Agent": "Mozilla/5.0 (compatible; personal-hotel-finder/1.0)"},
            timeout=10,
        )
        if r.ok:
            results = r.json()
            if results:
                return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Hotel model
# ---------------------------------------------------------------------------

class Hotel:
    def __init__(self, name: str, price_per_night: float, total_price: float,
                 currency: str, address: str, stars: Optional[float],
                 rating: Optional[float], url: str, booking_url: str = "",
                 lat: Optional[float] = None, lon: Optional[float] = None,
                 distance_km: Optional[float] = None, source: str = "unknown",
                 price_is_live: bool = False):
        self.name = name
        self.price_per_night = price_per_night
        self.total_price = total_price
        self.currency = currency
        self.address = address
        self.stars = stars
        self.rating = rating
        self.url = url
        self.booking_url = booking_url
        self.lat = lat
        self.lon = lon
        self.distance_km = distance_km
        self.source = source
        self.price_is_live = price_is_live
        self.google_maps_url: str = ""
        self.google_rating: Optional[float] = None
        self.google_rating_count: Optional[int] = None


def booking_search_url(name: str, checkin: str, checkout: str) -> str:
    q = urllib.parse.quote_plus(name + ", New York")
    return (
        f"https://www.booking.com/search.html?ss={q}"
        f"&checkin={checkin}&checkout={checkout}&group_adults=1&no_rooms=1"
    )


def google_maps_search_url(name: str, address: str = "") -> str:
    query = urllib.parse.quote_plus(f"{name} {address}".strip())
    return f"https://www.google.com/maps/search/?api=1&query={query}"


def google_maps_place_url(place_id: str) -> str:
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}"


# ---------------------------------------------------------------------------
# Google Maps Places enrichment (optional — set GOOGLE_MAPS_API_KEY)
# ---------------------------------------------------------------------------
# Get a free API key at https://console.cloud.google.com
# Enable "Places API" → create a key → restrict to Places API
# Free tier: $200/month credit (~11,000 text searches free)

def enrich_with_google_maps(hotels: list[Hotel], verbose: bool = True) -> None:
    """Fetch Google Maps ratings + place URLs for each hotel. Mutates in place."""
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not key:
        # No key: at least set a search URL for every hotel
        for h in hotels:
            h.google_maps_url = google_maps_search_url(h.name, h.address)
        return

    if verbose:
        print(f"\n  [Google Maps] Fetching ratings for {len(hotels)} hotels...", flush=True)

    for h in hotels:
        h.google_maps_url = google_maps_search_url(h.name, h.address)  # fallback
        try:
            r = std_requests.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={
                    "query": f"{h.name} New York",
                    "key": key,
                    "type": "lodging",
                },
                timeout=10,
            )
            if not r.ok:
                continue
            results = r.json().get("results", [])
            if not results:
                continue
            place = results[0]
            h.google_rating = place.get("rating")
            h.google_rating_count = place.get("user_ratings_total")
            place_id = place.get("place_id", "")
            if place_id:
                h.google_maps_url = google_maps_place_url(place_id)
        except Exception:
            continue

    if verbose:
        enriched = sum(1 for h in hotels if h.google_rating is not None)
        print(f"  [Google Maps] Got ratings for {enriched}/{len(hotels)} hotels")


# ---------------------------------------------------------------------------
# Source 1: RapidAPI — Booking.com data (only needs `requests`, no extra packages)
# ---------------------------------------------------------------------------
# Free signup at https://rapidapi.com — search for "Booking.com" by apidojo or
# "booking-com15" by DataCrawler. Free tier: ~100-500 req/month depending on provider.
# Set env var: RAPIDAPI_KEY

def search_rapidapi(checkin: str, checkout: str, lat: float, lon: float,
                    cap_usd: float, nights: int) -> list[Hotel]:
    key = os.environ.get("RAPIDAPI_KEY", "")
    if not key:
        print("    no RAPIDAPI_KEY set", file=sys.stderr)
        return []

    # Step 1: get destination ID for the location
    dest_id, search_type = _rapidapi_dest_id(key)
    print(f"    dest_id={dest_id!r} search_type={search_type!r}", file=sys.stderr)

    # Step 2: search hotels
    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": "booking-com15.p.rapidapi.com",
    }
    params = {
        "dest_id": dest_id,
        "search_type": search_type,
        "arrival_date": checkin,
        "departure_date": checkout,
        "adults": "1",
        "room_qty": "1",
        "page_number": "1",
        "units": "metric",
        "temperature_unit": "c",
        "languagecode": "en-us",
        "currency_code": "USD",
    }
    data = None
    for attempt, timeout in enumerate([30, 45, 60], 1):
        try:
            r = std_requests.get(
                "https://booking-com15.p.rapidapi.com/api/v1/hotels/searchHotels",
                headers=headers,
                params=params,
                timeout=timeout,
            )
            if r.ok:
                data = r.json()
                break
            print(f"    hotel search attempt {attempt}: {r.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"    hotel search attempt {attempt} error: {e}", file=sys.stderr)
    if data is None:
        return []

    hotels = []
    items = data.get("data", {}).get("hotels", []) or []
    print(f"    raw items from API: {len(items)}", file=sys.stderr)

    for item in items:
        try:
            prop = item.get("property", {})
            name = prop.get("name", "")
            if not name:
                continue

            # Try every known price field path across booking-com15 API versions
            pb = prop.get("priceBreakdown", {})
            gross = pb.get("grossPrice", {})
            # allInclusiveAmount includes taxes+fees — always prefer it
            total = (
                _f(pb.get("allInclusiveAmount", {}).get("value"))
                or _f(pb.get("allInclusiveAmount", {}).get("amount"))
                or _f(gross.get("value"))
                or _f(gross.get("amount"))
                or _f(item.get("priceDisplayInfo", {})
                        .get("displayPrice", {})
                        .get("amountPerStay", {})
                        .get("amountRounded"))
                or _f(prop.get("price"))
                or 0.0
            )
            currency = (pb.get("allInclusiveAmount", {}).get("currency")
                        or gross.get("currency") or "USD")

            if total == 0:
                continue
            # API returns total-stay price; divide by nights for per-night rate
            per_night = total / nights
            if per_night > cap_usd * 1.30:
                continue

            hlat = float(prop.get("latitude") or 0) or None
            hlon = float(prop.get("longitude") or 0) or None
            dist = haversine_km(lat, lon, hlat, hlon) if hlat and hlon else None

            hotel_id = prop.get("id") or item.get("hotel_id", "")
            url = (
                f"https://www.booking.com/searchresults.html"
                f"?dest_id={hotel_id}&dest_type=hotel"
                f"&checkin={checkin}&checkout={checkout}&group_adults=1&no_rooms=1"
                if hotel_id else booking_search_url(name, checkin, checkout)
            )

            hotels.append(Hotel(
                name=name,
                price_per_night=per_night,
                total_price=total,
                currency=currency,
                address=prop.get("wishlistName", "") or "",
                stars=prop.get("propertyClass"),
                rating=prop.get("reviewScore"),
                url=url,
                booking_url=url,
                lat=hlat, lon=hlon, distance_km=dist,
                source="rapidapi-booking",
                price_is_live=True,
            ))
        except (KeyError, TypeError, ValueError):
            continue

    return hotels


def _f(v) -> float:
    """Safe float conversion, returns 0.0 on failure."""
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _rapidapi_dest_id(key: str) -> tuple[str, str]:
    """Return (dest_id, search_type) for Chelsea NY, falling back to Manhattan."""
    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": "booking-com15.p.rapidapi.com",
    }
    for query in ["Chelsea, New York", "New York City"]:
        try:
            r = std_requests.get(
                "https://booking-com15.p.rapidapi.com/api/v1/hotels/searchDestination",
                headers=headers,
                params={"query": query, "languagecode": "en-us"},
                timeout=30,
            )
            if not r.ok:
                continue
            for res in r.json().get("data", []):
                stype = res.get("search_type", "")
                if stype in ("city", "district", "landmark", "region"):
                    return str(res["dest_id"]), stype.upper()
        except Exception:
            continue
    # Manhattan hardcoded fallback
    return "-2140479", "CITY"


# ---------------------------------------------------------------------------
# Source 2: Amadeus (Production or Sandbox)
# ---------------------------------------------------------------------------
# Get a FREE production key at https://developers.amadeus.com — no credit card
# Set env vars: AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET
# Without env vars, falls back to sandbox (synthetic test data, not live prices)

AMADEUS_BASE = "https://api.amadeus.com"          # production
AMADEUS_TEST = "https://test.api.amadeus.com"     # sandbox (synthetic prices)


def search_amadeus(checkin: str, checkout: str, lat: float, lon: float,
                   cap_usd: float, nights: int) -> list[Hotel]:
    client_id = os.environ.get("AMADEUS_CLIENT_ID", "")
    client_secret = os.environ.get("AMADEUS_CLIENT_SECRET", "")
    is_production = bool(client_id and client_secret)
    base = AMADEUS_BASE if is_production else AMADEUS_TEST
    cid = client_id or "demo"
    csec = client_secret or "demo"

    # Auth
    try:
        r = std_requests.post(
            f"{base}/v1/security/oauth2/token",
            data={"grant_type": "client_credentials",
                  "client_id": cid, "client_secret": csec},
            timeout=10,
        )
        if not r.ok:
            print(f"    auth failed {r.status_code}", file=sys.stderr)
            return []
        token = r.json().get("access_token", "")
    except Exception as e:
        print(f"    auth error: {e}", file=sys.stderr)
        return []

    if not token:
        return []

    headers = {"Authorization": f"Bearer {token}"}

    # Hotel IDs near location
    try:
        r = std_requests.get(
            f"{base}/v1/reference-data/locations/hotels/by-geocode",
            headers=headers,
            params={"latitude": lat, "longitude": lon,
                    "radius": 2, "radiusUnit": "KM", "hotelSource": "ALL"},
            timeout=15,
        )
        if not r.ok:
            print(f"    hotel-list {r.status_code}", file=sys.stderr)
            return []
        hotel_ids = [h["hotelId"] for h in r.json().get("data", [])[:20]]
    except Exception as e:
        print(f"    hotel-list error: {e}", file=sys.stderr)
        return []

    if not hotel_ids:
        return []

    # Offers
    try:
        r = std_requests.get(
            f"{base}/v3/shopping/hotel-offers",
            headers=headers,
            params={"hotelIds": ",".join(hotel_ids),
                    "checkInDate": checkin, "checkOutDate": checkout,
                    "adults": 1, "currency": "USD", "bestRateOnly": "true"},
            timeout=20,
        )
        if not r.ok:
            print(f"    offers {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return []
        data = r.json().get("data", [])
    except Exception as e:
        print(f"    offers error: {e}", file=sys.stderr)
        return []

    hotels = []
    for item in data:
        try:
            h = item["hotel"]
            offer = item["offers"][0]
            total = float(offer["price"]["total"])
            per_night = total / nights
            if per_night > cap_usd * 1.30:
                continue
            hlat = h.get("latitude")
            hlon = h.get("longitude")
            dist = haversine_km(lat, lon, float(hlat), float(hlon)) if hlat and hlon else None
            name = h.get("name", "Unknown")
            hotels.append(Hotel(
                name=name.title(),
                price_per_night=per_night,
                total_price=total,
                currency=offer["price"].get("currency", "USD"),
                address=", ".join(filter(None, [
                    h.get("address", {}).get("lines", [""])[0],
                    h.get("address", {}).get("cityName", ""),
                ])),
                stars=h.get("rating"),
                rating=None,
                url=booking_search_url(name, checkin, checkout),
                booking_url=booking_search_url(name, checkin, checkout),
                lat=hlat, lon=hlon, distance_km=dist,
                source="amadeus-production" if is_production else "amadeus-sandbox",
                price_is_live=is_production,
            ))
        except (KeyError, TypeError, ValueError):
            continue

    return hotels


# ---------------------------------------------------------------------------
# Source 2: Booking.com  (curl_cffi Chrome impersonation)
# ---------------------------------------------------------------------------

_CHROME_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}


def _cffi_get(url: str, params: dict = None, headers: dict = None,
              timeout: int = 20) -> Optional[object]:
    """GET with curl_cffi Chrome impersonation (bypasses Cloudflare JA3/JA4 checks)."""
    if not CFFI_AVAILABLE:
        return None
    try:
        r = cffi_requests.get(
            url,
            params=params,
            headers={**_CHROME_HEADERS, **(headers or {})},
            impersonate="chrome110",
            allow_redirects=True,
            timeout=timeout,
        )
        return r
    except Exception as e:
        print(f"    curl_cffi error: {e}", file=sys.stderr)
        return None


def search_booking(checkin: str, checkout: str, lat: float, lon: float,
                   cap_usd: float, nights: int) -> list[Hotel]:
    ci = datetime.strptime(checkin, "%Y-%m-%d")
    co = datetime.strptime(checkout, "%Y-%m-%d")

    if not CFFI_AVAILABLE:
        print("    curl_cffi not available; install with: pip install curl_cffi", file=sys.stderr)
        return []

    # First hit the homepage to get session cookies
    sess_r = _cffi_get("https://www.booking.com/", timeout=15)
    cookies = sess_r.cookies if sess_r and sess_r.ok else {}

    params = {
        "latitude": lat,
        "longitude": lon,
        "checkin_year": ci.year,
        "checkin_month": ci.month,
        "checkin_monthday": ci.day,
        "checkout_year": co.year,
        "checkout_month": co.month,
        "checkout_monthday": co.day,
        "group_adults": 1,
        "no_rooms": 1,
        "order": "price",
        "selected_currency": "USD",
        "changed_currency": 1,
        "nflt": "ht_id%3D204",  # hotel property type
    }

    html = None
    for attempt in range(3):
        try:
            r = cffi_requests.get(
                "https://www.booking.com/searchresults.html",
                params=params,
                headers=_CHROME_HEADERS,
                cookies=cookies,
                impersonate="chrome110",
                allow_redirects=True,
                timeout=30,
            )
        except Exception as e:
            print(f"    booking request error: {e}", file=sys.stderr)
            return []

        if r.status_code == 200:
            html = r.text
            break
        elif r.status_code == 202:
            # Booking.com async processing — follow Location header or re-request
            location = r.headers.get("Location") or r.headers.get("location")
            if location:
                try:
                    r2 = cffi_requests.get(
                        location if location.startswith("http") else "https://www.booking.com" + location,
                        headers=_CHROME_HEADERS,
                        cookies={**cookies, **r.cookies},
                        impersonate="chrome110",
                        allow_redirects=True,
                        timeout=30,
                    )
                    if r2.status_code == 200:
                        html = r2.text
                        break
                except Exception:
                    pass
            # Try parsing the 202 body anyway — sometimes results are embedded
            if r.text and len(r.text) > 1000:
                html = r.text
                break
            import time as _time
            _time.sleep(2)
        else:
            print(f"    booking HTTP {r.status_code}", file=sys.stderr)
            return []

    if not html:
        print("    booking: no usable response after retries", file=sys.stderr)
        return []

    return _parse_booking(html, lat, lon, nights, cap_usd, checkin, checkout)


def _parse_booking(html: str, ref_lat: float, ref_lon: float,
                   nights: int, cap_usd: float,
                   checkin: str, checkout: str) -> list[Hotel]:
    hotels = []

    # Strategy 1: b_search_results JS blob (older Booking.com)
    m = re.search(r'b_search_results\s*=\s*(\[.{100,}\])\s*;', html)
    if m:
        try:
            for item in json.loads(m.group(1)):
                h = _booking_item_to_hotel(item, ref_lat, ref_lon, nights, cap_usd, checkin, checkout)
                if h:
                    hotels.append(h)
        except json.JSONDecodeError:
            pass

    # Strategy 2: __NEXT_DATA__ (Next.js SSR)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            props = data.get("props", {}).get("pageProps", {})
            results = (props.get("searchResults", {}).get("results", [])
                       or props.get("hotels", [])
                       or [])
            for item in results:
                h = _booking_item_to_hotel(item, ref_lat, ref_lon, nights, cap_usd, checkin, checkout)
                if h:
                    hotels.append(h)
        except (json.JSONDecodeError, AttributeError):
            pass

    # Strategy 3: window.appData or similar embedded JSON
    for pattern in [
        r'window\.appData\s*=\s*({.{100,}})\s*;',
        r'"searchResults":\{"hotels":\[(.{100,})\]\}',
    ]:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                chunk = m.group(1)
                if not chunk.startswith("{"):
                    chunk = '{"hotels":[' + chunk + "]}"
                data = json.loads(chunk)
                for item in data.get("hotels", [data]):
                    h = _booking_item_to_hotel(item, ref_lat, ref_lon, nights, cap_usd, checkin, checkout)
                    if h:
                        hotels.append(h)
            except (json.JSONDecodeError, AttributeError):
                pass

    return hotels


def _booking_item_to_hotel(item: dict, ref_lat: float, ref_lon: float,
                            nights: int, cap_usd: float,
                            checkin: str, checkout: str) -> Optional[Hotel]:
    try:
        name = (item.get("hotel_name") or item.get("name")
                or item.get("hotelName") or "")
        if not name:
            return None
        price_data = item.get("price_breakdown", {}) or item.get("priceBreakdown", {})
        total = float(
            price_data.get("all_inclusive_price")
            or price_data.get("grossPrice", {}).get("value", 0)
            or item.get("min_total_price", 0)
            or item.get("minTotalPrice", 0)
            or 0
        )
        if total == 0:
            return None
        per_night = total / nights
        if per_night > cap_usd * 1.30:
            return None
        hlat = float(item.get("latitude") or item.get("lat") or 0) or None
        hlon = float(item.get("longitude") or item.get("lng") or item.get("lon") or 0) or None
        dist = haversine_km(ref_lat, ref_lon, hlat, hlon) if hlat and hlon else None
        rel_url = item.get("url") or item.get("hotelUrl") or ""
        full_url = ("https://www.booking.com" + rel_url) if rel_url.startswith("/") else (rel_url or booking_search_url(name, checkin, checkout))
        return Hotel(
            name=name,
            price_per_night=per_night,
            total_price=total,
            currency="USD",
            address=item.get("address") or item.get("location", {}).get("address", ""),
            stars=item.get("class") or item.get("starRating"),
            rating=item.get("review_score") or item.get("reviewScore"),
            url=full_url,
            booking_url=full_url,
            lat=hlat, lon=hlon, distance_km=dist,
            source="booking-live",
            price_is_live=True,
        )
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Source 3: Hotels.com  (curl_cffi + GraphQL)
# ---------------------------------------------------------------------------

def search_hotelsdotcom(checkin: str, checkout: str, lat: float, lon: float,
                        cap_usd: float, nights: int) -> list[Hotel]:
    if not CFFI_AVAILABLE:
        return []

    # Hotels.com uses Expedia Group's GraphQL API
    # First, get the region ID for the search area via their suggest API
    region_id = _hotelsdotcom_region_id(lat, lon)

    # Fallback: search by destination string
    checkin_parts = checkin.split("-")
    checkout_parts = checkout.split("-")

    # Try multiple Hotels.com URL patterns (they restructure frequently)
    candidates = [
        f"https://www.hotels.com/search?q-destination=Chelsea+New+York&q-check-in={checkin}&q-check-out={checkout}&q-rooms=1&q-room-0-adults=1",
        f"https://www.hotels.com/Hotel-Search?destination=Chelsea+New+York&startDate={checkin}&endDate={checkout}&adults=1&rooms=1",
        f"https://www.hotels.com/search?destination=Chelsea%2C+New+York&startDate={checkin}&endDate={checkout}&rooms=1&adults=1",
    ]
    if region_id:
        candidates.insert(0,
            f"https://www.hotels.com/search?regionId={region_id}&q-check-in={checkin}&q-check-out={checkout}&q-rooms=1&q-room-0-adults=1"
        )

    r = None
    for url in candidates:
        try:
            r = cffi_requests.get(
                url,
                headers=_CHROME_HEADERS,
                impersonate="chrome110",
                allow_redirects=True,
                timeout=25,
            )
            if r and r.status_code == 200:
                break
            print(f"    hotels.com {r.status_code if r else 'err'} → {url[:60]}", file=sys.stderr)
        except Exception as e:
            print(f"    hotels.com error: {e}", file=sys.stderr)

    if not r or r.status_code != 200:
        print(f"    hotels.com: no usable response from any URL", file=sys.stderr)
        return []

    return _parse_hotelsdotcom(r.text, lat, lon, nights, cap_usd, checkin, checkout)


def _hotelsdotcom_region_id(lat: float, lon: float) -> Optional[str]:
    """Get Hotels.com region ID for a location."""
    try:
        r = cffi_requests.get(
            "https://www.hotels.com/api/v4/destinations/search",
            params={"q": "Chelsea New York", "locale": "en_US", "siteid": 300000001},
            headers=_CHROME_HEADERS,
            impersonate="chrome110",
            timeout=10,
        )
        if r and r.ok:
            data = r.json()
            results = data.get("sr", []) or data.get("suggestions", []) or []
            if results:
                return str(results[0].get("gaiaId") or results[0].get("regionId") or "")
    except Exception:
        pass
    return None


def _parse_hotelsdotcom(html: str, ref_lat: float, ref_lon: float,
                        nights: int, cap_usd: float,
                        checkin: str, checkout: str) -> list[Hotel]:
    hotels = []

    # Hotels.com embeds __NEXT_DATA__ (Next.js) or window.__APOLLO_STATE__
    for pattern, key_path in [
        (r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', ["props", "pageProps"]),
        (r'window\.__APOLLO_STATE__\s*=\s*({.{200,}})\s*;', None),
        (r'window\.__data\s*=\s*({.{200,}})\s*;', None),
    ]:
        m = re.search(pattern, html, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
            if key_path:
                for k in key_path:
                    data = data.get(k, {})

            # Walk the data structure looking for hotel listings
            hotel_list = (
                data.get("searchResult", {}).get("properties", [])
                or data.get("hotels", [])
                or data.get("propertySearchListings", [])
                or []
            )

            for item in hotel_list:
                h = _hotelsdotcom_item(item, ref_lat, ref_lon, nights, cap_usd, checkin, checkout)
                if h:
                    hotels.append(h)
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue

    return hotels


def _hotelsdotcom_item(item: dict, ref_lat: float, ref_lon: float,
                       nights: int, cap_usd: float,
                       checkin: str, checkout: str) -> Optional[Hotel]:
    try:
        name = (item.get("name") or item.get("propertyName")
                or item.get("headingSection", {}).get("heading", ""))
        if not name:
            return None

        # Price extraction — Hotels.com has multiple schemas
        price = 0.0
        for path in [
            ["price", "lead", "amount"],
            ["ratePlan", "price", "current"],
            ["priceSummary", "options", 0, "priceBreakdown", "total", "amount"],
            ["nightlyPrice", "amount"],
        ]:
            obj = item
            for k in path:
                if isinstance(obj, dict):
                    obj = obj.get(k)
                elif isinstance(obj, list) and isinstance(k, int):
                    obj = obj[k] if len(obj) > k else None
                else:
                    obj = None
                if obj is None:
                    break
            if obj and isinstance(obj, (int, float, str)):
                try:
                    price = float(obj)
                    break
                except (ValueError, TypeError):
                    pass

        if price == 0:
            return None

        # Determine if price is per-night or total
        per_night = price if price < 1500 else price / nights
        total = per_night * nights
        if per_night > cap_usd * 1.30:
            return None

        coords = item.get("mapMarker", {}) or item.get("coordinate", {})
        hlat = float(coords.get("latLong", {}).get("latitude") or coords.get("lat") or 0) or None
        hlon = float(coords.get("latLong", {}).get("longitude") or coords.get("lon") or 0) or None
        dist = haversine_km(ref_lat, ref_lon, hlat, hlon) if hlat and hlon else None

        url = item.get("detailsUrl") or item.get("propertyDetailsUrl") or ""
        if url and not url.startswith("http"):
            url = "https://www.hotels.com" + url

        return Hotel(
            name=name,
            price_per_night=per_night,
            total_price=total,
            currency="USD",
            address=item.get("neighborhood") or item.get("address") or "",
            stars=item.get("starRating") or item.get("hotelStarRating"),
            rating=item.get("guestReviews", {}).get("rating") or item.get("reviewScore"),
            url=url or booking_search_url(name, checkin, checkout),
            booking_url=url or booking_search_url(name, checkin, checkout),
            lat=hlat, lon=hlon, distance_km=dist,
            source="hotelsdotcom-live",
            price_is_live=True,
        )
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Source 4: Curated estimates (fallback)
# ---------------------------------------------------------------------------

_CURATED = [
    {"name": "The Maritime Hotel",
     "address": "363 W 16th St, New York, NY 10011",
     "lat": 40.7420, "lon": -74.0030, "stars": 4.0, "rating": 8.0,
     "lo": 250, "hi": 450,
     "url": "https://www.themaritimehotel.com"},
    {"name": "The High Line Hotel",
     "address": "180 10th Ave, New York, NY 10011",
     "lat": 40.7461, "lon": -74.0072, "stars": 4.0, "rating": 9.0,
     "lo": 310, "hi": 560,
     "url": "https://www.thehighlinehotel.com"},
    {"name": "Hotel Gansevoort",
     "address": "18 9th Ave, New York, NY 10014",
     "lat": 40.7406, "lon": -74.0057, "stars": 4.0, "rating": 8.4,
     "lo": 300, "hi": 580,
     "url": "https://www.gansevoorthotelgroup.com/meatpacking"},
    {"name": "The Standard, High Line",
     "address": "848 Washington St, New York, NY 10014",
     "lat": 40.7410, "lon": -74.0083, "stars": 4.0, "rating": 8.5,
     "lo": 320, "hi": 650,
     "url": "https://www.standardhotels.com/new-york/properties/high-line"},
    {"name": "Hampton Inn Manhattan/Chelsea",
     "address": "108 W 24th St, New York, NY 10011",
     "lat": 40.7447, "lon": -73.9967, "stars": 3.0, "rating": 8.3,
     "lo": 200, "hi": 400,
     "url": "https://www.hilton.com/en/hotels/nycshhn-hampton-inn-manhattan-chelsea/"},
    {"name": "Motto by Hilton New York City Chelsea",
     "address": "113 W 24th St, New York, NY 10011",
     "lat": 40.7449, "lon": -73.9965, "stars": 3.0, "rating": 8.0,
     "lo": 180, "hi": 360,
     "url": "https://www.hilton.com/en/hotels/nycmtmo-motto-new-york-city-chelsea/"},
    {"name": "Hyatt Place New York City/Chelsea",
     "address": "325 W 28th St, New York, NY 10001",
     "lat": 40.7490, "lon": -74.0001, "stars": 3.0, "rating": 8.2,
     "lo": 200, "hi": 420,
     "url": "https://www.hyatt.com/en-US/hotel/new-york/hyatt-place-new-york-city-chelsea/nyczc"},
    {"name": "The Moxy NYC Chelsea",
     "address": "105 W 28th St, New York, NY 10001",
     "lat": 40.7481, "lon": -73.9973, "stars": 3.0, "rating": 8.6,
     "lo": 190, "hi": 400,
     "url": "https://www.marriott.com/hotels/travel/nycox-moxy-nyc-chelsea/"},
    {"name": "Hotel Indigo New York – Chelsea",
     "address": "127 W 28th St, New York, NY 10001",
     "lat": 40.7485, "lon": -73.9969, "stars": 4.0, "rating": 8.6,
     "lo": 240, "hi": 460,
     "url": "https://www.ihg.com/hotelindigo/hotels/us/en/new-york/nycch/hoteldetail"},
    {"name": "MADE Hotel",
     "address": "44 W 29th St, New York, NY 10001",
     "lat": 40.7456, "lon": -73.9931, "stars": 4.0, "rating": 8.5,
     "lo": 220, "hi": 420,
     "url": "https://www.madehotel.com"},
    {"name": "Courtyard by Marriott New York Manhattan/Chelsea",
     "address": "135 W 30th St, New York, NY 10001",
     "lat": 40.7494, "lon": -73.9961, "stars": 3.0, "rating": 8.1,
     "lo": 220, "hi": 430,
     "url": "https://www.marriott.com/hotels/travel/nycch-courtyard-new-york-manhattan-chelsea/"},
    {"name": "Kimpton Ink48 Hotel",
     "address": "653 11th Ave, New York, NY 10036",
     "lat": 40.7633, "lon": -74.0027, "stars": 4.0, "rating": 8.8,
     "lo": 280, "hi": 500,
     "url": "https://www.ink48.com"},
    {"name": "Pendry Manhattan West",
     "address": "438 W 36th St, New York, NY 10018",
     "lat": 40.7545, "lon": -74.0014, "stars": 5.0, "rating": 9.2,
     "lo": 400, "hi": 800,
     "url": "https://www.pendry.com/manhattan-west/"},
    {"name": "Voco Times Square South, an IHG Hotel",
     "address": "326 W 40th St, New York, NY 10018",
     "lat": 40.7570, "lon": -74.0003, "stars": 4.0, "rating": 8.3,
     "lo": 260, "hi": 480,
     "url": "https://www.ihg.com/voco/hotels/us/en/new-york/nycxs/hoteldetail"},
    {"name": "citizenM New York Times Square",
     "address": "218 W 50th St, New York, NY 10019",
     "lat": 40.7624, "lon": -73.9844, "stars": 4.0, "rating": 8.8,
     "lo": 190, "hi": 360,
     "url": "https://www.citizenm.com/hotels/united-states/new-york/new-york-times-square-hotel"},
    {"name": "citizenM New York Bowery",
     "address": "185 Bowery, New York, NY 10002",
     "lat": 40.7206, "lon": -73.9942, "stars": 4.0, "rating": 8.9,
     "lo": 200, "hi": 380,
     "url": "https://www.citizenm.com/hotels/united-states/new-york/new-york-bowery-hotel"},
    {"name": "The Ludlow Hotel",
     "address": "180 Ludlow St, New York, NY 10002",
     "lat": 40.7199, "lon": -73.9884, "stars": 4.0, "rating": 8.7,
     "lo": 270, "hi": 500,
     "url": "https://www.ludlowhotel.com"},
    {"name": "EVEN Hotel New York – Midtown East",
     "address": "221 E 44th St, New York, NY 10017",
     "lat": 40.7506, "lon": -73.9736, "stars": 3.0, "rating": 8.4,
     "lo": 210, "hi": 390,
     "url": "https://www.ihg.com/evenhotels/hotels/us/en/new-york/nycmh/hoteldetail"},
    {"name": "AC Hotel by Marriott New York Downtown",
     "address": "151 Maiden Ln, New York, NY 10038",
     "lat": 40.7074, "lon": -74.0057, "stars": 4.0, "rating": 8.5,
     "lo": 230, "hi": 450,
     "url": "https://www.marriott.com/hotels/travel/nycbm-ac-hotel-new-york-downtown/"},
    {"name": "1 Hotel Brooklyn Bridge",
     "address": "60 Furman St, Brooklyn, NY 11201",
     "lat": 40.6981, "lon": -73.9984, "stars": 5.0, "rating": 9.0,
     "lo": 450, "hi": 900,
     "url": "https://www.1hotels.com/brooklyn-bridge"},
]


def search_curated(checkin: str, checkout: str, lat: float, lon: float,
                   cap_usd: float, nights: int) -> list[Hotel]:
    JULY_FACTOR = 1.15
    hotels = []
    for h in _CURATED:
        dist = haversine_km(lat, lon, h["lat"], h["lon"])
        est = (h["lo"] + h["hi"]) / 2 * JULY_FACTOR
        if est > cap_usd * 1.30:
            continue
        name = h["name"]
        hotels.append(Hotel(
            name=name,
            price_per_night=est,
            total_price=est * nights,
            currency="USD",
            address=h["address"],
            stars=h.get("stars"),
            rating=h.get("rating"),
            url=h["url"],
            booking_url=booking_search_url(name, checkin, checkout),
            lat=h["lat"], lon=h["lon"], distance_km=dist,
            source="curated-estimate",
            price_is_live=False,
        ))
    return hotels


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

SOURCES = {
    "rapidapi": ("RapidAPI / Booking.com", search_rapidapi),
    "amadeus": ("Amadeus API", search_amadeus),
    "booking": ("Booking.com scraper", search_booking),
    "hotelsdotcom": ("Hotels.com scraper", search_hotelsdotcom),
    "curated": ("Curated estimates", search_curated),
}

_DEFAULT_ORDER = ["rapidapi", "amadeus", "booking", "hotelsdotcom", "curated"]


def find_hotels(checkin: str, checkout: str, location: str,
                cap_usd: float,
                ref_lat: Optional[float] = None,
                ref_lon: Optional[float] = None,
                force_source: Optional[str] = None,
                verbose: bool = True) -> list[Hotel]:
    ci = datetime.strptime(checkin, "%Y-%m-%d")
    co = datetime.strptime(checkout, "%Y-%m-%d")
    nights = (co - ci).days
    if nights <= 0:
        raise ValueError(f"checkout must be after checkin ({nights} nights)")

    if verbose:
        rapidapi_status = "✓ RapidAPI key found" if os.environ.get("RAPIDAPI_KEY") else "✗ No RAPIDAPI_KEY (free at rapidapi.com — search 'booking-com15')"
        amadeus_status = "✓ Amadeus key found" if os.environ.get("AMADEUS_CLIENT_ID") else "✗ No Amadeus key"
        cffi_status = "✓ curl_cffi available" if CFFI_AVAILABLE else "✗ curl_cffi not found (scrapers disabled)"
        maps_status = "✓ Google Maps key found (ratings enabled)" if os.environ.get("GOOGLE_MAPS_API_KEY") else "✗ No GOOGLE_MAPS_API_KEY (Maps links included, ratings skipped)"
        print(f"\n  {rapidapi_status}")
        print(f"  {amadeus_status}")
        print(f"  {cffi_status}")
        print(f"  {maps_status}")
        print(f"\n  Location : {location}")
        print(f"  Dates    : {checkin} → {checkout} ({nights} nights)")
        print(f"  Cap      : ${cap_usd:.0f}/night  (showing up to ${cap_usd * 1.30:.0f})\n")

    if ref_lat is None or ref_lon is None:
        ref_lat, ref_lon = geocode(location)
    if verbose:
        print(f"  Anchor   : {ref_lat:.4f}, {ref_lon:.4f}\n")

    order = [force_source] if force_source else _DEFAULT_ORDER

    live_hotels: list[Hotel] = []
    for key in order:
        if key == "curated":
            continue
        label, fn = SOURCES[key]
        if verbose:
            print(f"  [{label}]", end=" ", flush=True)
        try:
            results = fn(checkin, checkout, ref_lat, ref_lon, cap_usd, nights)
        except Exception as e:
            results = []
            if verbose:
                print(f"\n    Error: {e}", file=sys.stderr)
        live_count = sum(1 for h in results if h.price_is_live)
        if verbose:
            status = f"{len(results)} results ({live_count} live)" if results else "0 results"
            print(status)
        live_hotels.extend(results)

    # Always augment with curated (for any hotel not already found live)
    curated = search_curated(checkin, checkout, ref_lat, ref_lon, cap_usd, nights)
    live_names = {re.sub(r"\W+", "", h.name.lower()) for h in live_hotels}
    all_hotels = list(live_hotels)
    for h in curated:
        if re.sub(r"\W+", "", h.name.lower()) not in live_names:
            all_hotels.append(h)

    if not all_hotels:
        print("\nNo hotels found.", file=sys.stderr)
        return []

    within = sorted(
        [h for h in all_hotels if h.price_per_night <= cap_usd],
        key=lambda h: (not h.price_is_live, h.distance_km or 99, h.price_per_night),
    )
    over = sorted(
        [h for h in all_hotels if h.price_per_night > cap_usd],
        key=lambda h: (not h.price_is_live, h.price_per_night),
    )
    results = within + over
    enrich_with_google_maps(results, verbose=verbose)
    return results


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def format_results(hotels: list[Hotel], cap_usd: float, nights: int,
                   checkin: str, checkout: str) -> str:
    if not hotels:
        return "No hotels found."

    any_live = any(h.price_is_live for h in hotels)
    lines = ["", "=" * 72, f"{'HOTEL RESULTS':^72}", "=" * 72]

    if not any_live:
        lines += [
            "",
            "  ⚠  No live prices available — showing July 2026 estimates.",
            "  ⚠  To get real prices, set AMADEUS_CLIENT_ID + AMADEUS_CLIENT_SECRET",
            "  ⚠  Free signup (no credit card): https://developers.amadeus.com",
            "",
        ]

    within = [h for h in hotels if h.price_per_night <= cap_usd]
    over = [h for h in hotels if h.price_per_night > cap_usd]

    if within:
        lines.append(f"\n  WITHIN ${cap_usd:.0f}/NIGHT CAP\n")
        for i, h in enumerate(within, 1):
            lines.extend(_fmt(i, h, nights))

    if over:
        lines.append(f"\n  OVER CAP — CREDIT-WORTHY  (${cap_usd:.0f}–${cap_usd*1.3:.0f}/night)\n")
        for i, h in enumerate(over, 1):
            lines.extend(_fmt(len(within) + i, h, nights))

    lines += [
        "", "=" * 72,
        f"  {nights} nights  |  {checkin} → {checkout}  |  {len(hotels)} hotel(s)",
        "=" * 72, "",
    ]
    return "\n".join(lines)


def _fmt(rank: int, h: Hotel, nights: int) -> list[str]:
    stars = "★" * int(h.stars or 0)
    # Prefer Google Maps rating if available, fall back to source rating
    if h.google_rating is not None:
        count_str = f" ({h.google_rating_count:,} reviews)" if h.google_rating_count else ""
        rating = f"  ·  Google ★ {h.google_rating:.1f}{count_str}"
    elif h.rating:
        rating = f"  ·  {h.rating:.1f}/10"
    else:
        rating = ""
    dist = f"  ·  {h.distance_km:.2f} km" if h.distance_km is not None else ""
    price_tag = "LIVE" if h.price_is_live else "est."
    lines = [
        f"  {rank:>2}.  {h.name}",
        f"        {stars}{rating}{dist}",
        f"        ${h.price_per_night:.0f}/night  (${h.total_price:.0f} total)  [{price_tag}]",
        f"        {h.address}",
        f"        Book:  {h.booking_url or h.url}",
        f"        Maps:  {h.google_maps_url}",
        "",
    ]
    return lines


# ---------------------------------------------------------------------------
# Debug helper
# ---------------------------------------------------------------------------

def _debug_scrape(source: str, checkin: str, checkout: str) -> None:
    """Dump raw HTTP response so we can see what the site is actually returning."""

    if source == "rapidapi":
        key = os.environ.get("RAPIDAPI_KEY", "")
        if not key:
            print("RAPIDAPI_KEY not set")
            return
        headers = {
            "x-rapidapi-key": key,
            "x-rapidapi-host": "booking-com15.p.rapidapi.com",
        }
        # Step 1: destination search
        print("=== Step 1: searchDestination ===")
        for query in ["Chelsea, New York", "New York City"]:
            r = std_requests.get(
                "https://booking-com15.p.rapidapi.com/api/v1/hotels/searchDestination",
                headers=headers,
                params={"query": query, "languagecode": "en-us"},
                timeout=30,
            )
            print(f"Query: {query!r}  →  HTTP {r.status_code}")
            print(r.text[:2000])
            print()

        # Step 2: hotel search with a known good dest_id for Manhattan
        print("=== Step 2: searchHotels (dest_id=-2140479 = Manhattan) ===")
        r = std_requests.get(
            "https://booking-com15.p.rapidapi.com/api/v1/hotels/searchHotels",
            headers=headers,
            params={
                "dest_id": "-2140479",  # Manhattan fallback
                "search_type": "CITY",
                "arrival_date": checkin,
                "departure_date": checkout,
                "adults": "1",
                "room_qty": "1",
                "page_number": "1",
                "languagecode": "en-us",
                "currency_code": "USD",
            },
            timeout=60,
        )
        print(f"HTTP {r.status_code}  ({len(r.text)} chars)")
        print(r.text[:3000])
        return

    if not CFFI_AVAILABLE:
        print("curl_cffi not available")
        return

    ci = datetime.strptime(checkin, "%Y-%m-%d")
    co = datetime.strptime(checkout, "%Y-%m-%d")

    if source == "booking":
        url = "https://www.booking.com/searchresults.html"
        params = {
            "latitude": 40.7422, "longitude": -74.0041,
            "checkin_year": ci.year, "checkin_month": ci.month, "checkin_monthday": ci.day,
            "checkout_year": co.year, "checkout_month": co.month, "checkout_monthday": co.day,
            "group_adults": 1, "no_rooms": 1, "selected_currency": "USD",
        }
    else:
        url = "https://www.hotels.com/search"
        params = {"q-destination": "Chelsea New York",
                  "q-check-in": checkin, "q-check-out": checkout,
                  "q-rooms": 1, "q-room-0-adults": 1}

    print(f"GET {url}")
    try:
        r = cffi_requests.get(url, params=params, headers=_CHROME_HEADERS,
                              impersonate="chrome110", allow_redirects=True, timeout=30)
        print(f"Status: {r.status_code}")
        print(f"Headers: {dict(r.headers)}")
        print(f"Body ({len(r.text)} chars):\n{r.text[:3000]}")
    except Exception as e:
        print(f"Error: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Find hotels near a location within a nightly price cap.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
LIVE PRICES (easiest — only needs `requests`, no extra packages):
  1. Go to https://rapidapi.com and sign up (free)
  2. Search for "booking-com15" → Subscribe to the FREE plan
  3. Copy your API key from the dashboard
  4. Run:
       RAPIDAPI_KEY=xxx python3 hotel_finder.py ...

Examples:
  python3 hotel_finder.py \\
      --checkin 2026-07-12 --checkout 2026-07-19 \\
      --location "111 8th Avenue, Chelsea, New York" --cap 470

  RAPIDAPI_KEY=xxx python3 hotel_finder.py \\
      --checkin 2026-07-12 --checkout 2026-07-19 \\
      --location "111 8th Avenue, Chelsea, New York" --cap 470
""",
    )
    parser.add_argument("--checkin", required=True)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--cap", type=float, required=True,
                        help="Max nightly rate in USD (incl. taxes)")
    parser.add_argument("--lat", type=float, default=None)
    parser.add_argument("--lon", type=float, default=None)
    parser.add_argument("--source",
                        choices=list(SOURCES.keys()),
                        default=None,
                        help="Force a specific source: rapidapi | amadeus | booking | hotelsdotcom | curated")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--debug", action="store_true",
                        help="Dump raw response body for debugging scrapers")
    args = parser.parse_args()

    if args.debug and args.source in ("booking", "hotelsdotcom", "rapidapi"):
        _debug_scrape(args.source, args.checkin, args.checkout)
        return

    hotels = find_hotels(
        checkin=args.checkin,
        checkout=args.checkout,
        location=args.location,
        cap_usd=args.cap,
        ref_lat=args.lat,
        ref_lon=args.lon,
        force_source=args.source,
        verbose=not args.quiet,
    )

    nights = (datetime.strptime(args.checkout, "%Y-%m-%d")
              - datetime.strptime(args.checkin, "%Y-%m-%d")).days

    if args.as_json:
        print(json.dumps([{
            "rank": i + 1,
            "name": h.name,
            "price_per_night_usd": round(h.price_per_night, 2),
            "total_price_usd": round(h.total_price, 2),
            "address": h.address,
            "stars": h.stars,
            "rating": h.rating,
            "google_rating": h.google_rating,
            "google_rating_count": h.google_rating_count,
            "google_maps_url": h.google_maps_url,
            "distance_km": round(h.distance_km, 3) if h.distance_km is not None else None,
            "within_cap": h.price_per_night <= args.cap,
            "price_is_live": h.price_is_live,
            "booking_url": h.booking_url or h.url,
            "direct_url": h.url,
            "source": h.source,
        } for i, h in enumerate(hotels)], indent=2))
    else:
        print(format_results(hotels, args.cap, nights, args.checkin, args.checkout))


if __name__ == "__main__":
    main()
