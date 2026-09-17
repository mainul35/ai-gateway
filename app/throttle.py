"""Simple in-memory throttle for failed logins.

The sign-in page is reachable from the internet through the tunnel, so repeated password guesses
from one address are slowed down. State is per process, which is fine for a single gateway.
"""
import time
from collections import defaultdict

MAX_ATTEMPTS = 8
WINDOW_SECONDS = 900  # failures older than this are forgotten

_failures = defaultdict(list)


def _recent(key, now):
    attempts = [t for t in _failures[key] if now - t < WINDOW_SECONDS]
    _failures[key] = attempts
    return attempts


def is_blocked(key):
    return len(_recent(key, time.monotonic())) >= MAX_ATTEMPTS


def seconds_until_unblocked(key):
    attempts = _recent(key, time.monotonic())
    if len(attempts) < MAX_ATTEMPTS:
        return 0
    return max(0, int(WINDOW_SECONDS - (time.monotonic() - attempts[0])))


def record_failure(key):
    now = time.monotonic()
    _recent(key, now)
    _failures[key].append(now)


def clear(key):
    _failures.pop(key, None)
