#!/usr/bin/env python3
"""
Hotel finder: searches for hotels near a location within a price cap.

Usage:
    python3 hotel_finder.py \
        --checkin 2026-07-12 \
        --checkout 2026-07-19 \
        --location "111 8th Avenue, Chelsea, New York" \
        --cap 470

    # Or bypass geocoding with explicit coordinates:
    python3 hotel_finder.py \
        --checkin 2026-07-12 --checkout 2026-07-19 \
        --lat 40.7422 --lon -74.0041 \
        --location "Google NYC (111 8th Ave)" \
        --cap 470

Tries multiple live data sources in order, falls back to a curated list of
well-known hotels with July peak-season estimated prices.
"""

import argparse
import json
import math
import re
import sys
import urllib.parse
from datetime import datetime
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

def geocode(location: str) -> tuple[float, float]:
    """Return (lat, lon) for a free-text location, trying multiple services."""
    loc_lower = location.lower()

    # Fast path: known office locations
    if "111 8th" in loc_lower or (
        "google" in loc_lower and any(k in loc_lower for k in ("nyc", "new york", "chelsea"))
    ):
        return 40.7422, -74.0041  # 111 8th Avenue, Chelsea, NYC

    # Photon (OSM-based, no auth required)
    try:
        r = requests.get(
            "https://photon.komoot.io/api/",
            params={"q": location, "limit": 1, "lang": "en"},
            headers={"User-Agent": "hotel-finder/1.0"},
            timeout=10,
        )
        if r.ok:
            features = r.json().get("features", [])
            if features:
                coords = features[0]["geometry"]["coordinates"]
                return float(coords[1]), float(coords[0])
    except Exception:
        pass

    # Nominatim with a browser-like UA
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": location, "format": "json", "limit": 1},
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; personal-hotel-finder/1.0)",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=10,
        )
        if r.ok:
            results = r.json()
            if results:
                return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass

    raise ValueError(
        f"Could not geocode {location!r}. "
        "Use --lat / --lon to supply explicit coordinates."
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Hotel dataclass
# ---------------------------------------------------------------------------

class Hotel:
    def __init__(self, name: str, price_per_night: float, total_price: float,
                 currency: str, address: str, stars: Optional[float],
                 rating: Optional[float], url: str, booking_url: str = "",
                 lat: Optional[float] = None, lon: Optional[float] = None,
                 distance_km: Optional[float] = None, source: str = "unknown"):
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


# ---------------------------------------------------------------------------
# Booking URL builders (deep-link to search results with dates pre-filled)
# ---------------------------------------------------------------------------

def booking_com_url(name: str, checkin: str, checkout: str) -> str:
    q = urllib.parse.quote_plus(name + " New York")
    ci = checkin.replace("-", "")  # YYYYMMDD
    co = checkout.replace("-", "")
    # Booking.com search with dates
    return (
        f"https://www.booking.com/search.html?ss={urllib.parse.quote_plus(name + ', New York')}"
        f"&checkin={checkin}&checkout={checkout}&group_adults=1&no_rooms=1"
    )


def google_hotels_url(name: str, checkin: str, checkout: str) -> str:
    q = urllib.parse.quote_plus(name + " New York hotel")
    return (
        f"https://www.google.com/travel/hotels?q={q}"
        f"&checkin={checkin}&checkout={checkout}&adults=1"
    )


def hotels_com_url(name: str, checkin: str, checkout: str) -> str:
    q = urllib.parse.quote_plus(name + " New York")
    ci_parts = checkin.split("-")
    co_parts = checkout.split("-")
    return (
        f"https://www.hotels.com/search?q-destination={q}"
        f"&q-check-in={checkin}&q-check-out={checkout}"
        f"&q-rooms=1&q-room-0-adults=1"
    )


# ---------------------------------------------------------------------------
# Source 1: Amadeus sandbox
# ---------------------------------------------------------------------------

def search_amadeus(checkin: str, checkout: str, lat: float, lon: float,
                   cap_usd: float, nights: int) -> list[Hotel]:
    # Get token
    try:
        r = requests.post(
            "https://test.api.amadeus.com/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "demo",
                "client_secret": "demo",
            },
            timeout=8,
        )
        if not r.ok:
            print(f"  [amadeus] auth failed ({r.status_code})", file=sys.stderr)
            return []
        token = r.json().get("access_token", "")
    except Exception as e:
        print(f"  [amadeus] auth error: {e}", file=sys.stderr)
        return []

    if not token:
        return []

    headers = {"Authorization": f"Bearer {token}"}

    # Hotel IDs near location
    try:
        r = requests.get(
            "https://test.api.amadeus.com/v1/reference-data/locations/hotels/by-geocode",
            headers=headers,
            params={"latitude": lat, "longitude": lon, "radius": 2, "radiusUnit": "KM", "hotelSource": "ALL"},
            timeout=15,
        )
        if not r.ok:
            print(f"  [amadeus] hotel list {r.status_code}", file=sys.stderr)
            return []
        hotel_ids = [h["hotelId"] for h in r.json().get("data", [])[:20]]
    except Exception as e:
        print(f"  [amadeus] hotel list error: {e}", file=sys.stderr)
        return []

    if not hotel_ids:
        return []

    # Availability and pricing
    try:
        r = requests.get(
            "https://test.api.amadeus.com/v3/shopping/hotel-offers",
            headers=headers,
            params={
                "hotelIds": ",".join(hotel_ids),
                "checkInDate": checkin,
                "checkOutDate": checkout,
                "adults": 1,
                "currency": "USD",
                "bestRateOnly": "true",
            },
            timeout=20,
        )
        if not r.ok:
            print(f"  [amadeus] offers {r.status_code}", file=sys.stderr)
            return []
        data = r.json().get("data", [])
    except Exception as e:
        print(f"  [amadeus] offers error: {e}", file=sys.stderr)
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
                name=name,
                price_per_night=per_night,
                total_price=total,
                currency=offer["price"].get("currency", "USD"),
                address=", ".join(filter(None, [
                    h.get("address", {}).get("lines", [""])[0],
                    h.get("address", {}).get("cityName", ""),
                ])),
                stars=h.get("rating"),
                rating=None,
                url=google_hotels_url(name, checkin, checkout),
                booking_url=booking_com_url(name, checkin, checkout),
                lat=hlat, lon=hlon, distance_km=dist,
                source="amadeus-live",
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return hotels


