"""The playground: saved conversations, images, and the tools a chat can switch on.

Every user only ever sees their own conversations and files.
"""
import asyncio
import base64
import binascii
import io
import json
import logging
import os
import re
import shutil
import secrets
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import access, backends, feedback, limits, settings, usage as usage_log
from app.auth import Principal, authenticate
from app.db import get_session, session_factory
from app.models import (AnswerFeedback, ChatFile, Conversation, ConversationMessage, MemoryEntry, SharedAccessRequest,
                        SharedConversation, User, utcnow)
from app.engine.supervisor import supervisor
from app.routers.openai_v1 import PROXY_PATHS, forward
from app.tools import (agent, chooser, documents, image_prompt, images, jobs, map_plan,
                        map_web, maps, markdown, mcp_client, media, memory as memory_tool,
                        photo, places, portrait, router as intent_router, self_tools, subtitles,
                        turn_facts, understand, video_story, voices, web_search)

log = logging.getLogger("playground")

router = APIRouter(prefix="/chat", tags=["chat"])

# Asked for a web page: the model is told which picture addresses load (see where it is used)
WANTS_A_PAGE = re.compile(r"\b(web ?page|web ?site|landing page|home ?page|html|single[- ]page|page design|portfolio site)\b", re.I)
PAGE_IMAGES = """If you write a web page, every picture in it must load. Never write a photo address from
memory: made-up Unsplash, Pexels or similar links do not exist and show as broken images. For each
picture use https://picsum.photos/seed/<one-word-subject>/<width>/<height> (for example
https://picsum.photos/seed/beach/800/600) and give it an alt text naming what it shows, such as
alt="Tropical beach at sunset": the page's preview replaces each picture with a real photo of what
its alt text names. CSS background pictures follow the same rule."""

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


class ShareIn(BaseModel):
    allow_edit_requests: bool = True


class PersonIn(BaseModel):
    action: str = Field(pattern="^(approve|reject|remove)$")


def _require_user(principal):
    if principal.user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conversations belong to a user; the master key has none")
    return principal.user


async def _owned(session, conversation_id, user, with_messages=False):
    """The person's conversation by its number, or by its uid (the playground's address)."""
    key = (Conversation.id == int(conversation_id) if str(conversation_id).isdigit()
           else Conversation.uid == str(conversation_id))
    query = select(Conversation).where(key, Conversation.user_id == user.id)
    if with_messages:
        query = query.options(selectinload(Conversation.messages))
    conversation = (await session.execute(query)).scalar_one_or_none()
    if conversation is None:
        # Same answer whether it does not exist or belongs to someone else
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


# What a person may do in a conversation shared with them (SharedAccessRequest.status): "viewer" -
# they opened the link and may read it; "pending" - they asked to write in it; "approved" - they may;
# "rejected" - they asked and were refused, and still read it
MEMBER_READS = ("viewer", "pending", "approved", "rejected")


async def _access(session, conversation_id, user, write=False, with_messages=False):
    """(conversation, how): the conversation and "owner", "editor" or "viewer" - by its number or uid.
    Someone else's conversation is theirs to read only while it is shared and they have opened its
    link, and to write in only once its owner has approved them. 404 otherwise, as for one that does
    not exist."""
    key = (Conversation.id == int(conversation_id) if str(conversation_id).isdigit()
           else Conversation.uid == str(conversation_id))
    query = select(Conversation).where(key)
    if with_messages:
        query = query.options(selectinload(Conversation.messages))
    conversation = (await session.execute(query)).scalar_one_or_none()
    if conversation is not None and conversation.user_id == user.id:
        return conversation, "owner"
    if conversation is not None:
        member = (await session.execute(
            select(SharedAccessRequest.status).join(SharedConversation,
                                                    SharedConversation.id == SharedAccessRequest.shared_id)
            .where(SharedConversation.conversation_id == conversation.id, SharedConversation.status == "active",
                   SharedAccessRequest.requester_id == user.id))).scalar_one_or_none()
        if member == "approved":
            return conversation, "editor"
        if member in MEMBER_READS and not write:
            return conversation, "viewer"
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")


async def _writable(session, conversation_id, user, with_messages=False):
    """The conversation, when this person may add to it: its owner, or someone it is shared with whom
    the owner has approved."""
    return (await _access(session, conversation_id, user, write=True, with_messages=with_messages))[0]


