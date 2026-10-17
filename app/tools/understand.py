"""What a playground message is asking for, read from everything that came with it.

The small router model is not asked which tool to use. It is told what the person sent - the words,
the last few turns, and each attachment with its kind and name - and asked to describe the request:
what they want back, what is to happen to a picture they attached, whether the answer needs facts
newer than any model has, and what kind of work it is. The tool and the model then follow from that
description in plain code (decide), limited to what the toggles allow.

This replaced three separate one-word questions (app/tools/router.py: which tool, what kind of work,
read or change the picture), each choosing from a fixed list and each patched with keyword rules for
the phrasings it got wrong. Those remain as the fallback for when the small model cannot answer.

The answer is constrained to a JSON schema, so even a 1.7B model on the CPU cannot reply with
something unreadable; it writes one sentence of what the person wants first, which is the
"understanding", and fills in the fields after it.

The old one-word tool question is still asked, at the same time, as a second opinion that only
counts when the reading settles on a plain answer (decide). Measured with scripts/routing_eval on
the live CPU router: 64/64 and 29/30 right tool, against 58/64 and 27/30 for the three questions.
More examples in the prompt made it worse - the 1.7B model starts copying them - so a miss is
fixed by a general rule there, or not at all.
"""
import json
import logging
import re

from app import settings
from app.tools import router
from app.tools.router import BACKDROP, BLUR, CHAT, CLEAN, EDIT, IMAGE, MAP, PHOTOS, SEARCH

log = logging.getLogger("tools.understand")

ANSWER, NEW_PICTURE, CHANGED_PICTURE, REAL_PHOTOS, PLACE = (
    "answer", "new_picture", "changed_picture", "real_photos", "place")
WANTS = (ANSWER, NEW_PICTURE, CHANGED_PICTURE, REAL_PHOTOS, PLACE)
CHANGES = ("none", "improve_quality", "blur_background", "plain_background", "other")
SOURCES = ("knowledge", "attachment", "web_today")
WORK = ("code", "reasoning", "long_read", "picture", "expertise", "small_talk")

SCHEMA = {
    "type": "object",
    "properties": {
        "request": {"type": "string"},
        "wants_back": {"type": "string", "enum": list(WANTS)},
        "picture_change": {"type": "string", "enum": list(CHANGES)},
        "facts_from": {"type": "string", "enum": list(SOURCES)},
        "work": {"type": "string", "enum": list(WORK)},
    },
    "required": ["request", "wants_back", "picture_change", "facts_from", "work"],
}

