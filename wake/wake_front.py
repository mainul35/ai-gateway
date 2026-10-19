"""Wake front: the always-on doorway to a gateway that sleeps.

Runs on proxy-vm, which never sleeps. Cloudflare Tunnel points here instead of at homelabai.

  homelabai awake   every request is passed straight through, streamed answers included
  homelabai asleep  any authorised request sends a Wake-on-LAN packet and is answered once it is up:
                      - a streamed completion gets its SSE headers at once and ": waking" comments
                        every 10 s until the gateway answers, so Cloudflare's 100 s limit never bites
                      - any other API call is held up to WAKE_HOLD_SECONDS, then passed through, or
                        answered 503 with Retry-After if the machine is not up yet
                      - a page opened in a browser gets a "waking up" page that reloads itself
                    a page's timer polling (dashboard stats, task feeds) is answered 503 and wakes
                    nothing, or an open tab would keep the machine awake forever
                    a launcher download or a static file is served from the copy kept the last
                    time the gateway served it, and wakes nothing

Only a caller that looks authorised can wake the machine, otherwise every scanner on the internet
would keep it awake:
  - a gateway API key that the gateway has accepted before, or the master key (WAKE_MASTER_KEY_SHA256)
  - a valid login cookie (checked with the gateway's own session secret, WAKE_SESSION_SECRET)
  - a request that passed Cloudflare Access (it carries Cf-Access-Jwt-Assertion)
  - an unknown key of the right shape, at most once per WAKE_UNVERIFIED_COOLDOWN; a key the gateway
    then rejects with 401 never wakes anything again
Nothing here can check an unknown key for real - the key database sleeps with the gateway - so the
shape check plus a cooldown is the honest best, and the gateway still authenticates every request.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.background import BackgroundTask

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s wake: %(message)s")
log = logging.getLogger("wake")
logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per proxied request is noise


def _env(name, default=None):
    value = os.environ.get(name, "").strip()
    return value if value else default


UPSTREAM = (_env("WAKE_UPSTREAM") or "").rstrip("/")          # e.g. http://192.168.50.20:9000
WOL_MAC = _env("WAKE_MAC", "")                                 # e.g. a8:a1:59:9d:f4:ae
WOL_BROADCAST = _env("WAKE_BROADCAST", "255.255.255.255")      # e.g. 192.168.50.255
WOL_PORT = int(_env("WAKE_WOL_PORT", "9"))
HOLD_SECONDS = float(_env("WAKE_HOLD_SECONDS", "90"))          # under Cloudflare's 100 s
STREAM_HOLD_SECONDS = float(_env("WAKE_STREAM_HOLD_SECONDS", "300"))
BOOT_TIMEOUT = float(_env("WAKE_BOOT_TIMEOUT", "300"))         # give up waking after this long
RESEND_SECONDS = float(_env("WAKE_RESEND_SECONDS", "15"))      # a lost packet is resent
UNVERIFIED_COOLDOWN = float(_env("WAKE_UNVERIFIED_COOLDOWN", "600"))
BUTTON_COOLDOWN = float(_env("WAKE_BUTTON_COOLDOWN", "120"))
SESSION_SECRET = _env("WAKE_SESSION_SECRET")                   # = gateway.session.secret
SESSION_COOKIE = _env("WAKE_SESSION_COOKIE", "gateway_session")
SESSION_MAX_AGE = 60 * 60 * 24 * 7
MASTER_KEY_SHA256 = (_env("WAKE_MASTER_KEY_SHA256") or "").lower()
DOWN_RECHECK_SECONDS = 10

# app/auth.py: "sk-" + secrets.token_urlsafe(32)
KEY_SHAPE = re.compile(r"^sk-[A-Za-z0-9_-]{43}$")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "trailers", "transfer-encoding", "upgrade"}
STREAMED_PATHS = ("/v1/chat/completions", "/v1/completions")


class State:
    up: bool | None = None         # None until the first request finds out
    checked_at = 0.0
    waking: asyncio.Task | None = None
    wake_reason = ""
    wake_started = 0.0
    verified: set[str] = set()     # key/cookie hashes the gateway has accepted
    rejected: set[str] = set()     # ...and the ones it answered 401 to
    unverified_wakes: dict[str, float] = {}
    last_button = 0.0


state = State()
client: httpx.AsyncClient


@asynccontextmanager
async def lifespan(_):
    global client
    if not UPSTREAM:
        raise SystemExit("WAKE_UPSTREAM is not set (the gateway's LAN address, e.g. http://192.168.50.20:9000)")
    if not WOL_MAC:
        log.warning("WAKE_MAC is not set: requests are passed through, but nothing can be woken")
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=3.0, read=None, write=60.0, pool=None),
                               follow_redirects=False)
    log.info("forwarding to %s, waking %s via %s:%s", UPSTREAM, WOL_MAC or "-", WOL_BROADCAST, WOL_PORT)
    yield
    await client.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# --- waking ------------------------------------------------------------------------------------

def send_magic_packet():
    mac = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", WOL_MAC))
    if len(mac) != 6:
        raise ValueError(f"WAKE_MAC is not a MAC address: {WOL_MAC!r}")
    packet = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (WOL_BROADCAST, WOL_PORT))


async def gateway_is_up():
    try:
        response = await client.get(f"{UPSTREAM}/health", timeout=2.0)
        up = response.status_code == 200
    except httpx.HTTPError:
        up = False
    state.up, state.checked_at = up, time.monotonic()
    return up


async def _wake_loop():
    began = time.monotonic()
    last_sent = 0.0
    while time.monotonic() - began < BOOT_TIMEOUT:
        if await gateway_is_up():
            log.info("gateway is up after %.0f s", time.monotonic() - began)
            return True
        if WOL_MAC and time.monotonic() - last_sent >= RESEND_SECONDS:
            try:
                send_magic_packet()
            except OSError as e:
                log.warning("could not send the magic packet: %s", e)
            last_sent = time.monotonic()
        await asyncio.sleep(2)
    log.warning("gateway did not come up within %.0f s", BOOT_TIMEOUT)
    return False


def start_waking(reason):
    """One wake at a time, however many requests are waiting on it."""
    if state.waking is None or state.waking.done():
        log.info("waking homelabai: %s", reason)
        state.wake_reason, state.wake_started = reason, time.monotonic()
        state.waking = asyncio.create_task(_wake_loop())
    return state.waking


async def wait_until_up(task, timeout):
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        return False


# --- who is asking -----------------------------------------------------------------------------

def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def credential(request):
    """(kind, hash): how the caller identifies itself, and a hash to remember it by."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
    if token:
        h = _hash(token)
        if h in state.rejected:
            return None, h
        if h in state.verified or (MASTER_KEY_SHA256 and h == MASTER_KEY_SHA256):
            return "key", h
        return ("unverified", h) if KEY_SHAPE.match(token) else (None, h)

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        h = _hash(cookie)
        if SESSION_SECRET:
            try:
                URLSafeTimedSerializer(SESSION_SECRET, salt="session").loads(cookie, max_age=SESSION_MAX_AGE)
                return "session", h
            except BadSignature:
                return None, h
        if h in state.verified:
            return "session", h
        return ("unverified", h) if h not in state.rejected else (None, h)

    if request.headers.get("cf-access-jwt-assertion"):
        return "access", None
    return None, None


