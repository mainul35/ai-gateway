"""Decides what a playground message asks for, so the toggles mean "allowed" rather than "forced".

With web search, image understanding and image generation all switched on, "What is the latest Java
version?" must be answered from the web, not drawn. A small fast model reads the message and picks one
action; if it is unavailable or answers something unexpected, plain keyword rules decide instead. Those
rules prefer answering over drawing, because a wrong picture wastes far more time than a wrong search.
"""
import logging
import re

from app import settings
from app.tools.chooser import CATEGORIES
from app.tools.media import (LOOK_WORDS, MADE_UP_WORDS, describes_a_picture,
                             points_at_something)

log = logging.getLogger("tools.router")

CHAT, SEARCH, IMAGE, EDIT = "chat", "search", "image", "edit"
PHOTOS = "photos"               # a real photograph of a real thing, found rather than drawn
CLEAN, BLUR = "clean", "blur"   # what is asked of a photograph, as opposed to a drawing
BACKDROP = "backdrop"           # the person kept exactly as they are, on a different plain colour
MAP = "map"                     # somewhere on the earth, rather than something to read or draw

PROMPT = """You route a user's message to one of these actions. Answer with one word and nothing else.

SEARCH - needs current or factual information from the web about the world: news, prices, releases,
         versions, dates, "latest", "today", anything you could not answer reliably from memory.
CHAT   - can be answered or discussed directly: explanations, opinions, maths, writing, and anything
         about the user's own code, configuration, logs, machine or earlier messages, including
         troubleshooting. Questions about a picture the user attached are also CHAT, and so is a
         message that simply puts a picture forward as evidence - "here is what I found", "notice
         this error", "this is what it looks like now", a screenshot with no request at all. That
         is showing you something so you can read it. Attaching a picture is not asking for it to
         be changed; only words asking for a change make it one.
IMAGE  - asks for a picture to be created that nobody has ever photographed: "draw", "generate an
         image of", "make me a picture of". A logo, icon, emblem, badge, poster, banner, cover,
         wallpaper or avatar asked for by someone who wants one is IMAGE too - "design me a logo"
         is a request for a picture, however much it sounds like a brief, and answering it in
         words leaves them with no logo. A message that only describes a picture, with no question
         and no instruction, is also IMAGE. Asking how such a thing is made, or what makes a good
         one, or which tool to use, is CHAT: that is about the craft rather than a request for
         the thing.
PHOTOS - asks to be SHOWN something that really exists: a product, a brand, a building, a place, a
         person, an animal, a machine, a book, a car, or a video of something that happened. The
         test is whether a photograph of it is already out there to be found. If it is, this is
         PHOTOS, because a drawing of a real product is a picture of something that does not
         exist. Asking to see something mentioned earlier in the conversation - "show me the ones
         you suggested", "what does that one look like" - is PHOTOS.
EDIT   - asks for the attached picture to be changed as a picture: "make it night", "remove the car",
         "add a hat", "make it look like a painting".
CLEAN  - asks for the quality of a photograph to be improved, with nothing in it changed: noise or
         grain removed, haze or mist cleared, softness sharpened, or made good enough to crop into
         or enlarge. "Make it sharper", "it looks hazy", "too grainy" are all CLEAN. If the scene
         itself would look different afterwards - the weather, the season, the time of day, the
         style, anything added or taken away - it is EDIT, not CLEAN.
BLUR   - asks for the background of a photograph to be blurred, or for the subject to stand out
         from it: shallower depth of field than the camera gave.
MAP    - asks about somewhere on the earth: finding a place, what is at a coordinate, getting
         from one place to another, how far or how long a journey is, or what of some kind is
         nearby or along the way. Wanting to go somewhere is MAP even when it is phrased as a
         wish rather than a question, and so is anything asking for a route, directions, the way
         there, or what is near the person asking. A question about a place that wants a fact
         rather than a position on the ground - which country a city is in, what it is famous
         for, its population, its history - is CHAT, not MAP.
BACKDROP - asks for the background behind a person to become a plain colour, or for the colour it
         already is to become a different one: passport and visa photographs. "White background",
         "make the background light blue", "I need this on red for my visa form" are all BACKDROP.
         Only the background colour changes; the person is untouched.

Examples:
"What is the latest Java version as of today?" -> SEARCH
"Why is my Spring Boot app slow?" -> CHAT
"Why does my container lose its network after a reboot?" -> CHAT
"What is 17 * 23?" -> CHAT
"draw a fox in the snow" -> IMAGE
"an oil painting of a harbour at dawn, stormy sky" -> IMAGE
"Design me a logo with name Lumina AI" -> IMAGE
"make me an app icon for a weather app" -> IMAGE
"a poster for the school summer fair" -> IMAGE
"how do I design a good logo?" -> CHAT
"what makes a logo memorable?" -> CHAT
"show me the photos of the drinks you are suggesting" -> PHOTOS
"what does a Fairphone 5 look like?" -> PHOTOS
"find me pictures of the Shibuya crossing" -> PHOTOS
"show me a video of a Shinkansen leaving Tokyo station" -> PHOTOS
"can I see the actual product?" -> PHOTOS
"make it night with northern lights" -> EDIT
"remove the noise from this photo" -> CLEAN
"too grainy, clean it up so I can crop in" -> CLEAN
"the tower looks hazy, make it sharper" -> CLEAN
"this came out soft, can you fix it" -> CLEAN
"make it look like winter" -> EDIT
"add fog to this picture" -> EDIT
"blur the background so the bird stands out" -> BLUR
"where is Dhaka University" -> MAP
"route from Gulshan to the airport" -> MAP
"find petrol stations along the way to Chittagong" -> MAP
"what is at 23.8103, 90.4125" -> MAP
"how far is Cox\'s Bazar from Dhaka by road" -> MAP
"I want to go to Futako Tamagawa, show me the easiest route" -> MAP
"take me to the nearest pharmacy" -> MAP
"what restaurants are near me" -> MAP
"how do I get to the station from here" -> MAP
"what is the capital of Japan" -> CHAT
"what is Kyoto famous for" -> CHAT
"can you give this more bokeh?" -> BLUR
"change the background to white for my passport photo" -> BACKDROP
"I need a light blue background on this" -> BACKDROP
"put a plain grey backdrop behind me" -> BACKDROP
"what is in this image?" -> CHAT
"can you read the text in this screenshot?" -> CHAT
"Here is what I found in FontSubstitute" -> CHAT
"notice that the font looks broken" -> CHAT
"this is the error I am getting" -> CHAT
"I found these entries in the registry" -> CHAT
"clicking Try again three times started three answers at once" -> CHAT
"it should wait until the first one is done before starting another" -> CHAT

Allowed answers this time: {allowed}. Answer with one of those words only."""

