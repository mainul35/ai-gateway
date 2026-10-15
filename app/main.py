"""FastAPI gateway: OpenAI-compatible API in front of local and remote model backends."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import activity, bootstrap, db, knowledge, settings, sso
from app.engine.supervisor import supervisor
from app.coding import runner as coding_runner
from app.routers import admin, anthropic_v1, auth_sso, chat, coding, openai_v1, ui
from utils.ollama_client import ollama_host

# Without this our own INFO logs never reach the console; uvicorn only configures its own loggers
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
log = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.create_tables()
    await bootstrap.ensure_schema()
    await bootstrap.ensure_default_admin()
    log.info("Ollama backend: %s", ollama_host())
    if settings.master_key_is_generated():
        log.warning("No gateway.master.key configured; generated for this run: %s", settings.master_key())
    await supervisor.start_reaper()
    log.info("llama.cpp engine available: %s", supervisor.is_available())
    refresher = None
    try:
        await knowledge.ensure_schema()
        refresher = asyncio.create_task(knowledge.refresh_loop())
    except Exception as e:
        # A database without pgvector loses the knowledge base, not the gateway
        log.warning("knowledge base unavailable: %s", e)
    await coding_runner.start()
    yield
    await coding_runner.stop()
    if refresher:
        refresher.cancel()
    await supervisor.shutdown()
    await db.dispose()


# The API's own documentation is for admins (app/routers/ui.py serves it to them): open, it
# published every route of a public gateway to anyone who asked
app = FastAPI(title="Model Gateway", version="0.1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


# Sent with every response. No other site may frame these pages (clickjacking an admin's buttons),
# nothing is sniffed into a type it was not served as, no plugin content, no <base> rewriting the
# page's links, and the browser features the playground uses - camera, microphone, location - are
# this site's only. A CSP that also limits scripts needs the pages' inline scripts moved out first.
SECURITY_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'self'; object-src 'none'; base-uri 'none'",
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(self), microphone=(self), geolocation=(self), payment=(), usb=()",
}


class SecurityHeaders:
    """Plain ASGI rather than @app.middleware: that wraps every response, the gateway's long SSE
    streams included, in a way that has trouble with streaming and client disconnects."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        proto = headers.get(b"x-forwarded-proto", scope.get("scheme", "http").encode()).decode()
        https = proto.split(",")[0].strip().lower() == "https"

        async def with_headers(message):
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(name.lower().encode(), value.encode()) for name, value in SECURITY_HEADERS.items()
                         if name.lower().encode() not in present]
                if https and b"strict-transport-security" not in present:
                    extra.append((b"strict-transport-security", b"max-age=31536000"))
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        await self.app(scope, receive, with_headers)


app.add_middleware(activity.ActivityMiddleware)
app.add_middleware(SecurityHeaders)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # homelab: the API key is the access control, not the origin
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(openai_v1.router)
# Anthropic's Messages API, for Claude Code
app.include_router(anthropic_v1.router)
app.include_router(anthropic_v1.hello)
app.include_router(admin.router)
app.include_router(auth_sso.router)
app.include_router(chat.router)
app.include_router(coding.router)
app.include_router(ui.router)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/internal/activity", include_in_schema=False)
async def internal_activity(request: Request):
    """Whether the gateway is busy, for the machine's idle-sleep check (ops/idle-shutdown)."""
    if not activity.is_local_request(request):
        raise HTTPException(404)
    return activity.snapshot()


@app.post("/internal/unload", include_in_schema=False)
async def internal_unload(request: Request):
    """Unloads every model, for ops/idle-shutdown just before it suspends the machine."""
    if not activity.is_local_request(request):
        raise HTTPException(404)
    return await supervisor.unload_everything()
