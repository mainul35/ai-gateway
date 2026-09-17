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
