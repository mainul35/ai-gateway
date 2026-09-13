"""Settings from a .properties file (key=value), with environment variables as a fallback."""
import os
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_FILE = os.path.join(PROJECT_ROOT, "config", "config.properties")

_lock = threading.Lock()
_cache = {"path": None, "mtime": None, "properties": {}}


def config_file_path():
    return os.getenv("CONFIG_FILE") or DEFAULT_CONFIG_FILE


def parse_properties(text):
    properties = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        # Split on the first "=" only, so values like URLs and tokens can contain ":" and "="
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value.strip()
    return properties


def _properties():
    path = config_file_path()
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    with _lock:
        # Re-read only when the file changed, so edits apply without restarting the app
        if _cache["path"] != path or _cache["mtime"] != mtime:
            try:
                with open(path, encoding="utf-8") as f:
                    _cache.update(path=path, mtime=mtime, properties=parse_properties(f.read()))
            except OSError:
                return {}
        return _cache["properties"]


def get(key, env_var=None, default=None):
    """Returns the property if it has a value, otherwise the environment variable, otherwise the default."""
    value = _properties().get(key)
    if value:
        return value
    if env_var and os.getenv(env_var):
        return os.getenv(env_var)
    return default


def get_float(key, env_var=None):
    value = get(key, env_var)
    try:
        return float(value) if value else None
    except ValueError:
        return None
