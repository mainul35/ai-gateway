"""Admin API: users, API keys, usage, and the models themselves."""
import asyncio
import json
import logging
import os
import re
import threading
from datetime import timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import access, activity, backends, catalogue, fitting, knowledge, power, settings, usage as usage_log
from app import live_metrics as live, proven
from app.tools import jobs, mcp_client, mcp_config
from app.auth import Principal, generate_key, hash_key, require_admin, require_manager
from app import config_writer
from app.engine.supervisor import supervisor
from app.engine import builds as engine_builds, gguf_meta, profiles as engine_profiles, serving
from utils import hf_client, hf_download
from utils.ollama_client import (StreamCancellation, check_ollama_status, ollama_host,
                                 stream_create_model, stream_pull_model)
from utils.system_info import get_system_info, measure_hardware
from app.db import get_session
from app.models import ApiKey, UsageRecord, User, utcnow

log = logging.getLogger("admin")

router = APIRouter(prefix="/admin", tags=["admin"])


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    email: str | None = None
    role: str = Field(default="user", pattern="^(user|manager|admin)$")
    model_access: str | None = Field(default=None, pattern="^(all|selected|public|none)$")
    allowed_models: list[str] | None = None


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern="^(user|manager|admin)$")
    model_access: str | None = Field(default=None, pattern="^(all|selected|public|none)$")
    allowed_models: list[str] | None = None
    is_active: bool | None = None


class KeyIn(BaseModel):
    user: str = Field(min_length=1, description="User name the key belongs to")
    name: str = Field(default="default", max_length=128)


def _user_json(user):
    return {
        "id": user.id, "name": user.name, "email": user.email, "role": user.role,
        "is_active": user.is_active, "created_at": user.created_at.isoformat(),
        "model_access": user.model_access or "all",
        "allowed_models": access.parse_patterns(user.allowed_models),
        "sign_in": "local password" if user.password_hash else "single sign-on",
        # "pending": a first SSO sign-in waiting for an admin to approve it
        "approval": user.approval,
        # What the auth server said at their last sign-in, kept whole and shown as it arrived.
        # While permissions are being wired up to it, "what does it actually send us" is the
        # question, and a gateway that cannot answer it is no help at all.
        "claims": json.loads(user.claims) if user.claims else None,
        "claims_seen_at": user.claims_seen_at.isoformat() if user.claims_seen_at else None,
        "tool_capabilities": access.capabilities_of(user),
        # No part of the key itself: the user list is for managing keys, not reading them, and a
        # prefix on every row is a piece of somebody's credential on an admin's screen
        "keys": [
            {"id": k.id, "name": k.name, "is_active": k.is_active,
             "created_at": k.created_at.isoformat() if k.created_at else None,
             "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None}
            for k in user.api_keys
        ],
    }


async def _get_user(session, name):
    result = await session.execute(select(User).options(selectinload(User.api_keys)).where(User.name == name))
    return result.scalar_one_or_none()


async def _other_active_admins(session, user_id):
    result = await session.execute(
        select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True), User.id != user_id)
    )
    return result.scalar() or 0


def _check_can_manage(principal, target):
    if not access.can_manage(principal, target):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Managers can only manage accounts with the user role")
    # A sign-in request is the admins' to decide: until then nobody gives it keys or access
    if target.approval == "pending" and not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can act on a sign-in request")


def _check_grant_allowed(principal, model_access, allowed_models):
    """A manager can only hand out access they have themselves, so the role cannot escalate privileges."""
    if principal.is_admin:
        return
    own = principal.user.model_access or "all"
    if own == "all":
        return
    if model_access == "all":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot grant access to all models")
    for entry in allowed_models or []:
        if access.is_pattern(entry):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"{entry} is a pattern; with restricted access you can only grant exact model names")
        if not access.can_use_model(principal, entry):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"You cannot grant {entry}: you do not have it yourself")


@router.get("/users")
async def list_users(_: Principal = Depends(require_manager), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(User).options(selectinload(User.api_keys)).order_by(User.id))
    return [_user_json(u) for u in result.scalars().all()]


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserIn, principal: Principal = Depends(require_manager),
                      session: AsyncSession = Depends(get_session)):
    if payload.role != "user" and not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can create managers or admins")
    if await _get_user(session, payload.name):
        raise HTTPException(status.HTTP_409_CONFLICT, f"User {payload.name} already exists")
    model_access = payload.model_access or access.default_access()
    _check_grant_allowed(principal, model_access, payload.allowed_models)
    user = User(name=payload.name, email=payload.email, role=payload.role, model_access=model_access,
                allowed_models=access.serialize_patterns(payload.allowed_models))
    session.add(user)
    await session.commit()
    await session.refresh(user, attribute_names=["api_keys"])
    return _user_json(user)


@router.patch("/users/{name}")
async def update_user(name: str, payload: UserPatch, principal: Principal = Depends(require_manager),
                      session: AsyncSession = Depends(get_session)):
    user = await _get_user(session, name)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User {name} not found")
    _check_can_manage(principal, user)

    if payload.role is not None and payload.role != user.role:
        if not principal.is_admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can change roles")
        if user.role == "admin" and await _other_active_admins(session, user.id) == 0:
            raise HTTPException(status.HTTP_409_CONFLICT, "This is the last active admin; promote someone else first")
        user.role = payload.role

    if payload.model_access is not None or payload.allowed_models is not None:
        model_access = payload.model_access or user.model_access or "all"
        allowed = payload.allowed_models if payload.allowed_models is not None \
            else access.parse_patterns(user.allowed_models)
        _check_grant_allowed(principal, model_access, allowed)
        user.model_access = model_access
        user.allowed_models = access.serialize_patterns(allowed)

    if payload.is_active and user.approval == "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "This is a sign-in request: approve it instead")

    if payload.is_active is not None and payload.is_active != user.is_active:
        if not payload.is_active:
            if principal.user is not None and principal.user.id == user.id:
                raise HTTPException(status.HTTP_409_CONFLICT, "You cannot disable your own account")
            if user.role == "admin" and await _other_active_admins(session, user.id) == 0:
                raise HTTPException(status.HTTP_409_CONFLICT, "This is the last active admin")
            for key in user.api_keys:
                key.is_active = False
        user.is_active = payload.is_active

    await session.commit()
    await session.refresh(user, attribute_names=["api_keys"])
    return _user_json(user)


