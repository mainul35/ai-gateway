"""POST /v1/messages/count_tokens, against a fake llama-server that only knows /tokenize.

The counted path - a llama.cpp engine already running, asked its real token count - and every way
it falls back to context_fit.estimate: no engine running, one that died, a backend that is not
llama.cpp, and a /tokenize that fails, times out or answers nonsense. And that counting never
starts, loads or swaps an engine. No real model, no GPU, no Ollama.

    python -m pytest -q tests/test_count_tokens.py
"""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app import backends, context_fit
from app.auth import Principal, authenticate
from app.backends import Backend
from app.engine.supervisor import RunningModel, Supervisor, supervisor
from app.routers import anthropic_v1

MAIN = "qwen3.6-256k"
OLLAMA_MODEL = "gemma4:31b"


def isolated(monkeypatch, tmp_path):
    """No config.properties and no Ollama: only what the test itself hands in."""
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "absent.properties"))
    monkeypatch.setenv("ANTHROPIC_MODEL_DEFAULT", MAIN)


def fake_models(monkeypatch, models):
    async def available(force_refresh=False):
        return models
    monkeypatch.setattr(backends, "available_models", available)


def gateway_models(port):
    """A llama.cpp profile and an Ollama model, the way /v1/messages would resolve them."""
    return {
        MAIN: Backend(name="llamacpp", kind="llamacpp", base_url=f"http://127.0.0.1:{port}/v1",
                      upstream_model=MAIN, capabilities=("tools", "thinking"),
                      context_length=256_000),
        OLLAMA_MODEL: Backend(name="ollama", kind="ollama", base_url="http://127.0.0.1:11434/v1",
                              upstream_model=OLLAMA_MODEL, capabilities=("tools", "vision")),
    }


class Tokenizer:
    """A fake llama-server: POST /tokenize answers one token per word, however the mode says.

    mode: "ok", "fail" (500), "garbage" (200, not JSON) or "slow" (answers late).
    """

    def __init__(self):
        self.mode = "ok"
        self.requests = []           # (path, body) of everything that reached it
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server.server_address[1]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _handler_for(engine):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            engine.requests.append((self.path, body))
            if self.path != "/tokenize":
                self.reply(404, b"no such route")
            elif engine.mode == "fail":
                self.reply(500, b"engine died")
            elif engine.mode == "garbage":
                self.reply(200, b"<html>not json</html>")
            else:
                if engine.mode == "slow":
                    time.sleep(0.4)
                self.reply(200, json.dumps({"tokens": (body.get("content") or "").split()}).encode())

        def reply(self, status, payload):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    return Handler


def running_engine(name, port, dead=False):
    """The supervisor's entry for an engine on that port; no real process behind it."""
    return RunningModel(SimpleNamespace(name=name, port=port),
                        SimpleNamespace(returncode=1 if dead else None, pid=None))