PROMPT = """You read what a person sent to an assistant and describe what they are asking for. You do
not answer it. Look at everything: their words, what was said just before, and what they attached.
Reply with JSON only.

request: one short sentence saying what the person wants, in your own words.

wants_back: what has to come back for them to be satisfied.
  answer          - words: an explanation, a fact, advice, code, a summary, a translation, a reply.
                    A question about an attached picture or document wants an answer. So does a
                    picture or screenshot put forward with no request, or as evidence of a problem:
                    showing something is not asking for it to be redrawn. A screenshot of code,
                    settings or text, with a change asked for in that text ("set the port to 8080"),
                    wants an answer: the change is to the file, not to the picture.
  new_picture     - a picture that does not exist yet and has to be made: a drawing, a painting, a
                    design, a logo, an icon, a poster, a card, a wallpaper, a sticker, or a scene
                    described in words. Asking HOW to make one, or what makes a good one, is answer.
  changed_picture - the attached picture back, different: something added, removed or changed in it,
                    a new style or look, better quality, a blurred or replaced background - or a new
                    picture made from it with the same person or thing in it ("use my photo to make
                    me an astronaut"). Also a change to a picture made earlier in the conversation.
  real_photos     - to be shown photographs or videos of a particular real thing that already exists
                    and has been photographed: a product, a building, a place, a car, a person, the
                    things mentioned earlier. "What does the new X look like?" is real_photos.
                    "A nice landscape" names nothing real: new_picture.
  place           - a position on a map: where somewhere is, how to get from one place to another,
                    how far, or what is nearby or along the way. A fact about a place (its population,
                    language, history, what it is famous for) is answer.

picture_change: only for changed_picture with a picture attached, otherwise "none".
  improve_quality  - the same picture, cleaner: noise or grain removed, sharper, haze cleared.
  blur_background  - the background blurred so the subject stands out.
  plain_background - the background behind a person made one plain colour (passport, visa, ID).
  other            - any other change, including removing, adding or replacing things in the
                     background.

facts_from: where the facts for a good answer come from.
  knowledge  - what any well-read person or textbook knows, or can work out: explanations, advice,
               health, cooking, code and errors, maths, languages, capitals, history, science,
               writing. Almost every question is this.
  attachment - what the person attached: the document, log or picture is read.
  web_today  - what changes from day to day or year to year, and so must be looked up now: news,
               this year's winners, today's prices or rates, weather and forecasts, scores, the
               latest version or release, whether something is out yet - also for things named in
               an attachment. Ask: would the answer have been different a year ago?

work: what kind of thinking the answer needs.
  code      - programming, errors, logs, configuration, commands, servers, containers, databases.
              Anything about software is code, never expertise.
  reasoning - careful working out: maths, logic, planning, comparing options.
  long_read - reading through a long attached document or text.
  picture   - looking at an attached picture.
  expertise - a real question that needs knowledge to answer well: hardware, health, money, law,
              science, travel, how something works, advice, recommendations, writing that matters.
  small_talk - greetings, thanks, jokes, a short poem, a quick rewrite, a one-line fact, and any
              request for a picture, a photo or a map.

Examples:
Message: "what is the latest version of Node.js?"
{"request": "the current Node.js version number", "wants_back": "answer", "picture_change": "none", "facts_from": "web_today", "work": "small_talk"}
Message: "why does my nginx return 502?"
{"request": "help finding the cause of an nginx 502 error", "wants_back": "answer", "picture_change": "none", "facts_from": "knowledge", "work": "code"}
Message: "Design me a logo for Lumina AI"
{"request": "a logo made for Lumina AI", "wants_back": "new_picture", "picture_change": "none", "facts_from": "knowledge", "work": "small_talk"}
Message: "can I see the Fairphone 5?"
{"request": "photos of the real Fairphone 5", "wants_back": "real_photos", "picture_change": "none", "facts_from": "knowledge", "work": "small_talk"}
Message: "route from Gulshan to the airport"
{"request": "a route from Gulshan to the airport", "wants_back": "place", "picture_change": "none", "facts_from": "knowledge", "work": "small_talk"}
Attached: 1 picture (IMG_1022.jpg)
Message: "This is me. Make a picture of me riding a horse"
{"request": "a new picture of the person in the photo riding a horse", "wants_back": "changed_picture", "picture_change": "other", "facts_from": "attachment", "work": "picture"}
Attached: 1 picture (Screenshot 2026-09-02.png)
Message: "notice that the font looks broken"
{"request": "an explanation of the broken font shown in the screenshot", "wants_back": "answer", "picture_change": "none", "facts_from": "attachment", "work": "picture"}
Attached: 1 picture (DSC0042.jpg)
Message: "too grainy, clean it up"
{"request": "the same photo with the grain removed", "wants_back": "changed_picture", "picture_change": "improve_quality", "facts_from": "attachment", "work": "picture"}
Message: "what should I eat when I have a cold?"
{"request": "advice on food during a cold", "wants_back": "answer", "picture_change": "none", "facts_from": "knowledge", "work": "expertise"}
Message: "will 64GB of RAM let me run a 70B model with my 4090?"
{"request": "whether 64GB RAM and a 4090 can run a 70B model", "wants_back": "answer", "picture_change": "none", "facts_from": "knowledge", "work": "expertise"}
Message: "who won the last F1 race?"
{"request": "the winner of the most recent F1 race", "wants_back": "answer", "picture_change": "none", "facts_from": "web_today", "work": "small_talk"}
Message: "will it rain in Dhaka this weekend"
{"request": "the weather forecast for Dhaka this weekend", "wants_back": "answer", "picture_change": "none", "facts_from": "web_today", "work": "small_talk"}
Message: "thanks, that worked!"
{"request": "thanks for the help", "wants_back": "answer", "picture_change": "none", "facts_from": "knowledge", "work": "small_talk"}
Attached: 1 picture (application-yml.png)
Message: "set the timeout to 30 seconds here"
{"request": "how to change the timeout to 30 seconds in the configuration shown", "wants_back": "answer", "picture_change": "none", "facts_from": "attachment", "work": "code"}
Attached: 1 document (meeting-notes.pdf)
Message: "make a poster for this"
{"request": "a poster designed from the attached document", "wants_back": "new_picture", "picture_change": "none", "facts_from": "knowledge", "work": "small_talk"}"""


def describe_attachments(attachments):
    """What came with the message, as the line the prompt shows: "1 picture (a.jpg), 1 document (b.pdf)"."""
    groups = {}
    for item in attachments or ():
        kind = "document" if item.get("kind") == "document" else "picture"
        groups.setdefault(kind, []).append(str(item.get("name") or "")[:60])
    parts = []
    for kind in ("picture", "document"):
        names = groups.get(kind)
        if names:
            label = kind if len(names) == 1 else kind + "s"
            shown = ", ".join(n for n in names[:4] if n)
            parts.append(f"{len(names)} {label}" + (f" ({shown})" if shown else ""))
    return ", ".join(parts)


