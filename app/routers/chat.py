"""The playground: saved conversations, images, and the tools a chat can switch on.

Every user only ever sees their own conversations and files.
"""
import asyncio
import base64
import io
import json
import logging
import re
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import access, backends, settings, usage as usage_log
from app.auth import Principal, authenticate
from app.db import get_session, session_factory
from app.models import ChatFile, Conversation, ConversationMessage, MemoryEntry, utcnow
from app.routers.openai_v1 import PROXY_PATHS, forward
from app.tools import (chooser, documents, image_prompt, images, jobs, map_plan, maps, markdown,
                        memory as memory_tool, photo, portrait, router as intent_router, web_search)

log = logging.getLogger("playground")

router = APIRouter(prefix="/chat", tags=["chat"])

TITLE_LENGTH = 80
LIST_LIMIT = 200


class ConversationIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=256)
    system_prompt: str | None = None


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    model: str | None = Field(default=None, max_length=256)
    system_prompt: str | None = None


class MessageIn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = ""
    reasoning: str | None = None
    model: str | None = Field(default=None, max_length=256)
    stats: dict | None = None
    # {"images": [{"file_id"}], "sources": [{"title", "url"}], "generated": {"file_id", "prompt"}}
    attachments: dict | None = None


def _require_user(principal):
    if principal.user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conversations belong to a user; the master key has none")
    return principal.user


async def _owned(session, conversation_id, user, with_messages=False):
    query = select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id)
    if with_messages:
        query = query.options(selectinload(Conversation.messages))
    conversation = (await session.execute(query)).scalar_one_or_none()
    if conversation is None:
        # Same answer whether it does not exist or belongs to someone else
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


def _conversation_json(conversation, message_count=None):
    data = {
        "id": conversation.id, "title": conversation.title, "model": conversation.model,
        "system_prompt": conversation.system_prompt,
        "created_at": conversation.created_at.isoformat(), "updated_at": conversation.updated_at.isoformat(),
    }
    if message_count is not None:
        data["message_count"] = message_count
    return data


def _message_json(message):
    return {
        "id": message.id, "role": message.role, "content": message.content, "reasoning": message.reasoning,
        # Answers are Markdown; the playground shows the rendered form and keeps the text for copying
        "html": markdown.render(message.content) if message.role == "assistant" else None,
        "model": message.model, "stats": json.loads(message.stats) if message.stats else None,
        "attachments": json.loads(message.attachments) if message.attachments else None,
        "created_at": message.created_at.isoformat(),
    }


@router.get("/conversations")
async def list_conversations(principal: Principal = Depends(authenticate),
                             session: AsyncSession = Depends(get_session)):
    user = _require_user(principal)
    counts = (select(ConversationMessage.conversation_id, func.count(ConversationMessage.id).label("n"))
              .group_by(ConversationMessage.conversation_id).subquery())
    rows = await session.execute(
        select(Conversation, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.conversation_id == Conversation.id)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(LIST_LIMIT)
    )
    return [_conversation_json(conversation, count) for conversation, count in rows.all()]


@router.post("/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(payload: ConversationIn, principal: Principal = Depends(authenticate),
                              session: AsyncSession = Depends(get_session)):
    user = _require_user(principal)
    conversation = Conversation(user_id=user.id, title=(payload.title or "New conversation")[:TITLE_LENGTH],
                                model=payload.model, system_prompt=payload.system_prompt)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return _conversation_json(conversation, 0)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: int, principal: Principal = Depends(authenticate),
                           session: AsyncSession = Depends(get_session)):
    conversation = await _owned(session, conversation_id, _require_user(principal), with_messages=True)
    return {**_conversation_json(conversation, len(conversation.messages)),
            "messages": [_message_json(m) for m in conversation.messages]}


@router.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: int, payload: ConversationPatch,
                              principal: Principal = Depends(authenticate),
                              session: AsyncSession = Depends(get_session)):
    conversation = await _owned(session, conversation_id, _require_user(principal))
    if payload.title is not None:
        conversation.title = payload.title.strip()[:TITLE_LENGTH] or conversation.title
    if payload.model is not None:
        conversation.model = payload.model
    if payload.system_prompt is not None:
        conversation.system_prompt = payload.system_prompt or None
    await session.commit()
    await session.refresh(conversation)
    return _conversation_json(conversation)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int, principal: Principal = Depends(authenticate),
                              session: AsyncSession = Depends(get_session)):
    conversation = await _owned(session, conversation_id, _require_user(principal))
    await session.delete(conversation)
    await session.commit()
    return {"deleted": conversation_id}


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def add_message(conversation_id: int, payload: MessageIn, principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    conversation = await _owned(session, conversation_id, _require_user(principal))
    message = ConversationMessage(conversation_id=conversation.id, role=payload.role, content=payload.content,
                                  reasoning=payload.reasoning or None, model=payload.model,
                                  stats=json.dumps(payload.stats) if payload.stats else None,
                                  attachments=json.dumps(payload.attachments) if payload.attachments else None)
    session.add(message)
    conversation.updated_at = utcnow()
    if payload.model:
        conversation.model = payload.model
    await session.commit()
    await session.refresh(message)
    if payload.role == "assistant" and memory_tool.is_enabled():
        # After the reply, so the wait is never the user's
        asyncio.create_task(_refresh_memory(principal, conversation.user_id, conversation.id))
    return _message_json(message)


# --- tools ------------------------------------------------------------------------

@router.get("/capabilities")
async def capabilities(principal: Principal = Depends(authenticate)):
    """Which tools this server offers; the playground only shows toggles for these."""
    return {
        "web_search": web_search.is_available(),
        "vision": settings.feature_enabled("vision"),
        "image_generation": images.is_available() and access.can_generate_images(principal),
        "image_sizes": list(images.SIZES),
        # "qwen" follows edit instructions and takes up to 3 images; "flux" re-renders one image
        "edit_engine": await images.edit_engine() if images.is_available() else None,
        "max_edit_images": images.MAX_EDIT_IMAGES,
        "photo_clean": images.is_available(),
        "photo_blur": images.is_available() and photo.is_depth_available(),
        "photo_backdrop": images.is_available() and portrait.is_available(),
        "maps": settings.feature_enabled("maps"),
        "routing": intent_router.is_enabled(),
        "markdown": markdown.is_available(),
        "max_upload_mb": settings.max_upload_bytes() // (1024 * 1024),
    }


# Checked against the bytes, not the type the browser claims
_IMAGE_SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                     (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"BM", "image/bmp"))
# What phones and cameras produce besides JPEG. A browser cannot show most of them, so they are
# converted on the way in rather than refused.
_CONVERTED = {"image/heic", "image/avif", "image/tiff", "image/bmp"}
# A raw file is a TIFF underneath, so the bytes alone do not tell one from an ordinary TIFF
_RAW_SUFFIXES = (".orf", ".rw2", ".arw", ".cr2", ".cr3", ".nef", ".raf", ".dng", ".pef", ".srw")


