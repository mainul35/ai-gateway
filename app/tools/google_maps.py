"""The same map questions, answered by Google instead of OpenStreetMap.

This exists because OpenStreetMap does not contain everything. Shinkoiwa Masjid is a real building
that is simply not in it, and the web-scraping pipeline that finds such places is a workaround for
a gap that Google does not have. Google also knows train timetables, which OSRM does not, and in
Tokyo the train is the answer.

It is not a free swap. It needs a key on a billing account, it cannot be self-hosted, and its terms
forbid one thing in particular that shapes this whole file:

    "Customer must not use Google Maps Content from the Places API in conjunction with a non-Google
    map."  - Maps Platform Service Terms 5.3, and the same words again for Directions (1.3),
    Distance Matrix (2.3) and Geocoding (3.3).

So results from here may only ever be drawn on a Google map. Nothing in the gateway may take a
place found by this module and put it on a Leaflet tile, and when the Google map cannot be drawn
the answer is a list with no map at all rather than a map that breaks the terms. Coordinates from
here may also be kept for at most thirty days (5.4), which the gateway's one hour cache is well
inside.

Everything returns the same shapes as app.tools.maps, so the rest of the gateway does not know or
care which of the two answered.
"""
import logging

import httpx

from app import settings
from app.tools.maps import (MAX_RESULTS, MapError, SEARCH_RADIUS, bounds, distance,  # noqa: F401
                            google_link, meaningful)

log = logging.getLogger("tools.google_maps")

PLACES = "https://places.googleapis.com/v1"
GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
ROUTES = "https://routes.googleapis.com/directions/v2"
TIMEOUT = 25

# Our OpenStreetMap tags, and the Google place type that means the same thing. Anything not here
# is searched for by its words instead, which Google is good at.
TYPES = {
    ("amenity", "place_of_worship", "muslim"): "mosque",
    ("amenity", "place_of_worship", "christian"): "church",
    ("amenity", "place_of_worship", "hindu"): "hindu_temple",
    ("amenity", "place_of_worship", "buddhist"): "buddhist_temple",
    ("amenity", "place_of_worship", "shinto"): "place_of_worship",
    ("amenity", "place_of_worship", "jewish"): "synagogue",
    ("amenity", "place_of_worship", "sikh"): "place_of_worship",
    ("amenity", "place_of_worship", None): "place_of_worship",
    ("amenity", "fuel", None): "gas_station",
    ("amenity", "restaurant", None): "restaurant",
    ("amenity", "cafe", None): "cafe",
    ("amenity", "fast_food", None): "fast_food_restaurant",
    ("amenity", "hospital", None): "hospital",
    ("amenity", "clinic", None): "doctor",
    ("amenity", "pharmacy", None): "pharmacy",
    ("amenity", "atm", None): "atm",
    ("amenity", "bank", None): "bank",
    ("amenity", "toilets", None): "public_bathroom",
    ("amenity", "parking", None): "parking",
    ("amenity", "charging_station", None): "electric_vehicle_charging_station",
    ("amenity", "school", None): "school",
    ("amenity", "university", None): "university",
    ("amenity", "bus_station", None): "bus_station",
    ("shop", "supermarket", None): "supermarket",
    ("shop", "convenience", None): "convenience_store",
    ("tourism", "hotel", None): "hotel",
    ("tourism", "museum", None): "museum",
    ("tourism", "attraction", None): "tourist_attraction",
    ("tourism", "viewpoint", None): "tourist_attraction",
    ("leisure", "park", None): "park",
    ("railway", "station", None): "train_station",
    ("aeroway", "aerodrome", None): "airport",
}

TRAVEL = {"driving": "DRIVE", "foot": "WALK", "cycling": "BICYCLE", "transit": "TRANSIT"}
FIELDS = ("places.id,places.displayName,places.formattedAddress,places.location,"
          "places.primaryType,places.googleMapsUri")


def is_available():
    return bool(settings.google_maps_key())


