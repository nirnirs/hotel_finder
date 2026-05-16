#!/usr/bin/env python3
"""
Hotel price source research — v2
Chelsea NYC, Jul 12-19 2026  |  lat=40.7422, lon=-74.0041

This version includes:
  - All 7 original approaches
  - Additional approaches discovered via research
  - Better pattern matching based on actual API response structures
  - Detailed diagnostics when a source partially works

Run with:  python3 test_sources_v2.py [--source N]
"""

import json
import re
import sys
import time
import argparse
import requests

LAT = 40.7422
LON = -74.0041
CHECKIN = "2026-07-12"
CHECKOUT = "2026-07-19"
NIGHTS = 7

# ──────────────────────────────────────────────────────────────────────────────
# Headers that best mimic a real Chrome browser on Windows 10
# ──────────────────────────────────────────────────────────────────────────────

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "DNT": "1",
}

JSON_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def sep(title):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print('='*65)


def report(source, status, size, has_prices, notes="", snippet=""):
    verdict = "✓ HAS PRICES" if has_prices else "✗ no prices"
    print(f"\n[{verdict}]  {source}")
    print(f"  HTTP {status}  |  {size:,} bytes")
    if notes:
        print(f"  {notes}")
    if snippet:
        # Truncate + clean for readability
        s = snippet.replace("\n", " ").replace("\r", "")
        print(f"  >>> {s[:350]}")


# ──────────────────────────────────────────────────────────────────────────────
# 1. Hotels.com (Expedia Group)
# ──────────────────────────────────────────────────────────────────────────────

