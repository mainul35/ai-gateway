"""OpenAI-compatible endpoints: what clients like Open WebUI and the OpenAI SDKs talk to."""
import asyncio
import base64
import json
import time

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse

from app import access, api_images, anthropic_compat as compat, backends, context_fit, limits, settings, usage as usage_log
from app.engine.supervisor import supervisor
from app.auth import Principal, authenticate
from app.routers import code_sessions
from app.tools import auto_model, images

router = APIRouter(prefix="/v1", tags=["openai"])

PROXY_PATHS = {
    "chat_completions": "/chat/completions",
    "completions": "/completions",
    "embeddings": "/embeddings",
}


@router.get("/models")
async def list_models(request: Request, principal: Principal = Depends(authenticate)):
    # Anthropic's clients (the Claude desktop app) ask with an anthropic-version header and take only
    # Claude's model IDs: they get Claude's IDs, each standing for one of these (app/claude_desktop.py)
    if request.headers.get("anthropic-version"):
        from app import claude_desktop
        return await claude_desktop.catalogue(principal)
    models = await backends.available_models()
    return {
        "object": "list",
        # "auto" first: not a model but a choice - the gateway picks one per request (app/tools/auto_model.py)
        "data": [{"id": "auto", "object": "model", "created": 0, "owned_by": "gateway",
                  "capabilities": ["auto"]}] + [
            {"id": name, "object": "model", "created": 0, "owned_by": backend.name,
             "capabilities": sorted(backend.capabilities),
             # How much it reads at once, for clients that need telling (ai-gateway launch does)
             **({"context_length": backend.context_length} if backend.context_length else {})}
            for name, backend in sorted(models.items())
            if access.can_use_model(principal, name)
        ],
    }


async def _proxy(request: Request, principal: Principal, endpoint: str, path: str):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Request body must be JSON")
    reply = None
    if endpoint == "chat_completions":
        # A coding tool's request is its session's latest step (the playground's Code mode), and its
        # answer the session's newest message
        reply = code_sessions.observe(request.headers, principal, body, "chat")
    return code_sessions.answered(await forward(principal, endpoint, path, body), reply)


async def _fitted(body, backend, endpoint, force=False):
    """A chat made to fit the model's window (app/context_fit.py), so OpenCode's long sessions carry
    on instead of failing; None when nothing makes it fit. Other endpoints go as they are."""
    if endpoint != "chat_completions":
        return body
    fitted, _how = await context_fit.fit_for(body, backend, backend.url("/chat/completions"), force,
                                             context_fit.CHAT)
    return fitted


def _estimate(body):
    return context_fit.estimate(body, shape=context_fit.CHAT)