def _conversation_json(conversation, message_count=None):
    data = {
        "id": conversation.id, "uid": conversation.uid, "title": conversation.title, "model": conversation.model,
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
        # Who wrote it, in a conversation several people write in (None: its owner, before sharing)
        "author_id": message.author_id,
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
async def get_conversation(conversation_id: str, principal: Principal = Depends(authenticate),
                           session: AsyncSession = Depends(get_session)):
    user = _require_user(principal)
    conversation, how = await _access(session, conversation_id, user, with_messages=True)
    ids = [m.id for m in conversation.messages if m.role == "assistant"]
    judged = {f.message_id: {"helpful": f.helpful, "category": f.category, "reason": f.reason} for f in (await session.execute(
        select(AnswerFeedback).where(AnswerFeedback.user_id == user.id,
                                     AnswerFeedback.message_id.in_(ids)))).scalars()} if ids else {}
    # The names of whoever wrote in it, so a shared conversation shows who said what
    authors = {m.author_id for m in conversation.messages if m.author_id} | {conversation.user_id}
    names = {u.id: u.name for u in (await session.execute(select(User).where(User.id.in_(authors)))).scalars()}
    shared = (await session.execute(select(SharedConversation).where(
        SharedConversation.conversation_id == conversation.id, SharedConversation.status == "active"))).scalar_one_or_none()
    asked = None
    if how != "owner" and shared is not None:
        asked = (await session.execute(select(SharedAccessRequest.status).where(
            SharedAccessRequest.shared_id == shared.id, SharedAccessRequest.requester_id == user.id))).scalar_one_or_none()
    return {**_conversation_json(conversation, len(conversation.messages)),
            "access": how, "owner": names.get(conversation.user_id), "shared": shared is not None,
            # a reader may ask to write in it, once, while its owner lets people ask
            "may_ask": how == "viewer" and asked == "viewer" and bool(shared and shared.allow_edit_requests),
            "asked": asked,
            "messages": [{**_message_json(m), "author": names.get(m.author_id or (conversation.user_id if m.role == "user" else 0)),
                          "by_me": m.role == "user" and (m.author_id or conversation.user_id) == user.id,
                          "feedback": judged.get(m.id)} for m in conversation.messages]}


class FeedbackIn(BaseModel):
    message_id: int | None = None
    conversation_id: int | None = None
    starts_with: str = Field(default="", max_length=400)
    helpful: bool | None = None
    # The task the verdict counts against (chooser.FEEDBACK_TASKS): the kind of question the router
    # read, or what the person said went wrong - "the translation was wrong" counts against translating
    category: str | None = Field(default=None, max_length=32)
    reason: str | None = Field(default=None, max_length=500)
    file_id: int | None = None      # a picture drawn, found by its file: its text is only its prompt


@router.post("/feedback")
async def give_feedback(payload: FeedbackIn, principal: Principal = Depends(authenticate),
                        session: AsyncSession = Depends(get_session)):
    """Helpful? for one answer, from the person who asked. Asking again replaces the verdict.

    A "No" can say what went wrong, which is what Auto learns from: the model is ranked lower for that
    task - drawing, reading a picture, translating, code - and only a little for anything else."""
    user = _require_user(principal)
    if payload.category and payload.category not in chooser.FEEDBACK_TASKS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown kind of work: {payload.category}")
    if payload.message_id is not None:
        message = await session.get(ConversationMessage, payload.message_id)
        if message is not None:
            await _access(session, message.conversation_id, user)
    elif payload.conversation_id is not None and payload.file_id is not None:
        await _access(session, payload.conversation_id, user)
        message = (await session.execute(
            select(ConversationMessage).where(ConversationMessage.conversation_id == payload.conversation_id,
                                              ConversationMessage.role == "assistant",
                                              # its own picture, not one edited from it (source_file_id)
                                              ConversationMessage.attachments.startswith(
                                                  f'{{"generated": {{"file_id": {payload.file_id},', autoescape=True))
            .order_by(ConversationMessage.id.desc()).limit(1))).scalar_one_or_none()
    elif payload.conversation_id is not None and payload.starts_with:
        await _access(session, payload.conversation_id, user)
        message = (await session.execute(
            select(ConversationMessage).where(ConversationMessage.conversation_id == payload.conversation_id,
                                              ConversationMessage.role == "assistant",
                                              ConversationMessage.content.startswith(payload.starts_with, autoescape=True))
            .order_by(ConversationMessage.id.desc()).limit(1))).scalar_one_or_none()
    else:
        message = None
    if message is None or message.role != "assistant":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That answer is not saved, so it cannot be rated")
    entry = (await session.execute(select(AnswerFeedback).where(
        AnswerFeedback.message_id == message.id, AnswerFeedback.user_id == user.id))).scalar_one_or_none()
    if entry is None:
        entry = AnswerFeedback(message_id=message.id, user_id=user.id, model=message.model)
        session.add(entry)
    entry.helpful, entry.updated_at = payload.helpful, utcnow()
    if payload.category:
        entry.category = payload.category
    # What went wrong belongs to a "No"; a "Yes", or taking the verdict back, clears it
    entry.reason = (payload.reason or "").strip() or None if payload.helpful is False else None
    await session.commit()
    return {"message_id": message.id, "helpful": entry.helpful, "category": entry.category, "model": entry.model}


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


# --- shared conversations -------------------------------------------------------------------------
# A conversation shared by a link: whoever is signed in and opens it may read it (and is listed, as a
# "viewer"); they may ask to write in it, and its owner approves or refuses them. Approved, they add
# messages as the owner does, each tagged with who wrote it. Stopping the sharing ends all of it.

def _share_json(shared, people=()):
    return {"token": shared.token, "path": f"/s/{shared.token}", "allow_edit_requests": shared.allow_edit_requests,
            "created_at": shared.created_at.isoformat(),
            "people": [{"id": person_id, "name": name, "status": state} for person_id, name, state in people]}


async def _share_of(session, conversation):
    return (await session.execute(select(SharedConversation).where(
        SharedConversation.conversation_id == conversation.id, SharedConversation.status == "active"))).scalar_one_or_none()


async def _people(session, shared):
    rows = await session.execute(
        select(User.id, User.name, SharedAccessRequest.status).join(User, User.id == SharedAccessRequest.requester_id)
        .where(SharedAccessRequest.shared_id == shared.id).order_by(SharedAccessRequest.created_at))
    return rows.all()


@router.get("/conversations/{conversation_id}/share")
async def get_share(conversation_id: int, principal: Principal = Depends(authenticate),
                    session: AsyncSession = Depends(get_session)):
    """The owner's view of a conversation's sharing: its link, and who has opened it or asked to write."""
    conversation = await _owned(session, conversation_id, _require_user(principal))
    shared = await _share_of(session, conversation)
    return {"shared": None if shared is None else _share_json(shared, await _people(session, shared))}


@router.post("/conversations/{conversation_id}/share")
async def share_conversation(conversation_id: int, payload: ShareIn, principal: Principal = Depends(authenticate),
                             session: AsyncSession = Depends(get_session)):
    """A link to the conversation, made once and kept; asked again, it changes whether people may ask
    to write in it."""
    user = _require_user(principal)
    conversation = await _owned(session, conversation_id, user)
    shared = await _share_of(session, conversation)
    if shared is None:
        # A share stopped earlier is made anew, with a new link: the old one stays dead
        old = (await session.execute(select(SharedConversation).where(
            SharedConversation.conversation_id == conversation.id))).scalar_one_or_none()
        if old is not None:
            await session.delete(old)
            await session.flush()
        shared = SharedConversation(token=secrets.token_urlsafe(24), conversation_id=conversation.id, owner_id=user.id)
        session.add(shared)
    shared.allow_edit_requests = payload.allow_edit_requests
    await session.commit()
    return {"shared": _share_json(shared, await _people(session, shared))}


@router.delete("/conversations/{conversation_id}/share")
async def stop_sharing(conversation_id: int, principal: Principal = Depends(authenticate),
                       session: AsyncSession = Depends(get_session)):
    """The link stops working, and nobody it was shared with can read or write in it any more."""
    conversation = await _owned(session, conversation_id, _require_user(principal))
    shared = await _share_of(session, conversation)
    if shared is not None:
        await session.delete(shared)          # its people go with it (ON DELETE CASCADE)
        await session.commit()
    return {"shared": None}


@router.post("/conversations/{conversation_id}/share/people/{person_id}")
async def decide_person(conversation_id: int, person_id: int, payload: PersonIn,
                        principal: Principal = Depends(authenticate), session: AsyncSession = Depends(get_session)):
    """The owner lets someone write in the conversation, refuses them (they may still read it), or
    removes them (they lose it until they open the link again)."""
    conversation = await _owned(session, conversation_id, _require_user(principal))
    shared = await _share_of(session, conversation)
    if shared is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This conversation is not shared")
    member = (await session.execute(select(SharedAccessRequest).where(
        SharedAccessRequest.shared_id == shared.id, SharedAccessRequest.requester_id == person_id))).scalar_one_or_none()
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That person has not opened this conversation's link")
    if payload.action == "remove":
        await session.delete(member)
    else:
        member.status = "approved" if payload.action == "approve" else "rejected"
    await session.commit()
    return {"shared": _share_json(shared, await _people(session, shared))}


@router.post("/conversations/{conversation_id}/share/ask")
async def ask_to_write(conversation_id: str, principal: Principal = Depends(authenticate),
                       session: AsyncSession = Depends(get_session)):
    """Someone reading a shared conversation asks its owner to let them write in it."""
    user = _require_user(principal)
    conversation, how = await _access(session, conversation_id, user)
    if how != "viewer":
        raise HTTPException(status.HTTP_409_CONFLICT, "You can already write in it, or have asked")
    shared = await _share_of(session, conversation)
    if shared is None or not shared.allow_edit_requests:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Its owner does not take requests to write in it")
    member = (await session.execute(select(SharedAccessRequest).where(
        SharedAccessRequest.shared_id == shared.id, SharedAccessRequest.requester_id == user.id))).scalar_one()
    if member.status != "viewer":
        raise HTTPException(status.HTTP_409_CONFLICT, "You have asked already")
    member.status = "pending"
    await session.commit()
    return {"asked": "pending"}


SHARE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


async def join_share(session, user, token):
    """Opening a shared link: the person is listed as one who may read the conversation (its owner
    stays its owner). Its uid, or None when the link was never made or its sharing was stopped."""
    if not SHARE_TOKEN.match(token or ""):
        return None
    shared = (await session.execute(select(SharedConversation).where(
        SharedConversation.token == token, SharedConversation.status == "active"))).scalar_one_or_none()
    if shared is None:
        return None
    conversation = await session.get(Conversation, shared.conversation_id)
    if conversation.user_id != user.id:
        member = (await session.execute(select(SharedAccessRequest).where(
            SharedAccessRequest.shared_id == shared.id, SharedAccessRequest.requester_id == user.id))).scalar_one_or_none()
        if member is None:
            session.add(SharedAccessRequest(shared_id=shared.id, requester_id=user.id, status="viewer"))
            await session.commit()
    return conversation.uid


@router.post("/shared/{token}/join")
async def join_shared(token: str, principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    """Opening a shared link from a client: the person may read it now, and gets its address."""
    uid = await join_share(session, _require_user(principal), token)
    if uid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link does not work: it was never made, or its sharing was stopped")
    return {"uid": uid}


@router.get("/shared-with-me")
async def shared_with_me(principal: Principal = Depends(authenticate), session: AsyncSession = Depends(get_session)):
    """Conversations other people have shared and this person has opened: the playground lists them."""
    user = _require_user(principal)
    rows = await session.execute(
        select(Conversation, User.name, SharedAccessRequest.status)
        .join(SharedConversation, SharedConversation.conversation_id == Conversation.id)
        .join(SharedAccessRequest, SharedAccessRequest.shared_id == SharedConversation.id)
        .join(User, User.id == Conversation.user_id)
        .where(SharedAccessRequest.requester_id == user.id, SharedConversation.status == "active")
        .order_by(Conversation.updated_at.desc()).limit(LIST_LIMIT))
    return [{**_conversation_json(conversation), "owner": owner, "access": "editor" if state == "approved" else "viewer"}
            for conversation, owner, state in rows.all()]


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def add_message(conversation_id: int, payload: MessageIn, principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    user = _require_user(principal)
    conversation = await _writable(session, conversation_id, user)
    message = ConversationMessage(conversation_id=conversation.id, role=payload.role, content=payload.content,
                                  author_id=user.id if payload.role == "user" else None,
                                  reasoning=payload.reasoning or None, model=payload.model,
                                  stats=json.dumps(payload.stats) if payload.stats else None,
                                  attachments=json.dumps(payload.attachments) if payload.attachments else None)
    session.add(message)
    conversation.updated_at = utcnow()
    # A message's model says who produced it, and for a tool's answer the playground signs it with
    # the tool - "OpenStreetMap", "Picture search" - because that is the truth and it belongs in
    # the transcript. The conversation's model means something else entirely: which model to carry
    # on with. Adopting one as the other left conversations pointing at a name that is not a model
    # at all, so reopening them announced that you had lost access to a model you never had.
    if payload.model and await backends.resolve(payload.model) is not None:
        conversation.model = payload.model
    await session.commit()
    await session.refresh(message)
    if payload.role == "assistant" and memory_tool.is_enabled():
        # After the reply, so the wait is never the user's
        asyncio.create_task(_refresh_memory(principal, conversation.user_id, conversation.id))
    return _message_json(message)


# --- tools ------------------------------------------------------------------------

@router.delete("/conversations/{conversation_id}/messages/{message_id}/after")
async def drop_what_followed(conversation_id: int, message_id: int,
                             principal: Principal = Depends(authenticate),
                             session: AsyncSession = Depends(get_session)):
    """Removes the turns that came after a question, so that it can be asked again.

    Asking an older question again leaves everything after it answering something that is no
    longer there. What is kept is the question and its answers - the run of replies directly after
    it, however many times it has been asked - and everything from the next question onwards goes.

    The conversation's running summary goes with them: it describes turns that no longer exist,
    and it carries a count of how many messages it stands in for, which would otherwise swallow
    the messages that remain. It is written again after the next turn.
    """
    user = _require_user(principal)
    conversation = await _owned(session, conversation_id, user, with_messages=True)
    messages = sorted(conversation.messages, key=lambda m: m.id)
    at = next((i for i, m in enumerate(messages) if m.id == message_id), None)
    if at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That message is not in this conversation")
    if messages[at].role != "user":
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Only a question can be asked again, so only a question has turns "
                            "after it to drop")
    # Where this question's own answers end and the next question begins
    ends = next((i for i in range(at + 1, len(messages)) if messages[i].role == "user"),
                len(messages))
    doomed = messages[ends:]
    for message in doomed:
        await session.delete(message)
    if doomed:
        summary = await _conversation_memory(session, user.id, conversation_id)
        if summary is not None:
            await session.delete(summary)
        conversation.updated_at = utcnow()
        await session.commit()
    return {"removed": len(doomed)}


class EditedQuestionIn(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)


@router.patch("/conversations/{conversation_id}/messages/{message_id}")
async def edit_question(conversation_id: int, message_id: int, payload: EditedQuestionIn,
                        principal: Principal = Depends(authenticate),
                        session: AsyncSession = Depends(get_session)):
    """Rewords a question, so that it can be asked again as it should have been asked.

    Everything after it goes, its own answers too: they answer words that are no longer there.
    The playground then asks the reworded question, and the new answer is filed under it. The
    running summary goes for the same reason as in drop_what_followed.
    """
    user = _require_user(principal)
    conversation = await _owned(session, conversation_id, user, with_messages=True)
    messages = sorted(conversation.messages, key=lambda m: m.id)
    at = next((i for i, m in enumerate(messages) if m.id == message_id), None)
    if at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That message is not in this conversation")
    if messages[at].role != "user":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only your own messages can be edited")
    messages[at].content = payload.content
    doomed = messages[at + 1:]
    for message in doomed:
        await session.delete(message)
    summary = await _conversation_memory(session, user.id, conversation_id)
    if summary is not None:
        await session.delete(summary)
    conversation.updated_at = utcnow()
    await session.commit()
    return {"removed": len(doomed)}


@router.get("/capabilities")
async def capabilities(principal: Principal = Depends(authenticate)):
    """Which tools this server offers; the playground only shows toggles for these."""
    return {
        "web_search": web_search.is_available(),
        # Finding a real photograph of a real thing, rather than drawing one. It rides on the same
        # search service, so it is offered exactly when web search is.
        "media_search": web_search.is_available() and media.is_available(),
        "vision": settings.feature_enabled("vision"),
        "image_generation": images.is_available() and access.can_generate_images(principal),
        # A narrated, illustrated story video: pictures, narration and ffmpeg all needed
        "video": images.is_available() and video_story.is_available() and access.can_generate_images(principal),
        "image_sizes": list(images.SIZES),
        # "qwen" follows edit instructions and takes up to 3 images; "flux" re-renders one image
        "edit_engine": await images.edit_engine() if images.is_available() else None,
        "max_edit_images": images.MAX_EDIT_IMAGES,
        "photo_clean": images.is_available(),
        "photo_blur": images.is_available() and photo.is_depth_available(),
        "photo_backdrop": images.is_available() and portrait.is_available(),
        # A YouTube link's subtitles, transcribed from its sound on this server
        "subtitles": subtitles.is_available(),
        "maps": settings.feature_enabled("maps"),
        # Whether there is anything to be agentic with: the feature on, and a tool server
        # that this host could actually run
        "agent": agent.is_available_to(principal),
        # Which map the browser must draw on, and the browser key for it when that is Google's.
        # The two are never mixed: Google's terms forbid their places on anybody else's tiles.
        "maps_tiles": places.tiles() if settings.feature_enabled("maps") else "osm",
        "google_maps_key": (settings.google_maps_key()
                            if settings.feature_enabled("maps") and places.name() == "google"
                            else ""),
        "routing": intent_router.is_enabled(),
        # Never picked by Auto to answer: they read messages and index documents (chooser.is_helper)
        "helper_models": chooser.helper_names(),
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
    await limits.check_storage(principal, session, len(data))
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
async def get_file(file_id: int, request: Request, principal: Principal = Depends(authenticate),
                   session: AsyncSession = Depends(get_session)):
    user = _require_user(principal)
    stored = (await session.execute(
        select(ChatFile).where(ChatFile.id == file_id, ChatFile.user_id == user.id))).scalar_one_or_none()
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    headers = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
               "Accept-Ranges": "bytes"}
    # A video is played in pieces: Safari will not play one at all without byte ranges, and seeking
    # anywhere needs them
    wanted = re.match(r"bytes=(\d*)-(\d*)$", request.headers.get("range") or "")
    size = len(stored.data)
    if wanted and size and (wanted.group(1) or wanted.group(2)):
        if wanted.group(1):
            start = int(wanted.group(1))
            end = min(int(wanted.group(2)) if wanted.group(2) else size - 1, size - 1)
        else:
            start, end = max(0, size - int(wanted.group(2))), size - 1
        if start > end or start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        return Response(stored.data[start:end + 1], status_code=206, media_type=stored.mime_type,
                        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"})
    return Response(stored.data, media_type=stored.mime_type, headers=headers)


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
    # Which question this answers. The playground can be asked to run a question again, and
    # the answers are kept side by side rather than one painting over the other; that only
    # works if each answer says which question it belongs to, and the answer is written down
    # here rather than by the browser.
    answers_to: int | None = None


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
        await _writable(session, payload.conversation_id, user)
    use_vision = payload.vision and settings.feature_enabled("vision")
    latest_user = next((m for m in reversed(payload.messages) if m.role == "user"), None)
    # Pictures from earlier turns are dropped for a text-only model rather than failing the chat
    use_vision = use_vision and "vision" in backend.capabilities
    # Every attachment is loaded, then sorted: pictures go to a model that can see, documents go to
    # any model at all, because by the time they get there they are only text
    attached = [i for m in payload.messages for i in m.images]
    # Counted on the message being sent: a long conversation gathers more than that over time
    limits.check_attachments(principal, len(set(payload.messages[-1].images)) if payload.messages else 0)
    everything = await _load_files(session, user, attached)
    papers = [f for f in everything.values() if f.kind == "document"]
    # Only a picture in the message being sent stops a text-only model. One from an earlier turn
    # failed every later question in the conversation once Auto picked a model that cannot see.
    asked_about = set(latest_user.images) if latest_user else set()
    if any(i in asked_about and f.kind != "document" for i, f in everything.items()) \
            and payload.vision and "vision" not in backend.capabilities:
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
    latest_words = next((m.content for m in reversed(payload.messages) if m.role == "user"), "")
    if WANTS_A_PAGE.search(latest_words or ""):
        # Models write photo addresses from memory, and they do not exist: three made-up Unsplash
        # photos in a travel page, all 404 (2026-10-21). Picsum always answers, and the preview swaps
        # each picture for a real photo of what its alt text names (playground.html, PAGE_FIXER).
        messages.insert(1 if messages and messages[0]["role"] == "system" else 0,
                        {"role": "system", "content": PAGE_IMAGES})
    if payload.conversation_id is not None and turn_facts.asks_how_made(latest_words):
        # What made the last few answers - a picture's model, a search, a map - read from what was
        # saved with them, so a question about it is answered from the record rather than guessed.
        # Only when the question is about that: put in front of every question, Qwen3-Next took it
        # for the task and answered "Difference between ECC and non-ECC memory" with token counts.
        recent = (await session.execute(
            select(ConversationMessage).where(ConversationMessage.conversation_id == payload.conversation_id)
            .order_by(ConversationMessage.id.desc()).limit(4 * turn_facts.RECENT))).scalars().all()
        facts = turn_facts.context_block(list(reversed(recent)))
        if facts:
            messages.insert(1 if messages and messages[0]["role"] == "system" else 0,
                            {"role": "system", "content": facts})

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
        # Measured here, where the model's words arrive one by one. The browser's own clock can see
        # them in a burst - after a web search, through a proxy - and once showed 1760 tok/s.
        yield _event("stats", stats=answer.stats(), model=payload.model)

    async def save(_job):
        """Writes the turn down once the answer is finished, whoever is still watching."""
        if payload.conversation_id is None or not (answer.text or answer.reasoning):
            return
        async with session_factory()() as db:
            conversation = await db.get(Conversation, payload.conversation_id)
            if conversation is None or conversation.user_id != user.id:
                return
            filed = {}
            if answer.sources:
                filed["sources"] = answer.sources
            if payload.answers_to is not None:
                filed["answers_to"] = payload.answers_to
            db.add(ConversationMessage(
                conversation_id=payload.conversation_id, role="assistant", content=answer.text,
                reasoning=answer.reasoning or None, model=payload.model,
                stats=json.dumps(answer.stats()),
                attachments=json.dumps(filed) if filed else None))
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
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Job-Id": str(job.id)})


@router.get("/conversations/{conversation_id}/job")
async def running_job(conversation_id: int, principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    """What this conversation is still busy with, so reopening it can pick the thread back up."""
    user = _require_user(principal)
    await _access(session, conversation_id, user)
    job = jobs.running_for(user.id, conversation_id)
    return {"job": job.json() if job else None}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, principal: Principal = Depends(authenticate)):
    """The Stop button: a job outlives the page that started it, so closing the stream stops nothing."""
    return {"cancelled": jobs.cancel(_require_user(principal).id, job_id)}


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
    """After a turn: the conversation's running summary when it is due, and the user's notes always.

    Runs in its own session and never disturbs the chat: failures are only logged.
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
            model = memory_tool.model_for(conversation.model)
            if not model:
                return
            await _refresh_summary(principal, session, user_id, conversation, model)
            notes_model = await _notes_model(conversation.model)
            if notes_model:
                await _check_notes(principal, session, user_id, conversation, notes_model)
    except Exception as e:  # memory is a convenience; never let it break a conversation
        log.warning("Could not refresh memory for conversation %s: %s", conversation_id, e)
    finally:
        _refreshing.discard(conversation_id)


async def _refresh_summary(principal, session, user_id, conversation, model):
    """Rewrites the summary of the turns that are no longer sent in full, every few turns."""
    keep = settings.keep_recent_messages()
    older = conversation.messages[:-keep] if keep else list(conversation.messages)
    if not older:
        return
    entry = await _conversation_memory(session, user_id, conversation.id)
    if entry is not None and entry.source == "manual":
        return  # the user wrote these notes themselves
    if entry is not None and len(older) - entry.covered_count < settings.summarize_every():
        return
    previous = entry.content if entry else None
    # The whole conversation is summarised, but only the older turns count as covered: the recent
    # ones are still sent in full, and the notes are the better for including them
    summary = memory_tool.clean_text(await _ask_helper(
        principal, memory_tool.summary_request(previous, memory_tool.transcript(conversation.messages), model)))
    if not summary:
        return
    if entry is None:
        entry = MemoryEntry(user_id=user_id, scope="conversation", conversation_id=conversation.id)
        session.add(entry)
    entry.content = summary
    entry.covered_count = len(older)
    entry.source = "auto"
    entry.updated_at = utcnow()
    await session.commit()


MEMORY_TOOLS = object()     # where[] marker for the agent's memory tools, as self_tools.BUILTIN is for its own


async def _agent_memory(user_id):
    """The user's notes for one agent turn, keyed n1, n2 ... as the model sees them."""
    async with session_factory()() as db:
        notes = list((await db.execute(select(MemoryEntry).where(
            MemoryEntry.user_id == user_id, MemoryEntry.scope == "user").order_by(MemoryEntry.id))).scalars())
    state = {"ids": {f"n{i + 1}": n.id for i, n in enumerate(notes)},
             "text": {f"n{i + 1}": n.content for i, n in enumerate(notes)},
             "manual": {f"n{i + 1}" for i, n in enumerate(notes) if n.source == "manual"}}
    state["listed"] = lambda: [(k, t, k in state["manual"]) for k, t in state["text"].items()]
    return state


async def _memory_edit(user_id, memory, tool, args, seen_text):
    """One memory tool call from the agent, checked and applied at once. Returns (text, failed)."""
    if not memory:
        return "Memory is switched off.", True
    edit, refused = memory_tool.check_tool_edit(tool, args, memory["text"], seen_text, memory["manual"])
    if refused:
        return refused, True
    action, key, text = edit
    async with session_factory()() as db:
        if action == "add":
            if len(memory["text"]) >= settings.max_user_notes():
                return (f"Not added: there are {settings.max_user_notes()} notes, the most kept. "
                        "Update or forget one first."), True
            entry = MemoryEntry(user_id=user_id, scope="user", content=text, source="auto")
            db.add(entry)
            await db.commit()
            key = f"n{max([int(k[1:]) for k in memory['text']] + [0]) + 1}"
            memory["ids"][key], memory["text"][key] = entry.id, text
            log.info("Agent noted for user %s: %s", user_id, text)
            return f"Noted as {key}.", False
        entry = await db.get(MemoryEntry, memory["ids"][key])
        if entry is None or entry.user_id != user_id:
            return f"Not changed: {key} is gone.", True
        if action == "update":
            log.info("Agent updated a note for user %s: %r -> %r", user_id, entry.content, text)
            entry.content, entry.updated_at = text, utcnow()
            memory["text"][key] = text
        else:
            log.info("Agent forgot a note for user %s: %r", user_id, entry.content)
            await db.delete(entry)
            del memory["text"][key], memory["ids"][key]
        await db.commit()
    return f"{key} {'updated' if action == 'update' else 'forgotten'}.", False


NOTES_MIN_BILLIONS = 14     # smaller models were measured to record plans as facts and to duplicate notes


async def _notes_model(conversation_model):
    """Which model keeps the user's notes: one able to, and never one loaded just for this.

    memory.model when it is set. Otherwise the conversation's own model when it is big enough, then a
    big enough model already on the GPU. llama3.2:3b, asked to keep notes, missed a RAM upgrade, wrote
    a planned GPU down as installed and added a note that was already there - so with only a small
    model about, this turn's check is skipped, and the next turn with a capable one does it.
    """
    if settings.memory_model():
        return settings.memory_model()
    available = await backends.available_models()

    def able(name):
        backend = available.get(name)
        if backend is None or chooser.is_helper(name):
            return False
        return chooser._parameters({"name": name, "parameter_size": (backend.details or {}).get(
            "parameter_size", "")}) >= NOTES_MIN_BILLIONS
    if conversation_model and able(conversation_model):
        return conversation_model
    for name in supervisor.loaded_names() + await supervisor.ollama_loaded():
        if able(name):
            return name
    return None


async def _check_notes(principal, session, user_id, conversation, model):
    """Checks the latest exchange against the user's notes and applies the edits it calls for.

    Every turn, so a fact that stops being true mid-task is taken out then and there, not left to
    contradict the next conversation. The user's own notes are shown, so nothing repeats them, and
    are never changed.
    """
    exchange, user_text = memory_tool.exchange_text(conversation.messages)
    if not exchange:
        return
    notes = list((await session.execute(select(MemoryEntry).where(
        MemoryEntry.user_id == user_id, MemoryEntry.scope == "user").order_by(MemoryEntry.id))).scalars())
    by_key = {f"n{i + 1}": note for i, note in enumerate(notes)}
    protected = {key for key, note in by_key.items() if note.source == "manual"}
    changes = memory_tool.parse_changes(await _ask_helper(principal, memory_tool.check_request(
        [(key, note.content, key in protected) for key, note in by_key.items()], exchange, model)),
        {key: note.content for key, note in by_key.items()}, user_text, protected)
    if not changes or not any(changes.values()):
        return
    for key, text in changes["update"].items():
        by_key[key].content, by_key[key].updated_at = text, utcnow()
    for key in changes["delete"]:
        await session.delete(by_key[key])
    room = settings.max_user_notes() - (len(notes) - len(changes["delete"]))
    for text in changes["add"][:max(room, 0)]:
        session.add(MemoryEntry(user_id=user_id, scope="user", content=text, source="auto"))
    await session.commit()
    log.info("Notes for user %s: %d updated, %d deleted, %d added", user_id,
             len(changes["update"]), len(changes["delete"]), min(len(changes["add"]), max(room, 0)))


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


async def _save_result(db, user_id, conversation_id, png, name, prompt, extra=None, stats=None,
                       answers_to=None, model=None):
    """Stores the picture and, when it belongs to a conversation, the message that shows it.

    Written by the server rather than the browser so that a dropped connection cannot lose it: the
    work is done and paid for, and reopening the conversation has to show it. `model` is the image
    model that drew it, so a "No" under the picture counts against that model's drawing.
    """
    stored = ChatFile(user_id=user_id, kind="generated", mime_type="image/png",
                      name=name, prompt=prompt, data=png)
    db.add(stored)
    await db.commit()
    await db.refresh(stored)
    if conversation_id is not None:
        generated = {"file_id": stored.id, "prompt": prompt, **(extra or {})}
        filed = {"generated": generated}
        if answers_to is not None:
            filed["answers_to"] = answers_to
        conversation = await db.get(Conversation, conversation_id)
        if conversation is not None and conversation.user_id == user_id:
            db.add(ConversationMessage(conversation_id=conversation_id, role="assistant",
                                       content=f"({prompt.lower()})", model=model,
                                       stats=json.dumps(stats) if stats else None,
                                       attachments=json.dumps(filed)))
            conversation.updated_at = utcnow()
            await db.commit()
    return stored


# --- pictures of real things ----------------------------------------------------------------------

class MediaIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: int | None = None
    kind: str = Field(default="auto", pattern="^(auto|images|videos)$")


async def _media_subjects(principal, session, user, payload):
    """What the message is actually asking to see, in words a search box can use.

    "Show me the photos of the drinks you are suggesting" names nothing at all, and searching for
    it finds pictures of the word drinks. What it points at is two paragraphs back, so the
    conversation goes to the small model and comes back as "Contrex mineral water bottle".
    """
    history = []
    if payload.conversation_id is not None:
        conversation = (await session.execute(
            select(Conversation).options(selectinload(Conversation.messages))
            .where(Conversation.id == payload.conversation_id,
                   Conversation.user_id == user.id))).scalar_one_or_none()
        history = [{"role": m.role, "content": m.content or ""}
                   for m in (conversation.messages if conversation else [])[-6:]]
    if not intent_router.is_enabled():
        return [media.plain_subject(payload.message)], "your own words"
    answer = await _ask_helper(principal, media.build_request(settings.router_model(),
                                                              payload.message, history))
    subjects = media.read_subjects(answer, payload.message)
    return subjects, settings.router_model() if answer else "your own words"


async def _pictures_of(subject, kind):
    """Candidates for one subject, fetched and proved to be pictures. Never raises."""
    try:
        if kind == media.VIDEOS:
            found = await media.find_videos(subject)
            # The thumbnail is all that is fetched: the video itself stays where it is and is
            # watched there, which is also the only way its publisher gets counted
            return await media.collect([f for f in found if f["src"]], keep=media.KEEP,
                                       smallest=media.THUMB_SMALLEST)
        found = await media.find_images(subject)
        pictures = await media.collect(found)
        if len(pictures) >= 2:
            return pictures
        # The image engines index the well known. A local shop, a small manufacturer or anything
        # written up in one language and asked about in another can be missing from them entirely
        # while its own front page carries a photograph of it.
        log.info("only %d pictures of %r from image search; reading pages instead",
                 len(pictures), subject)
        scraped = await media.scrape_pages(subject)
        seen = {p["src"] for p in pictures}
        return pictures + await media.collect([s for s in scraped if s["src"] not in seen],
                                              keep=media.KEEP - len(pictures))
    except media.MediaError as e:
        log.info("no media for %r (%s)", subject, e)
        return []
    except Exception as e:                      # one bad subject must not lose the other two
        log.warning("media search for %r failed: %s", subject, e, exc_info=True)
        return []


@router.post("/media")
async def find_media(payload: MediaIn, principal: Principal = Depends(authenticate),
                     session: AsyncSession = Depends(get_session)):
    """Finds real photographs or videos of what was asked about, and keeps a copy of each.

    A copy, rather than a link, for three reasons: somebody else's CDN is entitled to stop serving
    a URL at any moment and this conversation has to still show what it showed; a page full of
    hotlinks announces its reader to every site on it; and a good few of those hosts refuse a
    request that did not come from their own page, which would leave the answer full of broken
    frames. What is kept is a scaled-down copy, shown beside the name of the site it came from and
    a link to the page that carried it.
    """
    user = _require_user(principal)
    if not (web_search.is_available() and media.is_available()):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Searching the web is turned off on this server, so there is nowhere "
                            "to look for pictures.")
    if payload.conversation_id is not None:
        await _writable(session, payload.conversation_id, user)

    kind = payload.kind if payload.kind != "auto" else media.kind_of(payload.message)
    subjects, decided_by = await _media_subjects(principal, session, user, payload)
    if not subjects:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "It is not clear what to look for. Name the thing you want to see.")

    started = time.monotonic()
    everything = await asyncio.gather(*(_pictures_of(s, kind) for s in subjects))

    groups, saved = [], 0
    for subject, found in zip(subjects, everything):
        items = []
        for picture in found:
            stored = ChatFile(user_id=user.id, kind="web", mime_type=picture["mime"],
                              name=(picture.get("title") or subject)[:256],
                              prompt=picture.get("page") or picture["src"], data=picture["data"])
            session.add(stored)
            items.append((stored, picture))
        if items:
            await session.commit()
            saved += len(items)
        groups.append({
            "subject": subject,
            "items": [{"file_id": stored.id, "url": f"/chat/files/{stored.id}",
                       "title": picture.get("title") or subject,
                       "page": picture.get("page") or picture["src"],
                       "site": picture.get("site") or "",
                       "width": picture["width"], "height": picture["height"],
                       "found_by": picture.get("found_by") or "",
                       "length": picture.get("length") or "",
                       "author": picture.get("author") or "",
                       "embed": picture.get("embed") or ""}
                      for stored, picture in items],
        })

    result = {"asked": payload.message, "kind": kind, "groups": groups,
              "subjects_by": decided_by, "seconds": round(time.monotonic() - started, 1)}
    if not saved:
        result["note"] = (
            "Nothing usable came back for "
            + ", ".join(f"“{s}”" for s in subjects)
            + ". Either nobody has published a picture of it, or the ones that came back could not "
              "be fetched. Naming the thing more exactly usually finds it.")
    return result


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
    answers_to: int | None = None          # the question this answers, for asking it again


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
        await _writable(session, payload.conversation_id, user)
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
                                   f"photo_{payload.action}", True, 503, None,
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
                                        {"source_file_id": payload.file_id, "asked": payload.request,
                                         "made_by": turn_facts.photo_maker(payload.action, done)},
                                        {"total": seconds},
                                        answers_to=payload.answers_to)
            yield _event("image", file=_file_json(stored), action=payload.action,
                         did=done[:1].upper() + done[1:], saved=True, seconds=seconds)

    # One picture or transcription at a time per person (app/limits.py); the slot is given back
    # when the stream ends, however it ends
    slot = await limits.take_gpu_slot(principal)
    if payload.conversation_id is not None:
        asked = (payload.request or payload.action).strip()[:120]
        job = jobs.start(user.id, payload.conversation_id, "photo", asked, limits.holding(slot, stream()))
        return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return StreamingResponse(limits.holding(slot, stream()), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class SubtitlesIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)     # holds the YouTube link
    translate: bool = False                                 # English, whatever the video speaks
    conversation_id: int | None = None
    answers_to: int | None = None


