"""The model name "auto" for API clients: the gateway picks the model, as the playground's Auto does.

The same two steps as the playground. The small router model says what kind of work the latest
message asks for - code, reasoning, a long read, a picture, or everyday - and the chooser
(app/tools/chooser.py) turns that into the model this caller may use that suits it best: the
smallest one for everyday questions, a bigger one only when the work calls for it. A request that
sends tools only goes to a model that can call them, and one with a picture only to one that can see.

Not for Claude Code: it calls tools on every turn and is better on one model for a whole session.
"""
import json
import logging
from datetime import timedelta

from app import access, backends, settings, usage as usage_log
from app.db import session_factory
from app.engine.supervisor import supervisor
from app.models import utcnow
from app.tools import chooser, router as intent_router

log = logging.getLogger("tools.auto_model")

NAMES = ("auto",)
HEALTH_DAYS = 30        # how far back a model's failures and latency count against it


def is_auto(name):
    return (name or "").strip().lower() in NAMES


def _text(content):
    """A message's words, from a plain string or a list of OpenAI parts or Anthropic blocks."""
    if isinstance(content, str):
        return content
    return " ".join(part.get("text") or "" for part in content or ()
                    if isinstance(part, dict) and part.get("type") == "text")


async def _category(principal, messages, has_images):
    """What kind of work the latest user message is, and who decided: the router model or keywords."""
    # Only messages with words: in a tool loop the latest "user" message is tool results alone, and
    # judging that would drop the task to the smallest model halfway through it. The task is the
    # last thing the person actually said, so every step of one task goes to the same model.
    said = [{"role": m.get("role"), "content": _text(m.get("content"))}
            for m in messages if m.get("role") in ("user", "assistant") and _text(m.get("content")).strip()]
    latest = next((m["content"] for m in reversed(said) if m["role"] == "user"), "")
    if not intent_router.is_enabled():
        return intent_router.category_by_keywords(latest, has_images), "keywords"
    # Imported here: openai_v1 imports this module, and the router's one-word answer goes through
    # the same forward() as every other request
    from app.routers.openai_v1 import PROXY_PATHS, forward
    try:
        response = await forward(principal, "chat_completions", PROXY_PATHS["chat_completions"],
                                 intent_router.build_category_request(
                                     latest, intent_router.plain_history(said[:-1])),
                                 skip_access=True)
        answer = json.loads(response.body)["choices"][0]["message"].get("content")
    except Exception as e:      # a router that fails leaves the keywords, never a failed request
        log.info("router could not classify for auto: %s", e)
        return intent_router.category_by_keywords(latest, has_images), "keywords"
    found = intent_router.clean_category(answer, has_images)
    category, by = intent_router.settle_category(found, latest, has_images)
    return category, settings.router_model() if by == "model" else "keywords"


async def choose(principal, messages, has_images=False, needs_tools=False):
    """The model to answer with: (name, category, why), or (None, category, reason) when none will do."""
    category, decided_by = await _category(principal, messages, has_images)
    async with session_factory()() as session:
        health = await usage_log.by_model(session, utcnow() - timedelta(days=HEALTH_DAYS))
    candidates = []
    for name, backend in (await backends.available_models()).items():
        if not access.can_use_model(principal, name):
            continue
        if needs_tools and backend.capabilities and "tools" not in backend.capabilities:
            continue
        details = backend.details or {}
        candidates.append({"name": name, "capabilities": sorted(backend.capabilities),
                           "parameter_size": details.get("parameter_size", ""),
                           "context_length": backend.context_length, "health": health.get(name, {}),
                           "blocked": supervisor.blocked_by_busy_gpu(name, backend)})
    picked = chooser.pick(candidates, category, needs_vision=has_images)
    if picked:
        log.info("auto: %s work (by %s) -> %s (%s)", category, decided_by, picked["model"], picked["why"])
        return picked["model"], category, picked["why"]
    fallback = chooser.smallest(candidates, needs_vision=has_images)
    if fallback:
        return fallback, category, "smallest model available"
    return None, category, ("no model you may use can see images" if has_images
                            else "no model you may use can call tools" if needs_tools
                            else "no model you may use is available")