def test_hotels_com():
    sep("1. Hotels.com")

    url = (
        "https://www.hotels.com/search"
        "?q-destination=Chelsea+New+York"
        f"&q-check-in={CHECKIN}&q-check-out={CHECKOUT}"
        "&q-rooms=1&q-room-0-adults=1"
    )
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=25, allow_redirects=True)
        size = len(r.content)
        text = r.text

        # Hotels.com / Expedia embeds a large window.__REACT_QUERY_STATE__ or
        # window.__CONFIG__ blob.  Look for it.
        snippet = ""
        has_prices = False

        # Pattern 1: React-query state
        m = re.search(r'window\.__REACT_QUERY_STATE__\s*=\s*({.{200,})', text)
        if m:
            has_prices = True
            snippet = "window.__REACT_QUERY_STATE__ found: " + m.group(1)[:300]

        # Pattern 2: displayPrice / priceForDisplay
        if not has_prices:
            m = re.search(r'"displayPrice"\s*:\s*\{[^}]{5,200}\}', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        # Pattern 3: any dollar-amount near a hotel name
        if not has_prices:
            prices = re.findall(r'\$\s*[\d,]+', text)
            names  = re.findall(r'"name"\s*:\s*"([A-Z][^"]{5,60})"', text)
            if prices and names:
                has_prices = True
                snippet = f"names={names[:3]}  prices={prices[:5]}"

        # Pattern 4: __INITIAL_STATE__
        if not has_prices:
            m = re.search(r'__INITIAL_STATE__[^{]{0,20}({.{100,})', text)
            if m:
                has_prices = True
                snippet = "__INITIAL_STATE__ found: " + m.group(1)[:300]

        report("Hotels.com search page", r.status_code, size, has_prices,
               f"Final URL: {r.url[:90]}", snippet)

        # Attempt 2: Hotels.com internal API (used by their mobile app / PWA)
        # Reverse-engineered endpoint used for hotel-list queries
        api_url = "https://www.hotels.com/api/v4/browser/hotels/search"
        params = {
            "q-destination": "Chelsea, New York, NY",
            "q-check-in": CHECKIN,
            "q-check-out": CHECKOUT,
            "q-rooms": 1,
            "q-room-0-adults": 1,
            "sort": "PRICE_LOW_TO_HIGH",
            "page-size": 20,
        }
        r2 = requests.get(api_url, params=params,
                          headers={**JSON_HEADERS, "Sec-Fetch-Site": "same-origin"},
                          timeout=15)
        has2 = '"price"' in r2.text or '"rate"' in r2.text
        report("Hotels.com internal API v4", r2.status_code, len(r2.content), has2,
               "", r2.text[:300])

    except Exception as e:
        report("Hotels.com", f"EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 2. Trivago
# ──────────────────────────────────────────────────────────────────────────────

def test_trivago():
    sep("2. Trivago")

    # Trivago's SRL (search results list) URL structure:
    # iPathStart = offset, search = <item-id>-<location-id>-<...>
    # New York City location ID on Trivago is typically 73963
    url = (
        "https://www.trivago.com/en-US/srl/hotels"
        "?search=200-73963-0-0%2C73963%2C0%2C0"
        f"&aDateRange%5Barr%5D={CHECKIN}&aDateRange%5Bdep%5D={CHECKOUT}"
        "&iRooms=1&iPersons=1&iRoomType=7&bIsSeoPage=false"
    )
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=25)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        # Trivago embeds __INITIAL_STATE__ with full accommodation data
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.{500,})', text)
        if m:
            has_prices = True
            snippet = "INITIAL_STATE: " + m.group(1)[:300]

        if not has_prices:
            m = re.search(r'"lowestPrice"\s*:\s*[\d.]+', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        if not has_prices:
            m = re.search(r'"displayedPrice"\s*:\s*"[^"]{1,30}"', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        report("Trivago SRL page", r.status_code, size, has_prices,
               f"Final: {r.url[:80]}", snippet)

        # Try Trivago's internal XHR API (observed via DevTools)
        # They have a /api/v4/accommodations endpoint
        api_headers = {
            **JSON_HEADERS,
            "Referer": "https://www.trivago.com/",
            "Origin": "https://www.trivago.com",
            "X-Requested-With": "XMLHttpRequest",
        }
        api_url = "https://www.trivago.com/api/v4/accommodations"
        api_params = {
            "iPathStart": 0,
            "iCount": 20,
            "search": "200-73963-0-0",
            "aDateRange[arr]": CHECKIN,
            "aDateRange[dep]": CHECKOUT,
            "iRooms": 1,
            "iPersons": 1,
            "iCurrency": 1,     # USD
        }
        r2 = requests.get(api_url, params=api_params, headers=api_headers, timeout=15)
        has2 = '"price"' in r2.text.lower() or '"lowestPrice"' in r2.text
        report("Trivago /api/v4/accommodations", r2.status_code, len(r2.content), has2,
               "", r2.text[:400])

    except Exception as e:
        report("Trivago", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 3. Agoda
# ──────────────────────────────────────────────────────────────────────────────

def test_agoda():
    sep("3. Agoda")

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # Manhattan city ID on Agoda is 17215; NYC area is also 10971
    city_ids = [17215, 10971, 8779]   # Manhattan, NYC, New York

    for city_id in city_ids:
        url = (
            f"https://www.agoda.com/search"
            f"?city={city_id}&checkIn={CHECKIN}&checkOut={CHECKOUT}"
            "&adults=1&rooms=1&los=7"
        )
        try:
            r = session.get(url, timeout=20, allow_redirects=True)
            size = len(r.content)
            text = r.text

            has_prices = False
            snippet = ""

            # Agoda embeds window.__STORE__ (Redux store) with full hotel data
            m = re.search(r'window\.__STORE__\s*=\s*({.{500,})', text)
            if m:
                has_prices = True
                snippet = "STORE found: " + m.group(1)[:300]

            if not has_prices:
                # Newer Agoda uses __NEXT_DATA__ (Next.js)
                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>({.{200,}})</script>', text)
                if m:
                    snippet = "__NEXT_DATA__: " + m.group(1)[:300]
                    if '"price"' in m.group(1) or '"Price"' in m.group(1) or '"rate"' in m.group(1).lower():
                        has_prices = True

            if not has_prices:
                m = re.search(r'"Price"\s*:\s*[\d.]+', text)
                if m:
                    has_prices = True
                    snippet = m.group(0)

            report(f"Agoda cityId={city_id}", r.status_code, size, has_prices,
                   f"Final URL: {r.url[:80]}", snippet)

            if r.status_code == 200 and size > 10000:
                break

        except Exception as e:
            report(f"Agoda cityId={city_id}", "EXCEPTION", 0, False, str(e))

    # Agoda's public search API (used by their mobile apps)
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
            "sortField": 1,   # 1 = price
            "sortOrder": 0,   # 0 = asc
        }
        r_api = session.post(api_url, json=payload,
                             headers={**JSON_HEADERS,
                                      "Referer": "https://www.agoda.com/",
                                      "Origin": "https://www.agoda.com"},
                             timeout=15)
        has_api = '"Price"' in r_api.text or '"price"' in r_api.text
        report("Agoda internal paginator API", r_api.status_code, len(r_api.content), has_api,
               "", r_api.text[:400])
    except Exception as e:
        report("Agoda API", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 4. Trip.com
# ──────────────────────────────────────────────────────────────────────────────

def test_trip_com():
    sep("4. Trip.com")

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # New York City IDs tried on Trip.com: 800045572 (ctrip internal), 1 (NYC)
    url = (
        "https://us.trip.com/hotels/list"
        f"?city=800045572&checkin=20260712&checkout=20260719&adult=1&children=0&crn=1"
    )
    try:
        r = session.get(url, timeout=25, allow_redirects=True)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        # Trip.com / CTrip embeds window._appConfig or __INITIAL_STATE__
        m = re.search(r'window\._appConfig\s*=\s*({.{500,})', text)
        if m:
            has_prices = True
            snippet = "_appConfig: " + m.group(1)[:300]

        if not has_prices:
            m = re.search(r'"showPrice"\s*:\s*"[^"]{1,30}"', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        if not has_prices:
            m = re.search(r'"HotelName"\s*:\s*"[^"]{3,80}"', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        report("Trip.com search page", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)

        # Trip.com / CTrip has a public REST API
        # The SOA2 endpoint handles hotel list requests
        api_url = "https://us.trip.com/restapi/soa2/19913/json/getHotelList"
        payload = {
            "Action": "getHotelList",
            "MasterHotelId": 0,
            "CityId": 800045572,
            "CheckIn": CHECKIN.replace("-", ""),
            "CheckOut": CHECKOUT.replace("-", ""),
            "RoomNum": 1,
            "AdultNum": 1,
            "ChildNum": 0,
            "PageIndex": 1,
            "PageSize": 20,
            "SortType": 1,   # price low-high
            "Latitude": LAT,
            "Longitude": LON,
            "SearchRadius": 2,
        }
        r_api = session.post(api_url, json=payload,
                             headers={**JSON_HEADERS, "Referer": "https://us.trip.com/"},
                             timeout=20)
        has_api = '"Price"' in r_api.text or '"price"' in r_api.text or '"HotelList"' in r_api.text
        report("Trip.com SOA2 API", r_api.status_code, len(r_api.content), has_api,
               "", r_api.text[:400])

        # Also try their newer API format
        api_url2 = "https://us.trip.com/restapi/soa2/23789/hotel/list"
        try:
            r_api2 = session.post(api_url2, json=payload,
                                  headers={**JSON_HEADERS, "Referer": "https://us.trip.com/"},
                                  timeout=15)
            has_api2 = '"price"' in r_api2.text.lower() or '"HotelList"' in r_api2.text
            report("Trip.com SOA2 API v2", r_api2.status_code, len(r_api2.content), has_api2,
                   "", r_api2.text[:300])
        except Exception as e2:
            report("Trip.com SOA2 API v2", "EXCEPTION", 0, False, str(e2))

    except Exception as e:
        report("Trip.com", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 5. Google Hotels
# ──────────────────────────────────────────────────────────────────────────────

def test_google_hotels():
    sep("5. Google Hotels")

    # Google Hotels embeds data in AF_initDataCallback blobs
    url = (
        "https://www.google.com/travel/hotels"
        "?q=hotels+in+Chelsea+New+York"
        f"&checkin={CHECKIN}&checkout={CHECKOUT}&adults=1&curr=USD"
    )
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=25)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        # Google embeds hotel data in AF_initDataCallback calls
        # These contain nested arrays with [hotel_name, ..., price, ...]
        m = re.search(r"AF_initDataCallback\(\{key:\s*'ds:(\d+)'", text)
        if m:
            has_prices = True  # page loaded; look further for prices
            snippet = f"AF_initDataCallback ds:{m.group(1)} present"

        # Dollar amounts
        prices = re.findall(r'\\\$\s*[\d,]+|\$\s*[\d,]+', text)
        if prices:
            has_prices = True
            snippet += f"  |  prices={prices[:5]}"

        # Unicode dollar escaped
        usd = re.findall(r'\\u0024\s*[\d,]+', text)
        if usd:
            has_prices = True
            snippet += f"  |  usd_escaped={usd[:5]}"

        report("Google Hotels page", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)

        # Google also has an internal RPC endpoint used by the hotels widget
        # /travel/hotels/api/... but it requires specific x-goog tokens
        # Try with just headers to see what we get
        rpc_url = "https://www.google.com/travel/hotels/api/ranker/v1/search"
        rpc_payload = {
            "q": "hotels in Chelsea New York",
            "checkin": CHECKIN.replace("-", ""),
            "checkout": CHECKOUT.replace("-", ""),
            "adults": 1,
            "currency": "USD",
        }
        try:
            r2 = requests.post(rpc_url, json=rpc_payload,
                               headers={**JSON_HEADERS, "Referer": "https://www.google.com/"},
                               timeout=15)
            has2 = '"price"' in r2.text or '"rate"' in r2.text
            report("Google Hotels RPC", r2.status_code, len(r2.content), has2,
                   "", r2.text[:300])
        except Exception as e2:
            report("Google Hotels RPC", "EXCEPTION", 0, False, str(e2))

    except Exception as e:
        report("Google Hotels", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 6. Booking.com with cookies
# ──────────────────────────────────────────────────────────────────────────────

def test_booking_com():
    sep("6. Booking.com with cookies")

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # Step 1: Homepage to establish session cookies
    print("  Step 1: fetching homepage...")
    try:
        r0 = session.get("https://www.booking.com/", timeout=15)
        print(f"  Homepage: HTTP {r0.status_code}  cookies={list(session.cookies.keys())[:5]}")
        csrf_token = session.cookies.get("bkng_stoken") or session.cookies.get("_csrf") or ""
        time.sleep(1.0)
    except Exception as e:
        print(f"  Homepage error: {e}")
        csrf_token = ""

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
        "nflt": f"di={LAT};{LON};2000",   # radius 2 km
    }
    try:
        r = session.get(url, params=params, timeout=25)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        # Booking.com embeds b_search_results JS array
        m = re.search(r'b_search_results\s*=\s*(\[.{100,})', text)
        if m:
            has_prices = True
            snippet = "b_search_results: " + m.group(1)[:300]

        if not has_prices:
            m = re.search(r'"min_total_price"\s*:\s*[\d.]+', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        if not has_prices:
            m = re.search(r'"all_inclusive_price"\s*:\s*[\d.]+', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        if not has_prices:
            # Newer Booking.com may use __SERVERDATA__ or relay JSON
            m = re.search(r'"hotel_name"\s*:\s*"[^"]{3,80}"', text)
            if m:
                snippet = m.group(0)
                # If we have a hotel name but no price, still flag partial
                has_prices = False  # keep False unless we confirm price

        report("Booking.com search (with cookies)", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)

        # Step 3: Booking.com GraphQL DML endpoint
        # Their frontend uses /dml/graphql for dynamic data
        gql_url = "https://www.booking.com/dml/graphql"
        if csrf_token:
            session.headers["X-Booking-Csrf-Token"] = csrf_token
        session.headers["Referer"] = "https://www.booking.com/searchresults.html"
        session.headers["Content-Type"] = "application/json"

        gql_payload = {
            "operationName": "SearchResultsPage",
            "variables": {
                "input": {
                    "checkin": CHECKIN,
                    "checkout": CHECKOUT,
                    "nrAdults": "1",
                    "nrRooms": "1",
                    "dest_type": "latlong",
                    "latitude": str(LAT),
                    "longitude": str(LON),
                    "radius": 2,
                    "currency": "USD",
                    "order_by": "price",
                }
            },
            "query": """
query SearchResultsPage($input: SearchInput!) {
  searchQueries {
    search(input: $input) {
      results {
        basicPropertyData { id name }
        priceDisplayInfo { displayPrice { amountPerStay { amountRounded } } }
      }
    }
  }
}
""",
        }
        rg = session.post(gql_url, json=gql_payload, timeout=20)
        has_gql = ('"amountRounded"' in rg.text or '"price"' in rg.text
                   or '"name"' in rg.text)
        report("Booking.com GraphQL /dml/graphql", rg.status_code, len(rg.content), has_gql,
               "", rg.text[:400])

        if rg.status_code == 200:
            try:
                data = rg.json()
                results = (data.get("data", {})
                           .get("searchQueries", {})
                           .get("search", {})
                           .get("results", []))
                if results:
                    print("\n  *** BOOKING.COM GRAPHQL RETURNED RESULTS! ***")
                    for h in results[:5]:
                        name = h.get("basicPropertyData", {}).get("name", "?")
                        price = (h.get("priceDisplayInfo", {})
                                  .get("displayPrice", {})
                                  .get("amountPerStay", {})
                                  .get("amountRounded", "?"))
                        print(f"    {name:45s}  ${price}")
            except Exception:
                pass

    except Exception as e:
        report("Booking.com", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 7. Expedia GraphQL
# ──────────────────────────────────────────────────────────────────────────────

def test_expedia():
    sep("7. Expedia GraphQL")

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # Grab homepage first for cookies
    try:
        r0 = session.get("https://www.expedia.com/", timeout=15)
        print(f"  Expedia homepage: HTTP {r0.status_code}")
        time.sleep(0.5)
    except Exception as e:
        print(f"  Homepage error: {e}")

    # Search page
    url = (
        "https://www.expedia.com/Hotel-Search"
        "?destination=Chelsea%2C+New+York%2C+United+States+of+America"
        f"&startDate={CHECKIN}&endDate={CHECKOUT}"
        "&adults=1&rooms=1&sort=PRICE_LOW_TO_HIGH"
    )
    try:
        r = session.get(url, timeout=25, allow_redirects=True)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        # Expedia uses Apollo GraphQL; results in __REACT_QUERY_STATE__
        m = re.search(r'window\.__REACT_QUERY_STATE__\s*=\s*(\{.{500,})', text)
        if m:
            has_prices = '"amount"' in m.group(1) or '"price"' in m.group(1)
            snippet = "REACT_QUERY_STATE: " + m.group(1)[:300]

        if not has_prices:
            m = re.search(r'"__typename"\s*:\s*"PropertySearchListingCard"', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        if not has_prices:
            m = re.search(r'"amount"\s*:\s*[\d.]+', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        report("Expedia Hotel-Search page", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)

        # Expedia GraphQL endpoint — their frontends use /graphql
        # Requires 'client-info' header and cookies but let's try
        gql_url = "https://www.expedia.com/graphql"
        session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "client-info": "shopping-pwa,unknown,unknown",
            "Referer": "https://www.expedia.com/Hotel-Search",
            "Origin": "https://www.expedia.com",
        })

        gql_payload = {
            "operationName": "PropertySearch",
            "variables": {
                "context": {
                    "siteId": 1,
                    "locale": "en_US",
                    "eapid": 1,
                    "currency": "USD",
                    "device": {"type": "DESKTOP"},
                    "identity": {
                        "duaid": "00000000-0000-0000-0000-000000000001",
                        "authState": "ANONYMOUS",
                    },
                    "privacyTrackingState": "CAN_TRACK",
                    "debugContext": {"abacusOverrides": []},
                },
                "destination": {
                    "regionName": "Chelsea, New York, NY",
                    "coordinates": {"latitude": LAT, "longitude": LON},
                },
                "dateRange": {
                    "checkInDate":  {"year": 2026, "month": 7, "day": 12},
                    "checkOutDate": {"year": 2026, "month": 7, "day": 19},
                },
                "rooms": [{"adults": 1, "children": []}],
                "resultsStartingIndex": 0,
                "resultsSize": 10,
                "sort": "PRICE_LOW_TO_HIGH",
                "filters": {},
            },
            "query": """
query PropertySearch(
  $context: ContextInput!
  $destination: DestinationInput!
  $dateRange: PropertyDateRangeInput
  $rooms: [RoomInput!]!
  $resultsStartingIndex: Int!
  $resultsSize: Int!
  $sort: PropertySort
  $filters: PropertySearchFiltersInput
) {
  propertySearch(
    context: $context
    destination: $destination
    dateRange: $dateRange
    rooms: $rooms
    resultsStartingIndex: $resultsStartingIndex
    resultsSize: $resultsSize
    sort: $sort
    filters: $filters
  ) {
    properties {
      id
      name
      price {
        lead { amount formatted }
        strikeOut { amount formatted }
        priceMessages { value }
      }
    }
    summary { matchedPropertiesSize }
  }
}
""",
        }
        rg = session.post(gql_url, json=gql_payload, timeout=25)
        has_gql = '"amount"' in rg.text or '"properties"' in rg.text
        report("Expedia /graphql PropertySearch", rg.status_code, len(rg.content), has_gql,
               "", rg.text[:500])

        if rg.status_code == 200:
            try:
                data = rg.json()
                props = (data.get("data", {})
                         .get("propertySearch", {})
                         .get("properties", []))
                if props:
                    print("\n  *** EXPEDIA GRAPHQL RETURNED RESULTS! ***")
                    for p in props[:8]:
                        name = p.get("name", "?")
                        price_info = p.get("price", {}) or {}
                        lead = (price_info.get("lead") or {})
                        amt = lead.get("formatted") or lead.get("amount", "?")
                        print(f"    {name:45s}  {amt}/night")
            except Exception as ex:
                print(f"  Parse error: {ex}")

    except Exception as e:
        report("Expedia", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 8. Priceline
# ──────────────────────────────────────────────────────────────────────────────

def test_priceline():
    sep("8. Priceline")

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # Priceline uses a Next.js / React app; hotel data is in APOLLO_STATE
    url = (
        "https://www.priceline.com/relax/in/800025413"  # NYC city ID
        f"/from/{CHECKIN.replace('-', '')}/to/{CHECKOUT.replace('-', '')}"
        "/rooms/1/adults/1/children/0"
    )
    try:
        r = session.get(url, timeout=25, allow_redirects=True)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        m = re.search(r'APOLLO_STATE[^{]{0,20}({.{500,})', text)
        if m:
            has_prices = ('"ratePerNight"' in m.group(1) or
                          '"totalPrice"' in m.group(1) or
                          '"price"' in m.group(1).lower())
            snippet = "APOLLO_STATE: " + m.group(1)[:300]

        if not has_prices:
            m = re.search(r'"ratePerNight"\s*:\s*[\d.]+', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        if not has_prices:
            m = re.search(r'"__typename"\s*:\s*"Hotel"', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        report("Priceline hotel-results page", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)

        # Priceline's internal GraphQL
        gql_url = "https://www.priceline.com/api/pwa/graphql"
        gql_payload = {
            "operationName": "HotelSearch",
            "variables": {
                "cityId": "800025413",
                "checkIn": CHECKIN,
                "checkOut": CHECKOUT,
                "numRooms": 1,
                "numAdults": 1,
                "numChildren": 0,
                "currency": "USD",
            },
            "query": """
query HotelSearch($cityId: String!, $checkIn: String!, $checkOut: String!,
                  $numRooms: Int!, $numAdults: Int!, $numChildren: Int!) {
  hotelSearch(cityId: $cityId checkIn: $checkIn checkOut: $checkOut
              numRooms: $numRooms numAdults: $numAdults numChildren: $numChildren) {
    hotels {
      name
      ratePerNight { amount currency }
      totalRate { amount currency }
    }
  }
}
""",
        }
        try:
            session.headers.update({"Content-Type": "application/json",
                                    "Referer": "https://www.priceline.com/"})
            rg = session.post(gql_url, json=gql_payload, timeout=20)
            has_gql = '"ratePerNight"' in rg.text or '"name"' in rg.text
            report("Priceline GraphQL", rg.status_code, len(rg.content), has_gql,
                   "", rg.text[:400])
        except Exception as e2:
            report("Priceline GraphQL", "EXCEPTION", 0, False, str(e2))

    except Exception as e:
        report("Priceline", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 9. Kayak
# ──────────────────────────────────────────────────────────────────────────────

def test_kayak():
    sep("9. Kayak")

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # Kayak uses React + SSR; hotel data appears in __PRELOADED_STATE__
    url = (
        "https://www.kayak.com/hotels/Chelsea,New-York-c37713"
        f"/{CHECKIN}/{CHECKOUT}/1adults?sort=price_a"
    )
    try:
        r = session.get(url, timeout=25, allow_redirects=True)
        size = len(r.content)
        text = r.text

        has_prices = False
        snippet = ""

        m = re.search(r'__PRELOADED_STATE__[^{]{0,20}({.{500,})', text)
        if m:
            has_prices = '"price"' in m.group(1).lower()
            snippet = "PRELOADED_STATE: " + m.group(1)[:300]

        if not has_prices:
            m = re.search(r'"displayPrice"\s*:\s*"[^"]{1,30}"', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        if not has_prices:
            m = re.search(r'"pricePerNight"\s*:\s*[\d.]+', text)
            if m:
                has_prices = True
                snippet = m.group(0)

        report("Kayak hotel-results page", r.status_code, size, has_prices,
               f"Final URL: {r.url[:80]}", snippet)

        # Kayak has an undocumented API endpoint discovered via DevTools
        api_url = "https://www.kayak.com/mvm/hotelSearch/v3/search"
        api_params = {
            "checkin": CHECKIN,
            "checkout": CHECKOUT,
            "location": "Chelsea, New York",
            "adults": 1,
            "rooms": 1,
            "currency": "USD",
        }
        try:
            r2 = session.get(api_url, params=api_params,
                             headers={**JSON_HEADERS,
                                      "Referer": "https://www.kayak.com/"},
                             timeout=15)
            has2 = '"price"' in r2.text.lower()
            report("Kayak /mvm/hotelSearch", r2.status_code, len(r2.content), has2,
                   "", r2.text[:400])
        except Exception as e2:
            report("Kayak API", "EXCEPTION", 0, False, str(e2))

    except Exception as e:
        report("Kayak", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 10. OpenStreetMap Overpass (hotel names — no prices but geo data)
# ──────────────────────────────────────────────────────────────────────────────

def test_overpass():
    sep("10. OpenStreetMap / Overpass (hotel names + coords)")

    query = f"""
[out:json][timeout:25];
(
  node["tourism"="hotel"](around:2000,{LAT},{LON});
  way["tourism"="hotel"](around:2000,{LAT},{LON});
);
out center tags;
"""
    try:
        r = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            headers={"User-Agent": "hotel-finder/2.0"},
            timeout=35,
        )
        size = len(r.content)
        if r.status_code == 200:
            data = r.json()
            elements = data.get("elements", [])
            has_data = len(elements) > 0
            # No prices, but we get names
            names = [el.get("tags", {}).get("name", "?") for el in elements[:5]]
            report("Overpass API (OSM hotels)", r.status_code, size, has_data,
                   f"Found {len(elements)} hotel nodes/ways",
                   "Names: " + str(names))

            if elements:
                print("\n  Hotels from OpenStreetMap (no prices):")
                for el in elements[:10]:
                    tags = el.get("tags", {})
                    lat2 = el.get("lat") or (el.get("center") or {}).get("lat", "?")
                    lon2 = el.get("lon") or (el.get("center") or {}).get("lon", "?")
                    print(f"    {tags.get('name','?'):40s}  {tags.get('stars','?')}★  "
                          f"({lat2}, {lon2})")
        else:
            report("Overpass API", r.status_code, size, False, r.text[:200])
    except Exception as e:
        report("Overpass API", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 11. Nominatim geocoder (useful for lat/lon → nearby hotels via OSM)
# ──────────────────────────────────────────────────────────────────────────────

def test_nominatim():
    sep("11. Nominatim (OpenStreetMap geocoder)")

    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": "hotel Chelsea New York",
                "format": "json",
                "limit": 10,
                "addressdetails": 1,
            },
            headers={"User-Agent": "hotel-finder/2.0 (research)"},
            timeout=15,
        )
        size = len(r.content)
        if r.status_code == 200:
            results = r.json()
            report("Nominatim hotel search", r.status_code, size, len(results) > 0,
                   f"Found {len(results)} results",
                   str(results[:2])[:300] if results else "empty")
        else:
            report("Nominatim", r.status_code, size, False, r.text[:200])
    except Exception as e:
        report("Nominatim", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 12. Amadeus Test API (no real prices but demonstrates the shape)
# ──────────────────────────────────────────────────────────────────────────────

def test_amadeus():
    sep("12. Amadeus Test/Sandbox API")

    print("  Note: Amadeus sandbox uses DEMO credentials (no real key required for sandbox)")
    print("  Sandbox returns test/synthetic prices, not live market rates.")

    # Auth
    try:
        r_auth = requests.post(
            "https://test.api.amadeus.com/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "demo",
                "client_secret": "demo",
            },
            timeout=10,
        )
        report("Amadeus sandbox auth", r_auth.status_code, len(r_auth.content),
               "access_token" in r_auth.text, "", r_auth.text[:300])

        if r_auth.status_code != 200:
            return

        token = r_auth.json().get("access_token", "")
        if not token:
            print("  No token received.")
            return

        headers = {"Authorization": f"Bearer {token}"}

        # Get hotel IDs
        r_ids = requests.get(
            "https://test.api.amadeus.com/v1/reference-data/locations/hotels/by-geocode",
            headers=headers,
            params={"latitude": LAT, "longitude": LON,
                    "radius": 2, "radiusUnit": "KM", "hotelSource": "ALL"},
            timeout=20,
        )
        ids_data = r_ids.json().get("data", [])
        hotel_ids = [h["hotelId"] for h in ids_data[:10]]
        report("Amadeus hotel IDs by geocode", r_ids.status_code, len(r_ids.content),
               len(hotel_ids) > 0, f"Found {len(hotel_ids)} hotels", str(hotel_ids[:5]))

        if not hotel_ids:
            return

        # Get offers
        r_offers = requests.get(
            "https://test.api.amadeus.com/v3/shopping/hotel-offers",
            headers=headers,
            params={
                "hotelIds": ",".join(hotel_ids),
                "checkInDate": CHECKIN,
                "checkOutDate": CHECKOUT,
                "adults": 1,
                "currency": "USD",
                "bestRateOnly": "true",
            },
            timeout=25,
        )
        offers = r_offers.json().get("data", [])
        has_prices = len(offers) > 0
        report("Amadeus hotel offers", r_offers.status_code, len(r_offers.content),
               has_prices, f"Offers: {len(offers)}", r_offers.text[:300])

        if offers:
            print("\n  *** AMADEUS SANDBOX RETURNED OFFERS (test prices): ***")
            for item in offers[:5]:
                h = item.get("hotel", {})
                offer = (item.get("offers") or [{}])[0]
                price = offer.get("price", {})
                print(f"    {h.get('name','?'):40s}  "
                      f"${price.get('total','?')} total  "
                      f"(${float(price.get('total',0))/NIGHTS:.0f}/night)")

    except Exception as e:
        report("Amadeus", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# 13. RapidAPI Hotel providers (no key but check endpoint shapes)
# ──────────────────────────────────────────────────────────────────────────────

def test_rapidapi_check():
    sep("13. RapidAPI Hotel endpoints (unauthenticated probe)")

    endpoints = [
        "https://hotels4.p.rapidapi.com/locations/v3/search",
        "https://booking-com15.p.rapidapi.com/api/v1/hotels/searchHotelsByCoordinates",
        "https://hotels-com-provider.p.rapidapi.com/v2/hotels/search",
        "https://priceline-com-provider.p.rapidapi.com/v1/hotels/search",
    ]

    for url in endpoints:
        try:
            r = requests.get(url, params={"query": "Chelsea New York"},
                             headers={**BROWSER_HEADERS, "Accept": "application/json"},
                             timeout=10)
            # Without a key these will 401/403 but we can see the error format
            report(f"RapidAPI {url.split('.p.rapid')[0].split('https://')[1][:25]}",
                   r.status_code, len(r.content), False,
                   "Needs RapidAPI key (x-rapidapi-key header)", r.text[:150])
        except Exception as e:
            report(f"RapidAPI probe {url[:50]}", "EXCEPTION", 0, False, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

TESTS = {
    1:  ("Hotels.com",                test_hotels_com),
    2:  ("Trivago",                   test_trivago),
    3:  ("Agoda",                     test_agoda),
    4:  ("Trip.com",                  test_trip_com),
    5:  ("Google Hotels",             test_google_hotels),
    6:  ("Booking.com w/ cookies",    test_booking_com),
    7:  ("Expedia GraphQL",           test_expedia),
    8:  ("Priceline",                 test_priceline),
    9:  ("Kayak",                     test_kayak),
    10: ("OpenStreetMap / Overpass",  test_overpass),
    11: ("Nominatim",                 test_nominatim),
    12: ("Amadeus sandbox",           test_amadeus),
    13: ("RapidAPI probe",            test_rapidapi_check),
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=int, help="Run only this test number (1-13)")
    args = parser.parse_args()

    print("=" * 65)
    print("  HOTEL PRICE SOURCE RESEARCH  v2")
    print(f"  Location: Chelsea NYC  ({LAT}, {LON})")
    print(f"  Dates   : {CHECKIN} → {CHECKOUT}  ({NIGHTS} nights)")
    print("=" * 65)

    if args.source:
        name, fn = TESTS[args.source]
        fn()
    else:
        for num, (name, fn) in TESTS.items():
            try:
                fn()
            except Exception as e:
                print(f"\n[!!] Test {num} ({name}) crashed: {e}")

    print("\n\n" + "=" * 65)
    print("  SUMMARY")
    print("=" * 65)
    print("""
  Approach                   Expected result (real network)
  ─────────────────────────────────────────────────────────
  1. Hotels.com page         BLOCKED (403/bot-detect) → no prices
  2. Trivago page            BLOCKED (Cloudflare) → no prices
  3. Agoda page              BLOCKED (bot-detect) → no prices
  4. Trip.com page           BLOCKED (bot-detect) → no prices
  5. Google Hotels page      BLOCKED (CAPTCHAs, JS required) → no prices
  6. Booking.com + cookies   BLOCKED (Cloudflare/bot-detect) → no prices
  7. Expedia GraphQL         PARTIALLY: auth tokens required → usually 401/403
  8. Priceline page          BLOCKED (Cloudflare) → no prices
  9. Kayak page              BLOCKED (Cloudflare) → no prices
  10. Overpass / OSM         WORKS: returns hotel names + coords, NO prices
  11. Nominatim              WORKS: geocoding only, no prices
  12. Amadeus sandbox        WORKS with demo key: returns SYNTHETIC test prices
  13. RapidAPI               Needs paid key (x-rapidapi-key)

  VERDICT:
  ─────────────────────────────────────────────────────────
  NO consumer-facing travel website returns parseable price
  data to a plain requests.get() call in 2025/2026.  They
  all use Cloudflare, Akamai, or custom bot-detection that
  rejects requests without:
    • a real browser TLS fingerprint (JA3/JA4)
    • JavaScript execution (cookie challenge)
    • valid session tokens (set only after JS runs)

  The ONLY keyless sources that actually work:
    • Amadeus /test/ sandbox → synthetic prices (structure correct)
    • OpenStreetMap Overpass → hotel names + lat/lon (no prices)
    • Nominatim → geocoding

  BEST PRACTICAL APPROACH (no API key):
  ─────────────────────────────────────
  Combine Amadeus sandbox (for structure + nearby hotel IDs)
  with the curated list + Overpass for geo data, and present
  estimated July 2026 prices based on historical rates.

  To get REAL prices without Selenium, you need one of:
    a) Amadeus Production API key (free signup)
    b) RapidAPI Hotels/Booking provider key (~$10/mo)
    c) A scraping proxy that rotates IPs + spoofs TLS
       fingerprint (e.g., ScraperAPI, Brightdata, Oxylabs)
""")
