from flask import Flask, render_template, request, jsonify
import psutil
import os
import sys
from utils.ollama_client import check_ollama_status, list_models, create_model, delete_model, pull_model
from utils.hf_client import get_model_info, get_model_sizes, get_model_architecture

app = Flask(__name__)

QUANTIZATION_LEVELS = {
    "q4_0": {"name": "Q4_0", "bits": 4, "quality": "Medium", "speed": "Fast", "description": "Good balance of quality and performance"},
    "q4_1": {"name": "Q4_1", "bits": 4, "quality": "Medium", "speed": "Fast", "description": "Slightly better than Q4_0"},
    "q5_0": {"name": "Q5_0", "bits": 5, "quality": "High", "speed": "Medium", "description": "Better quality with moderate memory"},
    "q5_1": {"name": "Q5_1", "bits": 5, "quality": "High", "speed": "Medium", "description": "Best quality for 5-bit quantization"},
    "q8_0": {"name": "Q8_0", "bits": 8, "quality": "Very High", "speed": "Slower", "description": "Near-original quality"},
    "q2_k": {"name": "Q2_K", "bits": 2, "quality": "Low", "speed": "Very Fast", "description": "Maximum compression, lower quality"},
    "q3_k_m": {"name": "Q3_K_M", "bits": 3, "quality": "Low-Medium", "speed": "Fast", "description": "Good compression with decent quality"},
    "q6_k": {"name": "Q6_K", "bits": 6, "quality": "High", "speed": "Medium", "description": "High quality with good compression"},
}

def get_system_info():
    total_vram = 0
    gpu_info = []
    
    try:
        import torch
        if torch.cuda.is_available():
            total_vram = sum(torch.cuda.get_device_properties(i).total_memory for i in range(torch.cuda.device_count()))
            for i in range(torch.cuda.device_count()):
                gpu_info.append({
                    "name": torch.cuda.get_device_name(i),
                    "vram_total": torch.cuda.get_device_properties(i).total_memory,
                    "vram_free": torch.cuda.mem_get_info(i)[1],
                    "index": i
                })
    except ImportError:
        pass
    
    if not gpu_info:
        try:
            import subprocess
            nvidia_smi = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5
            )
            if nvidia_smi.returncode == 0:
                for line in nvidia_smi.stdout.strip().split('\n'):
                    if line.strip():
                        parts = [p.strip() for p in line.split(',')]
                        if len(parts) >= 3:
                            gpu_info.append({
                                "name": parts[0],
                                "vram_total": int(parts[1].replace(' MiB', '')) * 1024 * 1024,
                                "vram_free": int(parts[2].replace(' MiB', '')) * 1024 * 1024,
                                "index": len(gpu_info)
                            })
                            total_vram += int(parts[1].replace(' MiB', '')) * 1024 * 1024
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    
    return {
        "gpu_info": gpu_info,
        "total_vram": total_vram,
        "total_ram": mem.total,
        "available_ram": mem.available,
        "used_ram_percent": mem.percent,
        "swap_total": swap.total,
        "swap_used": swap.used,
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "cpu_count": psutil.cpu_count(),
        "platform": sys.platform,
    }

def calculate_model_size_in_bytes(total_size, quant_bits=4):
    original_bits = 16
    return int(total_size * quant_bits / original_bits)

def recommend_quantization(total_vram, total_ram, model_size_bytes, model_arch):
    recommendations = []
    
    available_for_model = min(total_vram * 0.8, total_ram * 0.7)
    
    for quant_name, quant_info in QUANTIZATION_LEVELS.items():
        estimated_size = calculate_model_size_in_bytes(model_size_bytes, quant_info["bits"])
        needed_vram = estimated_size * 1.2
        needed_ram = estimated_size * 1.5
        
        if needed_vram <= total_vram or needed_ram <= available_for_model:
            if total_vram > 0:
                vram_efficiency = (estimated_size / total_vram) * 100
                recommendations.append({
                    "quantization": quant_name,
                    "name": quant_info["name"],
                    "bits": quant_info["bits"],
                    "quality": quant_info["quality"],
                    "speed": quant_info["speed"],
                    "description": quant_info["description"],
                    "estimated_size_gb": round(estimated_size / (1024**3), 2),
                    "vram_needed_gb": round(needed_vram / (1024**3), 2),
                    "ram_needed_gb": round(needed_ram / (1024**3), 2),
                    "vram_efficiency": round(vram_efficiency, 1),
                    "recommended": vram_efficiency > 50 and vram_efficiency < 90,
                })
    
    recommendations.sort(key=lambda x: (-x["recommended"], x["vram_efficiency"]))
    return recommendations

