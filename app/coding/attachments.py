"""Pictures and documents given to a coding task with its question: a screenshot of the bug, a log,
a spec. They are the playground's own uploads (ChatFile), so the same box, paste and drop work.

A task's conversation is saved after every step, so a picture is kept in it as a reference to its
file and only turned into the picture itself when the model is asked. A document is only text by the
time a model sees it, so it goes into the question as text.
"""
import base64
import json

from sqlalchemy import select

from app import backends
from app.db import session_factory
from app.models import ChatFile
from app.tools import documents

MOST_FILES = 6
REF = "chatfile:"             # a picture in a saved conversation: chatfile:<id>
UNSEEN = "[A picture was attached here, but {model} cannot see pictures.]"


def _picture(f):
    return f.kind != "document" and (f.mime_type or "").startswith("image/")


async def _files(session, user_id, ids):
    rows = (await session.execute(select(ChatFile).where(ChatFile.id.in_(ids),
                                                         ChatFile.user_id == user_id))).scalars().all()
    return {f.id: f for f in rows}


async def check(session, user_id, ids, model):
    """The file ids, each one this person's own; ValueError says why not. Pictures need a model
    that can see, or the task would start with half its question missing."""
    wanted = []
    for i in ids or []:
        if isinstance(i, bool) or not isinstance(i, int):
            raise ValueError("Attachments are given by their file id")
        if i not in wanted:
            wanted.append(i)
    if len(wanted) > MOST_FILES:
        raise ValueError(f"A task can be given at most {MOST_FILES} files")
    if not wanted:
        return []
    found = await _files(session, user_id, wanted)
    if len(found) < len(wanted):
        raise ValueError("One of the attached files is gone; attach it again")
    if any(not _picture(f) and f.kind != "document" for f in found.values()):
        raise ValueError("Only pictures and documents can be given to a task")
    if any(_picture(f) for f in found.values()):
        backend = await backends.resolve(model)
        if backend is None or "vision" not in backend.capabilities:
            raise ValueError(f"{model} cannot see pictures. Pick a model that can, or leave the pictures out.")
    return wanted


async def question(text, ids, user_id):
    """The task's first message: its words, with the documents' text after them and the pictures
    beside them as references."""
    if not ids:
        return text
    async with session_factory()() as session:
        found = await _files(session, user_id, ids)
    papers = [found[i] for i in ids if i in found and found[i].kind == "document"]
    pictures = [found[i] for i in ids if i in found and _picture(found[i])]
    if papers:
        text += "\n\n" + documents.context_block([(f.name or "document", f.prompt or "") for f in papers])
    if not pictures:
        return text
    return [{"type": "text", "text": text}] + [
        {"type": "image_url", "image_url": {"url": f"{REF}{f.id}"}} for f in pictures]


def _refs(messages):
    return {int(part["image_url"]["url"][len(REF):])
            for m in messages if isinstance(m.get("content"), list)
            for part in m["content"]
            if part.get("type") == "image_url" and str(part["image_url"].get("url", "")).startswith(REF)}


async def for_model(messages, model, user_id):
    """The conversation as the model is sent it: each picture reference replaced by the picture, or
    by a line saying it is there when the model (a task continued on another one) cannot see."""
    refs = _refs(messages)
    if not refs:
        return messages
    backend = await backends.resolve(model)
    sees = backend is not None and "vision" in backend.capabilities
    found = {}
    if sees:
        async with session_factory()() as session:
            found = await _files(session, user_id, refs)
    out = []
    for m in messages:
        if not isinstance(m.get("content"), list):
            out.append(m)
            continue
        parts = []
        for part in m["content"]:
            url = str(part.get("image_url", {}).get("url", "")) if part.get("type") == "image_url" else ""
            if not url.startswith(REF):
                parts.append(part)
                continue
            f = found.get(int(url[len(REF):]))
            if f is None:
                parts.append({"type": "text", "text": UNSEEN.format(model=model) if not sees
                              else "[A picture was attached here, but it has since been deleted.]"})
            else:
                parts.append({"type": "image_url", "image_url": {
                    "url": f"data:{f.mime_type};base64,{base64.b64encode(f.data).decode()}"}})
        if not sees:          # text only: a model that cannot see may not take parts either
            parts = "\n\n".join(p["text"] for p in parts if p.get("type") == "text")
        out.append({**m, "content": parts})
    return out


async def described(session, ids):
    """What the task page shows of a task's files: name, and whether it is a picture to show."""
    if not ids:
        return []
    rows = (await session.execute(select(ChatFile.id, ChatFile.name, ChatFile.kind, ChatFile.mime_type)
                                  .where(ChatFile.id.in_(ids)))).all()
    found = {r.id: r for r in rows}
    return [{"id": i, "name": found[i].name or "file", "url": f"/chat/files/{i}",
             "picture": found[i].kind != "document" and (found[i].mime_type or "").startswith("image/")}
            for i in ids if i in found]


def listed(task):
    return json.loads(task.files) if getattr(task, "files", None) else []
