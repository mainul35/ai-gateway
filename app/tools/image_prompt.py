"""Turns what the user asked for into a prompt an image model can actually draw.

Flux reads a description of a picture, not a request to an assistant.
"""
import re

from app import settings

MAX_WORDS = 70

PROMPT = """You write prompts for an image generator. Turn the user's latest request into one vivid
description of a single picture.

Rules:
- Describe only what should be seen: subject, setting, composition, style, colours, lighting, mood.
- Never write instructions, questions or words like "generate", "create", "image of", "based on".
- The image generator cannot browse or copy an existing picture. Well-known logos and symbols may be
  described in words.
- If words should appear in the picture, name them once in double quotes and keep them short:
  "JDK 27", never a sentence, a paragraph or small print.
- If the user is correcting or refining the picture before, keep everything that still applies and
  change only what they asked to change.
- English only, under 60 words, no line breaks.

Reply with the description and nothing else."""

# Asked as its own question, because a small model answers one plain question well and ignores an
# extra instruction tacked onto another. The answer decides whether the lettering is corrected after.
WORDING_PROMPT = """The user asked for a picture. Some pictures must contain words: a banner, a poster,
a logo, a sign, a title card, anything the user says should "say" something.

Reply with the exact words that must appear in the picture, and nothing else. Keep them short, as they
would be written on the picture itself. If the picture needs no words at all, reply with NONE.

Only words the user actually asked for. A scene, a place, a mood or a style is not a caption: never
invent a title for a picture.
"""


def is_enabled():
    return settings.get_bool("images.rewrite.prompt", "IMAGES_REWRITE_PROMPT", True) and bool(model())


def model():
    return settings.get("images.prompt.model", "IMAGES_PROMPT_MODEL") or settings.router_model()


def build_request(request, history=(), previous_prompts=()):
    """history is [{role, content}] oldest first; previous_prompts are the pictures already made."""
    context = []
    recent = [m for m in history if (m.get("content") or "").strip()][-6:]
    if recent:
        context.append("Conversation so far:\n"
                       + "\n".join(f"{m['role']}: {m['content'][:600]}" for m in recent))
    if previous_prompts:
        context.append("Pictures already made in this conversation, most recent last:\n"
                       + "\n".join(f"- {p[:300]}" for p in list(previous_prompts)[-3:]))
    context.append(f"The user now asks: {request}\n\nDescription of the picture to draw:")
    return {
        "model": model(), "stream": False, "temperature": 0.3, "max_tokens": 400,
        "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": "\n\n".join(context)}],
    }


def wording_request(request, history=()):
    """Asks which words, if any, have to be spelled correctly in the picture."""
    recent = [m for m in history if (m.get("content") or "").strip()][-2:]
    context = "".join(f"Earlier: {m['content'][:200]}\n" for m in recent)
    return {
        "model": model(), "stream": False, "temperature": 0, "max_tokens": 200,
        "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": WORDING_PROMPT},
                     {"role": "user", "content": f'{context}"{request}" ->'}],
    }


def clean_wording(text):
    """The words a picture must spell, or None."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip().strip('"\'`').strip()
    text = re.sub(r"^(the )?(words?|text)\s*[:\-]\s*", "", text, flags=re.I).strip('"\'` ')
    if not text or len(text) > 40 or re.fullmatch(r"(none|no|n/a|nothing|null)\.?", text, re.I):
        return None
    return text if re.search(r"\w", text) else None


def with_wording(description, wording):
    """Makes sure the words to draw are in the description, since that is what is drawn from."""
    if not wording or wording.lower() in description.lower():
        return description
    return f'{description.rstrip(". ")}, with the text "{wording}"'


def clean(text, fallback):
    """Returns (description, wording): the wording is what must be spelled right in the picture."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    # Models like to answer with a label first; the description is what follows it
    text = re.sub(r"^\s*(here is|here's|prompt|description)\b[^:\n]*:\s*", "", text.strip(), flags=re.I)

    wording = None
    marked = re.search(r"^\s*TEXT\s*:\s*(.+)$", text, flags=re.I | re.M)
    if marked:
        wording = marked.group(1).strip().strip('"\'`').strip()
        text = text[:marked.start()] + text[marked.end():]
    description = " ".join(text.split()).strip(' `')
    if len(description.split()) < 3:
        return fallback, wording
    if not wording:
        # The description itself may carry the words, quoted as the prompt asks for
        quoted = re.search(r'"([^"]{1,40})"', description)
        wording = quoted.group(1).strip() if quoted else None
    if wording and (len(wording) > 40 or not re.search(r"\w", wording) or wording.lower() in ("none", "no")):
        wording = None
    return " ".join(description.split()[:MAX_WORDS]), wording
