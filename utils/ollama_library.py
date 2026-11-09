"""Ollama's own library (ollama.com): what a model is, which tags it has, and how big each one is.

Hugging Face describes a repository through one API; Ollama's library comes in two halves. The
registry (registry.ollama.ai) serves each tag's manifest - the model file, a vision projector, its
parameters, each with its exact size - and a small config naming the family and the quantization.
It lists no tags (its tags/list answers 404), so the list is read from the model's tags page on
ollama.com, where every tag is a link. The architecture the key/value cache is sized from (layers,
attention heads) is in the model file's own GGUF header, of which only the first megabytes are read.
"""
import concurrent.futures
import logging
import math
import re

import requests

from app.engine import gguf_meta

log = logging.getLogger("ollama_library")

REGISTRY = "https://registry.ollama.ai/v2"
SITE = "https://ollama.com"
TIMEOUT = 15
MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
MODEL_LAYER = "application/vnd.ollama.image.model"
PROJECTOR_LAYER = "application/vnd.ollama.image.projector"
# What a manifest carries besides the weights: none of it is loaded into memory to any extent
SMALL_LAYERS = ("application/vnd.ollama.image.params", "application/vnd.ollama.image.template",
                "application/vnd.ollama.image.license", "application/vnd.ollama.image.system",
                "application/vnd.ollama.image.messages")
HEADER_BYTES = 4 * 1024 ** 2   # the architecture's keys come long before the vocabulary
MOST_TAGS = 40                 # a model with more tags than this is checked by the ones asked for

PART = r"[a-z0-9][a-z0-9._-]*"
NAME = re.compile(rf"^(?:(?P<namespace>{PART})/)?(?P<model>{PART})(?::(?P<tag>[a-z0-9][\w.-]*))?$", re.I)


def parse(text):
    """(namespace, model, tag or None) from what a person typed or pasted, or None.

    Takes "ornith-1.5:122b", "someone/model:tag", the model's address on ollama.com, and the
    "ollama/" or "library/" a name is sometimes written with."""
    text = (text or "").strip()
    text = re.sub(r"^(https?://)?(www\.)?(ollama\.com|registry\.ollama\.ai)/", "", text, flags=re.I)
    text = re.sub(r"/tags/?$", "", text)
    text = re.sub(r"^(ollama|library)/", "", text, flags=re.I)
    found = NAME.match(text)
    if not found:
        return None
    return (found.group("namespace") or "library").lower(), found.group("model").lower(), found.group("tag")


def display(namespace, model, tag=None):
    """The name Ollama pulls it by: no namespace for the library's own models."""
    name = model if namespace == "library" else f"{namespace}/{model}"
    return f"{name}:{tag}" if tag else name


def page(namespace, model):
    return f"{SITE}/{namespace}/{model}"


def tags(namespace, model):
    """Every tag ollama.com lists for the model, in its order; None when it has no such model."""
    response = requests.get(f"{page(namespace, model)}/tags", timeout=TIMEOUT)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    link = re.compile(rf'href="/{re.escape(namespace)}/{re.escape(model)}:([\w.-]+)"', re.I)
    return list(dict.fromkeys(link.findall(response.text)))


def manifest(namespace, model, tag):
    """The tag's manifest, or None when there is no such tag."""
    response = requests.get(f"{REGISTRY}/{namespace}/{model}/manifests/{tag}",
                            headers={"Accept": MANIFEST}, timeout=TIMEOUT)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _blob(namespace, model, digest, **kwargs):
    return requests.get(f"{REGISTRY}/{namespace}/{model}/blobs/{digest}", timeout=TIMEOUT, **kwargs)


def config(namespace, model, digest):
    """{"model_format", "model_family", "model_type", "file_type"}, or {} if it cannot be read."""
    try:
        response = _blob(namespace, model, digest)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return {}


def header(namespace, model, digest):
    """The keys at the start of the model file's GGUF header; {} if they cannot be read."""
    try:
        response = _blob(namespace, model, digest, headers={"Range": f"bytes=0-{HEADER_BYTES - 1}"})
        response.raise_for_status()
        return gguf_meta._parse(response.content[:HEADER_BYTES], partial=True)
    except (requests.RequestException, ValueError) as e:
        log.info("Could not read the header of %s/%s: %s", namespace, model, e)
        return {}


