"""OpenAI's Responses API (POST /v1/responses), for Codex and other clients that speak only it.

Ollama and llama.cpp both answer /v1/responses themselves, so a request is passed through to the
model's backend, as anthropic_v1 does for the Messages API: the gateway adds who may ask (access,
the person's limits), loads the model, makes a conversation too long for it fit (app/context_fit.py),
keeps a long stream alive through Cloudflare, and records the usage. Backends that do not have it are refused with a 400 rather than translated.
"""
import asyncio
import json
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import access, anthropic_compat as compat, backends, context_fit, limits, settings, usage as usage_log
from app.auth import Principal, authenticate
from app.routers import code_sessions
from app.routers.anthropic_v1 import _target

router = APIRouter(prefix="/v1", tags=["responses"])

ENDPOINT = "responses"
HEARTBEAT_SECONDS = 15
_DONE = object()


def _error(status_code, message, kind="invalid_request_error", headers=None):
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status_code,
                        headers=headers)


def _remote_images(value):
    """Image URLs in the input that are not inline data: the model server would fetch them from
    inside this network, with no check at all (see app/api_images.py)."""
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            url = value.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            if isinstance(url, str) and not url.startswith("data:"):
                yield url
        for item in value.values():
            yield from _remote_images(item)
    elif isinstance(value, list):
        for item in value:
            yield from _remote_images(item)


async def _fitted(body, backend, base, force=False):
    """The request made to fit the model's window (app/context_fit.py), so Codex's long sessions
    carry on instead of failing; None when nothing makes it fit."""
    fitted, _how = await context_fit.fit_for(body, backend, f"{base}/v1/chat/completions", force,
                                             context_fit.RESPONSES)
    return fitted


def _too_long(body, backend):
    """OpenAI's error for it, which Codex answers by compacting the conversation."""
    error = compat.too_long_openai(backend.context_length, context_fit.estimate(body, shape=context_fit.RESPONSES))
    error["error"]["param"] = "input"
    return error


def _too_long_event(body, backend):
    """The same, as the stream's failure event, the way OpenAI's Responses API reports it."""
    failed = {"type": "response.failed",
              "response": {"id": "resp_gateway", "object": "response", "status": "failed",
                           "error": _too_long(body, backend)["error"]}}
    return f"event: response.failed\ndata: {json.dumps(failed)}\n\n".encode()


def _usage(payload):
    counts = (payload or {}).get("usage") or {}
    prompt, completion = counts.get("input_tokens") or 0, counts.get("output_tokens") or 0
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


@router.post("/responses")
async def responses(request: Request, principal: Principal = Depends(authenticate)):
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "Request body must be JSON")
    # A coding tool's request is its session's latest step (the playground's Code mode)
    code_sessions.observe(request.headers, principal, body, "responses")
    model = (body.get("model") or "").strip()
    if not model:
        return _error(400, "Field 'model' is required")
    if not access.can_use_model(principal, model):
        return _error(403, f"You do not have access to model '{model}'", "permission_error")
    try:
        await limits.check_request(principal)
    except HTTPException as e:
        return _error(e.status_code, e.detail, "rate_limit_error", e.headers)
    body = limits.cap_output(principal, body, fields=("max_output_tokens",))
    if next(_remote_images(body.get("input")), None):
        return _error(400, "Send images inline (data: URLs); image URLs are not fetched on this endpoint")
    backend = await backends.resolve(model)
    if backend is None:
        return _error(404, f"Model '{model}' is not available on this gateway", "not_found_error")
    if backend.kind not in ("ollama", "llamacpp"):
        return _error(400, f"Model '{model}' is not served by a backend that speaks the Responses API")
    if body.get("tools") and backend.capabilities and "tools" not in backend.capabilities:
        return _error(400, f"Model '{model}' cannot call tools; choose one listed with 'tools'")
    body = dict(body, model=backend.upstream_model)
    if body.get("stream"):
        return _streamed(principal, body, model, backend)
    return await _whole(principal, body, model, backend)


