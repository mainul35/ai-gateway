"""FastAPI gateway: OpenAI-compatible API in front of local and remote model backends."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app import db, settings, sso
from app.engine.supervisor import supervisor
from app.routers import admin, auth_sso, openai_v1
from utils.ollama_client import ollama_host

log = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.create_tables()
    log.info("Ollama backend: %s", ollama_host())
    if settings.master_key_is_generated():
        log.warning("No gateway.master.key configured; generated for this run: %s", settings.master_key())
    await supervisor.start_reaper()
    log.info("llama.cpp engine available: %s", supervisor.is_available())
    yield
    await supervisor.shutdown()
    await db.dispose()


app = FastAPI(title="Model Gateway", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # homelab: the API key is the access control, not the origin
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(openai_v1.router)
app.include_router(admin.router)
app.include_router(auth_sso.router)


LANDING_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Model Gateway</title><style>
:root{color-scheme:dark}
body{margin:0;padding:2.5rem 1.5rem;background:#0f172a;color:#f1f5f9;
     font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6}
main{max-width:52rem;margin:0 auto}
h1{font-size:1.9rem;margin:0 0 .25rem;background:linear-gradient(135deg,#6366f1,#8b5cf6);
   -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
p.sub{color:#94a3b8;margin:0 0 2rem}
.card{background:#1e293b;border-radius:12px;padding:1.25rem 1.5rem;margin-bottom:1rem}
.card h2{font-size:1.05rem;margin:0 0 .75rem;color:#a5b4fc}
code{background:#334155;padding:.15rem .4rem;border-radius:5px;font-size:.9em}
pre{background:#0b1220;padding:.9rem 1rem;border-radius:8px;overflow-x:auto;margin:.5rem 0 0}
a{color:#818cf8}
ul{margin:.3rem 0;padding-left:1.2rem}
.pill{display:inline-block;padding:.15rem .6rem;border-radius:20px;font-size:.78rem;font-weight:600}
.on{background:rgba(16,185,129,.2);color:#10b981}
.off{background:rgba(245,158,11,.2);color:#f59e0b}
</style></head><body><main>
<h1>Model Gateway</h1>
<p class="sub">OpenAI-compatible API for local and remote models.</p>
<div class="card"><h2>Status</h2>
<ul>
<li>Ollama backend: <code>__OLLAMA_HOST__</code></li>
<li>Single sign-on: <span class="pill __SSO_CLASS__">__SSO_STATE__</span> __SSO_LINK__</li>
<li>Health check: <a href="/health">/health</a> &middot; API reference: <a href="/docs">/docs</a></li>
</ul></div>
<div class="card"><h2>Use it from any OpenAI client</h2>
<pre>base_url: __BASE_URL__/v1
api_key:  &lt;your key&gt;</pre>
<p class="sub" style="margin:.75rem 0 0">Point Open WebUI, the OpenAI SDK, or any agent at that base URL.</p></div>
<div class="card"><h2>Endpoints</h2>
<ul>
<li><code>GET /v1/models</code>, <code>POST /v1/chat/completions</code>, <code>/v1/completions</code>, <code>/v1/embeddings</code></li>
<li><code>GET /auth/login</code> then <code>POST /auth/keys</code> &mdash; sign in and mint your own key</li>
<li><code>/admin/users</code>, <code>/admin/keys</code>, <code>/admin/usage</code> &mdash; admin key required</li>
</ul></div>
</main></body></html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing(request: Request):
    configured = sso.is_configured()
    base_url = str(request.base_url).rstrip("/")
    html = (LANDING_PAGE
            .replace("__OLLAMA_HOST__", ollama_host())
            .replace("__SSO_CLASS__", "on" if configured else "off")
            .replace("__SSO_STATE__", "configured" if configured else "not configured")
            .replace("__SSO_LINK__", '<a href="/auth/login">Sign in</a>' if configured else "")
            .replace("__BASE_URL__", base_url))
    return HTMLResponse(html)


@app.get("/health")
async def health():
    return {"status": "ok"}
