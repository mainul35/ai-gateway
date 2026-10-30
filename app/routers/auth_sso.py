"""Login through the OAuth2 provider, plus self-service API keys for logged-in users."""
import json
import logging

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import access, backends, claude_desktop, settings, sso
from app.auth import Principal, authenticate, generate_key, hash_key
from app.db import get_session
from app.models import ApiKey, UsageRecord, User, utcnow

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("sso")


def _auth_server_failed(problem, code=status.HTTP_503_SERVICE_UNAVAILABLE):
    """Never a 502: the Cloudflare tunnel in front of the gateway replaces a 502 (and a 504) with
    its own error page, and the auth server's reason goes with it. A 503 passes through intact.
    Logged as well, since a sign-in that fails is often reported long after the page is gone."""
    log.warning("Sign-in failed at the auth server: %s", problem)
    return HTTPException(code, problem)


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
    if user is None or not user.is_active or not sso.session_current(data, user):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is not active")
    return user


@router.get("/login")
async def login(request: Request, next: str = "/dashboard"):
    if not sso.is_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "SSO is not configured: set sso.client.id, sso.authorize.url and sso.token.url")
    verifier, challenge = sso.make_pkce()
    redirect_uri = _redirect_uri(request)
    # The page the person was on when asked to sign in, kept in the signed state so the callback
    # can take them back there
    state_token = sso.sign_state({"verifier": verifier, "redirect_uri": redirect_uri,
                                  "next": sso.safe_next(next)})
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

    tokens, problem, answer = await sso.exchange_code(code, state_data["redirect_uri"],
                                                      state_data["verifier"])
    if problem:
        raise _auth_server_failed(problem, answer)

    userinfo, problem = await sso.fetch_userinfo(tokens.get("access_token", ""))
    if problem:
        raise _auth_server_failed(problem)

    identity = sso.identity_from_userinfo(userinfo)
    # Kept whole, and refreshed at every sign-in, so a capability granted or taken away on the auth
    # server is reflected here the next time the person signs in rather than whenever someone
    # remembers to look
    claims = sso.capability_claims(userinfo, tokens.get("scope", ""))
    if not identity["subject"]:
        raise _auth_server_failed("Auth server returned no subject claim to know the person by")

    user = await _account_for(session, identity)
    if user is None:
        user = await _new_account(session, identity)
        user.claims = json.dumps(claims) if claims else None
        user.claims_seen_at = utcnow()
        await session.commit()
        await session.refresh(user)
        if user.approval == "pending":
            log.info("SSO sign-in request from %s (%s) waits for an admin", user.name, user.email)
            return _back_to_login("requested")
    else:
        if user.approval == "pending":
            return _back_to_login("pending")
        if not user.is_active:
            # Signing in again must not undo an admin disabling the account
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been disabled")
        if identity["email_verified"]:
            user.email = identity["email"]
        # Listed, verified admin emails are always promoted; otherwise keep the role set in the UI
        if sso.role_for(identity["email"], identity["email_verified"]) == "admin":
            user.role = "admin"
        user.claims = json.dumps(claims) if claims else None
        user.claims_seen_at = utcnow()
        await session.commit()
        await session.refresh(user)

    response = RedirectResponse(sso.safe_next(state_data.get("next")), status_code=303)
    response.set_cookie(sso.SESSION_COOKIE, sso.issue_session(user), max_age=sso.SESSION_MAX_AGE,
                        httponly=True, samesite="lax", secure=sso.cookie_secure(request), path="/")
    response.delete_cookie(sso.STATE_COOKIE, path="/auth")
    return response


def _back_to_login(notice):
    response = RedirectResponse(f"/login?notice={notice}", status_code=303)
    response.delete_cookie(sso.STATE_COOKIE, path="/auth")
    return response


async def _account_for(session, identity):
    """The account this auth-server identity already has, or None.

    By the auth server's own id. Before accounts carried it they were found by name - the email, or
    the display name - so one made that way is taken over once, by the person it was made for:
    only an account an SSO sign-in created (it has seen claims), with no password, not linked to
    anybody yet, and only on an email the auth server has verified. A manager's pre-made account,
    the built-in admin, and anyone merely calling themselves by somebody's name are never matched.
    """
    key = sso.subject_key(identity["subject"])
    found = (await session.execute(select(User).where(User.sso_subject == key))).scalar_one_or_none()
    if found is not None or not identity["email_verified"]:
        return found
    legacy = (await session.execute(select(User).where(
        User.name == identity["email"], User.sso_subject.is_(None), User.password_hash.is_(None),
        User.claims_seen_at.is_not(None)))).scalar_one_or_none()
    if legacy is not None:
        log.info("SSO account %s now linked to the auth server's id", legacy.name)
        legacy.sso_subject = key
    return legacy


async def _free_name(session, wanted, subject):
    """The account name to use: what the auth server calls them, unless that name is taken - by a
    local account, say, which an SSO sign-in must never become."""
    taken = select(User.id).where(User.name == wanted)
    if (await session.execute(taken)).first() is None:
        return wanted
    return f"{wanted[:100]} (sso {subject[:12]})"


async def _new_account(session, identity):
    """A first sign-in: a request for an admin, unless it is a listed and verified admin email.

    Nothing is usable until an admin approves it on the Users page; approved, it gets the public
    models (gateway.public.models) and no more, until somebody grants more.
    """
    admin = sso.role_for(identity["email"], identity["email_verified"]) == "admin"
    user = User(name=await _free_name(session, identity["name"], identity["subject"]),
                email=identity["email"] if identity["email_verified"] else None,
                role="admin" if admin else "user", model_access="all" if admin else "public",
                sso_subject=sso.subject_key(identity["subject"]),
                approval=None if admin else "pending", is_active=admin)
    session.add(user)
    return user


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {
        "name": user.name, "email": user.email, "role": user.role,
        "model_access": "all" if user.role == "admin" else (user.model_access or "all"),
        "allowed_models": access.parse_patterns(user.allowed_models),
        "keys": [{"id": k.id, "name": k.name, "prefix": k.key_prefix, "is_active": k.is_active}
                 for k in user.api_keys if k.is_active],
    }


class DesktopModelIn(BaseModel):
    model: str | None = Field(default=None, max_length=256)


@router.put("/desktop-model")
async def set_desktop_model(payload: DesktopModelIn, principal: Principal = Depends(authenticate),
                            session: AsyncSession = Depends(get_session)):
    """The model this person's Claude desktop app runs (app/claude_desktop.py), or none for the defaults.
    Set by `ai-gateway launch claude-desktop -Model x` with the person's own key."""
    if principal.user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The master key has no desktop app to set")
    chosen = (payload.model or "").strip() or None
    if chosen and chosen != "auto":
        models = await backends.available_models()
        if chosen not in models:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"There is no model {chosen} here")
        if not access.can_use_model(principal, chosen):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"You do not have access to {chosen}")
    user = await session.get(User, principal.user.id)
    user.desktop_model = chosen
    await session.commit()
    principal.user.desktop_model = chosen
    return {"desktop_model": chosen, "mapping": await claude_desktop.mapping(principal)}


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
async def logout(session: AsyncSession = Depends(get_session),
                 gateway_session: str | None = Cookie(default=None, alias=sso.SESSION_COOKIE)):
    from app.routers.ui import end_sessions
    await end_sessions(session, gateway_session)
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
