"""Admin API: users, API keys, usage, and the models themselves."""
import asyncio
import json
import logging
import re
import threading
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import access, backends, catalogue, fitting, settings
from app.auth import Principal, generate_key, hash_key, require_admin, require_manager
from app import config_writer
from app.engine.supervisor import supervisor
from utils import hf_client
from utils.ollama_client import (StreamCancellation, check_ollama_status, ollama_host,
                                 stream_create_model, stream_pull_model)
from utils.system_info import get_system_info
from utils.system_info import get_system_info
from app.db import get_session
from app.models import ApiKey, UsageRecord, User, utcnow

log = logging.getLogger("admin")

router = APIRouter(prefix="/admin", tags=["admin"])


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    email: str | None = None
    role: str = Field(default="user", pattern="^(user|manager|admin)$")
    model_access: str | None = Field(default=None, pattern="^(all|selected|none)$")
    allowed_models: list[str] | None = None


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern="^(user|manager|admin)$")
    model_access: str | None = Field(default=None, pattern="^(all|selected|none)$")
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
        "keys": [
            {"id": k.id, "name": k.name, "prefix": k.key_prefix, "is_active": k.is_active,
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


@router.delete("/users/{name}")
async def deactivate_user(name: str, principal: Principal = Depends(require_manager),
                          session: AsyncSession = Depends(get_session)):
    return await update_user(name, UserPatch(is_active=False), principal, session)


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
    since = utcnow() - timedelta(days=days)
    result = await session.execute(
        select(User.name, UsageRecord.model, func.count(UsageRecord.id),
               func.sum(UsageRecord.prompt_tokens), func.sum(UsageRecord.completion_tokens))
        .join(User, User.id == UsageRecord.user_id, isouter=True)
        .where(UsageRecord.created_at >= since)
        .group_by(User.name, UsageRecord.model)
        .order_by(func.count(UsageRecord.id).desc())
    )
    return {
        "since": since.isoformat(),
        "rows": [
            {"user": name or "master-key", "model": model, "requests": requests,
             "prompt_tokens": int(prompt or 0), "completion_tokens": int(completion or 0)}
            for name, model, requests, prompt, completion in result.all()
        ],
    }


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
    """Requests, failures, tokens and latency per model over a window, plus the last failure's words."""
    rows = (await session.execute(
        select(UsageRecord.model, func.count(UsageRecord.id),
               func.sum(case((UsageRecord.status_code >= 400, 1), else_=0)),
               func.max(UsageRecord.created_at), func.avg(UsageRecord.latency_ms),
               func.sum(UsageRecord.total_tokens))
        .where(UsageRecord.created_at >= since).group_by(UsageRecord.model)
    )).all()
    stats = {
        model: {"requests": int(requests or 0), "failures": int(failures or 0),
                "last_used": last.isoformat() if last else None, "_last": last,
                "avg_latency_ms": int(latency or 0), "tokens": int(tokens or 0), "last_error": None}
        for model, requests, failures, last, latency, tokens in rows
    }
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


def _deletability(backend):
    """Only models Ollama holds can be deleted from here; the rest are someone else's to remove."""
    if backend.kind == "ollama":
        return True, ""
    if backend.kind == "llamacpp":
        return False, "This is one of the gateway's own llama.cpp profiles. Remove it from config/engines.yaml."
    return False, "This one is defined in config/models.yaml. Remove the entry there."


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
                backend=backend.kind),
            "usage": {k: v for k, v in stats.items() if not k.startswith("_")},
            "used_for": used_for,
            "granted_to": sorted(granted.get(name, [])),
            "reachable": reachable,
            "loaded": name in loaded,
            "can_delete": can_delete,
            "delete_note": why_not,
            "notes": _notes(name, backend, stats, used_for, granted.get(name, []), reachable),
        })
    return {"window_days": HEALTH_DAYS, "models": listed}


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
    """Deletes a model from the Ollama server. Not undoable without pulling it again."""
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

    problem = await backends.delete_ollama_model(payload.name)
    if problem:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, problem)
    log.info("model %s deleted by %s", payload.name,
             getattr(principal.user, "name", None) or "master-key")
    # Its own size, not what the disk gets back: Ollama keeps any layer another model still points at,
    # so deleting one of two models built on the same base frees very little
    return {"deleted": payload.name, "size_bytes": backend.size_bytes}



# --- finding a model to install, and installing it ----------------------------------------------

# The client picks the id so that it can cancel before the first byte of progress has arrived
OPERATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_running = {}
_running_lock = threading.Lock()


class ModelCheck(BaseModel):
    model_id: str = Field(min_length=1, max_length=256)


