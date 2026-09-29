"""Login through the OAuth2 provider, plus self-service API keys for logged-in users."""
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import access, settings, sso
from app.auth import generate_key, hash_key
from app.db import get_session
from app.models import ApiKey, UsageRecord, User, utcnow

router = APIRouter(prefix="/auth", tags=["auth"])


def _redirect_uri(request: Request):
    base = settings.public_base_url()
    if base:
        return f"{base}/auth/callback"
    # Falls back to the address the browser used, which covers local and LAN access
    return str(request.url_for("sso_callback"))


async def current_user(session: AsyncSession = Depends(get_session),
                       gateway_session: str | None = Cookie(default=None, alias=sso.SESSION_COOKIE)):
    data = sso.read_session(gateway_session)
    if not data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    result = await session.execute(
        select(User).options(selectinload(User.api_keys)).where(User.id == data["user_id"])
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is not active")
    return user


@router.get("/login")
async def login(request: Request):
    if not sso.is_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "SSO is not configured: set sso.client.id, sso.authorize.url and sso.token.url")
    verifier, challenge = sso.make_pkce()
    redirect_uri = _redirect_uri(request)
    state_token = sso.sign_state({"verifier": verifier, "redirect_uri": redirect_uri})
    response = RedirectResponse(sso.authorize_url(redirect_uri, state_token, challenge))
    # The state travels in a cookie as well, so a replayed or forged callback is rejected
    response.set_cookie(sso.STATE_COOKIE, state_token, max_age=sso.STATE_MAX_AGE,
                        httponly=True, samesite="lax", path="/auth")
    return response


@router.get("/callback", name="sso_callback")
async def callback(request: Request, code: str | None = None, state: str | None = None,
                   error: str | None = None, session: AsyncSession = Depends(get_session),
                   gateway_oauth_state: str | None = Cookie(default=None, alias=sso.STATE_COOKIE)):
    if error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Sign-in failed: {error}")
    if not code or not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing code or state")
    if not gateway_oauth_state or state != gateway_oauth_state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "State mismatch; start the sign-in again")

    state_data = sso.read_state(state)
    if not state_data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Sign-in expired; start again")

    tokens, problem = await sso.exchange_code(code, state_data["redirect_uri"], state_data["verifier"])
    if problem:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, problem)

    userinfo, problem = await sso.fetch_userinfo(tokens.get("access_token", ""))
    if problem:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, problem)

    identity = sso.identity_from_userinfo(userinfo)
    if not identity["name"]:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Auth server returned no usable identity claims")

    result = await session.execute(select(User).where(User.name == identity["name"]))
    user = result.scalar_one_or_none()
    if user is None:
        # No seat limit: everyone the auth server accepts gets an account
        user = User(name=identity["name"], email=identity["email"], role=sso.role_for(identity["email"]),
                    model_access=access.default_access())
        session.add(user)
    else:
        if not user.is_active:
            # Signing in again must not undo an admin disabling the account
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been disabled")
        user.email = identity["email"] or user.email
        # Listed admin emails are always promoted; otherwise keep the role assigned in the UI
        if sso.role_for(identity["email"]) == "admin":
            user.role = "admin"
    await session.commit()
    await session.refresh(user)

    response = RedirectResponse("/dashboard")
    response.set_cookie(sso.SESSION_COOKIE, sso.issue_session(user), max_age=sso.SESSION_MAX_AGE,
                        httponly=True, samesite="lax", path="/")
    response.delete_cookie(sso.STATE_COOKIE, path="/auth")
    return response


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {
        "name": user.name, "email": user.email, "role": user.role,
        "model_access": "all" if user.role == "admin" else (user.model_access or "all"),
        "allowed_models": access.parse_patterns(user.allowed_models),
        "keys": [{"id": k.id, "name": k.name, "prefix": k.key_prefix, "is_active": k.is_active}
                 for k in user.api_keys if k.is_active],
    }


class SelfKeyIn(BaseModel):
    name: str = Field(default="default", max_length=128)


@router.post("/keys", status_code=status.HTTP_201_CREATED)
async def create_own_key(payload: SelfKeyIn, user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)):
    key = generate_key()
    api_key = ApiKey(user_id=user.id, name=payload.name, key_hash=hash_key(key), key_prefix=key[:12])
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return {"id": api_key.id, "name": api_key.name, "key": key}


@router.delete("/keys/{key_id}")
async def revoke_own_key(key_id: int, user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)):
    api_key = await session.get(ApiKey, key_id)
    if api_key is None or api_key.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    api_key.is_active = False
    await session.commit()
    return {"revoked": key_id}


@router.post("/logout")
async def logout():
    response = JSONResponse({"signed_out": True})
    response.delete_cookie(sso.SESSION_COOKIE, path="/")
    return response


@router.get("/status")
async def sso_status():
    return {"sso_configured": sso.is_configured(), "authorize_url": settings.sso_authorize_url()}


@router.get("/usage")
async def own_usage(days: int = 7, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    """The signed-in user's own usage, so everyone can see what they have consumed."""
    since = utcnow() - timedelta(days=days)
    result = await session.execute(
        select(UsageRecord.model, func.count(UsageRecord.id),
               func.sum(UsageRecord.prompt_tokens), func.sum(UsageRecord.completion_tokens))
        .where(UsageRecord.user_id == user.id, UsageRecord.created_at >= since)
        .group_by(UsageRecord.model).order_by(func.count(UsageRecord.id).desc())
    )
    return {"since": since.isoformat(), "rows": [
        {"model": model, "requests": requests, "prompt_tokens": int(prompt or 0),
         "completion_tokens": int(completion or 0)} for model, requests, prompt, completion in result.all()]}
