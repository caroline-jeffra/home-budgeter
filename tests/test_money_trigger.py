"""Tests for the append-only trigger on transactions."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest import import_rows
from app.models import Transaction
from tests.factories import make_account, make_import_batch
from tests.test_ingest import HEADER, TRTP_LINE


async def _imported_row(session: AsyncSession) -> Transaction:
    """Imports one row, so every protected column has a value."""
    account = await make_account(session)
    batch = await make_import_batch(session, account)
    await import_rows(session, account, [HEADER, TRTP_LINE], import_batch_id=batch.id)
    return (await session.scalars(select(Transaction))).one()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("booked_on", date(2026, 1, 1)),
        ("amount_cents", 1),
        ("balance_before_cents", 1),
        ("balance_after_cents", 1),
        ("raw_description", "rewritten"),
        ("line_no", 99),
        ("import_batch_id", None),
    ],
)
async def test_a_protected_column_cannot_change(
    session: AsyncSession, column: str, value: object
) -> None:
    row = await _imported_row(session)
    setattr(row, column, value)

    with pytest.raises(IntegrityError, match="append-only"):
        await session.flush()


async def test_account_id_cannot_change(session: AsyncSession) -> None:
    row = await _imported_row(session)
    other = await make_account(session)
    row.account_id = other.id

    with pytest.raises(IntegrityError, match="append-only"):
        await session.flush()
