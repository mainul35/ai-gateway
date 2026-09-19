"""Who may use which models, and who may manage whom.

Roles, lowest to highest:
  user     uses the models they have been granted
  manager  grants model access to users and manages their keys; cannot change roles or settings
  admin    everything, always with access to every model

Model access per user: "all", "none", or "selected" with a list of model names. Entries may be
shell-style patterns, so "qwen*" covers every model whose name starts with qwen.
"""
import fnmatch
import json

from app import settings

ROLES = ("user", "manager", "admin")
ACCESS_MODES = ("all", "selected", "none")


def default_access():
    """Access given to accounts created without an explicit choice (including SSO sign-ups)."""
    value = (settings.get("gateway.default.model.access", "GATEWAY_DEFAULT_MODEL_ACCESS") or "all").lower()
    return value if value in ACCESS_MODES else "all"


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
    return any(fnmatch.fnmatchcase(model, pattern) for pattern in parse_patterns(user.allowed_models))


def can_manage(principal, target):
    """Admins manage everyone; managers manage plain users only."""
    if principal.is_admin:
        return True
    return principal.is_manager and target.role == "user"
