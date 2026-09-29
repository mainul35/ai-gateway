"""Creates the built-in admin account on first start, so there is always a way in before SSO works."""
import logging
import secrets

from sqlalchemy import select, text

from app import settings
from app.db import engine, session_factory
from app.models import User
from app.security import hash_password

log = logging.getLogger("gateway")


async def ensure_schema():
    """Adds columns introduced after the first release; create_all does not alter existing tables."""
    async with engine().begin() as connection:
        await connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(256)"))
        # Existing accounts keep the access they effectively had before access control existed
        await connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS model_access VARCHAR(16) DEFAULT 'all'"))
        await connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS allowed_models TEXT"))
        await connection.execute(text("ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS attachments TEXT"))
        # One running summary per conversation, whatever races to write it
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