@router.post("/users/{name}/approve")
async def approve_user(name: str, _: Principal = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    """Lets a first SSO sign-in in, with the public models; more can be granted after."""
    user = await _get_user(session, name)
    if user is None or user.approval != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No sign-in request from {name}")
    user.approval = None
    user.is_active = True
    await session.commit()
    await session.refresh(user, attribute_names=["api_keys"])
    return _user_json(user)


@router.post("/users/{name}/reject")
async def reject_user(name: str, _: Principal = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    """Turns a sign-in request down. The request is removed; signing in again makes a new one."""
    user = await _get_user(session, name)
    if user is None or user.approval != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No sign-in request from {name}")
    await session.delete(user)
    await session.commit()
    return {"rejected": name}


@router.delete("/users/{name}")
async def deactivate_user(name: str, principal: Principal = Depends(require_manager),
                          session: AsyncSession = Depends(get_session)):
    return await update_user(name, UserPatch(is_active=False), principal, session)


@router.delete("/users/{name}/permanently")
async def delete_user_permanently(name: str, principal: Principal = Depends(require_admin),
                                  session: AsyncSession = Depends(get_session)):
    """A person removed for good, not just disabled: their keys, conversations, files, memory, feedback,
    GitHub connection, project notes and shared pages go with them (the tables cascade), and so does
    what was indexed from their repositories. Usage records stay, no longer naming anyone, so the
    accounting adds up; coding tasks and projects stay too. Admins only, never yourself, never the
    last active admin. Not undoable."""
    user = await _get_user(session, name)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User {name} not found")
    if principal.user is not None and principal.user.id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete your own account")
    if user.role == "admin":
        admins = (await session.execute(select(func.count()).select_from(User).where(
            User.role == "admin", User.is_active.is_(True), User.id != user.id))).scalar()
        if not admins:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is the last active admin")
    user_id = user.id
    try:
        sources = await knowledge.forget_user(user_id)
    except Exception as e:                  # an index that cannot be reached does not keep the person
        log.warning("could not clear the index of user %s: %s", name, e)
        sources = 0
    await session.delete(user)
    await session.commit()
    log.info("user %s (id %s) deleted permanently by %s; %s indexed source(s) removed", name, user_id,
             getattr(principal.user, "name", None) or "master-key", sources)
    return {"deleted": name, "indexed_sources_removed": sources}


@router.post("/keys", status_code=status.HTTP_201_CREATED)
async def create_key(payload: KeyIn, principal: Principal = Depends(require_manager),
                     session: AsyncSession = Depends(get_session)):
    user = await _get_user(session, payload.user)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User {payload.user} not found")
    _check_can_manage(principal, user)
    key = generate_key()
    api_key = ApiKey(user_id=user.id, name=payload.name, key_hash=hash_key(key), key_prefix=key[:12])
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    # The only time the full key is returned
    return {"id": api_key.id, "user": user.name, "name": api_key.name, "key": key}


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: int, principal: Principal = Depends(require_manager),
                     session: AsyncSession = Depends(get_session)):
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    _check_can_manage(principal, await session.get(User, api_key.user_id))
    api_key.is_active = False
    await session.commit()
    return {"revoked": key_id}


@router.get("/usage")
async def usage_summary(days: int = 7, _: Principal = Depends(require_manager),
                        session: AsyncSession = Depends(get_session)):
    """Usage per user and model; the dashboard groups it by user, largest first, with the per-model
    rows as each user's breakdown."""
    since = utcnow() - timedelta(days=days)
    result = await session.execute(
        select(User.name, UsageRecord.model, func.count(UsageRecord.id),
               func.sum(UsageRecord.prompt_tokens), func.sum(UsageRecord.completion_tokens),
               func.count(UsageRecord.id).filter(UsageRecord.status_code >= 400),
               func.max(UsageRecord.created_at))
        .join(User, User.id == UsageRecord.user_id, isouter=True)
        .where(UsageRecord.created_at >= since)
        .group_by(User.name, UsageRecord.model)
        .order_by((func.sum(UsageRecord.prompt_tokens) + func.sum(UsageRecord.completion_tokens)).desc())
    )
    return {
        "since": since.isoformat(),
        "rows": [
            {"user": name or "master-key", "model": model, "requests": requests,
             "prompt_tokens": int(prompt or 0), "completion_tokens": int(completion or 0),
             "failed": int(failed or 0), "last_used": last.isoformat() if last else None}
            for name, model, requests, prompt, completion, failed, last in result.all()
        ],
    }


@router.get("/usage/hourly")
async def usage_hourly(hours: int = 24, _: Principal = Depends(require_manager),
                       session: AsyncSession = Depends(get_session)):
    """Requests, failures and tokens per hour, oldest first, every hour present even when empty."""
    hours = max(1, min(hours, 24 * 14))
    now = utcnow().replace(minute=0, second=0, microsecond=0)
    since = now - timedelta(hours=hours - 1)
    # In UTC whatever the database's own time zone, so the hours match the ones counted out below
    hour = func.date_trunc("hour", func.timezone("UTC", UsageRecord.created_at))
    result = await session.execute(
        select(hour, func.count(UsageRecord.id),
               func.count(UsageRecord.id).filter(UsageRecord.status_code >= 400),
               func.sum(UsageRecord.prompt_tokens), func.sum(UsageRecord.completion_tokens))
        .where(UsageRecord.created_at >= since).group_by(hour))
    found = {when.replace(tzinfo=now.tzinfo) if when.tzinfo is None else when: row
             for when, *row in result.all()}
    buckets = []
    for i in range(hours):
        when = since + timedelta(hours=i)
        requests, failed, prompt, completion = found.get(when, (0, 0, 0, 0))
        buckets.append({"hour": when.isoformat(), "requests": requests, "failed": int(failed or 0),
                        "prompt_tokens": int(prompt or 0), "completion_tokens": int(completion or 0)})
    return {"hours": buckets}


@router.get("/metrics")
async def live_metrics(history: bool = False, _: Principal = Depends(require_admin)):
    """CPU, RAM and each GPU now, for the dashboard's live panel (app/live_metrics.py)."""
    return await asyncio.to_thread(live.current, history)


# --- the model list, and deleting from it ------------------------------------------------------

# Settings that name a model. Deleting one of these breaks a part of the gateway quietly, days later,
# so it is said plainly in the list rather than discovered afterwards.
MODEL_SETTINGS = [
    ("router.model", "deciding what a playground message asks for"),
    ("memory.model", "writing conversation memory"),
    ("images.prompt.model", "rewriting requests into something an image model can draw"),
]
HEALTH_DAYS = 90        # the window the counts are over
QUIET_DAYS = 30         # unused for longer than this is worth mentioning
FEW_REQUESTS = 5        # fewer than this in the window is "barely used"
SHAKY_FAILURES = 0.2    # a fifth of requests failing is worth a warning


async def _usage_by_model(session, since):
    """How each model has behaved, plus the words of its most recent failure."""
    stats = await usage_log.by_model(session, since)
    # One recent failure per model says more about what is wrong than a count does
    failures = (await session.execute(
        select(UsageRecord.model, UsageRecord.error, UsageRecord.status_code, UsageRecord.created_at)
        .where(UsageRecord.created_at >= since, UsageRecord.status_code >= 400)
        .order_by(UsageRecord.created_at.desc()).limit(400)
    )).all()
    for model, error, code, when in failures:
        entry = stats.get(model)
        if entry and entry["last_error"] is None:
            entry["last_error"] = {"status": code, "text": _plain_error(error),
                                   "at": when.isoformat() if when else None}
    return stats


def _plain_error(error):
    """An upstream's error as a sentence. They answer with JSON, which reads badly on a card."""
    text = (error or "").strip()
    if text.startswith("{"):
        try:
            body = json.loads(text)
            inner = body.get("error") if isinstance(body.get("error"), dict) else body
            text = (inner or {}).get("message") or text
        except (ValueError, AttributeError):
            pass
    return text[:300]


async def _granted_to(session):
    """Which users have been granted each model by name, so deleting one does not silently strand them."""
    granted = {}
    users = (await session.execute(
        select(User.name, User.allowed_models)
        .where(User.model_access == "selected", User.is_active.is_(True))
    )).all()
    for name, patterns in users:
        for pattern in access.parse_patterns(patterns):
            if "*" not in pattern and "?" not in pattern:
                granted.setdefault(pattern, []).append(name)
    return granted


def _used_for(name):
    """The gateway's own jobs that name this model in the settings."""
    return [purpose for key, purpose in MODEL_SETTINGS if (settings.get(key) or "").strip() == name]


def _notes(name, backend, usage, used_for, granted, reachable):
    """What an admin needs to know before pressing Delete, worst first."""
    notes = []
    for purpose in used_for:
        notes.append({"level": "stop", "text": f"The gateway uses this model for {purpose}. "
                                               f"Deleting it stops that working."})
    if granted:
        who = ", ".join(sorted(granted)[:4]) + (f" and {len(granted) - 4} more" if len(granted) > 4 else "")
        notes.append({"level": "stop" if len(granted) > 1 else "warn",
                      "text": f"Granted by name to {who}. They lose access to it."})
    if not reachable:
        notes.append({"level": "warn", "text": "Not reachable right now: "
                      + ("the Ollama server is not answering, so this is about the server rather than "
                         "the model" if backend.kind == "ollama"
                         else "the llama.cpp binary is missing, so this profile cannot start.")})

    requests = usage["requests"]
    failures = usage["failures"]
    if requests == 0:
        notes.append({"level": "info", "text": f"Not used once in the last {HEALTH_DAYS} days."})
    elif usage.get("_last"):
        quiet = (utcnow() - usage["_last"]).days
        if quiet >= QUIET_DAYS:
            notes.append({"level": "info", "text": f"Last used {quiet} days ago."})
        elif requests < FEW_REQUESTS:
            notes.append({"level": "info",
                          "text": f"Barely used: {requests} request{'' if requests == 1 else 's'} "
                                  f"in {HEALTH_DAYS} days."})
    if failures and requests and failures / requests >= SHAKY_FAILURES:
        share = round(100 * failures / requests)
        note = f"{failures} of {requests} requests failed ({share}%)."
        if usage["last_error"] and usage["last_error"]["text"]:
            note += f" Most recently: {usage['last_error']['text'][:160]}"
        notes.append({"level": "warn", "text": note})
    elif failures:
        notes.append({"level": "info", "text": f"{failures} failed request{'' if failures == 1 else 's'} "
                                               f"in {HEALTH_DAYS} days."})
    return notes


PROVIDER_LABELS = {"ollama": "Ollama", "llamacpp": "llama.cpp", "vllm": "vLLM", "sglang": "SGLang"}


def _deletability(backend):
    """Models Ollama holds and the llama.cpp ones - downloaded here or in config/engines.yaml - can be
    deleted from here; the rest are someone else's to remove."""
    if backend.kind in ("ollama", "llamacpp"):
        return True, ""
    return False, "This one is defined in config/models.yaml. Remove the entry there."


# Where the Models page downloads llama.cpp models to; nothing outside it is ever deleted from here
MODELS_ROOT = os.path.join(os.path.expanduser("~"), "models")
SPLIT_PART = re.compile(r"^(.*)-\d{5}-of-(\d{5})\.gguf$")


def _gguf_parts(model_path):
    """A model's files: a GGUF's every part when it is split in several (-00001-of-00003.gguf), else
    itself; for vLLM and SGLang, whose model is a folder of safetensors, every file in the folder."""
    if model_path and os.path.isdir(model_path):
        return [os.path.join(top, name) for top, _, names in os.walk(model_path) for name in names]
    split = SPLIT_PART.match(os.path.basename(model_path or ""))
    if not split:
        return [model_path] if model_path else []
    folder = os.path.dirname(model_path)
    return [os.path.join(folder, f"{split.group(1)}-{i:05d}-of-{split.group(2)}.gguf")
            for i in range(1, int(split.group(2)) + 1)]


def _engine_files(entry, others):
    """The files a llama.cpp model is - its GGUF, every part of it, and its image reader - less any
    another profile still uses: qwen3.6-256k and qwen3.6-1m are one GGUF and one reader. Only files
    under MODELS_ROOT."""
    shared = {os.path.realpath(f) for p in others.values() for f in _gguf_parts(p.model_path) + [p.mmproj] if f}
    files = _gguf_parts(entry.get("model_path")) + ([entry["mmproj"]] if entry.get("mmproj") else [])
    root = os.path.realpath(MODELS_ROOT) + os.sep
    return [f for f in files if os.path.realpath(f) not in shared
            and os.path.realpath(f).startswith(root) and os.path.isfile(f)]


async def _delete_engine_model(name):
    """Stops a llama.cpp model, takes its profile out (engine_profiles.remove_profile) and deletes its
    files: (freed bytes, problem). Not while it is answering someone."""
    running = next((r for r in supervisor.status().get("running") or [] if r.get("name") == name), None)
    if running and running.get("in_flight"):
        return 0, f"{name} is answering a request. Try again when it has finished."
    profile = engine_profiles.load_profiles().get(name)
    if profile is None:
        return 0, f"There is no llama.cpp model called {name}"
    await supervisor.stop(name)
    engine_profiles.remove_profile(name)
    entry = {"model_path": profile.model_path, "mmproj": profile.mmproj}
    freed, folders = 0, set()
    for path in _engine_files(entry, engine_profiles.load_profiles()):
        try:
            size = os.path.getsize(path)
            os.remove(path)
            freed += size
            folders.add(os.path.dirname(path))
        except OSError as e:
            log.warning("Could not delete %s: %s", path, e)
    for folder in sorted(folders, key=len, reverse=True):      # a model folder's own folders first
        if os.path.realpath(folder) != os.path.realpath(MODELS_ROOT):
            try:
                os.rmdir(folder)        # only when empty
            except OSError:
                pass
    backends.forget_discovery()
    return freed, None


@router.get("/models")
async def list_models_for_admin(_: Principal = Depends(require_admin),
                                session: AsyncSession = Depends(get_session)):
    """Every model, what it is for, and everything that argues for or against deleting it."""
    # Always asked afresh: this page is read to decide what to delete, and it is read again straight
    # after deleting. A cached list would show a model that is already gone, or hide one just pulled.
    models = await backends.available_models(force_refresh=True)
    since = utcnow() - timedelta(days=HEALTH_DAYS)
    usage = await _usage_by_model(session, since)
    granted = await _granted_to(session)
    engines = supervisor.status()
    # A llama.cpp profile that is not started is still usable: it is started on the first request.
    # Only a missing binary means nothing in that group can run at all.
    loaded = {model["name"] for model in engines.get("running") or [] if model.get("running")}
    running = {model["name"]: model for model in engines.get("running") or [] if model.get("running")}
    engines_available = engines.get("available", False)
    ollama_up = check_ollama_status().get("status") == "running"

    listed = []
    for name, backend in sorted(models.items()):
        details = backend.details or {}
        stats = usage.get(name, {"requests": 0, "failures": 0, "last_used": None, "_last": None,
                                 "avg_latency_ms": 0, "tokens": 0, "last_error": None})
        if backend.kind == "ollama":
            reachable = ollama_up
        elif backend.kind == "llamacpp":
            reachable = engines_available
        else:
            reachable = True
        used_for = _used_for(name)
        can_delete, why_not = _deletability(backend)
        listed.append({
            "name": name,
            "backend": backend.kind,
            "owned_by": backend.name,
            # Who serves it, as the page groups the models: Ollama, llama.cpp, vLLM, SGLang or an API
            "provider": PROVIDER_LABELS.get(backend.name, backend.name),
            "size_bytes": backend.size_bytes,
            "parameter_size": details.get("parameter_size", ""),
            "quantization": details.get("quantization_level", ""),
            "family": details.get("family", ""),
            "context_length": backend.context_length,
            "capabilities": sorted(backend.capabilities),
            "modified_at": backend.modified_at,
            "description": catalogue.describe(
                name, override=backend.description, family=details.get("family", ""),
                parameter_size=details.get("parameter_size", ""),
                quantization=details.get("quantization_level", ""),
                context_length=backend.context_length, capabilities=backend.capabilities,
                backend=backend.kind, engine=backend.name),
            "usage": {k: v for k, v in stats.items() if not k.startswith("_")},
            "used_for": used_for,
            "granted_to": sorted(granted.get(name, [])),
            "reachable": reachable,
            "loaded": name in loaded,
            "can_delete": can_delete,
            "delete_note": why_not,
            "notes": _notes(name, backend, stats, used_for, granted.get(name, []), reachable),
            "mtp": _mtp_state(name, backend, running.get(name)),
        })
    evidence = await asyncio.to_thread(proven.summary, set(models), await proven.feedback_by_task(session))
    return {"window_days": HEALTH_DAYS, "models": listed, **evidence}


def _mtp_state(name, backend, running):
    """Multi-token prediction for one model: {"state": on|off|unable|n/a, "why"} - from the engine when
    it is running, otherwise what the next start will do (supervisor.mtp_for)."""
    if backend.kind != "llamacpp":
        return {"state": "n/a", "why": "Ollama does not draft ahead"}
    profile = engine_profiles.load_profiles().get(backend.upstream_model)
    if profile is None:
        return {"state": "n/a", "why": ""}
    if profile.server != "llama":
        return {"state": "n/a", "why": f"served by {profile.server}, which drafts ahead only as its profile says"}
    layers = gguf_meta.mtp_layers(profile.model_path)
    if running is not None:
        on = bool(running.get("mtp")) or profile.has_mtp_flags()
    else:
        on = profile.has_mtp_flags() or supervisor.mtp_for(profile, profile.binary or supervisor.binary())
    if on:
        return {"state": "on", "why": "drafts two tokens ahead with the model's own MTP layer, then checks them"}
    if profile.mtp == "off":
        return {"state": "off", "why": "switched off in its profile (mtp: off)"}
    if not layers:
        return {"state": "unable", "why": "this model file has no MTP layers"}
    return {"state": "off", "why": "the engine would not start with it, so it runs without"}


class ModelDelete(BaseModel):
    # Model names carry slashes and colons - hf.co/unsloth/Qwen3.8-27B-GGUF:Q4_1 - so the name travels
    # in the body rather than in the path, where it would have to be escaped twice
    name: str = Field(min_length=1, max_length=256)
    force: bool = False


# A POST rather than a DELETE with a body: bodies on DELETE are legal but tunnels and proxies are
# known to drop them, and a delete that silently loses its argument is not a thing to leave lying about
@router.post("/models/delete")
async def delete_model(payload: ModelDelete, principal: Principal = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    """Deletes a model from Ollama, or a llama.cpp model downloaded from this page. Not undoable
    without downloading it again."""
    backend = await backends.resolve(payload.name)   # asks Ollama again when the name is unfamiliar
    if backend is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No model called {payload.name}")
    can_delete, why_not = _deletability(backend)
    if not can_delete:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, why_not)

    if not payload.force:
        # The two things that break other people rather than just freeing space
        blocking = [f"the gateway uses it for {purpose}" for purpose in _used_for(payload.name)]
        granted = (await _granted_to(session)).get(payload.name, [])
        if granted:
            blocking.append(f"it is granted by name to {', '.join(sorted(granted))}")
        if blocking:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "Not deleted: " + ", and ".join(blocking)
                                + ". Confirm again to delete it anyway.")

    if backend.kind == "llamacpp":
        freed, problem = await _delete_engine_model(payload.name)
        if problem:
            raise HTTPException(status.HTTP_409_CONFLICT, problem)
        log.info("llama.cpp model %s deleted by %s, %.1f GB freed", payload.name,
                 getattr(principal.user, "name", None) or "master-key", freed / 1024**3)
        return {"deleted": payload.name, "size_bytes": freed}

    problem = await backends.delete_ollama_model(payload.name)
    if problem:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, problem)
    log.info("model %s deleted by %s", payload.name,
             getattr(principal.user, "name", None) or "master-key")
    # Its own size, not what the disk gets back: Ollama keeps any layer another model still points at,
    # so deleting one of two models built on the same base frees very little
    return {"deleted": payload.name, "size_bytes": backend.size_bytes}



