"""Admin API: users, API keys and usage. No seat limit."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import access
from app.auth import Principal, generate_key, hash_key, require_admin, require_manager
from app import config_writer
from app.engine.supervisor import supervisor
from utils.ollama_client import check_ollama_status, ollama_host
from utils.system_info import get_system_info
from app.db import get_session
from app.models import ApiKey, UsageRecord, User, utcnow

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
