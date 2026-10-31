"""Anthropic's Messages API, so Claude Code can use this gateway's models.

Claude Code is pointed at the gateway with ANTHROPIC_BASE_URL and a gateway key (ANTHROPIC_AUTH_TOKEN
sends it as a bearer token, ANTHROPIC_API_KEY as x-api-key; both are accepted). It then sends
everything to POST /v1/messages - the same model names as any other client, or its own claude-*
names, which are mapped to a gateway model below.

Ollama and llama.cpp both speak the Messages API themselves, so a request for one of their models is
passed through unchanged: Claude Code's tool calls, images and streaming reach the model exactly as
they were sent, with nothing in between to get wrong. Anything else - an OpenAI-only upstream, or a
backend that turns out not to have /v1/messages - is translated (app/anthropic_compat.py).

Anthropic does not support Claude Code on models other than Claude; this is for running it on one's
own models anyway, knowing that.
"""
import asyncio
import json
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import access, anthropic_compat as compat, api_images, backends, context_fit, limits, settings, usage as usage_log
from app.auth import Principal, authenticate
from app.routers import code_sessions
from app.engine.supervisor import supervisor
from app import claude_desktop
from app.tools import auto_model

router = APIRouter(prefix="/v1", tags=["anthropic"])
hello = APIRouter(tags=["anthropic"])

log = logging.getLogger("gateway")

HEARTBEAT_SECONDS = 15
ENDPOINT = "anthropic_messages"
TOKENIZE_TIMEOUT = 20   # counting is a courtesy, not the request's purpose; then fall back to the estimate
_DONE = object()
# Backends found not to have /v1/messages, by name; translated from then on
_no_messages: set[str] = set()


def _error(status_code, message, kind="api_error"):
    return JSONResponse(compat.error_body(message, kind), status_code=status_code)


def _sse_error(message, kind="api_error"):
    return compat.sse("error", compat.error_body(message, kind))


def model_for(name):
    """The gateway model a request's model name means.

    Claude Code asks for its own models by name - claude-sonnet-..., and claude-haiku-... for
    background work like naming a session - unless told otherwise. Those are mapped to the models
    set in config.properties; every other name is taken as a gateway model as it stands. The small
    model defaults to the main one on purpose: a background title written by a second model would
    make the GPU swap models in the middle of somebody's task.
    """
    name = (name or "").strip()
    if not name.lower().startswith("claude"):
        return name
    main = settings.get("anthropic.model.default", "ANTHROPIC_MODEL_DEFAULT") or "qwen3.6-256k"
    if "haiku" in name.lower():
        return settings.get("anthropic.model.small", "ANTHROPIC_MODEL_SMALL") or main
    return main


def _plain(name):
    """A model name reduced to what survives being written as part of a claude-* name."""
    return "".join(c if c.isalnum() else "-" for c in name.lower())


async def _model_named(asked):
    """model_for, plus a gateway model chosen by a claude-* name: claude-gemma4-31b is gemma4:31b.

    The desktop app takes only model names that look like Claude's, so one gateway model per name is
    the only way to offer several in its picker. A name that matches no gateway model - Claude's own,
    claude-sonnet-4-5 - keeps meaning the configured default.
    """
    name = (asked or "").strip()
    if name.lower().startswith("anthropic/"):
        name = name[len("anthropic/"):]
    if name.lower().startswith("claude-"):
        wanted = _plain(name[len("claude-"):])
        for model in await backends.available_models():
            if _plain(model) == wanted:
                return model
    return model_for(name)


def _has_images(body):
    """Whether any message, or any tool result in one, carries an image."""
    def blocks(content):
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict):
                yield block
                yield from blocks(block.get("content"))
    return any(block.get("type") == "image"
               for message in body.get("messages") or () for block in blocks(message.get("content")))


async def _target(backend, model):
    """Where the request goes, the model loaded first when it is one of ours. (base_url, running)."""
    if backend.kind == "llamacpp":
        running, problem = await supervisor.ensure_running(model)
        if problem:
            raise RuntimeError(problem)
        running.in_flight += 1
        return running.base_url, running
    if backend.kind == "ollama":
        problem = await supervisor.make_room_for_ollama(backend.upstream_model)
        if problem:
            raise RuntimeError(problem)
    # Ollama's OpenAI address is <host>/v1; its Messages API is <host>/v1/messages
    base = backend.base_url[:-3] if backend.base_url.endswith("/v1") else backend.base_url
    return base, None