# --- finding a model to install, and installing it ----------------------------------------------

# An install outlives the page that started it: it is a task holding its own events, so reloading
# the browser loses the connection and nothing else. These are the tokens that can stop one.
_cancellations = {}
_cancellations_lock = threading.Lock()


def _progress_of(job):
    """The most recent byte counts a job has produced, for a page that has only just joined."""
    for event in reversed(job.events):
        try:
            data = json.loads(event.decode()[5:])
        except (ValueError, UnicodeDecodeError):
            continue
        if data.get("total") and data.get("completed") is not None:
            return {"status": data.get("status"), "completed": data["completed"], "total": data["total"]}
        if data.get("status"):
            return {"status": data["status"]}
    return {}


class ModelCheck(BaseModel):
    model_id: str = Field(min_length=1, max_length=256)


class ModelInstall(BaseModel):
    model_id: str = Field(min_length=1, max_length=256)
    # A QUANTIZATION_LEVELS key, or utils.hf_client.FILE_KEY + a file's name for one that names none
    quantization: str = Field(min_length=1, max_length=300)
    # "pull" takes the published GGUF as it is; "create" builds a local model with a context length;
    # "engine" downloads it for llama.cpp and adds an engine profile (config/engines.local.yaml);
    # "vllm" and "sglang" download the model's own files (safetensors) and add a profile that runs them
    mode: str = Field(default="pull", pattern="^(pull|create|engine|vllm|sglang)$")
    context_length: int = fitting.DEFAULT_CONTEXT_LENGTH


