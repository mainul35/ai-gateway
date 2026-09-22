"""Working out what a map question is actually asking for.

The message is read once by the small model into four fields - what kind of answer is wanted, the
thing being looked for, and the places involved - and keywords answer when the model will not. The
places are then looked up properly by Nominatim; nothing here guesses at a coordinate, because a
model inventing latitudes that look plausible is the one failure that would be hard to notice.
"""
import json
import re

PROMPT = """You read a question about places and answer with one line of JSON and nothing else.

Fields:
  "intent": one of
      "find"  - find a place, or places, by name or description
      "here"  - say what is at a coordinate the user gave
      "route" - work out the way from one place to another
      "along" - find places of some kind on the way between two places
      "near"  - find places of some kind close to one place
  "what":  the kind of place being looked for, in plain words, or "" if not applicable
  "from":  where the journey starts, in plain words, or ""
  "to":    where the journey ends, or the single place being asked about, or ""

Examples:
"where is Dhaka University" -> {"intent":"find","what":"Dhaka University","from":"","to":""}
"route from Gulshan to Hazrat Shahjalal airport" -> {"intent":"route","what":"","from":"Gulshan","to":"Hazrat Shahjalal airport"}
"petrol stations on the way from Dhaka to Sylhet" -> {"intent":"along","what":"petrol station","from":"Dhaka","to":"Sylhet"}
"restaurants near Banani" -> {"intent":"near","what":"restaurant","from":"","to":"Banani"}
"what is at 23.8103, 90.4125" -> {"intent":"here","what":"","from":"","to":""}
"how far is Chittagong from Dhaka by road" -> {"intent":"route","what":"","from":"Dhaka","to":"Chittagong"}

Answer with the JSON object only."""

INTENTS = ("find", "here", "route", "along", "near")
# Words that mean "where I am standing", which no amount of geocoding can resolve: only the
# browser knows, and only if the person allows it
ABOUT_ME = re.compile(
    r"\b(near|around|close to|next to|nearest to|by)\s+(me|here|us)\b"
    r"|\bnear\s?by\b|\bfrom here\b|\baround here\b|\bmy (current )?(location|position|place)\b"
    r"|\bcurrent location\b|\bwhere am i\b|\bnear my\b|\bclosest\b", re.I)


def wants_my_location(message, plan=None):
    """Whether answering this needs the coordinate of the person asking."""
    if ABOUT_ME.search(message or ""):
        return True
    # "restaurants nearby" with nowhere named is about here, whatever words were used
    if plan and plan.get("intent") in ("near", "along") and not (plan.get("to") or plan.get("from")):
        return True
    return False
FROM_TO = re.compile(r"\bfrom\s+(.+?)\s+(?:to|towards|until)\s+(.+?)[.?!]*$", re.I)
NEAR = re.compile(r"\b(?:near|nearest|around|close to|beside|next to)\s+(.+?)[.?!]*$", re.I)
ALONG = re.compile(r"\b(?:along|on)\s+the\s+(?:way|route)\b", re.I)


def build_request(message, model):
    return {"model": model, "stream": False, "temperature": 0, "max_tokens": 300,
            "reasoning_effort": "none",
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": f"Question: {message}\n\nJSON:"}]}


def read_answer(text):
    """The JSON the model was asked for, or None when it did not produce any."""
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    found = re.search(r"\{.*?\}", cleaned, re.S)
    if not found:
        return None
    try:
        data = json.loads(found.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("intent") not in INTENTS:
        return None
    return {"intent": data["intent"], "what": str(data.get("what") or "").strip(),
            "from": str(data.get("from") or "").strip(), "to": str(data.get("to") or "").strip()}


def by_keywords(message, has_coordinates=False):
    """Used when the model cannot answer. Plain, and never invents a place name."""
    text = (message or "").strip()
    journey = FROM_TO.search(text)
    if journey:
        start, end = journey.group(1).strip(), journey.group(2).strip()
        intent = "along" if ALONG.search(text) else "route"
        return {"intent": intent, "what": text if intent == "along" else "", "from": start, "to": end}
    near = NEAR.search(text)
    if near:
        return {"intent": "near", "what": text, "from": "", "to": near.group(1).strip()}
    if has_coordinates:
        return {"intent": "here", "what": "", "from": "", "to": ""}
    return {"intent": "find", "what": text, "from": "", "to": text}
