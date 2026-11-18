"""Models a cloud provider serves - Qwen Cloud, or any other OpenAI-compatible API - set up and
changed entirely from the Models page.

Nothing about a provider is in a file or in this code: the providers, their models, what each model
can do, who may use them and whether auto may pick them are rows in the database (CloudProvider),
and the API key is stored sealed (app/secretbox.py) and never sent back to the browser. A change on
the page applies to the next request, without a restart or a deploy.

Each model is offered as <provider>/<model id> - qwen/qwen3.8-max - so it can never be mistaken for
a model on this machine, and goes through the gateway like any other: access, limits and usage
accounting apply, and Claude Code's requests are translated to the OpenAI format on the way
(app/anthropic_compat.py).
"""
import json
import logging
import re
import urllib.parse

import httpx
from sqlalchemy import select

from app import secretbox
from app.db import session_factory
from app.models import CloudProvider

log = logging.getLogger(__name__)

KIND = "cloud"
NAME_SHAPE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MODEL_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
MOST_MODELS = 200

# What the database held when it was last read: the request path reads this, and every save
# refreshes it, so a change on the page applies to the very next request
_snapshot = {"providers": []}


class CloudError(Exception):
    """What is wrong with what was asked for; shown to the admin as it is."""


def openai_base(url):
    """The OpenAI-format base URL for what was pasted. The gateway speaks the OpenAI format to every
    upstream; providers that also publish an Anthropic endpoint (…/apps/anthropic, as Qwen Cloud
    does) serve the OpenAI one beside it at …/compatible-mode/v1, so that address is used instead.
    A full endpoint pasted by mistake (…/chat/completions, …/models) is cut back to its base."""
    url = (url or "").strip().rstrip("/")
    for tail in ("/chat/completions", "/messages", "/models"):
        if url.endswith(tail):
            url = url[: -len(tail)]
    if "/apps/anthropic" in url:
        url = url.split("/apps/anthropic")[0] + "/compatible-mode/v1"
    return url


def _model(entry):
    if isinstance(entry, str):
        entry = {"id": entry}
    model_id = str(entry.get("id") or "").strip()
    if not MODEL_SHAPE.match(model_id):
        raise CloudError(f"{model_id or 'A model'} is not a model ID: letters, digits and . _ : / - only.")
    out = {"id": model_id, "vision": bool(entry.get("vision")), "tools": bool(entry.get("tools", True)),
           "thinking": bool(entry.get("thinking"))}
    context = entry.get("context")
    if context not in (None, "", 0):
        try:
            context = int(context)
        except (TypeError, ValueError):
            raise CloudError(f"The context of {model_id} must be a number of tokens.")
        if not 1024 <= context <= 10_000_000:
            raise CloudError(f"The context of {model_id} must be between 1024 and 10,000,000 tokens.")
        out["context"] = context
    return out


def clean(entry):
    """One provider, checked and normalised, or CloudError saying what is wrong with it."""
    name = str(entry.get("name") or "").strip().lower()
    if not NAME_SHAPE.match(name):
        raise CloudError("A short name is needed, used in front of each model: lower-case letters, "
                         "digits and dashes, up to 32 characters.")
    base_url = openai_base(entry.get("base_url"))
    parts = urllib.parse.urlparse(base_url)
    if parts.scheme != "https" or not parts.hostname:
        raise CloudError("The address must start with https://.")
    models, seen = [], set()
    for raw in entry.get("models") or []:
        model = _model(raw)
        if model["id"] not in seen:
            seen.add(model["id"])
            models.append(model)
    if len(models) > MOST_MODELS:
        raise CloudError(f"At most {MOST_MODELS} models per provider.")
    return {"name": name, "label": str(entry.get("label") or "").strip()[:60] or name,
            "base_url": base_url, "enabled": bool(entry.get("enabled", True)),
            "everyone": bool(entry.get("everyone", False)), "auto_pick": bool(entry.get("auto_pick", False)),
            "models": models}


def _key_of(row):
    if not row.key_sealed:
        return ""
    try:
        return secretbox.unseal(row.key_sealed)
    except ValueError:
        log.warning("The API key of cloud provider %s cannot be read (encryption key changed)", row.name)
        return ""


def _as_dict(row):
    try:
        models = json.loads(row.models or "[]")
    except ValueError:
        models = []
    return {"name": row.name, "label": row.label or row.name, "base_url": row.base_url,
            "enabled": bool(row.enabled), "everyone": bool(row.everyone), "auto_pick": bool(row.auto_pick),
            "models": [m for m in models if isinstance(m, dict) and m.get("id")], "key": _key_of(row)}