def may_wake(kind, h):
    if kind in ("key", "session", "access"):
        return True
    if kind == "unverified":
        now = time.monotonic()
        if now - state.unverified_wakes.get(h, -UNVERIFIED_COOLDOWN) >= UNVERIFIED_COOLDOWN:
            state.unverified_wakes[h] = now
            return True
    return False


def is_navigation(request):
    return request.method == "GET" and request.headers.get("sec-fetch-mode") == "navigate"


# GETs the web pages repeat on a timer, whether or not anyone is looking at them: the dashboard's
# stats, the tasks page's project list and event feed, a folder waiting for remote work. Keep in
# step with POLLED in app/activity.py, which leaves the same requests out of the idle check.
POLLED = re.compile(r"^/(admin/system|admin/usage|v1/models|coding/projects|coding/tasks/\d+/events"
                    r"|coding/remote/\d+/next)$")


def is_background_poll(request, path):
    """A browser tab refreshing itself on a timer. Waking for it would mean a tab left open
    somewhere wakes the machine every few minutes, forever."""
    if request.method not in ("GET", "HEAD"):
        return False
    if path.startswith("/coding/remote/"):
        return bool(POLLED.match(path))          # the helper program polls too, without browser headers
    return "sec-fetch-mode" in request.headers and not is_navigation(request) and bool(POLLED.match(path))


