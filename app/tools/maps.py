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
                    "https://overpass.private.coffee/api/interpreter",
                    "https://maps.mail.ru/osm/tools/overpass/api/interpreter")
USER_AGENT = "ai-gateway/0.1 (+https://github.com/mainul35/ai-gateway)"

NOMINATIM_EVERY = 1.1        # seconds between calls, which is their published limit plus a margin
SEARCH_TIMEOUT = 25
ROUTE_TIMEOUT = 30
OVERPASS_TIMEOUT = 25
# Near first, then wide. Overpass answers "out center 24" with whatever twenty-four it finds
# first, not the twenty-four nearest - asked for restaurants within fifteen kilometres of a street
# in Tokyo it returned none closer than seven, with hundreds nearer that it simply did not mention.
# So the near radius is asked first and only an empty answer widens it, and the whole search runs
# against a clock, because a proxy in front of this stops waiting long before Overpass does.
RADII = (2000, 15000)
SEARCH_RADIUS = RADII[-1]
SEARCH_BUDGET = 40           # seconds for the whole search, however many requests that is
ALONG_SAMPLES = 6            # points taken along a route to look around, kept few: each one is a
                             # separate sub-query and a dozen of them times the free server out
MAX_RESULTS = 8

_last_nominatim = 0.0
_nominatim_lock = asyncio.Lock()

# Answers are kept because places do not move. An hour old is as true as a second old for where a
# mosque is, and on a day when every mirror is refusing - which happens - a stale answer is the
# difference between the feature working and the feature not existing.
CACHE_SECONDS = 3600
CACHE_LIMIT = 300
_answers = {}


class MapError(Exception):
    pass


# --- what the user might be asking for ----------------------------------------------------------
# The plain words people use, and the OpenStreetMap tag that actually finds the thing
AMENITIES = {
    # A place of worship is not one kind of place. Asked for a mosque, amenity=place_of_worship on
    # its own returns temples and churches too, which is the kind of wrong answer that is worse
    # than no answer. The religion is part of the question, so it is part of the query.
    "mosque": ({"amenity": "place_of_worship", "religion": "muslim"},
               ("mosque", "masjid", "musalla", "muslim prayer room", "muslim prayer space",
                "prayer room", "prayer space", "jamaat khana", "islamic centre", "islamic center",
                "muslim prayer")),
    "church": ({"amenity": "place_of_worship", "religion": "christian"},
               ("church", "chapel", "cathedral", "christian")),
    "hindu_temple": ({"amenity": "place_of_worship", "religion": "hindu"},
                     ("hindu temple", "mandir", "hindu")),
    "buddhist_temple": ({"amenity": "place_of_worship", "religion": "buddhist"},
                        ("buddhist temple", "buddhist", "pagoda")),
    "shinto_shrine": ({"amenity": "place_of_worship", "religion": "shinto"},
                      ("shinto shrine", "shinto", "jinja")),
    "synagogue": ({"amenity": "place_of_worship", "religion": "jewish"},
                  ("synagogue", "jewish")),
    "gurdwara": ({"amenity": "place_of_worship", "religion": "sikh"}, ("gurdwara", "sikh")),
    # Only when no religion was named at all
    "place_of_worship": ({"amenity": "place_of_worship"},
                         ("place of worship", "temple", "shrine", "worship")),

    "fuel": ({"amenity": "fuel"}, ("petrol", "petrol station", "petrol pump", "gas station", "fuel",
                                   "filling station", "cng")),
    "restaurant": ({"amenity": "restaurant"}, ("restaurant", "place to eat", "dinner", "lunch",
                                               "food", "eatery", "somewhere to eat")),
    "halal": ({"amenity": "restaurant", "diet:halal": "yes"}, ("halal", "halal food",
                                                               "halal restaurant")),
    "cafe": ({"amenity": "cafe"}, ("cafe", "coffee", "coffee shop", "tea")),
    "fast_food": ({"amenity": "fast_food"}, ("fast food", "burger", "takeaway")),
    "hotel": ({"tourism": "hotel"}, ("hotel", "place to stay", "accommodation", "guest house")),
    "hospital": ({"amenity": "hospital"}, ("hospital", "emergency")),
    "clinic": ({"amenity": "clinic"}, ("clinic", "doctor")),
    "pharmacy": ({"amenity": "pharmacy"}, ("pharmacy", "chemist", "medicine shop", "drug store")),
    "atm": ({"amenity": "atm"}, ("atm", "cash machine", "cash point")),
    "bank": ({"amenity": "bank"}, ("bank",)),
    "toilets": ({"amenity": "toilets"}, ("toilet", "toilets", "restroom", "washroom")),
    "parking": ({"amenity": "parking"}, ("parking", "car park")),
    "charging_station": ({"amenity": "charging_station"}, ("charging", "ev charger",
                                                           "charging point")),
    "supermarket": ({"shop": "supermarket"}, ("supermarket", "grocery", "groceries")),
    "convenience": ({"shop": "convenience"}, ("convenience store", "convenience", "corner shop",
                                              "konbini")),
    "school": ({"amenity": "school"}, ("school",)),
    "university": ({"amenity": "university"}, ("university", "college")),
    "park": ({"leisure": "park"}, ("park", "green space", "playground")),
    "viewpoint": ({"tourism": "viewpoint"}, ("viewpoint", "scenic spot", "lookout")),
    "attraction": ({"tourism": "attraction"}, ("attraction", "sightseeing", "tourist spot",
                                               "things to see")),
    "museum": ({"tourism": "museum"}, ("museum",)),
    "bus_station": ({"amenity": "bus_station"}, ("bus station", "bus stand")),
    "railway_station": ({"railway": "station"}, ("train station", "railway station", "station")),
    "airport": ({"aeroway": "aerodrome"}, ("airport",)),
}

