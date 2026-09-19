"""API key authentication."""
import hashlib
import secrets

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import settings, sso
from app.db import get_session
from app.models import ApiKey, User, utcnow

KEY_PREFIX = "sk-"


def generate_key():
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _bearer_token(authorization, x_api_key):
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (x_api_key or "").strip() or None


class Principal:
    """Who is making the request: either the master key or a user's API key."""

    def __init__(self, user=None, api_key=None, is_master=False):
        self.user = user
        self.api_key = api_key
        self.is_master = is_master

    @property
    def is_admin(self):
        return self.is_master or (self.user is not None and self.user.role == "admin")

    @property
    def is_manager(self):
        return self.is_admin or (self.user is not None and self.user.role == "manager")

    @property
    def label(self):
        if self.is_master:
            return "master-key"
        return self.user.name if self.user else "unknown"


async def authenticate(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    gateway_session: str | None = Cookie(default=None, alias=sso.SESSION_COOKIE),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    token = _bearer_token(authorization, x_api_key)
    if not token:
        # The web UI calls these same endpoints with its login cookie instead of a key
        data = sso.read_session(gateway_session)
        if data:
            user = await session.get(User, data["user_id"])
            if user and user.is_active:
                return Principal(user=user)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing API key")

    if secrets.compare_digest(token, settings.master_key()):
        return Principal(is_master=True)

    result = await session.execute(
        select(ApiKey).options(selectinload(ApiKey.user)).where(ApiKey.key_hash == hash_key(token))
    )
    api_key = result.scalar_one_or_none()
    if api_key is None or not api_key.is_active or not api_key.user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")

    api_key.last_used_at = utcnow()
    await session.commit()
    return Principal(user=api_key.user, api_key=api_key)


async def require_admin(principal: Principal = Depends(authenticate)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return principal


async def require_manager(principal: Principal = Depends(authenticate)) -> Principal:
    if not principal.is_manager:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Manager or admin access required")
    return principal
