"""Secrets the gateway keeps in its database for people - their GitHub tokens - encrypted at rest.

AES-GCM with a key of its own in config/encryption.key: created on first use, readable by the gateway's
user alone, never in the repository, and left alone by deploys (they ship only the files they list).
A database dump or backup on its own therefore holds no usable token. Losing the key file means the
stored tokens can no longer be read, and people connect their GitHub again; nothing else depends on it.
"""
import base64
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.settings import PROJECT_ROOT

KEY_FILE = os.path.join(PROJECT_ROOT, "config", "encryption.key")
_key = None


def _load_key():
    global _key
    if _key is None:
        if not os.path.exists(KEY_FILE):
            os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
            # O_EXCL: two workers starting at once must not each write a key of their own
            try:
                fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(base64.b64encode(secrets.token_bytes(32)).decode())
            except FileExistsError:
                pass
        with open(KEY_FILE, encoding="utf-8") as f:
            _key = base64.b64decode(f.read().strip())
    return _key


def seal(plain):
    """Text in, "v1:<nonce+ciphertext>" out."""
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_load_key()).encrypt(nonce, plain.encode(), b"ai-gateway")
    return "v1:" + base64.b64encode(nonce + sealed).decode()


def unseal(stored):
    """The text back; ValueError when it cannot be read (another key, or damaged)."""
    if not stored or not stored.startswith("v1:"):
        raise ValueError("not a sealed value")
    raw = base64.b64decode(stored[3:])
    try:
        return AESGCM(_load_key()).decrypt(raw[:12], raw[12:], b"ai-gateway").decode()
    except Exception as e:      # InvalidTag: the key changed since it was stored
        raise ValueError("this value was sealed with another key") from e
