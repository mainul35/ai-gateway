"""OAuth2 single sign-on (authorization code flow with PKCE).

Works with any OAuth2 provider that exposes authorize, token and userinfo endpoints, which is what
the VSD auth server provides. No OIDC discovery document is required and there is no user limit:
anyone the provider authenticates gets an account here.
"""
import base64
import hashlib
import secrets
import time

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app import settings

SESSION_COOKIE = "gateway_session"
STATE_COOKIE = "gateway_oauth_state"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # one week
STATE_MAX_AGE = 600  # ten minutes to complete a login


def _serializer(salt):
    return URLSafeTimedSerializer(settings.session_secret(), salt=salt)


def is_configured():
    return bool(settings.sso_client_id() and settings.sso_authorize_url() and settings.sso_token_url())


def make_pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def sign_state(payload):
    return _serializer("oauth-state").dumps(payload)


def read_state(token):
    try:
        return _serializer("oauth-state").loads(token, max_age=STATE_MAX_AGE)
    except BadSignature:
        return None


def issue_session(user):
    return _serializer("session").dumps({"user_id": user.id, "name": user.name, "issued_at": int(time.time())})


def read_session(token):
    if not token:
        return None
    try:
        return _serializer("session").loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None


def authorize_url(redirect_uri, state, challenge):
    params = {
        "response_type": "code",
        "client_id": settings.sso_client_id(),
        "redirect_uri": redirect_uri,
        "scope": settings.sso_scope(),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{settings.sso_authorize_url()}?{httpx.QueryParams(params)}"


async def exchange_code(code, redirect_uri, verifier):
    """Swaps the authorization code for an access token."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.sso_client_id(),
        "code_verifier": verifier,
    }
    secret = settings.sso_client_secret()
    auth = (settings.sso_client_id(), secret) if secret else None
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(settings.sso_token_url(), data=data, auth=auth,
                                     headers={"Accept": "application/json"})
    if response.status_code >= 400:
        return None, f"Token endpoint returned {response.status_code}: {response.text[:200]}"
    try:
        return response.json(), None
    except ValueError:
        return None, "Token endpoint did not return JSON"


async def fetch_userinfo(access_token):
    url = settings.sso_userinfo_url()
    if not url:
        return None, "No userinfo endpoint configured"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {access_token}",
                                                  "Accept": "application/json"})
    if response.status_code >= 400:
        return None, f"Userinfo endpoint returned {response.status_code}"
    try:
        return response.json(), None
    except ValueError:
        return None, "Userinfo endpoint did not return JSON"


def identity_from_userinfo(userinfo):
    """Maps provider claims onto our user fields, using the configured claim names."""
    subject = str(userinfo.get(settings.sso_claim_id()) or "").strip()
    email = str(userinfo.get(settings.sso_claim_email()) or "").strip()
    name = str(userinfo.get(settings.sso_claim_name()) or "").strip()
    # Prefer a stable, human-readable account name; fall back to the subject claim
    account = email or name or subject
    return {"subject": subject, "email": email or None, "name": account or None}


def role_for(email):
    admins = [a.strip().lower() for a in (settings.sso_admin_emails() or "").split(",") if a.strip()]
    return "admin" if email and email.lower() in admins else "user"
