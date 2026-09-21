"""Whether a model from Hugging Face will fit on this machine, and at which quantization.

This is the gateway's original job, from before it was a gateway: you give it a repository name, it
reads what the model is, works out what each quantization would cost in memory once the KV cache and
runtime overhead are counted, and says which ones would run on the GPU, which would spill into system
RAM, and which will not run at all.

The arithmetic is unchanged from the model checker it came from. It is an estimate and says so: the
sizes are exact when the repository publishes GGUF files and calculated from the parameter count when
it does not.
"""
import re

# "bpw" is the approximate effective bits per weight of each llama.cpp GGUF quantization
QUANTIZATION_LEVELS = {
    "q2_k": {"name": "Q2_K", "bits": 2, "bpw": 3.0, "quality": "Low", "speed": "Very Fast",
             "description": "Maximum compression, lower quality"},
    "q3_k_m": {"name": "Q3_K_M", "bits": 3, "bpw": 3.9, "quality": "Low-Medium", "speed": "Fast",
               "description": "Good compression with decent quality"},
    "q4_0": {"name": "Q4_0", "bits": 4, "bpw": 4.5, "quality": "Medium", "speed": "Fast",
             "description": "Good balance of quality and performance"},
    "q4_1": {"name": "Q4_1", "bits": 4, "bpw": 5.0, "quality": "Medium", "speed": "Fast",
             "description": "Slightly better than Q4_0"},
    "q4_k_m": {"name": "Q4_K_M", "bits": 4, "bpw": 4.85, "quality": "Medium", "speed": "Fast",
               "description": "Most popular choice, best 4-bit quality"},
    "q5_0": {"name": "Q5_0", "bits": 5, "bpw": 5.5, "quality": "High", "speed": "Medium",
             "description": "Better quality with moderate memory"},
    "q5_1": {"name": "Q5_1", "bits": 5, "bpw": 6.0, "quality": "High", "speed": "Medium",
             "description": "Best quality for legacy 5-bit quantization"},
    "q5_k_m": {"name": "Q5_K_M", "bits": 5, "bpw": 5.7, "quality": "High", "speed": "Medium",
               "description": "High quality 5-bit with k-quant improvements"},
    "q6_k": {"name": "Q6_K", "bits": 6, "bpw": 6.6, "quality": "High", "speed": "Medium",
             "description": "High quality with good compression"},
    "q8_0": {"name": "Q8_0", "bits": 8, "bpw": 8.5, "quality": "Very High", "speed": "Slower",
             "description": "Near-original quality"},
}
DEFAULT_QUANTIZATION = "q4_k_m"
DEFAULT_CONTEXT_LENGTH = 4096
MIN_CONTEXT_LENGTH = 256
MAX_CONTEXT_LENGTH = 1048576
KV_CACHE_BYTES_PER_ELEMENT = 2   # Ollama keeps the KV cache in f16 by default
MEMORY_OVERHEAD = 1.1            # compute buffers and runtime overhead on top of the weights
MEMORY_HEADROOM = 0.9            # leaves some memory for the operating system and everything else
RUN_MODE_ORDER = {"GPU": 0, "GPU + CPU": 1, "CPU": 2}


