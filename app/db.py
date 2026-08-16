"""Async database engine and session management."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url)

SessionLocal = async_sessionmaker[AsyncSession](engine, expire_on_commit=False)

async def get_session() -> AsyncGenerator[AsyncSession]:
    """Yield a database session, closing on request completion."""
    async with SessionLocal() as session:
        yield session