# What kind of work the message is, which decides which model should do it. Asked of the same small
# model, in its own call, at the same time as the action: the two questions are independent, and
# putting both in one prompt made the action worse, which is the answer that matters most.
CATEGORY_PROMPT = """You read a user's message and say what kind of work it asks for. Answer with one
word and nothing else.

CODE      - writing, reading, fixing, explaining or reviewing code, or anything about a program's
            behaviour: build errors, stack traces, configuration files, shell commands, SQL.
VISION    - it is about a picture the user has attached.
REASONING - it needs careful step-by-step thought before an answer is possible: maths, logic
            puzzles, planning, weighing options, working something out from given facts.
LONG      - it comes with, or is about, a long piece of text to read through: summarise this,
            what does this document say, go through this transcript.
GENERAL   - everything else: ordinary questions, explanations, writing, conversation.

Examples:
"Why does my Spring Boot app fail to start with a BeanCreationException?" -> CODE
"Write a function that merges two sorted lists" -> CODE
"What is in this image?" -> VISION
"If a train leaves at 3pm going 60mph, when does it arrive 200 miles away?" -> REASONING
"Should I use Postgres or MongoDB for this, and why?" -> REASONING
"Summarise the attached meeting notes" -> LONG
"What is the capital of Bangladesh?" -> GENERAL
"Write me a short poem about rain" -> GENERAL

Answer with one of: CODE, VISION, REASONING, LONG, GENERAL."""

CODE_WORDS = re.compile(
    r"\b(code|function|class|method|variable|compile|build|error|exception|stack ?trace|bug|debug|"
    r"api|endpoint|database|query|sql|docker|container|kubernetes|yaml|json|regex|script|shell|"
    r"python|java|javascript|typescript|rust|golang|spring|react|npm|maven|gradle|git)\b", re.I)
REASONING_WORDS = re.compile(
    r"\b(calculate|compute|solve|prove|derive|how many|how much|why (is|does|would)|"
    r"compare|trade-?offs?|should i|which is better|plan|strategy|step by step)\b", re.I)