def architecture(keys):
    """What app.fitting.kv_cache needs, from GGUF keys. A hybrid model keeps a key/value cache only
    in its full-attention layers - one in every `full_attention_interval` - so only those count."""
    arch = keys.get("general.architecture")
    if not arch:
        return {}

    def key(name):
        value = keys.get(f"{arch}.{name}")
        # Kept per layer by some models; the largest is what the cache has to hold
        return max(value) if isinstance(value, list) and value and all(isinstance(v, (int, float)) for v in value) else value

    layers = key("block_count")
    interval = key("full_attention_interval")
    if layers and isinstance(interval, int) and interval > 1:
        layers = math.ceil(layers / interval)
    heads = key("attention.head_count")
    width = key("embedding_length")
    found = {"num_hidden_layers": layers, "num_attention_heads": heads, "num_key_value_heads": key("attention.head_count_kv"),
             "head_dim": key("attention.key_length") or (width // heads if width and heads else None),
             "hidden_size": width, "max_position_embeddings": key("context_length")}
    return {k: v for k, v in found.items() if isinstance(v, int) and v > 0}


def look_up(text, wanted=None):
    """Everything the library says about a model: its tags with their sizes, family, quantization
    and format, and the architecture read from one model file's header. {"error", "status_code"}
    when there is nothing to say. Blocking."""
    parsed = parse(text)
    if not parsed:
        return {"error": "That does not look like an Ollama model name (model, model:tag or owner/model:tag)",
                "status_code": 400}
    namespace, model, tag = parsed
    try:
        listed = tags(namespace, model)
    except requests.RequestException as e:
        return {"error": f"Could not reach ollama.com: {e}", "status_code": 503}
    if listed is None:
        return {"error": f"Ollama's library has no model called {display(namespace, model)}", "status_code": 404}
    if tag:
        # The tag asked for and its variants: 122b, 122b-q8_0, 122b-nvfp4...
        chosen = [t for t in listed if t == tag or t.startswith(tag + "-")] or [tag]
        chosen.sort(key=lambda t: t != tag)
    else:
        chosen = listed[:MOST_TAGS] or ["latest"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        manifests = dict(zip(chosen, pool.map(lambda t: _quietly(manifest, namespace, model, t), chosen)))
        configs = dict(zip(chosen, pool.map(
            lambda t: config(namespace, model, manifests[t]["config"]["digest"]) if manifests.get(t) else {}, chosen)))
    rows = []
    for t in chosen:
        found = manifests.get(t)
        if not found:
            continue
        layers = found.get("layers") or []
        weights = next((l for l in layers if l.get("mediaType") == MODEL_LAYER), None)
        about = configs.get(t) or {}
        size = sum(l.get("size") or 0 for l in layers if l.get("mediaType") == MODEL_LAYER)
        if not size:
            # Not one GGUF file (safetensors, say, as NVFP4 is published): its weights are the rest
            size = sum(l.get("size") or 0 for l in layers
                       if l.get("mediaType") not in SMALL_LAYERS and l.get("mediaType") != PROJECTOR_LAYER)
        rows.append({
            "tag": t, "name": display(namespace, model, t),
            "size": size,
            "projector": sum(l.get("size") or 0 for l in layers if l.get("mediaType") == PROJECTOR_LAYER),
            "quantization": about.get("file_type") or "", "format": about.get("model_format") or "",
            "parameters": about.get("model_type") or "", "family": about.get("model_family") or "",
            "digest": weights.get("digest") if weights else None,
        })
    if not rows:
        return {"error": f"{display(namespace, model, tag)} is not in Ollama's library", "status_code": 404}
    # One header for each size: 122b and 122b-q8_0 are one model quantized two ways, while 9b and
    # 397b are different models with different caches. A variant that names no size ("latest",
    # "q8_0") is the model's default size, and takes the first header there is.
    sizes = {}
    for r in rows:
        if r["digest"] and r["format"] in ("gguf", ""):
            sizes.setdefault(_size_of(r["tag"]), r["digest"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        headers = dict(zip(sizes, pool.map(lambda d: header(namespace, model, d), sizes.values())))
    default = next((h for h in headers.values() if h), {})
    for r in rows:
        keys = headers.get(_size_of(r["tag"])) or default
        r["architecture"] = architecture(keys)
        r["title"] = keys.get("general.name") or ""
    first = rows[0]
    return {
        "name": display(namespace, model, tag), "model": display(namespace, model), "page": page(namespace, model),
        "family": first["family"], "parameters": first["parameters"], "title": first["title"],
        "architecture": first["architecture"], "tags": rows,
    }


def _size_of(tag):
    """The size a tag names - "122b" of "122b-q8_0" - or "" for one that names none."""
    found = re.match(r"(\d+(?:\.\d+)?[bmk])\b", tag or "", re.I)
    return found.group(1).lower() if found else ""


def _quietly(call, *args):
    try:
        return call(*args)
    except requests.RequestException as e:
        log.info("%s%s failed: %s", call.__name__, args, e)
        return None
