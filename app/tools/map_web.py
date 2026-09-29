"""Finding places the map has never heard of.

OpenStreetMap only contains what somebody took the trouble to add. Shinkoiwa Masjid is a real
building that real people pray in, and it is not in OpenStreetMap at all: nothing named like it
within five kilometres, nothing by that name anywhere on earth. No amount of querying the map finds
a thing the map does not contain.

The web knows about it. So when the map comes back thin, the web is searched for the same kind of
place near the same area, the pages are read, and a small model pulls out the names and street
addresses it finds. Those addresses are then given to the geocoder, and only the ones that resolve
to a real coordinate are kept.

That last step is the one that matters. A model asked for the address of a mosque will produce a
plausible one whether or not it read it anywhere, and a plausible address geocodes to a plausible
coordinate, and then somebody drives there. So nothing here is trusted on the model's word: a place
survives only if the geocoder agrees it exists, and it is marked as having come from the web rather
than from the map, because the two are not equally reliable and the reader should be told which is
which.
"""
import json
import logging
import re

log = logging.getLogger("tools.map_web")

# Fewer than this from the map is thin enough to be worth asking the web as well
THIN = 3
MAX_PAGES = 4
MAX_CANDIDATES = 6
PAGE_CHARACTERS = 6000

PROMPT = """You are reading web pages to find real places of a particular kind in a particular area.

List every place of the asked-for kind that the pages actually name, with its street address as
written on the page. Answer with a JSON array and nothing else, like:

[{"name": "Some Masjid", "address": "1-2-3 Somewhere, Katsushika, Tokyo"}]

Rules:
- Only places the pages actually mention. Do not add places you happen to know.
- Only addresses the pages actually give. If a place is named but no address is written, use "".
- At most 6 places. If the pages name none of the asked-for kind, answer with [].
"""


def build_query(kind, area):
    """What to search for: the kind of place, where, and the words people use for a list of them."""
    kind = (kind or "place").strip()
    area = (area or "").strip()
    return f"{kind} near {area}".strip() if area else kind


def build_request(model, kind, area, pages):
    """The extraction call: a lot of page text in, a small JSON array out."""
    body = []
    for page in pages[:MAX_PAGES]:
        text = (page.get("text") or "")[:PAGE_CHARACTERS]
        if text.strip():
            body.append(f"--- from {page.get('url', '')}\n{text}")
    return {
        "model": model, "stream": False, "temperature": 0, "max_tokens": 700,
        "reasoning_effort": "none",
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": f"Kind of place: {kind}\nArea: {area}\n\n"
                                        + "\n\n".join(body) + "\n\nJSON:"},
        ],
    }


# A string that opens with a house number and carries street words is an address, not a name
ADDRESS_SHAPED = re.compile(r"^\s*\d+[-–\d]*\s*(chome|ban|banchi|block|street|st\.?|road|"
                            r"rd\.?|avenue|ave\.?|lane|dori)?", re.I)


def _squashed(text):
    """Lowercase, with everything that is not a letter or digit removed, for comparing addresses."""
    return re.sub(r"[^0-9a-z\u3040-\u9fff]", "", (text or "").lower())


def verify(places, pages):
    """Keeps only the places whose address was actually written on one of the pages.

    This is the one check that matters. A model with nothing to read will still answer, and what it
    answers looks exactly like an address: "1-2-3 Shinkoiwa, Katsushika, Tokyo" for a mosque that is
    really at 4-34-8 Matsushima in Edogawa, a different ward. That invention geocodes perfectly well
    - a plausible address in a real neighbourhood resolves to a real coordinate - so the geocoder
    cannot catch it and nothing downstream can either. The page either says it or it does not.
    """
    haystack = _squashed(" ".join((page.get("text") or "") for page in pages))
    if not haystack:
        return []
    kept = []
    for place in places:
        name = _squashed(place.get("name"))
        if len(name) < 4 or name not in haystack:
            log.info("dropping %r: that name is not on any page read", place.get("name"))
            continue
        # The name is grounded, so the place is real. The address is a separate claim: keep it only
        # when the page actually carries it, and otherwise let the name be geocoded on its own.
        # An address nobody wrote is the one thing that must never reach the geocoder, because a
        # plausible address in a real neighbourhood resolves to a real and entirely wrong coordinate.
        address = _squashed(place.get("address"))
        if not (len(address) >= 8 and address in haystack):
            if place.get("address"):
                log.info("keeping %r but dropping its address, which is on no page",
                         place.get("name"))
            place = {**place, "address": ""}
        kept.append(place)
    return kept


def read_answer(text):
    """The array the model was asked for, or an empty list when it produced something else."""
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    found = re.search(r"\[.*\]", cleaned, re.S)
    if not found:
        return []
    try:
        rows = json.loads(found.group(0))
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    places = []
    for row in rows[:MAX_CANDIDATES]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        address = str(row.get("address") or "").strip()
        # "9 Chome-2-13 Akasaka" is an address the model filed under name. A name that is mostly
        # digits and street words is not what anybody calls the place
        letters = len(re.sub(r"[^A-Za-z぀-鿿]", "", name))
        if name and len(name) < 120 and letters >= 4 and not ADDRESS_SHAPED.match(name):
            places.append({"name": name, "address": address[:200]})
    return places


# A place found by searching "near Osaka" that geocodes to Lebanon is not that place. The geocoder
# will happily match a name against somewhere on another continent, and it did: Kobe Mosque landed
# eight and a half thousand kilometres away, a coffee shop in Buffalo, New York. Near means near.
TOO_FAR = 50_000


async def locate(candidates, centre, maps, limit=MAX_CANDIDATES, too_far=TOO_FAR):
    """Turns names and addresses into places, keeping only the ones the geocoder agrees exist.

    Every lookup is a request to a service that asks for one a second, so this is deliberately a
    handful of places and no more.
    """
    lat, lon = centre
    found = []
    for candidate in candidates[:limit]:
        for words in (f"{candidate['name']} {candidate['address']}".strip(),
                      candidate["address"], candidate["name"]):
            if not words.strip():
                continue
            try:
                hits = await maps.search(words, near=(lat, lon), limit=1)
            except maps.MapError as e:
                log.info("could not geocode %r (%s)", words, e)
                continue
            if not hits:
                continue
            place = hits[0]
            away = round(maps.distance(lat, lon, place["lat"], place["lon"]))
            if away > too_far:
                log.info("dropping %r: geocoded %d km from where the search was",
                         candidate["name"], away // 1000)
                continue
            place["name"] = candidate["name"] or place["name"]
            place["from_the_web"] = True
            place["metres_away"] = away
            found.append(place)
            break
    return found