async def _whole(principal, body, model, backend):
    started, running = time.monotonic(), None
    try:
        base, running = await _target(backend, model)
    except RuntimeError as e:
        return _error(503, str(e), "server_error")
    try:
        sending = await _fitted(body, backend, base)
        if sending is None:
            return JSONResponse(_too_long(body, backend), status_code=400)
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout(), connect=10)) as client:
            response = await client.post(f"{base}/v1/responses", json=sending, headers=backend.headers())
            if not response.is_success and compat.context_exceeded(response.text):
                # The model counts more than the estimate: made smaller, and tried once more
                smaller = await _fitted(body, backend, base, force=True)
                if smaller is not None and smaller is not sending:
                    response = await client.post(f"{base}/v1/responses", json=smaller, headers=backend.headers())
        if not response.is_success and compat.context_exceeded(response.text):
            await usage_log.record(principal, model, backend.name, ENDPOINT, False, 400, None,
                                   (time.monotonic() - started) * 1000, response.text[:500])
            return JSONResponse(_too_long(body, backend), status_code=400)
        usage = _usage(response.json()) if response.is_success else None
        await usage_log.record(principal, model, backend.name, ENDPOINT, False, response.status_code, usage,
                               (time.monotonic() - started) * 1000,
                               None if response.is_success else response.text[:500])
        # 502 and 504 are replaced by Cloudflare's own page; 503 passes through with its detail
        return Response(response.content, status_code=503 if response.status_code in (502, 504)
                        else response.status_code, media_type="application/json")
    except (httpx.HTTPError, ValueError) as e:
        await usage_log.record(principal, model, backend.name, ENDPOINT, False, 503, None,
                               (time.monotonic() - started) * 1000, str(e))
        return _error(503, f"Upstream error: {e}", "server_error")
    finally:
        if running:
            running.in_flight = max(0, running.in_flight - 1)


def _streamed(principal, body, model, backend):
    """The backend's own events, passed on as they come; an SSE comment every HEARTBEAT_SECONDS of
    silence (between events only) keeps Cloudflare from closing a long wait at 100 seconds."""
    queue: asyncio.Queue = asyncio.Queue()
    state = {"running": None, "status": 200, "error": None, "usage": None}
    started = time.monotonic()

    async def produce():
        try:
            try:
                base, state["running"] = await _target(backend, model)
            except RuntimeError as e:
                state.update(status=503, error=str(e))
                await queue.put(f"event: error\ndata: {json.dumps({'type': 'error', 'message': str(e)})}\n\n".encode())
                return
            # Made to fit before it goes; the heartbeat keeps the stream open while a summary is written
            sending = await _fitted(body, backend, base)
            async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout(), connect=10)) as client:
                for attempt in (1, 2):
                    if sending is None:
                        state.update(status=400, error="context length exceeded")
                        await queue.put(_too_long_event(body, backend))
                        return
                    async with client.stream("POST", f"{base}/v1/responses", json=sending,
                                             headers=backend.headers()) as response:
                        state["status"] = response.status_code
                        if response.status_code >= 400:
                            text = (await response.aread()).decode(errors="replace")
                            state["error"] = text[:500]
                            if compat.context_exceeded(text):
                                # Nothing has gone to the client yet: made smaller, and tried once more
                                smaller = await _fitted(body, backend, base, force=True) if attempt == 1 else None
                                if smaller is not None and smaller is not sending:
                                    sending = smaller
                                    continue
                                await queue.put(_too_long_event(body, backend))
                                return
                            await queue.put(f"event: error\ndata: {json.dumps({'type': 'error', 'message': text[:500]})}\n\n".encode())
                            return
                        state["error"] = None
                        async for line in response.aiter_lines():
                            if line.startswith("data:") and '"response.completed"' in line:
                                try:
                                    state["usage"] = _usage(json.loads(line[5:].strip()).get("response"))
                                except ValueError:
                                    pass
                            await queue.put((line + "\n").encode())
                        return
        except httpx.HTTPError as e:
            state.update(status=503, error=str(e))
            await queue.put(b'event: error\ndata: {"type": "error", "message": "upstream connection failed"}\n\n')
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
                between_events = item.endswith(b"\n\n") or item in (b"\n", b"\r\n")
                yield item
        finally:
            if not worker.done():
                worker.cancel()
            if state["running"]:
                state["running"].in_flight = max(0, state["running"].in_flight - 1)
            await usage_log.record(principal, model, backend.name, ENDPOINT, True, state["status"],
                                   state["usage"], (time.monotonic() - started) * 1000, state["error"])

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