class Cancel(BaseModel):
    job_id: int


def _build_answers(model_id, info, files, architecture):
    """Which of this machine's llama.cpp builds can run these files: first whether each knows the
    architecture's name, then - when one does - whether it really loads them (builds.load_test).
    [{"name", "binary", "default", "supported", "tested", "problem"}]. Blocking; threaded."""
    named = engine_builds.supports(architecture)
    if not files or not any(b["supported"] for b in named):
        return [b | {"tested": False, "problem": ""} for b in named]
    sizes = {s.get("rfilename"): s.get("size") or 0 for s in info.get("siblings", [])}
    tested = {t["binary"]: t for t in engine_builds.load_test(model_id, files, hf_client.mmproj_files(info)[:1], sizes)}
    answers = []
    for build in named:
        result = tested.get(build["binary"]) or {}
        loads = result.get("loads")
        answers.append(build | {"supported": build["supported"] if loads is None else loads,
                                "tested": loads is not None, "problem": result.get("problem") or ""})
    return answers


def _look_up(model_id):
    """Everything Hugging Face and this machine have to say about a repository. Blocking; threaded."""
    info = hf_client.get_model_info(model_id)
    if "error" in info:
        return {"error": info["error"], "status_code": info.get("status_code", 503)}
    sizes = hf_client.get_model_sizes(info)
    param_count, param_source = hf_client.get_parameter_count(info, sizes)
    details = hf_client.gguf_files_detailed(info, fitting.QUANTIZATION_LEVELS)
    gguf_files = {key: entry["size"] for key, entry in details.items()}
    architecture = hf_client.get_model_architecture(info["id"], info)
    cache = fitting.kv_cache(architecture)
    system = get_system_info()
    recommendations = fitting.recommend(system, param_count, cache["kv_cache_recommended_bytes"],
                                        gguf_files, details)
    for row in recommendations:
        entry = details.get(row["quantization"]) or {}
        # Ollama pulls one file by its tag; a model in parts, or an unnamed file beside others, it cannot
        row["ollama"] = bool(entry) and row["parts"] == 1 and bool(
            row["quantization"] in fitting.QUANTIZATION_LEVELS or entry.get("label") or len(details) == 1)
        row["label"] = entry.get("label", "")
    gguf = info.get("gguf") or {}
    # The file tried in each build: the one the page suggests, else the first that fits at all
    pick = next((r for r in recommendations if r.get("recommended") and r.get("available")),
                next((r for r in recommendations if r.get("available")), None))
    tried = (details.get(pick["quantization"]) or {}).get("files") or [] if pick else []
    builds = _build_answers(model_id, info, tried, gguf.get("architecture")) if gguf_files else []
    served = serving.assess(model_id, info, system)
    return {
        # Every way this machine runs a model, and whether this one can go that way - and why not
        "providers": _provider_answers(gguf_files, recommendations, builds, served),
        "serving": served,
        "model_info": hf_client.public_model_info(info),
        # Ollama installs GGUF and nothing else, so a repository without it needs somewhere to go
        "gguf_alternatives": [] if gguf_files else hf_client.search_gguf(model_id),
        "model_sizes": {k: v for k, v in sizes.items() if k != "files"},
        "parameters": {"count": param_count, "source": param_source},
        "is_gguf_repo": bool(gguf_files),
        "model_architecture": architecture,
        "system_info": system,
        "kv_cache": cache,
        "recommendations": recommendations,
        # Which of this machine's llama.cpp builds can load it (app/engine/builds.py), for the
        # "Download for llama.cpp" route: some models run there and not in Ollama
        "llamacpp": {"architecture": gguf.get("architecture") or "",
                     "context_length": gguf.get("context_length") or 0,
                     "image_reader": bool(hf_client.mmproj_files(info)),
                     "tested_with": os.path.basename(tried[0]) if tried else "",
                     "builds": builds},
    }


