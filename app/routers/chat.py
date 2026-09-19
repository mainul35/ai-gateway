"""Saved playground conversations. Every user only ever sees their own."""
import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import Principal, authenticate
from app.db import get_session
from app.models import Conversation, ConversationMessage, utcnow

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
        "model": message.model, "stats": json.loads(message.stats) if message.stats else None,
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
                                  stats=json.dumps(payload.stats) if payload.stats else None)
    session.add(message)
    conversation.updated_at = utcnow()
    if payload.model:
        conversation.model = payload.model
    await session.commit()
    await session.refresh(message)
    return _message_json(message)
