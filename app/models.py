"""Database tables: users, API keys and per-request usage."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="user")  # "user" or "admin"
    # Only the built-in admin has one; SSO accounts authenticate against the auth server
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # "all", "selected" or "none"; admins always have every model regardless
    model_access: Mapped[str] = mapped_column(String(16), default="all")
    # JSON list of model names or patterns, used when model_access is "selected"
    allowed_models: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Everything the auth server said at the last sign-in, as it said it. Kept whole rather than
    # picked apart: what a provider puts in a token changes, and a gateway that reads three fields
    # and throws the rest away cannot answer "what does it actually send us" - which is the first
    # question anybody asks when wiring permissions up to it.
    claims: Mapped[str | None] = mapped_column(Text, nullable=True)
    claims_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128), default="default")
    # Only the hash is stored; the key itself is shown once when created
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="api_keys")


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    api_key_id: Mapped[int | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    model: Mapped[str] = mapped_column(String(256), index=True)
    backend: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(64))
    streamed: Mapped[bool] = mapped_column(Boolean, default=False)
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Conversation(Base):
    """A saved playground chat. Belongs to one user."""
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="ConversationMessage.id")


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # "user" or "assistant"
    content: Mapped[str] = mapped_column(Text, default="")
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # JSON with tokens, speed and time to first token, shown again when the chat is reopened
    stats: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON: attached or generated images ([{"file_id", "kind"}]) and web sources ([{"title", "url"}])
    attachments: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class MemoryEntry(Base):
    """What the playground remembers: notes about a user, or a running summary of one conversation."""
    __tablename__ = "memory_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Set for a conversation summary, empty for a note that outlives any single conversation
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True)
    scope: Mapped[str] = mapped_column(String(16))   # "user" or "conversation"
    content: Mapped[str] = mapped_column(Text)
    # Messages already covered by a conversation summary, so the rest can be sent as they are
    covered_count: Mapped[int] = mapped_column(Integer, default=0)
    # "auto" when the gateway wrote it, "manual" once the user has edited or added it
    source: Mapped[str] = mapped_column(String(16), default="auto")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ChatFile(Base):
    """A file a user uploaded to the playground, or an image generated there.

    Only its owner can read it. A document keeps the text taken out of it in `prompt`, which is what
    the model is given: nothing sends the bytes of a spreadsheet to a language model.
    """
    __tablename__ = "chat_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # "upload", "generated" or "document"
    # 128, because the Office media types are 65 and 71 characters long and 64 was not enough
    mime_type: Mapped[str] = mapped_column(String(128))
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # The prompt an image was generated from, shown under it
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    """A repository the gateway may change: cloned on this server, worked on in a worktree per task.

    Added from the Tasks page by a manager or admin. How it is checked (the verify command, in a
    throwaway container) is part of the project, because "the fix works" means something different
    for every codebase. How it is deployed is set by an admin only: it runs on this server.
    """
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)   # owner/repo
    # "git": cloned on this server, worked on in worktrees. "local": a folder on the computer of the
    # person who added it, reached through their browser or the helper - edited where it is.
    kind: Mapped[str] = mapped_column(String(16), default="git")
    client: Mapped[str | None] = mapped_column(String(16), nullable=True)    # "browser" or "helper"
    clone_url: Mapped[str] = mapped_column(String(512), default="")
    base_branch: Mapped[str] = mapped_column(String(128), default="main")
    # Run in the sandbox after every round of edits; non-zero exit means the fix is not done
    verify_command: Mapped[str] = mapped_column(Text, default="")
    sandbox_image: Mapped[str] = mapped_column(String(256), default="python:3.12-slim")
    # Builds that download dependencies need the network; the default is none
    sandbox_network: Mapped[bool] = mapped_column(Boolean, default=False)
    # Run on this server, in the project's own clone, after an approved change is merged
    deploy_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)   # told to the model with every task
    added_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CodingTask(Base):
    """One piece of work on a project: from a description to a pull request, a merge and a deploy."""
    __tablename__ = "coding_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(256))
    # queued, running, review, approved, merged, deploying, done, rejected, failed, cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)    # what it is doing now
    branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    verify_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # For a local project, JSON {path: text before the task, or null for a file it created}: what
    # the diff is measured against, and what Undo puts back
    originals: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class CodingTaskEvent(Base):
    """One step of a task, as it happened: kept so a task can be watched live and read again later."""
    __tablename__ = "coding_task_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("coding_tasks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))     # stage, call, result, verify, note, error, ...
    text: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