LONG_WORDS = re.compile(
    r"\b(summari[sz]e|summary|tl;?dr|these notes|this document|this transcript|this article|"
    r"go through|read through)\b", re.I)

IMAGE_WORDS = re.compile(
    r"\b(draw|sketch|paint|render|illustrate|generate|create|make|produce|design)\b[^.?!]{0,40}"
    r"\b(image|picture|photo|photograph|drawing|painting|illustration|logo|icon|poster|wallpaper|"
    r"art|artwork|banner|cover|emblem|badge|avatar|sticker|mockup|thumbnail)\b"
    r"|^\s*(an? |the )?(image|picture|photo|drawing|painting|illustration|logo|poster|wallpaper) of\b", re.I)
EDIT_WORDS = re.compile(
    r"\b(make it|turn it|change|replace|remove|delete|erase|add|put|swap|recolou?r|repaint|crop|zoom|"
    r"brighten|darken|blur)\b", re.I)
CLEAN_WORDS = re.compile(
    r"\b(noise|noisy|grain|grainy|denoise|de-noise|speckl\w*|iso|clean(ing)? up|clean it up|"
    r"sharp\w*|crisp\w*|clarity|haz\w*|mist\w*|foggy|smog|milky|washed out|soft|blurry|"
    r"crop into|zoom into|enlarge|upscale|restore)\b", re.I)
BLUR_WORDS = re.compile(
    r"\b(bokeh|background blur|blur the background|depth of field|dof|stand out from|"
    r"separate\w* from the background|portrait mode)\b", re.I)
BACKDROP_WORDS = re.compile(
    r"\bpassport\b|\bvisa photo\w*|\bid photo\b|"
    # a colour named next to the background, either way round: "white background", "background to blue"
    r"\b(background|backdrop|back ?drop)\b[^.?!]{0,20}\b(colou?r|white|blue|red|grey|gray|green|black|"
    r"beige|cream|pink|navy|#[0-9a-f]{3,6})\b|"
    r"\b(white|off-white|light blue|sky blue|dark blue|navy|red|grey|gray|light grey|light gray|green|"
    r"black|beige|cream|pink|plain)\b[^.?!]{0,12}\b(background|backdrop|back ?drop)\b", re.I)
MAP_WORDS = re.compile(
    r"\b(map|maps|route|directions?|navigate|navigation|how (do i|to) get|"
    r"(want|need|like) to (go|get) to|take me to|way to get|best way to|"
    r"how far|how long.*(drive|walk|cycle|by road)|distance (from|to|between)|"
    r"nearest|nearby|near me|around me|along the (way|route)|on the way|"
    r"where is|where are|located|location of|address of|coordinates?|lat(itude)?\b|lon(gitude)?\b|"
    r"petrol|fuel station|restaurants? near|hotels? near|atm near)\b", re.I)
SEARCH_WORDS = re.compile(
    r"\b(latest|newest|current|currently|today|todays|tonight|now|recent|recently|this (week|month|year)|"
    r"news|price|cost|release[ds]?|version|available|stock|weather|score|who is|who won|when (is|did|will)|"
    r"how much|20[2-9]\d)\b", re.I)


def is_enabled():
    return settings.get_bool("router.enabled", "ROUTER_ENABLED", True) and bool(settings.router_model())


def candidates(tools, has_images):
    """The actions the user's toggles allow for this message."""
    allowed = [CHAT]
    if tools.get("maps") and not has_images:
        allowed.append(MAP)
    # A web search is text only, so it cannot help with a picture the user just attached
    if tools.get("web_search") and not has_images:
        allowed += [SEARCH, PHOTOS]     # finding a photograph is a web search with a different eye
    if tools.get("image_generation"):
        allowed += [EDIT, CLEAN, BLUR, BACKDROP] if has_images else [IMAGE]
    return allowed


# Putting a picture in front of somebody is not asking them to redraw it. "Here is what I found
# in FontSubstitute", under a screenshot of the registry, is evidence: the answer is to read it.
# Taken as an edit, the gateway spends a minute of GPU redrawing the screenshot and hands back a
# picture of a registry editor that never existed, which answers nothing and costs the most of any
# wrong answer this router can give.
SHOWING_ME = re.compile(
    r"^\s*(here\s+(is|are|'s)|here's|this\s+is|these\s+are|that\s+is|it\s+is)\b"
    r"|\b(notice|note)\s+(that|this|the|how|it)\b"
    r"|\b(as you can see|look at (this|the|it)|have a look|see (this|the) (one|picture|image|screenshot))\b"
    r"|\bi\s+(found|got|see|attached|uploaded|am seeing|am getting)\b"
    r"|\bthis\s+(shows|is what|screenshot|picture|image|photo|error)\b"
    r"|\?\s*$", re.I)


