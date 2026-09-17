"""Records what each request cost, for per-user usage reporting."""
import json

from app.db import session_factory
from app.models import UsageRecord


async def record(principal, model, backend, endpoint, streamed, status_code, usage, latency_ms, error=None):
    usage = usage or {}
    try:
        async with session_factory()() as session:
            session.add(UsageRecord(
                user_id=principal.user.id if principal.user else None,
                api_key_id=principal.api_key.id if principal.api_key else None,
                model=model or "unknown",
                backend=backend or "unknown",
                endpoint=endpoint,
                streamed=streamed,
                status_code=status_code,
                prompt_tokens=usage.get("prompt_tokens") or 0,
                completion_tokens=usage.get("completion_tokens") or 0,
                total_tokens=usage.get("total_tokens") or 0,
                latency_ms=int(latency_ms),
                error=error,
            ))
            await session.commit()
    except Exception:
        # Usage accounting must never break a working request
        pass


def usage_from_sse_line(line):
    """Pulls the usage object out of an OpenAI SSE data line, if it carries one."""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        chunk = json.loads(payload)
    except ValueError:
        return None
    return chunk.get("usage") or None