def _image_type(data):
    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"avif", b"avis"):
            return "image/avif"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return None


def _file_json(file):
    data = {"id": file.id, "url": f"/chat/files/{file.id}", "mime_type": file.mime_type,
            "name": file.name, "kind": file.kind}
    # A document keeps its text in prompt, which is megabytes of no use to the browser
    data["prompt"] = None if file.kind == "document" else file.prompt
    return data


def _upright(data, mime):
    """Applies the picture's own rotation, if it has one. Anything unexpected is left as it was."""
    try:
        from PIL import Image, ImageOps
        picture = Image.open(io.BytesIO(data))
        if picture.getexif().get(274, 1) in (1, None):   # 274 is Orientation
            return data, mime
        turned = ImageOps.exif_transpose(picture)
        out = io.BytesIO()
        if mime == "image/jpeg":
            turned.convert("RGB").save(out, format="JPEG", quality=95)
        else:
            turned.save(out, format="PNG")
            mime = "image/png"
        return out.getvalue(), mime
    except Exception as e:
        log.info("Could not read the orientation of an upload: %s", e)
        return data, mime


def _to_browser_format(data, mime, name):
    """Turns what a browser cannot display into JPEG, leaving the picture itself alone."""
    try:
        from PIL import Image, ImageOps
        if mime == "image/heic":
            import pillow_heif
            pillow_heif.register_heif_opener()
    except ImportError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{name} is a {mime.split('/')[1].upper()} file and this server cannot "
                            f"read it ({e.name} is not installed)")
    try:
        picture = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        out = io.BytesIO()
        picture.save(out, format="JPEG", quality=92)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{name} could not be read ({e.__class__.__name__})")
    return out.getvalue(), "image/jpeg"


@router.post("/files", status_code=status.HTTP_201_CREATED)
async def upload_file(file: UploadFile = File(...), principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    user = _require_user(principal)
    limit = settings.max_upload_bytes()
    data = await file.read(limit + 1)
    name = (file.filename or "image")[:256]
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"{name} is larger than {limit // (1024 * 1024)} MB")
    mime = _image_type(data)
    if name.lower().endswith(_RAW_SUFFIXES):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{name} is a camera raw file, which this server cannot develop yet. "
                            "Export it as JPEG or HEIC and attach that.")
    if mime is None:
        # Not a picture: it may still be something a model can be told the contents of
        try:
            kind = documents.kind_of(name, data)
        except documents.DocumentError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        if kind is not None:
            # Read now rather than at send time, so a file that cannot be read says so while the
            # person is still looking at the upload rather than halfway through an answer
            try:
                text = await asyncio.to_thread(documents.extract, name, data)
            except documents.DocumentError as e:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
            stored = ChatFile(user_id=user.id, kind="document", mime_type=documents.MIME[kind],
                              name=name, prompt=text, data=data)
            session.add(stored)
            await session.commit()
            await session.refresh(stored)
            return {**_file_json(stored), "characters": len(text)}
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{name} is neither a picture nor a document this server can read. "
                            "Pictures: JPEG, PNG, HEIC, WebP, TIFF, GIF, BMP. Documents: PDF, "
                            "Word (.docx), Excel (.xlsx), and plain text.")
    if mime in _CONVERTED:
        data, mime = await asyncio.to_thread(_to_browser_format, data, mime, name)
    else:
        # A photograph taken upright carries its rotation in EXIF, which a browser applies and
        # everything else ignores. Baked in here, so what is worked on is what was seen.
        data, mime = await asyncio.to_thread(_upright, data, mime)
    stored = ChatFile(user_id=user.id, kind="upload", mime_type=mime, name=name, data=data)
    session.add(stored)
    await session.commit()
    await session.refresh(stored)
    return _file_json(stored)