def _asks_for_a_change(text):
    """Whether anything in the words asks for the picture itself to be different."""
    return bool(EDIT_WORDS.search(text) or CLEAN_WORDS.search(text)
                or BLUR_WORDS.search(text) or BACKDROP_WORDS.search(text))


def _only_showing(text):
    """The picture is evidence: it is being shown, and nothing asks for it to be changed."""
    text = text or ""
    return bool(SHOWING_ME.search(text)) and not _asks_for_a_change(text)


def _wants_the_real_thing(text):
    """Asking to be shown one particular thing that exists, in words that do not ask for a drawing.

    All three conditions matter. "Show me a nice nature photo" asks to be shown a photo and means
    a made-up one perfectly happily, because it names nothing: it is a kind of picture, not a
    thing. Overruling that would take away the image model for the whole class of request.
    """
    text = text or ""
    return bool(LOOK_WORDS.search(text)) and not MADE_UP_WORDS.search(text) \
        and points_at_something(text)


def by_keywords(message, allowed, has_images):
    """Used when the router model cannot decide. Only ever returns an allowed action."""
    text = (message or "").strip()
    # First, because a plain colour behind a person is asked for in the same words as an edit:
    # "change the background to white" is "change ... " to EDIT_WORDS and nothing to the rest
    if has_images and BACKDROP in allowed and BACKDROP_WORDS.search(text):
        return BACKDROP
    if MAP in allowed and not has_images and MAP_WORDS.search(text):
        return MAP
    if has_images and CLEAN in allowed and CLEAN_WORDS.search(text):
        return CLEAN
    if has_images and BLUR in allowed and BLUR_WORDS.search(text):
        return BLUR
    if has_images and EDIT in allowed and EDIT_WORDS.search(text) and not text.endswith("?"):
        return EDIT
    # Before IMAGE: wanting to see a thing and wanting one drawn are asked for in nearly the same
    # words, and only one of the two produces a picture of something that exists
    if PHOTOS in allowed and _wants_the_real_thing(text):
        return PHOTOS
    if IMAGE in allowed and IMAGE_WORDS.search(text):
        return IMAGE
    if SEARCH in allowed and SEARCH_WORDS.search(text):
        return SEARCH
    return CHAT


# A picture came with the message. The seven-way choice above asks a small model to weigh
# reading against four different ways of changing a picture all at once, and it is the reading that
# loses: "Here is what I found in FontSubstitute", under a screenshot of the registry, came back as
# an edit. Asked on its own, as the one question it really is, the same model gets it right - and
# this is the question worth spending a second call on, because taking evidence for an edit spends
# a minute of GPU redrawing a screenshot and answers nothing.
PICTURE_PROMPT = """A picture is attached to the user's message. Say what they want done with it.
Answer with one word and nothing else.

READ   - the picture is there to be looked at. Evidence, a screenshot of a problem, a photograph of
         something to identify, a document to read, a chart to explain. The message may describe
         what is in it, point at part of it, ask a question about it, or simply put it forward
         with no request at all.
CHANGE - the message asks for the picture itself to come back different: something added, removed
         or altered in it, the background replaced or recoloured, the quality improved, the
         background blurred.

The test is whether words would satisfy them. If what they want back is an answer, it is READ.
Only if what they want back is a new picture is it CHANGE. A picture with no request attached to
it is READ: attaching something is not asking for it to be redrawn.

Examples:
"Here is what I found in FontSubstitute" -> READ
"notice that the font looks broken" -> READ
"this is the error I am getting" -> READ
"what is in this image?" -> READ
"can you read the text in this screenshot?" -> READ
"why does my registry look like this" -> READ
"is this the right setting?" -> READ
"make it night with northern lights" -> CHANGE
"remove the car" -> CHANGE
"white background for my passport photo" -> CHANGE
"too grainy, clean it up" -> CHANGE
"blur the background so the bird stands out" -> CHANGE

Answer with READ or CHANGE."""

