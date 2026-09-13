import json
import os
import queue
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

# How often a waiting stream checks whether it was cancelled
CANCEL_POLL_SECONDS = 0.2


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
    """Lets another thread (e.g. the /api/cancel request) stop a streaming Ollama request."""

    def __init__(self):
        self._event = threading.Event()

    @property
    def cancelled(self):
        return self._event.is_set()

    def cancel(self):
        self._event.set()


_STREAM_END = object()


def _read_stream(path, payload, events, cancellation):
    """Worker thread: puts Ollama's progress events on the queue until the stream ends or is cancelled."""
    try:
        with requests.post(f"{OLLAMA_HOST}{path}", json=payload, stream=True, timeout=(10, 3600)) as response:
            if response.status_code >= 400:
                events.put({"error": _response_error(response)})
                return
            for line in response.iter_lines(decode_unicode=True):
                if cancellation.cancelled:
                    # Leaving the with block closes the connection, which is what makes Ollama stop the pull
                    return
                if line:
                    # Ollama reports failures as {"error": ...} lines inside a 200 stream
                    events.put(json.loads(line))
    except requests.exceptions.ConnectionError:
        events.put({"error": _connection_error()})
    except Exception as e:
        events.put({"error": str(e)})
    finally:
        events.put(_STREAM_END)


def _iter_stream(path, payload, cancellation=None):
    """Yields Ollama's progress events as they arrive.

    Failures are yielded as {"error": ...} and a cancellation as {"cancelled": True}.
    """
    cancellation = cancellation or StreamCancellation()
    if cancellation.cancelled:
        yield {"cancelled": True}
        return

    # Reading happens in a worker thread because a blocked socket read can't be interrupted portably, and
    # Ollama can go quiet for minutes (e.g. "verifying sha256 digest"). Waiting on a queue instead lets a
    # cancel take effect right away; the worker disconnects from Ollama as soon as its read returns.
    events = queue.Queue()
    threading.Thread(target=_read_stream, args=(path, payload, events, cancellation), daemon=True).start()
    try:
        while True:
            try:
                event = events.get(timeout=CANCEL_POLL_SECONDS)
            except queue.Empty:
                event = None
            if cancellation.cancelled:
                yield {"cancelled": True}
                return
            if event is _STREAM_END:
                return
            if event is not None:
                yield event
    finally:
        # Also covers the consumer stopping early (e.g. the browser disconnected): tell the worker to disconnect
        cancellation.cancel()


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