def build_request(message, attachments=(), history=()):
    recent = "\n".join(f"{m['role']}: {(m.get('content') or '')[:300]}" for m in list(history)[-4:])
    lines = []
    if recent:
        lines.append(f"Earlier in the conversation:\n{recent}\n")
    attached = describe_attachments(attachments)
    if attached:
        lines.append(f"Attached: {attached}")
    lines.append(f'Message: "{message or ""}"')
    return {
        "model": settings.router_model(),
        "stream": False,
        "temperature": 0,
        "max_tokens": 300,
        "reasoning_effort": "none",
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "reading", "schema": SCHEMA}},
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": "\n".join(lines)}],
    }


def read_answer(text):
    """The reading out of the model's reply, with every field checked; None when it is unusable."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    match = re.search(r"\{.*\}", text, re.S)
    try:
        reading = json.loads(match.group(0)) if match else None
    except ValueError:
        reading = None
    if not isinstance(reading, dict) or reading.get("wants_back") not in WANTS:
        return None
    return {
        "request": str(reading.get("request") or "")[:300],
        "wants_back": reading["wants_back"],
        "picture_change": reading.get("picture_change") if reading.get("picture_change") in CHANGES else "none",
        "facts_from": reading.get("facts_from") if reading.get("facts_from") in SOURCES else "knowledge",
        "work": reading.get("work") if reading.get("work") in WORK else "expertise",
    }


# Words that only ever mean "look it up now". The small model knows the rule and still files "who won
# the final this year" and "is the iPhone 17 out yet" under what it already knows; these words are a
# better witness than that, and nothing else in a reading is second-guessed by keywords.
NOW_WORDS = re.compile(
    r"\b(latest|newest|today'?s?|tonight|tomorrow'?s?|this (week|weekend|month|year|season)|right now|"
    r"currently|current (price|rate|version|score|status)s?|out yet|released yet|news|forecast|"
    r"weather|exchange rate|stock price|live score)\b", re.I)

PHOTO_TOOLS = {"improve_quality": CLEAN, "blur_background": BLUR, "plain_background": BACKDROP}
CATEGORY = {"code": "code", "reasoning": "reasoning", "long_read": "long", "expertise": "general",
            "small_talk": "light"}


def decide(reading, allowed, has_images, message="", second_opinion=None):
    """The tool for a reading, from those the toggles allow. CHAT when the wanted one is switched off.

    One thing is checked against the words rather than taken on trust: the three photograph tools
    each do one fixed thing whatever the sentence says, so one is only used when the words ask for
    what it does. Read as "blur the background", "remove the person in the background" would have
    come back blurred with the person still in it; the editor follows the instruction instead.
    """
    wants = reading["wants_back"]

    def first(*actions):
        return next((a for a in actions if a in allowed), CHAT)

    # "Blur the background" came back as wanting an answer with a background blur as the change: the
    # two fields disagree, and the words settle it - they ask for exactly what that tool does
    tool = PHOTO_TOOLS.get(reading["picture_change"])
    if wants == ANSWER and has_images and tool and router.NAMED_BY[tool].search(message or ""):
        wants = CHANGED_PICTURE
    if wants == PLACE:
        return first(MAP)
    if wants == REAL_PHOTOS:
        return first(PHOTOS)
    if wants in (NEW_PICTURE, CHANGED_PICTURE):
        if has_images:
            # Any picture asked for with a picture attached is made from that picture
            tool = PHOTO_TOOLS.get(reading["picture_change"], EDIT)
            if not router.NAMED_BY.get(tool, re.compile("")).search(message or ""):
                tool = EDIT
            return first(tool, EDIT)
        return first(IMAGE)
    if reading["facts_from"] == "web_today" or NOW_WORDS.search(message or ""):
        return first(SEARCH)
    # A plain answer is the reading's catch-all. When the one-word tool question, asked alongside,
    # saw a search or a fix for the attached photo, that is taken: a 1.7B model reads "when is the
    # next launch" as something it knows, and "it is hazy, can you fix it" as a question.
    if second_opinion in (SEARCH, CLEAN, BLUR, BACKDROP) and second_opinion in allowed:
        return second_opinion
    return CHAT


def category(reading, has_images):
    """The kind of work, in the chooser's words (app/tools/chooser.py)."""
    if has_images:
        return "vision"
    return CATEGORY.get(reading["work"], "general")