async def _fitted(body, backend, base, force=False):
    """The request made to fit the model's window (app/context_fit.py): (body, how); body is None
    when nothing makes it fit."""
    url = f"{base}/v1/chat/completions" if backend.kind in ("ollama", "llamacpp") \
        else backend.url("/chat/completions")
    return await context_fit.fit_for(body, backend, url, force)


# What a usage's input counts are reported under; Claude Code adds them up to see how full the window is
INPUT_COUNTS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _scale(original, sent):
    """How much bigger the request a client sent is than the one the model read, once made to fit.

    The model counts only what it was given. Told that, a client never sees its conversation grow past
    what the shortening hides, and keeps on until nothing can be shortened any more: Claude Code then
    compacts only after a request is refused, from a conversation already over the window. Reported
    at the size it sent, it compacts in time."""
    if sent is None or sent is original:
        return 1.0
    return max(1.0, context_fit.estimate(original) / max(1, context_fit.estimate(sent)))


def _scaled_usage(usage, scale):
    if scale > 1 and isinstance(usage, dict):
        for name in INPUT_COUNTS:
            if isinstance(usage.get(name), int):
                usage[name] = round(usage[name] * scale)
    return usage


def _reported(text, scale):
    """Messages API events (SSE text) with their input counts at the size the client sent (_scale)."""
    if scale <= 1 or "input_tokens" not in text:
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("data:") and "input_tokens" in line:
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            if isinstance(event, dict):
                _scaled_usage(event.get("usage"), scale)
                _scaled_usage((event.get("message") or {}).get("usage"), scale)
                lines[i] = "data: " + json.dumps(event)
    return "\n".join(lines)


def _passes_through(backend):
    return backend.kind in ("ollama", "llamacpp") and backend.name not in _no_messages


def _counted_text(body):
    """The text of a request as the model would read it, and its pictures counted aside.

    Everything llama-server's /tokenize is given: the system prompt, every message's text, tool calls'
    names and inputs, tool results' contents, and the tool definitions as their JSON. A picture or
    document block is not text - its data never reaches the tokenizer - but still costs the window
    context_fit.IMAGE_TOKENS, the figure the estimate gives it.
    """
    parts, media = [], 0

    def content(value):
        nonlocal media
        if isinstance(value, str):
            parts.append(value)
            return
        for block in value or ():
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in context_fit.MESSAGES.picture_kinds:
                media += 1
            elif kind == "text":
                parts.append(block.get("text") or "")
            elif kind == "tool_use":
                parts.append(block.get("name") or "")
                parts.append(json.dumps(block.get("input") or {}))
            elif kind == "tool_result":
                content(block.get("content"))
            elif kind in ("thinking", "redacted_thinking"):
                parts.append(block.get("thinking") or block.get("data") or "")

    content(body.get("system"))
    for message in body.get("messages") or ():
        if isinstance(message, dict):
            content(message.get("content"))
    parts.append(json.dumps(body.get("tools") or []))
    return "\n".join(part for part in parts if part), media


async def _real_count(body, backend, model):
    """The model's own token count of a request, or None when no engine can give one.

    Only a llama.cpp engine that is already running answers it, and only its /tokenize is asked of it:
    counting never starts, loads or swaps a model. No engine, not llama.cpp, a failed or timed-out
    call - None, and the caller keeps the estimate.
    """
    if backend.kind != "llamacpp":
        return None
    running = supervisor.loaded(model)
    if running is None:
        return None
    text, media = _counted_text(body)
    try:
        with supervisor.track_request(running):      # not reaped mid-count
            async with httpx.AsyncClient(timeout=httpx.Timeout(TOKENIZE_TIMEOUT, connect=5)) as client:
                # llama-server reads "content", vLLM "prompt" and the model's name; both answer "tokens"
                response = await client.post(f"{running.base_url}/tokenize",
                                             json={"content": text, "prompt": text, "model": model},
                                             headers=backend.headers())
        if not response.is_success:
            return None
        return len(response.json()["tokens"]) + media * context_fit.IMAGE_TOKENS
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("count_tokens: %s /tokenize failed: %s", model, e)
        return None


def _upstream_headers(request, backend):
    headers = {"content-type": "application/json"}
    # Passed on as they came: the docs ask gateways not to filter these
    for name in ("anthropic-version", "anthropic-beta"):
        if request.headers.get(name):
            headers[name] = request.headers[name]
    headers.setdefault("anthropic-version", "2023-06-01")
    if backend.api_key:
        headers["x-api-key"] = backend.api_key
        headers["authorization"] = f"Bearer {backend.api_key}"
    return headers