@router.post("/subtitles")
async def video_subtitles(payload: SubtitlesIn, principal: Principal = Depends(authenticate),
                          session: AsyncSession = Depends(get_session)):
    """Subtitles for a YouTube video, transcribed from its sound. Streams progress, then the result.

    The transcript is saved as the answer's text, so the conversation can go on about the video -
    summarise it, translate it, ask what was said - and the .srt and .vtt files are saved beside it.
    """
    user = _require_user(principal)
    if not await asyncio.to_thread(subtitles.is_available):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Subtitles are not set up on this server")
    video = subtitles.video_id(payload.message)
    if not video:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No YouTube link in the message")
    if payload.conversation_id is not None:
        await _writable(session, payload.conversation_id, user)
    user_id = user.id

    async def stream():
        events = asyncio.Queue()

        async def progress(stage, fraction):
            await events.put(_event("progress", stage=stage, fraction=fraction))

        started = time.monotonic()
        job = asyncio.create_task(subtitles.subtitles(video, payload.translate, progress))
        try:
            while not job.done() or not events.empty():
                getter = asyncio.create_task(events.get())
                done, _ = await asyncio.wait({getter, job}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                else:
                    getter.cancel()
            result = job.result()
        except subtitles.SubtitleError as e:
            await usage_log.record(principal, f"whisper-{subtitles.MODEL}", "whisper", "subtitles", True, 503,
                                   None, (time.monotonic() - started) * 1000, str(e))
            yield _error(str(e))
            return
        finally:
            if not job.done():
                job.cancel()
        await usage_log.record(principal, f"whisper-{subtitles.MODEL}", "whisper", "subtitles", True, 200,
                               None, (time.monotonic() - started) * 1000)

        base = re.sub(r"[^\w.-]+", "-", result["title"]).strip("-")[:80] or video
        suffix = "-en" if payload.translate else ""
        async with session_factory()() as db:
            files = []
            for extension, mime, text in (("srt", "application/x-subrip", subtitles.srt(result["segments"])),
                                          ("vtt", "text/vtt", subtitles.vtt(result["segments"]))):
                stored = ChatFile(user_id=user_id, kind="subtitles", mime_type=mime,
                                  name=f"{base}{suffix}.{extension}", prompt=None, data=text.encode())
                db.add(stored)
                files.append(stored)
            await db.commit()
            facts = {key: result[key] for key in ("title", "channel", "url", "duration", "language",
                                                  "language_sure", "translated", "model", "seconds")}
            facts.update(video=video, segments=len(result["segments"]),
                         files={"srt": files[0].id, "vtt": files[1].id})
            if payload.conversation_id is not None:
                conversation = await db.get(Conversation, payload.conversation_id)
                if conversation is not None and conversation.user_id == user_id:
                    filed = {"subtitles": facts}
                    if payload.answers_to is not None:
                        filed["answers_to"] = payload.answers_to
                    db.add(ConversationMessage(conversation_id=conversation.id, role="assistant",
                                               content=subtitles.transcript(result),
                                               stats=json.dumps({"total": result["seconds"]}),
                                               attachments=json.dumps(filed)))
                    conversation.updated_at = utcnow()
                    await db.commit()
        yield _event("subtitles", **facts, text=subtitles.transcript(result), saved=True)

    # One picture or transcription at a time per person (app/limits.py); the slot is given back
    # when the stream ends, however it ends
    slot = await limits.take_gpu_slot(principal)
    if payload.conversation_id is not None:
        job = jobs.start(user.id, payload.conversation_id, "subtitles", payload.message[:120], limits.holding(slot, stream()))
        return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return StreamingResponse(limits.holding(slot, stream()), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


CHOOSER_DAYS = 30      # how far back the chooser looks at a model's record
NEAR_LIMIT = 30_000    # metres: beyond this, a result is not an answer to "near me"


async def _choose_model(principal, session, category, has_images, prefer=None):
    """The best model this user may use for this kind of work, with the reason it was picked."""
    available = await backends.available_models()
    health = await usage_log.by_model(session, utcnow() - timedelta(days=CHOOSER_DAYS))
    judged = await feedback.by_model(session, utcnow() - timedelta(days=CHOOSER_DAYS),
                                     principal.user.id if principal.user else None)
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
            "blocked": supervisor.blocked_by_busy_gpu(name, backend),
            "feedback": judged.get(name),
        })
    return chooser.pick(candidates, category, needs_vision=has_images, prefer=prefer)




# --- maps -----------------------------------------------------------------------------------

async def _from_the_web(principal, kind, centre):
    """Places of a kind near somewhere, found by searching the web and reading what it says.

    For the things OpenStreetMap does not have. Every candidate still has to survive the geocoder
    before it is shown, so what comes back is at worst a real place in the wrong order, never an
    address a model made up.
    """
    if not (web_search.is_available() and intent_router.is_enabled()):
        return []
    area = (centre.get("address") or centre.get("name") or "").split(",")
    area = ", ".join(part.strip() for part in area[:3] if part.strip())
    query = map_web.build_query(kind, area)
    try:
        results = await web_search.search([query], limit=map_web.MAX_PAGES + 3)
        # A contact page is short by nature, and short is exactly where an address lives
        pages = await web_search.fetch_pages(results, least=60,
                                             wanted=map_web.MAX_PAGES) if results else []
    except web_search.SearchError as e:
        log.info("map web search failed (%s)", e)
        return []
    readable = [{"url": p.get("url", ""), "text": p.get("content") or ""}
                for p in pages if p.get("content")]
    if not readable:
        # Nothing was read, so there is nothing to extract from. Asking anyway is asking a model to
        # make something up, and it will.
        log.info("map web search read no pages for %r", query)
        return []
    answer = await _ask_helper(principal, map_web.build_request(settings.router_model(), kind,
                                                                area, readable))
    candidates = map_web.verify(map_web.read_answer(answer), readable)
    if not candidates:
        return []
    return await map_web.locate(candidates, (centre["lat"], centre["lon"]), places.active())


class _Found(Exception):
    """Not a failure: the answer was reached down a different path and the rest is not needed."""


class _NeedsYou(maps.MapError):
    """Answerable only with the coordinate of whoever is asking."""

    def __init__(self):
        super().__init__("This needs to know where you are starting from. Allow this page to use "
                         "your location, or say where you are setting off from.")


class _NotOnTheMap(maps.MapError):
    """A place that does not exist is a bad request, not a broken upstream: 503 is for the
    services falling over. Never 502 for either: the Cloudflare tunnel in front of this gateway
    replaces a 502 with a page of its own before the reason ever reaches the browser."""


class MapIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: int | None = None
    # The last thing asked, so "find only the masjids" keeps the "around me" of the question before
    earlier: str = Field(default="", max_length=2000)
    # Where the browser says the person is, when they allowed it to say
    here_lat: float | None = Field(default=None, ge=-90, le=90)
    here_lon: float | None = Field(default=None, ge=-180, le=180)


async def _candidates(words, fallback_points, index, near=None, atlas=maps):
    """Everywhere a name might mean, or the one place a coordinate already is."""
    if index < len(fallback_points):
        lat, lon = fallback_points[index]
        try:
            return [await atlas.reverse(lat, lon)]
        except maps.MapError:
            return [{"name": f"{lat:.5f}, {lon:.5f}", "address": "", "lat": lat, "lon": lon,
                     "kind": "", "osm": None}]
    if not words:
        return []
    found = await atlas.find(words, near=near, limit=5)
    if not found:
        raise _NotOnTheMap(f"Nowhere called \u201c{words}\u201d could be found on the map. "
                           "Try the name as it appears locally, or add the city.")
    return found