class ModelInstall(BaseModel):
    model_id: str = Field(min_length=1, max_length=256)
    quantization: str = Field(min_length=1, max_length=32)
    # "pull" takes the published GGUF as it is; "create" builds a local model with a context length
    mode: str = Field(default="pull", pattern="^(pull|create)$")
    context_length: int = fitting.DEFAULT_CONTEXT_LENGTH
    operation_id: str = ""


class Cancel(BaseModel):
    operation_id: str = Field(min_length=1, max_length=64)


def _look_up(model_id):
    """Everything Hugging Face and this machine have to say about a repository. Blocking; threaded."""
    info = hf_client.get_model_info(model_id)
    if "error" in info:
        return {"error": info["error"], "status_code": info.get("status_code", 502)}
    sizes = hf_client.get_model_sizes(info)
    param_count, param_source = hf_client.get_parameter_count(info, sizes)
    gguf_files = hf_client.get_gguf_files(info, fitting.QUANTIZATION_LEVELS)
    architecture = hf_client.get_model_architecture(info["id"], info)
    cache = fitting.kv_cache(architecture)
    system = get_system_info()
    return {
        "model_info": hf_client.public_model_info(info),
        "model_sizes": {k: v for k, v in sizes.items() if k != "files"},
        "parameters": {"count": param_count, "source": param_source},
        "is_gguf_repo": bool(gguf_files),
        "model_architecture": architecture,
        "system_info": system,
        "kv_cache": cache,
        "recommendations": fitting.recommend(system, param_count,
                                             cache["kv_cache_recommended_bytes"], gguf_files),
    }


@router.post("/models/check")
async def check_model(payload: ModelCheck, _: Principal = Depends(require_admin)):
    """What this model is, and which of its quantizations this machine could actually run."""
    model_id = payload.model_id.strip()
    if not hf_client.is_valid_model_id(model_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "That does not look like a Hugging Face repository name (owner/model)")
    found = await asyncio.to_thread(_look_up, model_id)
    if "error" in found:
        raise HTTPException(found.get("status_code") or status.HTTP_502_BAD_GATEWAY, found["error"])
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
    if payload.quantization not in fitting.QUANTIZATION_LEVELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown quantization {payload.quantization}")
    operation = payload.operation_id or uuid.uuid4().hex
    if not OPERATION_ID.match(operation):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid operation id")

    cancellation = StreamCancellation()
    with _running_lock:
        if operation in _running:
            raise HTTPException(status.HTTP_409_CONFLICT, "That operation is already running")
        _running[operation] = cancellation

    source = fitting.hf_reference(model_id, payload.quantization)
    if payload.mode == "create":
        name = fitting.ollama_name(model_id, payload.quantization)
        context = fitting.clamp_context(payload.context_length)
        rows = stream_create_model(name, source, {"num_ctx": context}, cancellation)
    else:
        name = source
        rows = stream_pull_model(source, cancellation)

    log.info("%s of %s started by %s", payload.mode, name,
             getattr(principal.user, "name", None) or "master-key")

    async def stream():
        try:
            yield f"data: {json.dumps({'operation_id': operation, 'model_name': name})}\n\n".encode()
            async for event in _as_events(_final_result(rows, name)):
                yield event
        finally:
            with _running_lock:
                _running.pop(operation, None)
            backends.forget_discovery()   # a new model should appear in the list at once

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/models/cancel")
async def cancel_install(payload: Cancel, _: Principal = Depends(require_admin)):
    with _running_lock:
        cancellation = _running.get(payload.operation_id)
    if cancellation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nothing by that name is running")
    cancellation.cancel()
    return {"cancelling": payload.operation_id}


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
    info = get_system_info()
    users = (await session.execute(select(func.count(User.id)))).scalar() or 0
    keys = (await session.execute(select(func.count(ApiKey.id)).where(ApiKey.is_active.is_(True)))).scalar() or 0
    requests_today = (await session.execute(
        select(func.count(UsageRecord.id)).where(UsageRecord.created_at >= utcnow() - timedelta(days=1))
    )).scalar() or 0
    tokens_today = (await session.execute(
        select(func.coalesce(func.sum(UsageRecord.total_tokens), 0)).where(UsageRecord.created_at >= utcnow() - timedelta(days=1))
    )).scalar() or 0
    return {
        "ollama": {**check_ollama_status(), "host": ollama_host()},
        "engine": supervisor.status(),
        "gpus": info["gpu_info"],
        "total_vram": info["total_vram"],
        "total_ram": info["total_ram"],
        "available_ram": info["available_ram"],
        "counts": {"users": users, "active_keys": keys,
                   "requests_24h": requests_today, "tokens_24h": int(tokens_today)},
    }
