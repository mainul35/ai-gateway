"""Gateway settings, read from config/config.properties with environment fallback."""
import secrets

from utils import config

DEFAULTS = {
    "gateway.database.url": "postgresql+asyncpg://gateway@localhost:5433/gateway",
    "gateway.models.file": "config/models.yaml",
    "gateway.discovery.ttl.seconds": "30",
    "gateway.request.timeout.seconds": "600",
}

# Generated once per process when no master key is configured, so a fresh install still works
_generated_master_key = None


def get(key, env_var=None):
    return config.get(key, env_var, DEFAULTS.get(key))


def database_url():
    return get("gateway.database.url", "GATEWAY_DATABASE_URL")


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