TRANSPORT = {"driving": ("driving", ("drive", "driving", "car", "by road", "taxi")),
             "cycling": ("cycling", ("cycle", "cycling", "bike", "bicycle")),
             "foot": ("foot", ("walk", "walking", "on foot", "foot")),
             "transit": ("transit", ("train", "metro", "subway", "underground", "bus", "rail",
                                     "public transport", "transit", "tube"))}

# The public OSRM accepts every profile name in the URL and answers all of them from the same car
# data: ask it to walk thirteen kilometres and it says fourteen minutes. So only driving is real
# here, and anything else is answered with the road route and told plainly what it is. Google is
# offered instead, because Google does have walking, cycling and transit.
ROUTED = "driving"
NOT_ROUTED = {
    "foot": "This is the road route. The routing service here has car data only, so it cannot "
            "time a walk - the link opens the same journey in Google Maps, which can.",
    "cycling": "This is the road route. The routing service here has car data only, so it cannot "
               "time a ride - the link opens the same journey in Google Maps, which can.",
    "transit": "This is the road route. Nothing here knows train or bus timetables - the link "
               "opens the same journey in Google Maps, which does.",
}

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
    """The OSM tags the words are asking for, or None.

    The longest wording wins, so "buddhist temple" beats "temple" and "muslim prayer room" beats
    "prayer room": the more a person said, the more exactly they should be answered.
    """
    lowered = (text or "").lower()
    best, longest = None, 0
    for key, (tags, words) in AMENITIES.items():
        for word in words:
            if re.search(rf"\b{re.escape(word)}s?\b", lowered) and len(word) > longest:
                best, longest = tags, len(word)
    return best


# "no taxi" and "I don't have a car" both name a car, and both mean the opposite of choosing one.
# "I prefer less walking" is the same shape. So a mode word is only taken as a choice when nothing
# just before it turns it down.
REFUSED = re.compile(
    # Either apostrophe, or none: people type don't, don\u2019t and dont, and a pattern that knows
    # only one of the three reads "I don't have a car" as a request for a car
    r"\b(?:no|not|never|without|avoid(?:ing)?|hate|dislike|less|minimal|minimum|"
    r"can['\u2019]?t|cannot|cant|don['\u2019]?t|do not|doesn['\u2019]?t|won['\u2019]?t|"
    r"rather than|instead of|other than|except)\b"
    # Within a short reach and not across the end of a sentence, so "I have a car. Walk?" is safe
    r"[^.;!?]{0,25}$", re.I)


def transport_for(text):
    """How the person wants to travel, reading refusals as refusals.

    When every way of travelling they mentioned was one they were ruling out, the answer is what
    is left: somebody with no car, no taxi and a dislike of walking is describing a train.
    """
    lowered = (text or "").lower()
    chosen, refused = [], []
    for mode, (profile, words) in TRANSPORT.items():
        for word in words:
            for found in re.finditer(rf"\b{re.escape(word)}\b", lowered):
                before = lowered[max(0, found.start() - 40):found.start()]
                (refused if REFUSED.search(before) else chosen).append(profile)
                break
    for profile in ("transit", "foot", "cycling", "driving"):
        if profile in chosen:
            return profile
    if refused:
        # Everything named was ruled out. What is left of the four is the answer, and when a car
        # is among the refusals the honest reading is public transport.
        left = [p for p in ("transit", "foot", "cycling", "driving") if p not in refused]
        return left[0] if left else "transit"
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


