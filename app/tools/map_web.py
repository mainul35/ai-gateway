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
MAX_PAGES = 3
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
        if name and len(name) < 120:
            places.append({"name": name, "address": address[:200]})
    return places


async def locate(candidates, centre, maps, limit=MAX_CANDIDATES):
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
            if hits:
                place = hits[0]
                place["name"] = candidate["name"] or place["name"]
                place["from_the_web"] = True
                place["metres_away"] = round(maps.distance(lat, lon, place["lat"], place["lon"]))
                found.append(place)
                break
    return found