def calculate_kv_cache(model_arch, quant_bits=4):
    hidden_size = model_arch.get("hidden_size", 4096) or 4096
    num_layers = model_arch.get("num_hidden_layers", 32) or 32
    num_heads = model_arch.get("num_attention_heads", 32) or 32
    max_positions = model_arch.get("max_position_embeddings", 2048) or 2048
    
    if num_heads == 0:
        num_heads = 32
    if hidden_size == 0:
        hidden_size = 4096
    
    head_dim = hidden_size // num_heads
    kv_per_layer = 2 * num_heads * head_dim * (quant_bits / 8)
    total_kv_bytes = kv_per_layer * num_layers * max_positions
    
    return {
        "kv_cache_per_token_bytes": round(kv_per_layer, 2),
        "kv_cache_total_bytes": total_kv_bytes,
        "kv_cache_total_gb": round(total_kv_bytes / (1024**3), 2),
        "kv_cache_per_token_mb": round(kv_per_layer / (1024**2), 4),
        "recommended_context_length": min(max_positions, 4096),
        "max_context_length": max_positions,
    }

@app.route("/")
def index():
    system_info = get_system_info()
    ollama_status = check_ollama_status()
    return render_template("index.html", 
                         system_info=system_info, 
                         ollama_status=ollama_status)

@app.route("/api/system-info")
def api_system_info():
    return jsonify(get_system_info())

@app.route("/api/check-ollama")
def api_check_ollama():
    return jsonify(check_ollama_status())

@app.route("/api/check-model", methods=["POST"])
def api_check_model():
    model_id = request.json.get("model_id", "")
    if not model_id:
        return jsonify({"error": "Model ID is required"}), 400
    
    model_info = get_model_info(model_id)
    if "error" in model_info:
        return jsonify(model_info), 404
    
    model_sizes = get_model_sizes(model_id)
    model_arch = get_model_architecture(model_id)
    
    system_info = get_system_info()
    recommendations = recommend_quantization(
        system_info["total_vram"],
        system_info["available_ram"],
        model_sizes.get("total_size", 0),
        model_arch
    )
    
    kv_cache_info = calculate_kv_cache(model_arch)
    
    return jsonify({
        "model_info": model_info,
        "model_sizes": model_sizes,
        "model_architecture": model_arch,
        "system_info": system_info,
        "recommendations": recommendations,
        "kv_cache": kv_cache_info,
    })

@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    model_id = request.json.get("model_id", "")
    quantization = request.json.get("quantization", "q4_0")
    context_length = request.json.get("context_length", 4096)
    
    if not model_id:
        return jsonify({"error": "Model ID is required"}), 400
    
    ollama_model_name = f"{model_id.split('/')[-1]}:{quantization}"
    
    modelfile = f"""FROM {model_id}
PARAMETER num_ctx {context_length}
PARAMETER num_gpu 999
"""
    
    result = create_model(ollama_model_name, modelfile)
    
    if result.get("success"):
        return jsonify({
            "success": True,
            "model_name": ollama_model_name,
            "message": "Model created successfully",
            "progress": result.get("progress", [])
        })
    else:
        return jsonify({
            "success": False,
            "error": result.get("error", "Deployment failed"),
            "progress": result.get("progress", [])
        }), 500

@app.route("/api/pull-model", methods=["POST"])
def api_pull_model():
    model_id = request.json.get("model_id", "")
    quantization = request.json.get("quantization", "q4_0")
    
    if not model_id:
        return jsonify({"error": "Model ID is required"}), 400
    
    ollama_model_name = f"{model_id.split('/')[-1]}:{quantization}"
    result = pull_model(ollama_model_name)
    
    if result.get("success"):
        return jsonify({
            "success": True,
            "model_name": ollama_model_name,
            "message": "Model pulled successfully",
            "progress": result.get("progress", [])
        })
    else:
        return jsonify({
            "success": False,
            "error": result.get("error", "Pull failed"),
            "progress": result.get("progress", [])
        }), 500

@app.route("/api/list-models")
def api_list_models():
    return jsonify(list_models())

@app.route("/api/delete-model", methods=["POST"])
def api_delete_model():
    model_name = request.json.get("model_name", "")
    if not model_name:
        return jsonify({"error": "Model name is required"}), 400
    
    result = delete_model(model_name)
    if result.get("success"):
        return jsonify({"success": True, "message": "Model deleted successfully"})
    else:
        return jsonify({"success": False, "error": result.get("error", "Delete failed")}), 500

if __name__ == "__main__":
    print("=" * 60)
    print("  Ollama Model Checker - Starting Server")
    print("=" * 60)
    print(f"  Server URL: http://localhost:5000")
    print(f"  Press Ctrl+C to stop")
    print("=" * 60)
    app.run(debug=True, host="0.0.0.0", port=5000)
