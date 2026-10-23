"""Async database engine and session management."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

CONNECTION_TIMEOUTS = (
    "-c lock_timeout=5s -c statement_timeout=30s -c idle_in_transaction_session_timeout=60s"
)

engine = create_async_engine(
    settings.database_url,
    connect_args={"options": CONNECTION_TIMEOUTS},
)

SessionLocal = async_sessionmaker[AsyncSession](engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """Yield a database session, closing on request completion."""
    async with SessionLocal() as session:
        yield session