async def find(words, near=None, limit=MAX_RESULTS):
    """Search, then try harder: a name nobody wrote down exactly is still a name of somewhere.

    "Koyama Futako Tamagawa" finds nothing, because it is two place names run together and
    Nominatim matches the whole string or nothing. Dropping a word from the front, then from the
    back, turns it into "Futako Tamagawa", which is a station in Setagaya. Each attempt is a
    separate request to a service that asks for one a second, so there are at most three.
    """
    attempts = [words]
    pieces = (words or "").split()
    if len(pieces) > 2:
        attempts.append(" ".join(pieces[1:]))     # without the first word
        attempts.append(" ".join(pieces[:-1]))    # without the last
    elif len(pieces) == 2:
        attempts.append(pieces[-1])
    seen = set()
    for attempt in attempts:
        attempt = attempt.strip()
        if not attempt or attempt.lower() in seen:
            continue
        seen.add(attempt.lower())
        found = await search(attempt, near=near, limit=limit)
        if found:
            if attempt != words:
                log.info("map: %r found nothing, %r did", words, attempt)
                for place in found:
                    place["matched"] = attempt
            return found
    return []


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
    """A route through the given (lat, lon) points, and what it is honestly a route for."""
    if len(points) < 2:
        raise MapError("A route needs somewhere to start and somewhere to finish.")
    asked, mode = mode, ROUTED          # everything is driven, whatever was asked
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
        "asked_for": asked,
        "note": NOT_ROUTED.get(asked),
        "distance_km": round(best["distance"] / 1000, 1),
        "minutes": round(best["duration"] / 60),
        # (lat, lon) for the browser, which is the order every map library wants
        "line": [[point[1], point[0]] for point in best["geometry"]["coordinates"]],
    }


