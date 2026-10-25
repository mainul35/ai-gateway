"""app/istory.py against a fake vsd-auth-server token endpoint and a fake istory ingest API.

The token request (client credentials, istory's resource and scope, HTTP Basic), the token being
reused and renewed after a 401, the multipart send, and how istory's answers map to what is
recorded: sent, not linked, imports turned off, retried. No database, no istory, no auth server.

    python -m pytest -q tests/test_istory.py
"""
import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from app import istory


class Fake:
    """Both services on one port: /token and /api/ingest/videos."""

    def __init__(self):
        self.tokens = []            # form fields of each token request
        self.sends = []             # (authorization, body) of each ingest request
        self.answers = []           # (status, json) for the next ingest requests; then 202
        self.issued = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/token":
                    fields = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                    fields["basic"] = base64.b64decode(self.headers["Authorization"].split()[1]).decode()
                    fake.tokens.append(fields)
                    fake.issued += 1
                    self._json(200, {"access_token": f"token-{fake.issued}", "expires_in": 300})
                elif self.path == "/api/ingest/videos":
                    fake.sends.append((self.headers.get("Authorization"), body))
                    status, answer = fake.answers.pop(0) if fake.answers else (
                        202, {"videoId": "abc123", "studioUrl": "https://istory.test/studio/video/abc123"})
                    self._json(status, answer)
                else:
                    self._json(404, {})

            def _json(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "absent.properties"))
    f = Fake()
    monkeypatch.setenv("ISTORY_ENABLED", "true")
    monkeypatch.setenv("ISTORY_URL", f.url)
    monkeypatch.setenv("ISTORY_TOKEN_URL", f.url + "/token")
    monkeypatch.setenv("ISTORY_CLIENT_ID", "ai-gateway-backend")
    monkeypatch.setenv("ISTORY_CLIENT_SECRET", "s3cret")
    istory._token.update(value=None, expires=0.0)
    yield f
    f.server.shutdown()


def send(**overrides):
    args = dict(data=b"\x00\x00\x00\x18ftypmp42", name="Fox (English).mp4", file_id=7,
                sub="ada@example.com", vsd_user_id="42", title="The fox and the moon", language="en")
    args.update(overrides)
    return asyncio.run(istory._post(**args))


def test_sends_the_video_with_a_client_credentials_token_for_istory(fake):
    result = send()
    assert result["videoId"] == "abc123"
    token = fake.tokens[0]
    assert token["grant_type"] == "client_credentials"
    assert token["resource"] == "urn:istory:api"
    assert token["scope"] == "istory:ingest"
    assert token["basic"] == "ai-gateway-backend:s3cret"
    authorization, body = fake.sends[0]
    assert authorization == "Bearer token-1"
    for part in (b'name="subject"', b"ada@example.com", b'name="externalId"', b'name="userId"',
                 b"The fox and the moon", b'filename="Fox (English).mp4"', b"ftypmp42"):
        assert part in body


def test_the_token_is_reused_and_renewed_once_when_refused(fake):
    send()
    send()
    assert len(fake.tokens) == 1
    fake.answers.append((401, {"error": "invalid_token"}))
    send()
    assert len(fake.tokens) == 2
    assert fake.sends[-1][0] == "Bearer token-2"


@pytest.mark.parametrize("status, error", [(404, "user_not_linked"), (403, "auto_import_disabled"),
                                           (415, "unsupported_type")])
def test_answers_that_will_not_change_are_permanent(fake, status, error):
    fake.answers.append((status, {"error": error, "message": "..."}))
    with pytest.raises(istory.Permanent) as raised:
        send()
    assert str(raised.value) == error


def test_server_trouble_is_worth_retrying(fake):
    fake.answers.append((503, {"error": "unavailable"}))
    with pytest.raises(RuntimeError):
        send()


def test_only_accounts_that_sign_in_with_vsd_have_someone_to_send_to():
    local = SimpleNamespace(sso_subject=None, claims=None)
    assert istory.subject_of(local) == (None, None)
    sso = SimpleNamespace(sso_subject="vsdauthserver.example|ada@example.com",
                          claims=json.dumps({"sub": "ada@example.com", "user_id": 42}))
    assert istory.subject_of(sso) == ("ada@example.com", "42")
    no_claims = SimpleNamespace(sso_subject="vsdauthserver.example|ada@example.com", claims=None)
    assert istory.subject_of(no_claims) == ("ada@example.com", None)


def test_nothing_is_sent_while_switched_off(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "absent.properties"))
    monkeypatch.delenv("ISTORY_ENABLED", raising=False)
    started = []
    monkeypatch.setattr(istory.asyncio, "create_task", lambda coro: started.append(coro))
    istory.send_soon(1, 2, "title")
    assert started == []
