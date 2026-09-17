"""Admin API: users, API keys and usage. No seat limit."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import Principal, generate_key, hash_key, require_admin
from app.db import get_session
from app.models import ApiKey, UsageRecord, User, utcnow

router = APIRouter(prefix="/admin", tags=["admin"])


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    email: str | None = None
    role: str = Field(default="user", pattern="^(user|admin)$")


class KeyIn(BaseModel):
    user: str = Field(min_length=1, description="User name the key belongs to")
    name: str = Field(default="default", max_length=128)


def _user_json(user):
    return {
        "id": user.id, "name": user.name, "email": user.email, "role": user.role,
        "is_active": user.is_active, "created_at": user.created_at.isoformat(),
        "keys": [
            {"id": k.id, "name": k.name, "prefix": k.key_prefix, "is_active": k.is_active,
             "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None}
            for k in user.api_keys
        ],
    }


async def _get_user(session, name):
    result = await session.execute(select(User).options(selectinload(User.api_keys)).where(User.name == name))
    return result.scalar_one_or_none()


@router.get("/users")
async def list_users(_: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(User).options(selectinload(User.api_keys)).order_by(User.id))
    return [_user_json(u) for u in result.scalars().all()]


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserIn, _: Principal = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    if await _get_user(session, payload.name):
        raise HTTPException(status.HTTP_409_CONFLICT, f"User '{payload.name}' already exists")
    user = User(name=payload.name, email=payload.email, role=payload.role)
    session.add(user)
    await session.commit()
    await session.refresh(user, attribute_names=["api_keys"])
    return _user_json(user)


@router.delete("/users/{name}")
async def deactivate_user(name: str, _: Principal = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    user = await _get_user(session, name)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User '{name}' not found")
    user.is_active = False
    for key in user.api_keys:
        key.is_active = False
    await session.commit()
    return {"deactivated": name}


@router.post("/keys", status_code=status.HTTP_201_CREATED)
async def create_key(payload: KeyIn, _: Principal = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)):
    user = await _get_user(session, payload.user)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User '{payload.user}' not found")
    key = generate_key()
    api_key = ApiKey(user_id=user.id, name=payload.name, key_hash=hash_key(key), key_prefix=key[:12])
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    # The only time the full key is returned
    return {"id": api_key.id, "user": user.name, "name": api_key.name, "key": key}


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: int, _: Principal = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)):
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    api_key.is_active = False
    await session.commit()
    return {"revoked": key_id}


@router.get("/usage")
async def usage_summary(days: int = 7, _: Principal = Depends(require_admin),
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
