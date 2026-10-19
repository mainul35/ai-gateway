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
    return _serializer("session").dumps({"user_id": user.id, "name": user.name, "issued_at": int(time.time()),
                                         "epoch": user.session_epoch or 0})


def session_current(data, user):
    """Whether a session read from a cookie is still this user's: not signed out since it was made."""
    return bool(data) and user is not None and data.get("epoch", 0) == (user.session_epoch or 0)


def cookie_secure(request):
    """Session cookies only over HTTPS when the page came over HTTPS - the public address always
    does (Cloudflare says so in X-Forwarded-Proto) - so a plain-HTTP test on the LAN still works."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() == "https"


def safe_next(target, default="/dashboard"):
    """Where to go after signing in: a path on this site and nothing else. "//evil.example" and
    "/\\evil.example" are other sites to a browser, though they start with a slash."""
    target = target or ""
    if (not target.startswith("/") or target.startswith(("//", "/\\"))
            or any(ord(ch) < 32 or ch == "\\" for ch in target)):
        return default
    return target


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
    """Swaps the authorization code for an access token: (tokens, None, 200) or (None, why, status).

    The status is the one to answer the browser with. A 400 from the token endpoint means it looked
    at this code and refused it - used already, expired, or from another attempt - which starting
    again fixes. Anything else is the auth server failing or misconfigured, and gets a 503 rather than
    a 502, which the Cloudflare tunnel would swap for its own page and lose the reason with it.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.sso_client_id(),
        "code_verifier": verifier,
    }
    secret = settings.sso_client_secret()
    auth = (settings.sso_client_id(), secret) if secret else None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(settings.sso_token_url(), data=data, auth=auth,
                                         headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        return None, f"Could not reach the token endpoint: {e}", 503
    if response.status_code == 400:
        return None, (f"The auth server refused this sign-in: {response.text[:200]}. "
                      "Start the sign-in again."), 400
    if response.status_code >= 400:
        return None, f"Token endpoint returned {response.status_code}: {response.text[:200]}", 503
    try:
        return response.json(), None, 200
    except ValueError:
        return None, "Token endpoint did not return JSON", 503


async def fetch_userinfo(access_token):
    url = settings.sso_userinfo_url()
    if not url:
        return None, "No userinfo endpoint configured"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {access_token}",
                                                      "Accept": "application/json"})
    except httpx.HTTPError as e:
        return None, f"Could not reach the userinfo endpoint: {e}"
    if response.status_code >= 400:
        return None, f"Userinfo endpoint returned {response.status_code}"
    try:
        return response.json(), None
    except ValueError:
        return None, "Userinfo endpoint did not return JSON"


# Claims that say who somebody is, which are already kept in their own columns, and the ones a
# token carries about itself. Everything else is what an auth server has chosen to tell us, which
# is where a capability would arrive.
IDENTITY_CLAIMS = ("sub", "iss", "aud", "exp", "iat", "nbf", "jti", "at_hash", "auth_time", "azp",
                   "nonce", "sid", "typ", "session_state")


def capability_claims(userinfo, granted_scope=""):
    """What the auth server says about a user beyond their name, ready to build rules on.

    Nothing is interpreted here. The shapes providers use differ - a space-separated scope string,
    a list of roles, a nested object per client - and guessing at one is how an integration ends up
    quietly allowing everything. This keeps what arrived, and something that understands the
    provider decides what it means.
    """
    kept = {key: value for key, value in (userinfo or {}).items() if key not in IDENTITY_CLAIMS}
    if granted_scope:
        kept.setdefault("scope", granted_scope)
    return kept


def _issuer():
    """Which auth server this is, for telling its subjects apart from another's."""
    from urllib.parse import urlparse
    return (urlparse(settings.sso_authorize_url() or "").hostname or "sso").lower()


def subject_key(subject):
    return f"{_issuer()}|{subject}"


def _verified(userinfo):
    """Whether the auth server vouches for the email. An email it has not checked is just text the
    person typed, so it decides nothing here: not which account, not whether they are an admin.
    sso.require.verified.email=false trusts every email, for an auth server that never sends the
    claim and only ever holds checked addresses."""
    if not settings.get_bool("sso.require.verified.email", "SSO_REQUIRE_VERIFIED_EMAIL", True):
        return True
    claim = settings.get("sso.claim.email_verified", "SSO_CLAIM_EMAIL_VERIFIED") or "email_verified"
    value = (userinfo or {}).get(claim)
    return value is True or str(value).strip().lower() == "true"


def identity_from_userinfo(userinfo):
    """Maps provider claims onto our user fields, using the configured claim names.

    `subject` is what the account is found by; `name` is only what it is called here, the email
    when there is one, else the display name, else the subject.
    """
    subject = str(userinfo.get(settings.sso_claim_id()) or "").strip()
    email = str(userinfo.get(settings.sso_claim_email()) or "").strip()
    name = str(userinfo.get(settings.sso_claim_name()) or "").strip()
    account = email or name or subject
    return {"subject": subject, "email": email or None, "name": account[:120] or None,
            "email_verified": bool(email) and _verified(userinfo)}


def role_for(email, verified=True):
    """Admin only for a listed email the auth server has verified: an unverified one is whatever
    anybody chose to register with, the admin's own address included."""
    admins = [a.strip().lower() for a in (settings.sso_admin_emails() or "").split(",") if a.strip()]
    return "admin" if email and verified and email.lower() in admins else "user"