def body(model=MAIN):
    """A Claude Code turn: system, text, a tool call and its result, a picture and a document."""
    return {
        "model": model,
        "system": "You are a coding agent.",
        "messages": [
            {"role": "user", "content": "Fix the login bug in app/auth.py"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Reading the file."},
                {"type": "tool_use", "name": "Read", "input": {"path": "app/auth.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "text", "text": "def login(user): return user"},
                    {"type": "image", "source": {"type": "base64", "data": "PICTUREDATA"}},
                ]},
                {"type": "document", "source": {"type": "base64", "data": "DOCUMENTDATA"}},
            ]},
        ],
        "tools": [{"name": "Read", "description": "Read a file",
                   "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
    }


def post(payload=None, raw=None, principal=None):
    """One POST /v1/messages/count_tokens on a gateway whose only router is the Anthropic one."""
    app = FastAPI()
    app.include_router(anthropic_v1.router)
    app.dependency_overrides[authenticate] = lambda: principal or Principal(is_master=True)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway") as client:
            if raw is not None:
                return await client.post("/v1/messages/count_tokens", content=raw)
            return await client.post("/v1/messages/count_tokens", json=payload)
    return asyncio.run(go())


def never_starts_an_engine(monkeypatch):
    """Fail the test if anything counts by starting, loading or swapping an engine."""
    def boom(*args, **kwargs):
        raise AssertionError("an engine was started just to count tokens")
    monkeypatch.setattr(Supervisor, "ensure_running", boom)
    monkeypatch.setattr(Supervisor, "_start_locked", boom)


# --- the counted path -------------------------------------------------------------------------------


def test_the_running_engine_answers_with_its_real_count(monkeypatch, tmp_path):
    isolated(monkeypatch, tmp_path)
    engine = Tokenizer()
    fake_models(monkeypatch, gateway_models(engine.port))
    monkeypatch.setitem(supervisor._running, MAIN, running_engine(MAIN, engine.port))
    never_starts_an_engine(monkeypatch)
    try:
        # A Claude Code name resolves, exactly as /v1/messages would, to the engine that is running
        response = post(body("claude-sonnet-5"))
    finally:
        engine.stop()
    assert response.status_code == 200

    path, payload = engine.requests[0]
    sent = payload["content"]
    assert path == "/tokenize" and len(engine.requests) == 1
    # everything the model would read went in: system, turns, tool call and its result, tool defs
    for piece in ("You are a coding agent.", "Fix the login bug", "Reading the file.",
                  "Read", '"path"', "def login(user)", "input_schema"):
        assert piece in sent
    # and none of the picture's or the document's data, each of which costs IMAGE_TOKENS instead
    assert "PICTUREDATA" not in sent and "DOCUMENTDATA" not in sent
    assert response.json() == {"input_tokens": len(sent.split()) + 2 * context_fit.IMAGE_TOKENS}


# --- the fallbacks ------------------------------------------------------------------------------------


def test_no_engine_running_keeps_the_estimate(monkeypatch, tmp_path):
    isolated(monkeypatch, tmp_path)
    fake_models(monkeypatch, gateway_models(9))       # llama.cpp, but nothing started
    never_starts_an_engine(monkeypatch)
    response = post(body())
    assert response.status_code == 200
    assert response.json() == {"input_tokens": context_fit.estimate(body())}
    assert not supervisor._running                    # and nothing was started


def test_a_dead_engine_keeps_the_estimate(monkeypatch, tmp_path):
    isolated(monkeypatch, tmp_path)
    engine = Tokenizer()
    fake_models(monkeypatch, gateway_models(engine.port))
    monkeypatch.setitem(supervisor._running, MAIN, running_engine(MAIN, engine.port, dead=True))
    never_starts_an_engine(monkeypatch)
    try:
        response = post(body())
    finally:
        engine.stop()
    assert response.json() == {"input_tokens": context_fit.estimate(body())}
    assert not engine.requests                          # the dead engine was not asked anything


def test_a_backend_that_is_not_llama_cpp_keeps_the_estimate(monkeypatch, tmp_path):
    isolated(monkeypatch, tmp_path)
    engine = Tokenizer()
    fake_models(monkeypatch, gateway_models(engine.port))
    monkeypatch.setitem(supervisor._running, OLLAMA_MODEL, running_engine(OLLAMA_MODEL, engine.port))
    never_starts_an_engine(monkeypatch)
    try:
        response = post(body(OLLAMA_MODEL))
    finally:
        engine.stop()
    assert response.json() == {"input_tokens": context_fit.estimate(body(OLLAMA_MODEL))}
    assert not engine.requests                          # Ollama has no /tokenize to ask


def test_a_failed_tokenize_keeps_the_estimate(monkeypatch, tmp_path):
    isolated(monkeypatch, tmp_path)
    engine = Tokenizer()
    engine.mode = "fail"
    fake_models(monkeypatch, gateway_models(engine.port))
    monkeypatch.setitem(supervisor._running, MAIN, running_engine(MAIN, engine.port))
    try:
        response = post(body())
    finally:
        engine.stop()
    assert engine.requests                                # asked once, and refused
    assert response.json() == {"input_tokens": context_fit.estimate(body())}


def test_a_hung_tokenize_keeps_the_estimate(monkeypatch, tmp_path):
    isolated(monkeypatch, tmp_path)
    engine = Tokenizer()
    engine.mode = "slow"
    fake_models(monkeypatch, gateway_models(engine.port))
    monkeypatch.setitem(supervisor._running, MAIN, running_engine(MAIN, engine.port))
    monkeypatch.setattr(anthropic_v1, "TOKENIZE_TIMEOUT", 0.1)
    try:
        response = post(body())
    finally:
        engine.stop()
    assert response.json() == {"input_tokens": context_fit.estimate(body())}


def test_nonsense_from_tokenize_keeps_the_estimate(monkeypatch, tmp_path):
    isolated(monkeypatch, tmp_path)
    engine = Tokenizer()
    engine.mode = "garbage"
    fake_models(monkeypatch, gateway_models(engine.port))
    monkeypatch.setitem(supervisor._running, MAIN, running_engine(MAIN, engine.port))
    try:
        response = post(body())
    finally:
        engine.stop()
    assert engine.requests
    assert response.json() == {"input_tokens": context_fit.estimate(body())}


# --- resolution errors, same as /v1/messages ---------------------------------------------------------


def test_a_model_the_caller_may_not_use_is_403(monkeypatch, tmp_path):
    isolated(monkeypatch, tmp_path)
    fake_models(monkeypatch, gateway_models(9))
    never_starts_an_engine(monkeypatch)
    closed_door = Principal(user=SimpleNamespace(role="user", model_access="none"))
    response = post(body(MAIN), principal=closed_door)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "permission_error"


def test_no_such_model_is_404(monkeypatch, tmp_path):
    isolated(monkeypatch, tmp_path)
    fake_models(monkeypatch, gateway_models(9))
    never_starts_an_engine(monkeypatch)
    response = post(body("does-not-exist"))
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found_error"


def test_a_body_that_is_not_json_is_400():
    response = post(raw=b"count me, but I am not JSON")
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
