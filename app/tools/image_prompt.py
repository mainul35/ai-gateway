"""Turns what the user asked for into a prompt an image model can actually draw.

Flux reads a description of a picture, not a request to an assistant. "Generate me an image of Java 27
based on the images available on the web" describes nothing to draw, and a follow-up such as "I wanted
a banner, not an IDE screenshot" makes sense only against the picture before it. So the conversation,
including the prompts of the images already made in it, is rewritten into one visual description.
"""
import re

from app import settings

MAX_WORDS = 70

PROMPT = """You write prompts for an image generator. Turn the user's latest request into one vivid
description of a single picture.

Rules:
- Describe only what should be seen: subject, setting, composition, style, colours, lighting, mood.
- Never write instructions, questions or words like "generate", "create", "image of", "based on".
- The image generator cannot browse, copy an existing picture, or read long text. Well-known logos and
  symbols may be described in words. Keep any words meant to appear in the picture to a few characters.
- If the user is correcting or refining the picture before, keep everything that still applies and
  change only what they asked to change.
- English only, under 60 words, no line breaks, no quotes.

Reply with the description and nothing else."""


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


def clean(text, fallback):
    """The description, or the user's own words if the model produced nothing usable."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    # Models like to answer with a label first; the description is what follows it
    text = re.sub(r"^\s*(here is|here's|prompt|description)\b[^:\n]*:\s*", "", text.strip(), flags=re.I)
    text = " ".join(text.split()).strip(' "\'`')
    if len(text.split()) < 3:
        return fallback
    return " ".join(text.split()[:MAX_WORDS])
