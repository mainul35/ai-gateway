"""Places, routes, and what is along the way, from OpenStreetMap.

Three free services do the work, none of which needs a key:

  * Nominatim turns words into coordinates and coordinates back into an address
  * OSRM works out a route between points
  * Overpass finds things of a kind near a point, or strung along a route

They are public services run on donated hardware, and their usage policies are the reason for the
throttle below and for the identifying User-Agent: one request a second to Nominatim, no bulk work,
and an address somebody could write to. A gateway that hammers them gets blocked, deservedly. If
this is ever used heavily the honest answer is to run Nominatim and OSRM on the homelab, which is a
docker-compose away and needs a regional extract rather than the planet.

Google is not called at all. It needs a key and a billing account, and everything here works
without one - but a Google Maps link is a URL, so the results carry one for opening the place or
the route over there.
"""
import asyncio
import logging
import math
import re
import time

import httpx

log = logging.getLogger("tools.maps")

NOMINATIM = "https://nominatim.openstreetmap.org"
OSRM = "https://router.project-osrm.org"
# Tried in order. Both cover the whole planet, which is the only kind of mirror that belongs here:
# overpass.osm.ch was in this list until it answered "200 OK, nothing found" for Dhaka, because it
# carries Switzerland and nothing else. A regional extract does not fail when asked about somewhere
# it has never heard of - it agrees that there is nothing there, which is far worse than an error.
OVERPASS_SERVERS = ("https://overpass-api.de/api/interpreter",
                    "https://overpass.private.coffee/api/interpreter")
USER_AGENT = "ai-gateway/0.1 (+https://github.com/mainul35/ai-gateway)"

NOMINATIM_EVERY = 1.1        # seconds between calls, which is their published limit plus a margin
SEARCH_TIMEOUT = 25
ROUTE_TIMEOUT = 30
OVERPASS_TIMEOUT = 60
ALONG_SAMPLES = 6            # points taken along a route to look around, kept few: each one is a
                             # separate sub-query and a dozen of them times the free server out
MAX_RESULTS = 8

_last_nominatim = 0.0
_nominatim_lock = asyncio.Lock()


class MapError(Exception):
    pass


# --- what the user might be asking for ----------------------------------------------------------
# The plain words people use, and the OpenStreetMap tag that actually finds the thing
AMENITIES = {
    "fuel": ("amenity", "fuel", ("petrol", "petrol station", "petrol pump", "gas station", "fuel",
                                 "filling station", "cng")),
    "restaurant": ("amenity", "restaurant", ("restaurant", "place to eat", "dinner", "lunch",
                                             "food", "eatery")),
    "cafe": ("amenity", "cafe", ("cafe", "coffee", "coffee shop", "tea")),
    "fast_food": ("amenity", "fast_food", ("fast food", "burger", "takeaway")),
    "hotel": ("tourism", "hotel", ("hotel", "place to stay", "accommodation", "guest house")),
    "hospital": ("amenity", "hospital", ("hospital", "emergency")),
    "clinic": ("amenity", "clinic", ("clinic", "doctor")),
    "pharmacy": ("amenity", "pharmacy", ("pharmacy", "chemist", "medicine shop", "drug store")),
    "atm": ("amenity", "atm", ("atm", "cash machine", "cash point")),
    "bank": ("amenity", "bank", ("bank",)),
    "toilets": ("amenity", "toilets", ("toilet", "toilets", "restroom", "washroom")),
    "parking": ("amenity", "parking", ("parking", "car park")),
    "charging_station": ("amenity", "charging_station", ("charging", "ev charger", "charging point")),
    "supermarket": ("shop", "supermarket", ("supermarket", "grocery", "groceries")),
    "convenience": ("shop", "convenience", ("convenience store", "convenience", "corner shop",
                                            "konbini")),
    "pharmacy_shop": ("shop", "chemist", ("chemist shop",)),
    "school": ("amenity", "school", ("school",)),
    "university": ("amenity", "university", ("university", "college")),
    "place_of_worship": ("amenity", "place_of_worship", ("mosque", "masjid", "temple", "church",
                                                         "place of worship")),
    "park": ("leisure", "park", ("park", "green space", "playground")),
    "viewpoint": ("tourism", "viewpoint", ("viewpoint", "scenic spot", "lookout")),
    "attraction": ("tourism", "attraction", ("attraction", "sightseeing", "tourist spot",
                                             "things to see")),
    "museum": ("tourism", "museum", ("museum",)),
    "bus_station": ("amenity", "bus_station", ("bus station", "bus stand")),
    "railway_station": ("railway", "station", ("train station", "railway station")),
    "airport": ("aeroway", "aerodrome", ("airport",)),
}

