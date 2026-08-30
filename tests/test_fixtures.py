"""Proves the rollback fixture actually rolls back."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import engine
from app.models import Account


async def test_write_a_row(session: AsyncSession) -> None:
    """Writes a row and commits it. The commit must not escape the test."""
    session.add(Account(name="Rollback probe"))
    await session.commit()
    count = await session.scalar(select(func.count()).select_from(Account))
    assert count == 1


async def test_previous_row_is_gone(session: AsyncSession) -> None:
    """The row from the previous test must not be visible here."""
    count = await session.scalar(select(func.count()).select_from(Account))
    assert count == 0


async def test_commit_does_not_escape_the_transaction(session: AsyncSession) -> None:
    """A commit inside a test must stay invisible to any other connection."""
    session.add(Account(name="Escape probe"))
    await session.commit()

    async with engine.connect() as outside:
        visible = await outside.scalar(select(func.count()).select_from(Account))

    assert visible == 0, "commit escaped the test transaction."