def _provider_answers(gguf_files, recommendations, builds, served):
    """[{"name", "supported", "why"}] for Ollama, llama.cpp, vLLM and SGLang."""
    fits = [r for r in recommendations if r.get("available")]
    if not gguf_files:
        ollama = (False, "it publishes no GGUF files, the only thing Ollama installs")
    elif not fits:
        ollama = (False, "none of its files fits in this machine's memory")
    elif not any(r.get("ollama") for r in fits):
        ollama = (False, "its files are split in parts, or named so Ollama cannot pull them")
    else:
        ollama = (True, "")
    if not gguf_files:
        llama = (False, "it publishes no GGUF files, the only thing llama.cpp loads")
    elif any(b.get("supported") for b in builds):
        llama = (True, "")
    else:
        problem = next((b.get("problem") for b in builds if b.get("problem")), "")
        llama = (False, "no llama.cpp build here can load it" + (f" ({problem})" if problem else ""))
    answers = [{"name": "Ollama", "supported": ollama[0], "why": ollama[1]},
               {"name": "llama.cpp", "supported": llama[0], "why": llama[1]}]
    for server, answer in (served.get("engines") or {}).items():
        answers.append({"name": answer["label"], "server": server, "supported": answer["supported"],
                        "why": answer["why"]})
    return answers


@router.post("/models/check")
async def check_model(payload: ModelCheck, _: Principal = Depends(require_admin)):
    """What this model is, and which of its quantizations this machine could actually run."""
    model_id = payload.model_id.strip()
    if not hf_client.is_valid_model_id(model_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "That does not look like a Hugging Face repository name (owner/model)")
    found = await asyncio.to_thread(_look_up, model_id)
    if "error" in found:
        raise HTTPException(found.get("status_code") or status.HTTP_503_SERVICE_UNAVAILABLE, found["error"])
    return found


def _final_result(events, model_name):
    """Passes progress through and ends with exactly one {"done": true, ...}."""
    last_status = None
    for event in events:
        if event.get("cancelled"):
            yield {"done": True, "success": False, "cancelled": True, "error": "Cancelled"}
            return
        if event.get("error"):
            yield {"done": True, "success": False, "error": event["error"]}
            return
        last_status = event.get("status") or last_status
        # /api/create forwards the pull's own "success" before it creates the model, so only the last
        # status of the whole stream counts; stopping at the first one would cut the create short
        if last_status != "success":
            yield event
    if last_status == "success":
        yield {"done": True, "success": True, "model_name": model_name}
    else:
        yield {"done": True, "success": False, "error": "The install did not finish"}


async def _as_events(rows):
    """A blocking generator, read one item at a time off the event loop."""
    ending = object()
    while True:
        row = await asyncio.to_thread(next, rows, ending)
        if row is ending:
            return
        yield f"data: {json.dumps(row)}\n\n".encode()


# Installs in progress, written down: the download is Ollama's, but it is driven by the gateway's
# request, which a restart (every deploy) ends - and with it the download and the job a reloaded
# page picks up (2026-10-19). Each install stays listed until it really ends - finished, failed or
# cancelled by someone - and the gateway resumes what is listed when it starts. Ollama keeps the
# part already downloaded, so a resumed pull carries on from there.
INSTALLS_FILE = os.path.join(settings.PROJECT_ROOT, "state", "installs.json")
_installs_lock = threading.Lock()


def _listed_installs():
    try:
        with open(INSTALLS_FILE, encoding="utf-8") as f:
            return [e for e in json.load(f) if isinstance(e, dict) and e.get("name")]
    except (OSError, ValueError):
        return []