TRANSPORT = {"driving": ("driving", ("drive", "driving", "car", "by road", "taxi")),
             "cycling": ("cycling", ("cycle", "cycling", "bike", "bicycle")),
             "foot": ("foot", ("walk", "walking", "on foot", "foot"))}

# 23.8103, 90.4125 - and the same with N/S/E/W, or in degrees and minutes
DECIMAL_PAIR = re.compile(
    r"(?<![\d.])([-+]?\d{1,2}(?:\.\d+)?)\s*°?\s*([NnSs])?\s*[,;/ ]\s*([-+]?\d{1,3}(?:\.\d+)?)\s*°?\s*([EeWw])?(?![\d.])")
DMS = re.compile(
    r"(\d{1,3})\s*°\s*(\d{1,2})\s*['′]\s*(\d{1,2}(?:\.\d+)?)\s*[\"″]?\s*([NnSsEeWw])")


def parse_points(text):
    """Every coordinate written in the message, as (lat, lon) pairs."""
    found = []
    degrees = DMS.findall(text or "")
    if len(degrees) >= 2:
        values = []
        for whole, minutes, seconds, hemisphere in degrees:
            value = int(whole) + int(minutes) / 60 + float(seconds) / 3600
            if hemisphere.upper() in ("S", "W"):
                value = -value
            values.append((value, hemisphere.upper()))
        for i in range(0, len(values) - 1, 2):
            first, second = values[i], values[i + 1]
            lat, lon = (first[0], second[0]) if first[1] in ("N", "S") else (second[0], first[0])
            if _sane(lat, lon):
                found.append((lat, lon))
        if found:
            return found
    for lat, ns, lon, ew in DECIMAL_PAIR.findall(text or ""):
        lat, lon = float(lat), float(lon)
        if ns and ns.upper() == "S":
            lat = -lat
        if ew and ew.upper() == "W":
            lon = -lon
        if _sane(lat, lon):
            found.append((lat, lon))
    return found


def _sane(lat, lon):
    # A pair like "2, 3" is far more likely to be a number than a spot in the Atlantic, so a
    # coordinate has to carry a decimal point somewhere to be believed
    return -90 <= lat <= 90 and -180 <= lon <= 180 and (lat % 1 or lon % 1)


def amenity_for(text):
    """The OSM tag the words are asking for, or None."""
    lowered = (text or "").lower()
    best = None
    for key, (tag, value, words) in AMENITIES.items():
        for word in words:
            if re.search(rf"\b{re.escape(word)}s?\b", lowered) and (best is None or len(word) > best[2]):
                best = (tag, value, len(word))
    return (best[0], best[1]) if best else None


def transport_for(text):
    lowered = (text or "").lower()
    for mode, (profile, words) in TRANSPORT.items():
        if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in words):
            return profile
    return "driving"


async def _client():
    return httpx.AsyncClient(timeout=SEARCH_TIMEOUT,
                             headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})


async def _be_polite():
    """Nominatim asks for no more than one request a second, so that is what it gets."""
    global _last_nominatim
    async with _nominatim_lock:
        wait = NOMINATIM_EVERY - (time.monotonic() - _last_nominatim)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_nominatim = time.monotonic()


def _place(row):
    return {
        "name": (row.get("name") or row.get("display_name", "").split(",")[0] or "").strip(),
        "address": row.get("display_name", ""),
        "lat": float(row["lat"]), "lon": float(row["lon"]),
        "kind": f"{row.get('category', '')}/{row.get('type', '')}".strip("/"),
        "osm": f"https://www.openstreetmap.org/{row.get('osm_type', 'node')}/{row.get('osm_id')}"
               if row.get("osm_id") else None,
    }