@router.get("/files/{file_id}")
async def get_file(file_id: int, principal: Principal = Depends(authenticate),
                   session: AsyncSession = Depends(get_session)):
    user = _require_user(principal)
    stored = (await session.execute(
        select(ChatFile).where(ChatFile.id == file_id, ChatFile.user_id == user.id))).scalar_one_or_none()
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    return Response(stored.data, media_type=stored.mime_type,
                    headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


async def _load_files(session, user, file_ids):
    if not file_ids:
        return {}
    rows = await session.execute(select(ChatFile).where(ChatFile.id.in_(file_ids), ChatFile.user_id == user.id))
    return {f.id: f for f in rows.scalars()}


def _event(kind, **data):
    """A playground event on the model's SSE channel; OpenAI chunks never carry a "gateway" key."""
    return f"data: {json.dumps({'gateway': {'type': kind, **data}})}\n\n".encode()


def _error(message):
    return f"data: {json.dumps({'error': {'message': message}})}\n\n".encode()


class _Answer:
    """The model's reply, reassembled from the chunks as they pass through on their way out.

    The gateway used to hand the chunks straight to the browser and let the browser save the finished
    message. That works exactly as long as the browser is still there; walk away mid-answer and the
    whole turn is lost, having been generated and paid for. So it is read here as well.
    """

    def __init__(self):
        self.text = ""
        self.reasoning = ""
        self.tokens = None
        self.sources = None
        self.started = time.monotonic()
        self.first_token_at = None
        self._partial = ""

    def feed(self, chunk):
        # A chunk is a piece of the stream, not a whole line: the last one is kept back until it ends
        self._partial += chunk.decode(errors="replace") if isinstance(chunk, bytes) else chunk
        lines = self._partial.split("\n")
        self._partial = lines.pop()
        for line in lines:
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body or body == "[DONE]":
                continue
            try:
                data = json.loads(body)
            except ValueError:
                continue
            if isinstance(data.get("usage"), dict):
                self.tokens = data["usage"].get("completion_tokens") or self.tokens
            delta = (data.get("choices") or [{}])[0].get("delta") or {}
            # llama.cpp calls it reasoning_content and Ollama calls it reasoning; the same thing
            thought = delta.get("reasoning_content") or delta.get("reasoning")
            if thought:
                self.first_token_at = self.first_token_at or time.monotonic()
                self.reasoning += thought
            if delta.get("content"):
                self.first_token_at = self.first_token_at or time.monotonic()
                self.text += delta["content"]

    def stats(self):
        now = time.monotonic()
        total = now - self.started
        writing = (now - self.first_token_at) if self.first_token_at else total
        return {"ttft": round(self.first_token_at - self.started, 1) if self.first_token_at else None,
                "tokens": self.tokens,
                "tps": round(self.tokens / writing, 1) if self.tokens and writing > 0 else None,
                "total": round(total, 1)}


class ChatMessageIn(BaseModel):
    role: str = Field(pattern="^(system|user|assistant)$")
    content: str = ""
    images: list[int] = Field(default_factory=list)


class CompleteIn(BaseModel):
    model: str = Field(max_length=256)
    conversation_id: int | None = None
    messages: list[ChatMessageIn]
    temperature: float | None = None
    max_tokens: int | None = None
    thinking: bool | None = None
    web_search: bool = False
    vision: bool = False


async def _ask_helper(principal, body):
    """One non-streamed call for the gateway's own helpers; returns the text, or None if it failed."""
    try:
        response = await forward(principal, "chat_completions", PROXY_PATHS["chat_completions"], body,
                                 skip_access=True)
        return json.loads(response.body)["choices"][0]["message"].get("content")
    except (HTTPException, ValueError, KeyError, IndexError, TypeError) as e:
        log.info("Helper call to %s failed: %s", body.get("model"), e)
        return None


async def _search_query(principal, payload, backend):
    """Turns the conversation into a search query; falls back to the user's own words."""
    latest = next((m.content for m in reversed(payload.messages) if m.role == "user"), "")
    recent = [m for m in payload.messages if m.role != "system"][-6:]
    transcript = "\n".join(f"{m.role}: {m.content[:1500]}" for m in recent)
    # The small router model writes queries too, so the big model is not loaded just for one line
    model = settings.router_model() or payload.model
    body = {"model": model, "stream": False, "temperature": 0, "max_tokens": 400,
            "messages": [{"role": "system", "content": web_search.QUERY_PROMPT},
                         {"role": "user", "content": f"Conversation:\n{transcript}\n\nSearch query:"}]}
    # A one-line query needs no reasoning; skipping it saves most of the wait
    if model == payload.model and backend.kind == "llamacpp":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    else:
        body["reasoning_effort"] = "none"
    return web_search.clean_query(await _ask_helper(principal, body), latest)


@router.post("/complete")
async def complete(payload: CompleteIn, principal: Principal = Depends(authenticate),
                   session: AsyncSession = Depends(get_session)):
    """One playground turn: optional web search and images, then the model's streamed answer.

    Streams OpenAI chunks, plus playground events ({"gateway": {...}}) for progress and sources.
    """
    user = _require_user(principal)
    if not access.can_use_model(principal, payload.model):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"You do not have access to model '{payload.model}'")
    backend = await backends.resolve(payload.model)
    if backend is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Model '{payload.model}' is not available")

    if payload.conversation_id is not None:
        await _owned(session, payload.conversation_id, user)
    use_vision = payload.vision and settings.feature_enabled("vision")
    latest_user = next((m for m in reversed(payload.messages) if m.role == "user"), None)
    # Pictures from earlier turns are dropped for a text-only model rather than failing the chat
    use_vision = use_vision and "vision" in backend.capabilities
    # Every attachment is loaded, then sorted: pictures go to a model that can see, documents go to
    # any model at all, because by the time they get there they are only text
    attached = [i for m in payload.messages for i in m.images]
    everything = await _load_files(session, user, attached)
    papers = [f for f in everything.values() if f.kind == "document"]
    pictures = [f for f in everything.values() if f.kind != "document"]
    if pictures and payload.vision and "vision" not in backend.capabilities:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{payload.model} cannot see images. Pick a model marked as "
                            "vision-capable, or attach a document instead.")
    files = {i: f for i, f in everything.items() if f.kind != "document"} if use_vision else {}

    history = list(payload.messages)
    remembered = None
    if memory_tool.is_enabled():
        notes = [n.content for n in await _user_notes(session, user)]
        summary = await _conversation_memory(session, user.id, payload.conversation_id)
        remembered = memory_tool.context_block(notes, summary.content if summary else None)
        if summary and summary.covered_count:
            # Those turns are in the summary, so they are not sent again
            system_prompts = [m for m in history if m.role == "system"]
            rest = [m for m in history if m.role != "system"]
            history = system_prompts + rest[summary.covered_count:]

    messages = []
    for m in history:
        attached = [files[i] for i in m.images if i in files] if m.role == "user" else []
        if attached:
            parts = [{"type": "text", "text": m.content or "Describe this image."}]
            parts += [{"type": "image_url", "image_url": {
                "url": f"data:{f.mime_type};base64,{base64.b64encode(f.data).decode()}"}} for f in attached]
            messages.append({"role": m.role, "content": parts})
        else:
            messages.append({"role": m.role, "content": m.content})

    if papers:
        block = documents.context_block([(f.name or "document", f.prompt or "") for f in papers])
        messages.insert(1 if messages and messages[0]["role"] == "system" else 0,
                        {"role": "system", "content": block})
    if remembered:
        # After the user's own system prompt, so theirs still comes first
        messages.insert(1 if messages and messages[0]["role"] == "system" else 0,
                        {"role": "system", "content": remembered})

    body = {"model": payload.model, "messages": messages, "stream": True}
    if payload.temperature is not None:
        body["temperature"] = payload.temperature
    if payload.max_tokens:
        body["max_tokens"] = payload.max_tokens
    if payload.thinking is not None and backend.kind == "llamacpp":
        body["chat_template_kwargs"] = {"enable_thinking": payload.thinking}
    do_search = payload.web_search and web_search.is_available()

    answer = _Answer()

    async def stream():
        if do_search:
            yield _event("status", text="Working out what to search for")
            latest = next((m.content for m in reversed(payload.messages) if m.role == "user"), "")
            query = await _search_query(principal, payload, backend)
            yield _event("status", text=f"Searching the web for “{query}”")
            try:
                # The user's own words are searched too: a written query can come out wrong, and
                # two phrasings agreeing on a page is a good sign in itself
                results = await web_search.search([query, latest],
                                                  wants_recent=web_search.wants_recent(query, latest))
                if not results:
                    yield _event("notice", text="The web search found nothing; answering without it.")
            except web_search.SearchError as e:
                results = []
                yield _event("notice", text=f"Web search failed ({e}); answering without it.")
            if results:
                yield _event("status", text=f"Reading the top {min(len(results), settings.search_fetch_pages())} pages")
                results = await web_search.fetch_pages(results)
                answer.sources = [{"title": r["title"], "url": r["url"]} for r in results]
                yield _event("sources", query=query, sources=answer.sources)
                # After the user's own system prompt, if there is one, so theirs still comes first
                position = 1 if messages and messages[0]["role"] == "system" else 0
                messages.insert(position, {"role": "system", "content": web_search.sources_prompt(results, query)})
            yield _event("status", text="Writing the answer")
        try:
            response = await forward(principal, "chat_completions", PROXY_PATHS["chat_completions"], body)
        except HTTPException as e:
            yield _error(str(e.detail))
            return
        async for chunk in response.body_iterator:
            answer.feed(chunk)
            yield chunk

    async def save(_job):
        """Writes the turn down once the answer is finished, whoever is still watching."""
        if payload.conversation_id is None or not (answer.text or answer.reasoning):
            return
        async with session_factory()() as db:
            conversation = await db.get(Conversation, payload.conversation_id)
            if conversation is None or conversation.user_id != user.id:
                return
            db.add(ConversationMessage(
                conversation_id=payload.conversation_id, role="assistant", content=answer.text,
                reasoning=answer.reasoning or None, model=payload.model,
                stats=json.dumps(answer.stats()),
                attachments=json.dumps({"sources": answer.sources}) if answer.sources else None))
            conversation.updated_at = utcnow()
            conversation.model = payload.model
            await db.commit()
        if memory_tool.is_enabled():
            await _refresh_memory(principal, user.id, payload.conversation_id)

    if payload.conversation_id is None:
        # Nothing to come back to, so there is nothing to keep
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    asked = (latest_user.content if latest_user else "") or "New message"
    job = jobs.start(user.id, payload.conversation_id, "chat", asked[:120], stream(), on_finish=save)
    return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/conversations/{conversation_id}/job")