def _list_install(entry, listed):
    with _installs_lock:
        entries = [e for e in _listed_installs() if e["name"] != entry["name"]] + ([entry] if listed else [])
        os.makedirs(os.path.dirname(INSTALLS_FILE), exist_ok=True)
        with open(INSTALLS_FILE + ".new", "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(INSTALLS_FILE + ".new", INSTALLS_FILE)


ENGINE_CONTEXT_MAX = 65536    # a new engine's window; a model known to afford more gets it in its profile


def _engine_rows(entry, name, cancellation):
    """A model downloaded for llama.cpp, then made an engine: progress events, as an Ollama pull's.

    Blocking, run off the event loop like the pulls. The repository is read again here rather than
    trusted from the page, so a download resumed after a restart works from the same facts.
    """
    model_id, key = entry["model_id"], entry["quantization"]
    yield {"status": "reading the repository"}
    info = hf_client.get_model_info(model_id)
    if "error" in info:
        yield {"error": info["error"]}
        return
    files = (hf_client.gguf_files_detailed(info, fitting.QUANTIZATION_LEVELS).get(key) or {}).get("files")
    if not files:
        yield {"error": f"{model_id} publishes no file for {key}"}
        return
    gguf = info.get("gguf") or {}
    architecture = gguf.get("architecture") or ""
    yield {"status": "loading it in this machine's llama.cpp builds (its headers only, nothing downloaded yet)"}
    answers = _build_answers(model_id, info, files, architecture)
    able = [b for b in answers if b["supported"]]
    if not able:
        why = next((b["problem"] for b in answers if b["problem"]), "")
        yield {"error": f"None of this machine's llama.cpp builds can load {architecture or 'this model'}"
                        + (f" ({why})" if why else "") + ": a newer llama.cpp is needed"}
        return
    build = next((b for b in able if b["default"]), able[0])
    readers = hf_client.mmproj_files(info)[:1]
    sizes = {s.get("rfilename"): s.get("size") or 0 for s in info.get("siblings", [])}
    folder = os.path.join(os.path.expanduser("~"), "models", fitting.short_name(model_id))
    for event in hf_download.download(model_id, files + readers, folder, cancellation, sizes):
        if event.get("status") == "success":
            break
        yield event
        if "error" in event or event.get("cancelled"):
            return
    profile = {"name": name, "model_path": os.path.join(folder, os.path.basename(files[0])),
               "ctx_size": min(int(gguf.get("context_length") or ENGINE_CONTEXT_MAX), ENGINE_CONTEXT_MAX),
               "parallel": 1,
               # --fit places what fits on the GPU and the rest in RAM; room kept for an image reader
               "extra_args": ["--fit", "on"] + (["--fit-target", "1536"] if readers else []) + ["--cache-ram", "24576"]}
    if gguf.get("total"):
        profile["parameter_size"] = f"{round(gguf['total'] / 1e9)}B"
    if readers:
        profile["mmproj"] = os.path.join(folder, os.path.basename(readers[0]))
    if not build["default"]:
        profile["binary"] = build["binary"]
        profile["env"] = next((b["env"] for b in engine_builds.builds() if b["binary"] == build["binary"]), {})
    engine_profiles.add_local_profile(profile)
    log.info("engine %s added (%s, %s)", name, build["name"], profile["model_path"])
    yield {"status": "success"}


def _serving_rows(entry, name, cancellation):
    """A model's own files downloaded for vLLM or SGLang, then a profile that runs them on both GPUs:
    progress events, as an Ollama pull's. Asked again whether it can run here before anything is
    downloaded, so a resumed download works from the same facts."""
    model_id, server = entry["model_id"], entry["mode"]
    yield {"status": "reading the repository"}
    info = hf_client.get_model_info(model_id)
    if "error" in info:
        yield {"error": info["error"]}
        return
    system = get_system_info()
    answer = serving.assess(model_id, info, system)
    verdict = (answer.get("engines") or {}).get(server) or {}
    if answer.get("source") != model_id or not verdict.get("supported"):
        yield {"error": f"{serving.ENGINES[server]} cannot run {model_id} here: "
                        + (verdict.get("why") or "it is not the repository with the model's own files")}
        return
    files = serving.download_files(info)
    sizes = {s.get("rfilename"): s.get("size") or 0 for s in info.get("siblings", [])}
    folder = os.path.join(os.path.expanduser("~"), "models", fitting.short_name(model_id))
    for event in hf_download.download(model_id, files, folder, cancellation, sizes):
        if event.get("status") == "success":
            break
        yield event
        if "error" in event or event.get("cancelled"):
            return
    config = hf_client.get_model_config(model_id)
    entry = serving.profile(server, name, folder, {} if "error" in config else config, system)
    count = hf_client.get_parameter_count(info, hf_client.get_model_sizes(info))[0]
    if count:
        entry["parameter_size"] = f"{round(count / 1e9)}B"
    engine_profiles.add_local_profile(entry)
    log.info("%s engine %s added (%s)", serving.ENGINES[server], name, folder)
    yield {"status": "success"}


def _start_install(entry, who):
    """Starts a pull, build or llama.cpp download as a job of its own, listed until it ends:
    (job, model name)."""
    cancellation = StreamCancellation()
    label = entry.get("label", "")
    source = fitting.hf_reference(entry["model_id"], entry["quantization"], label)
    if entry["mode"] == "engine":
        name = fitting.engine_name(entry["model_id"], entry["quantization"], label)
        rows = _engine_rows(entry, name, cancellation)
    elif entry["mode"] in serving.ENGINES:
        name = f"{fitting.short_name(entry['model_id'])}-{entry['mode']}"
        rows = _serving_rows(entry, name, cancellation)
    elif entry["mode"] == "create":
        name = fitting.ollama_name(entry["model_id"], entry["quantization"], label)
        rows = stream_create_model(name, source, {"num_ctx": fitting.clamp_context(entry["context_length"])},
                                   cancellation)
    else:
        name = source
        rows = stream_pull_model(source, cancellation)
    entry = {**entry, "name": name}
    _list_install(entry, True)
    log.info("%s of %s started by %s", entry["mode"], name, who)

    async def work():
        ended = False
        try:
            async for event in _as_events(_final_result(rows, name)):
                ended = ended or b'"done": true' in event
                yield event
        finally:
            backends.forget_discovery()   # a new model should appear in the list at once
            if ended:                     # not when the gateway is stopping: then it is resumed
                _list_install(entry, False)

    job = jobs.start(entry["owner"], None, "install", name, work())
    with _cancellations_lock:
        _cancellations[job.id] = cancellation
    return job, name


def resume_installs():
    """At startup: the installs a restart cut short, started again where Ollama left them."""
    running = {job.label for job in jobs.running_of_kind("install")}
    for entry in _listed_installs():
        if entry["name"] not in running:
            try:
                _start_install(entry, "the gateway, resuming after a restart")
            except Exception as e:
                log.warning("could not resume the install of %s: %s", entry["name"], e)


@router.post("/models/install")
async def install_model(payload: ModelInstall, principal: Principal = Depends(require_admin)):
    """Pulls a model from Hugging Face, or builds a local one with a context length of your choosing.

    A download of twenty gigabytes is not a request anyone should have to sit and watch, but it is
    Ollama doing the downloading: closing this stream stops the progress arriving, not the work.
    Cancelling is a separate, deliberate act.
    """
    model_id = payload.model_id.strip()
    if not hf_client.is_valid_model_id(model_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is not a Hugging Face repository name")
    label = ""
    if payload.mode not in serving.ENGINES and payload.quantization not in fitting.QUANTIZATION_LEVELS:
        if not payload.quantization.startswith(hf_client.FILE_KEY):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown quantization {payload.quantization}")
        # A file that names no standard quantization: Ollama finds it by the label its name shows
        info = await asyncio.to_thread(hf_client.get_model_info, model_id)
        details = hf_client.gguf_files_detailed(info, fitting.QUANTIZATION_LEVELS) if "error" not in info else {}
        entry = details.get(payload.quantization)
        if not entry:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{model_id} publishes no such file")
        label = entry["label"]
        if payload.mode != "engine" and (len(entry["files"]) > 1 or not (label or len(details) == 1)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Ollama cannot pull that one (a model in parts, or a file it cannot name); "
                                "download it for llama.cpp instead")
    owner = principal.user.id if principal.user else 0
    job, name = _start_install({"model_id": model_id, "quantization": payload.quantization, "mode": payload.mode,
                                "context_length": payload.context_length, "owner": owner, "label": label},
                               getattr(principal.user, "name", None) or "master-key")

    async def stream():
        yield f"data: {json.dumps({'job_id': job.id, 'model_name': name})}\n\n".encode()
        async for event in jobs.follow(job):
            yield event

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/models/installing")
async def installing(_: Principal = Depends(require_admin)):
    """What this machine is downloading right now, so a reloaded page can pick it back up."""
    return {"installing": [{**job.json(), "progress": _progress_of(job)}
                           for job in jobs.running_of_kind("install")]}


@router.get("/models/install/{job_id}/watch")
async def watch_install(job_id: int, _: Principal = Depends(require_admin)):
    """Everything the install has produced so far, then the rest as it arrives."""
    job = jobs.any_job(job_id)
    if job is None or job.kind != "install":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such install")
    return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/models/cancel")
async def cancel_install(payload: Cancel, _: Principal = Depends(require_admin)):
    with _cancellations_lock:
        cancellation = _cancellations.get(payload.job_id)
    job = jobs.any_job(payload.job_id)
    if cancellation is None or job is None or job.done:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nothing by that number is running")
    cancellation.cancel()
    return {"cancelling": payload.job_id}


@router.get("/engines")
async def engine_status(_: Principal = Depends(require_admin)):
    return supervisor.status()


@router.post("/engines/{name}/start")
async def engine_start(name: str, _: Principal = Depends(require_admin)):
    model, problem = await supervisor.ensure_running(name)
    if problem:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, problem)
    return model.status()


@router.post("/engines/{name}/stop")
async def engine_stop(name: str, _: Principal = Depends(require_admin)):
    return {"stopped": await supervisor.stop(name)}


async def _ollama_models():
    """Ollama's installed models, each with whether it is loaded and how much of it is on the GPU.
    Names and sizes only: nothing about where Ollama runs (see system_overview)."""
    host = ollama_host()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            tags = (await client.get(f"{host}/api/tags")).json().get("models") or []
            loaded = {e.get("name") or e.get("model"): e
                      for e in (await client.get(f"{host}/api/ps")).json().get("models") or []}
    except (httpx.HTTPError, ValueError):
        return []
    rows = []
    for tag in tags:
        name = tag.get("name") or tag.get("model")
        if not name:
            continue
        here = loaded.get(name)
        rows.append({"name": name, "size": tag.get("size") or 0, "loaded": bool(here),
                     "memory": (here or {}).get("size") or 0, "on_gpu": (here or {}).get("size_vram") or 0})
    rows.sort(key=lambda r: (not r["loaded"], r["name"]))
    return rows


class OllamaModel(BaseModel):
    model: str = Field(min_length=1, max_length=256)


@router.post("/ollama/load")
async def ollama_load(payload: OllamaModel, _: Principal = Depends(require_admin)):
    """Loads an installed Ollama model, as a request for it would: idle llama.cpp models give way
    first when it would not fit beside them (supervisor.make_room_for_ollama)."""
    if payload.model not in {r["name"] for r in await _ollama_models()}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Ollama has no model {payload.model}")
    problem = await supervisor.make_room_for_ollama(payload.model)
    if problem:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, problem)
    try:
        # A generate with no prompt only loads; Ollama's own keep-alive then applies
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
            response = await client.post(f"{ollama_host()}/api/generate", json={"model": payload.model})
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Ollama did not answer: {e}")
    if response.status_code >= 400 or body.get("error"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"Ollama could not load {payload.model}: {body.get('error') or response.text[:300]}")
    return {"loaded": payload.model}


