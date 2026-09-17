"""Local password hashing, for the built-in admin account (SSO users have no password)."""
import base64
import hashlib
import hmac
import os

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password, stored):
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, salt_b64, digest_b64 = stored.split("$", 2)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=len(expected))
    return hmac.compare_digest(candidate, expected)
