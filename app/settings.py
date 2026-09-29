"""Gateway settings, read from config/config.properties with environment fallback."""
import os
import secrets

from utils import config

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    "features.maps.enabled": "true",
    # Empty means OpenStreetMap, which needs nothing. A Google Maps Platform key switches the
    # whole feature over - results and the map together, because their terms forbid mixing.
    "maps.google.key": "",
    "maps.provider": "auto",            # auto, osm, google

    "search.searxng.url": "http://127.0.0.1:8888",
    "search.results": "6",
    "search.fetch_pages": "4",
    "search.language": "en",
    "images.comfyui.url": "http://127.0.0.1:8188",
    "images.checkpoint": "flux1CompactCLIPAnd_Flux1DevFp16.safetensors",
    "images.model_name": "flux-dev",
    "images.steps": "20",
    "images.guidance": "3.5",
    "images.max_upload_mb": "25",
    # A request is rewritten into something an image model can draw, with the conversation for context
    "images.rewrite.prompt": "true",
    # Lettering drawn by Flux is usually misspelled; the editing model puts it right afterwards
    "images.fix.text": "true",
    "images.text.attempts": "2",
    # Photographs: noise removed without inventing detail, and blur added by distance
    "photo.denoise.model": "scunet_color_real_psnr.pth",
    "photo.upscale.model": "RealESRGAN_x2plus.pth",
    "photo.depth.model": "models/depth-anything-v2-small.onnx",
    "photo.cutout.model": "models/birefnet-lite.onnx",
    "images.prompt.model": "",
    # A request is rewritten into something an image model can draw, with the conversation for context
    "images.rewrite.prompt": "true",
    "images.prompt.model": "",
    # Small model that decides what a message asks for, and writes search queries and summaries
    "router.model": "qwen3:1.7b",
    # Playground memory: a running summary per conversation, durable notes per user
    "memory.enabled": "true",
    # Empty means each conversation's own model writes its notes; it is already loaded
    "memory.model": "",
    "memory.summarize.every": "8",
    "memory.keep.recent": "8",
    "memory.max.user.notes": "20",
    # Instruction-based editing; when these files are not in ComfyUI, edits fall back to Flux image-to-image
    "images.edit.model": "qwen_image_edit_2511_fp8mixed.safetensors",
    "images.edit.text_encoder": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
    "images.edit.vae": "qwen_image_vae.safetensors",
    "images.edit.lora": "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
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


def search_language():
    """Language asked of the search engines; "all" to take whatever comes back."""
    return get("search.language", "SEARCH_LANGUAGE")


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


def google_maps_key():
    return get("maps.google.key", "GOOGLE_MAPS_KEY")


def router_model():
    """Set to nothing to fall back to keyword rules instead."""
    return get("router.model", "ROUTER_MODEL")


def memory_model():
    """Set to pin summaries to one model; empty lets each conversation use its own."""
    return get("memory.model", "MEMORY_MODEL")


def summarize_every():
    """How many new messages may pile up before the conversation summary is refreshed."""
    return int(get("memory.summarize.every", "MEMORY_SUMMARIZE_EVERY"))


def keep_recent_messages():
    """Recent turns always sent in full; everything older is covered by the summary."""
    return int(get("memory.keep.recent", "MEMORY_KEEP_RECENT"))


def max_user_notes():
    return int(get("memory.max.user.notes", "MEMORY_MAX_USER_NOTES"))


def fix_image_text():
    return get_bool("images.fix.text", "IMAGES_FIX_TEXT", True)


def text_attempts():
    """How many times the editing model may try to get the lettering right."""
    return max(1, int(get("images.text.attempts", "IMAGES_TEXT_ATTEMPTS")))


def denoise_model():
    return get("photo.denoise.model", "PHOTO_DENOISE_MODEL")


def upscale_model():
    return get("photo.upscale.model", "PHOTO_UPSCALE_MODEL")


def depth_model_path():
    path = get("photo.depth.model", "PHOTO_DEPTH_MODEL") or ""
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def cutout_model_path():
    path = get("photo.cutout.model", "PHOTO_CUTOUT_MODEL") or ""
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def edit_model():
    return get("images.edit.model", "IMAGES_EDIT_MODEL")


def edit_text_encoder():
    return get("images.edit.text_encoder", "IMAGES_EDIT_TEXT_ENCODER")


def edit_vae():
    return get("images.edit.vae", "IMAGES_EDIT_VAE")


def edit_lora():
    """Lightning LoRA: 4 steps instead of 40. Empty runs the full model."""
    return get("images.edit.lora", "IMAGES_EDIT_LORA")


def max_upload_bytes():
    return int(float(get("images.max_upload_mb", "IMAGES_MAX_UPLOAD_MB")) * 1024 * 1024)


def public_base_url():
    """Public URL of this gateway, used to build the OAuth2 redirect URI."""
    return (get("gateway.public.url", "GATEWAY_PUBLIC_URL") or "").rstrip("/")