async def _somewhere(words, fallback_points, index, atlas=maps):
    found = await _candidates(words, fallback_points, index, atlas=atlas)
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

    atlas = places.active()             # OpenStreetMap, or Google when a key is set
    message = payload.message.strip()
    # Read together for anything the follow-up left out, kept apart for anything it named
    context = f"{payload.earlier.strip()} {message}".strip() if payload.earlier.strip() else message
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

    # "I want to go to X" names one place, and the small model is as likely to file it under from
    # as under to. One place named on a journey is where the journey ends; where it begins is here.
    if plan["intent"] in ("route", "along") and plan["from"] and not plan["to"]:
        plan["from"], plan["to"] = "", plan["from"]

    # A journey dressed up in preferences - "the easiest and fastest route to go to X, I prefer
    # less walking" - reads to the small model as one long place name, which matches nothing. When
    # the sentence plainly asks to get somewhere, the destination is taken from the sentence.
    towards = map_plan.destination_in(message)
    if towards and plan["intent"] in ("find", "near") and not points:
        plan = {**plan, "intent": "route", "to": towards, "from": "", "what": ""}
    elif towards and plan["intent"] in ("route", "along") and not plan["to"]:
        plan["to"] = towards

    about_me = map_plan.wants_my_location(context, plan)
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

    tag = maps.amenity_for(plan["what"] or message) or maps.amenity_for(context)
    mode = maps.transport_for(message)
    result = {"intent": plan["intent"], "asked": message, "what": plan["what"],
              "places": [], "route": None, "start": None, "end": None, "mode": mode,
              "provider": places.name(), "tiles": places.tiles(),
              "from_your_location": bool(about_me and standing)}

    try:
        if plan["intent"] == "here":
            if not points:
                raise _NotOnTheMap("No coordinate was given to look up.")
            result["places"] = [await atlas.reverse(*points[0])]

        elif plan["intent"] in ("route", "along"):
            # A destination with no starting point means from where the person is, which is what
            # every map application does and what "I want to go to X" plainly means
            if not plan["from"] and not points:
                if not standing:
                    raise _NeedsYou()
                points = [standing]
                result["from_your_location"] = True
            # Both ends are resolved together, so that two vague names settle on the pairing
            # that makes a journey rather than a continent
            start, end = maps.nearest_pair(
                await _candidates(plan["from"], points, 0, atlas=atlas),
                await _candidates(plan["to"], points, 1 if plan["from"] or points else 0,
                                  near=standing, atlas=atlas))
            if not start or not end:
                raise _NotOnTheMap("A route needs both a start and a finish; name them as "
                                    "\u201cfrom A to B\u201d.")
            result["start"], result["end"] = start, end
            result["route"] = await atlas.route([(start["lat"], start["lon"]),
                                                (end["lat"], end["lon"])], mode)
            if plan["intent"] == "along":
                if not tag:
                    raise _NotOnTheMap("What should be looked for along the way? Name a kind of "
                                        "place, such as petrol stations or restaurants.")
                result["places"] = await atlas.along(tag, result["route"]["line"])

        elif plan["intent"] == "near":
            # "Masjids near me" names a kind of place, not a place. Looking up the word Masjid
            # finds a railway station in Mumbai, which is how a question asked in Tokyo came back
            # with Maharashtra. A kind of place is what to look for, never where to look.
            around = plan["to"] or plan["what"]
            if around and maps.amenity_for(around):
                around = ""
            if not around and not points:
                if not standing:
                    raise _NeedsYou()
                points = [standing]
                result["from_your_location"] = True
            centre = await _somewhere(around, points, 0, atlas)
            if not centre:
                raise _NotOnTheMap("Near where? Name a place, or allow this page to use your "
                                   "location.")
            result["start"] = centre
            if not tag:
                # No tag for this kind of place, so the map is asked what near here is called
                # something like it - and only if that finds nothing is the whole world searched
                wanted = plan["what"] or message
                result["places"] = await atlas.named_nearby(wanted, centre["lat"], centre["lon"])
                if not result["places"]:
                    # Nominatim searches the planet and only leans towards a box, so asking it for
                    # "a coworking space near me" returns one five thousand kilometres away. Near
                    # means near: anything beyond a long day's drive is not an answer to this
                    # question, and no answer is better than that one.
                    found = await atlas.find(wanted, near=(centre["lat"], centre["lon"]))
                    for place in found:
                        place["metres_away"] = round(maps.distance(centre["lat"], centre["lon"],
                                                                   place["lat"], place["lon"]))
                    result["places"] = sorted((p for p in found if p["metres_away"] <= NEAR_LIMIT),
                                              key=lambda p: p["metres_away"])
                if not result["places"]:
                    raise _NotOnTheMap(
                        f"Nothing within {NEAR_LIMIT // 1000} km of there is called anything like "
                        f"“{maps.meaningful(wanted) or wanted}”. The map knows places by "
                        "name and by kind - try the kind of place, or the name as it is written "
                        "locally.")
            else:
                try:
                    result["places"] = await atlas.nearby(tag, centre["lat"], centre["lon"],
                                                         words=plan["what"] or message)
                except maps.MapError as e:
                    # Overpass refusing is not the end of the question. The web path below needs
                    # nothing from it, so a failed map search becomes a thin one and the search
                    # carries on somewhere else rather than becoming a page with an error on it.
                    log.info("map search unavailable (%s); trying the web instead", e)
                    result["places"] = []
                    result["map_search_failed"] = True
            # The web is asked every time, not only when the map is thin. Shinkoiwa Masjid is the
            # first thing a search engine returns for this question and the map does not contain it
            # at all, so a full list from the map is still a list with the nearest one missing.
            extra = await _from_the_web(principal, plan["what"] or message, centre)
            if extra:
                # Two sources for one street corner: anything within a hundred metres of a place
                # the map already gave is the same building under a different name
                for place in extra:
                    same = any(maps.distance(place["lat"], place["lon"], p["lat"], p["lon"]) < 100
                               for p in result["places"])
                    if not same:
                        result["places"].append(place)
            result["searched_the_web"] = bool(extra)

        else:                                   # find
            # Asked to find a kind of place with somewhere to be, that is a search around here
            if tag and (points or standing):
                centre = await _somewhere("", points or [standing], 0, atlas)
                result["start"] = centre
                result["from_your_location"] = not points
                result["intent"] = "near"
                result["places"] = await atlas.nearby(tag, centre["lat"], centre["lon"],
                                                     words=plan["what"] or message)
                raise _Found()
            # find, not search: a name that does not resolve whole is tried in parts
            result["places"] = await atlas.find(plan["what"] or message,
                                               near=(points[0] if points else standing))
            if not result["places"]:
                raise _NotOnTheMap(
                    "Nothing on the map matched that. Try naming the place on its own - a "
                    "station, a district, a landmark - rather than in a sentence.")
    except _Found:
        pass
    except _NeedsYou as e:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, str(e))
    except _NotOnTheMap as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except maps.MapError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))

    # Somewhere to travel from means every answer can say what the journey actually costs, which
    # is the difference between "three kilometres away" and "twenty minutes round a river"
    origin = result["start"] if result["intent"] in ("near", "find") else None
    if origin and result["places"]:
        await atlas.travel_times((origin["lat"], origin["lon"]), result["places"], mode)
        result["places"].sort(key=lambda p: (p.get("road_minutes") is None,
                                             p.get("road_minutes", 0), p["metres_away"]))

    if not result["places"] and not result["route"]:
        # Past the try above, so this is raised as the answer it is rather than through it
        if result.get("map_search_failed"):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "The map's place search is not answering at the moment, and searching the web for "
                "it found nothing either. These are busy free services; a minute usually fixes it.")
        widest = maps.SEARCH_RADIUS // 1000
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Nothing of that kind is mapped within {widest} km of there. OpenStreetMap only "
            "knows what somebody has added to it, so a quiet area can genuinely be empty - try a "
            "wider kind of place, or name a town to search around.")

    centre = result["start"] if result["intent"] in ("near", "find") else None
    for place in result["places"]:
        place["google"] = maps.google_link(place=place)
        # "and their directions": every place carries the way to it from wherever the search began
        if centre:
            place["directions"] = maps.google_link(start=(centre["lat"], centre["lon"]),
                                                   end=(place["lat"], place["lon"]), mode=mode)
    if result["places"] and result["places"][0].get("searched_within_m"):
        result["searched_within_km"] = round(result["places"][0]["searched_within_m"] / 1000)
    if result["route"]:
        result["google_route"] = maps.google_link(
            start=(result["start"]["lat"], result["start"]["lon"]),
            end=(result["end"]["lat"], result["end"]["lon"]),
            mode=result["route"].get("asked_for") or mode)
    everything = result["places"] + [p for p in (result["start"], result["end"]) if p]
    result["bounds"] = maps.bounds(everything, (result["route"] or {}).get("line", []))
    await usage_log.record(principal, "openstreetmap", "maps", "map", False, 200, None, 0)
    return result


class AttachmentIn(BaseModel):
    kind: str = Field(default="picture", pattern="^(picture|document)$")
    name: str = Field(default="", max_length=200)


class RouteIn(BaseModel):
    message: str = ""
    has_images: bool = False
    # Everything attached, pictures and documents, by kind and file name: "design a logo from this"
    # means one thing with a brief attached and another with a photograph
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=20)
    web_search: bool = False
    image_generation: bool = False
    maps: bool = False
    # Off in the playground means a photograph is never given a blurred background, whatever is said
    photo_blur: bool = True
    history: list[ChatMessageIn] = Field(default_factory=list)
    # The model the conversation is already using: it keeps the job when nothing scores better,
    # because swapping model reloads weights and throws away a warm cache
    current_model: str = ""
    choose_model: bool = False


@router.post("/route")
async def route(payload: RouteIn, principal: Principal = Depends(authenticate),
                session: AsyncSession = Depends(get_session)):
    """What this message asks for: which tool, what kind of work, and which model should do it.

    Normally one question: the small model describes the request from everything that came with it
    (app/tools/understand.py), and the tool and the kind of work follow from that description. With
    router.understand off, or when that answer cannot be read, the older questions below decide.

    The two older questions go to the small model at the same time. They are independent, so asking them
    together costs one round trip rather than two, and keeping them in separate prompts keeps the
    action answer as good as it was - putting both in one prompt made it worse.
    """
    tools = {
        "web_search": payload.web_search and web_search.is_available(),
        "image_generation": (payload.image_generation and images.is_available()
                             and access.can_generate_images(principal)),
        "maps": payload.maps and settings.feature_enabled("maps"),
        # Without the depth model the blur endpoint refuses, so it is no choice to offer either
        "photo_blur": (payload.photo_blur and payload.has_images
                       and await asyncio.to_thread(photo.is_depth_available)),
    }
    allowed = intent_router.candidates(tools, payload.has_images)
    history = intent_router.plain_history(
        [{"role": m.role, "content": m.content} for m in payload.history])
    asking_model = intent_router.is_enabled()

    if asking_model and settings.get_bool("router.understand", "ROUTER_UNDERSTAND", True):
        attachments = [a.model_dump() for a in payload.attachments] or (
            [{"kind": "picture", "name": ""}] if payload.has_images else [])

        async def second_opinion():
            """The one-word tool question, asked at the same time, so it costs no extra wait."""
            if len(allowed) == 1:
                return None
            found = intent_router.clean_answer(await _ask_helper(
                principal, intent_router.build_request(payload.message, allowed, history)), allowed)
            return intent_router.settle_action(found, payload.message, allowed, payload.has_images)[0]

        answer, other = await asyncio.gather(
            _ask_helper(principal, understand.build_request(payload.message, attachments, history)),
            second_opinion())
        reading = understand.read_answer(answer)
        if reading is not None:
            only = len(allowed) == 1
            action = allowed[0] if only else understand.decide(reading, allowed, payload.has_images,
                                                               payload.message, other)
            decision = {"action": action,
                        "decided_by": "the only tool switched on" if only else settings.router_model(),
                        "understood": reading["request"]}
            if payload.choose_model:
                return await _with_model(principal, session, decision,
                                         understand.category(reading, payload.has_images),
                                         settings.router_model(), payload)
            return decision

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

    async def look_or_change():
        """With a picture attached, the one question the seven-way choice keeps getting wrong.

        Asked on its own, at the same time as the others, so it costs no extra wait: the three
        calls go out together and the answer arrives with the rest.
        """
        changes = (intent_router.EDIT, intent_router.CLEAN, intent_router.BLUR,
                   intent_router.BACKDROP)
        if not (payload.has_images and asking_model and any(a in allowed for a in changes)):
            return None
        answer = await _ask_helper(principal,
                                   intent_router.build_picture_request(payload.message, history))
        return intent_router.read_picture_answer(answer)

    (action, decided_by), (category, category_by), wanted = await asyncio.gather(
        decide_action(), decide_category(), look_or_change())
    action, decided_by = intent_router.settle_picture(action, decided_by, wanted, payload.message,
                                                      allowed)
    decision = {"action": action, "decided_by": decided_by}
    if category is None:
        return decision
    return await _with_model(principal, session, decision, category, category_by, payload)


async def _with_model(principal, session, decision, category, category_by, payload):
    """The decision, with the model to answer it when Auto is choosing."""
    action = decision["action"]
    decision.update(category=category, category_by=category_by)
    # Only a text answer is the model's to choose: a picture is drawn by the image model, and a
    # photograph is worked on by tools that have nothing to do with the drop-down
    if action in (intent_router.CHAT, intent_router.SEARCH):
        picked = await _choose_model(principal, session, category, payload.has_images,
                                     prefer=payload.current_model or None)
        if picked:
            decision.update(model=picked["model"], model_why=picked["why"],
                            considered=picked["considered"], passed_over=picked.get("passed_over"))
    return decision


async def _drawable_prompt(principal, session, user, request, conversation_id, documents=()):
    """What to draw, the wording it must spell, and the rewrite to show, if there was one.

    `documents` are (name, text) of files attached to the request - "read this and design a logo
    for it" - and are what the prompt is written from, so they come after the conversation.
    """
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
    for name, text in documents:
        history.append({"role": "user", "content": f"Attached document {name}:\n{text[:DOCUMENT_CHARS_FOR_IMAGES]}"})
    # Both questions at once: what to draw, and which words have to be spelled right in it
    described, worded = await asyncio.gather(
        _ask_helper(principal, image_prompt.build_request(request, history, made)),
        _ask_helper(principal, image_prompt.wording_request(request, history)))
    drawn, quoted = image_prompt.clean(described, request)
    wording = image_prompt.clean_wording(worded) or quoted
    drawn = image_prompt.with_wording(drawn, wording)
    return drawn, wording, (drawn if drawn != request else None)


DOCUMENT_CHARS_FOR_IMAGES = 6000     # of each attached document, for writing an image prompt


class ImageIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None
    size: str = "square"
    source_file_id: int | None = None      # the picture to edit
    reference_file_ids: list[int] = Field(default_factory=list, max_length=2)  # extra pictures it may use
    document_file_ids: list[int] = Field(default_factory=list, max_length=4)   # documents to draw from
    strength: float = Field(default=0.75, ge=0.05, le=1.0)
    answers_to: int | None = None          # the question this answers, for asking it again


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
        await _writable(session, payload.conversation_id, user)
    sources = []
    if payload.source_file_id is not None:
        wanted = [payload.source_file_id, *payload.reference_file_ids]
        found = await _load_files(session, user, wanted)
        if any(i not in found for i in wanted):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Source image not found")
        sources = [(found[i].data, found[i].mime_type) for i in wanted]
    elif payload.reference_file_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Reference images need an image to edit")
    documents = []
    if payload.document_file_ids:
        found = await _load_files(session, user, payload.document_file_ids)
        documents = [(found[i].name, found[i].prompt or "") for i in payload.document_file_ids
                     if i in found and found[i].kind == "document"]
    user_id = user.id
    # An edit is already an instruction about a picture that exists, so it is passed through as written
    drawable, wording, rewritten = ((payload.prompt, None, None) if sources else
                                    await _drawable_prompt(principal, session, user, payload.prompt,
                                                           payload.conversation_id, documents))

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
            await usage_log.record(principal, settings.image_model_name(), "comfyui", endpoint, True, 503,
                                   None, (time.monotonic() - started) * 1000, str(e))
            yield _error(str(e))
            return
        finally:
            if not job.done():
                job.cancel()  # the browser went away; generate() stops the job on the image server
        await usage_log.record(principal, settings.image_model_name(), "comfyui", endpoint, True, 200,
                               None, (time.monotonic() - started) * 1000)
        # Recorded with the picture, so that "which model made this?" has a true answer later
        engine = await images.edit_engine()
        made_by = turn_facts.image_maker(engine, bool(sources),
                                         bool(wording) and settings.fix_image_text() and engine == "qwen")
        async with session_factory()() as db:
            seconds = round(time.monotonic() - started, 1)
            stored = await _save_result(db, user_id, payload.conversation_id, png,
                                        f"image-{seed}.png", drawable,
                                        {"seed": seed, "size": payload.size,
                                         "asked": payload.prompt,
                                         "source_file_id": payload.source_file_id,
                                         "made_by": made_by},
                                        {"total": seconds}, answers_to=payload.answers_to,
                                        model=settings.image_model_name())
            yield _event("image", file=_file_json(stored), seed=seed, saved=True, seconds=seconds)

    # One picture or transcription at a time per person (app/limits.py); the slot is given back
    # when the stream ends, however it ends
    slot = await limits.take_gpu_slot(principal)
    if payload.conversation_id is not None:
        job = jobs.start(user.id, payload.conversation_id, "image", (payload.prompt or "")[:120], limits.holding(slot, stream()))
        return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return StreamingResponse(limits.holding(slot, stream()), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- video stories ------------------------------------------------------------------------------

class VideoIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=video_story.STORY_CHARS)     # a whole story, given to tell
    conversation_id: int | None = None
    file_ids: list[int] = Field(default_factory=list, max_length=6)     # pictures and documents as material
    languages: list[str] = Field(default_factory=lambda: ["en", "bn"])
    answers_to: int | None = None
    # Prepare the scenes as text to read and correct first, and draw and voice nothing until the
    # person asks for the video (POST /chat/video/{message}/produce). Needs a conversation to keep it.
    review: bool = True


async def _video_facts(prompt):
    """(text for the storyboard, [{title, url}]) from a web search; nothing when search is off or empty."""
    if not web_search.is_available():
        return "", []
    try:
        results = (await web_search.search([prompt]))[:8]
    except Exception as e:       # a story can still be told from the material given
        log.info("video story search failed: %s", e)
        return "", []
    text = "\n\n".join(f"[{i}] {r.get('title', '')}\nURL: {r.get('url', '')}\n{(r.get('content') or r.get('snippet') or '')[:1500]}"
                       for i, r in enumerate(results, 1))
    return text, [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results]


