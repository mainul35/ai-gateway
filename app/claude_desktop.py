"""The Claude desktop app on this gateway: Claude's own model names, each standing for one of ours.

The desktop app, set to a third-party gateway, asks it for its models (GET /v1/models, with an
anthropic-version header) and takes only IDs that are Claude's. So, as Ollama's own Claude Desktop
proxy does, a few of Claude's real IDs are offered, each mapped to a gateway model, and the picker
shows both names: "Sonnet 5" in a request means qwen3.6-vllm here, and the person choosing sees
"Sonnet 5 · qwen3.6-vllm". Requests naming one of these IDs are sent to the model it stands for.

The mapping is anthropic.desktop.models, "claude-sonnet-5=qwen3.6-vllm, claude-opus-5=gemma4:31b";
an ID left out gets a default from the models there are, each a different one while there are
models enough, so the picker offers a choice. "auto" is a target too: the gateway then picks the
model per request (app/tools/auto_model.py).

Claude Code asks the same GET /v1/models when its gateway model discovery is on, and keeps every ID
with "claude" in it: it gets every model it may use as claude/<model>, shown by the model's name, and
a request naming claude/<model> runs that model.
"""
from app import access, backends, settings

# Claude's IDs that the app accepts, in the order it prefers them (from Ollama's own proxy), and more
# of Claude's real IDs after them, so more than four gateway models fit in the picker
SLOTS = (
    {"id": "claude-sonnet-5", "name": "Sonnet 5", "family": "sonnet", "created_at": "2026-06-30T00:00:00Z", "default": True},
    {"id": "claude-opus-5", "name": "Opus 5", "family": "opus", "created_at": "2026-07-24T00:00:00Z", "default": True},
    {"id": "claude-haiku-4-5-20251001", "name": "Haiku 4.5", "family": "haiku", "created_at": "2025-10-01T00:00:00Z", "default": True},
    {"id": "claude-fable-5", "name": "Fable 5", "family": "fable", "created_at": "2026-06-09T00:00:00Z", "default": True},
    {"id": "claude-sonnet-4-6", "name": "Sonnet 4.6", "family": "sonnet", "created_at": "2025-11-18T00:00:00Z", "default": False},
    {"id": "claude-opus-5-5", "name": "Opus 5.5", "family": "opus", "created_at": "2026-10-01T00:00:00Z", "default": False},
    {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5", "family": "sonnet", "created_at": "2026-10-08T00:00:00Z", "default": False},
    {"id": "claude-fable-5-1", "name": "Fable 5.1", "family": "fable", "created_at": "2026-09-01T00:00:00Z", "default": False},
    {"id": "claude-opus-4-8", "name": "Opus 4.8", "family": "opus", "created_at": "2026-05-01T00:00:00Z", "default": False},
)
SLOT_IDS = {slot["id"] for slot in SLOTS}
# The slots each given the next model not yet shown, in this order
SPREAD = ("claude-opus-5", "claude-fable-5", "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1",
          "claude-opus-4-8")

# Claude Code's name for a gateway model: "claude" in it passes its discovery filter, and not starting
# with claude- keeps it from being taken for one of Claude's own models (its context window included)
PREFIX = "claude/"


def _configured():
    raw = settings.get("anthropic.desktop.models", "ANTHROPIC_DESKTOP_MODELS") or ""
    pairs = {}
    for item in raw.replace(";", ",").split(","):
        if "=" in item:
            slot, model = (part.strip() for part in item.split("=", 1))
            if slot in SLOT_IDS and model:
                pairs[slot] = model
    return pairs


def _tool_models(models, principal):
    """The models this person may give an agent - those that can call tools - widest window first."""
    return sorted((n for n, b in models.items() if "tools" in b.capabilities
                   and (principal is None or access.can_use_model(principal, n))),
                  key=lambda n: (-(models[n].context_length or 0), n))


def _defaults(models, principal=None):
    """A model per ID when none is configured: the main model for Sonnet 5, the small (or main) one
    for Haiku, the gateway's own choice for Sonnet 4.6, and every other model the person may use on
    the next of the SPREAD IDs - a coder first. The long model is left to [1m] (route): it is the main
    one with a wider window, not another choice. IDs past the last model are not offered, but for the
    four the app starts on, which fall back to the main model."""
    can = lambda name, *caps: name in models and all(c in models[name].capabilities for c in caps)
    main = settings.get("anthropic.model.default", "ANTHROPIC_MODEL_DEFAULT") or "qwen3.6-vllm"
    if not can(main, "tools"):
        main = next((n for n in sorted(models) if can(n, "tools")), main)
    small = settings.get("anthropic.model.small", "ANTHROPIC_MODEL_SMALL") or main
    long = settings.get("anthropic.model.long", "ANTHROPIC_MODEL_LONG") or "qwen3.6-sglang-1m"
    rest = [n for n in _tool_models(models, principal) if n not in (main, small, long)]
    rest.sort(key=lambda n: "coder" not in n.lower())     # stable: a coder first, else widest first
    found = {"claude-sonnet-5": main, "claude-haiku-4-5-20251001": small, "claude-sonnet-4-6": "auto"}
    for slot in SPREAD:
        found[slot] = rest.pop(0) if rest else None
    for slot in SLOTS:
        if slot["default"] and not found.get(slot["id"]):
            found[slot["id"]] = main
    return found


async def mapping(principal=None):
    """{Claude ID: gateway model or "auto"} for every ID, for this person.

    Someone who chose a model for their desktop app (`ai-gateway launch claude-desktop -Model x`,
    users.desktop_model) gets it behind every one of Claude's main names - Sonnet, Opus, Haiku, Fable -
    so whichever the app asks for, and its background calls too, run the model they chose and the
    GPU never swaps mid-task. Sonnet 4.6 stays Auto. Their choice is used only while it is still a
    model there is and they may use."""
    models = await backends.available_models()
    found = {**_defaults(models, principal), **_configured()}
    chosen = getattr(getattr(principal, "user", None), "desktop_model", None)
    if chosen and (chosen == "auto" or (chosen in models and access.can_use_model(principal, chosen))):
        found.update({slot["id"]: chosen for slot in SLOTS if slot["default"]})
    return found


LONG_SUFFIX = "[1m]"


async def route(asked, principal=None):
    """The gateway model (or "auto") a request naming one of the IDs means; anything else unchanged.

    A name ending in [1m] is how the desktop app's "Offer 1M-context variant" and Claude Code's
    sonnet[1m] ask for a million-token window: it goes to the gateway's 1M model (anthropic.model.long,
    qwen3.6-sglang-1m) when there is one, and otherwise to whatever the name without it means.
    """
    name = (asked or "").strip()
    if name.lower().endswith(LONG_SUFFIX):
        long = settings.get("anthropic.model.long", "ANTHROPIC_MODEL_LONG") or "qwen3.6-sglang-1m"
        if long in await backends.available_models():
            return long
        name = name[:-len(LONG_SUFFIX)]
    if name.lower().startswith(PREFIX):         # Claude Code's claude/<model>
        return name[len(PREFIX):]
    if name in SLOT_IDS:
        # An ID with no model of its own (not offered) means what any other Claude name does
        return (await mapping(principal))[name] or name
    return asked


def _about(backend):
    """One line for a picker: how much the model reads at once, and whether it sees pictures."""
    context = backend.context_length or 32768
    window = f"{context // 1048576}M" if context >= 1048576 else f"{context // 1024}K"
    return f"{window} context" + (", reads images" if "vision" in backend.capabilities else "") + ", on the gateway"


def is_claude_code(headers):
    """Whether GET /v1/models comes from Claude Code: its user agent, or the launcher's client header."""
    return ((headers.get("user-agent") or "").lower().startswith("claude-cli")
            or (headers.get("x-ai-gateway-client") or "").lower() == "claude-code")


async def catalogue(principal, claude_code=False):
    """GET /v1/models as Anthropic's clients want it. The desktop app: Claude's IDs, the gateway
    models' names. Claude Code: every model the person may give it, as claude/<model>."""
    models = await backends.available_models()
    data = []
    if claude_code:
        for name in _tool_models(models, principal):
            data.append({"id": PREFIX + name, "type": "model", "display_name": name,
                         "description": _about(models[name]), "created_at": "2026-01-01T00:00:00Z",
                         "max_tokens": models[name].context_length or 32768})
    chosen = {} if claude_code else await mapping(principal)
    for slot in SLOTS if not claude_code else ():
        target = chosen.get(slot["id"])
        if target == "auto":
            shown, context = "auto - the gateway picks per request", 131072
        elif target in models and access.can_use_model(principal, target):
            shown, context = target, models[target].context_length or 32768
        else:
            continue     # not a model this person may use, or not one there is: not offered
        # Both names: the one the app knows the row by, and the model that answers
        data.append({"id": slot["id"], "type": "model", "display_name": f"{slot['name']} · {shown}",
                     "created_at": slot["created_at"],
                     "max_tokens": context, "anthropic_family_tier": slot["family"],
                     "is_family_default": slot["default"]})
    return {"data": data, "first_id": data[0]["id"] if data else "", "last_id": data[-1]["id"] if data else "",
            "has_more": False}