@hello.head("/api/hello")
@hello.get("/api/hello")
async def api_hello():
    """Claude Code warms its connection with this; anything but an error will do."""
    return Response(status_code=200)


@router.post("/messages/count_tokens")
async def count_tokens(request: Request, principal: Principal = Depends(authenticate)):
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "Request body must be JSON", "invalid_request_error")

    # Resolved exactly as /v1/messages does, so the count comes from the model the request would run on
    asked = await claude_desktop.route(body.get("model"), principal)
    model = await _model_named(asked)
    if not model:
        return _error(400, "Field 'model' is required", "invalid_request_error")
    if not access.can_use_model(principal, model):
        return _error(403, f"You do not have access to model '{model}'", "permission_error")
    backend = await backends.resolve(model)
    if backend is None:
        return _error(404, f"Model '{model}' is not available on this gateway"
                           + (f" (asked for as '{asked}')" if asked != model else ""), "not_found_error")

    # Claude Code compacts a conversation by this count, so an overestimate of 3 characters a token
    # makes it compact far too early on our models: ask the running engine the real number when one
    # can answer cheaply, and keep the estimate (app/context_fit.py, on the high side, a client
    # compacts by it before the model's window, not after) when it cannot.
    counted = await _real_count(body, backend, model)
    return {"input_tokens": counted if counted is not None else context_fit.estimate(body)}


@router.post("/messages")
async def messages(request: Request, principal: Principal = Depends(authenticate)):
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "Request body must be JSON", "invalid_request_error")
    # A coding tool's request is its session's latest step (the playground's Code mode)
    reply = code_sessions.observe(request.headers, principal, body, "messages")

    body["messages"] = compat.fold_system_messages(body.get("messages"))

    # Image URLs are fetched here, not by the model server inside this network (app/api_images.py)
    try:
        await api_images.inline(body.get("messages"))
    except api_images.ImageRefused as e:
        return _error(400, str(e), "invalid_request_error")

    # One of Claude's own IDs, as the desktop app sends them, means the gateway model it stands for
    asked = await claude_desktop.route(body.get("model"), principal)
    # "auto", or "claude-auto" from the desktop app, which takes only Claude-looking names: the
    # gateway picks the model for this request, as the playground's Auto does
    if auto_model.is_auto(asked) or (asked or "").strip().lower() == "claude-auto":
        model, _category, why = await auto_model.choose(
            principal, body.get("messages") or [], _has_images(body), bool(body.get("tools")))
        if model is None:
            return _error(400, f"auto could not pick a model: {why}", "invalid_request_error")
    else:
        model = await _model_named(asked)
    if not model:
        return _error(400, "Field 'model' is required", "invalid_request_error")
    if not access.can_use_model(principal, model):
        return _error(403, f"You do not have access to model '{model}'", "permission_error")
    try:
        await limits.check_request(principal)
    except HTTPException as e:
        return JSONResponse(compat.error_body(e.detail, "rate_limit_error"), status_code=e.status_code,
                            headers=e.headers)
    body = limits.cap_output(principal, body, fields=("max_tokens",))
    backend = await backends.resolve(model)
    if backend is None:
        return _error(404, f"Model '{model}' is not available on this gateway"
                           + (f" (asked for as '{asked}')" if asked != model else ""), "not_found_error")
    # Claude Code cannot work without tools, and a model without them fails on the first request
    # with nothing said about why; better to say so here
    if body.get("tools") and backend.capabilities and "tools" not in backend.capabilities:
        return _error(400, f"Model '{model}' cannot call tools, and Claude Code needs one that can."
                           " Choose a model listed with 'tools' in the gateway's model list.",
                      "invalid_request_error")
    # Same for pictures: llama.cpp without a projector answers a 500, and clients retry a 500 over
    # and over before giving up with nothing useful said. A 400 they show as it is.
    if _has_images(body) and backend.capabilities and "vision" not in backend.capabilities:
        seeing = sorted(name for name, b in (await backends.available_models()).items()
                        if "vision" in b.capabilities and access.can_use_model(principal, name))
        return _error(400, f"Model '{model}'" + (f" (asked for as '{asked}')" if asked != model else "")
                      + " cannot see images. Models that can: " + (", ".join(seeing) or "none")
                      + ". In the Claude desktop app, name one as claude-<model>, e.g. claude-gemma4-31b.",
                      "invalid_request_error")

    # ...and its answer the session's newest message
    if body.get("stream"):
        return code_sessions.answered(_streamed(request, principal, body, model, backend), reply)
    return code_sessions.answered(await _whole(request, principal, body, model, backend), reply)