READ, CHANGE = "read", "change"


def build_picture_request(message, history=()):
    """The one question that matters when a picture is attached: read it, or change it?"""
    recent = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in list(history)[-2:])
    context = f"Earlier in the conversation:\n{recent}\n\n" if recent else ""
    return {
        "model": settings.router_model(),
        "stream": False,
        "temperature": 0,
        "max_tokens": 200,
        "reasoning_effort": "none",
        "messages": [
            {"role": "system", "content": PICTURE_PROMPT},
            {"role": "user", "content": f"{context}Message: {message}\n\nAnswer:"},
        ],
    }


def read_picture_answer(text):
    """READ or CHANGE out of the model's reply, or None when it named neither."""
    words = re.findall(r"[a-z]+", re.sub(r"<think>.*?</think>", "", (text or ""), flags=re.S).lower())
    for word in words:
        if word in (READ, CHANGE):
            return word
    return None


def settle_picture(action, by, wanted, message):
    """Reading beats changing when the model says the picture was given to be looked at.

    Only ever in that direction. The two wrong answers are not equal: reading a picture that was
    meant to be edited wastes a sentence, and editing a picture that was meant to be read spends a
    minute of GPU and hands back a fabricated screenshot in place of the evidence.
    """
    if wanted is None or action not in (EDIT, CLEAN, BLUR, BACKDROP):
        return action, by
    if wanted == READ and not _asks_for_a_change(message or ""):
        return CHAT, settings.router_model() or "keywords"
    return action, by


def build_category_request(message, history=()):
    """The second classification call: what kind of work, rather than which tool."""
    recent = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in list(history)[-2:])
    context = f"Earlier in the conversation:\n{recent}\n\n" if recent else ""
    return {
        "model": settings.router_model(),
        "stream": False,
        "temperature": 0,
        "max_tokens": 200,
        "reasoning_effort": "none",
        "messages": [
            {"role": "system", "content": CATEGORY_PROMPT},
            {"role": "user", "content": f"{context}Message: {message}\n\nKind:"},
        ],
    }


def clean_category(text, has_images=False):
    """The category word out of the model's reply, or None when it did not name one."""
    words = re.findall(r"[a-z]+", re.sub(r"<think>.*?</think>", "", (text or ""), flags=re.S).lower())
    for word in words:
        if word in CATEGORIES:
            return "vision" if has_images and word == "vision" else word
    return None


def settle_category(answered, message, has_images=False):
    """The model's answer, unless it said GENERAL while the words plainly said otherwise.

    GENERAL is the absence of a signal rather than a finding, so a positive one beats it: asked
    "write me a python function" with no further detail, the small model answers GENERAL, and the
    words python and function are better evidence than that. A specific answer is never overruled.
    """
    if answered and answered != "general":
        return answered, "model"
    by_words = category_by_keywords(message, has_images)
    if by_words != "general":
        return by_words, "keywords"
    return answered or by_words, "model" if answered else "keywords"


def category_by_keywords(message, has_images=False):
    """Used when the model cannot decide. Prefers general, because a wrong specialist is worse."""
    text = (message or "").strip()
    if has_images:
        return "vision"
    if CODE_WORDS.search(text):
        return "code"
    if LONG_WORDS.search(text) or len(text) > 4000:
        return "long"
    if REASONING_WORDS.search(text):
        return "reasoning"
    return "general"


# Asking how a picture is made, rather than asking for one. "Design me a logo" wants a logo;
# "how do I design a logo" wants an explanation, and drawing one in reply answers nothing. The
# difference is whether the sentence is a question about the craft or a request for the thing.
ABOUT_THE_CRAFT = re.compile(
    r"^\s*(how|what|which|why|where|when|who)\b"
    r"|\b(can|could|should|do|would|may) i\b"
    r"|\b(best way|tips|advice|tutorial|step by step|walk me through|explain how)\b", re.I)


# Questions that name a place but want a fact about it. A map answers these with a pin and no
# words, which is a worse answer than a sentence, so the model is overruled on them.
FACT_ABOUT_PLACE = re.compile(
    r"^\s*(what|which|who|when|why|how many|how much)\b(?!.*\b(route|directions?|get (to|there)|"
    r"far|long|near|nearby|nearest|closest|way to)\b)"
    r".*\b(capital|population|people live|live in|famous|known for|currency|language|founded|"
    r"history|weather|climate|time zone|country|continent|mean|called)\b", re.I)


