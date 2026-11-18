"""app/coding/attachments.py: screenshots and documents given to a coding task. No database: the
lookups are replaced.

    python -m pytest -q tests/test_task_attachments.py
"""
import asyncio
import base64
import contextlib
from types import SimpleNamespace

import pytest

from app.coding import attachments

FILES = {
    1: SimpleNamespace(id=1, kind="upload", mime_type="image/png", name="shot.png", data=b"PNG", prompt=None),
    2: SimpleNamespace(id=2, kind="document", mime_type="application/pdf", name="spec.pdf", data=b"",
                       prompt="The spec says hello."),
    3: SimpleNamespace(id=3, kind="voice", mime_type="audio/mpeg", name="v.mp3", data=b"", prompt=None),
}


@pytest.fixture
def world(monkeypatch):
    seeing = {"eyes"}

    async def files(session, user_id, ids):
        return {i: FILES[i] for i in ids if i in FILES}

    async def resolve(model):
        return SimpleNamespace(capabilities={"tools", "vision"} if model in seeing else {"tools"})

    @contextlib.asynccontextmanager
    async def session():
        yield None
    monkeypatch.setattr(attachments, "_files", files)
    monkeypatch.setattr(attachments.backends, "resolve", resolve)
    monkeypatch.setattr(attachments, "session_factory", lambda: session)


def test_check(world):
    run = asyncio.run
    assert run(attachments.check(None, 7, [1, 2, 1], "eyes")) == [1, 2]
    assert run(attachments.check(None, 7, [2], "blind")) == [2]       # a document needs no eyes
    assert run(attachments.check(None, 7, [], "blind")) == []
    with pytest.raises(ValueError, match="cannot see pictures"):
        run(attachments.check(None, 7, [1], "blind"))
    with pytest.raises(ValueError, match="gone"):
        run(attachments.check(None, 7, [1, 99], "eyes"))
    with pytest.raises(ValueError, match="Only pictures and documents"):
        run(attachments.check(None, 7, [3], "eyes"))
    with pytest.raises(ValueError, match="at most"):
        run(attachments.check(None, 7, list(range(1, attachments.MOST_FILES + 2)), "eyes"))


def test_question_keeps_pictures_as_references_and_documents_as_text(world):
    assert asyncio.run(attachments.question("Fix it", [], 7)) == "Fix it"
    content = asyncio.run(attachments.question("Fix it", [1, 2], 7))
    assert content[0]["type"] == "text" and "The spec says hello." in content[0]["text"]
    assert content[1] == {"type": "image_url", "image_url": {"url": "chatfile:1"}}


def test_for_model_sends_the_picture_to_a_model_that_can_see(world):
    messages = [{"role": "system", "content": "s"},
                {"role": "user", "content": [{"type": "text", "text": "Fix it"},
                                             {"type": "image_url", "image_url": {"url": "chatfile:1"}}]}]
    sent = asyncio.run(attachments.for_model(messages, "eyes", 7))
    assert sent[1]["content"][1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(b"PNG").decode()
    assert messages[1]["content"][1]["image_url"]["url"] == "chatfile:1"       # the saved one is untouched
    blind = asyncio.run(attachments.for_model(messages, "blind", 7))
    assert blind[1]["content"] == "Fix it\n\n" + attachments.UNSEEN.format(model="blind")
    plain = [{"role": "user", "content": "hi"}]
    assert asyncio.run(attachments.for_model(plain, "blind", 7)) is plain