def _shown(provider):
    """A provider as the page sees it: whether a key is set, never the key."""
    out = {k: v for k, v in provider.items() if k != "key"}
    return {**out, "key_set": bool(provider.get("key")),
            "names": [f"{provider['name']}/{m['id']}" for m in provider.get("models") or []]}


async def _rows(session):
    return (await session.execute(select(CloudProvider).order_by(CloudProvider.name))).scalars().all()


async def refresh(session=None):
    """Reads the providers again into the snapshot the request path uses."""
    if session is None:
        async with session_factory()() as own:
            rows = await _rows(own)
    else:
        rows = await _rows(session)
    _snapshot["providers"] = [_as_dict(r) for r in rows]
    return _snapshot["providers"]


async def everything(session):
    return {"providers": [_shown(p) for p in await refresh(session)]}


async def save(session, entry, key=None, renaming_from=None):
    """Adds a provider, or changes the one called `renaming_from`. A blank key keeps the one set."""
    provider = clean(entry)
    key = (key or "").strip()
    if key and (len(key.splitlines()) != 1 or len(key) > 400):
        raise CloudError("An API key is a single line.")
    rows = {r.name: r for r in await _rows(session)}
    if provider["name"] in rows and provider["name"] != renaming_from:
        raise CloudError(f"There is already a provider called {provider['name']}.")
    if renaming_from is not None:
        row = rows.get(renaming_from)
        if row is None:
            raise CloudError(f"No provider called {renaming_from}.")
        if not key and not row.key_sealed:
            raise CloudError("Its API key is needed.")
    else:
        if not key:
            raise CloudError("Its API key is needed.")
        row = CloudProvider()
        session.add(row)
    row.name, row.label, row.base_url = provider["name"], provider["label"], provider["base_url"]
    row.enabled, row.everyone, row.auto_pick = provider["enabled"], provider["everyone"], provider["auto_pick"]
    row.models = json.dumps(provider["models"])
    if key:
        row.key_sealed = secretbox.seal(key)
    await session.commit()
    await refresh(session)
    return _shown(_as_dict(row))


async def remove(session, name):
    row = next((r for r in await _rows(session) if r.name == name), None)
    if row is None:
        raise CloudError(f"No provider called {name}.")
    await session.delete(row)
    await session.commit()
    await refresh(session)


async def saved_key(session, name):
    row = next((r for r in await _rows(session) if r.name == name), None)
    return _key_of(row) if row else ""


def backends():
    """name -> Backend for every model of every provider that is switched on and has a key."""
    from app.backends import Backend
    found = {}
    for provider in _snapshot["providers"]:
        if not (provider["enabled"] and provider["key"]):
            continue
        for model in provider["models"]:
            found[f"{provider['name']}/{model['id']}"] = Backend(
                name=provider["label"], kind=KIND, base_url=provider["base_url"],
                upstream_model=model["id"], api_key=provider["key"],
                capabilities={"completion"} | {c for c in ("vision", "tools", "thinking") if model.get(c)},
                context_length=model.get("context"),
                description=f"{model['id']} from {provider['label']}, a cloud API: "
                            "each request is sent there and paid for per token.")
    return found


def _provider_of(model):
    if "/" not in (model or ""):
        return None
    prefix = model.split("/", 1)[0]
    return next((p for p in _snapshot["providers"] if p["name"] == prefix), None)


def admins_only(model):
    """Whether a model is a cloud one whose provider is kept to admins."""
    provider = _provider_of(model)
    return provider is not None and not provider["everyone"]


def auto_may_pick(model):
    provider = _provider_of(model)
    return provider is None or provider["auto_pick"]


async def offered(base_url, key):
    """The model IDs the provider lists at /models, or CloudError saying why there are none."""
    base_url = openai_base(base_url)
    if not base_url.startswith("https://"):
        raise CloudError("The address must start with https://.")
    if not key:
        raise CloudError("Give its API key first.")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(base_url + "/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as e:
        raise CloudError(f"Could not reach {urllib.parse.urlparse(base_url).hostname}: {e}")
    if response.status_code in (401, 403):
        raise CloudError("The provider refused the key (HTTP %d). Check it is the right kind of key "
                         "for this address." % response.status_code)
    if not response.is_success:
        raise CloudError(f"The provider answered HTTP {response.status_code} for its model list; "
                         "type the model IDs in instead.")
    try:
        data = response.json().get("data") or []
    except (ValueError, AttributeError):
        raise CloudError("The provider's model list was not JSON; type the model IDs in instead.")
    return sorted({str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")})