@router.post("/video")
async def make_video(payload: VideoIn, principal: Principal = Depends(authenticate),
                     session: AsyncSession = Depends(get_session)):
    """A factual story as a narrated, illustrated video, in English and Bangla (app/tools/video_story.py).
    Streams progress - the facts, the story, each scene drawn and checked, the narration - then the
    videos, saved to the conversation."""
    user = _require_user(principal)
    if not images.is_available() or not video_story.is_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Video stories are not set up on this server")
    if not access.can_generate_images(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to image generation")
    # What the person wrote before in this conversation: "the story above" points at one of these.
    # Sent alone, such a request was searched for as it stood, and made a video of a news item.
    earlier = []
    if payload.conversation_id is not None:
        conversation = await _writable(session, payload.conversation_id, user)
        rows = (await session.execute(select(ConversationMessage.content).where(
            ConversationMessage.conversation_id == conversation.id, ConversationMessage.role == "user")
            .order_by(ConversationMessage.id.desc()).limit(6))).scalars().all()
        earlier = list(dict.fromkeys(text[:video_story.STORY_CHARS] for text in reversed(rows)
                                     if text and text.strip() != payload.prompt.strip()))[-5:]
    languages = list(dict.fromkeys(lang for lang in payload.languages if lang in video_story.LANGUAGES)) or ["en"]
    found = await _load_files(session, user, payload.file_ids)
    pictures = [(f.data, f.mime_type) for f in found.values() if f.kind != "document" and (f.mime_type or "").startswith("image/")]
    material = "\n\n".join(f"{f.name}:\n{(f.prompt or '')[:6000]}" for f in found.values() if f.kind == "document")
    ctx = {"prompt": payload.prompt, "languages": languages, "material": material, "pictures": pictures,
           "earlier": earlier, "conversation_id": payload.conversation_id, "answers_to": payload.answers_to,
           "review": payload.review and payload.conversation_id is not None}
    slot = await limits.take_gpu_slot(principal)
    work = limits.holding(slot, _one_video_at_a_time(_video_pipeline(principal, user.id, ctx)))
    if payload.conversation_id is not None:
        job = jobs.start(user.id, payload.conversation_id, "video", payload.prompt[:120], work)
        return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                          "X-Job-Id": str(job.id)})
    return StreamingResponse(work, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/video/{message_id}/continue")
async def continue_video(message_id: int, principal: Principal = Depends(authenticate),
                         session: AsyncSession = Depends(get_session)):
    """Carries on with a video story that stopped partway - an error, a restart, the server going to
    sleep, Stop - from what it had saved: the plan, every picture that passed, every video made."""
    user = _require_user(principal)
    if not images.is_available() or not video_story.is_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Video stories are not set up on this server")
    message = (await session.execute(
        select(ConversationMessage).join(Conversation).where(
            ConversationMessage.id == message_id, Conversation.user_id == user.id))).scalar_one_or_none()
    video = (json.loads(message.attachments or "{}") if message else {}).get("video")
    if not video:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video story not found")
    if video.get("state", "done") == "done":
        raise HTTPException(status.HTTP_409_CONFLICT, "This video is already finished")
    if video.get("state") == "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "This story is waiting to be read; make its video from the Videos page")
    running = jobs.running_for(user.id, message.conversation_id)
    if running is not None and running.kind == "video":
        raise HTTPException(status.HTTP_409_CONFLICT, "This video is still being made")
    ctx = {"prompt": video.get("asked", ""), "languages": video.get("languages") or ["en", "bn"],
           "conversation_id": message.conversation_id, "message_id": message.id, "video": video}
    slot = await limits.take_gpu_slot(principal)
    job = jobs.start(user.id, message.conversation_id, "video", "continuing: " + video.get("title", "")[:100],
                     limits.holding(slot, _one_video_at_a_time(_video_pipeline(principal, user.id, ctx))))
    return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Job-Id": str(job.id)})


class SceneEdit(BaseModel):
    index: int = Field(ge=0)
    text: dict[str, str] = Field(default_factory=dict)          # language -> the scene's new words
    retranslate: bool = False      # the scene's other languages translated again from the one edited
    redraw: bool = False
    picture: str | None = Field(default=None, max_length=1500)  # what to draw instead; redraws the scene
    # A picture already drawn for it and looked at (redraw_picture): put in as it is, nothing drawn
    picture_file: int | None = None
    people: str | None = Field(default=None, pattern="^(none|from_behind)$")   # who that picture may show


class VideoEditIn(BaseModel):
    scenes: list[SceneEdit] = Field(default_factory=list, max_length=video_story.GIVEN_MAX_SCENES)
    # Languages to add: the story translated, voiced and made into a video in each
    add_languages: list[str] = Field(default_factory=list, max_length=len(video_story.LANGUAGES))
    # Scenes to take out (their numbers as they are now, from 0); every language is made again from
    # the pictures and narration kept, nothing drawn
    delete: list[int] = Field(default_factory=list, max_length=video_story.GIVEN_MAX_SCENES)


# Everything kept for each scene, in scene order: taken out together when a scene is
PER_SCENE = ("pictures", "drawn", "people", "parts", "notes") + tuple(video_story.LANGUAGES)


def _set_line(story, lang, i, text):
    """A scene's whole line in a language changed, and its parts with it: a scene of one part takes the
    new words; the parts of a scene of several voices no longer fit them, and it becomes the narrator's."""
    story[lang][i] = text
    _forget_voice(story, lang, i)
    parts = story.get("parts")
    if not parts or i >= len(parts):
        return
    if len(parts[i]) == 1:
        parts[i][0].setdefault("text", {})[lang] = text
    else:
        parts[i] = [{"speaker": "narrator",
                     "text": {code: story[code][i] for code in video_story.LANGUAGES if i < len(story.get(code) or [])}}]


def _scene_parts_in(story, lang, i):
    """A scene's parts in one language, as the narrator is given them: {speaker, text} in order. A
    scene with no parts - or none with words in this language - is the narrator's line alone."""
    parts = story.get("parts")
    if parts and i < len(parts) and parts[i]:
        out = []
        for p in parts[i]:
            t = p.get("text")
            text = (t or {}).get(lang, "") if isinstance(t, dict) else (t or "")
            if text:
                out.append({"speaker": p.get("speaker") or "narrator", "text": text})
        if out:
            return out
    return [{"speaker": "narrator", "text": (story.get(lang) or [""])[i]}]


def _drop_scene(story, i):
    for key in PER_SCENE:
        if isinstance(story.get(key), list) and i < len(story[key]):
            del story[key][i]
    for lang in list((story.get("narration") or {})):
        if i < len(story["narration"][lang]):
            del story["narration"][lang][i]
    for key in [k for k in story if k.startswith("voice_")]:
        if isinstance(story[key], list) and i < len(story[key]):
            del story[key][i]
    if story.get("pending"):
        story["pending"] = {str(int(k) - (int(k) > i)): v for k, v in story["pending"].items() if int(k) != i}


def _forget_voice(story, lang, i):
    """A line's narration and the person's reading of it no longer fit its changed words."""
    entries = (story.get("narration") or {}).get(lang)
    if entries and i < len(entries):
        entries[i] = None
    readings = story.get(f"voice_{lang}")
    if readings and i < len(readings):
        readings[i] = None


async def _owned_video(session, user, message_id):
    message = (await session.execute(
        select(ConversationMessage).join(Conversation).where(
            ConversationMessage.id == message_id, Conversation.user_id == user.id))).scalar_one_or_none()
    video = (json.loads(message.attachments or "{}") if message else {}).get("video")
    if not video:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video story not found")
    return message, video


@router.post("/video/{message_id}/edit")
async def edit_video(message_id: int, payload: VideoEditIn, principal: Principal = Depends(authenticate),
                     session: AsyncSession = Depends(get_session)):
    """Changes some scenes of a video story and makes again only what they change: a scene's words in
    a language (re-voiced, that language's video remade), its other languages translated again, or its
    picture drawn again (every language's video remade). Every other picture, line of narration and
    finished scene clip is used as it is, and the videos replaced are kept as earlier versions."""
    user = _require_user(principal)
    message, video = await _owned_video(session, user, message_id)
    story = video.get("story")
    if not story or not story.get("pictures"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This video was made before stories were kept; make it again")
    running = jobs.running_for(user.id, message.conversation_id)
    if running is not None and running.kind == "video":
        raise HTTPException(status.HTTP_409_CONFLICT, "This video is still being made")
    languages = video.get("languages") or [lang for lang in ("en", "bn") if lang in story]
    count = len(story["pictures"])
    remake, translate = set(), []
    for change in payload.scenes:
        i = change.index
        if i >= count:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"This story has {count} scenes")
        edited = None
        for lang, text in change.text.items():
            text = " ".join(text.split())
            if lang not in languages or not text or text == story[lang][i]:
                continue
            _set_line(story, lang, i, text)
            remake.add(lang)
            edited = lang
        if change.retranslate and edited:
            for other in languages:
                if other != edited:
                    translate.append((i, edited, other))
                    remake.add(other)
        if change.picture and change.picture.strip():
            story["drawn"][i] = change.picture.strip()
        if change.picture_file:
            drawn = await _load_files(session, user, [change.picture_file])
            if change.picture_file not in drawn or drawn[change.picture_file].kind != "video-scene":
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"The new picture for scene {i + 1} is gone; draw it again")
            story["pictures"][i] = change.picture_file
            (story.get("pending") or {}).pop(str(i), None)
            if change.people and isinstance(story.get("people"), list) and i < len(story["people"]):
                story["people"][i] = change.people
            remake.update(languages)
        elif change.redraw or (change.picture and change.picture.strip()):
            story["pictures"][i] = None
            remake.update(languages)
    for lang in payload.add_languages:
        if lang not in video_story.LANGUAGES:
            continue
        if lang not in languages:                   # a new language: translated, then its video made
            languages.append(lang)
        elif lang in (video.get("videos") or {}):
            continue
        remake.add(lang)                            # (told in it already: only its video is made)
        if video.get("make") is not None and lang not in video["make"]:
            video["make"].append(lang)
    # Scenes taken out last, so the numbers above are the ones the person saw
    gone = sorted({i for i in payload.delete if 0 <= i < count}, reverse=True)
    if len(gone) >= count:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A video needs at least one scene")
    for i in gone:
        _drop_scene(story, i)
    if gone:
        video["scenes"] = len(story["pictures"])
        translate = [(i - sum(1 for g in gone if g < i), source, target) for i, source, target in translate
                     if i not in gone]
        remake.update(languages)
    video["languages"] = languages
    # Lines and voices saved since a language's video was made: it is made again with them
    remake.update(lang for lang in video.get("behind") or [] if lang in languages)
    if not remake:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing was changed")
    for lang in remake:
        _keep_version(video, lang)
    if any(picture is None for picture in story["pictures"]):
        video["drawing"] = {"attempt": 0, "wrong": {}, "keep_every_scene": True}
    else:
        video["drawing"] = {"done": True}
    if translate:
        video["translate"] = translate
    video["state"] = "paused"
    video.pop("paused", None)
    await _save_video(message.id, video)
    ctx = {"prompt": video.get("asked", ""), "languages": languages,
           "conversation_id": message.conversation_id, "message_id": message.id, "video": video}
    slot = await limits.take_gpu_slot(principal)
    job = jobs.start(user.id, message.conversation_id, "video", "editing: " + video.get("title", "")[:100],
                     limits.holding(slot, _one_video_at_a_time(_video_pipeline(principal, user.id, ctx))))
    return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Job-Id": str(job.id)})


class RedrawIn(BaseModel):
    index: int = Field(ge=0)
    # How the person wants the picture: a whole description or only what should be different
    request: str = Field(min_length=1, max_length=1500)


class SaveIn(BaseModel):
    lang: str = Field(pattern="^[a-z]{2}$")
    index: int = Field(ge=0)
    text: str = Field(min_length=1)


class StoreVoiceIn(BaseModel):
    # A recording (base64, as the browser recorded it) for one scene
    recording: str = Field(min_length=100)
    lang: str = Field(pattern="^[a-z]{2}$")
    index: int = Field(ge=0)


# One story's saved state is changed by one request at a time: read, changed and written together
_story_locks: dict[int, asyncio.Lock] = {}


async def _change_story(message_id, user_id, change):
    """`change(video)` applied to the video story as it is saved now - read under a lock and its row
    locked, written straight after - so two saves at once each keep the other's change: a line saved
    while another scene's is, or while a voiceover is being cleaned. What `change` returns; it may
    raise an HTTPException, and then nothing is written."""
    async with _story_locks.setdefault(message_id, asyncio.Lock()):
        async with session_factory()() as db:
            row = (await db.execute(
                select(ConversationMessage).join(Conversation).where(
                    ConversationMessage.id == message_id, Conversation.user_id == user_id)
                .with_for_update(of=ConversationMessage))).scalar_one_or_none()
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Video story not found")
            filed = json.loads(row.attachments or "{}")
            video = filed.get("video") or {}
            result = change(video)
            filed["video"] = video
            row.attachments = json.dumps(filed)
            await db.commit()
            return result


def _scene_line(video, lang, index):
    """The story, and its line in `lang` for scene `index`, or the reason it cannot be changed."""
    story = video.get("story")
    if not story:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This video was made before stories were kept; make it again to change it")
    if lang not in story:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This story has no {video_story.language_name(lang)}")
    count = len(story["pictures"])
    if index >= count:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"This story has {count} scenes (0–{count - 1})")
    return story, story[lang][index]


def _behind(video, lang):
    """A language whose video no longer says what is saved - a line or a voice saved since it was
    made - kept on the story, so "Save and remake" makes it again with nothing else changed."""
    if lang in (video.get("videos") or {}):
        behind = video.setdefault("behind", [])
        if lang not in behind:
            behind.append(lang)


def _not_being_made(user_id, message):
    # Making the video writes the whole story when it is done: a change saved meanwhile would be lost
    running = jobs.running_for(user_id, message.conversation_id)
    if running is not None and running.kind == "video":
        raise HTTPException(status.HTTP_409_CONFLICT, "This video is being made; save when it is done")


@router.post("/video/{message_id}/store-voice")
async def store_voiceover(message_id: int, payload: StoreVoiceIn, principal: Principal = Depends(authenticate),
                          session: AsyncSession = Depends(get_session)):
    """One scene's reading in one language - recorded in the page, or an audio file - cleaned and kept
    as that line's voice, without making the video again: it is used when the video is made again."""
    user = _require_user(principal)
    message, video = await _owned_video(session, user, message_id)
    _not_being_made(user.id, message)
    _story, line = _scene_line(video, payload.lang, payload.index)
    # A line that is only a verse is the qari's recitation, never the person's reading
    if not video_story.needs_reading(line, payload.lang):
        raise HTTPException(status.HTTP_409_CONFLICT, "This scene is a verse: the qari recites it")
    # As the browser sends it, a data: URL; the base64 after its comma is the audio
    encoded = payload.recording.split(",", 1)[1] if payload.recording.startswith("data:") else payload.recording
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The audio did not arrive whole; try again")
    if len(raw) < 100:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The recording is empty or too short")
    folder = video_story.workspace()
    try:
        cleaned, seconds = await video_story.clean_recording(raw, folder, payload.index)
    except video_story.VideoError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"The audio could not be used: {e}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    word = video_story.language_name(payload.lang)
    file_id = await _store_file(user.id, "voice", "audio/mp4", f"scene {payload.index + 1} ({word}).m4a", cleaned)

    def keep_voice(video):
        story, line = _scene_line(video, payload.lang, payload.index)
        if not video_story.needs_reading(line, payload.lang):
            raise HTTPException(status.HTTP_409_CONFLICT, "This scene is a verse: the qari recites it")
        entries = story.setdefault("narration", {}).setdefault(payload.lang, [])
        entries.extend([None] * (len(story["pictures"]) - len(entries)))
        replaced = (entries[payload.index] or {}).get("file_id")
        entries[payload.index] = {"file_id": file_id, "seconds": round(seconds, 2), "by": "recorded"}
        _behind(video, payload.lang)
        return replaced

    try:
        replaced = await _change_story(message_id, user.id, keep_voice)
    except HTTPException:
        await _drop_file(user.id, file_id)
        raise
    if replaced and replaced != file_id:
        await _drop_file(user.id, replaced)
    return {"ok": True, "file_id": file_id, "seconds": round(seconds, 2)}


@router.post("/video/{message_id}/save")
async def save_scene_text(message_id: int, payload: SaveIn, principal: Principal = Depends(authenticate),
                          session: AsyncSession = Depends(get_session)):
    """Save a scene's text in one language without rebuilding the video."""
    user = _require_user(principal)
    message, _video = await _owned_video(session, user, message_id)
    _not_being_made(user.id, message)

    def set_text(video):
        story, line = _scene_line(video, payload.lang, payload.index)
        if " ".join(payload.text.split()) != " ".join((line or "").split()):
            _set_line(story, payload.lang, payload.index, payload.text)
            _behind(video, payload.lang)

    await _change_story(message_id, user.id, set_text)
    return {"ok": True}


@router.post("/video/{message_id}/redraw")
async def redraw_picture(message_id: int, payload: RedrawIn, principal: Principal = Depends(authenticate),
                         session: AsyncSession = Depends(get_session)):
    """One scene's picture drawn again, to look at before the video is made again with it. The story
    model rewrites the scene's description from the person's request (REDRAW_PROMPT: the request
    followed, the rules kept), the picture is drawn from it and checked like every scene's (a face
    turned away once), and kept as a file the edit puts in (SceneEdit.picture_file). Nothing in the
    story changes until then. Events: progress, then "picture" {file_id, ok, why, drawn, people, note}."""
    user = _require_user(principal)
    message, video = await _owned_video(session, user, message_id)
    story = video.get("story") or {}
    pictures = story.get("pictures") or []
    i = payload.index
    if i >= len(pictures):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"This story has {len(pictures)} scenes")
    running = jobs.running_for(user.id, message.conversation_id)
    if running is not None and running.kind == "video":
        raise HTTPException(status.HTTP_409_CONFLICT, "This video is still being made")
    asked = " ".join(payload.request.split())
    current = ((story.get("drawn") or [""] * len(pictures))[i]) or ""
    source = video.get("source") or "en"
    words = (story.get(source) or story.get("en") or [""] * len(pictures))[i] or ""
    story_model = settings.get("video.story.model", "VIDEO_STORY_MODEL")
    check_model = settings.get("video.check.model", "VIDEO_CHECK_MODEL")
    user_id = user.id

    async def stream():
        folder = video_story.workspace()
        queue = asyncio.Queue()

        async def progress(stage, fraction=None):
            await queue.put(_event("progress", stage=f"Drawing scene {i + 1} · {stage}" +
                                   (f" {round(fraction * 100)}%" if fraction is not None else ""),
                                   fraction=round(0.1 + 0.6 * (fraction or 0), 3)))

        async def drawing(work):
            task = asyncio.create_task(work)
            try:
                while not task.done() or not queue.empty():
                    getter = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                    if getter in done:
                        yield getter.result()
                    else:
                        getter.cancel()
                yield task.result()
            finally:
                if not task.done():
                    task.cancel()

        try:
            yield _event("progress", stage="Reading what you asked for", fraction=0.02)
            drawn, people, note = video_story.parse_redraw(await _ask_helper(
                principal, video_story.redraw_request(story_model, words, current, asked)))
            scene = {"picture": drawn, "people": people}
            png, ok, why, turned = None, False, "", False
            for attempt in range(2):
                yield _event("progress", stage=f"Drawing scene {i + 1}" + (" again" if attempt else ""), fraction=0.05)
                if why in video_story.TURNABLE and not turned:
                    turned = True
                    work = images.generate(video_story.TURN_AWAY, video_story.SIZE,
                                           sources=[(png, "image/png")], on_progress=progress)
                else:
                    work = images.generate(video_story.picture_prompt(scene, why=why), video_story.SIZE,
                                           on_progress=progress, story=True)
                async for item in drawing(work):
                    if isinstance(item, bytes):
                        yield item
                    else:
                        png = item[0]
                yield _event("progress", stage=f"Checking scene {i + 1} against the rules", fraction=0.8)
                ok, why = video_story.verdict(
                    await _ask_helper(principal, video_story.check_request(check_model, png, scene)), scene)
                if ok:
                    break
            path = os.path.join(folder, "scene.png")
            with open(path, "wb") as f:
                f.write(png)
            file_id = await _store_file(user_id, "video-scene", "image/jpeg", f"scene {i + 1}.jpg",
                                        video_story.as_jpeg(path))
            yield _event("picture", file_id=file_id, ok=ok, why=why, drawn=drawn, people=people, note=note, index=i)
        except (video_story.VideoError, images.ImageError) as e:
            yield _error(str(e))
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    slot = await limits.take_gpu_slot(principal)
    job = jobs.start(user.id, message.conversation_id, "picture", f"redrawing scene {i + 1}",
                     limits.holding(slot, _one_video_at_a_time(
                         stream(), "Waiting: another picture or video is being made first")))
    return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Job-Id": str(job.id)})


