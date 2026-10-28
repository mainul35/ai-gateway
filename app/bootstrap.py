"""Creates the built-in admin account on first start, so there is always a way in before SSO works."""
import logging
import secrets

from sqlalchemy import select, text

from app import settings
from app.db import engine, session_factory
from app.models import User
from app.security import hash_password

log = logging.getLogger("gateway")


# Columns added after the first release, as (table, column, type); create_all does not alter
# existing tables
_ADDED_COLUMNS = (
    ("answer_feedback", "reason", "TEXT"),
    ("conversations", "uid", "VARCHAR(36)"),
    ("users", "password_hash", "VARCHAR(256)"),
    # Existing accounts keep the access they effectively had before access control existed
    ("users", "model_access", "VARCHAR(16) DEFAULT 'all'"),
    ("users", "allowed_models", "TEXT"),
    ("users", "desktop_model", "VARCHAR(256)"),
    # What the auth server sent at the last sign-in, kept so permissions can be built on it
    ("users", "claims", "TEXT"),
    ("users", "claims_seen_at", "TIMESTAMPTZ"),
    ("conversation_messages", "attachments", "TEXT"),
    # Coding tasks on folders on people's own computers, added after the first version shipped
    ("projects", "kind", "VARCHAR(16) DEFAULT 'git'"),
    ("projects", "client", "VARCHAR(16)"),
    ("coding_tasks", "originals", "TEXT"),
    ("coding_tasks", "state", "TEXT"),
    # SSO accounts by the auth server's own id, and sign-ins waiting for an admin
    ("users", "sso_subject", "VARCHAR(320)"),
    ("users", "approval", "VARCHAR(16)"),
    ("users", "session_epoch", "INTEGER DEFAULT 0"),
    # Who wrote a message in a shared conversation; what the Code mode needs of a Claude Code session
    ("conversation_messages", "author_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
    ("ccd_sessions", "seen_at", "TIMESTAMPTZ"),
    ("ccd_sessions", "transcript", "TEXT"),
    ("ccd_sessions", "client", "VARCHAR(32)"),
)


async def ensure_schema():
    """Adds the columns and index introduced after the first release, when they are missing.

    Only when missing: ALTER TABLE takes an exclusive lock even with IF NOT EXISTS, and waits for
    every open transaction on the table first. During a deploy the old gateway can still be draining
    a long streamed request with a transaction open on users, and every new start then hung at
    "Waiting for application startup" behind it - the rollback too. A change that is still needed
    gives up after lock_timeout instead, and the start fails where it can be seen.
    """
    async with engine().begin() as connection:
        await connection.execute(text("SET LOCAL lock_timeout = '20s'"))
        present = {(table, column) for table, column in (await connection.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema()"))).all()}
        for table, column, kind in _ADDED_COLUMNS:
            if (table, column) not in present:
                await connection.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}"))
        # Conversations from before they had a uid get one, and uids stay unique
        if (await connection.execute(text("SELECT 1 FROM conversations WHERE uid IS NULL LIMIT 1"))).first():
            await connection.execute(text("UPDATE conversations SET uid = gen_random_uuid()::text WHERE uid IS NULL"))
        if not (await connection.execute(text(
                "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = 'conversations_uid'"))).first():
            await connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS conversations_uid ON conversations (uid)"))
        # One running summary per conversation, whatever races to write it
        has_index = (await connection.execute(text(
            "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname = 'memory_one_per_conversation'"))).first()
        if not has_index:
            await connection.execute(text("DELETE FROM memory_entries WHERE scope = 'conversation' AND id NOT IN "
                                          "(SELECT min(id) FROM memory_entries WHERE scope = 'conversation' "
                                          "GROUP BY conversation_id)"))
            await connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS memory_one_per_conversation "
                                          "ON memory_entries (conversation_id) WHERE scope = 'conversation'"))


async def ensure_default_admin():
    name = settings.get("gateway.admin.user", "GATEWAY_ADMIN_USER") or "admin"
    async with session_factory()() as session:
        existing = (await session.execute(select(User).where(User.name == name))).scalar_one_or_none()
        if existing and existing.password_hash:
            return
        password = settings.get("gateway.admin.password", "GATEWAY_ADMIN_PASSWORD")
        generated = not password
        if generated:
            password = secrets.token_urlsafe(12)
        if existing:
            existing.password_hash = hash_password(password)
            existing.role = "admin"
        else:
            session.add(User(name=name, role="admin", password_hash=hash_password(password)))
        await session.commit()
    if generated:
        log.warning("Created admin account '%s' with generated password: %s", name, password)
        log.warning("Set gateway.admin.password in config/config.properties to choose your own.")
    else:
        log.info("Admin account '%s' ready (password from configuration)", name)