async def search(query, near=None, limit=MAX_RESULTS):
    """Places matching the words. `near` biases the results towards a point."""
    if not (query or "").strip():
        raise MapError("There is nothing to search for.")
    params = {"q": query, "format": "jsonv2", "limit": str(limit), "addressdetails": "1",
              "accept-language": "en"}
    if near:
        lat, lon = near
        # A box roughly 60 km on a side, preferred but not required, so a nearby match wins
        params.update(viewbox=f"{lon - 0.3},{lat + 0.3},{lon + 0.3},{lat - 0.3}", bounded="0")
    await _be_polite()
    async with await _client() as client:
        try:
            response = await client.get(f"{NOMINATIM}/search", params=params)
            response.raise_for_status()
            rows = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MapError(f"The map search did not answer ({e}).")
    return [_place(row) for row in rows if row.get("lat")]


async def reverse(lat, lon):
    """What is at a coordinate."""
    await _be_polite()
    async with await _client() as client:
        try:
            response = await client.get(f"{NOMINATIM}/reverse",
                                        params={"lat": lat, "lon": lon, "format": "jsonv2",
                                                "accept-language": "en"})
            response.raise_for_status()
            row = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MapError(f"That coordinate could not be looked up ({e}).")
    if "error" in row:
        raise MapError(f"Nothing is mapped at {lat:.5f}, {lon:.5f}.")
    return _place(row)