async def travel_times(origin, places, mode="driving"):
    """How far and how long each place is by road from one origin, in a single request.

    Straight-line distance is what a map shows and not what a journey costs: of two mosques three
    kilometres away, one is a seven minute drive and the other is round a river. OSRM answers a
    whole column of that at once, which is one request rather than one per place.
    """
    if not places:
        return
    coordinates = ";".join([f"{origin[1]},{origin[0]}"]
                           + [f"{p['lon']},{p['lat']}" for p in places])
    async with httpx.AsyncClient(timeout=ROUTE_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        try:
            response = await client.get(f"{OSRM}/table/v1/{ROUTED}/{coordinates}",
                                        params={"sources": "0", "annotations": "duration,distance"})
            response.raise_for_status()
            answer = response.json()
        except (httpx.HTTPError, ValueError) as e:
            log.info("no travel times (%s)", e)
            return
    if answer.get("code") != "Ok":
        return
    seconds = (answer.get("durations") or [[]])[0][1:]
    metres = (answer.get("distances") or [[]])[0][1:]
    for place, duration, distance in zip(places, seconds, metres):
        if duration is None or distance is None:
            continue
        place["road_km"] = round(distance / 1000, 1)
        place["road_minutes"] = round(duration / 60)
        place["road_mode"] = ROUTED


def _thin(line, wanted):
    """`wanted` points spread evenly along a line, including both ends."""
    if len(line) <= wanted:
        return list(line)
    step = (len(line) - 1) / (wanted - 1)
    return [line[round(i * step)] for i in range(wanted)]


def _remember(query, elements):
    if len(_answers) >= CACHE_LIMIT:
        for old in sorted(_answers, key=lambda k: _answers[k][0])[:CACHE_LIMIT // 4]:
            _answers.pop(old, None)
    _answers[query] = (time.monotonic(), elements)


async def _overpass(query, deadline=None):
    """Asks each mirror in turn, within whatever time is left.

    Once each, and no more: every extra attempt is another half minute a person spends looking at
    a spinner, and two refusals in a row mean the service is busy rather than unlucky.
    """
    remembered = _answers.get(query)
    if remembered and time.monotonic() - remembered[0] < CACHE_SECONDS:
        return remembered[1]

    trouble = None
    for server in OVERPASS_SERVERS:
        left = OVERPASS_TIMEOUT if deadline is None else deadline - time.monotonic()
        if left < 3:
            break
        try:
            async with httpx.AsyncClient(timeout=min(OVERPASS_TIMEOUT, left),
                                         headers={"User-Agent": USER_AGENT}) as client:
                response = await client.post(server, data={"data": query})
                response.raise_for_status()
                elements = response.json().get("elements", [])
            _remember(query, elements)
            return elements
        except (httpx.HTTPError, ValueError) as e:
            trouble = e.__class__.__name__ if not str(e) else str(e)
            log.info("overpass %s did not answer (%s)", server, trouble)
    if remembered:
        # Every mirror is refusing and this was asked before. A building does not move in an hour.
        log.info("overpass unreachable; answering from what was kept")
        return remembered[1]
    raise MapError(f"The place search did not answer ({trouble or 'out of time'}). These are busy "
                   "free services; trying again in a minute usually works.")


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


def _filters(tags):
    """The tags as an Overpass filter: every one of them has to match."""
    return "".join(f'["{key}"="{value}"]' for key, value in tags.items())


async def nearby(tag, lat, lon, radius_m=None, limit=MAX_RESULTS, words=""):
    """Things of a kind near a point.

    Overpass is the right tool and a busy one. When every mirror is refusing, Nominatim can be asked
    the same question in words: the answers are the same OpenStreetMap data, found by name rather
    than by tag, so there are fewer of them and they are less complete. Fewer real places beats an
    error page, as long as the answer admits which way round it was.
    """
    where = _filters(tag)
    widths = [radius_m] if radius_m else list(RADII)
    deadline = time.monotonic() + SEARCH_BUDGET
    elements, radius_m = [], widths[-1]
    try:
        for width in widths:
            query = (f'[out:json][timeout:20];(node{where}(around:{int(width)},{lat},{lon});'
                     f'way{where}(around:{int(width)},{lat},{lon}););out center {limit * 4};')
            elements = await _overpass(query, deadline)
            if elements:
                radius_m = width
                break
    except MapError:
        if not words:
            raise
        log.info("overpass unavailable; falling back to a name search for %r", words)
        found = await search(words, near=(lat, lon), limit=limit)
        for place in found:
            place["metres_away"] = round(_metres(lat, lon, place["lat"], place["lon"]))
            place["by_name"] = True
        # The same distance the real search would have covered, not four times it: a restaurant
        # forty-five kilometres away is not an answer to "near me", whichever service found it
        near_enough = [p for p in sorted(found, key=lambda p: p["metres_away"])
                       if p["metres_away"] <= radius_m][:limit]
        if not near_enough:
            # The search did not happen. Saying "there is nothing there" would be inventing a
            # fact out of a failed request, and the two are not the same answer at all.
            raise
        return near_enough
    places = [p for p in map(_from_overpass, elements) if p]
    places.sort(key=lambda p: (p["name"] == "(unnamed)", _metres(lat, lon, p["lat"], p["lon"])))
    for place in places:
        place["metres_away"] = round(_metres(lat, lon, place["lat"], place["lon"]))
        place["searched_within_m"] = radius_m
    return places[:limit]


# Words that are in the sentence but not in the name of anything: searching for them finds nothing
FILLER = re.compile(
    r"^(find|show|get|give|tell|me|my|a|an|the|any|some|all|only|please|near|nearby|nearest|"
    r"closest|around|here|there|to|of|in|on|at|is|are|was|were|and|or|but|for|with|where|what|"
    r"which|can|you|i|we|us|place|places|spot|spots|location|locations)$", re.I)


def meaningful(words):
    """The part of a question that could be part of a name."""
    kept = [w for w in re.findall(r"[\w\u00c0-\uffff'-]+", words or "") if not FILLER.match(w)]
    return " ".join(kept[:4])


async def named_nearby(words, lat, lon, radius_m=8000, limit=MAX_RESULTS):
    """Anything near a point whose name contains these words.

    For a kind of place this has no tag for, asking Nominatim its name in the whole world and
    nudging it towards a box gives answers from the wrong continent. Overpass can be asked the
    honest question instead - what near this point is called something like this - which is what
    a person means by "find X around me".
    """
    terms = meaningful(words)
    if not terms:
        return []
    pattern = "|".join(re.escape(part) for part in terms.split())
    query = (f'[out:json][timeout:40];'
             f'(node["name"~"{pattern}",i](around:{int(radius_m)},{lat},{lon});'
             f'way["name"~"{pattern}",i](around:{int(radius_m)},{lat},{lon}););'
             f'out center {limit * 4};')
    try:
        elements = await _overpass(query)
    except MapError:
        return []
    places = [p for p in map(_from_overpass, elements) if p]
    for place in places:
        place["metres_away"] = round(_metres(lat, lon, place["lat"], place["lon"]))
        place["by_name"] = True
    places.sort(key=lambda p: p["metres_away"])
    return places[:limit]


async def along(tag, line, radius_m=1500, limit=MAX_RESULTS):
    """Things of a kind strung along a route, in the order they are passed."""
    where = _filters(tag)
    samples = _thin(line, ALONG_SAMPLES)
    around = "".join(
        f'node{where}(around:{int(radius_m)},{lat},{lon});'
        f'way{where}(around:{int(radius_m)},{lat},{lon});'
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
        modes = {"driving": "driving", "cycling": "bicycling", "foot": "walking",
                 "transit": "transit"}
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