def wants_stream(path, body):
    if path not in STREAMED_PATHS or not body:
        return False
    try:
        return json.loads(body).get("stream") is True
    except (ValueError, AttributeError):
        return False


# --- passing requests through ------------------------------------------------------------------

def _upstream_headers(request, plain=False):
    headers = [(k, v) for k, v in request.headers.raw if k.decode("latin-1").lower() not in HOP_BY_HOP]
    if plain:
        # The bytes go out under our own SSE headers, so they must not arrive compressed
        headers = [(k, v) for k, v in headers if k.decode("latin-1").lower() != "accept-encoding"]
    peer = request.client.host if request.client else ""
    forwarded_for = request.headers.get("x-forwarded-for")
    headers = [(k, v) for k, v in headers if k.decode("latin-1").lower() != "x-forwarded-for"]
    headers.append((b"x-forwarded-for", (f"{forwarded_for}, {peer}" if forwarded_for else peer).encode()))
    return headers


def _learn(h, status_code):
    if h is None:
        return
    if status_code == 401:
        state.rejected.add(h)
        state.verified.discard(h)
    elif status_code < 500:
        state.verified.add(h)


async def open_upstream(request, path, body, h, plain=False):
    url = f"{UPSTREAM}{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    upstream = client.build_request(request.method, url, headers=_upstream_headers(request, plain), content=body)
    response = await client.send(upstream, stream=True)
    state.up, state.checked_at = True, time.monotonic()
    _learn(h, response.status_code)
    return response


# --- files kept for while the gateway sleeps -----------------------------------------------------
# The launchers people download and the pages' static files need no model and no data, so a sleeping
# gateway is not woken for them. Each one the gateway serves through here is kept, in memory, and
# served from that copy while it sleeps. The launchers are also fetched now and then while it is up,
# so a download works even when nobody fetched it since the last deploy.
KEPT_PATHS = re.compile(r"^/(downloads/[\w.-]+|static/[\w./-]+)$")
LAUNCHERS = ("ai-gateway.ps1", "ai-gateway.sh", "claude-gateway.ps1", "claude-gateway.sh")
KEPT_HEADERS = ("content-type", "content-disposition", "cache-control", "etag", "last-modified")
KEPT_MAX_FILE = 5 * 1024 * 1024
KEPT_MAX_TOTAL = 50 * 1024 * 1024
REFETCH_SECONDS = float(_env("WAKE_REFETCH_SECONDS", "1800"))
kept: dict[str, tuple[bytes, list]] = {}
# What a request through Cloudflare says about where it came to, for the launchers' fetch: the
# gateway writes its public address into each launcher from these
FORWARDED = ("host", "x-forwarded-proto", "x-forwarded-host", "cf-visitor")
fetching = {"at": -REFETCH_SECONDS, "headers": None}


def keep(path, body, headers):
    if len(body) > KEPT_MAX_FILE or any(k.lower() == b"set-cookie" for k, _ in headers):
        return
    kept.pop(path, None)
    if sum(len(b) for b, _ in kept.values()) + len(body) > KEPT_MAX_TOTAL:
        return
    kept[path] = (body, [(k, v) for k, v in headers if k.decode("latin-1").lower() in KEPT_HEADERS])


async def relay_and_keep(path, response):
    """A file the gateway served, passed on whole and kept."""
    try:
        body = await response.aread()
    finally:
        await response.aclose()
    if response.status_code == 200:
        keep(path, body, response.headers.raw)
    out = Response(body, status_code=response.status_code)
    out.raw_headers = [(k, v) for k, v in response.headers.raw
                       if k.decode("latin-1").lower() not in HOP_BY_HOP | {"content-length"}]
    out.headers["content-length"] = str(len(body))
    return out