class PendingPictureIn(BaseModel):
    index: int = Field(ge=0)
    file_id: int | None = None          # None: the scene keeps its picture, nothing waits for it
    drawn: str = Field(default="", max_length=3000)
    people: str = Field(default="from_behind", pattern="^(none|from_behind)$")


@router.put("/video/{message_id}/pending-picture")
async def save_pending_picture(message_id: int, payload: PendingPictureIn, principal: Principal = Depends(authenticate),
                               session: AsyncSession = Depends(get_session)):
    """A picture drawn again for a scene (redraw_picture), kept with the story until the video is made
    again with it: it survives the page being closed, and the next edit puts it in. The video itself
    does not change until then."""
    user = _require_user(principal)
    message, video = await _owned_video(session, user, message_id)
    story = video.get("story") or {}
    if payload.index >= len(story.get("pictures") or []):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No such scene")
    running = jobs.running_for(user.id, message.conversation_id)
    if running is not None and running.kind == "video":
        raise HTTPException(status.HTTP_409_CONFLICT, "This video is still being made")
    if payload.file_id is not None:
        found = await _load_files(session, user, [payload.file_id])
        if payload.file_id not in found or found[payload.file_id].kind != "video-scene":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That picture is gone; draw it again")

    def keep_picture(video):
        pending = (video.get("story") or {}).setdefault("pending", {})
        if payload.file_id is None:
            pending.pop(str(payload.index), None)
        else:
            pending[str(payload.index)] = {"file_id": payload.file_id, "drawn": payload.drawn, "people": payload.people}
        return pending

    # Read and written with the story as it is saved now: not over a line saved meanwhile
    return {"pending": await _change_story(message.id, user.id, keep_picture)}


class DraftPart(BaseModel):
    speaker: str = Field(min_length=1, max_length=20)
    text: dict[str, str] = Field(default_factory=dict)         # language -> this part's words


class DraftScene(BaseModel):
    parts: list[DraftPart] = Field(min_length=1, max_length=30)
    picture: str = Field(default="", max_length=1500)          # what to draw
    people: str = Field(default="from_behind", pattern="^(none|from_behind)$")
    # Shown after the scene on a dark screen, not read: references, footnotes
    notes: list[str] = Field(default_factory=list, max_length=20)


class CastMember(BaseModel):
    id: str = Field(min_length=1, max_length=20)
    name: str = Field(default="", max_length=80)
    kind: str
    voice: str | None = Field(default=None, max_length=30)


class DraftIn(BaseModel):
    scenes: list[DraftScene] = Field(min_length=1, max_length=video_story.GIVEN_MAX_SCENES)
    cast: list[CastMember] = Field(min_length=1, max_length=40)


async def _draft_of(session, user, message_id):
    message, video = await _owned_video(session, user, message_id)
    if video.get("state") != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "This story's video is already made; edit its scenes instead")
    running = jobs.running_for(user.id, message.conversation_id)
    if running is not None and running.kind == "video":
        raise HTTPException(status.HTTP_409_CONFLICT, "This story is being prepared right now")
    return message, video


@router.put("/video/{message_id}/draft")
async def save_draft(message_id: int, payload: DraftIn, principal: Principal = Depends(authenticate),
                     session: AsyncSession = Depends(get_session)):
    """The scenes of a story not yet made into a video, as the person has corrected them: their order,
    each part's speaker and words in every language, what each picture should show, and the cast.
    A part with words missing in a language is translated by POST /chat/video/{message}/prepare."""
    user = _require_user(principal)
    message, video = await _draft_of(session, user, message_id)
    story = video["story"]
    cast = [member.model_dump() for member in payload.cast if member.kind in video_story.KINDS]
    if not any(member["id"] == "narrator" for member in cast):
        cast.insert(0, {"id": "narrator", "name": "Narrator", "kind": "narrator", "voice": "narrator"})
    ids = {member["id"] for member in cast}
    languages = [code for code in video_story.LANGUAGES if code in story or code in (video.get("languages") or [])]
    parts = []
    for scene in payload.scenes:
        parts.append([{"speaker": part.speaker if part.speaker in ids else "narrator",
                       "text": {lang: " ".join(text.split()) for lang, text in part.text.items()
                                if lang in video_story.LANGUAGES and text.strip()}} for part in scene.parts])
    story["parts"] = parts
    story["cast"] = video_story.assign_voices(cast)
    story["drawn"] = [" ".join(scene.picture.split()) for scene in payload.scenes]
    story["people"] = [scene.people for scene in payload.scenes]
    story["notes"] = [[" ".join(note.split())[:1000] for note in scene.notes if note.strip()] for scene in payload.scenes]
    story["pictures"] = [None] * len(parts)
    for lang in languages:
        story[lang] = [video_story.scene_text(scene, lang) for scene in parts]
    for key in [k for k in story if k == "narration" or k.startswith("voice_")]:
        story.pop(key)
    video["scenes"] = len(parts)
    await _save_video(message.id, video)
    missing = any(not (part["text"] or {}).get(lang) for scene in parts for part in scene for lang in languages)
    return {"video": video, "needs_translation": missing}


@router.post("/video/{message_id}/prepare")
async def prepare_draft(message_id: int, principal: Principal = Depends(authenticate),
                        session: AsyncSession = Depends(get_session)):
    """Fills in what a draft is missing - the words of a part in a language it was not written in, the
    speakers of a story that has none - and leaves it a draft."""
    user = _require_user(principal)
    message, video = await _draft_of(session, user, message_id)
    ctx = {"prompt": video.get("asked", ""), "languages": video.get("languages") or ["en", "bn"], "review": True,
           "conversation_id": message.conversation_id, "message_id": message.id, "video": video}
    job = jobs.start(user.id, message.conversation_id, "video", "preparing: " + video.get("title", "")[:100],
                     _one_video_at_a_time(_video_pipeline(principal, user.id, ctx)))
    return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Job-Id": str(job.id)})


class ProduceIn(BaseModel):
    # The story's languages to make a video in now; the others keep their words, and their video can be
    # made later (an edit adding the language). None: every one of them
    languages: list[str] | None = Field(default=None, max_length=len(video_story.LANGUAGES))


@router.post("/video/{message_id}/produce")
async def produce_video(message_id: int, payload: ProduceIn | None = None, principal: Principal = Depends(authenticate),
                        session: AsyncSession = Depends(get_session)):
    """Makes the video of a story the person has read and corrected: every picture drawn and checked,
    every part voiced, a video in each language asked for. Only now is the GPUs' time spent."""
    user = _require_user(principal)
    if not images.is_available() or not video_story.is_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Video stories are not set up on this server")
    message, video = await _draft_of(session, user, message_id)
    told_in = video.get("languages") or ["en", "bn"]
    if payload is not None and payload.languages is not None:
        chosen = [lang for lang in told_in if lang in payload.languages]
        if not chosen:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Pick at least one language to make a video in")
        video["make"] = chosen
    video["drawing"] = {"attempt": 0, "wrong": {}, "keep_every_scene": True}
    await _save_video(message.id, video)
    ctx = {"prompt": video.get("asked", ""), "languages": video.get("languages") or ["en", "bn"],
           "conversation_id": message.conversation_id, "message_id": message.id, "video": video}
    slot = await limits.take_gpu_slot(principal)
    job = jobs.start(user.id, message.conversation_id, "video", "producing: " + video.get("title", "")[:100],
                     limits.holding(slot, _one_video_at_a_time(_video_pipeline(principal, user.id, ctx))))
    return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Job-Id": str(job.id)})


class VersionIn(BaseModel):
    lang: str = Field(pattern="^[a-z]{2}$")
    file_id: int


@router.post("/video/{message_id}/version")
async def restore_version(message_id: int, payload: VersionIn, principal: Principal = Depends(authenticate),
                          session: AsyncSession = Depends(get_session)):
    """Brings back an earlier version of a language's video; the one it replaces becomes a version."""
    user = _require_user(principal)
    message, video = await _owned_video(session, user, message_id)
    versions = (video.get("versions") or {}).get(payload.lang) or []
    chosen = next((v for v in versions if v.get("file_id") == payload.file_id), None)
    if chosen is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such version")
    versions.remove(chosen)
    _keep_version(video, payload.lang)
    video.setdefault("videos", {})[payload.lang] = {k: v for k, v in chosen.items() if k != "replaced_at"}
    await _save_video(message.id, video)
    return {"video": video}


@router.get("/videos")
async def list_videos(principal: Principal = Depends(authenticate), session: AsyncSession = Depends(get_session)):
    """The person's video stories, newest first, for the Videos page."""
    user = _require_user(principal)
    rows = (await session.execute(
        select(ConversationMessage, Conversation.uid).join(Conversation).where(
            Conversation.user_id == user.id, ConversationMessage.role == "assistant",
            ConversationMessage.content.like("(video story:%")).order_by(ConversationMessage.id.desc()))).all()
    stories = []
    for message, uid in rows:
        video = json.loads(message.attachments or "{}").get("video")
        if not video:
            continue
        story = video.get("story") or {}
        pictures = [p for p in story.get("pictures") or [] if p]
        stories.append({"message_id": message.id, "conversation": uid, "title": video.get("title"),
                        "title_bn": video.get("title_bn"), "state": video.get("state", "done"),
                        "languages": list((video.get("videos") or {}).keys()),
                        "scenes": len(story.get("pictures") or []) or video.get("scenes"),
                        "picture": pictures[0] if pictures else None, "editable": bool(story),
                        "versions": sum(len(v) for v in (video.get("versions") or {}).values()),
                        "made": message.created_at.isoformat(timespec="minutes")})
    return {"videos": stories}


@router.get("/videos/{message_id}")
async def get_video(message_id: int, principal: Principal = Depends(authenticate),
                    session: AsyncSession = Depends(get_session)):
    user = _require_user(principal)
    message, video = await _owned_video(session, user, message_id)
    conversation = await session.get(Conversation, message.conversation_id)
    running = jobs.running_for(user.id, message.conversation_id)
    return {"message_id": message.id, "conversation": conversation.uid if conversation else None, "video": video,
            # the voice each of a story's characters is given, in its languages: so the draft page can
            # say which are voices the model has and which are the language's one voice, turned
            "voice_plan": _voice_plan(video),
            "being_made": running is not None and running.kind == "video"}


def _voice_plan(video):
    """{lang: {voice slot: {name, turned}}} for a story's languages: which of its characters get a
    voice the model has, and which the language's own voice, turned a little (app/tools/voices.py)."""
    story = video.get("story") or {}
    langs = [l for l in video_story.LANGUAGES if l in story or l in (video.get("languages") or [])]
    plan = {}
    for lang in langs:
        narrator = voices.narrator(lang)
        plan[lang] = {slot: {"name": voices.spec(lang, slot, narrator)[1] if narrator[0] == "kokoro"
                             else "the voice",
                             "turned": voices.shifted(lang, slot, narrator)}
                      for slot in voices.kinds()}
    return plan


async def _save_video(message_id, video, stats=None):
    """The video story's state, written to its message as it goes."""
    if message_id is None:
        return
    async with session_factory()() as db:
        row = await db.get(ConversationMessage, message_id)
        if row is None:
            return
        filed = json.loads(row.attachments or "{}")
        filed["video"] = video
        row.attachments = json.dumps(filed)
        if stats is not None:
            row.stats = json.dumps(stats)
        await db.commit()


async def _store_file(user_id, kind, mime, name, data, prompt=None):
    async with session_factory()() as db:
        stored = ChatFile(user_id=user_id, kind=kind, mime_type=mime, name=name, prompt=prompt, data=data)
        db.add(stored)
        await db.commit()
        return stored.id


DRAW_BATCH = 7            # scenes drawn before they are checked, and the ones that pass kept