@router.post("/ollama/unload")
async def ollama_unload(payload: OllamaModel, _: Principal = Depends(require_admin)):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{ollama_host()}/api/generate",
                                         json={"model": payload.model, "keep_alive": 0})
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Ollama did not answer: {e}")
    if response.status_code >= 400 or body.get("error"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"Ollama could not unload {payload.model}: {body.get('error') or response.text[:300]}")
    return {"unloaded": payload.model}


@router.get("/settings")
async def read_settings(_: Principal = Depends(require_admin)):
    return {"config_file": config_writer.config.config_file_path(), "values": config_writer.read_settings()}


@router.post("/settings")
async def write_settings(payload: dict, _: Principal = Depends(require_admin)):
    try:
        written = config_writer.write_settings(payload)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    # The config file is re-read when it changes, so the new values are live immediately
    return {"saved": written}


@router.get("/system")
async def system_overview(_: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    # Measured, not get_system_info(): its configured sizes would show a loaded card as empty
    info = await asyncio.to_thread(measure_hardware)   # psutil and nvidia-smi block
    users = (await session.execute(select(func.count(User.id)))).scalar() or 0
    keys = (await session.execute(select(func.count(ApiKey.id)).where(ApiKey.is_active.is_(True)))).scalar() or 0
    requests_today = (await session.execute(
        select(func.count(UsageRecord.id)).where(UsageRecord.created_at >= utcnow() - timedelta(days=1))
    )).scalar() or 0
    tokens_today = (await session.execute(
        select(func.coalesce(func.sum(UsageRecord.total_tokens), 0)).where(UsageRecord.created_at >= utcnow() - timedelta(days=1))
    )).scalar() or 0
    # What is up and what is loaded - never where: no addresses, ports, process ids or file paths.
    # The page is public-facing (an admin's browser, a screenshot, a shared screen), and those are a
    # map of the machine behind it.
    ollama = check_ollama_status()
    engine = supervisor.status()
    return {
        "providers": _providers(ollama, engine),
        "ollama": {"status": ollama.get("status"), "version": ollama.get("version"),
                   "models": await _ollama_models() if ollama.get("status") == "running" else []},
        "engine": {"available": engine["available"], "profiles": engine["profiles"],
                   "servers": {name: ENGINE_LABELS.get(profile.server, profile.server)
                               for name, profile in engine_profiles.load_profiles().items()},
                   "running": [{k: r[k] for k in ("name", "running", "uptime_seconds", "idle_seconds",
                                                  "in_flight", "ttl_seconds")} for r in engine["running"]]},
        "gpus": info["gpu_info"],
        "gpu_source": info["gpu_source"],
        "total_vram": info["total_vram"],
        "total_ram": info["total_ram"],
        "available_ram": info["available_ram"],
        "counts": {"users": users, "active_keys": keys,
                   "requests_24h": requests_today, "tokens_24h": int(tokens_today)},
    }


ENGINE_LABELS = {"llama": "llama.cpp", "vllm": "vLLM", "sglang": "SGLang"}


def _providers(ollama, engine):
    """Every kind of model provider this gateway has, with whether it can answer: Ollama, each engine
    the profiles run (llama.cpp, vLLM, SGLang - one entry each, with its profiles and how many are
    loaded) and each OpenAI-compatible API in models.yaml, by its host. Never an address, port or path."""
    from app.backends import _load_model_file

    out = [{"name": "Ollama", "status": ollama.get("status") or "unknown",
            "ok": ollama.get("status") == "running", "detail": ("v" + ollama["version"]) if ollama.get("version") else ""}]
    profiles = engine_profiles.load_profiles()
    loaded = {r["name"] for r in engine["running"]}
    for server, label in ENGINE_LABELS.items():
        names = [n for n, p in profiles.items() if p.server == server]
        if server == "llama":
            installed = engine["available"] or any(
                p.binary and os.path.isfile(p.binary) for p in profiles.values() if p.server == "llama")
        else:
            installed = any(p.binary and os.path.isfile(p.binary) for p in profiles.values() if p.server == server)
        if not names and server != "llama":
            continue
        running = [n for n in names if n in loaded]
        out.append({"name": label, "status": ("running" if running else "available") if installed else "not installed",
                    "ok": installed, "detail": f"{len(names)} model{'' if len(names) == 1 else 's'}"
                    + (f", {len(running)} loaded" if running else "")})
    upstreams = {}
    for backend in _load_model_file().values():
        if backend.kind == "openai":
            host = (backend.base_url or "").split("://", 1)[-1].split("/", 1)[0].split("@")[-1]
            upstreams[host] = upstreams.get(host, 0) + 1
    for host, count in sorted(upstreams.items()):
        out.append({"name": host or "OpenAI-compatible API", "status": "configured", "ok": True,
                    "detail": f"{count} model{'' if count == 1 else 's'}"})
    return out


# --- knowledge base -------------------------------------------------------------------------


@router.get("/knowledge")
async def knowledge_overview(_: Principal = Depends(require_manager)):
    """What is indexed, and how the current or last indexing run went."""
    return {"enabled": knowledge.is_enabled(), "embedding_model": knowledge.embedding_model(),
            "sources": await knowledge.list_sources(), "progress": knowledge.progress}


@router.post("/knowledge/reindex")
async def knowledge_reindex(only: str | None = None, _: Principal = Depends(require_admin)):
    """Starts an indexing run in the background; `only` limits it to sources matching a pattern."""
    if not knowledge.is_enabled():
        raise HTTPException(status.HTTP_409_CONFLICT, "The knowledge base is switched off (knowledge.enabled)")
    started = knowledge.start_reindex(only)
    return {"started": started, "progress": knowledge.progress,
            **({} if started else {"detail": "An indexing run is already going"})}


# --- tool servers ---------------------------------------------------------------------------

class McpServerIn(BaseModel):
    """One tool server as the Tools page sends it.

    Everything except the name is optional on a change, so toggling one switch does not require
    the browser to send back a server it is not editing.
    """
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=300)
    transport: str | None = Field(default=None, pattern="^(stdio|http)$")
    enabled: bool | None = None
    command: str | None = Field(default=None, max_length=300)
    args: list[str] | None = Field(default=None, max_length=40)
    url: str | None = Field(default=None, max_length=500)
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    only: list[str] | None = Field(default=None, max_length=100)


