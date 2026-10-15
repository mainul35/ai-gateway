"""The Claude desktop app on this gateway: Claude's own model names, each standing for one of ours.

The desktop app, set to a third-party gateway, asks it for its models (GET /v1/models, with an
anthropic-version header) and takes only IDs that are Claude's. So, as Ollama's own Claude Desktop
proxy does, a few of Claude's real IDs are offered, each mapped to a gateway model, and the picker
shows the gateway model's name: "Sonnet 5" in a request means qwen3-next-80b here, and the person
choosing sees "qwen3-next-80b". Requests naming one of these IDs are sent to the model it stands for.

The mapping is anthropic.desktop.models, "claude-sonnet-5=qwen3-next-80b, claude-opus-5=gemma4:31b";
an ID left out gets a default from the models there are. "auto" is a target too: the gateway then
picks the model per request (app/tools/auto_model.py).
"""
from app import access, backends, settings

# Claude's IDs that the app accepts, in the order it prefers them (from Ollama's own proxy)
SLOTS = (
    {"id": "claude-sonnet-5", "family": "sonnet", "created_at": "2026-06-30T00:00:00Z", "default": True},
    {"id": "claude-opus-5", "family": "opus", "created_at": "2026-07-24T00:00:00Z", "default": True},
    {"id": "claude-haiku-4-5-20251001", "family": "haiku", "created_at": "2025-10-01T00:00:00Z", "default": True},
    {"id": "claude-fable-5", "family": "fable", "created_at": "2026-06-09T00:00:00Z", "default": True},
    {"id": "claude-sonnet-4-6", "family": "sonnet", "created_at": "2025-11-18T00:00:00Z", "default": False},
)
SLOT_IDS = {slot["id"] for slot in SLOTS}


def _configured():
    raw = settings.get("anthropic.desktop.models", "ANTHROPIC_DESKTOP_MODELS") or ""
    pairs = {}
    for item in raw.replace(";", ",").split(","):
        if "=" in item:
            slot, model = (part.strip() for part in item.split("=", 1))
            if slot in SLOT_IDS and model:
                pairs[slot] = model
    return pairs


def _defaults(models):
    """A model per ID when none is configured: the main model for Sonnet, one that can see for Opus,
    the small (or main) one for Haiku, a coder for Fable, and the gateway's own choice for Sonnet 4.6."""
    can = lambda name, *caps: name in models and all(c in models[name].capabilities for c in caps)
    main = settings.get("anthropic.model.default", "ANTHROPIC_MODEL_DEFAULT") or "qwen3-next-80b"
    if not can(main, "tools"):
        main = next((n for n in sorted(models) if can(n, "tools")), main)
    small = settings.get("anthropic.model.small", "ANTHROPIC_MODEL_SMALL") or main
    seeing = next((n for n in ("gemma4:31b", *sorted(models)) if can(n, "tools", "vision")), main)
    coder = next((n for n in sorted(models) if "coder" in n.lower() and can(n, "tools")), main)
    return {"claude-sonnet-5": main, "claude-opus-5": seeing, "claude-haiku-4-5-20251001": small,
            "claude-fable-5": coder, "claude-sonnet-4-6": "auto"}


async def mapping():
    """{Claude ID: gateway model or "auto"} for every ID."""
    models = await backends.available_models()
    return {**_defaults(models), **_configured()}


async def route(asked):
    """The gateway model (or "auto") a request naming one of the IDs means; anything else unchanged."""
    name = (asked or "").strip()
    if name in SLOT_IDS:
        return (await mapping())[name]
    return asked


async def catalogue(principal):
    """GET /v1/models as the desktop app wants it: Claude's IDs, the gateway models' names."""
    models = await backends.available_models()
    data = []
    for slot in SLOTS:
        target = (await mapping())[slot["id"]]
        if target == "auto":
            shown, context = "auto - the gateway picks per request", 131072
        elif target in models and access.can_use_model(principal, target):
            shown, context = target, models[target].context_length or 32768
        else:
            continue     # not a model this person may use, or not one there is: not offered
        data.append({"id": slot["id"], "type": "model", "display_name": shown, "created_at": slot["created_at"],
                     "max_tokens": context, "anthropic_family_tier": slot["family"],
                     "is_family_default": slot["default"]})
    return {"data": data, "first_id": data[0]["id"] if data else "", "last_id": data[-1]["id"] if data else "",
            "has_more": False}
