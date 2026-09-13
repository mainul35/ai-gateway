import json
import os
import threading
import requests


def _normalize_host(host):
    # OLLAMA_HOST is shared with the Ollama server itself, where values like "0.0.0.0" or
    # "127.0.0.1:11434" (no scheme) are valid. Turn them into a URL a client can connect to.
    host = (host or "").strip().rstrip("/") or "http://localhost:11434"
    if "://" not in host:
        # Without a scheme Ollama assumes plain HTTP on its default port
        host_part, _, path = host.partition("/")
        if ":" not in host_part:
            host_part = f"{host_part}:11434"
        host = f"http://{host_part}" + (f"/{path}" if path else "")
    scheme, rest = host.split("://", 1)
    if rest.startswith("0.0.0.0"):
        rest = "127.0.0.1" + rest[len("0.0.0.0"):]
    return f"{scheme}://{rest}"


OLLAMA_HOST = _normalize_host(os.getenv("OLLAMA_HOST"))


def _connection_error():
    return f"Cannot connect to Ollama at {OLLAMA_HOST}. Is it running?"


def _response_error(response):
    try:
        return response.json().get("error") or response.text
    except ValueError:
        return response.text or f"HTTP {response.status_code}"


def check_ollama_status():
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/version", timeout=5)
        response.raise_for_status()
        return {"status": "running", "version": response.json().get("version", "unknown"), "host": OLLAMA_HOST}
    except requests.exceptions.ConnectionError:
        return {"status": "offline", "version": None, "host": OLLAMA_HOST}
    except Exception as e:
        return {"status": "error", "version": None, "host": OLLAMA_HOST, "message": str(e)}


def list_models():
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
        response.raise_for_status()
        models = response.json().get("models") or []
        return [{"name": m["name"], "size": m.get("size", 0), "modified": m.get("modified_at", "")} for m in models]
    except requests.exceptions.ConnectionError:
        return {"error": _connection_error()}
    except Exception as e:
        return {"error": str(e)}


class StreamCancellation:
    """Lets another thread stop a streaming Ollama request.

    Closing the connection is what makes Ollama abort the pull; it keeps the partial download for next time.
    """

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._response = None

    @property
    def cancelled(self):
        return self._event.is_set()

    def cancel(self):
        self._event.set()
        with self._lock:
            if self._response is not None:
                # Also unblocks a read that is waiting for Ollama's next progress line
                self._response.close()

    def attach(self, response):
        with self._lock:
            self._response = response
        if self.cancelled:
            response.close()


def _iter_stream(path, payload, cancellation=None):
    """Yields Ollama's progress events as they arrive.

    Failures are yielded as {"error": ...} and a cancellation as {"cancelled": True}.
    """
    is_cancelled = lambda: cancellation is not None and cancellation.cancelled
    if is_cancelled():
        yield {"cancelled": True}
        return
    try:
        with requests.post(f"{OLLAMA_HOST}{path}", json=payload, stream=True, timeout=(10, 3600)) as response:
            if cancellation is not None:
                cancellation.attach(response)
            if response.status_code >= 400:
                yield {"error": _response_error(response)}
                return
            for line in response.iter_lines(decode_unicode=True):
                if is_cancelled():
                    break
                if line:
                    # Ollama reports failures as {"error": ...} lines inside a 200 stream
                    yield json.loads(line)
        if is_cancelled():
            yield {"cancelled": True}
    except requests.exceptions.ConnectionError:
        yield {"cancelled": True} if is_cancelled() else {"error": _connection_error()}
    except Exception as e:
        # Closing the response from another thread surfaces here as a read error
        yield {"cancelled": True} if is_cancelled() else {"error": str(e)}


def stream_create_model(name, source, parameters=None, cancellation=None):
    payload = {"model": name, "from": source}
    if parameters:
        payload["parameters"] = parameters
    return _iter_stream("/api/create", payload, cancellation)


def stream_pull_model(name, cancellation=None):
    return _iter_stream("/api/pull", {"model": name, "stream": True}, cancellation)


def delete_model(name):
    try:
        response = requests.delete(f"{OLLAMA_HOST}/api/delete", json={"model": name}, timeout=10)
        if response.status_code >= 400:
            return {"success": False, "error": _response_error(response)}
        return {"success": True}
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": _connection_error()}
    except Exception as e:
        return {"success": False, "error": str(e)}
