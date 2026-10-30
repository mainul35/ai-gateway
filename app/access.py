"""Who may use which models, and who may manage whom.

Roles, lowest to highest:
  user     uses the models they have been granted
  manager  grants model access to users and manages their keys; cannot change roles or settings
  admin    everything, always with access to every model

Model access per user: "all", "none", "selected" with a list of model names, or "public": the
models listed in gateway.public.models, which is what an approved SSO sign-up gets. Entries may be
shell-style patterns, so "qwen*" covers every model whose name starts with qwen.
"""
import fnmatch
import json

from app import settings

ROLES = ("user", "manager", "admin")
ACCESS_MODES = ("all", "selected", "public", "none")


def default_access():
    """Access given to accounts created without an explicit choice (including SSO sign-ups)."""
    value = (settings.get("gateway.default.model.access", "GATEWAY_DEFAULT_MODEL_ACCESS") or "all").lower()
    return value if value in ACCESS_MODES else "all"


def public_models():
    """The models every approved account may use: gateway.public.models, names or patterns,
    separated by commas or spaces. Empty means none."""
    raw = settings.get("gateway.public.models", "GATEWAY_PUBLIC_MODELS") or ""
    return [p for p in raw.replace(",", " ").split() if p]


def parse_patterns(raw):
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return [str(p) for p in value if str(p).strip()] if isinstance(value, list) else []


def serialize_patterns(patterns):
    cleaned = sorted({str(p).strip() for p in patterns or [] if str(p).strip()})
    return json.dumps(cleaned) if cleaned else None


def is_pattern(entry):
    return any(ch in entry for ch in "*?[")


def can_use_model(principal, model):
    if principal.is_master:
        return True
    user = principal.user
    if user is None:
        return False
    if user.role == "admin":
        return True
    mode = user.model_access or "all"
    if mode == "all":
        return True
    if mode == "none":
        return False
    patterns = public_models() if mode == "public" else parse_patterns(user.allowed_models)
    return any(fnmatch.fnmatchcase(model, pattern) for pattern in patterns)


# --- features ---------------------------------------------------------------------------------

# What a person may do beyond talking to a model, each granted or not per account by an admin (a
# manager, for plain users, only what they have themselves). Admins have every one. The server
# refuses a feature not granted; the playground and the menu only hide it.
FEATURES = {
    "web_search": "Web search",
    "vision": "Image understanding",
    "maps": "Maps",
    "image_generation": "Image generation and photo editing",
    "video": "Video stories",
    "subtitles": "YouTube subtitles",
    "agent": "Agent (tool servers)",
}
# A picture or a video holds the whole GPU for a minute or more: not given by default to an account
# that has only the public models, which is what an approved SSO sign-up gets
GPU_FEATURES = ("image_generation", "video")


def default_features(user):
    """An account's features until someone chooses them: what it could do before features were
    granted one by one - everything, less the GPU's with public models, nothing with none."""
    mode = (user.model_access if user else None) or "all"
    if mode == "none":
        return []
    return [f for f in FEATURES if mode != "public" or f not in GPU_FEATURES]


def features_of(user):
    if user is None:
        return []
    if user.role == "admin":
        return list(FEATURES)
    if user.features is None:
        return default_features(user)
    try:
        chosen = json.loads(user.features)
    except ValueError:
        return []
    return [f for f in FEATURES if isinstance(chosen, list) and f in chosen]


def serialize_features(features):
    return json.dumps([f for f in FEATURES if f in set(features or [])])


def can_use_feature(principal, feature):
    if principal.is_master or principal.is_admin:
        return True
    user = principal.user
    return user is not None and user.is_active and feature in features_of(user)


def can_generate_images(principal):
    return can_use_feature(principal, "image_generation")


def can_manage(principal, target):
    """Admins manage everyone; managers manage plain users only."""
    if principal.is_admin:
        return True
    return principal.is_manager and target.role == "user"


# --- tool servers ---------------------------------------------------------------------------

"""How a user is allowed to reach a tool server.

Three ways, chosen with tools.access.mode:

    open     anyone who may use a model may use the tool servers that are switched on. What the
             gateway did before this existed, and what a single-person install wants.
    claims   the auth server decides. Whatever it said about the person at their last sign-in is
             searched for a capability naming the server, and the gateway grants nothing it did
             not find. This is the one to use when somewhere else owns the question.
    none     nobody but an admin.

The claims mode is deliberately thin. An auth server's shape is its own - a space-separated scope
string, a list of roles, an object per client - so the only things configured here are which claim
to read and what a capability for a server looks like. Nothing is inferred, and a claim that is
missing grants nothing rather than everything: the failure has to be a closed door.
"""

TOOL_ACCESS_MODES = ("open", "claims", "none")


def tool_access_mode():
    value = (settings.get("tools.access.mode", "TOOLS_ACCESS_MODE") or "open").strip().lower()
    return value if value in TOOL_ACCESS_MODES else "open"


def _claim_values(user, claim):
    """One claim from what the auth server said, as a list of strings, whatever shape it arrived in.

    A scope arrives as "a b c", a role list as ["a", "b"], and some providers nest one inside an
    object per client. Those three cover nearly everything; anything else reads as nothing, which
    is a closed door rather than an open one.
    """
    if not user or not user.claims:
        return []
    try:
        claims = json.loads(user.claims)
    except (ValueError, TypeError):
        return []
    found = claims
    for part in claim.split("."):            # "resource_access.gateway.roles"
        if not isinstance(found, dict):
            return []
        found = found.get(part)
    if isinstance(found, str):
        return found.split()
    if isinstance(found, list):
        return [str(v) for v in found]
    return []


def capabilities_of(user):
    """Everything the auth server granted this person, in the claim the gateway was told to read."""
    claim = settings.get("tools.access.claim", "TOOLS_ACCESS_CLAIM") or "scope"
    return _claim_values(user, claim)


def can_use_tool_server(principal, server_name):
    """Whether this principal may reach one tool server by name."""
    if principal.is_master or principal.is_admin:
        return True
    user = principal.user
    if user is None or not user.is_active:
        return False
    mode = tool_access_mode()
    if mode == "none":
        return False
    if mode == "open":
        # The same bar as using a model at all: someone with no models has no business with tools
        return (user.model_access or "all") != "none"
    pattern = settings.get("tools.access.capability", "TOOLS_ACCESS_CAPABILITY") or "mcp:{server}"
    wanted = {pattern.replace("{server}", server_name), pattern.replace("{server}", "*")}
    return bool(wanted & set(capabilities_of(user)))


def tool_servers_for(principal, servers):
    """The ones of these a principal may reach."""
    return [s for s in servers if can_use_tool_server(principal, s.name)]