# ---------------------------------------------------------------------------
# Source 2: Overpass (OpenStreetMap) — hotel names + coords,
#           no prices (used to enrich curated list)
# ---------------------------------------------------------------------------

def _osm_hotels(lat: float, lon: float, radius_m: int = 2000) -> list[dict]:
    query = f"""
[out:json][timeout:25];
(
  node["tourism"="hotel"](around:{radius_m},{lat},{lon});
  way["tourism"="hotel"](around:{radius_m},{lat},{lon});
);
out center tags;
"""
    try:
        r = requests.post("https://overpass-api.de/api/interpreter",
                          data={"data": query}, timeout=30)
        r.raise_for_status()
        return r.json().get("elements", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Source 3: Booking.com scraper
# ---------------------------------------------------------------------------

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
}


def search_booking(checkin: str, checkout: str, lat: float, lon: float,
                   cap_usd: float, nights: int) -> list[Hotel]:
    ci = datetime.strptime(checkin, "%Y-%m-%d")
    co = datetime.strptime(checkout, "%Y-%m-%d")

    url = "https://www.booking.com/searchresults.html"
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
    }

    session = requests.Session()
    session.headers.update(_BROWSER_HEADERS)

    try:
        r = session.get(url, params=params, timeout=20)
        if r.status_code != 200:
            print(f"  [booking] HTTP {r.status_code}", file=sys.stderr)
            return []
    except Exception as e:
        print(f"  [booking] error: {e}", file=sys.stderr)
        return []

    hotels = []

    # Try b_search_results JS blob
    blob_match = re.search(r'b_search_results\s*=\s*(\[.{100,}\])\s*;', r.text)
    if blob_match:
        try:
            for item in json.loads(blob_match.group(1)):
                try:
                    name = item.get("hotel_name") or item.get("name", "")
                    price = float(
                        item.get("price_breakdown", {}).get("all_inclusive_price", 0)
                        or item.get("min_total_price", 0) or 0
                    )
                    if not name or price == 0:
                        continue
                    per_night = price / nights
                    if per_night > cap_usd * 1.30:
                        continue
                    hlat = float(item.get("latitude", 0)) or None
                    hlon = float(item.get("longitude", 0)) or None
                    dist = haversine_km(lat, lon, hlat, hlon) if hlat and hlon else None
                    hotels.append(Hotel(
                        name=name,
                        price_per_night=per_night,
                        total_price=price,
                        currency="USD",
                        address=item.get("address", ""),
                        stars=item.get("class"),
                        rating=item.get("review_score"),
                        url="https://www.booking.com" + item.get("url", ""),
                        booking_url="https://www.booking.com" + item.get("url", ""),
                        lat=hlat, lon=hlon, distance_km=dist,
                        source="booking-live",
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
        except json.JSONDecodeError:
            pass

    return hotels


# ---------------------------------------------------------------------------
# Source 4: Curated list of well-known NYC/Chelsea hotels
#           Prices are July 2026 estimates (peak season, ~15% above typical midpoint)
# ---------------------------------------------------------------------------

_CURATED = [
    {
        "name": "The Maritime Hotel",
        "address": "363 W 16th St, New York, NY 10011",
        "lat": 40.7420, "lon": -74.0030,
        "stars": 4.0, "rating": 8.0,
        "typical_low": 250, "typical_high": 450,
        "direct_url": "https://www.themaritimehotel.com",
    },
    {
        "name": "The High Line Hotel",
        "address": "180 10th Ave, New York, NY 10011",
        "lat": 40.7461, "lon": -74.0072,
        "stars": 4.0, "rating": 9.0,
        "typical_low": 310, "typical_high": 560,
        "direct_url": "https://www.thehighlinehotel.com",
    },
    {
        "name": "Hotel Gansevoort",
        "address": "18 9th Ave, New York, NY 10014",
        "lat": 40.7406, "lon": -74.0057,
        "stars": 4.0, "rating": 8.4,
        "typical_low": 300, "typical_high": 580,
        "direct_url": "https://www.gansevoorthotelgroup.com/meatpacking",
    },
    {
        "name": "The Standard, High Line",
        "address": "848 Washington St, New York, NY 10014",
        "lat": 40.7410, "lon": -74.0083,
        "stars": 4.0, "rating": 8.5,
        "typical_low": 320, "typical_high": 650,
        "direct_url": "https://www.standardhotels.com/new-york/properties/high-line",
    },
    {
        "name": "Hampton Inn Manhattan/Chelsea",
        "address": "108 W 24th St, New York, NY 10011",
        "lat": 40.7447, "lon": -73.9967,
        "stars": 3.0, "rating": 8.3,
        "typical_low": 200, "typical_high": 400,
        "direct_url": "https://www.hilton.com/en/hotels/nycshhn-hampton-inn-manhattan-chelsea/",
    },
    {
        "name": "Motto by Hilton New York City Chelsea",
        "address": "113 W 24th St, New York, NY 10011",
        "lat": 40.7449, "lon": -73.9965,
        "stars": 3.0, "rating": 8.0,
        "typical_low": 180, "typical_high": 360,
        "direct_url": "https://www.hilton.com/en/hotels/nycmtmo-motto-new-york-city-chelsea/",
    },
    {
        "name": "Hyatt Place New York City/Chelsea",
        "address": "325 W 28th St, New York, NY 10001",
        "lat": 40.7490, "lon": -74.0001,
        "stars": 3.0, "rating": 8.2,
        "typical_low": 200, "typical_high": 420,
        "direct_url": "https://www.hyatt.com/en-US/hotel/new-york/hyatt-place-new-york-city-chelsea/nyczc",
    },
    {
        "name": "The Moxy NYC Chelsea",
        "address": "105 W 28th St, New York, NY 10001",
        "lat": 40.7481, "lon": -73.9973,
        "stars": 3.0, "rating": 8.6,
        "typical_low": 190, "typical_high": 400,
        "direct_url": "https://www.marriott.com/hotels/travel/nycox-moxy-nyc-chelsea/",
    },
    {
        "name": "Hotel Indigo New York – Chelsea",
        "address": "127 W 28th St, New York, NY 10001",
        "lat": 40.7485, "lon": -73.9969,
        "stars": 4.0, "rating": 8.6,
        "typical_low": 240, "typical_high": 460,
        "direct_url": "https://www.ihg.com/hotelindigo/hotels/us/en/new-york/nycch/hoteldetail",
    },
    {
        "name": "MADE Hotel",
        "address": "44 W 29th St, New York, NY 10001",
        "lat": 40.7456, "lon": -73.9931,
        "stars": 4.0, "rating": 8.5,
        "typical_low": 220, "typical_high": 420,
        "direct_url": "https://www.madehotel.com",
    },
    {
        "name": "Courtyard by Marriott New York Manhattan/Chelsea",
        "address": "135 W 30th St, New York, NY 10001",
        "lat": 40.7494, "lon": -73.9961,
        "stars": 3.0, "rating": 8.1,
        "typical_low": 220, "typical_high": 430,
        "direct_url": "https://www.marriott.com/hotels/travel/nycch-courtyard-new-york-manhattan-chelsea/",
    },
    {
        "name": "Kimpton Ink48 Hotel",
        "address": "653 11th Ave, New York, NY 10036",
        "lat": 40.7633, "lon": -74.0027,
        "stars": 4.0, "rating": 8.8,
        "typical_low": 280, "typical_high": 500,
        "direct_url": "https://www.ink48.com",
    },
    {
        "name": "Pendry Manhattan West",
        "address": "438 W 36th St, New York, NY 10018",
        "lat": 40.7545, "lon": -74.0014,
        "stars": 5.0, "rating": 9.2,
        "typical_low": 400, "typical_high": 800,
        "direct_url": "https://www.pendry.com/manhattan-west/",
    },
    {
        "name": "Voco Times Square South, an IHG Hotel",
        "address": "326 W 40th St, New York, NY 10018",
        "lat": 40.7570, "lon": -74.0003,
        "stars": 4.0, "rating": 8.3,
        "typical_low": 260, "typical_high": 480,
        "direct_url": "https://www.ihg.com/voco/hotels/us/en/new-york/nycxs/hoteldetail",
    },
    {
        "name": "citizenM New York Times Square",
        "address": "218 W 50th St, New York, NY 10019",
        "lat": 40.7624, "lon": -73.9844,
        "stars": 4.0, "rating": 8.8,
        "typical_low": 190, "typical_high": 360,
        "direct_url": "https://www.citizenm.com/hotels/united-states/new-york/new-york-times-square-hotel",
    },
    {
        "name": "citizenM New York Bowery",
        "address": "185 Bowery, New York, NY 10002",
        "lat": 40.7206, "lon": -73.9942,
        "stars": 4.0, "rating": 8.9,
        "typical_low": 200, "typical_high": 380,
        "direct_url": "https://www.citizenm.com/hotels/united-states/new-york/new-york-bowery-hotel",
    },
    {
        "name": "The Ludlow Hotel",
        "address": "180 Ludlow St, New York, NY 10002",
        "lat": 40.7199, "lon": -73.9884,
        "stars": 4.0, "rating": 8.7,
        "typical_low": 270, "typical_high": 500,
        "direct_url": "https://www.ludlowhotel.com",
    },
    {
        "name": "EVEN Hotel New York – Midtown East",
        "address": "221 E 44th St, New York, NY 10017",
        "lat": 40.7506, "lon": -73.9736,
        "stars": 3.0, "rating": 8.4,
        "typical_low": 210, "typical_high": 390,
        "direct_url": "https://www.ihg.com/evenhotels/hotels/us/en/new-york/nycmh/hoteldetail",
    },
    {
        "name": "AC Hotel by Marriott New York Downtown",
        "address": "151 Maiden Ln, New York, NY 10038",
        "lat": 40.7074, "lon": -74.0057,
        "stars": 4.0, "rating": 8.5,
        "typical_low": 230, "typical_high": 450,
        "direct_url": "https://www.marriott.com/hotels/travel/nycbm-ac-hotel-new-york-downtown/",
    },
    {
        "name": "1 Hotel Brooklyn Bridge",
        "address": "60 Furman St, Brooklyn, NY 11201",
        "lat": 40.6981, "lon": -73.9984,
        "stars": 5.0, "rating": 9.0,
        "typical_low": 450, "typical_high": 900,
        "direct_url": "https://www.1hotels.com/brooklyn-bridge",
    },
]


def search_curated(checkin: str, checkout: str, lat: float, lon: float,
                   cap_usd: float, nights: int) -> list[Hotel]:
    """Curated NYC/Chelsea hotels with July 2026 peak-season estimated prices."""
    JULY_FACTOR = 1.15  # July is NYC peak; prices ~15% above baseline midpoint

    hotels = []
    for h in _CURATED:
        dist = haversine_km(lat, lon, h["lat"], h["lon"])
        est = (h["typical_low"] + h["typical_high"]) / 2 * JULY_FACTOR

        # Include up to 30% over cap (user mentioned credits)
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
            url=h["direct_url"],
            booking_url=booking_com_url(name, checkin, checkout),
            lat=h["lat"],
            lon=h["lon"],
            distance_km=dist,
            source="curated-estimate",
        ))
    return hotels


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def find_hotels(checkin: str, checkout: str, location: str,
                cap_usd: float, ref_lat: Optional[float] = None,
                ref_lon: Optional[float] = None,
                verbose: bool = True) -> list[Hotel]:
    ci = datetime.strptime(checkin, "%Y-%m-%d")
    co = datetime.strptime(checkout, "%Y-%m-%d")
    nights = (co - ci).days
    if nights <= 0:
        raise ValueError(f"checkout must be after checkin (got {nights} nights)")

    if verbose:
        print(f"\nSearching hotels near: {location}")
        print(f"Dates: {checkin} → {checkout} ({nights} nights)")
        print(f"Budget: ${cap_usd:.0f}/night  (showing up to ${cap_usd * 1.30:.0f} for credit-worthy options)\n")

    if ref_lat is None or ref_lon is None:
        ref_lat, ref_lon = geocode(location)
    if verbose:
        print(f"Anchor: {ref_lat:.4f}, {ref_lon:.4f}\n")

    # Try live sources first; always fall through to curated
    live_sources = [
        ("Amadeus sandbox API", search_amadeus),
        ("Booking.com scraper", search_booking),
    ]

    live_hotels: list[Hotel] = []
    for name, fn in live_sources:
        if verbose:
            print(f"  Trying {name}...", end=" ", flush=True)
        try:
            results = fn(checkin, checkout, ref_lat, ref_lon, cap_usd, nights)
        except Exception as e:
            results = []
            print(f"\n    Error: {e}", file=sys.stderr)
        if verbose:
            print(f"{len(results)} result(s)")
        live_hotels.extend(results)

    # Always include curated list
    if verbose:
        print("  Loading curated hotel list...", end=" ", flush=True)
    curated = search_curated(checkin, checkout, ref_lat, ref_lon, cap_usd, nights)
    if verbose:
        print(f"{len(curated)} result(s)")

    # Merge: live data wins for hotels with matching names
    live_names = {re.sub(r"\W+", "", h.name.lower()) for h in live_hotels}
    merged = list(live_hotels)
    for h in curated:
        key = re.sub(r"\W+", "", h.name.lower())
        if key not in live_names:
            merged.append(h)

    if not merged:
        print("\nNo hotels found.", file=sys.stderr)
        return []

    # Sort: within cap → closest first; over cap → price ascending
    within = sorted(
        [h for h in merged if h.price_per_night <= cap_usd],
        key=lambda h: (h.distance_km or 99, h.price_per_night),
    )
    over = sorted(
        [h for h in merged if h.price_per_night > cap_usd],
        key=lambda h: h.price_per_night,
    )

    return within + over


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def _stars(n: Optional[float]) -> str:
    if not n:
        return ""
    return "★" * int(n)