def recommend(system_info, param_count, kv_cache_bytes, gguf_files):
    """Every quantization that would run here, best first, with one marked as the one to take."""
    total_vram = system_info["total_vram"]
    gpu_budget = total_vram * MEMORY_HEADROOM
    ram_budget = system_info["available_ram"] * MEMORY_HEADROOM
    recommendations = []

    for quant_name, quant_info in QUANTIZATION_LEVELS.items():
        # For a GGUF repository only the quantizations actually published can be pulled
        if gguf_files and quant_name not in gguf_files:
            continue
        estimated_size = gguf_files.get(quant_name) or int(param_count * quant_info["bpw"] / 8)
        if not estimated_size:
            continue
        memory_needed = estimated_size * MEMORY_OVERHEAD + kv_cache_bytes

        if total_vram and memory_needed <= gpu_budget:
            run_mode = "GPU"
        elif total_vram and memory_needed <= gpu_budget + ram_budget:
            run_mode = "GPU + CPU"   # Ollama offloads the layers that do not fit to system RAM
        elif not total_vram and memory_needed <= ram_budget:
            run_mode = "CPU"
        else:
            continue

        recommendations.append({
            "quantization": quant_name, "name": quant_info["name"], "bits": quant_info["bits"],
            "bpw": quant_info["bpw"], "quality": quant_info["quality"], "speed": quant_info["speed"],
            "description": quant_info["description"],
            "estimated_size_gb": round(estimated_size / (1024 ** 3), 2),
            "size_is_exact": quant_name in gguf_files,
            "memory_needed_gb": round(memory_needed / (1024 ** 3), 2),
            "vram_usage_percent": round(memory_needed / total_vram * 100, 1) if total_vram else None,
            "run_mode": run_mode, "available": quant_name in gguf_files, "recommended": False,
        })

    if recommendations:
        best_mode = min(RUN_MODE_ORDER[r["run_mode"]] for r in recommendations)
        candidates = [r for r in recommendations if RUN_MODE_ORDER[r["run_mode"]] == best_mode]
        if best_mode > 0:
            # When the model will not fit on the GPU alone, speed matters more than the last of the quality
            candidates = [r for r in candidates if r["bits"] <= 4] or candidates
        max(candidates, key=lambda r: r["bpw"])["recommended"] = True

    recommendations.sort(key=lambda r: (not r["recommended"], RUN_MODE_ORDER[r["run_mode"]], -r["bpw"]))
    return recommendations


def kv_cache(model_architecture):
    """What the key/value cache costs per token, and for a context worth using."""
    num_layers = model_architecture.get("num_hidden_layers") or 32
    num_heads = model_architecture.get("num_attention_heads") or 32
    hidden_size = model_architecture.get("hidden_size") or 4096
    # Grouped-query attention models cache fewer key/value heads than attention heads
    num_kv_heads = model_architecture.get("num_key_value_heads") or num_heads
    head_dim = model_architecture.get("head_dim") or hidden_size // num_heads
    max_positions = model_architecture.get("max_position_embeddings") or 2048
    recommended_context = min(max_positions, DEFAULT_CONTEXT_LENGTH)

    # One key and one value vector per layer for every token
    per_token_bytes = 2 * num_layers * num_kv_heads * head_dim * KV_CACHE_BYTES_PER_ELEMENT
    return {
        "kv_cache_per_token_bytes": per_token_bytes,
        "kv_cache_per_token_mb": round(per_token_bytes / (1024 ** 2), 4),
        "kv_cache_total_gb": round(per_token_bytes * max_positions / (1024 ** 3), 2),
        "kv_cache_recommended_bytes": per_token_bytes * recommended_context,
        "kv_cache_recommended_gb": round(per_token_bytes * recommended_context / (1024 ** 3), 2),
        "recommended_context_length": recommended_context,
        "max_context_length": max_positions,
        "estimated": not (model_architecture.get("num_hidden_layers")
                          and model_architecture.get("num_attention_heads")),
    }


def hf_reference(model_id, quantization):
    """What Ollama calls this quantization of this repository."""
    return f"hf.co/{model_id}:{QUANTIZATION_LEVELS[quantization]['name']}"


def ollama_name(model_id, quantization):
    """A short local name for a model created from a repository, e.g. "mistral-7b:q4_k_m"."""
    repo = model_id.split("/")[-1].lower()
    repo = re.sub(r"[-_.]gguf$", "", repo)
    repo = re.sub(r"[^a-z0-9_.-]", "-", repo).strip("-.")
    return f"{repo or 'model'}:{quantization}"


def clamp_context(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_CONTEXT_LENGTH
    return max(MIN_CONTEXT_LENGTH, min(value, MAX_CONTEXT_LENGTH))
