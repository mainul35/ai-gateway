import requests
import os

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

def check_ollama_status():
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/version", timeout=5)
        response.raise_for_status()
        return {"status": "running", "version": response.json().get("version", "unknown")}
    except requests.exceptions.ConnectionError:
        return {"status": "offline", "version": None}
    except Exception as e:
        return {"status": "error", "version": None, "message": str(e)}

def list_models():
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
        response.raise_for_status()
        models = response.json().get("models", [])
        return [{"name": m["name"], "size": m.get("size", 0), "modified": m.get("modified_at", "")} for m in models]
    except Exception as e:
        return {"error": str(e)}

def create_model(name, modelfile_content):
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/create",
            json={"name": name, "modelfile": modelfile_content},
            stream=True,
            timeout=3600
        )
        response.raise_for_status()
        progress = []
        for line in response.iter_lines(decode_unicode=True):
            if line:
                data = __import__('json').loads(line)
                progress.append(data.get("status", ""))
                if data.get("status") == "success":
                    return {"success": True, "progress": progress}
        return {"success": False, "progress": progress, "error": "Upload did not complete successfully"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def delete_model(name):
    try:
        response = requests.delete(f"{OLLAMA_HOST}/api/delete", json={"name": name}, timeout=10)
        response.raise_for_status()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

def pull_model(name, stream=True):
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/pull",
            json={"name": name, "stream": stream},
            stream=True,
            timeout=3600
        )
        response.raise_for_status()
        progress = []
        for line in response.iter_lines(decode_unicode=True):
            if line:
                data = __import__('json').loads(line)
                progress.append(data.get("status", ""))
                if data.get("status") == "success":
                    return {"success": True, "progress": progress}
        return {"success": False, "progress": progress, "error": "Pull did not complete successfully"}
    except Exception as e:
        return {"success": False, "error": str(e)}