def format_results(hotels: list[Hotel], cap_usd: float, nights: int,
                   checkin: str, checkout: str) -> str:
    if not hotels:
        return "No hotels found matching your criteria."

    has_live = any(h.source not in ("curated-estimate",) for h in hotels)
    price_note = "live price" if has_live else "est. July 2026 price"

    lines = [
        "",
        "=" * 72,
        f"{'HOTEL RESULTS':^72}",
        "=" * 72,
    ]

    if not has_live:
        lines += [
            "",
            "  NOTE: Live booking APIs unavailable. Prices below are July 2026",
            "  estimates based on typical rates + peak-season adjustment (~15%).",
            "  Click the booking link to see real-time prices.",
            "",
        ]

    within = [h for h in hotels if h.price_per_night <= cap_usd]
    over = [h for h in hotels if h.price_per_night > cap_usd]

    if within:
        lines.append(f"\n  WITHIN ${cap_usd:.0f}/NIGHT CAP\n")
        for i, h in enumerate(within, 1):
            lines.extend(_fmt(i, h, nights, price_note))

    if over:
        lines.append(f"\n  OVER CAP — USE YOUR CREDITS  (${cap_usd:.0f}–${cap_usd*1.3:.0f}/night)\n")
        for i, h in enumerate(over, 1):
            lines.extend(_fmt(len(within) + i, h, nights, price_note))

    lines += [
        "",
        "=" * 72,
        f"  {nights} nights  |  {checkin} → {checkout}  |  {len(hotels)} hotel(s) shown",
        "=" * 72,
        "",
    ]
    return "\n".join(lines)


