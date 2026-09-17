"""FastAPI gateway: OpenAI-compatible API in front of local and remote model backends."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db, settings
from app.routers import admin, auth_sso, openai_v1
from utils.ollama_client import ollama_host

log = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.create_tables()
    log.info("Ollama backend: %s", ollama_host())
    if settings.master_key_is_generated():
        log.warning("No gateway.master.key configured; generated for this run: %s", settings.master_key())
    yield
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


@app.get("/health")
async def health():
    return {"status": "ok"}
