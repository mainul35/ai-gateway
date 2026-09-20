"""The web interface: login, dashboard, user management and SSO settings."""
import os

from fastapi import APIRouter, Cookie, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings, sso, throttle
from app.db import get_session
from app.models import User
from app.security import verify_password

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

router = APIRouter(tags=["ui"], include_in_schema=False)
templates = Jinja2Templates(directory=TEMPLATES_DIR)


async def signed_in_user(session: AsyncSession, cookie_value):
    data = sso.read_session(cookie_value)
    if not data:
        return None
    user = await session.get(User, data["user_id"])
    return user if user and user.is_active else None


async def current_user(session: AsyncSession = Depends(get_session),
                       gateway_session: str | None = Cookie(default=None, alias=sso.SESSION_COOKIE)):
    return await signed_in_user(session, gateway_session)


def _page(request, name, user, **context):
    # Never cached: the page carries the whole interface, so a browser holding yesterday's copy runs
    # yesterday's playground against today's gateway, and the mismatch looks like a broken feature.
    return templates.TemplateResponse(request, name, {"user": user, **context},
                                      headers={"Cache-Control": "no-store"})


def _require_login(user, path):
    """Pages redirect to the login screen instead of returning 401 like the API does."""
    return None if user else RedirectResponse(f"/login?next={path}", status_code=303)


@router.get("/")
async def root(user: User | None = Depends(current_user)):
    return RedirectResponse("/dashboard" if user else "/login", status_code=303)


@router.get("/login")
async def login_page(request: Request, next: str = "/dashboard", error: str | None = None,
                     user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse(next, status_code=303)
    return _page(request, "login.html", None, next=next, error=error,
                 sso_configured=sso.is_configured(), sso_name=settings.get("sso.display.name") or "single sign-on")


@router.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                       next: str = Form("/dashboard"), session: AsyncSession = Depends(get_session)):
    # Tunnelled traffic arrives from Cloudflare, so trust its client-IP header when present
    client_ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")
    throttle_key = f"{client_ip}:{username}"

    def login_failed(message):
        return _page(request, "login.html", None, next=next, error=message,
                     sso_configured=sso.is_configured(),
                     sso_name=settings.get("sso.display.name") or "single sign-on")

    if throttle.is_blocked(throttle_key):
        wait = throttle.seconds_until_unblocked(throttle_key)
        return login_failed(f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).")

    result = await session.execute(select(User).where(User.name == username))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash or ""):
        throttle.record_failure(throttle_key)
        return login_failed("Wrong username or password")
    throttle.clear(throttle_key)
    response = RedirectResponse(next if next.startswith("/") else "/dashboard", status_code=303)
    response.set_cookie(sso.SESSION_COOKIE, sso.issue_session(user), max_age=sso.SESSION_MAX_AGE,
                        httponly=True, samesite="lax", path="/")
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(sso.SESSION_COOKIE, path="/")
    return response


@router.get("/dashboard")
async def dashboard(request: Request, user: User | None = Depends(current_user)):
    return _require_login(user, "/dashboard") or _page(request, "dashboard.html", user)


def _require_role(user, path, roles):
    """Signed out goes to login; signed in without the role goes back to the dashboard."""
    if not user:
        return _require_login(user, path)
    return None if user.role in roles else RedirectResponse("/dashboard", status_code=303)


@router.get("/users")
async def users_page(request: Request, user: User | None = Depends(current_user)):
    return _require_role(user, "/users", ("manager", "admin")) or _page(request, "users.html", user)


@router.get("/playground")
async def playground_page(request: Request, user: User | None = Depends(current_user)):
    return _require_login(user, "/playground") or _page(request, "playground.html", user)


@router.get("/settings/sso")
async def sso_settings_page(request: Request, user: User | None = Depends(current_user)):
    redirect_uri = f"{settings.public_base_url() or str(request.base_url).rstrip('/')}/auth/callback"
    return _require_role(user, "/settings/sso", ("admin",)) or \
        _page(request, "sso.html", user, redirect_uri=redirect_uri)
