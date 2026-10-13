"""Tests for the balance chain and where it breaks."""

from datetime import date
from unittest.mock import ANY

from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.continuity import (
    ChainBreak,
    ContinuityReport,
    chain_breaks,
    classify,
    overlap_window,
)
from app.ingest import import_rows
from app.models import Account, ImportBatch, Transaction
from tests.factories import make_account, make_import_batch, make_transaction
from tests.test_ingest import AUTH, BEA_LINE, HEADER, TRTP_LINE, _csv_upload

# Continues TRTP_LINE: opens at its closing balance, 236.04.
NEXT_LINE = (
    "12345678,EUR,20260809,20260809,236.04,226.04,-10.00,"
    '"BEA, Google Pay  ALBERT HEIJN 1234,PAS410"'
)

# TRTP_LINE continues this one: closes at its opening balance, 86.04
OPENING_LINE = (
    "12345678,EUR,20260807,20260807,96.04,86.04,-10.00,"
    '"BEA, Google Pay  ALBERT HEIJN 1234,PAS410"'
)

# The bank restates 08-08's +150.00 as +140.00: same day, same opening balance.
RESTATED_TRTP_LINE = TRTP_LINE.replace("236.04,150.00", "226.04,140.00")


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


def _brk(transaction_id: int, day: date) -> ChainBreak:
    """A break with throwaway balances; classify() only reads the id and date."""
    return ChainBreak(transaction_id, day, 1, 0, 1)


def test_a_new_break_on_the_overlap_edge_is_a_conflict() -> None:
    day = date(2026, 8, 8)
    brk = _brk(1, day)

    assert classify([], [brk], (day, day)) == ContinuityReport([], [], [brk])


def test_a_break_that_persists_is_neither_opened_nor_closed() -> None:
    brk = _brk(1, date(2026, 8, 8))

    assert classify([brk], [brk], None) == ContinuityReport([], [], [])


async def test_filling_a_hole_has_no_overlap(session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    # Stored rows on 08-07 and 08-09; the batch brings the missing 08-08.
    await _import(session, account, [HEADER, BEA_LINE, NEXT_LINE])
    second = await _import(session, account, [HEADER, TRTP_LINE])

    assert await overlap_window(session, account.id, second.id) is None


async def test_overlap_is_the_span_of_stored_rows_inside_the_batch(
    session: AsyncSession,
) -> None:
    account = await make_account(session, bank_name="abn_amro")
    await _import(session, account, [HEADER, TRTP_LINE])
    # The batch spans 08-07 to 08-09, but only 08-08 was already stored.
    second = await _import(session, account, [HEADER, BEA_LINE, TRTP_LINE, NEXT_LINE])

    assert await overlap_window(session, account.id, second.id) == (
        date(2026, 8, 8),
        date(2026, 8, 8),
    )


async def _upload(client: AsyncClient, account_id: int, lines: list[str]) -> Response:
    """Posts CSV lines to the import endpoint, which commits."""
    return await client.post(
        f"/accounts/{account_id}/import", files=_csv_upload(lines), headers=AUTH
    )


async def test_import_reports_a_gap_it_opens(client: AsyncClient, session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    await _upload(client, account.id, [HEADER, BEA_LINE])
    # Nothing links 08-07 (closes 190.60) to 08-09 (opens 236.04).
    second = await _upload(client, account.id, [HEADER, NEXT_LINE])
    
    assert second.status_code == 200
    assert second.json()["gaps_opened"] == [
       {
            "transaction_id": ANY,
            "booked_on": "2026-08-09",
            "import_batch_id": second.json()["batch_id"],
            "expected_before_cents": 19060,
            "actual_before_cents": 23604,
        }
    ]
    assert second.json()["gaps_closed"] == []


async def test_import_reports_a_gap_it_closes(client: AsyncClient, session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    # TRTP is missing between OPENING (closes 86.04) and NEXT (opens 236.04).
    first = await _upload(client, account.id, [HEADER, OPENING_LINE, NEXT_LINE])
    second = await _upload(client, account.id, [HEADER, TRTP_LINE])

    assert len(first.json()["gaps_opened"]) == 1
    assert second.json()["gaps_closed"] == first.json()["gaps_opened"]
    assert second.json()["gaps_opened"] == []


async def test_a_restated_row_is_a_conflict_and_writes_nothing(
        client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session, bank_name="abn_amro")

    account_id = account.id
    first = await _upload(client, account_id, [HEADER, TRTP_LINE, NEXT_LINE])
    assert first.status_code == 200
    second = await _upload(client, account_id, [HEADER, RESTATED_TRTP_LINE])

    assert second.status_code == 409
    assert second.json()["detail"]["conflicts"] == [
        {"booked_on": "2026-08-08", "expected_before_cents": 23604, "actual_before_cents": 8604}
    ]
    transactions = await session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account_id)
    )
    batches = await session.scalar(
        select(func.count()).select_from(ImportBatch).where(ImportBatch.account_id == account_id)
    )
    assert (transactions, batches) == (2, 1)


async def test_reconciliation_unknown_account_is_404(client: AsyncClient) -> None:
    response = await client.get("/accounts/999999/reconciliation", headers=AUTH)

    assert response.status_code == 404


async def test_reconciliation_lists_the_accounts_gaps(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session, bank_name="abn_amro")
    await _upload(client, account.id, [HEADER, BEA_LINE])
    second = await _upload(client, account.id, [HEADER, NEXT_LINE])

    response = await client.get(f"/accounts/{account.id}/reconciliation", headers=AUTH)

    assert response.json() == {
        "account_id": account.id,
        "continuous": False,
        "gaps": second.json()["gaps_opened"],
    }


async def test_reconciliation_of_a_continuous_account(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session, bank_name="abn_amro")
    await _upload(client, account.id, [HEADER, TRTP_LINE, NEXT_LINE])

    response = await client.get(f"/accounts/{account.id}/reconciliation", headers=AUTH)

    assert response.json() == {"account_id": account.id, "continuous": True, "gaps": []}
