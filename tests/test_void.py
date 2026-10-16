"""Tests for voiding an import batch."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import build_parser
from app.ingest import import_rows
from app.models import Account, ImportBatch, Transaction, TransactionSplit, TransferMatch
from app.void import void_batch
from tests.factories import make_account, make_import_batch, make_split, make_transfer_match
from tests.test_ingest import BEA_LINE, HEADER, TRTP_LINE


async def _imported_batch(
    session: AsyncSession, account: Account, line: str = TRTP_LINE
) -> ImportBatch:
    """Imports one row into a new batch on the account."""
    batch = await make_import_batch(session, account, raw_bytes=b"raw export")
    await import_rows(session, account, [HEADER, line], import_batch_id=batch.id)
    return batch


async def _rows_in(session: AsyncSession, batch: ImportBatch) -> list[Transaction]:
    """The transactions still stored for a batch."""
    stmt = select(Transaction).where(Transaction.import_batch_id == batch.id)
    return list((await session.scalars(stmt)).all())


async def test_void_deletes_the_batch_rows_and_keeps_the_batch(session: AsyncSession) -> None:
    account = await make_account(session)
    batch = await _imported_batch(session, account)

    deleted = await void_batch(session, batch.id, "  bank restated 08-08  ")

    assert deleted == 1
    assert await _rows_in(session, batch) == []
    await session.refresh(batch, ["voided_at", "void_reason", "raw_bytes"])
    assert batch.raw_bytes == b"raw export"
    assert batch.voided_at is not None
    assert batch.void_reason == "bank restated 08-08"


async def test_void_leaves_other_batches_alone(session: AsyncSession) -> None:
    account = await make_account(session)
    voided = await _imported_batch(session, account, TRTP_LINE)
    kept = await _imported_batch(session, account, BEA_LINE)

    await void_batch(session, voided.id, "bad export")

    assert len(await _rows_in(session, kept)) == 1


async def test_void_rejects_an_unknown_batch(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="no import batch"):
        await void_batch(session, 999_999, "typo")


async def test_void_rejects_a_voided_batch(session: AsyncSession) -> None:
    account = await make_account(session)
    batch = await _imported_batch(session, account)
    await void_batch(session, batch.id, "first reason")

    with pytest.raises(ValueError, match="already voided"):
        await void_batch(session, batch.id, "second reason")

    await session.refresh(batch, ["void_reason"])
    assert batch.void_reason == "first reason"


@pytest.mark.parametrize("reason", ["", "   "])
async def test_void_rejects_a_blank_reason(session: AsyncSession, reason: str) -> None:
    account = await make_account(session)
    batch = await _imported_batch(session, account)

    with pytest.raises(ValueError, match="needs a reason"):
        await void_batch(session, batch.id, reason)


async def test_void_deletes_splits_first(session: AsyncSession) -> None:
    account = await make_account(session)
    batch = await _imported_batch(session, account)
    [row] = await _rows_in(session, batch)
    split = await make_split(session, row)

    await void_batch(session, batch.id, "bad export")

    stmt = select(TransactionSplit).where(TransactionSplit.id == split.id)
    assert await session.scalar(stmt) is None


async def test_void_deletes_transfer_matches_first(session: AsyncSession) -> None:
    account = await make_account(session)
    batch = await _imported_batch(session, account)
    [row] = await _rows_in(session, batch)
    match = await make_transfer_match(session, source=row)

    await void_batch(session, batch.id, "bad export")

    stmt = select(TransferMatch).where(TransferMatch.id == match.id)
    assert await session.scalar(stmt) is None


def test_void_requires_a_reason() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["void", "1"])