def _fmt(rank: int, h: Hotel, nights: int, price_note: str) -> list[str]:
    stars = _stars(h.stars)
    rating = f"  ·  {h.rating:.1f}/10" if h.rating else ""
    dist = f"  ·  {h.distance_km:.2f} km from office" if h.distance_km is not None else ""
    total = f"${h.total_price:.0f} total"
    per = f"${h.price_per_night:.0f}/night"

    lines = [
        f"  {rank:>2}.  {h.name}",
        f"        {stars}{rating}{dist}",
        f"        {per}  ({total} / {nights} nights)  [{price_note}]",
        f"        {h.address}",
        f"        Book → {h.booking_url or h.url}",
        "",
    ]
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Find hotels near a location within a nightly price cap.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 hotel_finder.py \\
      --checkin 2026-07-12 --checkout 2026-07-19 \\
      --location "111 8th Avenue, Chelsea, New York" --cap 470

  python3 hotel_finder.py \\
      --checkin 2026-07-12 --checkout 2026-07-19 \\
      --lat 40.7422 --lon -74.0041 \\
      --location "Google NYC" --cap 470 --json
""",
    )
    parser.add_argument("--checkin", required=True,
                        help="Check-in date (YYYY-MM-DD)")
    parser.add_argument("--checkout", required=True,
                        help="Check-out date (YYYY-MM-DD)")
    parser.add_argument("--location", required=True,
                        help="Reference address / description for the office/venue")
    parser.add_argument("--cap", type=float, required=True,
                        help="Max nightly rate in USD including taxes")
    parser.add_argument("--lat", type=float, default=None,
                        help="Override latitude (skip geocoding)")
    parser.add_argument("--lon", type=float, default=None,
                        help="Override longitude (skip geocoding)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Output JSON instead of human-readable text")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress progress output")
    args = parser.parse_args()

    hotels = find_hotels(
        checkin=args.checkin,
        checkout=args.checkout,
        location=args.location,
        cap_usd=args.cap,
        ref_lat=args.lat,
        ref_lon=args.lon,
        verbose=not args.quiet,
    )

    ci = datetime.strptime(args.checkin, "%Y-%m-%d")
    co = datetime.strptime(args.checkout, "%Y-%m-%d")
    nights = (co - ci).days

    if args.as_json:
        print(json.dumps([{
            "rank": i + 1,
            "name": h.name,
            "price_per_night_usd": round(h.price_per_night, 2),
            "total_price_usd": round(h.total_price, 2),
            "address": h.address,
            "stars": h.stars,
            "rating": h.rating,
            "distance_km": round(h.distance_km, 3) if h.distance_km is not None else None,
            "within_cap": h.price_per_night <= args.cap,
            "booking_url": h.booking_url or h.url,
            "direct_url": h.url,
            "source": h.source,
        } for i, h in enumerate(hotels)], indent=2))
    else:
        print(format_results(hotels, args.cap, nights, args.checkin, args.checkout))


if __name__ == "__main__":
    main()
