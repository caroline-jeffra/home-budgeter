"""Proves the factories build valid rows inside the test transaction."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Transaction
from tests.factories import make_category, make_split, make_transaction


async def test_factory_row_is_visible_in_the_session(session: AsyncSession) -> None:
    """A flushed row is queryable without a commit."""
    await make_transaction(session)
    count = await session.scalar(select(func.count()).select_from(Transaction))
    assert count == 1


async def test_repeated_calls_do_not_collide_on_unique_columns(
    session: AsyncSession,
) -> None:
    """Two categories in one test must not raise IntegrityError on `name`."""
    first = await make_category(session)
    second = await make_category(session)
    assert first.name != second.name


async def test_split_defaults_to_the_full_transaction_amount(
    session: AsyncSession,
) -> None:
    transaction = await make_transaction(session, amount_cents=-9_999)
    split = await make_split(session, transaction)
    assert split.amount_cents == transaction.amount_cents


async def test_factory_rows_do_not_survive(session: AsyncSession) -> None:
    """Rows from the tests above must not be visible here."""
    count = await session.scalar(select(func.count()).select_from(Transaction))
    assert count == 0
