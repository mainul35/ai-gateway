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
         troubleshooting. Questions about a picture the user attached are also CHAT.
IMAGE  - asks for a picture to be created that nobody has ever photographed: "draw", "generate an
         image of", "make me a picture of". A message that only describes a picture, with no
         question and no instruction, is also IMAGE.
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
    r"\b(image|picture|photo|photograph|drawing|painting|illustration|logo|icon|poster|wallpaper|art|artwork)\b"
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


# Questions that name a place but want a fact about it. A map answers these with a pin and no
# words, which is a worse answer than a sentence, so the model is overruled on them.
FACT_ABOUT_PLACE = re.compile(
    r"^\s*(what|which|who|when|why|how many|how much)\b(?!.*\b(route|directions?|get (to|there)|"
    r"far|long|near|nearby|nearest|closest|way to)\b)"
    r".*\b(capital|population|people live|live in|famous|known for|currency|language|founded|"
    r"history|weather|climate|time zone|country|continent|mean|called)\b", re.I)


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
    if answered and answered != CHAT:
        return answered, "model"
    if MAP in allowed and not has_images and MAP_WORDS.search(message or ""):
        return MAP, "keywords"
    if PHOTOS in allowed and _wants_the_real_thing(message):
        return PHOTOS, "keywords"
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
