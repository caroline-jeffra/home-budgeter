"""Tests for the append-only trigger on transactions."""

from datetime import date

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest import import_rows
from app.models import CategorizedBy, Transaction
from tests.factories import make_account, make_category, make_import_batch
from tests.test_ingest import HEADER, TRTP_LINE


async def _imported_row(session: AsyncSession) -> Transaction:
    """Imports one row, so every protected column has a value."""
    account = await make_account(session)
    batch = await make_import_batch(session, account)
    await import_rows(session, account, [HEADER, TRTP_LINE], import_batch_id=batch.id)
    return (await session.scalars(select(Transaction))).one()


async def _open_void_gate(session: AsyncSession, batch_id: int) -> None:
    """Lets this transaction delete the rows of one import batch."""
    await session.execute(select(func.set_config("app.void_batch_id", str(batch_id), True)))


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


async def test_a_no_op_update_is_allowed(session: AsyncSession) -> None:
    row = await _imported_row(session)

    await session.execute(
        text("UPDATE transactions SET amount_cents = amount_cents WHERE id = :id"),
        {"id": row.id},
    )

    amount = await session.scalar(select(Transaction.amount_cents).where(Transaction.id == row.id))
    assert amount == 15000


async def test_categorization_and_description_stay_mutable(session: AsyncSession) -> None:
    row = await _imported_row(session)
    category = await make_category(session)
    row.category_id = category.id
    row.categorized_by = CategorizedBy.MANUAL
    row.description = "albert heijn"
    row.normalizer_version += 1

    await session.flush()


async def test_a_transaction_cannot_be_deleted(session: AsyncSession) -> None:
    row = await _imported_row(session)

    with pytest.raises(IntegrityError, match="append-only"):
        await session.execute(delete(Transaction).where(Transaction.id == row.id))


async def test_the_void_gate_deletes_only_its_batch(session: AsyncSession) -> None:
    row = await _imported_row(session)
    assert row.import_batch_id is not None
    await _open_void_gate(session, row.import_batch_id)

    await session.execute(delete(Transaction).where(Transaction.id == row.id))

    remaining = await session.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.account_id == row.account_id)
    )
    assert remaining == 0


async def test_the_void_gate_does_not_open_other_batches(session: AsyncSession) -> None:
    row = await _imported_row(session)
    other_batch = await make_import_batch(session)
    await _open_void_gate(session, other_batch.id)

    with pytest.raises(IntegrityError, match="append-only"):
        await session.execute(delete(Transaction).where(Transaction.id == row.id))
