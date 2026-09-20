"""Decides what a playground message asks for, so the toggles mean "allowed" rather than "forced".

With web search, image understanding and image generation all switched on, "What is the latest Java
version?" must be answered from the web, not drawn. A small fast model reads the message and picks one
action; if it is unavailable or answers something unexpected, plain keyword rules decide instead. Those
rules prefer answering over drawing, because a wrong picture wastes far more time than a wrong search.
"""
import logging
import re

from app import settings

log = logging.getLogger("tools.router")

CHAT, SEARCH, IMAGE, EDIT = "chat", "search", "image", "edit"

PROMPT = """You route a user's message to one of these actions. Answer with one word and nothing else.

SEARCH - needs current or factual information from the web about the world: news, prices, releases,
         versions, dates, "latest", "today", anything you could not answer reliably from memory.
CHAT   - can be answered or discussed directly: explanations, opinions, maths, writing, and anything
         about the user's own code, configuration, logs, machine or earlier messages, including
         troubleshooting. Questions about a picture the user attached are also CHAT.
IMAGE  - asks for a picture to be created: "draw", "generate an image of", "a photo of a ...". A
         message that only describes a picture, with no question and no instruction, is also IMAGE.
EDIT   - asks for the attached picture to be changed: "make it night", "remove the car", "add a hat".

Examples:
"What is the latest Java version as of today?" -> SEARCH
"Why is my Spring Boot app slow?" -> CHAT
"Why does my container lose its network after a reboot?" -> CHAT
"What is 17 * 23?" -> CHAT
"draw a fox in the snow" -> IMAGE
"an oil painting of a harbour at dawn, stormy sky" -> IMAGE
"make it night with northern lights" -> EDIT
"what is in this image?" -> CHAT
"can you read the text in this screenshot?" -> CHAT

Allowed answers this time: {allowed}. Answer with one of those words only."""

IMAGE_WORDS = re.compile(
    r"\b(draw|sketch|paint|render|illustrate|generate|create|make|produce|design)\b[^.?!]{0,40}"
    r"\b(image|picture|photo|photograph|drawing|painting|illustration|logo|icon|poster|wallpaper|art|artwork)\b"
    r"|^\s*(an? |the )?(image|picture|photo|drawing|painting|illustration|logo|poster|wallpaper) of\b", re.I)
EDIT_WORDS = re.compile(
    r"\b(make it|turn it|change|replace|remove|delete|erase|add|put|swap|recolou?r|repaint|crop|zoom|"
    r"brighten|darken|blur)\b", re.I)
SEARCH_WORDS = re.compile(
    r"\b(latest|newest|current|currently|today|todays|tonight|now|recent|recently|this (week|month|year)|"
    r"news|price|cost|release[ds]?|version|available|stock|weather|score|who is|who won|when (is|did|will)|"
    r"how much|20[2-9]\d)\b", re.I)


def is_enabled():
    return settings.get_bool("router.enabled", "ROUTER_ENABLED", True) and bool(settings.router_model())


def candidates(tools, has_images):
    """The actions the user's toggles allow for this message."""
    allowed = [CHAT]
    # A web search is text only, so it cannot help with a picture the user just attached
    if tools.get("web_search") and not has_images:
        allowed.append(SEARCH)
    if tools.get("image_generation"):
        allowed.append(EDIT if has_images else IMAGE)
    return allowed


def by_keywords(message, allowed, has_images):
    """Used when the router model cannot decide. Only ever returns an allowed action."""
    text = (message or "").strip()
    if has_images and EDIT in allowed and EDIT_WORDS.search(text) and not text.endswith("?"):
        return EDIT
    if IMAGE in allowed and IMAGE_WORDS.search(text):
        return IMAGE
    if SEARCH in allowed and SEARCH_WORDS.search(text):
        return SEARCH
    return CHAT


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
