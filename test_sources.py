#!/usr/bin/env python3
"""
Test multiple hotel price data sources using only requests library.
Target: Chelsea, NYC  |  Jul 12-19 2026  |  lat=40.7422, lon=-74.0041
"""

import json
import re
import sys
import time
import requests

LAT = 40.7422
LON = -74.0041
CHECKIN = "2026-07-12"
CHECKOUT = "2026-07-19"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def report(source, status, size, has_prices, notes="", snippet=""):
    print(f"\n{'='*60}")
    print(f"SOURCE: {source}")
    print(f"  Status:     {status}")
    print(f"  Size:       {size} bytes")
    print(f"  Has prices: {has_prices}")
    if notes:
        print(f"  Notes:      {notes}")
    if snippet:
        print(f"  Snippet:    {snippet[:400]}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Hotels.com
# ─────────────────────────────────────────────────────────────────────────────

def test_hotels_com():
    url = (
        "https://www.hotels.com/search"
        "?q-destination=Chelsea+New+York"
        "&q-check-in=2026-07-12&q-check-out=2026-07-19"
        "&q-rooms=1&q-room-0-adults=1"
    )
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=20, allow_redirects=True)
        size = len(r.content)
        text = r.text

        # Look for embedded JSON
        has_prices = False
        snippet = ""

        # Try to find JSON blobs with price data
        patterns = [
            r'"price":\s*\{[^}]{10,200}\}',
            r'"displayPrice":\s*"[^"]{1,30}"',
            r'"rate":\s*\{[^}]{10,200}\}',
            r'"__INITIAL_STATE__"\s*:\s*(\{.{0,100})',
            r'window\.__data\s*=\s*(\{.{0,100})',
            r'"hotelName":\s*"([^"]{3,80})"',
            r'"name":\s*"([^"]{3,80})".*?"price"',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                has_prices = True
                snippet = m.group(0)[:400]
                break

        report("1. Hotels.com", r.status_code, size, has_prices,
               f"URL: {r.url[:80]}", snippet)
        return r.status_code, size, has_prices, text
    except Exception as e:
        report("1. Hotels.com", f"ERROR: {e}", 0, False)
        return None, 0, False, ""


# ─────────────────────────────────────────────────────────────────────────────
# 2. Trivago
# ─────────────────────────────────────────────────────────────────────────────

def test_trivago():
    # Try their internal API endpoint
    endpoints = [
        "https://www.trivago.com/en-US/srl/hotels",
        "https://www.trivago.com/api/v4/hotels",
        "https://www.trivago.com/api/v2/accommodation",
    ]

    # First try the main search page
    url = "https://www.trivago.com/en-US/srl/hotels?search=200-4132-0-0%2C4132%2C0%2C0&iPathStart=1&iRooms=1&iPersons=1&aDateRange[arr]=2026-07-12&aDateRange[dep]=2026-07-19&iRoomType=7&aPriceRange[from]=0&aPriceRange[to]=0&iViewType=0&bIsSeoPage=false&bIgnoreImplicitFilters=false"

    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        patterns = [
            r'"price":\s*\{[^}]{10,200}\}',
            r'"displayedPrice":\s*"[^"]{1,30}"',
            r'"lowestPrice":\s*[\d.]+',
            r'"name":\s*"[^"]{3,80}"[^}]{0,100}"price"',
            r'window\.__INITIAL_STATE',
            r'"accommodations":\s*\[',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                has_prices = True
                snippet = m.group(0)[:400]
                break

        report("2. Trivago (main page)", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)

        # Also try JSON API
        api_url = "https://www.trivago.com/api/v4/hotels"
        params = {
            "location": "New York Chelsea",
            "checkin": CHECKIN,
            "checkout": CHECKOUT,
            "adults": 1,
        }
        try:
            r2 = requests.get(api_url, params=params, headers={**BROWSER_HEADERS, "Accept": "application/json"}, timeout=15)
            report("2b. Trivago API v4", r2.status_code, len(r2.content), False,
                   f"Response: {r2.text[:200]}")
        except Exception as e2:
            report("2b. Trivago API v4", f"ERROR: {e2}", 0, False)

        return r.status_code, size, has_prices, text
    except Exception as e:
        report("2. Trivago", f"ERROR: {e}", 0, False)
        return None, 0, False, ""


# ─────────────────────────────────────────────────────────────────────────────
# 3. Agoda
# ─────────────────────────────────────────────────────────────────────────────

def test_agoda():
    # NYC city ID attempts: 17215, 14327 (Manhattan)
    urls = [
        "https://www.agoda.com/search?city=17215&checkIn=2026-07-12&checkOut=2026-07-19&adults=1",
        "https://www.agoda.com/search?city=14327&checkIn=2026-07-12&checkOut=2026-07-19&adults=1",
    ]

    for url in urls:
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=20, allow_redirects=True)
            size = len(r.content)
            text = r.text

            has_prices = False
            snippet = ""

            patterns = [
                r'"price":\s*\{[^}]{10,200}\}',
                r'"priceForDisplay":\s*"[^"]{1,30}"',
                r'"lowestPrice":\s*[\d.]+',
                r'"hotelName":\s*"[^"]{3,80}"',
                r'"displayPrice":\s*[\d.]+',
                r'window\.__INITIAL_DATA__',
                r'"hotels":\s*\[',
                r'"Price":\s*[\d.]+',
            ]
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    has_prices = True
                    snippet = m.group(0)[:400]
                    break

            report(f"3. Agoda ({url[-30:]})", r.status_code, size, has_prices,
                   f"Final URL: {r.url[:80]}", snippet)

            if r.status_code == 200 and size > 10000:
                break

        except Exception as e:
            report("3. Agoda", f"ERROR: {e}", 0, False)

    # Try Agoda's internal API
    try:
        api_url = "https://www.agoda.com/api/zh-cn/paginator/api/search"
        payload = {
            "cityId": 17215,
            "checkIn": CHECKIN,
            "checkOut": CHECKOUT,
            "rooms": 1,
            "adults": 1,
            "children": 0,
            "pageNo": 1,
            "pageSize": 20,
        }
        api_headers = {**BROWSER_HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
        r3 = requests.post(api_url, json=payload, headers=api_headers, timeout=15)
        report("3b. Agoda internal API", r3.status_code, len(r3.content), False,
               f"Response: {r3.text[:300]}")
    except Exception as e:
        report("3b. Agoda internal API", f"ERROR: {e}", 0, False)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Trip.com
# ─────────────────────────────────────────────────────────────────────────────

def test_trip_com():
    # NYC city IDs to try
    urls = [
        "https://us.trip.com/hotels/list?city=800045572&checkin=20260712&checkout=20260719&adult=1&children=0",
        "https://us.trip.com/hotels/list?cityId=800045572&checkin=20260712&checkout=20260719&adult=1&children=0",
    ]

    for url in urls:
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=20, allow_redirects=True)
            size = len(r.content)
            text = r.text

            has_prices = False
            snippet = ""

            patterns = [
                r'"price":\s*\{[^}]{10,200}\}',
                r'"price":\s*[\d.]+',
                r'"showPrice":\s*"[^"]{1,30}"',
                r'"hotelName":\s*"[^"]{3,80}"',
                r'"HotelName":\s*"[^"]{3,80}"',
                r'window\._appConfig\s*=',
                r'"hotels":\s*\[',
                r'"HotelList":\s*\[',
            ]
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    has_prices = True
                    snippet = m.group(0)[:400]
                    break

            report(f"4. Trip.com ({url[-40:]})", r.status_code, size, has_prices,
                   f"Final URL: {r.url[:80]}", snippet)
            if r.status_code == 200:
                break
        except Exception as e:
            report("4. Trip.com", f"ERROR: {e}", 0, False)

    # Try Trip.com's internal API
    try:
        api_url = "https://us.trip.com/restapi/soa2/19913/json/getHotelList"
        payload = {
            "Action": "getHotelList",
            "cityId": 800045572,
            "checkIn": CHECKIN.replace("-", ""),
            "checkOut": CHECKOUT.replace("-", ""),
            "roomNum": 1,
            "adultNum": 1,
            "childNum": 0,
            "pageIndex": 1,
            "pageSize": 20,
        }
        api_headers = {**BROWSER_HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
        r2 = requests.post(api_url, json=payload, headers=api_headers, timeout=15)
        report("4b. Trip.com API", r2.status_code, len(r2.content),
               '"price"' in r2.text.lower() or '"Price"' in r2.text,
               f"Response: {r2.text[:300]}")
    except Exception as e:
        report("4b. Trip.com API", f"ERROR: {e}", 0, False)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Google Hotels JSON
# ─────────────────────────────────────────────────────────────────────────────

def test_google_hotels():
    url = (
        "https://www.google.com/travel/hotels"
        "?q=hotels+in+Chelsea+New+York"
        f"&checkin={CHECKIN}&checkout={CHECKOUT}&adults=1"
    )
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        patterns = [
            r'\[\d+,\s*"[^"]{5,80}"\s*,\s*null\s*,\s*null\s*,\s*\[\s*\[\s*\[\s*\[\s*[\d.]+',  # price in nested array
            r'"price":\s*[\d.]+',
            r'"displayPrice":\s*"[^"]{1,30}"',
            r'AF_initDataCallback\(\{key:\s*\'ds:',
            r'\["\$\d+',
            r'\\u0024\s*[\d,]+',  # unicode dollar sign followed by price
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                has_prices = True
                snippet = m.group(0)[:400]
                break

        # Also look for hotel names + dollar amounts
        price_match = re.findall(r'\$\s*[\d,]+', text)
        if price_match:
            has_prices = True
            snippet = str(price_match[:5])

        report("5. Google Hotels", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)

        # Try the search page directly with specific params
        url2 = "https://www.google.com/search?q=hotels+chelsea+new+york+july+2026&tbm=lcl"
        r2 = requests.get(url2, headers=BROWSER_HEADERS, timeout=15)
        snippet2 = ""
        prices2 = re.findall(r'\$[\d,]+', r2.text)
        if prices2:
            snippet2 = str(prices2[:5])
        report("5b. Google Search Hotels", r2.status_code, len(r2.content),
               bool(prices2), "", snippet2)

        return r.status_code, size, has_prices, text
    except Exception as e:
        report("5. Google Hotels", f"ERROR: {e}", 0, False)
        return None, 0, False, ""


# ─────────────────────────────────────────────────────────────────────────────
# 6. Booking.com with session cookies
# ─────────────────────────────────────────────────────────────────────────────

def test_booking_com():
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # Step 1: Get homepage to establish cookies
    print("\n[Booking.com] Fetching homepage for cookies...")
    try:
        r0 = session.get("https://www.booking.com", timeout=15)
        print(f"  Homepage: {r0.status_code}, cookies: {dict(session.cookies)}")
    except Exception as e:
        print(f"  Homepage error: {e}")

    time.sleep(1)

    # Step 2: Search
    url = "https://www.booking.com/searchresults.html"
    params = {
        "ss": "Chelsea, New York",
        "latitude": LAT,
        "longitude": LON,
        "checkin": CHECKIN,
        "checkout": CHECKOUT,
        "group_adults": 1,
        "no_rooms": 1,
        "order": "price",
        "selected_currency": "USD",
    }
    try:
        r = session.get(url, params=params, timeout=20)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        patterns = [
            r'b_search_results\s*=\s*(\[.{100,500})',
            r'"min_total_price":\s*[\d.]+',
            r'"price_breakdown":\s*\{[^}]{10,200}\}',
            r'"hotel_name":\s*"[^"]{3,80}"',
            r'"all_inclusive_price":\s*[\d.]+',
            r'data-hotelid="\d+"[^>]*data-price="[\d.]+"',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                has_prices = True
                snippet = m.group(0)[:400]
                break

        report("6. Booking.com (with cookies)", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)

        # Also try their GraphQL endpoint
        gql_url = "https://www.booking.com/dml/graphql"
        gql_headers = {**BROWSER_HEADERS,
                       "Content-Type": "application/json",
                       "Accept": "application/json",
                       "X-Booking-Csrf": ""}
        gql_payload = {
            "operationName": "SearchResultsPage",
            "variables": {
                "input": {
                    "checkin": CHECKIN,
                    "checkout": CHECKOUT,
                    "nrAdults": 1,
                    "nrRooms": 1,
                    "dest_type": "latlong",
                    "latitude": LAT,
                    "longitude": LON,
                    "radius": 2,
                }
            },
            "query": "query SearchResultsPage($input: SearchInput!) { hotels(input: $input) { name price { total } } }"
        }
        try:
            rg = session.post(gql_url, json=gql_payload, headers=gql_headers, timeout=15)
            report("6b. Booking.com GraphQL", rg.status_code, len(rg.content),
                   '"price"' in rg.text, f"Response: {rg.text[:300]}")
        except Exception as e2:
            report("6b. Booking.com GraphQL", f"ERROR: {e2}", 0, False)

        return r.status_code, size, has_prices, text
    except Exception as e:
        report("6. Booking.com", f"ERROR: {e}", 0, False)
        return None, 0, False, ""


# ─────────────────────────────────────────────────────────────────────────────
# 7. Expedia GraphQL / internal API
# ─────────────────────────────────────────────────────────────────────────────

def test_expedia():
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # First, get the search page
    url = (
        "https://www.expedia.com/Hotel-Search"
        "?destination=Chelsea%2C+New+York%2C+United+States+of+America"
        f"&startDate={CHECKIN}&endDate={CHECKOUT}"
        "&adults=1&rooms=1"
    )
    try:
        r = session.get(url, timeout=20, allow_redirects=True)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        patterns = [
            r'"price":\s*\{[^}]{10,200}\}',
            r'"lead":\s*\{[^}]{10,200}\}',
            r'"amount":\s*[\d.]+',
            r'"hotelName":\s*"[^"]{3,80}"',
            r'"name":\s*"[^"]{3,80}"[^}]{0,100}"price"',
            r'window\.__INITIAL_STATE__',
            r'"__typename":\s*"PropertySearchListingCard"',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                has_prices = True
                snippet = m.group(0)[:400]
                break

        report("7. Expedia Search Page", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)

        # Try Expedia's internal API used by their frontend
        api_url = "https://www.expedia.com/graphql"
        api_headers = {
            **BROWSER_HEADERS,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "client-info": "shopping-pwa,unknown,unknown",
        }
        gql_payload = {
            "operationName": "PropertySearch",
            "variables": {
                "context": {
                    "siteId": 1,
                    "locale": "en_US",
                    "eapid": 1,
                    "currency": "USD",
                    "device": {"type": "DESKTOP"},
                    "identity": {"duaid": "test", "authState": "ANONYMOUS"},
                    "privacyTrackingState": "CAN_TRACK",
                    "debugContext": {"abacusOverrides": []},
                },
                "destination": {
                    "regionName": "Chelsea, New York, NY",
                    "coordinates": {"latitude": LAT, "longitude": LON},
                },
                "dateRange": {
                    "checkInDate": {"year": 2026, "month": 7, "day": 12},
                    "checkOutDate": {"year": 2026, "month": 7, "day": 19},
                },
                "rooms": [{"adults": 1, "children": []}],
                "resultsStartingIndex": 0,
                "resultsSize": 10,
                "sort": "PRICE_LOW_TO_HIGH",
            },
            "query": """
query PropertySearch($context: ContextInput!, $destination: DestinationInput!, $dateRange: PropertyDateRangeInput, $rooms: [RoomInput!]!, $resultsStartingIndex: Int!, $resultsSize: Int!, $sort: PropertySort) {
  propertySearch(
    context: $context
    destination: $destination
    dateRange: $dateRange
    rooms: $rooms
    resultsStartingIndex: $resultsStartingIndex
    resultsSize: $resultsSize
    sort: $sort
  ) {
    properties {
      name
      price { lead { amount } }
    }
  }
}
""",
        }
        try:
            rg = session.post(api_url, json=gql_payload, headers=api_headers, timeout=20)
            has_gql_prices = '"amount"' in rg.text or '"price"' in rg.text
            report("7b. Expedia GraphQL", rg.status_code, len(rg.content),
                   has_gql_prices, f"Response: {rg.text[:500]}")

            if rg.status_code == 200:
                try:
                    data = rg.json()
                    props = (data.get("data", {}).get("propertySearch", {}) or {}).get("properties", [])
                    if props:
                        print("  *** EXPEDIA GRAPHQL RETURNED HOTELS! ***")
                        for p in props[:5]:
                            print(f"    - {p.get('name')} | price: {p.get('price')}")
                except Exception:
                    pass
        except Exception as e2:
            report("7b. Expedia GraphQL", f"ERROR: {e2}", 0, False)

        return r.status_code, size, has_prices, text
    except Exception as e:
        report("7. Expedia", f"ERROR: {e}", 0, False)
        return None, 0, False, ""


# ─────────────────────────────────────────────────────────────────────────────
# 8. BONUS: Kayak / HotelsCombined / Priceline
# ─────────────────────────────────────────────────────────────────────────────

def test_priceline():
    url = (
        "https://www.priceline.com/hotel/search"
        "?city=New+York&state=NY&country=US"
        f"&check_in={CHECKIN}&check_out={CHECKOUT}&num_rooms=1&num_guests=1"
    )
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=20, allow_redirects=True)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        patterns = [
            r'"price":\s*[\d.]+',
            r'"ratePerNight":\s*[\d.]+',
            r'"totalPrice":\s*[\d.]+',
            r'"hotelName":\s*"[^"]{3,80}"',
            r'"__typename":\s*"Hotel"',
            r'window\.__INITIAL_STATE__',
            r'APOLLO_STATE',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                has_prices = True
                snippet = m.group(0)[:400]
                break

        report("8. Priceline", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)
        return r.status_code, size, has_prices, text
    except Exception as e:
        report("8. Priceline", f"ERROR: {e}", 0, False)
        return None, 0, False, ""


def test_kayak():
    url = (
        "https://www.kayak.com/hotels/Chelsea,New-York-c37713"
        f"/{CHECKIN}/{CHECKOUT}/1adults?sort=price_a"
    )
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=20, allow_redirects=True)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        patterns = [
            r'"price":\s*[\d.]+',
            r'"displayPrice":\s*"[^"]{1,30}"',
            r'"pricePerNight":\s*[\d.]+',
            r'"name":\s*"[^"]{3,80}"[^}]{0,200}"price"',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                has_prices = True
                snippet = m.group(0)[:400]
                break

        report("8b. Kayak", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)
        return r.status_code, size, has_prices, text
    except Exception as e:
        report("8b. Kayak", f"ERROR: {e}", 0, False)
        return None, 0, False, ""


# ─────────────────────────────────────────────────────────────────────────────
# 9. BONUS: Hotelbeds / RateHawk / SerpAPI Google Hotels
# ─────────────────────────────────────────────────────────────────────────────

def test_serp_google_hotels():
    """Try Google's Knowledge Graph hotel API (no auth needed for basic queries)."""
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google_hotels",
        "q": "hotels in Chelsea New York",
        "check_in_date": CHECKIN,
        "check_out_date": CHECKOUT,
        "adults": 1,
        "currency": "USD",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        has_prices = '"price"' in r.text or '"rate"' in r.text
        report("9. SerpAPI Google Hotels (no key)", r.status_code, len(r.content),
               has_prices, f"Response: {r.text[:300]}")
    except Exception as e:
        report("9. SerpAPI Google Hotels", f"ERROR: {e}", 0, False)


def test_makcorps():
    """MakCorps Hotel API - has a free tier."""
    url = "https://api.makcorps.com/free/newyork"
    params = {
        "checkin": CHECKIN,
        "checkout": CHECKOUT,
        "adults": 1,
        "rooms": 1,
    }
    headers = {**BROWSER_HEADERS, "Accept": "application/json"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        has_prices = '"price"' in r.text.lower() or '"rate"' in r.text.lower()
        report("10. MakCorps Free API", r.status_code, len(r.content),
               has_prices, f"Response: {r.text[:400]}")
    except Exception as e:
        report("10. MakCorps Free API", f"ERROR: {e}", 0, False)


def test_hotelscombined():
    """HotelsCombined (owned by Booking) - check if they have a public API."""
    url = (
        "https://www.hotelscombined.com/Hotels/New-York_New-York_United-States"
        f"?adults=1&checkin={CHECKIN}&checkout={CHECKOUT}&rooms=1"
    )
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=20, allow_redirects=True)
        size = len(r.content)
        text = r.text
        has_prices = any(p in text for p in ['"price"', '"Price"', '"rate"', '"Rate"'])
        snippet = ""
        m = re.search(r'"price[^"]*":\s*[\d.]+', text)
        if m:
            snippet = m.group(0)
        report("11. HotelsCombined", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)
    except Exception as e:
        report("11. HotelsCombined", f"ERROR: {e}", 0, False)


# ─────────────────────────────────────────────────────────────────────────────
# Run all tests
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("HOTEL PRICE DATA SOURCE RESEARCH")
    print(f"Location: Chelsea NYC  ({LAT}, {LON})")
    print(f"Dates: {CHECKIN} to {CHECKOUT}")
    print("=" * 60)

    print("\n\n>>> TEST 1: Hotels.com")
    test_hotels_com()

    print("\n\n>>> TEST 2: Trivago")
    test_trivago()

    print("\n\n>>> TEST 3: Agoda")
    test_agoda()

    print("\n\n>>> TEST 4: Trip.com")
    test_trip_com()

    print("\n\n>>> TEST 5: Google Hotels")
    test_google_hotels()

    print("\n\n>>> TEST 6: Booking.com with cookies")
    test_booking_com()

    print("\n\n>>> TEST 7: Expedia GraphQL")
    test_expedia()

    print("\n\n>>> TEST 8: Priceline + Kayak")
    test_priceline()
    test_kayak()

    print("\n\n>>> TEST 9/10/11: Additional sources")
    test_serp_google_hotels()
    test_makcorps()
    test_hotelscombined()

    print("\n\nDone. Check results above for working sources.")