async def forward(principal: Principal, endpoint: str, path: str, body: dict, skip_access=False):
    """Sends an OpenAI request to the model's upstream, with access checks and usage accounting.

    Returns a JSONResponse, or a StreamingResponse when body["stream"] is set. skip_access is for the
    gateway's own helper calls (routing, search queries, summaries), which are not the user's choice of
    model and so are not theirs to be granted.
    """
    model = body.get("model")
    if not model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Field 'model' is required")

    # Image URLs are fetched here, not by the model server inside this network (app/api_images.py)
    if endpoint == "chat_completions" and body.get("messages"):
        body = dict(body)
        try:
            await api_images.inline(body["messages"])
        except api_images.ImageRefused as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    if auto_model.is_auto(model) and endpoint == "chat_completions":
        messages = body.get("messages") or []
        has_images = any(isinstance(m.get("content"), list) and
                         any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
                         for m in messages)
        model, category, why = await auto_model.choose(principal, messages, has_images, bool(body.get("tools")))
        if model is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"auto could not pick a model: {why}")
        body = dict(body, model=model)

    if not skip_access and not access.can_use_model(principal, model):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"You do not have access to model '{model}'")
    # The person's rate, day's tokens and longest answer (app/limits.py); the gateway's own helper
    # calls are not theirs to spend
    if not skip_access:
        await limits.check_request(principal)
        if endpoint in ("chat_completions", "completions"):
            body = limits.cap_output(principal, body)

    backend = await backends.resolve(model)
    if backend is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Model '{model}' is not available")

    streamed = bool(body.get("stream"))
    if streamed:
        return _streamed(principal, endpoint, path, body, model, backend)

    running = None
    if backend.kind == "llamacpp":
        # Loads the model (and frees VRAM by stopping another) before the request is forwarded
        running, problem = await supervisor.ensure_running(model)
        if problem:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, problem)
        backend = backends.Backend(name="llamacpp", kind="llamacpp", base_url=f"{running.base_url}/v1",
                                   upstream_model=model, context_length=backend.context_length)
        running.in_flight += 1
    elif backend.kind == "ollama":
        problem = await supervisor.make_room_for_ollama(backend.upstream_model)
        if problem:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, problem)

    started = time.monotonic()
    client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout(), connect=10))
    try:
        sending = await _fitted(body, backend, endpoint)
        if sending is None:
            return JSONResponse(compat.too_long_openai(backend.context_length, _estimate(body)), status_code=400)
        response = await client.post(backend.url(path), json=dict(sending, model=backend.upstream_model),
                                     headers=backend.headers())
        if not response.is_success and compat.context_exceeded(response.text):
            # The model counts more than the estimate: made smaller, and tried once more
            smaller = await _fitted(body, backend, endpoint, force=True)
            if smaller is not None and smaller is not sending:
                response = await client.post(backend.url(path), json=dict(smaller, model=backend.upstream_model),
                                             headers=backend.headers())
    except httpx.HTTPError as e:
        await usage_log.record(principal, model, backend.name, endpoint, False, 503,
                               None, (time.monotonic() - started) * 1000, str(e))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Upstream error: {e}")
    finally:
        # Also when the client hangs up and the request is cancelled: an engine left counted as busy
        # is never unloaded and holds up every swap. The count drops before the await, which a
        # cancelled request may not get past.
        if running:
            running.in_flight = max(0, running.in_flight - 1)
        await client.aclose()
    payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
    pace = usage_log.Pace(started)
    if isinstance(payload, dict):
        pace.saw_payload(payload)
    await usage_log.record(principal, model, backend.name, endpoint, False, response.status_code,
                           (payload or {}).get("usage"), (time.monotonic() - started) * 1000,
                           None if response.is_success else response.text[:500], pace=pace)
    # The Cloudflare tunnel swaps a 502 or 504 for a page of its own, so the upstream's reason
    # would never reach the client; 503 says the same thing and passes through. OpenAI's SDKs
    # retry any 5xx alike, so nothing a client does changes with it.
    answer = 503 if response.status_code in (502, 504) else response.status_code
    # Out of room for the conversation: OpenAI's own error for it, which agents answer by compacting
    if not response.is_success and compat.context_exceeded(response.text):
        return JSONResponse(compat.too_long_openai(backend.context_length, _estimate(body)), status_code=400)
    if payload is None:
        return JSONResponse({"error": {"message": response.text[:500]}}, status_code=answer)
    return JSONResponse(payload, status_code=answer)


HEARTBEAT_SECONDS = 15
_DONE = object()


def _sse_error(message):
    return f"data: {json.dumps({'error': {'message': message}})}\n\n".encode()


def _streamed(principal, endpoint, path, body, model, backend):
    """A streamed request, answered at once and kept alive until the model has something to say.

    The Cloudflare tunnel drops a request after 100 seconds without a byte from the gateway. Loading
    an engine model takes about 50 and reading a long conversation can take minutes before the first
    token, so a long chat used to end in a connection error. Now the response starts immediately,
    the model is loaded inside it, and a comment line (": keep-alive") goes out every
    HEARTBEAT_SECONDS of silence, always between events. Comment lines are part of the SSE format and
    every OpenAI client skips them, so to the client this is the same stream, only never silent.
    """
    queue: asyncio.Queue = asyncio.Queue()
    state = {"running": None, "status": 200, "error": None, "usage": None}
    started = time.monotonic()
    pace = usage_log.Pace(started)

    async def produce():
        client = None
        target = backend
        try:
            if target.kind == "llamacpp":
                running, problem = await supervisor.ensure_running(model)
                if problem:
                    state.update(status=503, error=problem)
                    await queue.put(_sse_error(problem))
                    return
                state["running"] = running
                running.in_flight += 1
                target = backends.Backend(name="llamacpp", kind="llamacpp", base_url=f"{running.base_url}/v1",
                                          upstream_model=model, context_length=backend.context_length)
            elif target.kind == "ollama":
                problem = await supervisor.make_room_for_ollama(target.upstream_model)
                if problem:
                    state.update(status=503, error=problem)
                    await queue.put(_sse_error(problem))
                    return
            # Made to fit before it goes; the heartbeat keeps the stream open while a summary is written
            sending = await _fitted(body, target, endpoint)
            client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout(), connect=10))
            for attempt in (1, 2):
                if sending is None:
                    error = compat.too_long_openai(target.context_length, _estimate(body))
                    state.update(status=400, error=error["error"]["message"])
                    await queue.put(f"data: {json.dumps(error)}\n\n".encode())
                    return
                upstream_body = dict(sending, model=target.upstream_model)
                # Token counts in the final chunk, so streamed requests are still accounted for
                upstream_body.setdefault("stream_options", {"include_usage": True})
                async with client.stream("POST", target.url(path), json=upstream_body, headers=target.headers()) as response:
                    state["status"] = response.status_code
                    if response.status_code >= 400:
                        state["error"] = (await response.aread()).decode(errors="replace")[:500]
                        if compat.context_exceeded(state["error"]):
                            # Nothing has gone to the client yet: made smaller, and tried once more
                            smaller = await _fitted(body, target, endpoint, force=True) if attempt == 1 else None
                            if smaller is not None and smaller is not sending:
                                sending = smaller
                                continue
                            error = compat.too_long_openai(target.context_length, _estimate(body))
                            await queue.put(f"data: {json.dumps(error)}\n\n".encode())
                            return
                        await queue.put(_sse_error(state["error"]))
                        return
                    state["error"] = None
                    async for line in response.aiter_lines():
                        if line:
                            state["usage"] = usage_log.usage_from_sse_line(line) or state["usage"]
                            pace.saw(line)
                        # aiter_lines strips newlines; SSE needs them back
                        await queue.put((line + "\n").encode())
                    return
        except httpx.HTTPError as e:
            state.update(status=503, error=str(e))
            await queue.put(_sse_error("upstream connection failed"))
        finally:
            if client is not None:
                await client.aclose()
            await queue.put(_DONE)

    async def stream():
        worker = asyncio.create_task(produce())
        between_events = True           # a heartbeat may only go where a blank line would be harmless
        try:
            yield b": connected\n\n"    # the response starts now, not when the first token is ready
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    if between_events:
                        yield b": keep-alive\n\n"
                    continue
                if item is _DONE:
                    break
                between_events = item in (b"\n", b"\r\n")
                yield item
        finally:
            if not worker.done():
                worker.cancel()         # the client went away: stop asking the model
            running = state["running"]
            if running:
                running.in_flight = max(0, running.in_flight - 1)
            await usage_log.record(principal, model, backend.name, endpoint, True, state["status"],
                                   state["usage"], (time.monotonic() - started) * 1000, state["error"],
                                   pace=pace)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/chat/completions")