async def _whole(request, principal, body, model, backend):
    started = time.monotonic()
    running = None
    try:
        base, running = await _target(backend, model)
    except RuntimeError as e:
        return _error(503, str(e), "overloaded_error")
    try:
        original = body
        body, _how = await _fitted(original, backend, base)
        if body is None:
            return JSONResponse(compat.too_long(backend.context_length, compat.estimate_tokens(original)), status_code=400)
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout(), connect=10)) as client:
            if _passes_through(backend):
                response = await client.post(f"{base}/v1/messages", headers=_upstream_headers(request, backend),
                                             json=dict(body, model=backend.upstream_model))
                sent = body
                if not response.is_success and compat.context_exceeded(response.text):
                    smaller, _how = await _fitted(original, backend, base, force=True)
                    if smaller is not None and smaller is not body:
                        sent = smaller
                        response = await client.post(f"{base}/v1/messages", headers=_upstream_headers(request, backend),
                                                     json=dict(smaller, model=backend.upstream_model))
                if response.status_code != 404:
                    usage = None
                    if response.is_success:
                        counts = response.json().get("usage") or {}
                        usage = {"prompt_tokens": counts.get("input_tokens") or 0,
                                 "completion_tokens": counts.get("output_tokens") or 0}
                        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                    await usage_log.record(principal, model, backend.name, ENDPOINT, False, response.status_code,
                                           usage, (time.monotonic() - started) * 1000,
                                           None if response.is_success else response.text[:500])
                    if not response.is_success and compat.context_exceeded(response.text):
                        return JSONResponse(compat.too_long(backend.context_length, compat.estimate_tokens(body)),
                                            status_code=400)
                    if response.is_success:
                        answer = response.json()
                        _scaled_usage(answer.get("usage"), _scale(original, sent))
                        return JSONResponse(answer)
                    # Error bodies go back as they came: Claude Code's retries read their wording
                    return Response(response.content, status_code=503 if response.status_code in (502, 504)
                                    else response.status_code, media_type="application/json")
                _no_messages.add(backend.name)
            # Translated: the backend's OpenAI chat API, and its answer turned back into a message
            request_body = compat.to_openai_request(dict(body, stream=False), backend.upstream_model)
            url = f"{base}/v1/chat/completions" if backend.kind in ("ollama", "llamacpp") \
                else backend.url("/chat/completions")
            response = await client.post(url, json=request_body, headers=backend.headers())
            if not response.is_success:
                await usage_log.record(principal, model, backend.name, ENDPOINT, False, response.status_code,
                                       None, (time.monotonic() - started) * 1000, response.text[:500])
                if compat.context_exceeded(response.text):
                    return JSONResponse(compat.too_long(backend.context_length, compat.estimate_tokens(body)),
                                        status_code=400)
                return _error(503 if response.status_code >= 500 else response.status_code, response.text[:500])
            payload = response.json()
            pace = usage_log.Pace(started)
            pace.saw_payload(payload)
            await usage_log.record(principal, model, backend.name, ENDPOINT, False, 200, payload.get("usage"),
                                   (time.monotonic() - started) * 1000, pace=pace)
            answer = compat.from_openai_response(payload, model)
            _scaled_usage(answer.get("usage"), _scale(original, body))
            return JSONResponse(answer)
    except httpx.HTTPError as e:
        await usage_log.record(principal, model, backend.name, ENDPOINT, False, 503, None,
                               (time.monotonic() - started) * 1000, str(e))
        return _error(503, f"Upstream error: {e}", "overloaded_error")
    finally:
        if running:
            running.in_flight = max(0, running.in_flight - 1)


