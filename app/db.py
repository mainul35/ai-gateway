"""Database engine and session handling."""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import settings
from app.models import Base

_engine = None
_session_factory = None


def engine():
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(settings.database_url(), pool_pre_ping=True, future=True)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def session_factory():
    engine()
    return _session_factory


async def create_tables():
    async with engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory()() as session:
        yield session


async def dispose():
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