def from_kept(path, method):
    body, headers = kept[path]
    out = Response(b"" if method == "HEAD" else body, status_code=200)
    out.raw_headers = list(headers) + [(b"content-length", str(len(body)).encode()),
                                       (b"x-served-by", b"wake-front (gateway asleep)")]
    return out


async def fetch_launchers():
    for name in LAUNCHERS:
        try:
            response = await client.get(f"{UPSTREAM}/downloads/{name}", headers=fetching["headers"], timeout=10.0)
        except httpx.HTTPError:
            return
        if response.status_code == 200:
            keep(f"/downloads/{name}", response.content, response.headers.raw)


def maybe_fetch_launchers(request):
    """Now and then, while the gateway is up, the launchers fetched as a visitor would see them."""
    if request.headers.get("host"):
        fetching["headers"] = {k: request.headers[k] for k in FORWARDED if k in request.headers}
    now = time.monotonic()
    if fetching["headers"] and now - fetching["at"] >= REFETCH_SECONDS:
        fetching["at"] = now
        asyncio.get_running_loop().create_task(fetch_launchers())


def relay(response):
    out = StreamingResponse(response.aiter_raw(), status_code=response.status_code,
                            background=BackgroundTask(response.aclose))
    out.raw_headers = [(k, v) for k, v in response.headers.raw
                       if k.decode("latin-1").lower() not in HOP_BY_HOP]
    return out


