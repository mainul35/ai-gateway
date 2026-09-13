from flask import Flask, Response, render_template, request, jsonify
import json
import psutil
import os
import re
import subprocess
import sys
from utils.ollama_client import check_ollama_status, list_models, stream_create_model, delete_model, stream_pull_model
from utils.hf_client import (
    is_valid_model_id,
    get_model_info,
    public_model_info,
    get_model_sizes,
    get_parameter_count,
    get_gguf_files,
    get_model_architecture,
)

app = Flask(__name__)

# "bpw" is the approximate effective bits per weight of each llama.cpp GGUF quantization
QUANTIZATION_LEVELS = {
    "q2_k": {"name": "Q2_K", "bits": 2, "bpw": 3.0, "quality": "Low", "speed": "Very Fast", "description": "Maximum compression, lower quality"},
    "q3_k_m": {"name": "Q3_K_M", "bits": 3, "bpw": 3.9, "quality": "Low-Medium", "speed": "Fast", "description": "Good compression with decent quality"},
    "q4_0": {"name": "Q4_0", "bits": 4, "bpw": 4.5, "quality": "Medium", "speed": "Fast", "description": "Good balance of quality and performance"},
    "q4_1": {"name": "Q4_1", "bits": 4, "bpw": 5.0, "quality": "Medium", "speed": "Fast", "description": "Slightly better than Q4_0"},
    "q4_k_m": {"name": "Q4_K_M", "bits": 4, "bpw": 4.85, "quality": "Medium", "speed": "Fast", "description": "Most popular choice, best 4-bit quality"},
    "q5_0": {"name": "Q5_0", "bits": 5, "bpw": 5.5, "quality": "High", "speed": "Medium", "description": "Better quality with moderate memory"},
    "q5_1": {"name": "Q5_1", "bits": 5, "bpw": 6.0, "quality": "High", "speed": "Medium", "description": "Best quality for legacy 5-bit quantization"},
    "q5_k_m": {"name": "Q5_K_M", "bits": 5, "bpw": 5.7, "quality": "High", "speed": "Medium", "description": "High quality 5-bit with k-quant improvements"},
    "q6_k": {"name": "Q6_K", "bits": 6, "bpw": 6.6, "quality": "High", "speed": "Medium", "description": "High quality with good compression"},
    "q8_0": {"name": "Q8_0", "bits": 8, "bpw": 8.5, "quality": "Very High", "speed": "Slower", "description": "Near-original quality"},
}
DEFAULT_QUANTIZATION = "q4_k_m"
DEFAULT_CONTEXT_LENGTH = 4096
MIN_CONTEXT_LENGTH = 256
MAX_CONTEXT_LENGTH = 1048576
KV_CACHE_BYTES_PER_ELEMENT = 2  # Ollama keeps the KV cache in f16 by default
MEMORY_OVERHEAD = 1.1  # compute buffers and runtime overhead on top of the weights
MEMORY_HEADROOM = 0.9  # keep some memory free for the OS and other processes
RUN_MODE_ORDER = {"GPU": 0, "GPU + CPU": 1, "CPU": 2}


def get_system_info():
    gpu_info = []

    try:
        import torch
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                free, total = torch.cuda.mem_get_info(i)
                gpu_info.append({
                    "name": torch.cuda.get_device_name(i),
                    "vram_total": total,
                    "vram_free": free,
                    "index": i
                })
    except Exception:
        # torch is optional (and may be CPU-only); fall back to nvidia-smi
        gpu_info = []

    if not gpu_info:
        try:
            nvidia_smi = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
            if nvidia_smi.returncode == 0:
                for line in nvidia_smi.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.rsplit(',', 2)]
                    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                        gpu_info.append({
                            "name": parts[0],
                            "vram_total": int(parts[1]) * 1024 * 1024,
                            "vram_free": int(parts[2]) * 1024 * 1024,
                            "index": len(gpu_info)
                        })
        except (subprocess.SubprocessError, OSError):
            pass

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    return {
        "gpu_info": gpu_info,
        "total_vram": sum(gpu["vram_total"] for gpu in gpu_info),
        "total_ram": mem.total,
        "available_ram": mem.available,
        "used_ram_percent": mem.percent,
        "swap_total": swap.total,
        "swap_used": swap.used,
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "cpu_count": psutil.cpu_count(),
        "platform": sys.platform,
    }