async def _video_pipeline(principal, user_id, ctx):
    """Makes a video story, or carries one on (ctx["video"], saved before): the plan, every scene's
    picture drawn and checked, the narration, a video for each language. Each step is saved to the
    conversation as it is done, so a video that stops partway - for any reason, a restart or sleep
    included - is "paused" with what it has, and continue_video carries it on from there, never
    planning or drawing again what it already has. Losing an hour of drawing to a narration that
    failed after the last picture (2026-10-25) is what this is for."""
    started = time.monotonic()
    story_model = settings.get("video.story.model", "VIDEO_STORY_MODEL")
    check_model = settings.get("video.check.model", "VIDEO_CHECK_MODEL")
    bangla_model = settings.get("video.bangla.model", "VIDEO_BANGLA_MODEL") or story_model
    attempts = max(1, int(settings.get("video.check.attempts", "VIDEO_CHECK_ATTEMPTS") or 3))
    languages = ctx["languages"]
    message_id = ctx.get("message_id")
    video = ctx.get("video")
    # The languages a video is made in: those asked for (video["make"]), else every one it is told in.
    # The others are still planned and translated; their video can be made later
    wanted = (video or {}).get("make")
    produced = [lang for lang in languages if wanted is None or lang in wanted]
    folder = video_story.workspace()
    events = asyncio.Queue()

    # Two measures of progress, for the page's two bars: the part being done (planning, the pictures,
    # each language's narration and video) and how far it has got, and the whole. One bar for both
    # ran up and back with every picture's sampling, which said nothing about a 28-scene video.
    parts = ["Planning the story", "Drawing and checking the pictures"]
    for lang in produced:
        word = video_story.language_name(lang)
        parts += [f"Narrating in {word}", f"Putting the {word} video together"]
    weights = [0.06, 0.74] + [0.2 / (len(parts) - 2)] * (len(parts) - 2)
    now = {"part": 1, "done": 0.0, "text": ""}

    def step(part, done, text):
        """A progress event: `part` (1-based) of the parts, `done` of it (0 to 1), what is happening."""
        now.update(part=part, done=done, text=text)
        return _step_event(part, done, text)

    def _step_event(part, done, text):
        overall = sum(weights[:part - 1]) + weights[part - 1] * done
        return _event("progress", stage=text, fraction=round(overall, 3), step=part, steps=len(parts),
                      step_name=parts[part - 1], part=round(done, 3))

    async def progress(stage, fraction=None):
        """The image model's own progress, told as detail of the part it belongs to."""
        detail = f"{now['text']} · {stage}" + (f" {round(fraction * 100)}%" if fraction is not None else "")
        await events.put(_step_event(now["part"], now["done"], detail))

    async def relay(work):
        """Runs `work`, passing on its progress as it comes; returns its result."""
        task = asyncio.create_task(work)
        try:
            while not task.done() or not events.empty():
                getter = asyncio.create_task(events.get())
                done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                else:
                    getter.cancel()
            yield task.result()
        finally:
            if not task.done():
                task.cancel()

    try:
        if video is None:
            # A story the person wrote is told as it is - in this request, in an earlier message they
            # point back to, or in a document - and nothing is looked up. Only a topic is searched for.
            prompt, material, earlier = ctx["prompt"], ctx["material"], ctx["earlier"]
            given = prompt if len(prompt) >= video_story.GIVEN_LENGTH else None
            if given is None and (material or any(len(e) >= video_story.GIVEN_LENGTH for e in earlier)):
                yield step(1, 0.1, "Finding the story you gave")
                given = video_story.given_story(await _ask_helper(principal, video_story.choose_request(
                    story_model, prompt, earlier, material)), earlier, material)
            sources = []
            if given:
                # Read word for word: the scenes are its own sentences; the model only plans the
                # pictures and translates into the other language. The Bangla model: on the Pond of
                # Milk, Gemma's translation was faithful and natural where Qwen's was clumsy.
                yield step(1, 0.3, "Planning a picture for every scene, and the translation")
                lang = "bn" if video_story.is_bangla(given) else "en"
                # References and footnotes in it are shown on a dark screen after their scene, not read
                yield step(1, 0.2, "Setting the references and footnotes apart from the story")
                paragraphs = video_story.story_paragraphs(given)
                told_text, notes, tidy = video_story.take_notes(await _ask_helper(
                    principal, video_story.notes_request(bangla_model, paragraphs)), paragraphs)
                title, parts = video_story.split_story(told_text)
                story = video_story.parse_illustrated(await _ask_helper(principal, video_story.illustrate_request(
                    bangla_model, title, parts, lang)), title, parts, lang)
            else:
                yield step(1, 0.1, "Finding the facts")
                facts, sources = await _video_facts(prompt)
                yield step(1, 0.4, "Writing the story")
                story = video_story.parse_story(await _ask_helper(principal, video_story.story_request(
                    story_model, prompt, material, facts, ctx["pictures"])))
            if "bn" in languages and not given:
                yield step(1, 0.8, "Making the Bangla easy for children")
                story = video_story.apply_bangla(
                    await _ask_helper(principal, video_story.bangla_request(bangla_model, story)), story)
            scenes = story["scenes"]
            used = [sources[n - 1] for n in story["sources"] if 0 < n <= len(sources)] or sources[:5]
            video = {"title": story["title_en"], "title_bn": story["title_bn"], "videos": {},
                     "scenes": len(scenes), "perspectives": [] if given else story["perspectives"],
                     "sources": [] if given else used, "asked": prompt, "from_story": bool(given),
                     "languages": languages, "state": "draft" if ctx.get("review") else "making",
                     "source": lang if given else "en",          # the language the story was written in
                     "story": {"pictures": [None] * len(scenes), "en": [s["narration_en"] for s in scenes],
                               "bn": [s["narration_bn"] for s in scenes],
                               "drawn": [s["picture"] for s in scenes],     # what each was drawn from
                               "people": [s["people"] for s in scenes],
                               # shown after each scene on a dark screen, not read: references, footnotes
                               "notes": video_story.notes_by_scene(notes, tidy, parts) if given else
                               [[] for _ in scenes]},
                     "drawing": {"attempt": 0, "wrong": {}}}
            if ctx.get("conversation_id") is not None:
                async with session_factory()() as db:
                    conversation = await db.get(Conversation, ctx["conversation_id"])
                    if conversation is not None and conversation.user_id == user_id:
                        filed = {"video": video}
                        if ctx.get("answers_to") is not None:
                            filed["answers_to"] = ctx["answers_to"]
                        message = ConversationMessage(conversation_id=conversation.id, role="assistant",
                                                      content=f"(video story: {video['title']})", model=story_model,
                                                      attachments=json.dumps(filed))
                        db.add(message)
                        conversation.updated_at = utcnow()
                        await db.commit()
                        message_id = message.id
        else:
            video["state"] = "draft" if ctx.get("review") else "making"
            video.pop("paused", None)
            await _save_video(message_id, video)
            yield step(1, 1.0, "Preparing the scenes" if ctx.get("review") else "Carrying on from where it stopped")
            # An edit asked for the other languages of a changed line to be translated again
            for n, (i, source, target) in enumerate(video.get("translate") or []):
                yield step(1, (n + 1) / len(video["translate"]), f"Translating scene {i + 1} again")
                done = video_story.translation(await _ask_helper(principal, video_story.translate_request(
                    bangla_model, video["story"][source][i], target)))
                if done is None:
                    raise video_story.VideoError(f"Scene {i + 1} could not be translated; try again")
                _set_line(video["story"], target, i, done)
            if video.pop("translate", None) is not None:
                await _save_video(message_id, video)
        for lang in languages:
            if lang in video["story"]:
                continue
            yield step(1, 0.9, f"Translating the story into {video_story.language_name(lang)}")
            source = "en" if "en" in video["story"] else next(k for k in ("bn",) if k in video["story"])
            title, lines = video_story.parse_translated_story(await _ask_helper(
                principal, video_story.translate_story_request(bangla_model, _title(video, source),
                                                               video["story"][source], lang)),
                len(video["story"][source]))
            video["story"][lang] = lines
            video.setdefault("titles", {})[lang] = title or _title(video, source)
            await _save_video(message_id, video)

        # Who speaks each part of each scene - the narrator, or a character in their own words - in
        # every language, so that each can be given their own voice
        told = video["story"]
        if "parts" not in told:
            yield step(1, 0.92, "Finding who speaks each part")
            source = video.get("source") if video.get("source") in told else "en"
            cast, found = video_story.parse_cast(await _ask_helper(principal, video_story.cast_request(
                bangla_model, told[source])), told[source])
            told["cast"] = cast
            told["parts"] = [[{"speaker": p["speaker"], "text": {source: p["text"]}} for p in scene] for scene in found]
            await _save_video(message_id, video)
        for lang in [code for code in video_story.LANGUAGES if code in told]:
            missing = [(i, k) for i, scene in enumerate(told["parts"]) for k, part in enumerate(scene)
                       if not (part.get("text") or {}).get(lang)]
            if not missing:
                continue
            # A scene of one part is its whole line; the parts of a scene of several are translated
            single = [(i, k) for i, k in missing
                      if len(told["parts"][i]) == 1 and i < len(told.get(lang) or []) and told[lang][i]]
            for i, k in single:
                told["parts"][i][k].setdefault("text", {})[lang] = told[lang][i]
            several = [(i, k) for i, k in missing if (i, k) not in single]
            if several:
                yield step(1, 0.96, f"Translating the speakers' parts into {video_story.language_name(lang)}")
                sources = []
                for i, k in several:
                    text = told["parts"][i][k]["text"]
                    sources.append(next((text[l] for l in (video.get("source"), "en", "bn") if l and text.get(l)),
                                        next(iter(text.values()), "")))
                lines = video_story.parse_translated_parts(await _ask_helper(principal, video_story.translate_parts_request(
                    bangla_model, sources, lang)), len(sources))
                for (i, k), line in zip(several, lines):
                    told["parts"][i][k].setdefault("text", {})[lang] = line
            for i, scene in enumerate(told["parts"]):
                whole = video_story.scene_text(scene, lang)
                if whole and told.setdefault(lang, [""] * len(told["parts"]))[i] != whole:
                    told[lang][i] = whole
                    _forget_voice(told, lang, i)
            await _save_video(message_id, video)

        # Prepared for review: nothing is drawn or voiced until the person has read the scenes and
        # asks for the video (produce_video)
        if video.get("state") == "draft":
            await _save_video(message_id, video)
            yield _event("draft", title=video["title"], scenes=len(told["parts"]), message_id=message_id)
            return
        yield _event("storyboard", title=video["title"], scenes=len(video["story"]["drawn"]),
                     message_id=message_id)

        told = video["story"]
        scenes = [{"narration_en": en, "narration_bn": bn, "picture": drawn, "people": people}
                  for en, bn, drawn, people in zip(told["en"], told["bn"], told["drawn"],
                                                   told.get("people") or ["from_behind"] * len(told["en"]))]
        drawing = video.setdefault("drawing", {"attempt": 0, "wrong": {}})
        # Drawn and checked a batch at a time: the image model and the checking model take turns on
        # the GPUs, so one batch is two swaps, not one per scene - and every picture that passes is
        # kept as its batch is checked. Drawing all 28 of a long story before the first check meant
        # twelve minutes of pictures lost to anything that stopped it in that time.
        if "done" not in drawing:
            paths = {}
            wrong = {int(k): why for k, why in drawing.get("wrong", {}).items()}
            turned = set()                                      # scenes whose people were turned away
            pending = [i for i, picture in enumerate(told["pictures"]) if picture is None]
            for attempt in range(drawing.get("attempt", 0), attempts + 1):
                drawing["attempt"] = attempt
                failed = []
                for first in range(0, len(pending), DRAW_BATCH):
                    batch = pending[first:first + DRAW_BATCH]
                    for i in batch:
                        plain = attempt >= attempts - 1 and attempt > 0     # the last tries: nobody in it
                        # A picture whose only fault is a face keeps its moment: the editing model turns
                        # the people away (two people talking face each other, however the scene is
                        # asked for); if a face is still there after that, the scene is drawn again
                        turn = wrong.get(i) in video_story.TURNABLE and i not in turned and i in paths
                        kept_now = sum(1 for picture in told["pictures"] if picture is not None)
                        yield step(2, kept_now / len(scenes),
                                   (f"Turning the people away in scene {i + 1}" if turn else
                                    f"Drawing scene {i + 1} of {len(scenes)}" + (" again" if attempt else ""))
                                   + f" · {kept_now} of {len(scenes)} pictures ready")
                        png = None
                        if turn:
                            turned.add(i)
                            with open(paths[i], "rb") as f:
                                work = images.generate(video_story.TURN_AWAY, video_story.SIZE,
                                                       sources=[(f.read(), "image/png")], on_progress=progress)
                        else:
                            work = images.generate(video_story.picture_prompt(scenes[i], plain, wrong.get(i, "")),
                                                   video_story.SIZE, on_progress=progress, story=True)
                        async for item in relay(work):
                            if isinstance(item, bytes):
                                yield item
                            else:
                                png, _seed = item
                        paths[i] = os.path.join(folder, f"scene-{i:02d}.png")
                        with open(paths[i], "wb") as f:
                            f.write(png)
                    for i in batch:
                        kept_now = sum(1 for picture in told["pictures"] if picture is not None)
                        yield step(2, kept_now / len(scenes), f"Checking scene {i + 1} against the rules"
                                   f" · {kept_now} of {len(scenes)} pictures ready")
                        with open(paths[i], "rb") as f:
                            ok, why = video_story.verdict(
                                await _ask_helper(principal, video_story.check_request(check_model, f.read(), scenes[i])),
                                scenes[i], must_match=attempt < attempts - 1)
                        if ok:
                            told["pictures"][i] = await _store_file(user_id, "video-scene", "image/jpeg",
                                                                    f"scene {i + 1}.jpg", video_story.as_jpeg(paths[i]))
                            wrong.pop(i, None)
                        else:
                            log.info("video scene %d redrawn: %s", i + 1, why)
                            wrong[i] = why
                            failed.append(i)
                    drawing["wrong"] = {str(i): why for i, why in wrong.items()}
                    await _save_video(message_id, video)
                drawing["attempt"] = attempt + 1
                await _save_video(message_id, video)
                pending = failed
                if not pending:
                    break
            kept = [i for i, picture in enumerate(told["pictures"]) if picture is not None]
            if video.get("from_story") or drawing.get("keep_every_scene"):
                # Every word of a given story is read - and every scene of an edited one stays: a
                # scene whose picture never passed shows the nearest picture that did, rather than
                # being left out
                if not kept:
                    raise video_story.VideoError("No picture passed the content check to make a video")
                for i in range(len(told["pictures"])):
                    if told["pictures"][i] is None:
                        told["pictures"][i] = told["pictures"][min(kept, key=lambda k: (abs(k - i), k > i))]
            else:
                # A scene whose picture never passed is left out rather than shown
                if len(kept) < video_story.MIN_SCENES:
                    raise video_story.VideoError("Too few pictures passed the content check to make a video")
                for key in ("pictures", "en", "bn", "drawn", "people"):
                    if key in told:
                        told[key] = [told[key][i] for i in kept]
                video["scenes"] = len(kept)
            drawing["done"] = True
            await _save_video(message_id, video)

        # The pictures as kept, for every language's video
        async with session_factory()() as db:
            files = {f.id: f.data for f in (await db.execute(select(ChatFile).where(
                ChatFile.id.in_(set(told["pictures"])), ChatFile.user_id == user_id))).scalars()}
        if any(picture not in files for picture in told["pictures"]):
            raise video_story.VideoError("Some of this story's pictures are gone; make it again")
        paths = []
        for n, picture in enumerate(told["pictures"]):
            paths.append(os.path.join(folder, f"kept-{n:02d}.jpg"))
            with open(paths[-1], "wb") as f:
                f.write(files[picture])

        videos = video.setdefault("videos", {})
        voices = told.setdefault("narration", {})
        cache = video_story.clip_cache(message_id)
        # Who speaks each part, in which voice: a character's id to the voice slot their cast gives them
        cast_map = {m["id"]: m.get("voice") or "narrator" for m in told.get("cast") or []}
        for k, lang in enumerate(produced):
            if lang in videos and not voices.get(lang):
                continue                                         # made before it stopped, or not edited
            word = video_story.language_name(lang)
            texts = told[lang]
            # Each line's narration is kept: after an edit only the lines changed are voiced again,
            # and a line the person read keeps their voice
            entries = ((voices.get(lang) or []) + [None] * len(texts))[:len(texts)]
            missing = [i for i, entry in enumerate(entries) if not entry]
            yield step(3 + 2 * k, 0.0, f"Narrating in {word}" +
                       (f": {len(missing)} of {len(texts)} lines" if 0 < len(missing) < len(texts) else ""))
            if missing:
                # Each scene's parts, in their speakers' voices, joined into the scene's one line
                spoken = await video_story.narrate_parts(lang, [_scene_parts_in(told, lang, i) for i in missing],
                                                         cast_map, folder)
                for i, (wav, seconds) in zip(missing, spoken):
                    audio = await video_story.to_m4a(wav, os.path.join(folder, f"voice-{lang}-{i:02d}.m4a"))
                    entries[i] = {"file_id": await _store_file(user_id, "voice", "audio/mp4",
                                                               f"scene {i + 1} ({word}).m4a", audio),
                                  "seconds": round(seconds, 3),
                                  # a verse alone is the qari's recitation, never softened like the voice
                                  "by": "machine" if video_story.needs_reading(texts[i], lang) else "qari"}
                voices[lang] = entries
                await _save_video(message_id, video)
            async with session_factory()() as db:
                audio = {f.id: f.data for f in (await db.execute(select(ChatFile).where(
                    ChatFile.id.in_({entry["file_id"] for entry in entries}), ChatFile.user_id == user_id))).scalars()}
            narration = []
            for i, entry in enumerate(entries):
                if entry["file_id"] not in audio:
                    raise video_story.VideoError(f"The narration of scene {i + 1} is gone; edit the scene to voice it again")
                path = os.path.join(folder, f"line-{lang}-{i:02d}.m4a")
                with open(path, "wb") as f:
                    f.write(audio[entry["file_id"]])
                narration.append((path, entry["seconds"]))
            yield step(4 + 2 * k, 0.0, f"Putting the {word} video together")
            title = _title(video, lang)
            data, seconds = await video_story.assemble(paths, narration, texts, title, lang, folder,
                                                       recorded=[entry.get("by") != "machine" for entry in entries],
                                                       keys=told["pictures"], cache=cache, notes=told.get("notes"))
            stored = await _store_file(user_id, "video", "video/mp4", f"{video['title'][:80]} ({word}).mp4", data,
                                       prompt=title)
            videos[lang] = {"file_id": stored, "url": f"/chat/files/{stored}", "seconds": seconds}
            if lang in (video.get("behind") or []):         # it says what is saved again
                video["behind"].remove(lang)
            read = [entry.get("by") == "recorded" for entry in entries if entry.get("by") != "qari"]
            if read and all(read):
                videos[lang]["voice"] = "recorded"
            elif any(read):
                videos[lang]["voice"] = "partly recorded"
            await _save_video(message_id, video)

        video["state"] = "done"
        video.pop("drawing", None)
        await _save_video(message_id, video, stats={"total": round(time.monotonic() - started, 1)})
        yield _event("video", **video, message_id=message_id, seconds=round(time.monotonic() - started, 1))
    except Exception as e:
        # Paused with what it has; the person can carry on (continue_video). A restart or sleep
        # gives no chance to say so - the message stays "making", which the page reads the same way.
        expected = isinstance(e, (video_story.VideoError, images.ImageError))
        if not expected:
            log.exception("video story stopped")
        if video is not None:
            # A draft being prepared stays a draft, to be read and prepared again
            video["state"] = "draft" if ctx.get("review") else "paused"
            video["paused"] = str(e) if expected else "Something went wrong while it was being made"
            await _save_video(message_id, video)
        yield _error((str(e) if expected else "The video stopped partway") +
                     ("; it is saved, and can be carried on from where it stopped" if message_id else ""))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# A video takes both GPUs in turn - its language models, then the image model - so two at once
# fight over them: a second request started Gemma while the first was drawing, Gemma could not
# load, and the second video failed as "the story's pictures could not be planned" (2026-10-25)
_video_turn = asyncio.Lock()


async def _one_video_at_a_time(work, waiting="Waiting for the video already being made"):
    if _video_turn.locked():
        yield _event("progress", stage=waiting, fraction=0.0)
    async with _video_turn:
        async for chunk in work:
            yield chunk


class VoiceIn(BaseModel):
    # One per scene, in order: a recording (base64, as the browser recorded it), or null to keep the
    # one saved last time for that scene
    recordings: list[str | None] = Field(max_length=video_story.GIVEN_MAX_SCENES)
    # Which language's video: the person reads the English too - its voice says Arabic names and
    # blessings wrongly
    lang: str = Field(default="bn", pattern="^[a-z]{2}$")