async def running_job(conversation_id: int, principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    """What this conversation is still busy with, so reopening it can pick the thread back up."""
    user = _require_user(principal)
    await _owned(session, conversation_id, user)
    job = jobs.running_for(user.id, conversation_id)
    return {"job": job.json() if job else None}


@router.get("/jobs/{job_id}/stream")
async def watch_job(job_id: int, principal: Principal = Depends(authenticate)):
    """Everything the job has produced so far, then the rest of it as it arrives."""
    job = jobs.get(_require_user(principal).id, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That job has been forgotten")
    return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- memory ---------------------------------------------------------------------

def _memory_json(entry):
    return {"id": entry.id, "scope": entry.scope, "conversation_id": entry.conversation_id,
            "content": entry.content, "source": entry.source,
            "updated_at": entry.updated_at.isoformat()}


async def _user_notes(session, user):
    rows = await session.execute(
        select(MemoryEntry).where(MemoryEntry.user_id == user.id, MemoryEntry.scope == "user")
        .order_by(MemoryEntry.updated_at.desc()).limit(settings.max_user_notes()))
    return list(rows.scalars())


async def _conversation_memory(session, user_id, conversation_id):
    if not conversation_id:
        return None
    rows = await session.execute(
        select(MemoryEntry).where(MemoryEntry.user_id == user_id, MemoryEntry.scope == "conversation",
                                  MemoryEntry.conversation_id == conversation_id).order_by(MemoryEntry.id))
    return rows.scalars().first()


@router.get("/memory")
async def list_memory(conversation_id: int | None = None, principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    """What the playground remembers about this user, and about this conversation."""
    user = _require_user(principal)
    summary = await _conversation_memory(session, user.id, conversation_id)
    return {"enabled": memory_tool.is_enabled(),
            "user": [_memory_json(n) for n in await _user_notes(session, user)],
            "conversation": _memory_json(summary) if summary else None}


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=memory_tool.SUMMARY_LENGTH)
    scope: str = Field(default="user", pattern="^(user|conversation)$")
    conversation_id: int | None = None


@router.post("/memory", status_code=status.HTTP_201_CREATED)
async def add_memory(payload: MemoryIn, principal: Principal = Depends(authenticate),
                     session: AsyncSession = Depends(get_session)):
    user = _require_user(principal)
    if payload.scope == "conversation":
        if payload.conversation_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A conversation note needs a conversation")
        await _owned(session, payload.conversation_id, user)
        existing = await _conversation_memory(session, user.id, payload.conversation_id)
        if existing is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This conversation already has notes; edit them")
    entry = MemoryEntry(user_id=user.id, scope=payload.scope, conversation_id=payload.conversation_id,
                        content=payload.content.strip(), source="manual")
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return _memory_json(entry)


class MemoryPatch(BaseModel):
    content: str = Field(min_length=1, max_length=memory_tool.SUMMARY_LENGTH)


async def _owned_memory(session, user, entry_id):
    entry = (await session.execute(select(MemoryEntry).where(
        MemoryEntry.id == entry_id, MemoryEntry.user_id == user.id))).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return entry


@router.patch("/memory/{entry_id}")
async def edit_memory(entry_id: int, payload: MemoryPatch, principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    """An edited entry is kept as the user wrote it and is never overwritten automatically."""
    entry = await _owned_memory(session, _require_user(principal), entry_id)
    entry.content = payload.content.strip()
    entry.source = "manual"
    entry.updated_at = utcnow()
    await session.commit()
    await session.refresh(entry)
    return _memory_json(entry)


@router.delete("/memory/{entry_id}")
async def delete_memory(entry_id: int, principal: Principal = Depends(authenticate),
                        session: AsyncSession = Depends(get_session)):
    entry = await _owned_memory(session, _require_user(principal), entry_id)
    await session.delete(entry)
    await session.commit()
    return {"deleted": entry_id}


_refreshing = set()


async def _refresh_memory(principal, user_id, conversation_id):
    """Rewrites the conversation summary, then folds what it learned into the user's notes.

    Runs after a turn, in its own session, and never disturbs the chat: failures are only logged.
    """
    if conversation_id in _refreshing:
        return  # a refresh is already running; the next turn picks up whatever it misses
    _refreshing.add(conversation_id)
    try:
        async with session_factory()() as session:
            conversation = (await session.execute(
                select(Conversation).options(selectinload(Conversation.messages))
                .where(Conversation.id == conversation_id, Conversation.user_id == user_id))).scalar_one_or_none()
            if conversation is None:
                return
            keep = settings.keep_recent_messages()
            older = conversation.messages[:-keep] if keep else list(conversation.messages)
            if not older:
                return
            model = memory_tool.model_for(conversation.model)
            if not model:
                return
            entry = await _conversation_memory(session, user_id, conversation_id)
            if entry is not None and entry.source == "manual":
                return  # the user wrote these notes themselves
            if entry is not None and len(older) - entry.covered_count < settings.summarize_every():
                return

            previous = entry.content if entry else None
            # The whole conversation is summarised, but only the older turns count as covered: the
            # recent ones are still sent in full, and the notes are the better for including them
            summary = memory_tool.clean_text(await _ask_helper(
                principal, memory_tool.summary_request(previous, memory_tool.transcript(conversation.messages),
                                                       model)))
            if not summary:
                return
            if entry is None:
                entry = MemoryEntry(user_id=user_id, scope="conversation", conversation_id=conversation_id)
                session.add(entry)
            entry.content = summary
            entry.covered_count = len(older)
            entry.source = "auto"
            entry.updated_at = utcnow()
            await session.commit()

            notes = [n for n in (await session.execute(select(MemoryEntry).where(
                MemoryEntry.user_id == user_id, MemoryEntry.scope == "user"))).scalars()]
            kept = [n for n in notes if n.source == "manual"]  # the user's own notes stay untouched
            automatic = [n for n in notes if n.source == "auto"]
            updated = memory_tool.parse_notes(await _ask_helper(
                principal, memory_tool.notes_request([n.content for n in notes], summary, model)))
            updated = [u for u in updated if u not in [k.content for k in kept]]
            for note, content in zip(automatic, updated):
                note.content, note.updated_at = content, utcnow()
            for content in updated[len(automatic):]:
                session.add(MemoryEntry(user_id=user_id, scope="user", content=content, source="auto"))
            for note in automatic[len(updated):]:
                await session.delete(note)   # dropped by the model, usually because it was superseded
            await session.commit()
    except Exception as e:  # memory is a convenience; never let it break a conversation
        log.warning("Could not refresh memory for conversation %s: %s", conversation_id, e)
    finally:
        _refreshing.discard(conversation_id)


class RenderIn(BaseModel):
    text: str = Field(default="", max_length=200_000)


@router.post("/render")
async def render_markdown(payload: RenderIn, _: Principal = Depends(authenticate)):
    """Renders a just-streamed answer. Streaming shows text; the finished answer is Markdown."""
    return {"html": markdown.render(payload.text)}


# What the words ask for, when a photograph is attached
WANTS_UPSCALE = re.compile(r"\b(crop|zoom|enlarge|upscale|bigger|larger|print|resolution)\b", re.I)
WANTS_SHARPEN = re.compile(r"\b(sharp\w*|soft|softness|blurry|blurred|unsharp|crisp\w*|clarity|"
                           r"definition|detail)\b", re.I)
WANTS_DEHAZE = re.compile(r"\b(haz\w*|mist\w*|foggy|fog|smog|milky|washed out|flat|dull|"
                          r"low contrast|contrast)\b", re.I)
WANTS_DENOISE = re.compile(r"\b(noise|noisy|grain|grainy|denoise|speckl\w*|iso|clean)\b", re.I)


async def _save_result(db, user_id, conversation_id, png, name, prompt, extra=None, stats=None):
    """Stores the picture and, when it belongs to a conversation, the message that shows it.

    Written by the server rather than the browser so that a dropped connection cannot lose it: the
    work is done and paid for, and reopening the conversation has to show it.
    """
    stored = ChatFile(user_id=user_id, kind="generated", mime_type="image/png",
                      name=name, prompt=prompt, data=png)
    db.add(stored)
    await db.commit()
    await db.refresh(stored)
    if conversation_id is not None:
        generated = {"file_id": stored.id, "prompt": prompt, **(extra or {})}
        conversation = await db.get(Conversation, conversation_id)
        if conversation is not None and conversation.user_id == user_id:
            db.add(ConversationMessage(conversation_id=conversation_id, role="assistant",
                                       content=f"({prompt.lower()})",
                                       stats=json.dumps(stats) if stats else None,
                                       attachments=json.dumps({"generated": generated})))
            conversation.updated_at = utcnow()
            await db.commit()
    return stored


class PhotoIn(BaseModel):
    file_id: int
    action: str = Field(pattern="^(clean|blur|backdrop)$")
    request: str = ""                                       # what was asked, in the user's words
    upscale: bool = True                                    # clean: enlarge for cropping
    strength: float = Field(default=1.0, ge=0.2, le=3.0)    # blur: how much
    colour: str = ""                                        # backdrop: a name or #rrggbb, else the words
    # blur: 0 the nearest thing, 1 the furthest; unset means wherever the photo is already sharp
    focus: float | None = Field(default=None, ge=0.0, le=1.0)
    conversation_id: int | None = None


@router.post("/photo")
async def edit_photo(payload: PhotoIn, principal: Principal = Depends(authenticate),
                     session: AsyncSession = Depends(get_session)):
    """Cleans up a photograph, or blurs its background. One or the other, never both at once."""
    user = _require_user(principal)
    if not images.is_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Image work is turned off on this server")
    if not access.can_generate_images(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to image work")
    if payload.action == "blur" and not await asyncio.to_thread(photo.is_depth_available):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Background blur needs the depth model; it is not installed")
    if payload.action == "backdrop" and not await asyncio.to_thread(portrait.is_available):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Changing the background colour needs the cut-out model; it is not installed")
    if payload.conversation_id is not None:
        await _owned(session, payload.conversation_id, user)
    found = (await _load_files(session, user, [payload.file_id])).get(payload.file_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo not found")
    source, mime, user_id = found.data, found.mime_type, user.id

    asked = (payload.request or "").strip()
    if asked:
        # The words decide, since they are what the person actually asked for
        wants = {
            "upscale": payload.upscale and bool(WANTS_UPSCALE.search(asked)),
            "sharpen": bool(WANTS_SHARPEN.search(asked)),
            "dehaze": bool(WANTS_DEHAZE.search(asked)),
            # Noise removal is the default, unless the request is plainly about something else
            "denoise": bool(WANTS_DENOISE.search(asked)) or not (WANTS_SHARPEN.search(asked)
                                                                 or WANTS_DEHAZE.search(asked)),
        }
    else:
        wants = {"upscale": payload.upscale, "sharpen": False, "dehaze": False, "denoise": True}
    # A colour picked in the interface wins; otherwise it is whatever the sentence named, then white
    wanted = portrait.colour_from(payload.colour or asked)

    async def stream():
        events = asyncio.Queue()

        async def progress(stage, fraction=None):
            await events.put(_event("progress", stage=stage, fraction=fraction))

        started = time.monotonic()
        if payload.action == "clean":
            async def clean():
                png = await images.clean_up(source, mime, wants["upscale"], progress,
                                            denoise=wants["denoise"], sharpen=wants["sharpen"])
                if wants["dehaze"]:
                    await progress("Clearing the haze")
                    png = await photo.dehaze_in_background(png, 1.0)
                return png
            job = asyncio.create_task(clean())
        elif payload.action == "backdrop":
            async def backdrop():
                await progress(f"Putting you on a {portrait.colour_name(wanted)} background")
                return await portrait.replace_background_in_background(source, wanted)
            job = asyncio.create_task(backdrop())
        else:
            async def blur():
                await progress("Working out what is near and what is far")
                return await photo.blur_in_background(source, payload.strength, payload.focus)
            job = asyncio.create_task(blur())
        try:
            while not job.done() or not events.empty():
                getter = asyncio.create_task(events.get())
                done, _ = await asyncio.wait({getter, job}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                else:
                    getter.cancel()
            png = job.result()
        except (images.ImageError, photo.PhotoError, portrait.PortraitError) as e:
            await usage_log.record(principal, settings.image_model_name(), "comfyui",
                                   f"photo_{payload.action}", True, 502, None,
                                   (time.monotonic() - started) * 1000, str(e))
            yield _error(str(e))
            return
        finally:
            if not job.done():
                job.cancel()
        await usage_log.record(principal, settings.image_model_name(), "comfyui",
                               f"photo_{payload.action}", True, 200, None,
                               (time.monotonic() - started) * 1000)
        async with session_factory()() as db:
            if payload.action == "blur":
                done = "background blurred"
            elif payload.action == "backdrop":
                done = f"background changed to {portrait.colour_name(wanted)}"
            else:
                did = [name for name, flag in (("noise removed", wants["denoise"]),
                                               ("haze cleared", wants["dehaze"]),
                                               ("sharpened", wants["sharpen"]),
                                               ("enlarged 2x", wants["upscale"])) if flag]
                done = ", ".join(did) or "cleaned up"
            seconds = round(time.monotonic() - started, 1)
            stored = await _save_result(db, user_id, payload.conversation_id, png,
                                        f"photo-{payload.action}.png", done,
                                        {"source_file_id": payload.file_id}, {"total": seconds})
            yield _event("image", file=_file_json(stored), action=payload.action,
                         did=done[:1].upper() + done[1:], saved=True, seconds=seconds)

    if payload.conversation_id is not None:
        asked = (payload.request or payload.action).strip()[:120]
        job = jobs.start(user.id, payload.conversation_id, "photo", asked, stream())
        return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


CHOOSER_DAYS = 30      # how far back the chooser looks at a model's record


async def _choose_model(principal, session, category, has_images, prefer=None):
    """The best model this user may use for this kind of work, with the reason it was picked."""
    available = await backends.available_models()
    health = await usage_log.by_model(session, utcnow() - timedelta(days=CHOOSER_DAYS))
    candidates = []
    for name, backend in available.items():
        if not access.can_use_model(principal, name):
            continue
        details = backend.details or {}
        candidates.append({
            "name": name,
            "capabilities": sorted(backend.capabilities),
            "parameter_size": details.get("parameter_size", ""),
            "context_length": backend.context_length,
            "health": health.get(name, {}),
        })
    return chooser.pick(candidates, category, needs_vision=has_images, prefer=prefer)




# --- maps -----------------------------------------------------------------------------------

class MapIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: int | None = None
    # Where the browser says the person is, when they allowed it to say
    here_lat: float | None = Field(default=None, ge=-90, le=90)
    here_lon: float | None = Field(default=None, ge=-180, le=180)


async def _candidates(words, fallback_points, index):
    """Everywhere a name might mean, or the one place a coordinate already is."""
    if index < len(fallback_points):
        lat, lon = fallback_points[index]
        try:
            return [await maps.reverse(lat, lon)]
        except maps.MapError:
            return [{"name": f"{lat:.5f}, {lon:.5f}", "address": "", "lat": lat, "lon": lon,
                     "kind": "", "osm": None}]
    if not words:
        return []
    found = await maps.search(words, limit=5)
    if not found:
        raise maps.MapError(f"Nowhere called \u201c{words}\u201d could be found on the map.")
    return found


async def _somewhere(words, fallback_points, index):
    found = await _candidates(words, fallback_points, index)
    return found[0] if found else None


@router.post("/map")
async def find_on_map(payload: MapIn, principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    """Answers a question about places with real coordinates, a route, and what is along it.

    The reading of the question is the model's; every coordinate in the answer comes from
    OpenStreetMap. That division is the point: a model asked for a latitude will produce one that
    looks entirely plausible and is wrong, and nobody would notice until they drove there.
    """
    _require_user(principal)
    if not settings.feature_enabled("maps"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Maps are turned off on this server")

    message = payload.message.strip()
    points = maps.parse_points(message)
    # A coordinate the browser supplied is used only where the question left a gap, never in place
    # of somewhere the person actually named
    standing = ((payload.here_lat, payload.here_lon)
                if payload.here_lat is not None and payload.here_lon is not None else None)
    plan = None
    if intent_router.is_enabled():
        answer = await _ask_helper(principal, map_plan.build_request(message, settings.router_model()))
        plan = map_plan.read_answer(answer)
    if plan is None:
        plan = map_plan.by_keywords(message, bool(points))
    # A coordinate in the message settles what it is about, whatever the model made of the words
    if points and plan["intent"] == "find":
        plan["intent"] = "near" if maps.amenity_for(plan["what"] or message) else "here"

    about_me = map_plan.wants_my_location(message, plan)
    if about_me and standing and not points:
        # "near me" and "from here" mean the browser's coordinate, so it takes the place of the
        # name the question never gave
        points = [standing]
        if plan["intent"] == "find":
            plan["intent"] = "near" if maps.amenity_for(plan["what"] or message) else "here"
        if plan["intent"] == "near" and not plan["to"]:
            plan["to"] = ""
        if plan["intent"] in ("route", "along") and not plan["from"]:
            plan["from"] = ""
    elif about_me and not standing and not points:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED,
                            "This needs to know where you are. Allow this page to use your "
                            "location, or name a place instead.")

    tag = maps.amenity_for(plan["what"] or message)
    mode = maps.transport_for(message)
    result = {"intent": plan["intent"], "asked": message, "what": plan["what"],
              "places": [], "route": None, "start": None, "end": None, "mode": mode,
              "from_your_location": bool(about_me and standing)}

    try:
        if plan["intent"] == "here":
            if not points:
                raise maps.MapError("No coordinate was given to look up.")
            result["places"] = [await maps.reverse(*points[0])]

        elif plan["intent"] in ("route", "along"):
            # Both ends are resolved together, so that two vague names settle on the pairing
            # that makes a journey rather than a continent
            start, end = maps.nearest_pair(await _candidates(plan["from"], points, 0),
                                           await _candidates(plan["to"], points, 1))
            if not start or not end:
                raise maps.MapError("A route needs both a start and a finish; name them as "
                                    "\u201cfrom A to B\u201d.")
            result["start"], result["end"] = start, end
            result["route"] = await maps.route([(start["lat"], start["lon"]),
                                                (end["lat"], end["lon"])], mode)
            if plan["intent"] == "along":
                if not tag:
                    raise maps.MapError("What should be looked for along the way? Name a kind of "
                                        "place, such as petrol stations or restaurants.")
                result["places"] = await maps.along(tag, result["route"]["line"])

        elif plan["intent"] == "near":
            centre = await _somewhere(plan["to"] or plan["what"], points, 0)
            if not centre:
                raise maps.MapError("Near where? Name a place, or give a coordinate.")
            result["start"] = centre
            if not tag:
                # Not a kind of place this knows a tag for, so it is searched for by its words
                result["places"] = await maps.search(plan["what"] or message,
                                                     near=(centre["lat"], centre["lon"]))
                for place in result["places"]:
                    place["metres_away"] = round(maps.distance(centre["lat"], centre["lon"],
                                                               place["lat"], place["lon"]))
                result["places"].sort(key=lambda p: p["metres_away"])
            else:
                result["places"] = await maps.nearby(tag, centre["lat"], centre["lon"],
                                                     words=plan["what"] or message)

        else:                                   # find
            result["places"] = await maps.search(plan["what"] or message,
                                                 near=(points[0] if points else None))
            if not result["places"]:
                raise maps.MapError("Nothing on the map matched that.")
    except maps.MapError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))

    for place in result["places"]:
        place["google"] = maps.google_link(place=place)
    if result["route"]:
        result["google_route"] = maps.google_link(
            start=(result["start"]["lat"], result["start"]["lon"]),
            end=(result["end"]["lat"], result["end"]["lon"]), mode=mode)
    everything = result["places"] + [p for p in (result["start"], result["end"]) if p]
    result["bounds"] = maps.bounds(everything, (result["route"] or {}).get("line", []))
    await usage_log.record(principal, "openstreetmap", "maps", "map", False, 200, None, 0)
    return result


class RouteIn(BaseModel):
    message: str = ""
    has_images: bool = False
    web_search: bool = False
    image_generation: bool = False
    maps: bool = False
    history: list[ChatMessageIn] = Field(default_factory=list)
    # The model the conversation is already using: it keeps the job when nothing scores better,
    # because swapping model reloads weights and throws away a warm cache
    current_model: str = ""
    choose_model: bool = False


@router.post("/route")
async def route(payload: RouteIn, principal: Principal = Depends(authenticate),
                session: AsyncSession = Depends(get_session)):
    """What this message asks for: which tool, what kind of work, and which model should do it.

    The two questions go to the small model at the same time. They are independent, so asking them
    together costs one round trip rather than two, and keeping them in separate prompts keeps the
    action answer as good as it was - putting both in one prompt made it worse.
    """
    tools = {
        "web_search": payload.web_search and web_search.is_available(),
        "image_generation": (payload.image_generation and images.is_available()
                             and access.can_generate_images(principal)),
        "maps": payload.maps and settings.feature_enabled("maps"),
    }
    allowed = intent_router.candidates(tools, payload.has_images)
    history = [{"role": m.role, "content": m.content} for m in payload.history]
    asking_model = intent_router.is_enabled()

    async def decide_action():
        if len(allowed) == 1:
            return allowed[0], "the only tool switched on"
        if not asking_model:
            return intent_router.by_keywords(payload.message, allowed, payload.has_images), "keywords"
        answer = await _ask_helper(principal,
                                   intent_router.build_request(payload.message, allowed, history))
        found = intent_router.clean_answer(answer, allowed)
        settled, by = intent_router.settle_action(found, payload.message, allowed, payload.has_images)
        if settled:
            return settled, settings.router_model() if by == "model" else "keywords"
        return intent_router.by_keywords(payload.message, allowed, payload.has_images), "keywords"

    async def decide_category():
        if not payload.choose_model:
            return None, None
        if not asking_model:
            return intent_router.category_by_keywords(payload.message, payload.has_images), "keywords"
        answer = await _ask_helper(principal,
                                   intent_router.build_category_request(payload.message, history))
        found = intent_router.clean_category(answer, payload.has_images)
        category, by = intent_router.settle_category(found, payload.message, payload.has_images)
        return category, settings.router_model() if by == "model" else "keywords"

    (action, decided_by), (category, category_by) = await asyncio.gather(decide_action(),
                                                                        decide_category())
    decision = {"action": action, "decided_by": decided_by}
    if category is None:
        return decision
    decision.update(category=category, category_by=category_by)
    # Only a text answer is the model's to choose: a picture is drawn by the image model, and a
    # photograph is worked on by tools that have nothing to do with the drop-down
    if action in (intent_router.CHAT, intent_router.SEARCH):
        picked = await _choose_model(principal, session, category, payload.has_images,
                                     prefer=payload.current_model or None)
        if picked:
            decision.update(model=picked["model"], model_why=picked["why"],
                            considered=picked["considered"])
    return decision


async def _drawable_prompt(principal, session, user, request, conversation_id):
    """What to draw, the wording it must spell, and the rewrite to show, if there was one."""
    if not image_prompt.is_enabled():
        return request, None, None
    history, made = [], []
    if conversation_id is not None:
        conversation = (await session.execute(
            select(Conversation).options(selectinload(Conversation.messages))
            .where(Conversation.id == conversation_id,
                   Conversation.user_id == user.id))).scalar_one_or_none()
        for message in (conversation.messages if conversation else [])[-12:]:
            history.append({"role": message.role, "content": message.content or ""})
            generated = (json.loads(message.attachments) if message.attachments else {}).get("generated")
            if generated and generated.get("prompt"):
                made.append(generated["prompt"])
    # Both questions at once: what to draw, and which words have to be spelled right in it
    described, worded = await asyncio.gather(
        _ask_helper(principal, image_prompt.build_request(request, history, made)),
        _ask_helper(principal, image_prompt.wording_request(request, history)))
    drawn, quoted = image_prompt.clean(described, request)
    wording = image_prompt.clean_wording(worded) or quoted
    drawn = image_prompt.with_wording(drawn, wording)
    return drawn, wording, (drawn if drawn != request else None)


class ImageIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None
    size: str = "square"
    source_file_id: int | None = None      # the picture to edit
    reference_file_ids: list[int] = Field(default_factory=list, max_length=2)  # extra pictures it may use
    strength: float = Field(default=0.75, ge=0.05, le=1.0)


@router.post("/images")
async def generate_image(payload: ImageIn, principal: Principal = Depends(authenticate),
                         session: AsyncSession = Depends(get_session)):
    """Generates (or, with a source image, edits) a picture. Streams progress, then the saved file."""
    user = _require_user(principal)
    if not images.is_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Image generation is turned off on this server")
    if not access.can_generate_images(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to image generation")
    if payload.conversation_id is not None:
        await _owned(session, payload.conversation_id, user)
    sources = []
    if payload.source_file_id is not None:
        wanted = [payload.source_file_id, *payload.reference_file_ids]
        found = await _load_files(session, user, wanted)
        if any(i not in found for i in wanted):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Source image not found")
        sources = [(found[i].data, found[i].mime_type) for i in wanted]
    elif payload.reference_file_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Reference images need an image to edit")
    user_id = user.id
    # An edit is already an instruction about a picture that exists, so it is passed through as written
    drawable, wording, rewritten = ((payload.prompt, None, None) if sources else
                                    await _drawable_prompt(principal, session, user, payload.prompt,
                                                           payload.conversation_id))

    async def stream():
        events = asyncio.Queue()
        if rewritten:
            yield _event("prompt", text=rewritten)

        async def progress(stage, fraction):
            await events.put(_event("progress", stage=stage, fraction=fraction))

        started = time.monotonic()
        endpoint = "image_edits" if sources else "image_generations"
        job = asyncio.create_task(images.generate(drawable, payload.size, sources, payload.strength,
                                                  progress, text=wording))
        try:
            # Relay progress while the job runs
            while not job.done() or not events.empty():
                getter = asyncio.create_task(events.get())
                done, _ = await asyncio.wait({getter, job}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                else:
                    getter.cancel()
            png, seed = job.result()
        except images.ImageError as e:
            await usage_log.record(principal, settings.image_model_name(), "comfyui", endpoint, True, 502,
                                   None, (time.monotonic() - started) * 1000, str(e))
            yield _error(str(e))
            return
        finally:
            if not job.done():
                job.cancel()  # the browser went away; generate() stops the job on the image server
        await usage_log.record(principal, settings.image_model_name(), "comfyui", endpoint, True, 200,
                               None, (time.monotonic() - started) * 1000)
        async with session_factory()() as db:
            seconds = round(time.monotonic() - started, 1)
            stored = await _save_result(db, user_id, payload.conversation_id, png,
                                        f"image-{seed}.png", drawable,
                                        {"seed": seed, "size": payload.size,
                                         "asked": payload.prompt,
                                         "source_file_id": payload.source_file_id},
                                        {"total": seconds})
            yield _event("image", file=_file_json(stored), seed=seed, saved=True, seconds=seconds)

    if payload.conversation_id is not None:
        job = jobs.start(user.id, payload.conversation_id, "image", (payload.prompt or "")[:120], stream())
        return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