COORDINATES = re.compile(r"-?\d{1,3}\.\d+\s*,\s*-?\d{1,3}\.\d+")
FROM_TO = re.compile(r"\bfrom\b.{1,80}\bto\b", re.I | re.S)


def names_somewhere(message):
    """Whether anything in the words could be answered by a map: a way to get somewhere, a place
    asked after, a coordinate, from-here-to-there."""
    text = message or ""
    return bool(MAP_WORDS.search(text) or COORDINATES.search(text) or FROM_TO.search(text))


def settle_action(answered, message, allowed, has_images):
    """The model's answer, unless it fell back to CHAT while the words plainly asked for a map.

    CHAT is this prompt's catch-all, and a message like "I want to go to Futako Tamagawa, show me
    the easiest route" reads conversationally enough to land there. The words route, directions and
    want to go to are better evidence than a shrug. Beyond that only two answers are second-guessed
    - CHAT in favour of MAP, and IMAGE in favour of PHOTOS - because guessing at the rest is how a
    question ends up as a picture.
    """
    if answered == MAP and FACT_ABOUT_PLACE.search(message or ""):
        return CHAT, "keywords"
    # After a few turns about something else the small model says MAP for messages that name no
    # place at all - "clicking Try again three times started three answers" went to the map four
    # times running, which looked it up as a coordinate and failed. A map can only answer words
    # that point somewhere, so without any, MAP is not believed. A bare place name with no other
    # words ("Shinjuku station") is lost to CHAT by this, which answers it in words, not wrongly.
    if answered == MAP and not names_somewhere(message):
        return CHAT, "keywords"
    # The one other place the model is overruled, for the same reason: "show me a photo of it" and
    # "draw me a photo of it" are one word apart, and answering the first by drawing produces a
    # picture of a product that does not exist, with a made-up label, presented as the thing asked
    # about. Only ever in this direction - nothing is ever turned INTO a drawing here.
    if answered == IMAGE and PHOTOS in allowed and _wants_the_real_thing(message):
        return PHOTOS, "keywords"
    # And the same line drawn from the other side: asked for "a nice nature photo" the small model
    # says PHOTOS, which is a reasonable reading of the words and the wrong tool. Nothing has been
    # named, so there is nothing to go and find; that request is what the image model is for.
    if answered == PHOTOS and IMAGE in allowed and describes_a_picture(message):
        return IMAGE, "keywords"
    # And the same care with a picture that was attached rather than asked for: when the words
    # only put it forward and ask for no change to it, reading it is the answer
    if answered in (EDIT, CLEAN, BLUR, BACKDROP) and has_images and _only_showing(message):
        return CHAT, "keywords"
    if answered and answered != CHAT:
        return answered, "model"
    if MAP in allowed and not has_images and MAP_WORDS.search(message or ""):
        return MAP, "keywords"
    if PHOTOS in allowed and _wants_the_real_thing(message):
        return PHOTOS, "keywords"
    # "Design me a logo with name Lumina AI" came back as CHAT, and the answer was three
    # paragraphs of art direction and a prompt to paste into somebody else's image generator -
    # from a gateway with an image generator attached. CHAT is this prompt's catch-all, and a
    # request for a logo reads enough like a brief to land there.
    if IMAGE in allowed and not has_images and IMAGE_WORDS.search(message or "") \
            and not ABOUT_THE_CRAFT.search(message or ""):
        return IMAGE, "keywords"
    return answered, "model" if answered else None


def clean_answer(text, allowed):
    """Pulls the action word out of the model's reply; None when it did not name an allowed one."""
    words = re.findall(r"[a-z]+", re.sub(r"<think>.*?</think>", "", (text or ""), flags=re.S).lower())
    for word in words:
        if word in allowed:
            return word
    return None


def build_request(message, allowed, history=()):
    """The classification call: short, cold and without any thinking."""
    recent = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in list(history)[-4:])
    context = f"Earlier in the conversation:\n{recent}\n\n" if recent else ""
    return {
        "model": settings.router_model(),
        "stream": False,
        "temperature": 0,
        "max_tokens": 200,
        "reasoning_effort": "none",
        "messages": [
            {"role": "system", "content": PROMPT.format(allowed=", ".join(a.upper() for a in allowed))},
            {"role": "user", "content": f"{context}Message: {message}\n\nAction:"},
        ],
    }
