"""Records what each request cost, for per-user usage reporting."""
import json

from sqlalchemy import case, func, select

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


async def by_model(session, since):
    """Requests, failures, latency and last use per model since a moment, keyed by model name.

    Both the admin model list and the model chooser want this: one to show an operator what has been
    going wrong, the other to stop picking a model that has been going wrong.
    """
    rows = (await session.execute(
        select(UsageRecord.model, func.count(UsageRecord.id),
               func.sum(case((UsageRecord.status_code >= 400, 1), else_=0)),
               func.max(UsageRecord.created_at), func.avg(UsageRecord.latency_ms),
               func.sum(UsageRecord.total_tokens))
        .where(UsageRecord.created_at >= since).group_by(UsageRecord.model)
    )).all()
    return {
        model: {"requests": int(requests or 0), "failures": int(failures or 0),
                "last_used": last.isoformat() if last else None, "_last": last,
                "avg_latency_ms": int(latency or 0), "tokens": int(tokens or 0), "last_error": None}
        for model, requests, failures, last, latency, tokens in rows
    }