@router.post("/video/{message_id}/voice")
async def video_voice(message_id: int, payload: VoiceIn, principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    """A language's video made again with the person's own voice: each scene's line as they read it.
    The recordings are kept, per language, so one scene can be read again later without the others."""
    user = _require_user(principal)
    message = (await session.execute(
        select(ConversationMessage).join(Conversation).where(
            ConversationMessage.id == message_id, Conversation.user_id == user.id))).scalar_one_or_none()
    if message is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video story not found")
    filed = json.loads(message.attachments or "{}")
    video = filed.get("video") or {}
    story = video.get("story")
    if not story:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This video was made before stories were kept; make it again to read it yourself")
    count = len(story["pictures"])
    lang = payload.lang
    word = video_story.language_name(lang)
    if lang not in story or lang not in (video.get("videos") or {}):
        raise HTTPException(status.HTTP_409_CONFLICT, f"This story has no {word} video to read")
    saved = story.get(f"voice_{lang}") or [None] * count
    if len(payload.recordings) != count:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"This story has {count} scenes")
    if any(len(r or "") > 6_000_000 for r in payload.recordings):
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "A recording is too long; keep each scene under two minutes")
    # A scene that is all Arabic - a verse - is recited by the qari, not read by the person
    reads = [video_story.needs_reading(line, lang) for line in story[lang]]
    # A scene not read yet keeps the voice it has (the machine's), so the scenes can be read one at a time
    old_entries = (story.get("narration") or {}).get(lang) or []
    unread = {n: old_entries[n] for n, (new, old) in enumerate(zip(payload.recordings, saved))
              if reads[n] and not new and not old and n < len(old_entries) and old_entries[n]}
    missing = [n + 1 for n, (new, old) in enumerate(zip(payload.recordings, saved))
               if reads[n] and not new and not old and n not in unread]
    if missing:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Read every scene first; not yet read: " + ", ".join(map(str, missing)))
    wanted = list(story["pictures"]) + [old["file_id"] for old in saved if old] +         [entry["file_id"] for entry in unread.values()]
    found = await _load_files(session, user, wanted)
    if any(i not in found for i in wanted):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Some of this story's pictures or recordings are gone")
    user_id = user.id

    async def stream():
        folder = video_story.workspace()
        replaced, created = [], []           # dropped once the new video is saved / if it is not
        try:
            paths, narration, voice = [], [], []
            for n, picture in enumerate(story["pictures"]):
                paths.append(os.path.join(folder, f"scene-{n:02d}.jpg"))
                with open(paths[-1], "wb") as f:
                    f.write(found[picture].data)
            for n, (new, old) in enumerate(zip(payload.recordings, saved)):
                line = story[lang][n]
                if not reads[n]:
                    if old:                           # read before the qari took these scenes
                        replaced.append(old["file_id"])
                    voice.append(None)
                    narration.append(await video_story.with_recitation(line, lang, None, 0, folder, f"{lang}-{n:02d}")
                                     or video_story.held_silence(line, lang, os.path.join(folder, f"held-{n:02d}.wav")))
                    continue
                if n in unread:
                    path = os.path.join(folder, f"kept-{n:02d}.m4a")
                    with open(path, "wb") as f:
                        f.write(found[unread[n]["file_id"]].data)
                    voice.append(None)
                    narration.append((path, unread[n]["seconds"]))
                    continue
                if new:
                    yield _event("progress", stage=f"Cleaning up the recording of scene {n + 1}",
                                 fraction=0.05 + 0.5 * n / count)
                    try:
                        raw = base64.b64decode(new.split(",", 1)[-1], validate=False)
                    except ValueError:
                        raise video_story.VideoError(f"The recording of scene {n + 1} could not be read")
                    data, seconds = await video_story.clean_recording(raw, folder, n)
                    async with session_factory()() as db:
                        kept = ChatFile(user_id=user_id, kind="voice", mime_type="audio/mp4",
                                        name=f"scene {n + 1}.m4a", data=data)
                        db.add(kept)
                        await db.commit()
                    created.append(kept.id)
                    if old:
                        replaced.append(old["file_id"])
                    voice.append({"file_id": kept.id, "seconds": seconds})
                else:
                    data = found[old["file_id"]].data
                    voice.append(old)
                path = os.path.join(folder, f"voice-{n:02d}.m4a")
                with open(path, "wb") as f:
                    f.write(data)
                # A verse in a line the person read keeps its recitation, in the order it is written
                narration.append(await video_story.with_recitation(line, lang, path, voice[-1]["seconds"],
                                                                   folder, f"{lang}-{n:02d}")
                                 or (path, voice[-1]["seconds"]))
            # Each scene's narration as it now is, kept like the machine's (the pipeline's "narration"),
            # so an edit later re-voices only the lines it changes and keeps the person's voice
            entries = []
            for n, (path, seconds) in enumerate(narration):
                if n in unread:
                    entries.append(unread[n])
                    continue
                if reads[n] and path.endswith(f"voice-{n:02d}.m4a"):
                    entries.append({"file_id": voice[n]["file_id"], "seconds": seconds, "by": "recorded"})
                    continue
                audio = await video_story.to_m4a(path, os.path.join(folder, f"keep-{n:02d}.m4a"))
                kept_id = await _store_file(user_id, "voice", "audio/mp4", f"scene {n + 1} ({word}).m4a", audio)
                created.append(kept_id)
                entries.append({"file_id": kept_id, "seconds": round(seconds, 3),
                                "by": "recorded" if reads[n] else "qari"})
            yield _event("progress", stage=f"Putting the {word} video together with your voice", fraction=0.6)
            title = _title(video, lang)
            data, seconds = await video_story.assemble(paths, narration, story[lang], title, lang, folder,
                                                       recorded=[n not in unread for n in range(count)],
                                                       keys=story["pictures"], notes=story.get("notes"),
                                                       cache=video_story.clip_cache(message_id))
            async with session_factory()() as db:
                stored = ChatFile(user_id=user_id, kind="video", mime_type="video/mp4",
                                  name=f"{video.get('title', 'Video story')[:80]} ({word}, your voice).mp4",
                                  prompt=title, data=data)
                db.add(stored)
                await db.commit()
            created.append(stored.id)
            # The video it replaces is kept as an earlier version, not removed
            _keep_version(video, lang)
            video.setdefault("videos", {})[lang] = {"file_id": stored.id, "url": f"/chat/files/{stored.id}",
                                                    "seconds": seconds,
                                                    "voice": "partly recorded" if unread else "recorded"}
            story.setdefault("narration", {})[lang] = entries
            in_use = {entry["file_id"] for entry in entries} | {v["file_id"] for v in voice if v}
            replaced += [entry["file_id"] for entry in old_entries if entry and entry["file_id"] not in in_use]
            story[f"voice_{lang}"] = voice
            video["story"] = story
            filed["video"] = video
            async with session_factory()() as db:
                row = await db.get(ConversationMessage, message_id)
                row.attachments = json.dumps(filed)
                await db.commit()
            created = []
            for file_id in replaced:
                await _drop_file(user_id, file_id)
            yield _event("video", **video, message_id=message_id)
        except video_story.VideoError as e:
            yield _error(str(e))
        finally:
            for file_id in created:          # the story still points at what it had
                await _drop_file(user_id, file_id)
            shutil.rmtree(folder, ignore_errors=True)

    # A job, like the video itself: leaving the page does not stop it
    job = jobs.start(user.id, message.conversation_id, "video", "reading the story in your voice", stream())
    return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Job-Id": str(job.id)})


@router.post("/video/{message_id}/upload-voice")
async def upload_voiceover(message_id: int, audio: UploadFile = File(...), lang: str = Form(...),
                           index: int = Form(...), principal: Principal = Depends(authenticate),
                           session: AsyncSession = Depends(get_session)):
    """Upload a voiceover for one scene of one language: convert the audio, store it, update the narration.
    The user clicks "Save and remake" to rebuild the video with the uploaded voice."""
    user = _require_user(principal)
    message = (await session.execute(
        select(ConversationMessage).join(Conversation).where(
            ConversationMessage.id == message_id, Conversation.user_id == user.id))).scalar_one_or_none()
    if message is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video story not found")
    filed = json.loads(message.attachments or "{}")
    video = filed.get("video") or {}
    story = video.get("story")
    if not story:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This video was made before stories were kept; make it again")
    count = len(story["pictures"])
    word = video_story.language_name(lang)
    if lang not in story:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This story has no {word}")
    if index < 0 or index >= count:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"This story has {count} scenes (0–{count - 1})")
    # A scene that is all Arabic - a verse - is recited by the qari, not uploaded
    line = story[lang][index]
    if not video_story.needs_reading(line, lang):
        raise HTTPException(status.HTTP_409_CONFLICT, "This scene is a verse recited by the qari; only qari recitation is allowed")
    user_id = user.id
    folder = video_story.workspace()

    async def stream():
        replaced, created = [], []
        try:
            yield _event("progress", stage="Converting audio", fraction=0.0)
            raw = await audio.read()
            if not raw or len(raw) < 100:
                raise video_story.VideoError("The uploaded file is empty or too small")
            # Convert uploaded audio to m4a
            audio_path = os.path.join(folder, f"upload-{index:02d}.m4a")
            try:
                seconds = await video_story.audio_to_m4a(raw, audio_path)
            except video_story.VideoError as e:
                raise video_story.VideoError(f"The uploaded audio could not be processed: {e}")
            except Exception as e:
                raise video_story.VideoError(f"The uploaded audio could not be processed; please upload an mp3, m4a, or wav file")
            # Store the voiceover file
            mime = audio.content_type or "audio/mpeg"
            mime = "audio/mpeg" if mime == "audio/mp3" else mime
            ext = audio.filename.rsplit(".", 1)[-1] if "." in audio.filename else "m4a"
            async with session_factory()() as db:
                kept = ChatFile(user_id=user_id, kind="voice", mime_type=mime,
                                name=f"scene {index + 1} ({word}).{ext}",
                                data=raw)
                db.add(kept)
                await db.commit()
                created.append(kept.id)
            # Update the narration entry for this scene
            old_entries = (story.get("narration") or {}).get(lang) or []
            old_entry = old_entries[index] if index < len(old_entries) else None
            if old_entry and old_entry.get("file_id"):
                replaced.append(old_entry["file_id"])
            # Mark this scene as having a user upload for the "stale" check
            old_entry_count = len(old_entries)
            entries = list(old_entries) if old_entries else []
            while len(entries) <= index:
                entries.append(None)
            entries[index] = {"file_id": kept.id, "seconds": round(seconds, 3), "by": "uploaded"}
            story.setdefault("narration", {})[lang] = entries
            video["story"] = story
            filed["video"] = video
            async with session_factory()() as db:
                row = await db.get(ConversationMessage, message_id)
                row.attachments = json.dumps(filed)
                await db.commit()
            for file_id in replaced:
                await _drop_file(user_id, file_id)
            created = []
            yield _event("progress", stage="Done", fraction=1.0)
            yield _event("voiceover", {"scene": index + 1, "lang": lang, "file_id": kept.id})
        except video_story.VideoError as e:
            yield _error(str(e))
        finally:
            for file_id in created:
                await _drop_file(user_id, file_id)
            shutil.rmtree(folder, ignore_errors=True)

    job = jobs.start(user.id, message.conversation_id, "video", f"uploading voiceover for scene {index + 1}", stream())
    return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Job-Id": str(job.id)})


def _title(video, lang):
    """A video story's title in one of its languages."""
    if lang == "bn" and video.get("title_bn"):
        return video["title_bn"]
    return (video.get("titles") or {}).get(lang) or video.get("title") or ""


def _keep_version(video, lang):
    """Moves a language's current video into its earlier versions, newest first."""
    current = (video.get("videos") or {}).pop(lang, None)
    if current:
        versions = video.setdefault("versions", {}).setdefault(lang, [])
        versions.insert(0, {**current, "replaced_at": utcnow().isoformat(timespec="seconds")})


async def _drop_file(user_id, file_id):
    """Removes a file the person no longer has any use for: a recording or a video replaced."""
    async with session_factory()() as db:
        row = await db.get(ChatFile, file_id)
        if row is not None and row.user_id == user_id:
            await db.delete(row)
            await db.commit()


# --- the agent ----------------------------------------------------------------------------------

class AgentIn(BaseModel):
    model: str = Field(max_length=256)
    conversation_id: int | None = None
    messages: list[ChatMessageIn]
    temperature: float | None = None
    max_tokens: int | None = None
    answers_to: int | None = None


async def _one_round(principal, body):
    """One call to the model. Returns its message, or raises Stop with something to show."""
    try:
        response = await forward(principal, "chat_completions", PROXY_PATHS["chat_completions"], body)
        answer = json.loads(response.body)
    except HTTPException as e:
        raise agent.Stop(str(e.detail))
    except (ValueError, TypeError) as e:
        raise agent.Stop(f"the model's reply could not be read ({e}).")
    choices = answer.get("choices") or []
    if not choices:
        raise agent.Stop("the model returned nothing.")
    return choices[0].get("message") or {}, answer.get("usage") or {}


@router.post("/agent")
async def run_agent(payload: AgentIn, principal: Principal = Depends(authenticate),
                    session: AsyncSession = Depends(get_session)):
    """Runs a model with the tool servers in reach, and streams what it does.

    Every step is sent as it happens - which tool, with what arguments, what came back - because an
    agent that works silently for two minutes and then produces an answer is impossible to trust
    and impossible to debug. The same events are what gets saved, so the transcript of a turn can
    be read again later.
    """
    user = _require_user(principal)
    if not agent.is_available_to(principal):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "You have no tool servers to use. Either none are switched on, or none "
                            "of them are yours to reach.")
    if not access.can_use_model(principal, payload.model):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"You do not have access to model '{payload.model}'")
    backend = await backends.resolve(payload.model)
    if backend is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Model '{payload.model}' is not available")
    if "tools" not in backend.capabilities:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{payload.model} cannot call tools. Pick a model marked as able to.")
    if payload.conversation_id is not None:
        await _writable(session, payload.conversation_id, user)

    user_id = user.id
    steps = []          # what happened, kept so the turn can be saved and read again

    async def stream():
        yield _event("status", text="Asking the tool servers what they offer")
        try:
            # Settled here, from the configuration and who is asking, before the model sees
            # anything: a user gets the tools of the servers they may reach and no others
            mine = (access.tool_servers_for(principal, mcp_client.available())
                    if mcp_client.is_enabled() else [])
            own = self_tools.is_available_to(principal)
            tools, where, trouble = await agent.offered(mine, builtin=own)
        except Exception as e:
            yield _error(f"The tool servers could not be reached ({e.__class__.__name__}).")
            return
        for problem in trouble:
            yield _event("notice", text=f"Leaving out {problem}")
        if not tools:
            yield _error("None of the switched-on tool servers answered, so there are no tools "
                         "to use. Check them on the Tools page.")
            return
        # The user's notes, as tools: a note found to be wrong is fixed then and there (_memory_edit)
        memory = await _agent_memory(user_id) if memory_tool.is_enabled() else None
        if memory:
            for spec in memory_tool.memory_tools(agent.SEPARATOR):
                where[spec["function"]["name"]] = (MEMORY_TOOLS, spec["function"]["name"].split(agent.SEPARATOR)[-1])
                tools.append(spec)
        yield _event("tools", tools=[t["function"]["name"] for t in tools])

        latest_said = next((m.content for m in reversed(payload.messages) if m.role == "user"), "")
        seen = [latest_said or ""]      # what this turn has shown: evidence a note may be changed on
        extra = [agent.about_self() if own else "",
                 memory_tool.tool_guide(memory["listed"](), agent.SEPARATOR) if memory else ""]
        messages = agent.with_system([{"role": m.role, "content": m.content} for m in payload.messages],
                                     "\n\n".join(e for e in extra if e))
        budget = agent.Budget()
        answer, finished = "", None

        for round_number in range(agent.MAX_ROUNDS):
            stopping = budget.why_stop()
            if stopping:
                finished = stopping
                break
            yield _event("status", text="Thinking" if round_number == 0
                         else f"Thinking again, with what the tools said (round {round_number + 1})")
            body = agent.request(payload.model, messages, tools, payload.temperature,
                                 payload.max_tokens)
            try:
                message, usage = await _one_round(principal, body)
            except agent.Stop as e:
                yield _error(str(e))
                return

            calls = agent.wanted(message)
            answer = (message.get("content") or "").strip()
            if not calls:
                break

            # Keep the model's own turn exactly as it sent it, so the tool replies line up with it
            messages.append({"role": "assistant", "content": message.get("content") or "",
                             "tool_calls": message.get("tool_calls") or []})
            if len(calls) > agent.MAX_CALLS_PER_ROUND:
                calls = calls[:agent.MAX_CALLS_PER_ROUND]
                yield _event("notice", text=f"It asked for more than {agent.MAX_CALLS_PER_ROUND} "
                                            f"tools at once; the rest were left for the next round.")

            for call in calls:
                known = where.get(call["name"])
                began = time.monotonic()
                yield _event("call", round=round_number + 1, tool=call["name"],
                             arguments=str(call.get("arguments") or "")[:2000])
                if not known:
                    # The model invented a tool. Telling it so is more useful than failing.
                    text, bad = (f"There is no tool called {call['name']}. The ones you have are: "
                                 f"{', '.join(sorted(where))}."), True
                else:
                    server, tool = known
                    try:
                        if server is MEMORY_TOOLS:
                            text, bad = await _memory_edit(user_id, memory, tool,
                                                           agent.arguments(call.get("arguments")),
                                                           "\n".join(seen))
                        elif server is self_tools.BUILTIN:
                            text, bad = await self_tools.call(
                                tool, agent.arguments(call.get("arguments")),
                                {"principal": principal, "model": payload.model,
                                 "conversation_id": payload.conversation_id})
                        else:
                            text, bad = await mcp_client.call(server, tool,
                                                              agent.arguments(call.get("arguments")))
                    except agent.Stop as e:
                        text, bad = str(e), True
                    except mcp_client.McpError as e:
                        text, bad = str(e), True
                    except Exception as e:
                        log.info("tool %s failed: %s", call["name"], e)
                        text, bad = f"{e.__class__.__name__}: {e}", True
                seconds = round(time.monotonic() - began, 1)
                budget.take(text)
                if known and known[0] is not MEMORY_TOOLS:
                    seen.append(text)
                # The same field names the live event uses, so one renderer draws both: a turn
                # being watched and the same turn read back tomorrow
                steps.append({"tool": call["name"], "failed": bad, "seconds": seconds,
                              "arguments": str(call.get("arguments") or "")[:2000],
                              "text": agent.summarise(text, 2000)})
                yield _event("result", tool=call["name"], failed=bad, seconds=seconds,
                             text=agent.summarise(text))
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "name": call["name"], "content": text})
        else:
            finished = (f"It reached {agent.MAX_ROUNDS} rounds of tool calls, which is as far as "
                        f"this goes. What it had at that point is below.")

        if finished:
            yield _event("notice", text=finished)
        if not answer:
            answer = ("It stopped without an answer in words. What the tools returned is above.")
        yield _event("answer", text=answer, html=markdown.render(answer),
                     calls=budget.calls, seconds=round(budget.spent, 1))

        if payload.conversation_id is not None:
            async with session_factory()() as db:
                conversation = await db.get(Conversation, payload.conversation_id)
                if conversation is not None and conversation.user_id == user_id:
                    filed = {"agent": {"steps": steps, "seconds": round(budget.spent, 1)}}
                    if payload.answers_to is not None:
                        filed["answers_to"] = payload.answers_to
                    db.add(ConversationMessage(
                        conversation_id=payload.conversation_id, role="assistant", content=answer,
                        model=payload.model, stats=json.dumps({"total": round(budget.spent, 1)}),
                        attachments=json.dumps(filed)))
                    conversation.updated_at = utcnow()
                    await db.commit()
            if memory_tool.is_enabled():
                # The same check as after a chat turn, for whatever the agent did not note itself
                asyncio.create_task(_refresh_memory(principal, user_id, payload.conversation_id))

    if payload.conversation_id is None:
        # Nothing to come back to, so there is nothing to keep
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    # A job, like a chat answer: a task given to the agent goes on when its page is left - walking
    # over to the dashboard used to cancel it - and the conversation picks it up again when reopened
    asked = next((m.content for m in reversed(payload.messages) if m.role == "user"), "") or "Agent task"
    job = jobs.start(user_id, payload.conversation_id, "agent", asked[:120], stream())
    return StreamingResponse(jobs.follow(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Job-Id": str(job.id)})