def recommend_quantization(system_info, param_count, kv_cache_bytes, gguf_files):
    total_vram = system_info["total_vram"]
    gpu_budget = total_vram * MEMORY_HEADROOM
    ram_budget = system_info["available_ram"] * MEMORY_HEADROOM
    recommendations = []

    for quant_name, quant_info in QUANTIZATION_LEVELS.items():
        # For GGUF repos only the quantizations actually published can be pulled
        if gguf_files and quant_name not in gguf_files:
            continue
        estimated_size = gguf_files.get(quant_name) or int(param_count * quant_info["bpw"] / 8)
        if not estimated_size:
            continue
        memory_needed = estimated_size * MEMORY_OVERHEAD + kv_cache_bytes

        if total_vram and memory_needed <= gpu_budget:
            run_mode = "GPU"
        elif total_vram and memory_needed <= gpu_budget + ram_budget:
            run_mode = "GPU + CPU"  # Ollama offloads the layers that don't fit to system RAM
        elif not total_vram and memory_needed <= ram_budget:
            run_mode = "CPU"
        else:
            continue

        recommendations.append({
            "quantization": quant_name,
            "name": quant_info["name"],
            "bits": quant_info["bits"],
            "bpw": quant_info["bpw"],
            "quality": quant_info["quality"],
            "speed": quant_info["speed"],
            "description": quant_info["description"],
            "estimated_size_gb": round(estimated_size / (1024**3), 2),
            "size_is_exact": quant_name in gguf_files,
            "memory_needed_gb": round(memory_needed / (1024**3), 2),
            "vram_usage_percent": round(memory_needed / total_vram * 100, 1) if total_vram else None,
            "run_mode": run_mode,
            "available": quant_name in gguf_files,
            "recommended": False,
        })

    if recommendations:
        best_mode = min(RUN_MODE_ORDER[r["run_mode"]] for r in recommendations)
        candidates = [r for r in recommendations if RUN_MODE_ORDER[r["run_mode"]] == best_mode]
        if best_mode > 0:
            # When the model doesn't fully fit on the GPU, favor speed over quality
            candidates = [r for r in candidates if r["bits"] <= 4] or candidates
        max(candidates, key=lambda r: r["bpw"])["recommended"] = True

    recommendations.sort(key=lambda r: (not r["recommended"], RUN_MODE_ORDER[r["run_mode"]], -r["bpw"]))
    return recommendations


def calculate_kv_cache(model_arch):
    num_layers = model_arch.get("num_hidden_layers") or 32
    num_heads = model_arch.get("num_attention_heads") or 32
    hidden_size = model_arch.get("hidden_size") or 4096
    # Grouped-query attention models cache fewer key/value heads than attention heads
    num_kv_heads = model_arch.get("num_key_value_heads") or num_heads
    head_dim = model_arch.get("head_dim") or hidden_size // num_heads
    max_positions = model_arch.get("max_position_embeddings") or 2048
    recommended_context = min(max_positions, DEFAULT_CONTEXT_LENGTH)

    # One key and one value vector per layer for every token
    per_token_bytes = 2 * num_layers * num_kv_heads * head_dim * KV_CACHE_BYTES_PER_ELEMENT
    total_kv_bytes = per_token_bytes * max_positions
    recommended_kv_bytes = per_token_bytes * recommended_context

    return {
        "kv_cache_per_token_bytes": per_token_bytes,
        "kv_cache_per_token_mb": round(per_token_bytes / (1024**2), 4),
        "kv_cache_total_bytes": total_kv_bytes,
        "kv_cache_total_gb": round(total_kv_bytes / (1024**3), 2),
        "kv_cache_recommended_bytes": recommended_kv_bytes,
        "kv_cache_recommended_gb": round(recommended_kv_bytes / (1024**3), 2),
        "recommended_context_length": recommended_context,
        "max_context_length": max_positions,
        "estimated": not (model_arch.get("num_hidden_layers") and model_arch.get("num_attention_heads")),
    }


def hf_reference(model_id, quantization):
    return f"hf.co/{model_id}:{QUANTIZATION_LEVELS[quantization]['name']}"


def ollama_model_name(model_id, quantization):
    repo = model_id.split("/")[-1].lower()
    repo = re.sub(r"[-_.]gguf$", "", repo)
    repo = re.sub(r"[^a-z0-9_.-]", "-", repo).strip("-.")
    return f"{repo or 'model'}:{quantization}"


def _json_body():
    return request.get_json(silent=True) or {}


def _parse_deploy_request(body):
    model_id = str(body.get("model_id", "")).strip()
    if not model_id:
        return None, (jsonify({"error": "Model ID is required"}), 400)
    if not is_valid_model_id(model_id):
        return None, (jsonify({"error": "Invalid model ID"}), 400)
    quantization = str(body.get("quantization") or DEFAULT_QUANTIZATION).lower()
    if quantization not in QUANTIZATION_LEVELS:
        return None, (jsonify({"error": f"Unsupported quantization: {quantization}"}), 400)
    return (model_id, quantization), None


