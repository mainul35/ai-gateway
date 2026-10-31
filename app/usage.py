"""Records what each request cost, for per-user usage reporting, and how fast it was answered."""
import json
import time

from sqlalchemy import case, func, select

from app.db import session_factory
from app.models import UsageRecord


async def record(principal, model, backend, endpoint, streamed, status_code, usage, latency_ms, error=None,
                 pace=None):
    usage = usage or {}
    speed = pace.figures(usage) if pace else {}
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
                **speed,
            ))
            await session.commit()
    except Exception:
        # Usage accounting must never break a working request
        pass


class Pace:
    """How fast one answer came, for comparing engines and models on the same work.

    Time to first token is measured here, from the moment the request arrived, so it includes
    loading the model and reading the prompt: what the person or agent waited. Prefill and decode
    speeds are the engine's own when it reports them - llama-server sends `timings` with its last
    chunk, and says there how much of the prompt it took from its cache - and otherwise decode is
    worked out from when the tokens arrived. Fed every line of a stream (OpenAI chunks or Messages
    API events), or a whole answer at once.
    """

    def __init__(self, started):
        self.started = started
        self.first = None
        self.last = None
        self.timings = None
        self.cached = None

    def saw(self, line):
        if not line.startswith("data:"):
            return
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            return
        if isinstance(event, dict):
            self.saw_payload(event, streamed=True)

    def saw_payload(self, payload, streamed=False):
        self.timings = payload.get("timings") or self.timings
        details = (payload.get("usage") or {}).get("prompt_tokens_details") or {}
        if details.get("cached_tokens") is not None:
            self.cached = details["cached_tokens"]
        if streamed and self._carries_tokens(payload):
            self.last = time.monotonic()
            self.first = self.first or self.last

    @staticmethod
    def _carries_tokens(payload):
        if payload.get("type") == "content_block_delta":        # Messages API
            return True
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            if (delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")
                    or delta.get("tool_calls") or choice.get("text")):
                return True
        return False

    def figures(self, usage):
        timings = self.timings or {}
        found = {}
        if self.first is not None:
            found["ttft_ms"] = int((self.first - self.started) * 1000)
        decode = timings.get("predicted_per_second")
        produced = (usage or {}).get("completion_tokens") or 0
        if not decode and self.first is not None and self.last > self.first and produced > 1:
            decode = (produced - 1) / (self.last - self.first)
        if decode:
            found["decode_tps"] = round(float(decode), 2)
        if timings.get("prompt_per_second"):
            found["prefill_tps"] = round(float(timings["prompt_per_second"]), 2)
        cached = timings.get("cache_n") if timings.get("cache_n") is not None else self.cached
        if cached is not None:
            found["cached_tokens"] = int(cached)
        return found


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
