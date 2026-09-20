"""Lets the settings page edit config/config.properties without touching anything else in the file.

Only the keys listed here can be written, values are rewritten in place so comments survive, and
secrets are never sent back to the browser.
"""
import re
from pathlib import Path

from utils import config

EDITABLE_KEYS = [
    "gateway.public.url",
    "sso.client.id",
    "sso.client.secret",
    "sso.authorize.url",
    "sso.token.url",
    "sso.userinfo.url",
    "sso.scope",
    "sso.claim.id",
    "sso.claim.email",
    "sso.claim.name",
    "sso.admin.emails",
    "engine.unload_ollama_first",
    # Playground tools
    "features.web_search.enabled",
    "features.vision.enabled",
    "features.image_generation.enabled",
    "router.enabled",
    "router.model",
    "search.searxng.url",
    "search.results",
    "search.fetch_pages",
    "search.language",
    "images.comfyui.url",
    "images.checkpoint",
    "images.steps",
    "images.guidance",
    "images.edit.model",
    "images.edit.text_encoder",
    "images.edit.vae",
    "images.edit.lora",
    "images.rewrite.prompt",
    "images.prompt.model",
    "images.rewrite.prompt",
    "images.prompt.model",
    "memory.enabled",
    "memory.model",
    "memory.summarize.every",
    "memory.keep.recent",
    "memory.max.user.notes",
]
SECRET_KEYS = {"sso.client.secret"}


def read_settings():
    """Current values; secrets are reported as set/unset only."""
    values = {}
    for key in EDITABLE_KEYS:
        raw = config.get(key) or ""
        values[key] = {"value": "" if key in SECRET_KEYS else raw, "is_secret": key in SECRET_KEYS,
                       "is_set": bool(raw)}
    return values


def write_settings(updates):
    """Writes the given keys. A secret left empty keeps its current value."""
    unknown = [k for k in updates if k not in EDITABLE_KEYS]
    if unknown:
        raise ValueError(f"Not editable: {', '.join(sorted(unknown))}")

    path = Path(config.config_file_path())
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    written = []
    for key, value in updates.items():
        value = (value or "").strip()
        if key in SECRET_KEYS and not value:
            continue  # blank secret field means "leave unchanged"
        if "\n" in value or "\r" in value:
            raise ValueError(f"Value for {key} must be a single line")
        pattern = rf"(?m)^{re.escape(key)}=.*$"
        replacement = f"{key}={value}"
        text = re.sub(pattern, replacement, text) if re.search(pattern, text) else text.rstrip("\n") + f"\n{replacement}\n"
        written.append(key)
    path.write_text(text, encoding="utf-8")
    return written
