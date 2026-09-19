"""Gateway settings, read from config/config.properties with environment fallback."""
import secrets

from utils import config

DEFAULTS = {
    "gateway.models.file": "config/models.yaml",
    "gateway.discovery.ttl.seconds": "30",
    "gateway.request.timeout.seconds": "600",
    "engine.profiles.file": "config/engines.yaml",
    "sso.scope": "openid profile email",
    "sso.claim.id": "sub",
    "sso.claim.email": "email",
    "sso.claim.name": "name",
    "features.web_search.enabled": "true",
    "features.vision.enabled": "true",
    "features.image_generation.enabled": "true",
    "search.searxng.url": "http://127.0.0.1:8888",
    "search.results": "5",
    "search.fetch_pages": "3",
    "images.comfyui.url": "http://127.0.0.1:8188",
    "images.checkpoint": "flux1CompactCLIPAnd_Flux1DevFp16.safetensors",
    "images.model_name": "flux-dev",
    "images.steps": "20",
    "images.guidance": "3.5",
    "images.max_upload_mb": "10",
}

# Generated once per process when no master key is configured, so a fresh install still works
_generated_master_key = None


def get(key, env_var=None):
    return config.get(key, env_var, DEFAULTS.get(key))


def get_bool(key, env_var=None, default=False):
    value = get(key, env_var)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def database_url():
    url = get("gateway.database.url", "GATEWAY_DATABASE_URL")
    if not url:
        # Deliberately no default: it would have to contain a password, and credentials never go in the repo
        raise RuntimeError("Set gateway.database.url in config/config.properties or GATEWAY_DATABASE_URL")
    return url


def models_file():
    return get("gateway.models.file", "GATEWAY_MODELS_FILE")


def discovery_ttl():
    return float(get("gateway.discovery.ttl.seconds", "GATEWAY_DISCOVERY_TTL"))


def request_timeout():
    return float(get("gateway.request.timeout.seconds", "GATEWAY_REQUEST_TIMEOUT"))


def master_key():
    """Admin key. Generated at startup if unset; the value is then printed once to the log."""
    global _generated_master_key
    configured = get("gateway.master.key", "GATEWAY_MASTER_KEY")
    if configured:
        return configured
    if _generated_master_key is None:
        _generated_master_key = "sk-master-" + secrets.token_urlsafe(24)
    return _generated_master_key


def master_key_is_generated():
    return not get("gateway.master.key", "GATEWAY_MASTER_KEY")


# --- Single sign-on (OAuth2 authorization code + PKCE) ---

_generated_session_secret = None


def session_secret():
    """Signs login sessions. Generated per process if unset, which logs everyone out on restart."""
    global _generated_session_secret
    configured = get("gateway.session.secret", "GATEWAY_SESSION_SECRET")
    if configured:
        return configured
    if _generated_session_secret is None:
        _generated_session_secret = secrets.token_urlsafe(32)
    return _generated_session_secret


def sso_client_id():
    return get("sso.client.id", "SSO_CLIENT_ID")


def sso_client_secret():
    return get("sso.client.secret", "SSO_CLIENT_SECRET")


def sso_authorize_url():
    return get("sso.authorize.url", "SSO_AUTHORIZE_URL")


def sso_token_url():
    return get("sso.token.url", "SSO_TOKEN_URL")


def sso_userinfo_url():
    return get("sso.userinfo.url", "SSO_USERINFO_URL")


def sso_scope():
    return get("sso.scope", "SSO_SCOPE")


def sso_claim_id():
    return get("sso.claim.id", "SSO_CLAIM_ID")


def sso_claim_email():
    return get("sso.claim.email", "SSO_CLAIM_EMAIL")


def sso_claim_name():
    return get("sso.claim.name", "SSO_CLAIM_NAME")


def sso_admin_emails():
    return get("sso.admin.emails", "SSO_ADMIN_EMAILS")


# --- Playground tools: web search, image understanding, image generation ---

def feature_enabled(name):
    """Server-wide switch for a playground tool; users toggle enabled tools per chat."""
    return get_bool(f"features.{name}.enabled", f"FEATURE_{name.upper()}")


def searxng_url():
    return (get("search.searxng.url", "SEARXNG_URL") or "").rstrip("/")


def search_results():
    return int(get("search.results", "SEARCH_RESULTS"))


def search_fetch_pages():
    return int(get("search.fetch_pages", "SEARCH_FETCH_PAGES"))


def comfyui_url():
    return (get("images.comfyui.url", "COMFYUI_URL") or "").rstrip("/")


def image_checkpoint():
    return get("images.checkpoint", "IMAGES_CHECKPOINT")


def image_model_name():
    """Name image requests are recorded under in usage, and accepted as `model` in /v1/images."""
    return get("images.model_name", "IMAGES_MODEL_NAME")


def image_steps():
    return int(get("images.steps", "IMAGES_STEPS"))


def image_guidance():
    return float(get("images.guidance", "IMAGES_GUIDANCE"))


def max_upload_bytes():
    return int(float(get("images.max_upload_mb", "IMAGES_MAX_UPLOAD_MB")) * 1024 * 1024)


def public_base_url():
    """Public URL of this gateway, used to build the OAuth2 redirect URI."""
    return (get("gateway.public.url", "GATEWAY_PUBLIC_URL") or "").rstrip("/")