def asleep(message, retry_after=60, status=503):
    return JSONResponse({"error": {"message": message, "type": "upstream_asleep", "code": status}},
                        status_code=status, headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"})


async def stream_after_waking(request, path, body, h, task):
    began = time.monotonic()
    while not task.done() and time.monotonic() - began < STREAM_HOLD_SECONDS:
        # An SSE comment: clients ignore it, Cloudflare sees bytes and keeps the request open
        yield b": waking homelabai\n\n"
        await asyncio.wait({task}, timeout=10)
    if not (task.done() and task.result()):
        message = "homelabai did not wake up in time; try again in a minute"
        yield f"data: {json.dumps({'error': {'message': message, 'type': 'upstream_asleep'}})}\n\n".encode()
        return
    try:
        response = await open_upstream(request, path, body, h, plain=True)
    except httpx.HTTPError as e:
        yield f"data: {json.dumps({'error': {'message': f'gateway unreachable after waking: {e}'}})}\n\n".encode()
        return
    try:
        if response.status_code != 200:
            detail = (await response.aread())[:1000].decode("utf-8", "replace")
            error = {"error": {"message": f"gateway answered {response.status_code}: {detail}",
                               "code": response.status_code}}
            yield f"data: {json.dumps(error)}\n\n".encode()
            return
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await response.aclose()


WAKING_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Waking up</title>
<style>
:root{color-scheme:light dark;--bg:#f6f5f2;--fg:#1d1c1a;--muted:#6b6862;--accent:#3d6b4f}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--muted:#9b978f;--accent:#7fb592}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:16px/1.5 system-ui,sans-serif;padding:16px;box-sizing:border-box}
main{max-width:420px;text-align:center}
h1{font-size:1.3rem;font-weight:600;margin:0 0 .5rem}
p{color:var(--muted);margin:.25rem 0}
.dot{display:inline-block;width:.6rem;height:.6rem;border-radius:50%;background:var(--accent);
margin-right:.5rem;animation:p 1.4s ease-in-out infinite}
@keyframes p{50%{opacity:.25}}
button{margin-top:1rem;font:inherit;padding:.5rem 1.1rem;border-radius:6px;border:1px solid var(--accent);
background:transparent;color:var(--fg);cursor:pointer}
</style></head><body><main>
<h1 id="title"><span class="dot"></span>__TITLE__</h1>
<p id="msg">__MESSAGE__</p>
__BUTTON__
<p id="elapsed"></p>
</main><script>
const began = Date.now();
async function check() {
  try {
    const r = await fetch('/__wake/status', {cache: 'no-store'});
    const s = await r.json();
    if (s.up) { location.reload(); return; }
    if (s.waking) {
      document.getElementById('msg').textContent = 'The server is starting. This page reloads by itself.';
      const b = document.getElementById('wake'); if (b) b.remove();
    }
  } catch (e) {}
  document.getElementById('elapsed').textContent = Math.round((Date.now() - began) / 1000) + ' s';
  setTimeout(check, 3000);
}
async function wake() {
  const b = document.getElementById('wake'); b.disabled = true;
  await fetch('/__wake/now', {method: 'POST'}).catch(() => {});
}
check();
</script></body></html>"""


def waking_page(waking):
    if waking:
        title, message, button = "Waking the server", "It takes about a minute. This page reloads by itself.", ""
    else:
        title = "The server is asleep"
        message = "It sleeps when nobody is using it. Waking it takes about a minute."
        button = '<button id="wake" onclick="wake()">Wake it up</button>'
    html = WAKING_PAGE.replace("__TITLE__", title).replace("__MESSAGE__", message).replace("__BUTTON__", button)
    return HTMLResponse(html, status_code=503, headers={"Retry-After": "30", "Cache-Control": "no-store"})


# --- routes ------------------------------------------------------------------------------------

@app.get("/__wake/health")
async def own_health():
    return {"status": "ok"}


@app.get("/__wake/status")
async def wake_status():
    waking = state.waking is not None and not state.waking.done()
    if not waking and time.monotonic() - state.checked_at > 3:
        await gateway_is_up()
    return {"up": bool(state.up), "waking": waking,
            "waking_for_seconds": round(time.monotonic() - state.wake_started) if waking else None}


@app.post("/__wake/now")
async def wake_button(request: Request):
    """The button on the asleep page. Anyone can press it, so it is rate-limited."""
    kind, h = credential(request)
    now = time.monotonic()
    if kind not in ("key", "session", "access") and now - state.last_button < BUTTON_COOLDOWN:
        return JSONResponse({"waking": state.waking is not None and not state.waking.done()}, status_code=429)
    state.last_button = now
    start_waking("wake button")
    return {"waking": True}


@app.api_route("/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def gateway(request: Request, rest: str):
    path = "/" + rest
    body = await request.body()
    kind, h = credential(request)

    waking = state.waking is not None and not state.waking.done()
    if state.up is False and not waking and time.monotonic() - state.checked_at > DOWN_RECHECK_SECONDS:
        await gateway_is_up()      # switched on by hand, or woken some other way
    keepable = request.method == "GET" and bool(KEPT_PATHS.match(path))
    if state.up is not False:
        try:
            response = await open_upstream(request, path, body, h)
            maybe_fetch_launchers(request)
            return await relay_and_keep(path, response) if keepable else relay(response)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
            log.info("gateway unreachable (%s): treating it as asleep", type(e).__name__)
            state.up, state.checked_at = False, time.monotonic()

    # --- the gateway is asleep from here on ---
    if path == "/health":
        return asleep("homelabai is asleep")            # the deploy script's check; wakes nothing
    if request.method in ("GET", "HEAD") and path in kept:
        return from_kept(path, request.method)          # a launcher or static file: no need to wake
    if request.method == "OPTIONS":
        # Let a browser's preflight through, so the real request that follows can wake the machine
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": request.headers.get("origin", "*"),
            "Access-Control-Allow-Methods": "*", "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "60"})

    if is_navigation(request):
        if may_wake(kind, h):
            start_waking(f"page {path} ({kind})")
        return waking_page(state.waking is not None and not state.waking.done())

    if is_background_poll(request, path) or not may_wake(kind, h):
        return asleep("homelabai is asleep; an authenticated request wakes it")

    task = start_waking(f"{request.method} {path} ({kind})")
    if wants_stream(path, body):
        return StreamingResponse(stream_after_waking(request, path, body, h, task),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if not await wait_until_up(task, HOLD_SECONDS):
        return asleep("homelabai is waking up; retry in about 30 seconds", retry_after=30)
    try:
        return relay(await open_upstream(request, path, body, h))
    except httpx.HTTPError as e:
        return asleep(f"gateway unreachable after waking: {e}", retry_after=15, status=502)
