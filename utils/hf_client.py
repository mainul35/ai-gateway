import requests

HF_API_BASE = "https://huggingface.co/api"

def get_model_info(model_id):
    try:
        response = requests.get(f"{HF_API_BASE}/models/{model_id}", timeout=15)
        if response.status_code == 404:
            return {"error": "Model not found", "status_code": 404}
        response.raise_for_status()
        data = response.json()
        return {
            "id": data.get("id", ""),
            "author": data.get("author", ""),
            "pipeline_tag": data.get("pipeline_tag", ""),
            "tags": data.get("tags", []),
            "likes": data.get("likes", 0),
            "downloads": data.get("downloads", 0),
            "siblings": data.get("siblings", []),
            "cardData": data.get("cardData", {}),
            "createdAt": data.get("createdAt", ""),
        }
    except Exception as e:
        return {"error": str(e)}

def get_model_sizes(model_id):
    info = get_model_info(model_id)
    if "error" in info:
        return info
    siblings = info.get("siblings", [])
    files = []
    for s in siblings:
        path = s.get("path", "")
        rfilename = s.get("rfilename", path)
        size = s.get("size", 0)
        if size and rfilename:
            files.append({"path": path, "size": size})
    total_size = sum(f["size"] for f in files)
    return {"files": files, "total_size": total_size, "file_count": len(files)}

def get_model_architecture(model_id):
    info = get_model_info(model_id)
    if "error" in info:
        return info
    card_data = info.get("cardData", {})
    arch = card_data.get("architectures", []) or []
    model_type = card_data.get("model_type", "") or ""
    return {
        "architectures": arch,
        "model_type": model_type,
        "hidden_size": card_data.get("hidden_size", 0),
        "num_attention_heads": card_data.get("num_attention_heads", 0),
        "num_hidden_layers": card_data.get("num_hidden_layers", 0),
        "vocab_size": card_data.get("vocab_size", 0),
        "max_position_embeddings": card_data.get("max_position_embeddings", 0),
        "dtype": card_data.get("inference", {}).get("params", {}).get("default", {}).get("dtype", ""),
    }

def get_model_config(model_id):
    try:
        response = requests.get(f"{HF_API_BASE}/models/{model_id}/config", timeout=15)
        if response.status_code == 404:
            return {"error": "Config not found"}
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}
