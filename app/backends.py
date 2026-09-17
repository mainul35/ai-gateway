"""Model routing: which upstream serves a given model name.

Models come from two places:
  * automatic discovery of every model on the configured Ollama server
  * config/models.yaml, for explicit entries and for overrides of discovered ones
"""
import os
import time

import httpx
import yaml

from app import settings
from app.engine.profiles import load_profiles
from utils.ollama_client import ollama_host

_cache = {"expires_at": 0.0, "models": {}}


class Backend:
    """An upstream that speaks the OpenAI API."""

    def __init__(self, name, kind, base_url, upstream_model, api_key=None):
        self.name = name
        self.kind = kind  # "ollama" or "openai"
        self.base_url = base_url.rstrip("/")
        self.upstream_model = upstream_model
        self.api_key = api_key

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
            discovered[name] = Backend(name="ollama", kind="ollama", base_url=f"{host}/v1", upstream_model=name)
    return discovered


def _engine_models():
    """Models served by our own llama-server processes; started on demand when first requested."""
    return {
        name: Backend(name="llamacpp", kind="llamacpp",
                      base_url=f"http://127.0.0.1:{profile.port}/v1", upstream_model=name)
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