def _key():
    key = settings.google_maps_key()
    if not key:
        raise MapError("Google maps are switched on but no key is set.")
    return key


def type_for(tag):
    """The Google place type for one of our OpenStreetMap tag sets, or None."""
    if not tag:
        return None
    key = next((k for k in ("amenity", "shop", "tourism", "leisure", "railway", "aeroway")
                if k in tag), None)
    if not key:
        return None
    return TYPES.get((key, tag[key], tag.get("religion"))) or TYPES.get((key, tag[key], None))


def _place(row):
    location = row.get("location") or {}
    return {
        "name": (row.get("displayName") or {}).get("text") or "(unnamed)",
        "address": row.get("formattedAddress") or "",
        "lat": location.get("latitude"), "lon": location.get("longitude"),
        "kind": row.get("primaryType") or "",
        "osm": None,                      # not an OpenStreetMap object; the link below is Google's
        "google": row.get("googleMapsUri"),
        "place_id": row.get("id"),
        "from_google": True,
    }


async def _post(url, body, field_mask=None):
    headers = {"X-Goog-Api-Key": _key(), "Content-Type": "application/json"}
    if field_mask:
        headers["X-Goog-FieldMask"] = field_mask
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.post(url, json=body, headers=headers)
            if response.status_code == 403:
                raise MapError("Google refused the key. Check that the API is enabled for it and "
                               "that the referrer restriction allows this server.")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            raise MapError(f"Google did not answer ({e}).")
        except ValueError as e:
            raise MapError(f"Google's answer could not be read ({e}).")


async def search(query, near=None, limit=MAX_RESULTS):
    """Places matching the words, biased towards a point when one is given."""
    if not (query or "").strip():
        raise MapError("There is nothing to search for.")
    body = {"textQuery": query, "maxResultCount": min(limit, 20)}
    if near:
        body["locationBias"] = {"circle": {"center": {"latitude": near[0], "longitude": near[1]},
                                           "radius": float(SEARCH_RADIUS)}}
    answer = await _post(f"{PLACES}/places:searchText", body, FIELDS)
    return [p for p in map(_place, answer.get("places", [])) if p["lat"] is not None]


async def find(words, near=None, limit=MAX_RESULTS):
    """Google's text search already copes with a name written loosely, so this is search."""
    return await search(words, near=near, limit=limit)


