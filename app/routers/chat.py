"""The playground: saved conversations, images, and the tools a chat can switch on.

Every user only ever sees their own conversations and files.
"""
import asyncio
import base64
import json
import logging
import time

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
from app.tools import (image_prompt, images, markdown, memory as memory_tool, router as intent_router,
                        web_search)

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
        "routing": intent_router.is_enabled(),
        "markdown": markdown.is_available(),
        "max_upload_mb": settings.max_upload_bytes() // (1024 * 1024),
    }


# Checked against the bytes, not the type the browser claims
_IMAGE_SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                     (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def _image_type(data):
    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _file_json(file):
    return {"id": file.id, "url": f"/chat/files/{file.id}", "mime_type": file.mime_type, "name": file.name,
            "kind": file.kind, "prompt": file.prompt}


@router.post("/files", status_code=status.HTTP_201_CREATED)
async def upload_file(file: UploadFile = File(...), principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    user = _require_user(principal)
    limit = settings.max_upload_bytes()
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"Images can be at most {limit // (1024 * 1024)} MB")
    mime = _image_type(data)
    if mime is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only PNG, JPEG, WebP and GIF images can be attached")
    stored = ChatFile(user_id=user.id, kind="upload", mime_type=mime, name=(file.filename or "image")[:256], data=data)
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
    if use_vision and latest_user and latest_user.images and "vision" not in backend.capabilities:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{payload.model} cannot read images. Pick a model marked as vision-capable.")
    # Pictures from earlier turns are dropped for a text-only model rather than failing the chat
    use_vision = use_vision and "vision" in backend.capabilities
    wanted = [i for m in payload.messages if m.role == "user" for i in m.images] if use_vision else []
    files = await _load_files(session, user, wanted)

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
                yield _event("sources", query=query,
                             sources=[{"title": r["title"], "url": r["url"]} for r in results])
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
            yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream",
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


class RouteIn(BaseModel):
    message: str = ""
    has_images: bool = False
    web_search: bool = False
    image_generation: bool = False
    history: list[ChatMessageIn] = Field(default_factory=list)


@router.post("/route")
async def route(payload: RouteIn, principal: Principal = Depends(authenticate)):
    """Picks one action for this message out of the tools the user has switched on."""
    tools = {
        "web_search": payload.web_search and web_search.is_available(),
        "image_generation": (payload.image_generation and images.is_available()
                             and access.can_generate_images(principal)),
    }
    allowed = intent_router.candidates(tools, payload.has_images)
    if len(allowed) == 1:
        return {"action": allowed[0], "decided_by": "the only tool switched on"}
    if not intent_router.is_enabled():
        return {"action": intent_router.by_keywords(payload.message, allowed, payload.has_images),
                "decided_by": "keywords"}

    history = [{"role": m.role, "content": m.content} for m in payload.history]
    answer = await _ask_helper(principal, intent_router.build_request(payload.message, allowed, history))
    action = intent_router.clean_answer(answer, allowed)
    if action:
        return {"action": action, "decided_by": settings.router_model()}
    return {"action": intent_router.by_keywords(payload.message, allowed, payload.has_images),
            "decided_by": "keywords"}


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
            stored = ChatFile(user_id=user_id, kind="generated", mime_type="image/png",
                              name=f"image-{seed}.png", prompt=drawable, data=png)
            db.add(stored)
            await db.commit()
            await db.refresh(stored)
            yield _event("image", file=_file_json(stored), seed=seed,
                         seconds=round(time.monotonic() - started, 1))

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
