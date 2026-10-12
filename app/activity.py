"""Is the gateway doing anything? Answers the homelab's idle check (ops/idle-shutdown).

The machine sleeps when nothing is happening, so "nothing" has to be known exactly: a coding task
can sit for minutes between model calls with the GPU at 0%, and a picture job can be waiting on
ComfyUI. GPU and CPU numbers miss those gaps; the gateway knows.

Counted as activity:
  - an HTTP request still being answered, until its last byte is sent (a streamed answer counts
    for as long as it streams)
  - the time of the last request, so a quick one between two idle checks still resets the clock
  - a coding task running or queued, and a deploy being watched
  - a playground job (chat, picture, photo) that has not finished
  - a request in flight to a llama.cpp engine

Not counted: health checks, this endpoint, the remote-folder long-poll, and the GETs a web page
repeats on a timer (dashboard stats every 15 s, the task list): an open tab is not someone working,
and counting it would keep the machine awake for as long as the tab stays open. A coding task waiting for
approval is not running either, so it does not keep the machine up; approving it later wakes it.
"""
import re
import time

from app.coding import runner as coding_runner
from app.engine.supervisor import supervisor
from app.tools import jobs

_IGNORED = re.compile(r"^/(health|internal/activity|coding/remote/\d+/next)$")
# GETs the web pages repeat on a timer (wake/wake_front.py has the same list). From a browser they
# are a tab refreshing itself, not someone working, so an open dashboard does not keep the machine up.
POLLED = re.compile(r"^/(admin/system|admin/usage|v1/models|coding/projects|coding/tasks/\d+/events)$")


def _is_background_poll(scope):
    if scope.get("method") not in ("GET", "HEAD") or not POLLED.match(scope.get("path", "")):
        return False
    headers = dict(scope.get("headers") or [])
    mode = headers.get(b"sec-fetch-mode")
    return mode is not None and mode != b"navigate"


_in_flight = 0
_last_request_at = None   # wall-clock seconds


class ActivityMiddleware:
    """Pure ASGI, so a streamed response is counted until it ends, not until its headers go out."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or _IGNORED.match(scope.get("path", "")) or _is_background_poll(scope):
            return await self.app(scope, receive, send)

        global _in_flight, _last_request_at
        _in_flight += 1
        _last_request_at = time.time()
        try:
            # Returns only once the whole body has been sent, streamed or not
            await self.app(scope, receive, send)
        finally:
            _in_flight -= 1
            # A long stream that just ended is as recent as a request that just arrived
            _last_request_at = time.time()


def snapshot(ignore_requests=0):
    """`ignore_requests`: requests to leave out of the count, e.g. the one asking."""
    in_flight = max(0, _in_flight - ignore_requests)
    running_jobs = [job for job in list(jobs._jobs.values()) if not job.done]
    engine_requests = sum(model.in_flight for model in list(supervisor._running.values()))
    coding = coding_runner.activity()
    busy_reasons = []
    if in_flight:
        busy_reasons.append(f"{in_flight} request(s) in flight")
    if coding["running"] is not None:
        busy_reasons.append(f"coding task {coding['running']} running")
    if coding["queued"]:
        busy_reasons.append(f"{coding['queued']} coding task(s) queued")
    if coding["deploys"]:
        busy_reasons.append(f"{coding['deploys']} deploy(s) being watched")
    if running_jobs:
        busy_reasons.append(f"{len(running_jobs)} playground job(s) running")
    if engine_requests:
        busy_reasons.append(f"{engine_requests} engine request(s) in flight")
    return {
        "busy": bool(busy_reasons),
        "reasons": busy_reasons,
        "in_flight": in_flight,
        "seconds_since_last_request": None if _last_request_at is None else round(time.time() - _last_request_at),
        "coding": coding,
        "jobs_running": len(running_jobs),
        "engine_requests": engine_requests,
    }


def is_local_request(request):
    """Only the machine itself may ask: a request that came through a proxy or tunnel carries one
    of these headers even when it reaches us from 127.0.0.1."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1"):
        return False
    forwarded = ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip")
    return not any(name in request.headers for name in forwarded)
