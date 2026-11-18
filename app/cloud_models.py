"""Models a cloud provider serves - Qwen Cloud, or any other OpenAI-compatible API - set up from the
Models page.

The providers are kept in config/cloud.yaml, which is not in the repository and is not shipped by a
deploy, so what is set up on the server stays there. The API key never goes in that file: it is kept
in config/config.properties as cloud.<name>.api_key, like every other secret, and is never sent back
to the browser.

Each model is offered as <provider>/<model id> - qwen/qwen3.8-max - so it can never be mistaken for
a model on this machine, and goes through the gateway like any other: access, limits and usage
accounting apply, and Claude Code's requests are translated to the OpenAI format on the way
(app/anthropic_compat.py).

A provider's models are for admins only until "everyone" is ticked: they cost money per token, and
a plain user whose access is "all models" should not start spending it unawares.
"""
import contextlib
import os
import re
import shutil
import tempfile
import threading
import urllib.parse

import httpx
import yaml

from app import settings
from app.config_writer import remove_key, write_secret
from utils import config

KIND = "cloud"
HEADER = """# Cloud model providers, set up on the Models page. Not in the repository and not shipped by a
# deploy. API keys are not here: they are cloud.<name>.api_key in config/config.properties.

"""
NAME_SHAPE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MODEL_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
MOST_MODELS = 100

# Ready-made settings for providers people ask for, which the page offers to fill in. Qwen Cloud's
# Token Plan answers in the OpenAI format at /compatible-mode/v1, beside its Anthropic one.
PRESETS = [
    {"name": "qwen", "label": "Qwen Cloud (Token Plan)",
     "base_url": "https://token-plan.maas.qwencloudapi.com/compatible-mode/v1",
     "key_hint": "a Token Plan key, starting sk-sp-"},
    {"name": "qwen-payg", "label": "Qwen Cloud (pay-as-you-go, Singapore)",
     "base_url": "https://ap-southeast-1.maas.qwencloudapi.com/compatible-mode/v1",
     "key_hint": "a pay-as-you-go key, starting sk-"},
]

_lock = threading.Lock()
_cache = {"mtime": None, "providers": []}


class CloudError(Exception):
    """What is wrong with what was asked for; shown to the admin as it is."""


def path():
    where = settings.get("cloud.providers.file", "CLOUD_PROVIDERS_FILE") or "config/cloud.yaml"
    return where if os.path.isabs(where) else os.path.join(settings.PROJECT_ROOT, where)


def key_name(name):
    return f"cloud.{name}.api_key"


def api_key(name):
    return config.get(key_name(name)) or ""


def openai_base(url):
    """The OpenAI-format base URL for what was pasted. Qwen Cloud and others publish an Anthropic
    endpoint (…/apps/anthropic) beside the OpenAI one (…/compatible-mode/v1); the gateway speaks
    the OpenAI format to every upstream, so the Anthropic address is turned into its twin."""
    url = (url or "").strip().rstrip("/")
    for tail in ("/chat/completions", "/messages", "/models"):
        if url.endswith(tail):
            url = url[: -len(tail)]
    if "/apps/anthropic" in url:
        url = url.split("/apps/anthropic")[0] + "/compatible-mode/v1"
    return url


def _read():
    target = path()
    try:
        mtime = os.stat(target).st_mtime
    except OSError:
        return []
    with _lock:
        if _cache["mtime"] != mtime:
            try:
                with open(target, encoding="utf-8") as handle:
                    loaded = yaml.safe_load(handle) or {}
            except (OSError, yaml.YAMLError):
                loaded = {}
            found = loaded.get("providers") if isinstance(loaded, dict) else None
            _cache.update(mtime=mtime, providers=found if isinstance(found, list) else [])
        return [dict(p) for p in _cache["providers"] if isinstance(p, dict)]


def _write(providers):
    """The whole file at once, through a temporary file so a crash cannot leave half of it."""
    body = yaml.safe_dump({"providers": providers}, sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=100)
    target = path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                         dir=os.path.dirname(target), prefix=".cloud-", suffix=".yaml")
    try:
        handle.write(HEADER + body)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        shutil.move(handle.name, target)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise
    with _lock:
        _cache["mtime"] = None


def _model(entry):
    if isinstance(entry, str):
        entry = {"id": entry}
    model_id = str(entry.get("id") or "").strip()
    if not MODEL_SHAPE.match(model_id):
        raise CloudError(f"{model_id or 'A model'} is not a model ID: letters, digits and . _ : / - only.")
    out = {"id": model_id, "vision": bool(entry.get("vision"))}
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
            "everyone": bool(entry.get("everyone", False)), "models": models}


def shown(provider):
    """A provider as the page sees it: whether a key is set, never the key."""
    return {**provider, "key_set": bool(api_key(provider["name"])),
            "names": [f"{provider['name']}/{m['id']}" for m in provider.get("models") or []]}


def everything():
    return {"file": path(), "presets": PRESETS, "providers": [shown(p) for p in _read()]}


def save(entry, key=None, renaming_from=None):
    """Adds a provider, or replaces the one called `renaming_from`. A blank key keeps the one set."""
    provider = clean(entry)
    providers = _read()
    names = [p.get("name") for p in providers]
    if provider["name"] in names and provider["name"] != renaming_from:
        raise CloudError(f"There is already a provider called {provider['name']}.")
    key = (key or "").strip()
    if key and (len(key.splitlines()) != 1 or len(key) > 400):
        raise CloudError("An API key is a single line.")
    if renaming_from is not None:
        if renaming_from not in names:
            raise CloudError(f"No provider called {renaming_from}.")
        providers[names.index(renaming_from)] = provider
    else:
        providers.append(provider)
    if not key and not api_key(provider["name"]) and not (renaming_from and api_key(renaming_from)):
        raise CloudError("Its API key is needed.")
    _write(providers)
    if renaming_from and renaming_from != provider["name"]:
        old = api_key(renaming_from)
        if old and not key:
            write_secret(key_name(provider["name"]), old)
        remove_key(key_name(renaming_from))
    if key:
        write_secret(key_name(provider["name"]), key)
    return shown(provider)


def remove(name):
    providers = _read()
    kept = [p for p in providers if p.get("name") != name]
    if len(kept) == len(providers):
        raise CloudError(f"No provider called {name}.")
    _write(kept)
    remove_key(key_name(name))


def backends():
    """name -> Backend for every model of every provider that is switched on and has a key."""
    from app.backends import Backend
    found = {}
    for provider in _read():
        name = provider.get("name")
        key = api_key(name) if name else ""
        if not (provider.get("enabled", True) and key and provider.get("base_url")):
            continue
        for model in provider.get("models") or []:
            if not isinstance(model, dict) or not model.get("id"):
                continue
            found[f"{name}/{model['id']}"] = Backend(
                name=provider.get("label") or name, kind=KIND, base_url=provider["base_url"],
                upstream_model=model["id"], api_key=key,
                capabilities={"completion", "tools"} | ({"vision"} if model.get("vision") else set()),
                context_length=model.get("context"),
                description=f"{model['id']} from {provider.get('label') or name}, a cloud API: "
                            "each request is sent there and paid for per token.")
    return found


def admins_only(model):
    """Whether a model is a cloud one its provider keeps to admins."""
    if "/" not in (model or ""):
        return False
    prefix = model.split("/", 1)[0]
    return any(p.get("name") == prefix and not p.get("everyone") for p in _read())


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
