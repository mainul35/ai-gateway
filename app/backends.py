"""Model routing: which upstream serves a given model name.

Models come from two places:
  * automatic discovery of every model on the configured Ollama server
  * config/models.yaml, for explicit entries and for overrides of discovered ones
"""
import asyncio
import os
import time

import httpx
import yaml

from app import settings
from app.engine.profiles import load_profiles
from utils.ollama_client import ollama_host

_cache = {"expires_at": 0.0, "models": {}}
# Ollama model digest -> what /api/show said about it; a digest never changes what it is
_capabilities_by_digest = {}
_context_by_digest = {}


class Backend:
    """An upstream that speaks the OpenAI API."""

    def __init__(self, name, kind, base_url, upstream_model, api_key=None, capabilities=(),
                 size_bytes=0, details=None, context_length=None, modified_at=None, description=""):
        self.name = name
        self.kind = kind  # "ollama", "llamacpp" or "openai"
        self.base_url = base_url.rstrip("/")
        self.upstream_model = upstream_model
        self.api_key = api_key
        # What the model can take or do beyond text, e.g. "vision"; shown to clients in /v1/models
        self.capabilities = set(capabilities)
        # Only the admin model list reads the rest: what it is, how big, and how much it remembers
        self.size_bytes = size_bytes
        self.details = details or {}
        self.context_length = context_length
        self.modified_at = modified_at
        self.description = description

    def url(self, path):
        return f"{self.base_url}{path}"

    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


def _load_model_file():
    path = settings.models_file()
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}

    models = {}
    for entry in data.get("models") or []:
        name = entry.get("name")
        if not name:
            continue
        kind = entry.get("backend", "openai")
        base_url = entry.get("base_url") or (ollama_host() + "/v1")
        api_key = entry.get("api_key")
        if api_key and api_key.startswith("env:"):
            api_key = os.getenv(api_key[4:], "")
        models[name] = Backend(
            name=entry.get("id", name),
            kind=kind,
            base_url=base_url,
            upstream_model=entry.get("upstream_model", name),
            api_key=api_key,
            capabilities=entry.get("capabilities") or (),
            description=entry.get("description") or "",
        )
    return models


async def _discover_ollama():
    host = ollama_host()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{host}/api/tags")
            response.raise_for_status()
            tags = response.json().get("models") or []
    except (httpx.HTTPError, ValueError):
        return {}

    discovered = {}
    for tag in tags:
        name = tag.get("name")
        if name:
            # Ollama exposes an OpenAI-compatible API at /v1
            discovered[name] = Backend(name="ollama", kind="ollama", base_url=f"{host}/v1", upstream_model=name,
                                       size_bytes=tag.get("size") or 0, details=tag.get("details"),
                                       modified_at=tag.get("modified_at"))
    await _add_ollama_capabilities(host, tags, discovered)
    return discovered


async def _add_ollama_capabilities(host, tags, discovered):
    """Asks Ollama what each model supports (vision, tools, thinking); cached per digest."""
    unknown = [t for t in tags if t.get("name") and t.get("digest") not in _capabilities_by_digest]
    if unknown:
        async with httpx.AsyncClient(timeout=10) as client:
            async def show(tag):
                try:
                    response = await client.post(f"{host}/api/show", json={"model": tag["name"]})
                    response.raise_for_status()
                    shown = response.json()
                    _capabilities_by_digest[tag.get("digest")] = set(shown.get("capabilities") or ())
                    # Every family names its own window: qwen3.context_length, llama.context_length
                    info = shown.get("model_info") or {}
                    _context_by_digest[tag.get("digest")] = next(
                        (value for key, value in info.items() if key.endswith(".context_length")), None)
                except (httpx.HTTPError, ValueError):
                    pass  # tried again at the next discovery
            await asyncio.gather(*(show(t) for t in unknown))
    for tag in tags:
        if tag.get("name") in discovered:
            discovered[tag["name"]].capabilities = set(_capabilities_by_digest.get(tag.get("digest"), ()))
            discovered[tag["name"]].context_length = _context_by_digest.get(tag.get("digest"))


def _engine_models():
    """Models served by our own llama-server processes; started on demand when first requested."""
    return {
        name: Backend(name="llamacpp", kind="llamacpp",
                      base_url=f"http://127.0.0.1:{profile.port}/v1", upstream_model=name,
                      # llama-server only sees images when it is given the model's vision projector
                      capabilities={"completion", "thinking"} | ({"vision"} if profile.mmproj else set()))
        for name, profile in load_profiles().items()
    }


async def available_models(force_refresh=False):
    now = time.monotonic()
    if not force_refresh and now < _cache["expires_at"]:
        return _cache["models"]

    models = await _discover_ollama()
    models.update(_engine_models())  # our tuned llama.cpp profiles win over the Ollama copy
    models.update(_load_model_file())  # explicit config wins over discovery
    _cache.update(expires_at=now + settings.discovery_ttl(), models=models)
    return models


async def resolve(model_name):
    models = await available_models()
    if model_name in models:
        return models[model_name]
    # A model may have been pulled since the last discovery
    models = await available_models(force_refresh=True)
    return models.get(model_name)


async def delete_ollama_model(name):
    """Removes a model from the Ollama server. Returns None, or why it could not be done.

    Ollama deletes the manifest and any blob no other model still points at, so the space a shared
    base costs is only returned when the last model using it goes.
    """
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request("DELETE", f"{ollama_host()}/api/delete", json={"model": name})
        if response.status_code == 404:
            return f"Ollama does not have a model called {name}"
        response.raise_for_status()
    except httpx.HTTPError as e:
        return f"Ollama would not delete it: {e}"
    _cache["expires_at"] = 0.0   # the next listing asks Ollama again rather than trusting the cache
    return None
