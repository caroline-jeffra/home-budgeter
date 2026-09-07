"""Tests for the CSV ingestion pipeline: parse, normalize, dedup, persist."""

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest import ABN_AMRO, ParsedRow, dedup, import_rows, normalize, parse
from app.models import Transaction
from tests.factories import make_account

HEADER = (
    "accountNumber,mutationcode,transactiondate,valuedate,startsaldo,endsaldo,amount,description"
)

# A quoted BEA line: commas inside the description, and run-on padding.
BEA_LINE = (
    '12345678,EUR,20260807,20260807,197.15,190.60,-6.55,'
    '"BEA, Google Pay                  BCK*Kiosk HN 2102,PAS410        '
    'NR:BS011691, 07.08.26/12:17      AMSTERDAM NH"'
)

# An unquoted /TRTP/ line, and an incoming amount (positive).
TRTP_LINE = (
    "12345678,EUR,20260808,20260808,86.04,236.04,150.00,"
    "/TRTP/SEPA OVERBOEKING/IBAN/NL12ABNA1234567890/BIC/ABNANL2A/NAME/RL STINE RL/EREF/NOTPROVIDED"
)

TWO_ROWS = [HEADER, BEA_LINE, TRTP_LINE]

def test_parse_reads_a_quoted_row() -> None:
    (row,) = parse([HEADER, BEA_LINE], ABN_AMRO)
    assert row.booked_on == date(2026, 8, 7)
    assert row.amount_cents == -655
    assert row.balance_after_cents == 19060
    assert row.description.startswith("BEA, Google Pay")
    assert "PAS410" in row.description


def test_parse_rejects_a_short_row() -> None:
    with pytest.raises(ValueError, match="line 2"):
        list(parse([HEADER, "12345678,EUR,20260807"], ABN_AMRO))


def test_parse_is_lazy() -> None:
    """Passes a malformed third line. If it is reached, the parse is not lazy."""
    rows = parse([HEADER, BEA_LINE, "12345678,BROKEN,20260807"], ABN_AMRO)
    first = next(rows)
    assert first.amount_cents == -655


def test_normalize_collapses_whitespace() -> None:
    (raw,) = parse([HEADER, BEA_LINE], ABN_AMRO)
    (clean,) = normalize(iter([raw]))
    assert "  " not in clean.description
    assert clean.description == (
        "BEA, Google Pay BCK*Kiosk HN 2102,PAS410 NR:BS011691, 07.08.26/12:17 AMSTERDAM NH"
    )
    assert clean.amount_cents == raw.amount_cents


def test_dedup_drops_known_and_in_file_duplicates() -> None:
    a = ParsedRow(date(2026, 8, 7), -655, 19060, "a")
    b = ParsedRow(date(2026, 8, 7), -2790, 16270, "b")
    a_again = ParsedRow(date(2026, 8, 7), -655, 19060, "a restated")

    kept = list(dedup(iter([a, b, a_again]), {b.dedup_key}))

    assert [r.description for r in kept] == ["a"]


async def test_import_inserts_rows(session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    inserted = await import_rows(session, account, TWO_ROWS)
    assert inserted == 2

    stored = (await session.scalars(select(Transaction))).all()
    assert len(stored) == 2
    assert {t.amount_cents for t in stored} == {-655, 15000}


async def test_reimport_is_a_no_op(session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    assert await import_rows(session, account, TWO_ROWS) == 2
    assert await import_rows(session, account, TWO_ROWS) == 0

    count = await session.scalar(select(func.count()).select_from(Transaction))
    assert count == 2


async def test_overlapping_ranges_import_only_new_rows(
    session: AsyncSession
) -> None:
    account = await make_account(session, bank_name="abn_amro")
    assert await import_rows(session, account, [HEADER, BEA_LINE]) == 1

    # Second export overlaps the first: BEA_LINE again, plus one new row.
    assert await import_rows(session, account, TWO_ROWS) == 1

    stored = (await session.scalars(select(Transaction))).all()
    assert len(stored) == 2
    assert {t.amount_cents for t in stored} == {-655, 15000}


async def test_unknown_bank_name_raises(session: AsyncSession) -> None:
    account = await make_account(session)
    with pytest.raises(ValueError, match="example bank"):
        await import_rows(session, account, TWO_ROWS)
