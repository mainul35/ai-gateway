"""OpenAI-compatible endpoints: what clients like Open WebUI and the OpenAI SDKs talk to."""
import base64
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
    streamed = bool(body.get("stream"))
    if streamed:
        # Ask for token counts in the final chunk, so streamed requests are still accounted for
        upstream_body.setdefault("stream_options", {"include_usage": True})

    started = time.monotonic()
    client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout(), connect=10))

    if not streamed:
        try:
            response = await client.post(backend.url(path), json=upstream_body, headers=backend.headers())
        except httpx.HTTPError as e:
            await client.aclose()
            if running:
                running.in_flight = max(0, running.in_flight - 1)
            await usage_log.record(principal, model, backend.name, endpoint, False, 502,
                                   None, (time.monotonic() - started) * 1000, str(e))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Upstream error: {e}")
        await client.aclose()
        if running:
            running.in_flight = max(0, running.in_flight - 1)
        payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
        await usage_log.record(principal, model, backend.name, endpoint, False, response.status_code,
                               (payload or {}).get("usage"), (time.monotonic() - started) * 1000,
                               None if response.is_success else response.text[:500])
        if payload is None:
            return JSONResponse({"error": {"message": response.text[:500]}}, status_code=response.status_code)
        return JSONResponse(payload, status_code=response.status_code)

    async def stream():
        captured_usage = None
        status_code = 200
        error = None
        try:
            async with client.stream("POST", backend.url(path), json=upstream_body, headers=backend.headers()) as response:
                status_code = response.status_code
                if response.status_code >= 400:
                    error = (await response.aread()).decode(errors="replace")[:500]
                    yield f"data: {{\"error\": {{\"message\": {error!r}}}}}\n\n".encode()
                    return
                async for line in response.aiter_lines():
                    if line:
                        captured_usage = usage_log.usage_from_sse_line(line) or captured_usage
                    # aiter_lines strips newlines; SSE needs them back
                    yield (line + "\n").encode()
        except httpx.HTTPError as e:
            status_code, error = 502, str(e)
            yield b"data: {\"error\": {\"message\": \"upstream connection failed\"}}\n\n"
        finally:
            await client.aclose()
            if running:
                running.in_flight = max(0, running.in_flight - 1)
            await usage_log.record(principal, model, backend.name, endpoint, True, status_code,
                                   captured_usage, (time.monotonic() - started) * 1000, error)

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
        await usage_log.record(principal, settings.image_model_name(), "comfyui", endpoint, False, 502,
                               None, (time.monotonic() - started) * 1000, str(e))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
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