def _streamed(request, principal, body, model, backend):
    """Answered at once and kept alive, as the OpenAI endpoint is (see openai_v1._streamed).

    Cloudflare closes a request after 100 seconds without a byte, and loading Qwen3-Next alone takes
    about 50. So the response starts straight away and an SSE comment goes out every
    HEARTBEAT_SECONDS of silence, always between events - comment lines are part of the format and
    every SSE reader skips them. A failure after the response has begun is sent as an `error` event,
    which is how the Messages API reports one mid-stream.
    """
    queue: asyncio.Queue = asyncio.Queue()
    state = {"running": None, "status": 200, "error": None, "usage": {}, "scale": 1.0}
    started = time.monotonic()
    pace = usage_log.Pace(started)
    headers = _upstream_headers(request, backend)

    async def relay(client, base, sending, retried=False):
        """Passed through: the backend's own events, unchanged. False when it has no /v1/messages."""
        url = f"{base}/v1/messages"
        async with client.stream("POST", url, headers=headers, json=dict(sending, model=backend.upstream_model)) as response:
            if response.status_code == 404:
                return False
            state["status"] = response.status_code
            if response.status_code >= 400:
                text = (await response.aread()).decode(errors="replace")
                state["error"] = text[:500]
                if compat.context_exceeded(text) and not retried:
                    # Nothing has been sent on yet: made smaller, and tried again before anyone sees it
                    smaller, _how = await _fitted(body, backend, base, force=True)
                    if smaller is not None and smaller is not sending:
                        state.update(status=200, error=None, scale=_scale(body, smaller))
                        return await relay(client, base, smaller, retried=True)
                if compat.context_exceeded(text):
                    await queue.put(compat.sse("error", compat.too_long(backend.context_length,
                                                                        compat.estimate_tokens(body))))
                    return True
                try:
                    error = json.loads(text)
                    await queue.put(compat.sse("error", error if error.get("type") == "error"
                                               else compat.error_body(text[:500])))
                except ValueError:
                    await queue.put(_sse_error(text[:500]))
                return True
            async for line in response.aiter_lines():
                if line:
                    state["usage"] = compat.usage_from_event(line, state["usage"])
                    pace.saw(line)
                await queue.put((_reported(line, state["scale"]) + "\n").encode())
            return True

    async def translate(client, base, sending):
        """The backend's OpenAI stream, turned into the Messages API's events as it arrives."""
        url = f"{base}/v1/chat/completions" if backend.kind in ("ollama", "llamacpp") \
            else backend.url("/chat/completions")
        translator = compat.StreamTranslator(model)
        request_body = compat.to_openai_request(dict(sending, stream=True), backend.upstream_model)
        async with client.stream("POST", url, json=request_body, headers=backend.headers()) as response:
            state["status"] = response.status_code
            if response.status_code >= 400:
                state["error"] = (await response.aread()).decode(errors="replace")[:500]
                if compat.context_exceeded(state["error"]):
                    await queue.put(compat.sse("error", compat.too_long(backend.context_length,
                                                                        compat.estimate_tokens(body))))
                    return
                await queue.put(_sse_error(state["error"]))
                return
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                state["usage"] = usage_log.usage_from_sse_line(line) or state["usage"]
                pace.saw_payload(chunk, streamed=True)
                events = translator.feed(chunk)
                if events:
                    await queue.put(_reported(events.decode(), state["scale"]).encode())
            await queue.put(_reported(translator.finish().decode(), state["scale"]).encode())

    async def produce():
        try:
            try:
                base, running = await _target(backend, model)
            except RuntimeError as e:
                state.update(status=503, error=str(e))
                await queue.put(_sse_error(str(e), "overloaded_error"))
                return
            state["running"] = running
            # Made to fit before it goes - the stream is already open, and the heartbeat keeps it so
            # while a summary is written
            sending, _how = await _fitted(body, backend, base)
            if sending is None:
                state.update(status=400, error="prompt is too long")
                await queue.put(compat.sse("error", compat.too_long(backend.context_length, compat.estimate_tokens(body))))
                return
            state["scale"] = _scale(body, sending)
            async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout(), connect=10)) as client:
                if _passes_through(backend):
                    if await relay(client, base, sending):
                        return
                    _no_messages.add(backend.name)
                await translate(client, base, sending)
        except httpx.HTTPError as e:
            state.update(status=503, error=str(e))
            await queue.put(_sse_error("upstream connection failed", "overloaded_error"))
        finally:
            await queue.put(_DONE)

    async def stream():
        worker = asyncio.create_task(produce())
        between_events = True
        try:
            yield b": connected\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    if between_events:
                        yield b": keep-alive\n\n"
                    continue
                if item is _DONE:
                    break
                # A translated batch always ends with its blank line; a relayed line ends an event
                # only when it is the blank line itself
                between_events = item.endswith(b"\n\n") or item in (b"\n", b"\r\n")
                yield item
        finally:
            if not worker.done():
                worker.cancel()
            running = state["running"]
            if running:
                running.in_flight = max(0, running.in_flight - 1)
            await usage_log.record(principal, model, backend.name, ENDPOINT, True, state["status"],
                                   state["usage"] or None, (time.monotonic() - started) * 1000, state["error"],
                                   pace=pace)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