@router.get("/mcp")
async def list_mcp_servers(_: Principal = Depends(require_admin)):
    """Every configured tool server, and whether this host could run it at all.

    Deliberately does not connect: launching a dozen child processes to draw a page is not what a
    page load should do. The tools come from checking one, which is a thing the admin asks for.
    """
    return {
        "enabled": mcp_client.is_enabled(),
        "file": settings.get("mcp.servers.file", "MCP_SERVERS_FILE") or "config/mcp.yaml",
        "servers": mcp_config.everything(),
    }


@router.post("/mcp", status_code=status.HTTP_201_CREATED)
async def add_mcp_server(payload: McpServerIn, _: Principal = Depends(require_admin)):
    """Adds a server to config/mcp.yaml.

    A stdio server is a command this gateway will run as its own user, so this is an admin-only
    endpoint in the same sense that installing a model is: it is a way to run something on the
    machine, and it is only in the hands of people who already have that.
    """
    try:
        return mcp_config.add(payload.model_dump(exclude_none=True))
    except mcp_config.ConfigError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.patch("/mcp/{name}")
async def update_mcp_server(name: str, payload: McpServerIn,
                            _: Principal = Depends(require_admin)):
    """Changes one server. Anything not sent keeps the value it had."""
    try:
        return mcp_config.update(name, payload.model_dump(exclude_none=True))
    except mcp_config.ConfigError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.delete("/mcp/{name}")
async def remove_mcp_server(name: str, _: Principal = Depends(require_admin)):
    try:
        return {"removed": mcp_config.remove(name)}
    except mcp_config.ConfigError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


class McpCheck(BaseModel):
    name: str = Field(min_length=1, max_length=128)


@router.post("/mcp/check")
async def check_mcp_server(payload: McpCheck, _: Principal = Depends(require_admin)):
    """Connects to one server and asks what it offers.

    This is the whole answer to "is my server working": it starts it the way the gateway will,
    speaks the protocol, and shows what came back - or exactly what went wrong instead.
    """
    server = mcp_client.find(payload.name)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"No server called {payload.name!r} is configured")
    return await mcp_client.probe(server)


# --- power -------------------------------------------------------------------------------------

class SuspendIn(BaseModel):
    force: bool = False                                  # suspend even if work is running
    delay_seconds: int = Field(default=5, ge=1, le=600)


@router.post("/power/suspend", status_code=status.HTTP_202_ACCEPTED)
async def suspend_machine(payload: SuspendIn | None = None, principal: Principal = Depends(require_admin)):
    """Puts homelabai to sleep. The next request through the wake front wakes it again."""
    payload = payload or SuspendIn()
    busy = activity.snapshot(ignore_requests=1)          # this request is not work
    if busy["busy"] and not payload.force:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            {"message": "the gateway is busy; send force=true to suspend anyway",
                             "reasons": busy["reasons"]})
    ok, problem = await power.allowed()
    if not ok:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, problem)
    if power.pending():
        return {"suspending_in_seconds": None, "already_pending": True}
    log.info("suspend requested by %s (force=%s)", principal.label, payload.force)
    power.schedule(payload.delay_seconds)
    return {"suspending_in_seconds": payload.delay_seconds, "interrupted": busy["reasons"] if payload.force else []}


@router.delete("/power/suspend")
async def cancel_suspend(principal: Principal = Depends(require_admin)):
    """Calls off a suspend that has not happened yet."""
    return {"cancelled": power.cancel()}