async def reverse(lat, lon):
    """What is at a coordinate."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.get(GEOCODE, params={"latlng": f"{lat},{lon}",
                                                         "key": _key(), "language": "en"})
            response.raise_for_status()
            answer = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MapError(f"That coordinate could not be looked up ({e}).")
    results = answer.get("results") or []
    if not results:
        raise MapError(f"Nothing is mapped at {lat:.5f}, {lon:.5f}.")
    first = results[0]
    return {"name": first.get("formatted_address", "").split(",")[0],
            "address": first.get("formatted_address", ""), "lat": lat, "lon": lon,
            "kind": ", ".join(first.get("types", [])[:2]), "osm": None,
            "place_id": first.get("place_id"), "from_google": True}


async def nearby(tag, lat, lon, radius_m=None, limit=MAX_RESULTS, words=""):
    """Things of a kind near a point, nearest first."""
    kind = type_for(tag)
    if not kind:
        return await named_nearby(words or "", lat, lon, radius_m or SEARCH_RADIUS, limit)
    body = {
        "includedTypes": [kind],
        "maxResultCount": min(limit, 20),
        "rankPreference": "DISTANCE",
        "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lon},
                                           "radius": float(radius_m or SEARCH_RADIUS)}},
    }
    answer = await _post(f"{PLACES}/places:searchNearby", body, FIELDS)
    places = [p for p in map(_place, answer.get("places", [])) if p["lat"] is not None]
    for place in places:
        place["metres_away"] = round(distance(lat, lon, place["lat"], place["lon"]))
        place["searched_within_m"] = radius_m or SEARCH_RADIUS
    return places[:limit]


async def named_nearby(words, lat, lon, radius_m=SEARCH_RADIUS, limit=MAX_RESULTS):
    """Anything near a point that matches these words."""
    terms = meaningful(words) or words
    if not terms.strip():
        return []
    places = await search(terms, near=(lat, lon), limit=limit)
    for place in places:
        place["metres_away"] = round(distance(lat, lon, place["lat"], place["lon"]))
    places.sort(key=lambda p: p["metres_away"])
    return [p for p in places if p["metres_away"] <= radius_m][:limit]


async def along(tag, line, radius_m=1500, limit=MAX_RESULTS):
    """Things of a kind strung along a route, in the order they are passed."""
    kind = type_for(tag)
    if not kind or not line:
        return []
    step = max(1, len(line) // 6)
    seen, found = set(), []
    for index, (lat, lon) in enumerate(line[::step]):
        try:
            near = await nearby(tag, lat, lon, radius_m=radius_m, limit=4)
        except MapError:
            continue
        for place in near:
            if place.get("place_id") in seen:
                continue
            seen.add(place.get("place_id"))
            place["along"] = index / max(len(line[::step]) - 1, 1)
            found.append(place)
    found.sort(key=lambda p: (p["along"], p["metres_away"]))
    return found[:limit]


async def route(points, mode="driving"):
    """A route between two points, by road, on foot, by bicycle or by public transport."""
    if len(points) < 2:
        raise MapError("A route needs somewhere to start and somewhere to finish.")
    travel = TRAVEL.get(mode, "DRIVE")
    body = {
        "origin": {"location": {"latLng": {"latitude": points[0][0], "longitude": points[0][1]}}},
        "destination": {"location": {"latLng": {"latitude": points[-1][0],
                                                "longitude": points[-1][1]}}},
        "travelMode": travel,
        "polylineQuality": "OVERVIEW",
    }
    if travel == "DRIVE":
        body["routingPreference"] = "TRAFFIC_AWARE"
    answer = await _post(f"{ROUTES}:computeRoutes", body,
                         "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline")
    routes = answer.get("routes") or []
    if not routes:
        raise MapError("No route was found between those places.")
    best = routes[0]
    seconds = int(str(best.get("duration", "0s")).rstrip("s") or 0)
    return {
        "mode": mode,                       # Google really did route the way that was asked for
        "asked_for": mode,
        "note": None,
        "distance_km": round(best.get("distanceMeters", 0) / 1000, 1),
        "minutes": round(seconds / 60),
        "line": [],                         # the browser draws Google's own route on Google's map
        "polyline": (best.get("polyline") or {}).get("encodedPolyline"),
    }


async def travel_times(origin, places, mode="driving"):
    """How far and how long each place is from one origin, in a single request."""
    if not places:
        return
    travel = TRAVEL.get(mode, "DRIVE")
    body = {
        "origins": [{"waypoint": {"location": {"latLng": {"latitude": origin[0],
                                                          "longitude": origin[1]}}}}],
        "destinations": [{"waypoint": {"location": {"latLng": {"latitude": p["lat"],
                                                               "longitude": p["lon"]}}}}
                         for p in places[:25]],
        "travelMode": travel,
    }
    try:
        rows = await _post(f"{ROUTES}:computeRouteMatrix", body,
                           "originIndex,destinationIndex,duration,distanceMeters,condition")
    except MapError as e:
        log.info("no travel times from Google (%s)", e)
        return
    for row in rows if isinstance(rows, list) else []:
        index = row.get("destinationIndex")
        if index is None or index >= len(places) or row.get("condition") != "ROUTE_EXISTS":
            continue
        seconds = int(str(row.get("duration", "0s")).rstrip("s") or 0)
        places[index]["road_km"] = round(row.get("distanceMeters", 0) / 1000, 1)
        places[index]["road_minutes"] = round(seconds / 60)
        places[index]["road_mode"] = mode