def _with_final_result(events, model_name, success_message, failure_message):
    """Passes progress events through and ends with exactly one {"done": True, ...} result event."""
    for event in events:
        if event.get("error"):
            yield {"done": True, "success": False, "error": event["error"]}
            return
        if event.get("status") == "success":
            yield {"done": True, "success": True, "model_name": model_name, "message": success_message}
            return
        yield event
    yield {"done": True, "success": False, "error": failure_message}


def _ollama_action_response(events, model_name, success_message, failure_message, stream):
    results = _with_final_result(events, model_name, success_message, failure_message)

    if stream:
        # Newline-delimited JSON so the browser can show download progress live
        return Response(
            (json.dumps(event) + "\n" for event in results),
            mimetype="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    progress = []
    final = {}
    for event in results:
        if event.get("done"):
            final = {k: v for k, v in event.items() if k != "done"}
        # Download progress repeats the same status many times; keep one entry per step
        elif event.get("status") and (not progress or progress[-1] != event["status"]):
            progress.append(event["status"])
    return jsonify({**final, "progress": progress}), 200 if final["success"] else 502


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/system-info")
def api_system_info():
    return jsonify(get_system_info())


@app.route("/api/check-ollama")
def api_check_ollama():
    return jsonify(check_ollama_status())


@app.route("/api/check-model", methods=["POST"])
def api_check_model():
    model_id = str(_json_body().get("model_id", "")).strip()
    if not model_id:
        return jsonify({"error": "Model ID is required"}), 400

    model_info = get_model_info(model_id)
    if "error" in model_info:
        return jsonify({"error": model_info["error"]}), model_info.get("status_code", 500)

    model_sizes = get_model_sizes(model_info)
    param_count, param_source = get_parameter_count(model_info, model_sizes)
    gguf_files = get_gguf_files(model_info, QUANTIZATION_LEVELS)
    model_arch = get_model_architecture(model_info["id"], model_info)
    kv_cache_info = calculate_kv_cache(model_arch)

    system_info = get_system_info()
    recommendations = recommend_quantization(
        system_info,
        param_count,
        kv_cache_info["kv_cache_recommended_bytes"],
        gguf_files,
    )

    return jsonify({
        "model_info": public_model_info(model_info),
        "model_sizes": {k: v for k, v in model_sizes.items() if k != "files"},
        "parameters": {"count": param_count, "source": param_source},
        "is_gguf_repo": bool(gguf_files),
        "model_architecture": model_arch,
        "system_info": system_info,
        "recommendations": recommendations,
        "kv_cache": kv_cache_info,
    })


@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    body = _json_body()
    parsed, error = _parse_deploy_request(body)
    if error:
        return error
    model_id, quantization = parsed

    try:
        context_length = int(body.get("context_length", DEFAULT_CONTEXT_LENGTH))
    except (TypeError, ValueError):
        return jsonify({"error": "Context length must be an integer"}), 400
    context_length = max(MIN_CONTEXT_LENGTH, min(context_length, MAX_CONTEXT_LENGTH))

    ollama_model = ollama_model_name(model_id, quantization)
    # Ollama pulls the GGUF from HuggingFace (if needed) and creates a model with our context length
    events = stream_create_model(ollama_model, hf_reference(model_id, quantization), {"num_ctx": context_length})
    return _ollama_action_response(
        events, ollama_model, "Model created successfully",
        "Model creation did not complete successfully", bool(body.get("stream")),
    )


@app.route("/api/pull-model", methods=["POST"])
def api_pull_model():
    body = _json_body()
    parsed, error = _parse_deploy_request(body)
    if error:
        return error
    model_id, quantization = parsed

    ollama_model = hf_reference(model_id, quantization)
    return _ollama_action_response(
        stream_pull_model(ollama_model), ollama_model, "Model pulled successfully",
        "Pull did not complete successfully", bool(body.get("stream")),
    )


@app.route("/api/list-models")
def api_list_models():
    models = list_models()
    if isinstance(models, dict) and "error" in models:
        return jsonify(models), 502
    return jsonify(models)


@app.route("/api/delete-model", methods=["POST"])
def api_delete_model():
    model_name = str(_json_body().get("model_name", "")).strip()
    if not model_name:
        return jsonify({"error": "Model name is required"}), 400

    result = delete_model(model_name)
    if result.get("success"):
        return jsonify({"success": True, "message": "Model deleted successfully"})
    else:
        return jsonify({"success": False, "error": result.get("error", "Delete failed")}), 502


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    # The Werkzeug debugger allows code execution, so never enable it by default on a network-facing host
    debug = os.getenv("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    print("=" * 60)
    print("  Ollama Model Checker - Starting Server")
    print("=" * 60)
    print(f"  Server URL: http://localhost:{port}")
    print(f"  Press Ctrl+C to stop")
    print("=" * 60)
    app.run(debug=debug, host=host, port=port)