async def route(points, mode="driving"):
    """A route through the given (lat, lon) points."""
    if len(points) < 2:
        raise MapError("A route needs somewhere to start and somewhere to finish.")
    path = ";".join(f"{lon},{lat}" for lat, lon in points)
    async with httpx.AsyncClient(timeout=ROUTE_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        try:
            response = await client.get(f"{OSRM}/route/v1/{mode}/{path}",
                                        params={"overview": "simplified", "geometries": "geojson"})
            response.raise_for_status()
            answer = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MapError(f"The routing service did not answer ({e}).")
    if answer.get("code") != "Ok" or not answer.get("routes"):
        raise MapError("No route was found between those places.")
    best = answer["routes"][0]
    return {
        "mode": mode,
        "distance_km": round(best["distance"] / 1000, 1),
        "minutes": round(best["duration"] / 60),
        # (lat, lon) for the browser, which is the order every map library wants
        "line": [[point[1], point[0]] for point in best["geometry"]["coordinates"]],
    }


def _thin(line, wanted):
    """`wanted` points spread evenly along a line, including both ends."""
    if len(line) <= wanted:
        return list(line)
    step = (len(line) - 1) / (wanted - 1)
    return [line[round(i * step)] for i in range(wanted)]


async def _overpass(query):
    """Asks each mirror in turn. A timeout from one of these means loaded, not broken."""
    trouble = None
    async with httpx.AsyncClient(timeout=OVERPASS_TIMEOUT,
                                 headers={"User-Agent": USER_AGENT}) as client:
        # The main server again at the end: these refuse when loaded, and a moment later they do not
        for attempt, server in enumerate(OVERPASS_SERVERS + (OVERPASS_SERVERS[0],)):
            if attempt == len(OVERPASS_SERVERS):
                await asyncio.sleep(2)
            try:
                response = await client.post(server, data={"data": query})
                response.raise_for_status()
                return response.json().get("elements", [])
            except (httpx.HTTPError, ValueError) as e:
                trouble = e
                log.info("overpass %s did not answer (%s)", server, e)
    raise MapError(f"The place search did not answer ({trouble}). These are busy free services; "
                   "trying again in a minute usually works.")


def _from_overpass(element):
    tags = element.get("tags") or {}
    centre = element.get("center") or {}
    lat = element.get("lat") if element.get("lat") is not None else centre.get("lat")
    lon = element.get("lon") if element.get("lon") is not None else centre.get("lon")
    if lat is None or lon is None:
        return None
    address = ", ".join(filter(None, [tags.get("addr:street"), tags.get("addr:city")]))
    return {"name": tags.get("name") or "(unnamed)", "address": address,
            "lat": float(lat), "lon": float(lon),
            "kind": tags.get("amenity") or tags.get("shop") or tags.get("tourism") or "",
            "osm": f"https://www.openstreetmap.org/{element.get('type', 'node')}/{element.get('id')}"}


async def nearby(tag, lat, lon, radius_m=3000, limit=MAX_RESULTS, words=""):
    """Things of a kind near a point.

    Overpass is the right tool and a busy one. When every mirror is refusing, Nominatim can be asked
    the same question in words: the answers are the same OpenStreetMap data, found by name rather
    than by tag, so there are fewer of them and they are less complete. Fewer real places beats an
    error page, as long as the answer admits which way round it was.
    """
    key, value = tag
    query = (f'[out:json][timeout:25];(node["{key}"="{value}"](around:{int(radius_m)},{lat},{lon});'
             f'way["{key}"="{value}"](around:{int(radius_m)},{lat},{lon}););out center {limit * 3};')
    try:
        elements = await _overpass(query)
    except MapError:
        if not words:
            raise
        log.info("overpass unavailable; falling back to a name search for %r", words)
        found = await search(words, near=(lat, lon), limit=limit)
        for place in found:
            place["metres_away"] = round(_metres(lat, lon, place["lat"], place["lon"]))
            place["by_name"] = True
        return [p for p in sorted(found, key=lambda p: p["metres_away"])
                if p["metres_away"] <= radius_m * 4][:limit]
    places = [p for p in map(_from_overpass, elements) if p]
    places.sort(key=lambda p: (p["name"] == "(unnamed)", _metres(lat, lon, p["lat"], p["lon"])))
    for place in places:
        place["metres_away"] = round(_metres(lat, lon, place["lat"], place["lon"]))
    return places[:limit]


async def along(tag, line, radius_m=1500, limit=MAX_RESULTS):
    """Things of a kind strung along a route, in the order they are passed."""
    key, value = tag
    samples = _thin(line, ALONG_SAMPLES)
    around = "".join(
        f'node["{key}"="{value}"](around:{int(radius_m)},{lat},{lon});'
        f'way["{key}"="{value}"](around:{int(radius_m)},{lat},{lon});'
        for lat, lon in samples)
    places = [p for p in map(_from_overpass, await _overpass(
        f"[out:json][timeout:90];({around});out center {limit * 4};")) if p]

    seen, ordered = set(), []
    for place in places:
        if place["osm"] in seen:
            continue
        seen.add(place["osm"])
        # How far along the route it sits, so the list reads in travelling order
        nearest = min(range(len(samples)),
                      key=lambda i: _metres(samples[i][0], samples[i][1], place["lat"], place["lon"]))
        place["along"] = nearest / max(len(samples) - 1, 1)
        place["metres_away"] = round(_metres(samples[nearest][0], samples[nearest][1],
                                             place["lat"], place["lon"]))
        ordered.append(place)
    ordered.sort(key=lambda p: (p["along"], p["name"] == "(unnamed)", p["metres_away"]))
    named = [p for p in ordered if p["name"] != "(unnamed)"]
    return (named or ordered)[:limit]


def distance(lat1, lon1, lat2, lon2):
    """Metres between two coordinates, for anything outside this module that needs to sort by it."""
    return _metres(lat1, lon1, lat2, lon2)


def _metres(lat1, lon1, lat2, lon2):
    """Great-circle distance, near enough for sorting a list of nearby places."""
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((phi2 - phi1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def nearest_pair(first, second):
    """Out of two lists of candidates, the one place from each that sits closest to the other.

    "from Gulshan to the airport" gave a two and a half thousand kilometre route, because Gulshan
    on its own is a neighbourhood in Multan before it is a neighbourhood in Dhaka. Nothing in the
    words says which; what says it is that one of the pairings is ten kilometres apart and the
    other is most of a continent. A journey between two places somebody names in one breath is the
    short one, near enough always.
    """
    if not first or not second:
        return (first[0] if first else None), (second[0] if second else None)
    best = min(((a, b) for a in first for b in second),
               key=lambda pair: _metres(pair[0]["lat"], pair[0]["lon"], pair[1]["lat"], pair[1]["lon"]))
    return best


def google_link(place=None, start=None, end=None, mode="driving"):
    """A Google Maps URL, which needs no key: the search is done here, the opening is done there."""
    if start and end:
        modes = {"driving": "driving", "cycling": "bicycling", "foot": "walking"}
        return (f"https://www.google.com/maps/dir/?api=1&origin={start[0]},{start[1]}"
                f"&destination={end[0]},{end[1]}&travelmode={modes.get(mode, 'driving')}")
    if place:
        return f"https://www.google.com/maps/search/?api=1&query={place['lat']},{place['lon']}"
    return None


def bounds(places, line=()):
    """The corners of a box holding everything, for the map to open on."""
    points = [(p["lat"], p["lon"]) for p in places] + [tuple(p) for p in line]
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return [[min(lats), min(lons)], [max(lats), max(lons)]]
