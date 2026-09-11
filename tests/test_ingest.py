"""Tests for the CSV ingestion pipeline: parse, normalize, dedup, persist."""

import io
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import main
from app.config import settings
from app.ingest import (
    ABN_AMRO,
    ImportSummary,
    ParsedRow,
    dedup,
    import_rows,
    normalize,
    parse,
)
from app.models import Transaction
from tests.factories import make_account

AUTH = {"Authorization": f"Bearer {settings.api_token}"}

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

def _csv_upload(
    lines: list[str], name: str = "export.csv"
) -> dict[str, tuple[str, io.BytesIO, str]]:
    """Builds a multipart file payload from CSV lines."""
    body = "\r\n".join(lines).encode("utf-8")
    return {"file": (name, io.BytesIO(body), "text/csv")}


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
    summary = await import_rows(session, account, TWO_ROWS)
    assert summary == ImportSummary(rows_read=2, inserted=2)

    stored = (await session.scalars(select(Transaction))).all()
    assert len(stored) == 2
    assert {t.amount_cents for t in stored} == {-655, 15000}


async def test_reimport_is_a_no_op(session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    assert await import_rows(session, account, TWO_ROWS) == ImportSummary(rows_read=2, inserted=2)
    assert await import_rows(session, account, TWO_ROWS) == ImportSummary(rows_read=2, inserted=0)

    count = await session.scalar(select(func.count()).select_from(Transaction))
    assert count == 2


async def test_overlapping_ranges_import_only_new_rows(
    session: AsyncSession
) -> None:
    account = await make_account(session, bank_name="abn_amro")
    assert await import_rows(session, account, [HEADER, BEA_LINE]) == ImportSummary(
        rows_read=1, inserted=1
    )

    # Second export overlaps the first: BEA_LINE again, plus one new row.
    assert await import_rows(session, account, TWO_ROWS) == ImportSummary(rows_read=2, inserted=1)

    stored = (await session.scalars(select(Transaction))).all()
    assert len(stored) == 2
    assert {t.amount_cents for t in stored} == {-655, 15000}


async def test_import_endpoint_inserts_rows(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A posted export lands as transactions, and the response counts them."""
    account = await make_account(session, bank_name="abn_amro")

    response = await client.post(
        f"/accounts/{account.id}/import",
        files=_csv_upload(TWO_ROWS),
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json() == {
        "account_id": account.id,
        "rows_read": 2,
        "inserted": 2,
        "skipped": 0,
    }

    count = await session.scalar(
        select(func.count()).select_from(Transaction).where(
            Transaction.account_id == account.id
        )
    )
    assert count == 2


async def test_import_endpoint_is_idempotent(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Re-importing the same export inserts nothing the second time."""
    account = await make_account(session, bank_name="abn_amro")

    first = await client.post(
        f"/accounts/{account.id}/import",
        files=_csv_upload(TWO_ROWS),
        headers=AUTH,
    )
    second = await client.post(
        f"/accounts/{account.id}/import",
        files=_csv_upload(TWO_ROWS),
        headers=AUTH,
    )

    assert first.json()["inserted"] == 2
    assert second.json() == {
        "account_id": account.id,
        "rows_read": 2,
        "inserted": 0,
        "skipped": 2,
    }

    count = await session.scalar(
        select(func.count()).select_from(Transaction).where(
            Transaction.account_id == account.id
        )
    )
    assert count == 2


async def test_import_unknown_account_is_404(client: AsyncClient) -> None:
    """Importing into an account that does not exist is a 404, not a 500."""
    response = await client.post(
        "/accounts/999999/import",
        files=_csv_upload(TWO_ROWS),
        headers=AUTH,
    )

    assert response.status_code == 404


async def test_import_malformed_row_persists_nothing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A bad row rejects the whole file. No partial import survives."""
    account = await make_account(session, bank_name="abn_amro")
    lines = [HEADER, BEA_LINE, "not,enough,fields"]

    response = await client.post(
        f"/accounts/{account.id}/import",
        files=_csv_upload(lines),
        headers=AUTH,
    )

    assert response.status_code == 422
    assert "line 3" in response.json()["detail"]

    count = await session.scalar(
        select(func.count()).select_from(Transaction).where(
            Transaction.account_id == account.id
        )
    )
    assert count == 0


async def test_import_oversized_upload_is_413(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body past the cap is refused before it is parsed."""
    account = await make_account(session, bank_name="abn_amro")
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 50)

    response = await client.post(
        f"/accounts/{account.id}/import",
        files=_csv_upload(TWO_ROWS),
        headers=AUTH,
    )

    assert response.status_code == 413
