"""How much one person may ask of this one machine.

The gateway is public and runs on a single GPU, so one account - a script with a key, or anyone the
auth server lets in - could otherwise keep it busy for everybody: requests in a loop, answers
asked to run to a hundred thousand tokens, one picture job after another, files until the database
disk fills. Admins and the master key are never limited; nor are the gateway's own helper calls
(routing, search queries), which are not the user's to spend.

Every limit is a setting, and 0 turns it off:

    limits.requests.per_minute      model requests per person per minute          (default 60)
    limits.tokens.per_day           tokens per person per day (UTC), any access   (default 0, off)
    limits.tokens.per_day.public    the same, for accounts with "public" access   (default 200000)
    limits.max_tokens               the most output one request may ask for       (default 8192)
    limits.gpu_jobs                 pictures and subtitles running at once, each  (default 1)
    limits.files.mb                 stored files per person, in MB                (default 1000)
    limits.attachments              files attached to one message                 (default 10)

Counts are kept in this process: a restart forgets the per-minute ones, which is fine for what they
are for. The daily token count comes from the usage records, so it survives restarts.
"""
import asyncio
import collections
import time
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select

from app import settings

DEFAULTS = {
    "limits.requests.per_minute": 60,
    "limits.tokens.per_day": 0,
    "limits.tokens.per_day.public": 200_000,
    "limits.max_tokens": 8192,
    "limits.gpu_jobs": 1,
    "limits.files.mb": 1000,
    "limits.attachments": 10,
}
TOKEN_CACHE_SECONDS = 30

_requests: dict[int, collections.deque] = collections.defaultdict(collections.deque)
_gpu_jobs: dict[int, int] = collections.defaultdict(int)
_tokens_today: dict[int, tuple[str, int, float]] = {}
_lock = asyncio.Lock()


def limit(name):
    raw = settings.get(name, name.upper().replace(".", "_"))
    try:
        return max(0, int(float(raw))) if raw not in (None, "") else DEFAULTS[name]
    except ValueError:
        return DEFAULTS[name]


def exempt(principal):
    return principal is None or principal.is_master or principal.is_admin


def _too_many(message, retry_after):
    return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, message,
                         headers={"Retry-After": str(max(1, int(retry_after)))})


async def check_request(principal):
    """One model request: within the per-minute rate and the day's tokens, or a 429 saying which."""
    if exempt(principal) or principal.user is None:
        return
    user_id = principal.user.id
    per_minute = limit("limits.requests.per_minute")
    if per_minute:
        now = time.monotonic()
        async with _lock:
            recent = _requests[user_id]
            while recent and now - recent[0] > 60:
                recent.popleft()
            if len(recent) >= per_minute:
                raise _too_many(f"More than {per_minute} requests a minute; wait a moment and try again",
                                60 - (now - recent[0]))
            recent.append(now)
    daily = limit("limits.tokens.per_day.public") if principal.user.model_access == "public" \
        else limit("limits.tokens.per_day")
    if daily:
        used = await tokens_today(user_id)
        if used >= daily:
            midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            left = 86400 - (datetime.now(timezone.utc) - midnight).total_seconds()
            raise _too_many(f"Today's allowance of {daily:,} tokens is used up; it starts again at "
                            "midnight UTC", left)


async def tokens_today(user_id):
    """Tokens this person has used since midnight UTC, from the usage records (cached briefly)."""
    from app.db import session_factory
    from app.models import UsageRecord

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cached = _tokens_today.get(user_id)
    if cached and cached[0] == day and time.monotonic() - cached[2] < TOKEN_CACHE_SECONDS:
        return cached[1]
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_factory()() as session:
        used = (await session.execute(
            select(func.coalesce(func.sum(UsageRecord.total_tokens), 0))
            .where(UsageRecord.user_id == user_id, UsageRecord.created_at >= midnight))).scalar() or 0
    _tokens_today[user_id] = (day, int(used), time.monotonic())
    return int(used)


def cap_output(principal, body, fields=("max_tokens", "max_completion_tokens")):
    """The output a request asks for, no more than limits.max_tokens; set to it when not asked."""
    if exempt(principal):
        return body
    most = limit("limits.max_tokens")
    if not most:
        return body
    body = dict(body)
    for field in fields:
        if field in body:
            try:
                body[field] = min(int(body[field]), most)
            except (TypeError, ValueError):
                body[field] = most
    if not any(field in body for field in fields):
        body[fields[0]] = most
    return body


async def take_gpu_slot(principal):
    """A picture or a transcription starting: limits.gpu_jobs of them at once per person, else a 429.

    Returns the slot to hand to holding(), which gives it back when the job's stream ends - the
    endpoint returns long before the work does, so a `with` around it would free the slot at once.
    """
    if exempt(principal) or principal.user is None:
        return None
    user_id = principal.user.id
    most = limit("limits.gpu_jobs")
    async with _lock:
        if most and _gpu_jobs[user_id] >= most:
            raise _too_many("You already have a picture or transcription running; wait for it to finish", 30)
        _gpu_jobs[user_id] += 1
    return user_id


async def _give_back(slot):
    if slot is not None:
        async with _lock:
            _gpu_jobs[slot] = max(0, _gpu_jobs[slot] - 1)


async def holding(slot, stream):
    """The job's stream, giving its slot back however it ends: finished, failed or abandoned."""
    try:
        async for item in stream:
            yield item
    finally:
        await _give_back(slot)


async def check_storage(principal, session, adding):
    """Room for `adding` more bytes of this person's files, or a 413."""
    if exempt(principal) or principal.user is None:
        return
    most = limit("limits.files.mb")
    if not most:
        return
    from app.models import ChatFile

    stored = (await session.execute(select(func.coalesce(func.sum(func.length(ChatFile.data)), 0))
                                    .where(ChatFile.user_id == principal.user.id))).scalar() or 0
    if stored + adding > most * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"Your files take {stored / 1024 / 1024:.0f} MB of the {most} MB you have here; "
                            "delete some conversations with attachments to make room")


def check_attachments(principal, count):
    most = limit("limits.attachments")
    if not exempt(principal) and most and count > most:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"At most {most} files can be attached to one message")
