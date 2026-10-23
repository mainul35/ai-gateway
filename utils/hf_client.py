import os
import re
import requests
from utils import config

HF_API_BASE = "https://huggingface.co/api"
HF_BASE = "https://huggingface.co"
TIMEOUT = 15

MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][\w.-]*(/[\w.-]+)?$")
WEIGHT_EXTENSIONS = (".safetensors", ".bin", ".pt", ".pth")

# Different model families name the same config fields differently (e.g. GPT-2 uses n_embd).
CONFIG_ALIASES = {
    "hidden_size": ["hidden_size", "n_embd", "d_model"],
    "num_hidden_layers": ["num_hidden_layers", "n_layer", "num_layers"],
    "num_attention_heads": ["num_attention_heads", "n_head", "num_heads"],
    "num_key_value_heads": ["num_key_value_heads", "num_kv_heads", "multi_query_group_num"],
    "head_dim": ["head_dim"],
    "vocab_size": ["vocab_size"],
    "max_position_embeddings": ["max_position_embeddings", "n_positions", "max_seq_len", "seq_length"],
}


def is_valid_model_id(model_id):
    return bool(model_id) and ".." not in model_id and bool(MODEL_ID_PATTERN.match(model_id))


def _headers():
    token = config.get("hf.token", "HF_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def get_model_info(model_id):
    if not is_valid_model_id(model_id):
        return {"error": "Invalid model ID. Expected format: owner/model-name", "status_code": 400}
    try:
        # blobs=true is required for the API to include file sizes in "siblings"
        response = requests.get(
            f"{HF_API_BASE}/models/{model_id}",
            params={"blobs": "true"},
            headers=_headers(),
            timeout=TIMEOUT,
        )
        # HuggingFace answers 401 for both missing repos and private repos without a token
        if response.status_code in (401, 404):
            return {"error": "Model not found (or it is private and no HuggingFace token is configured)", "status_code": 404}
        response.raise_for_status()
        data = response.json()
        return {
            "id": data.get("id", model_id),
            "author": data.get("author") or model_id.split("/")[0],
            "pipeline_tag": data.get("pipeline_tag") or "",
            "tags": data.get("tags", []),
            "likes": data.get("likes") or 0,
            "downloads": data.get("downloads") or 0,
            "gated": data.get("gated") or False,
            "siblings": data.get("siblings", []),
            "cardData": data.get("cardData") or {},
            "safetensors": data.get("safetensors") or {},
            "gguf": data.get("gguf") or {},
            "config": data.get("config") or {},
            "createdAt": data.get("createdAt", ""),
        }
    except requests.exceptions.RequestException as e:
        return {"error": f"Failed to reach HuggingFace: {e}", "status_code": 503}


def search_gguf(model_id, limit=6):
    """Repositories publishing GGUF conversions of this model.

    Ollama can only install GGUF, so a repository that publishes only safetensors is a dead end
    however well it would fit. Somebody has usually converted it already, and this is how they are
    found: the same name, filtered to GGUF, most downloaded first. Only those with the model's
    weights: a repository tagged GGUF may hold just the image reader (mmproj), the weights never
    uploaded, and there is nothing in it to run.
    """
    name = model_id.split("/")[-1]
    try:
        response = requests.get(f"{HF_API_BASE}/models", headers=_headers(), timeout=TIMEOUT,
                                params={"search": name, "filter": "gguf", "sort": "downloads",
                                        "direction": -1, "limit": limit * 2 + 1,
                                        "expand[]": ["siblings", "downloads", "likes"]})
        response.raise_for_status()
        found = response.json()
    except (requests.RequestException, ValueError):
        return []

    def has_weights(m):
        names = [(s.get("rfilename") or "").lower() for s in m.get("siblings") or () if isinstance(s, dict)]
        return any(n.endswith(".gguf") and "mmproj" not in n for n in names)

    return [{"id": m["id"], "downloads": m.get("downloads") or 0, "likes": m.get("likes") or 0}
            for m in found if isinstance(m, dict) and m.get("id") and m["id"] != model_id
            and has_weights(m)][:limit]


def public_model_info(info):
    """Model info without the bulky fields that are only needed server-side."""
    return {k: v for k, v in info.items() if k not in ("siblings", "cardData", "safetensors", "gguf", "config")}


def get_model_sizes(info):
    files = [
        {"path": s["rfilename"], "size": s.get("size") or 0}
        for s in info.get("siblings", [])
        if s.get("rfilename")
    ]
    safetensors_size = sum(f["size"] for f in files if f["path"].endswith(".safetensors"))
    # Repos often ship both .safetensors and .bin copies of the same weights; count only one set
    weights_size = safetensors_size or sum(f["size"] for f in files if f["path"].endswith(WEIGHT_EXTENSIONS))
    return {
        "files": files,
        "total_size": sum(f["size"] for f in files),
        "weights_size": weights_size,
        "file_count": len(files),
    }


def get_parameter_count(info, model_sizes):
    """Returns (parameter_count, source)."""
    if info.get("safetensors", {}).get("total"):
        return info["safetensors"]["total"], "safetensors metadata"
    if info.get("gguf", {}).get("total"):
        return info["gguf"]["total"], "gguf metadata"
    if model_sizes.get("weights_size"):
        # Assume 16-bit weights (2 bytes per parameter) when no metadata is available
        return model_sizes["weights_size"] // 2, "estimated from file size"
    return 0, "unknown"


# A GGUF whose name names none of the standard quantizations - NVIDIA's "...-24GB.gguf", Unsloth's
# "UD-Q4_K_XL", ISTA's "IQ1_M" - is still a model: it is listed under a key of its own, "file:<name>"
FILE_KEY = "file:"
_SHARD = re.compile(r"-\d{5}-of-\d{5}(?=\.gguf$)", re.I)
_QUANT_LABEL = re.compile(r"(UD-)?([IT]?Q\d(_[A-Z0-9]+)*|BF16|F16|F32|(?:MX|NV)FP4(?:_[A-Z0-9]+)*)(?=[.\-/]|$)", re.I)
# Helpers published beside a model, not models: speculative-decoding drafts and correctors
_HELPER = re.compile(r"(^|/)(mtp|draft|lilicorr)[-_]", re.I)


def gguf_files_detailed(info, quant_keys):
    """Each model file (or set of shards) the repo publishes: {key: {"size", "files", "label"}}.

    The standard quantizations are keyed by their QUANTIZATION_LEVELS key, everything else by
    FILE_KEY + its name without the shard suffix, so each is its own row instead of being ignored: a
    repository of only such files used to be reported as having no GGUF at all. "label" is the
    quantization its name shows (UD-Q4_K_XL, IQ1_M), or "" when it shows none. Image readers (mmproj)
    are not models and are left out.
    """
    found = {}
    for sibling in info.get("siblings", []):
        original = sibling.get("rfilename") or ""
        path = original.lower()
        if not path.endswith(".gguf") or "mmproj" in path or _HELPER.search(original):
            continue
        key = next((k for k in quant_keys
                    # The key must not continue with "_" so q4_0 doesn't match Q4_0_4_4 and q6_k doesn't match Q6_K_L
                    if re.search(rf"(^|[^a-z0-9]){re.escape(k)}(?=[.\-/]|$)", path)), None)
        label = ""
        if key is None:
            group = _SHARD.sub("", original)
            key = FILE_KEY + group
            shown = _QUANT_LABEL.findall(os.path.basename(group)[:-5])
            label = "".join(shown[-1][:2]).upper() if shown else ""
        # Split GGUF files (model-00001-of-00002.gguf) are summed together
        entry = found.setdefault(key, {"size": 0, "files": [], "label": label})
        entry["size"] += sibling.get("size") or 0
        entry["files"].append(original)
    for entry in found.values():
        entry["files"].sort()
    return found


def mmproj_files(info):
    """The repository's image readers, preferring F16 (what llama.cpp is usually given) to BF16 and F32."""
    files = [s.get("rfilename") for s in info.get("siblings", [])
             if (s.get("rfilename") or "").lower().endswith(".gguf") and "mmproj" in (s.get("rfilename") or "").lower()]
    rank = lambda p: (0 if "f16" in p.lower() and "bf16" not in p.lower() else 1 if "bf16" in p.lower() else 2, p)
    return sorted(files, key=rank)


def get_gguf_files(info, quant_keys):
    """Maps each quantization key to the total size of the matching GGUF file(s) in the repo."""
    return {key: entry["size"] for key, entry in gguf_files_detailed(info, quant_keys).items()}


def get_model_config(model_id):
    try:
        response = requests.get(
            f"{HF_BASE}/{model_id}/resolve/main/config.json",
            headers=_headers(),
            timeout=TIMEOUT,
        )
        if response.status_code in (401, 403, 404):
            return {"error": "Config not available"}
        response.raise_for_status()
        return response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        return {"error": str(e)}


def _base_model_id(info):
    base = info.get("cardData", {}).get("base_model")
    if isinstance(base, list):
        base = base[0] if base else None
    return base if isinstance(base, str) and is_valid_model_id(base) else None


def get_model_architecture(model_id, info):
    config = get_model_config(model_id)
    source = model_id
    # GGUF / quantized repos rarely ship config.json; fall back to the base model's config
    if "error" in config:
        base = _base_model_id(info)
        config = get_model_config(base) if base else config
        source = base
    if "error" in config:
        config, source = {}, None

    # Multimodal models keep the language model settings in text_config
    text_config = config.get("text_config") or {}

    def lookup(field):
        for name in CONFIG_ALIASES[field]:
            for cfg in (text_config, config):
                value = cfg.get(name)
                if isinstance(value, int) and value > 0:
                    return value
        return 0

    gguf = info.get("gguf", {})
    # The Hub API exposes a small config summary even for gated repos whose config.json needs a token
    hub_config = info.get("config", {})
    return {
        "architectures": config.get("architectures") or hub_config.get("architectures") or [],
        "model_type": config.get("model_type") or hub_config.get("model_type") or gguf.get("architecture") or "",
        "hidden_size": lookup("hidden_size"),
        "num_attention_heads": lookup("num_attention_heads"),
        "num_key_value_heads": lookup("num_key_value_heads"),
        "head_dim": lookup("head_dim"),
        "num_hidden_layers": lookup("num_hidden_layers"),
        "vocab_size": lookup("vocab_size"),
        "max_position_embeddings": lookup("max_position_embeddings") or gguf.get("context_length") or 0,
        "dtype": config.get("torch_dtype") or text_config.get("torch_dtype") or "",
        "config_source": source,
    }
