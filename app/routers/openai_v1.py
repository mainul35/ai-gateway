"""OpenAI-compatible endpoints: what clients like Open WebUI and the OpenAI SDKs talk to."""
import asyncio
import base64
import json
import time

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse

from app import access, backends, settings, usage as usage_log
from app.engine.supervisor import supervisor
from app.auth import Principal, authenticate
from app.tools import images

router = APIRouter(prefix="/v1", tags=["openai"])

PROXY_PATHS = {
    "chat_completions": "/chat/completions",
    "completions": "/completions",
    "embeddings": "/embeddings",
}


@router.get("/models")
async def list_models(principal: Principal = Depends(authenticate)):
    models = await backends.available_models()
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": backend.name,
             "capabilities": sorted(backend.capabilities)}
            for name, backend in sorted(models.items())
            if access.can_use_model(principal, name)
        ],
    }


async def _proxy(request: Request, principal: Principal, endpoint: str, path: str):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Request body must be JSON")
    return await forward(principal, endpoint, path, body)


async def forward(principal: Principal, endpoint: str, path: str, body: dict, skip_access=False):
    """Sends an OpenAI request to the model's upstream, with access checks and usage accounting.

    Returns a JSONResponse, or a StreamingResponse when body["stream"] is set. skip_access is for the
    gateway's own helper calls (routing, search queries, summaries), which are not the user's choice of
    model and so are not theirs to be granted.
    """
    model = body.get("model")
    if not model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Field 'model' is required")

    if not skip_access and not access.can_use_model(principal, model):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"You do not have access to model '{model}'")

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
        backend = backends.Backend(name="llamacpp", kind="llamacpp",
                                   base_url=f"{running.base_url}/v1", upstream_model=model)
        running.in_flight += 1

    upstream_body = dict(body, model=backend.upstream_model)
    started = time.monotonic()
    client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout(), connect=10))
    try:
        response = await client.post(backend.url(path), json=upstream_body, headers=backend.headers())
    except httpx.HTTPError as e:
        await client.aclose()
        if running:
            running.in_flight = max(0, running.in_flight - 1)
        await usage_log.record(principal, model, backend.name, endpoint, False, 503,
                               None, (time.monotonic() - started) * 1000, str(e))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Upstream error: {e}")
    await client.aclose()
    if running:
        running.in_flight = max(0, running.in_flight - 1)
    payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
    await usage_log.record(principal, model, backend.name, endpoint, False, response.status_code,
                           (payload or {}).get("usage"), (time.monotonic() - started) * 1000,
                           None if response.is_success else response.text[:500])
    # The Cloudflare tunnel swaps a 502 or 504 for a page of its own, so the upstream's reason
    # would never reach the client; 503 says the same thing and passes through. OpenAI's SDKs
    # retry any 5xx alike, so nothing a client does changes with it.
    answer = 503 if response.status_code in (502, 504) else response.status_code
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
                target = backends.Backend(name="llamacpp", kind="llamacpp",
                                          base_url=f"{running.base_url}/v1", upstream_model=model)
            upstream_body = dict(body, model=target.upstream_model)
            # Token counts in the final chunk, so streamed requests are still accounted for
            upstream_body.setdefault("stream_options", {"include_usage": True})
            client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout(), connect=10))
            async with client.stream("POST", target.url(path), json=upstream_body, headers=target.headers()) as response:
                state["status"] = response.status_code
                if response.status_code >= 400:
                    state["error"] = (await response.aread()).decode(errors="replace")[:500]
                    await queue.put(_sse_error(state["error"]))
                    return
                async for line in response.aiter_lines():
                    if line:
                        state["usage"] = usage_log.usage_from_sse_line(line) or state["usage"]
                    # aiter_lines strips newlines; SSE needs them back
                    await queue.put((line + "\n").encode())
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
                                   state["usage"], (time.monotonic() - started) * 1000, state["error"])

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
