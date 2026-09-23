"""Which map answers the question - and the rule that one of them answers all of it.

Two providers, the same shapes: OpenStreetMap through Nominatim, Overpass and OSRM, or Google
through Places, Geocoding and Routes. The rest of the gateway asks this module which one is in
charge and then treats it as the only one there is.

Nothing mixes the two, and that is not a matter of taste. Google's terms say a place found through
their APIs may not be drawn on a non-Google map, so a Google result on an OpenStreetMap tile is a
breach rather than a compromise. The consequence runs the other way as well: when Google is the
provider and its map will not load in the browser, the answer is the list on its own. A map is not
worth breaking the terms of the thing that answered.

With no key set, everything behaves exactly as it did before this module existed.
"""
from app import settings
from app.tools import google_maps, maps


def name():
    """Which provider is in charge: "google" when a key is set and allowed, otherwise "osm"."""
    wanted = (settings.get("maps.provider") or "auto").strip().lower()
    if wanted == "osm":
        return "osm"
    if wanted == "google":
        return "google" if google_maps.is_available() else "osm"
    return "google" if google_maps.is_available() else "osm"


def active():
    """The module that answers questions about places, routes and what is nearby."""
    return google_maps if name() == "google" else maps


def tiles():
    """What the browser may draw the answer on. Never the other one's."""
    return "google" if name() == "google" else "osm"