async def chat_completions(request: Request, principal: Principal = Depends(authenticate)):
    return await _proxy(request, principal, "chat_completions", PROXY_PATHS["chat_completions"])


@router.post("/completions")
async def completions(request: Request, principal: Principal = Depends(authenticate)):
    return await _proxy(request, principal, "completions", PROXY_PATHS["completions"])


@router.post("/embeddings")
async def embeddings(request: Request, principal: Principal = Depends(authenticate)):
    return await _proxy(request, principal, "embeddings", PROXY_PATHS["embeddings"])


# --- images -------------------------------------------------------------------------

# OpenAI sizes mapped onto the shapes Flux renders well
_OPENAI_SIZES = {"1024x1024": "square", "1024x1536": "portrait", "1024x1792": "portrait",
                 "1536x1024": "landscape", "1792x1024": "wide", "auto": "square"}


async def _run_image_job(principal, prompt, size, source=None, strength=0.75):
    if not images.is_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Image generation is turned off on this server")
    if not access.can_generate_images(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to image generation")
    if not (prompt or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Field 'prompt' is required")
    started = time.monotonic()
    endpoint = "image_edits" if source else "image_generations"
    try:
        png, _seed = await images.generate(prompt, _OPENAI_SIZES.get(size or "auto", size),
                                           [source] if source else [], strength)
    except images.ImageError as e:
        await usage_log.record(principal, settings.image_model_name(), "comfyui", endpoint, False, 503,
                               None, (time.monotonic() - started) * 1000, str(e))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    await usage_log.record(principal, settings.image_model_name(), "comfyui", endpoint, False, 200,
                           None, (time.monotonic() - started) * 1000)
    return {"created": int(time.time()), "data": [{"b64_json": base64.b64encode(png).decode(),
                                                   "revised_prompt": prompt}]}


@router.post("/images/generations")
async def image_generations(request: Request, principal: Principal = Depends(authenticate)):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Request body must be JSON")
    if int(body.get("n") or 1) != 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only n=1 is supported")
    if body.get("response_format") == "url":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only response_format=b64_json is supported")
    return await _run_image_job(principal, body.get("prompt"), body.get("size"))


@router.post("/images/edits")
async def image_edits(image: UploadFile = File(...), prompt: str = Form(...), size: str | None = Form(None),
                      strength: float = Form(0.75), principal: Principal = Depends(authenticate)):
    data = await image.read(settings.max_upload_bytes() + 1)
    if len(data) > settings.max_upload_bytes():
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Image is too large")
    mime = image.content_type or "image/png"
    if not mime.startswith("image/"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The upload must be an image")
    return await _run_image_job(principal, prompt, size, (data, mime), strength)
