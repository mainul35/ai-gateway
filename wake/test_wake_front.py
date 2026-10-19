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


def test_rejected_key_never_wakes_again(setup):
    base, woken = setup
    # Wake with a good key, then let the gateway reject another key once
    assert httpx.post(f"{base}/v1/chat/completions", json={"model": "x"}, timeout=60,
                      headers={"Authorization": f"Bearer {GOOD_KEY}"}).status_code == 200
    assert httpx.post(f"{base}/v1/chat/completions", json={},
                      headers={"Authorization": f"Bearer {OTHER_KEY}"}).status_code == 401
    status = httpx.get(f"{base}/__wake/status").json()
    assert status["up"] is True
