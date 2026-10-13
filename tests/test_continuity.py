"""Tests for the balance chain and where it breaks."""

from datetime import date
from unittest.mock import ANY

from sqlalchemy.ext.asyncio import AsyncSession

from app.continuity import ChainBreak, chain_breaks
from app.ingest import import_rows
from app.models import Account, ImportBatch
from tests.factories import make_account, make_import_batch, make_transaction
from tests.test_ingest import BEA_LINE, HEADER, TRTP_LINE

# Continues TRTP_LINE: opens at its closing balance, 236.04.
NEXT_LINE = (
    "12345678,EUR,20260809,20260809,236.04,226.04,-10.00,"
    '"BEA, Google Pay  ALBERT HEIJN 1234,PAS410"'
)


async def _import(session: AsyncSession, account: Account, lines: list[str]) -> ImportBatch:
    """Imports CSV lines into a fresh batch for the account."""
    batch = await make_import_batch(session, account)
    await import_rows(session, account, lines, import_batch_id=batch.id)
    return batch


async def test_a_continuous_chain_has_no_breaks(session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    await _import(session, account, [HEADER, TRTP_LINE, NEXT_LINE])

    assert await chain_breaks(session, account.id) == []


async def test_a_break_is_reported_at_the_row_that_does_not_continue(
    session: AsyncSession,
) -> None:
    account = await make_account(session, bank_name="abn_amro")
    # BEA closes at 190.60; TRTP opens at 86.04.
    batch = await _import(session, account, [HEADER, BEA_LINE, TRTP_LINE])

    assert await chain_breaks(session, account.id) == [
        ChainBreak(ANY, date(2026, 8, 8), batch.id, 19060, 8604)
    ]


async def test_chain_follows_booking_date_not_import_order(session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    # The later row is imported first.
    await _import(session, account, [HEADER, NEXT_LINE])
    await _import(session, account, [HEADER, TRTP_LINE])

    assert await chain_breaks(session, account.id) == []


async def test_manual_transactions_do_not_hide_a_break(session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    batch = await _import(session, account, [HEADER, BEA_LINE, TRTP_LINE])
    # No balance and no batch: without the filter it sorts between BEA and TRTP.
    await make_transaction(session, account, booked_on=date(2026, 8, 7))

    assert await chain_breaks(session, account.id) == [
        ChainBreak(ANY, date(2026, 8, 8), batch.id, 19060, 8604)
    ]


async def test_breaks_are_scoped_to_one_account(session: AsyncSession) -> None:
    a = await make_account(session, bank_name="abn_amro")
    b = await make_account(session, bank_name="abn_amro")
    await _import(session, a, [HEADER, TRTP_LINE, NEXT_LINE])
    # On its own this row is fine; mixed into A's chain it breaks before TRTP.
    await _import(session, b, [HEADER, BEA_LINE])

    assert await chain_breaks(session, a.id) == []
