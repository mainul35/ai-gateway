"""End-to-end tests for the wake front, against a fake homelabai that is asleep until it receives a
real Wake-on-LAN packet.

    pip install -r wake/requirements.txt pytest && pytest wake/
"""
import json
import os
import socket
import subprocess
import sys
import textwrap
import time

import httpx
import pytest
from itsdangerous import URLSafeTimedSerializer

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET = "test-session-secret"
GOOD_KEY = "sk-" + "A" * 43
OTHER_KEY = "sk-" + "B" * 43
MAC = "a8:a1:59:9d:f4:ae"

FAKE_BOX = textwrap.dedent('''
    import asyncio, json, os, socket, sys, time
    import uvicorn
    from fastapi import FastAPI, Header, Request
    from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

    wol_port, http_port, good = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    app = FastAPI()

    @app.get("/health")
    async def health(): return {"status": "ok"}

    @app.get("/chat/conversations")
    async def conversations(): return {"conversations": []}

    @app.get("/admin/system")
    async def system(): return {"ok": True}

    @app.get("/downloads/{name}")
    async def download(name: str, host: str = Header(default="")):
        return PlainTextResponse(f"# launcher {name} for {host}\\n",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/v1/models")
    async def models(authorization: str = Header(default=""), anthropic_version: str = Header(default="")):
        if authorization != f"Bearer {good}":
            return JSONResponse({"error": "Invalid API key"}, status_code=401)
        return {"data": [{"id": "claude-sonnet-5" if anthropic_version else "qwen3.6-256k"}]}

    @app.get("/auth/me")
    async def me(): return JSONResponse({"detail": "Not signed in"}, status_code=401)

    @app.get("/static/app.css")
    async def css(): return PlainTextResponse("body{}", media_type="text/css")

    @app.post("/__sleep")
    async def sleep():
        asyncio.get_running_loop().call_later(0.2, lambda: os._exit(0))
        return {"sleeping": True}

    @app.post("/v1/chat/completions")
    async def chat(request: Request, authorization: str = Header(default="")):
        if authorization != f"Bearer {good}":
            return JSONResponse({"error": "Invalid API key"}, status_code=401)
        if (await request.json()).get("stream"):
            async def gen():
                for word in ["hello", " world"]:
                    yield f"data: {json.dumps({'choices': [{'delta': {'content': word}}]})}\\n\\n"
                yield "data: [DONE]\\n\\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        return {"choices": [{"message": {"content": "hello world"}}]}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", wol_port))
    data, _ = sock.recvfrom(1024)
    assert data[:6] == b"\\xff" * 6 and len(data) == 102
    print("woken", flush=True)
    time.sleep(2)   # "resuming"
    uvicorn.run(app, host="127.0.0.1", port=http_port, log_level="warning")
''')


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def setup(tmp_path):
    wol_port, box_port, front_port = free_port(socket.SOCK_DGRAM), free_port(), free_port()
    script = tmp_path / "fake_box.py"
    script.write_text(FAKE_BOX)
    box_log = tmp_path / "box.log"
    box = subprocess.Popen([sys.executable, str(script), str(wol_port), str(box_port), GOOD_KEY],
                           stdout=open(box_log, "w"), stderr=subprocess.STDOUT)
    env = dict(os.environ, WAKE_UPSTREAM=f"http://127.0.0.1:{box_port}", WAKE_MAC=MAC,
               WAKE_BROADCAST="127.0.0.1", WAKE_WOL_PORT=str(wol_port), WAKE_SESSION_SECRET=SECRET,
               WAKE_HOLD_SECONDS="20", WAKE_RESEND_SECONDS="100")
    front = subprocess.Popen([sys.executable, "-m", "uvicorn", "wake_front:app", "--port", str(front_port),
                              "--log-level", "warning"], cwd=HERE, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{front_port}"
    for _ in range(50):
        try:
            httpx.get(f"{base}/__wake/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    yield base, lambda: "woken" in box_log.read_text()
    front.terminate()
    box.terminate()
    front.wait()
    box.wait()


def cookie():
    return URLSafeTimedSerializer(SECRET, salt="session").dumps({"user_id": 1})


def test_unauthenticated_and_polls_do_not_wake(setup):
    base, woken = setup
    assert httpx.get(f"{base}/").status_code == 503                                   # scanner
    assert httpx.get(f"{base}/health").status_code == 503                             # monitors
    assert httpx.post(f"{base}/v1/chat/completions", json={},
                      headers={"Authorization": "Bearer nope"}).status_code == 503    # not a key
    assert httpx.get(f"{base}/admin/system", cookies={"gateway_session": cookie()},
                     headers={"Sec-Fetch-Mode": "cors"}).status_code == 503           # dashboard timer
    assert httpx.get(f"{base}/coding/remote/3/next",
                     headers={"Authorization": f"Bearer {GOOD_KEY}"}).status_code == 503  # long-poll
    page = httpx.get(f"{base}/", headers={"Sec-Fetch-Mode": "navigate"})
    assert page.status_code == 503 and "Wake it up" in page.text                     # button, no wake
    forged = httpx.get(f"{base}/", headers={"Sec-Fetch-Mode": "navigate"},
                       cookies={"gateway_session": "forged.cookie.sig"})
    assert "Wake it up" in forged.text
    time.sleep(0.5)
    assert not woken()


def test_streamed_completion_wakes_then_answers(setup):
    base, woken = setup
    with httpx.stream("POST", f"{base}/v1/chat/completions", json={"stream": True, "model": "x"},
                      headers={"Authorization": f"Bearer {GOOD_KEY}"}, timeout=60) as response:
        assert response.status_code == 200
        body = response.read().decode()
    assert body.startswith(": waking homelabai")
    assert '"hello"' in body and "[DONE]" in body
    assert woken()


def test_click_in_open_tab_wakes_then_answers(setup):
    base, woken = setup
    response = httpx.get(f"{base}/chat/conversations", cookies={"gateway_session": cookie()},
                         headers={"Sec-Fetch-Mode": "cors"}, timeout=60)
    assert response.status_code == 200 and response.json() == {"conversations": []}
    assert woken()


def test_downloads_and_static_files_need_no_wake(setup):
    base, woken = setup
    assert httpx.get(f"{base}/downloads/ai-gateway.ps1").status_code == 503     # never served yet
    # Awake: a call wakes it, a static file and one launcher pass through and are kept, and the
    # other launchers are fetched alongside, as a visitor to the public address would get them
    public = {"Host": "gateway.example"}
    assert httpx.get(f"{base}/chat/conversations", cookies={"gateway_session": cookie()},
                     headers={"Sec-Fetch-Mode": "cors", **public}, timeout=60).status_code == 200
    assert httpx.get(f"{base}/static/app.css", headers=public).text == "body{}"
    assert httpx.get(f"{base}/downloads/ai-gateway.ps1", headers=public).status_code == 200
    time.sleep(1)
    httpx.post(f"{base}/__sleep")
    time.sleep(1)
    # Asleep: served from the copies, nothing woken (an unknown file still is not)
    ps1 = httpx.get(f"{base}/downloads/ai-gateway.ps1")
    assert ps1.status_code == 200 and ps1.text == "# launcher ai-gateway.ps1 for gateway.example\n"
    assert ps1.headers["x-served-by"].startswith("wake-front")
    assert 'filename="ai-gateway.ps1"' in ps1.headers["content-disposition"]
    sh = httpx.get(f"{base}/downloads/ai-gateway.sh")
    assert sh.status_code == 200 and "gateway.example" in sh.text               # fetched, never asked for
    css = httpx.get(f"{base}/static/app.css")
    assert css.text == "body{}" and css.headers["content-type"].startswith("text/css")
    assert httpx.head(f"{base}/downloads/ai-gateway.sh").status_code == 200
    assert httpx.get(f"{base}/static/other.js").status_code == 503


def test_desktop_model_check_is_answered_while_asleep(setup):
    base, woken = setup
    desktop = {"Authorization": f"Bearer {GOOD_KEY}", "anthropic-version": "2023-06-01", "Sec-Fetch-Mode": "cors"}
    # The app's first check, with no list kept yet: wakes the machine once and gets the list
    first = httpx.get(f"{base}/v1/models", headers=desktop, timeout=60)
    assert first.status_code == 200 and first.json()["data"][0]["id"] == "claude-sonnet-5"
    assert woken()
    httpx.post(f"{base}/__sleep")
    time.sleep(1)
    # Asleep: the same caller gets its own list back without a wake; a dashboard tab's timer does not
    again = httpx.get(f"{base}/v1/models", headers=desktop)
    assert again.status_code == 200 and again.json() == first.json()
    assert again.headers["x-served-by"].startswith("wake-front")
    assert httpx.get(f"{base}/v1/models", cookies={"gateway_session": cookie()},
                     headers={"Sec-Fetch-Mode": "cors"}).status_code == 503


def test_shared_page_link_wakes_once(setup):
    base, woken = setup
    nav = {"Sec-Fetch-Mode": "navigate"}
    # An address that is not a share link wakes nothing; neither does a script fetching one
    assert "Wake it up" in httpx.get(f"{base}/p/short", headers=nav).text
    assert httpx.get(f"{base}/p/" + "a" * 24).status_code == 503
    time.sleep(0.5)
    assert not woken()
    # A person opening a share link gets the waking page, and the machine wakes
    page = httpx.get(f"{base}/p/" + "a" * 24, headers=nav)
    assert page.status_code == 503 and "Wake it up" not in page.text
    for _ in range(40):
        if woken():
            break
        time.sleep(0.5)
    assert woken()


def test_rejected_key_never_wakes_again(setup):
    base, woken = setup
    # Wake with a good key, then let the gateway reject another key once
    assert httpx.post(f"{base}/v1/chat/completions", json={"model": "x"}, timeout=60,
                      headers={"Authorization": f"Bearer {GOOD_KEY}"}).status_code == 200
    assert httpx.post(f"{base}/v1/chat/completions", json={},
                      headers={"Authorization": f"Bearer {OTHER_KEY}"}).status_code == 401
    status = httpx.get(f"{base}/__wake/status").json()
    assert status["up"] is True


def test_login_only_401_does_not_reject_a_good_key(setup):
    base, woken = setup
    key = {"Authorization": f"Bearer {GOOD_KEY}"}
    assert httpx.post(f"{base}/v1/chat/completions", json={"model": "x"}, timeout=60, headers=key).status_code == 200
    # /auth/me wants a login cookie: a 401 there is about the cookie, not the key
    assert httpx.get(f"{base}/auth/me", headers=key).status_code == 401
    httpx.post(f"{base}/__sleep", headers=key)
    time.sleep(1)
    # Asleep now: the key must still be one that may wake it - held while waking, not turned away
    answer = httpx.get(f"{base}/chat/conversations", headers=key, timeout=60).json()
    assert "authenticated request wakes it" not in json.dumps(answer)


RESTARTING_BOX = textwrap.dedent('''
    import sys
    import uvicorn
    from fastapi import FastAPI
    app = FastAPI()
    @app.get("/health")
    async def health(): return {"status": "ok"}
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
''')


def test_gateway_restart_is_not_ten_seconds_asleep(tmp_path):
    """A deploy restarts the gateway in a few seconds. The wake front sees the refused connection
    and takes the machine for asleep; it has to notice it is back within a couple of seconds, not
    after DOWN_RECHECK_SECONDS - nothing that does not wake (/health, a dashboard's polls) would
    otherwise look again, and the deploy's own public check was answered 503 six times out of six."""
    box_port, front_port = free_port(), free_port()
    script = tmp_path / "box.py"
    script.write_text(RESTARTING_BOX)

    def start_box():
        process = subprocess.Popen([sys.executable, str(script), str(box_port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                httpx.get(f"http://127.0.0.1:{box_port}/health", timeout=1)
                return process
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError("the box did not start")

    box = start_box()
    env = dict(os.environ, WAKE_UPSTREAM=f"http://127.0.0.1:{box_port}", WAKE_MAC=MAC,
               WAKE_BROADCAST="127.0.0.1", WAKE_WOL_PORT=str(free_port(socket.SOCK_DGRAM)),
               WAKE_SESSION_SECRET=SECRET)
    front = subprocess.Popen([sys.executable, "-m", "uvicorn", "wake_front:app", "--port", str(front_port),
                              "--log-level", "warning"], cwd=HERE, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{front_port}"
    try:
        for _ in range(50):
            try:
                httpx.get(f"{base}/__wake/health", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert httpx.get(f"{base}/health").status_code == 200          # up
        box.terminate(); box.wait()
        assert httpx.get(f"{base}/health").status_code == 503          # restarting: taken for asleep
        box = start_box()
        time.sleep(2.5)
        assert httpx.get(f"{base}/health").status_code == 200          # back, seen within seconds
    finally:
        front.terminate(); front.wait()
        box.terminate(); box.wait()
