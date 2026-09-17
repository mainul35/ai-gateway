"""OpenAI-compatible endpoints: what clients like Open WebUI and the OpenAI SDKs talk to."""
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from app import backends, settings, usage as usage_log
from app.auth import Principal, authenticate

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
            {"id": name, "object": "model", "created": 0, "owned_by": backend.name}
            for name, backend in sorted(models.items())
        ],
    }


async def _proxy(request: Request, principal: Principal, endpoint: str, path: str):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Request body must be JSON")

    model = body.get("model")
    if not model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Field 'model' is required")

    backend = await backends.resolve(model)
    if backend is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Model '{model}' is not available")

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
            await usage_log.record(principal, model, backend.name, endpoint, False, 502,
                                   None, (time.monotonic() - started) * 1000, str(e))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Upstream error: {e}")
        await client.aclose()
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
