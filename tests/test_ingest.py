"""Tests for the CSV ingestion pipeline: parse, normalize, dedup, persist."""

import hashlib
import io
from dataclasses import replace
from datetime import date
from functools import partial
from unittest.mock import ANY

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import main
from app.config import settings
from app.db import engine
from app.ingest import (
    ABN_AMRO,
    NOISE_WORDS,
    ImportProfile,
    ImportSummary,
    ParsedRow,
    assign_occurrences,
    dedup,
    import_rows,
    normalize,
    normalize_text,
    open_text,
    parse,
    parse_cents,
)
from app.models import ImportBatch, Transaction
from app.void import void_batch
from tests.factories import make_account, make_import_batch

AUTH = {"Authorization": f"Bearer {settings.api_token}"}

HEADER = (
    "accountNumber,mutationcode,transactiondate,valuedate,startsaldo,endsaldo,amount,description"
)

# A quoted BEA line: commas inside the description, and run-on padding.
BEA_LINE = (
    "12345678,EUR,20260807,20260807,197.15,190.60,-6.55,"
    '"BEA, Google Pay                  BCK*Kiosk HN 2102,PAS410        '
    'NR:BS011691, 07.08.26/12:17      AMSTERDAM NH"'
)

# An unquoted /TRTP/ line, and an incoming amount (positive).
TRTP_LINE = (
    "12345678,EUR,20260808,20260808,86.04,236.04,150.00,"
    "/TRTP/SEPA OVERBOEKING/IBAN/NL12ABNA1234567890/BIC/ABNANL2A/NAME/RL STINE RL/EREF/NOTPROVIDED"
)

TWO_ROWS = [HEADER, BEA_LINE, TRTP_LINE]

# A storno and re-collection: the balance runs 100 -> 50 twice on one day.
REVISIT_ROWS = [
    HEADER,
    "12345678,EUR,20260809,20260809,100.00,50.00,-50.00,Incasso Gym",
    "12345678,EUR,20260809,20260809,50.00,100.00,50.00,Storno Gym",
    "12345678,EUR,20260809,20260809,100.00,50.00,-50.00,Incasso Gym",
]


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


def test_parse_records_line_no_and_opening_balance() -> None:
    bea, trtp = parse(TWO_ROWS, ABN_AMRO)
    assert (bea.line_no, bea.balance_before_cents) == (2, 19715)
    assert (trtp.line_no, trtp.balance_before_cents) == (3, 8604)


def test_parse_rejects_a_broken_balance() -> None:
    """A row whose startsaldo + amount != endsaldo fails at parse time."""
    broken = BEA_LINE.replace("190.60", "190.61")
    with pytest.raises(ValueError, match=r"line 2.*endsaldo"):
        list(parse([HEADER, broken], ABN_AMRO))


def test_parse_rejects_a_short_row() -> None:
    with pytest.raises(ValueError, match="line 2"):
        list(parse([HEADER, "12345678,EUR,20260807"], ABN_AMRO))


# A test-only variant in the Dutch style: `1.234,56`, semicolon-delimited (P2-D6).
DUTCH_STYLE = replace(ABN_AMRO, delimiter=";", decimal_separator=",", thousands_separator=".")


@pytest.mark.parametrize(
    ("profile", "raw", "cents"),
    [
        (ABN_AMRO, "1234.56", 123456),
        (ABN_AMRO, "-6.55", -655),
        (ABN_AMRO, "+150.00", 15000),
        (ABN_AMRO, "150", 15000),
        (ABN_AMRO, "0.5", 50),
        (ABN_AMRO, " 12.30 ", 1230),
        (DUTCH_STYLE, "1.234,56", 123456),
        (DUTCH_STYLE, "-1.234.567,89", -123456789),
        (DUTCH_STYLE, "1234,56", 123456),
        (DUTCH_STYLE, "999,99", 99999),
    ],
)
def test_parse_cents_reads_the_profile_format(profile: ImportProfile, raw: str, cents: int) -> None:
    assert parse_cents(raw, profile) == cents


@pytest.mark.parametrize(
    ("profile", "raw"),
    [
        # Dot-decimal under a comma-decimal profile: stripping '.' would read 100x too much.
        (DUTCH_STYLE, "1234.56"),
        # Sub-cent precision: truncating would silently drop money.
        (ABN_AMRO, "12.345"),
        # Grouping in a profile with no thousands separator.
        (ABN_AMRO, "1,234.56"),
        # Groups must be exactly three digits.
        (DUTCH_STYLE, "1.23,45"),
        (DUTCH_STYLE, "1.2345,67"),
        (ABN_AMRO, ""),
        (ABN_AMRO, "12.34EUR"),
    ],
)
def test_parse_cents_rejects_a_foreign_format(profile: ImportProfile, raw: str) -> None:
    with pytest.raises(ValueError, match=repr(raw)):
        parse_cents(raw, profile)


def test_parse_reads_a_dutch_style_row() -> None:
    header = HEADER.replace(",", ";")
    line = "12345678;EUR;20260807;20260807;1.197,15;1.190,60;-6,55;Kiosk, Amsterdam"
    [row] = parse([header, line], DUTCH_STYLE)
    assert (row.balance_before_cents, row.amount_cents, row.balance_after_cents) == (
        119715,
        -655,
        119060,
    )
    assert row.raw_description == "Kiosk, Amsterdam"


def test_parse_reports_a_foreign_amount_with_its_line() -> None:
    """A dot-decimal export under a comma-decimal profile is a 422-able error, not a 100x amount."""
    line = "12345678;EUR;20260807;20260807;197.15;190.60;-6.55;Kiosk"
    with pytest.raises(ValueError, match="line 2"):
        list(parse([HEADER.replace(",", ";"), line], DUTCH_STYLE))


def test_parse_is_lazy() -> None:
    """Passes a malformed third line. If it is reached, the parse is not lazy."""
    rows = parse([HEADER, BEA_LINE, "12345678,BROKEN,20260807"], ABN_AMRO)
    first = next(rows)
    assert first.amount_cents == -655


def test_open_text_strips_the_bom() -> None:
    raw = ("\ufeff" + "\r\n".join(TWO_ROWS)).encode("utf-8")
    assert next(open_text(raw, ABN_AMRO)).startswith("accountNumber")


def test_open_text_prefers_utf8() -> None:
    """UTF-8 is tried first: read as cp1252, 'café' would garble to 'cafÃ©' without an error."""
    assert next(open_text("café".encode(), ABN_AMRO)) == "café"


def test_open_text_falls_back_to_the_profile_encoding() -> None:
    assert next(open_text("café".encode("cp1252"), ABN_AMRO)) == "café"


def test_open_text_without_a_fallback_rejects_non_utf8() -> None:
    profile = replace(ABN_AMRO, fallback_encoding=None)
    with pytest.raises(UnicodeDecodeError):
        open_text("café".encode("cp1252"), profile)


def test_open_text_rejects_bytes_neither_encoding_decodes() -> None:
    """0x81 is a stray UTF-8 continuation byte and unassigned in cp1252."""
    with pytest.raises(UnicodeDecodeError):
        open_text(b"caf\x81", ABN_AMRO)


def test_parse_reads_a_quoted_newline_from_open_text() -> None:
    line = '12345678,EUR,20260809,20260809,100.00,50.00,-50.00,"Incasso\nGym"'
    raw = "\r\n".join([HEADER, line]).encode("utf-8")
    rows = list(parse(open_text(raw, ABN_AMRO), ABN_AMRO))
    assert [row.raw_description for row in rows] == ["Incasso\nGym"]


def test_normalize_collapses_whitespace() -> None:
    (raw,) = parse([HEADER, BEA_LINE], ABN_AMRO)
    (clean,) = normalize(iter([raw]))
    assert "  " not in clean.description
    assert clean.description == ("google pay bck*kiosk hn pas410 amsterdam nh")
    assert clean.raw_description == raw.raw_description


def test_normalize_text_composes_to_nfc() -> None:
    decomposed = "Cafe\u0301"
    assert normalize_text(decomposed) == "caf\u00e9"


def test_normalize_text_casefolds_not_lowercases() -> None:
    assert normalize_text("STRAßE") == "strasse"


def test_normalize_text_collapses_unicode_whitespace() -> None:
    assert normalize_text("ALBERT\u00a0 HEIJN \t") == "albert heijn"


def test_normalize_text_splits_on_separators() -> None:
    assert normalize_text("HOORN,PAS613 a:b/c") == "hoorn pas613 a b c"


def test_normalize_text_drops_noise_words() -> None:
    assert normalize_text("SEPA Incasso algemeen doorlopend Naam: Vinted") == "vinted"


def test_normalize_text_keeps_noise_inside_a_word() -> None:
    assert normalize_text("Bearing BV") == "bearing"


def test_noise_words_are_already_normalized() -> None:
    assert all(w == w.casefold() and not set(w) & set(",:/") for w in NOISE_WORDS)


def test_normalize_text_drops_tokens_without_letters() -> None:
    assert normalize_text("Kiosk 07.08.26 12:17 -") == "kiosk"


def test_normalize_text_drops_long_digit_runs() -> None:
    assert normalize_text("NL88ABNA0837494842 Lidl BS011691") == "lidl"


def test_normalize_text_keeps_card_numbers() -> None:
    assert normalize_text("Albert Heijn 2254,PAS490") == "albert heijn pas490"


def test_normalize_text_keeps_short_digits_in_words() -> None:
    assert normalize_text("123inkt.nl 3FM") == "123inkt.nl 3fm"


def test_normalize_reads_raw_description() -> None:
    row = ParsedRow(date(2026, 8, 7), -655, 19060, "stale", "Fresh  TEXT", 19715, 2)
    (clean,) = normalize(iter([row]))
    assert clean.description == "fresh text"
    assert clean.raw_description == "Fresh  TEXT"


def test_dedup_drops_known_and_in_file_duplicates() -> None:
    a = ParsedRow(date(2026, 8, 7), -655, 19060, "a", "a", 19715, 2)
    b = ParsedRow(date(2026, 8, 7), -2790, 16270, "b", "b", 19060, 3)
    a_again = ParsedRow(date(2026, 8, 7), -655, 19060, "a restated", "a restated", 19715, 4)

    kept = list(dedup(iter([a, b, a_again]), {b.dedup_key}))

    assert [r.description for r in kept] == ["a"]


def test_assign_occurrences_numbers_same_key_rows_in_file_order() -> None:
    a = ParsedRow(date(2026, 8, 9), -5000, 5000, "a", "a", 10000, 2)
    b = ParsedRow(date(2026, 8, 9), 5000, 10000, "b", "b", 5000, 3)
    a_again = ParsedRow(date(2026, 8, 9), -5000, 5000, "a", "a", 10000, 4)
    a_next_day = ParsedRow(date(2026, 8, 10), -5000, 5000, "a", "a", 10000, 5)

    rows = list(assign_occurrences(iter([a, b, a_again, a_next_day])))

    assert [r.occurrence for r in rows] == [0, 0, 1, 0]


async def test_import_inserts_rows(session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    batch = await make_import_batch(session, account)
    summary = await import_rows(session, account, TWO_ROWS, import_batch_id=batch.id)
    assert summary == ImportSummary(rows_read=2, inserted=2)

    stored = (await session.scalars(select(Transaction))).all()
    assert len(stored) == 2
    assert {t.amount_cents for t in stored} == {-655, 15000}
    assert {t.import_batch_id for t in stored} == {batch.id}
    assert {(t.line_no, t.balance_before_cents) for t in stored} == {(2, 19715), (3, 8604)}


async def test_reimport_is_a_no_op(session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    first = await make_import_batch(session, account)
    second = await make_import_batch(session, account)
    assert await import_rows(session, account, TWO_ROWS, import_batch_id=first.id) == ImportSummary(
        rows_read=2, inserted=2
    )
    assert await import_rows(
        session, account, TWO_ROWS, import_batch_id=second.id
    ) == ImportSummary(rows_read=2, inserted=0)

    count = await session.scalar(select(func.count()).select_from(Transaction))
    assert count == 2


async def test_overlapping_ranges_import_only_new_rows(session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    first = await make_import_batch(session, account)
    second = await make_import_batch(session, account)
    assert await import_rows(
        session, account, [HEADER, BEA_LINE], import_batch_id=first.id
    ) == ImportSummary(rows_read=1, inserted=1)

    # Second export overlaps the first: BEA_LINE again, plus one new row.
    assert await import_rows(
        session, account, TWO_ROWS, import_batch_id=second.id
    ) == ImportSummary(rows_read=2, inserted=1)

    stored = (await session.scalars(select(Transaction))).all()
    assert len(stored) == 2
    assert {t.amount_cents for t in stored} == {-655, 15000}
    assert {t.import_batch_id for t in stored} == {first.id, second.id}


async def test_same_day_balance_revisit_is_imported(session: AsyncSession) -> None:
    account = await make_account(session, bank_name="abn_amro")
    first = await make_import_batch(session, account)
    second = await make_import_batch(session, account)
    assert await import_rows(
        session, account, REVISIT_ROWS, import_batch_id=first.id
    ) == ImportSummary(rows_read=3, inserted=3)
    assert await import_rows(
        session, account, REVISIT_ROWS, import_batch_id=second.id
    ) == ImportSummary(rows_read=3, inserted=0)

    stored = (await session.scalars(select(Transaction).order_by(Transaction.line_no))).all()
    assert [t.occurrence for t in stored] == [0, 0, 1]


async def test_reimport_matches_across_batches(session: AsyncSession) -> None:
    """Occurrences survive chunking: each one-row batch finds its stored twin."""
    account = await make_account(session, bank_name="abn_amro")
    first = await make_import_batch(session, account)
    second = await make_import_batch(session, account)
    assert await import_rows(
        session, account, REVISIT_ROWS, import_batch_id=first.id, batch_size=1
    ) == ImportSummary(rows_read=3, inserted=3)
    assert await import_rows(
        session, account, REVISIT_ROWS, import_batch_id=second.id, batch_size=1
    ) == ImportSummary(rows_read=3, inserted=0)


async def test_import_endpoint_inserts_rows(client: AsyncClient, session: AsyncSession) -> None:
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
        "batch_id": ANY,
        "already_imported": False,
        "gaps_opened": [
            {
                "transaction_id": ANY,
                "booked_on": "2026-08-08",
                "import_batch_id": ANY,
                "expected_before_cents": 19060,
                "actual_before_cents": 8604,
            }
        ],
        "gaps_closed": [],
        "cosmetic_matches": 0,
        "real_disagreements": 0,
        "disagreements": [],
    }

    count = await session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account.id)
    )
    assert count == 2


async def test_import_endpoint_is_idempotent(client: AsyncClient, session: AsyncSession) -> None:
    """Re-importing the same file returns the original batch and writes nothing."""
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

    assert first.json()["already_imported"] is False
    assert second.json() == {
        "account_id": account.id,
        "rows_read": 2,
        "inserted": 2,
        "skipped": 0,
        "batch_id": first.json()["batch_id"],
        "already_imported": True,
        "gaps_opened": None,
        "gaps_closed": None,
        "cosmetic_matches": 0,
        "real_disagreements": 0,
        "disagreements": None,
    }

    count = await session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account.id)
    )
    assert count == 2

    batches = await session.scalar(select(func.count()).select_from(ImportBatch))
    assert batches == 1


async def test_import_while_account_is_locked_is_409(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A second import for the same account is refused, not queued."""
    account = await make_account(session, bank_name="abn_amro")

    async with engine.connect() as other:
        await other.execute(select(func.pg_advisory_xact_lock(account.id)))
        response = await client.post(
            f"/accounts/{account.id}/import",
            files=_csv_upload(TWO_ROWS),
            headers=AUTH,
        )

    assert response.status_code == 409
    assert "in progress" in response.json()["detail"]
    batches = await session.scalar(select(func.count()).select_from(ImportBatch))
    assert batches == 0


async def test_uploading_a_voided_file_is_a_conflict(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Re-uploading a voided file is refused, naming the reason, instead of a silent no-op."""
    account = await make_account(session, bank_name="abn_amro")
    first = await client.post(
        f"/accounts/{account.id}/import",
        files=_csv_upload(TWO_ROWS),
        headers=AUTH,
    )
    await void_batch(session, first.json()["batch_id"], "bank restated 08-08")

    response = await client.post(
        f"/accounts/{account.id}/import",
        files=_csv_upload(TWO_ROWS),
        headers=AUTH,
    )

    assert response.status_code == 409
    assert "bank restated 08-08" in response.json()["detail"]


async def test_import_records_a_batch(client: AsyncClient, session: AsyncSession) -> None:
    """A successful import stores the file, its hash and counts,
    and every inserted row points at the batch"""

    account = await make_account(session, bank_name="abn_amro")
    body = "\r\n".join(TWO_ROWS).encode("utf-8")

    response = await client.post(
        f"/accounts/{account.id}/import",
        files=_csv_upload(TWO_ROWS),
        headers=AUTH,
    )
    batch_id = response.json()["batch_id"]

    batch = await session.get(ImportBatch, batch_id)
    assert batch is not None
    assert batch.account_id == account.id
    assert batch.filename == "export.csv"
    assert batch.size == len(body)
    assert batch.file_sha256 == hashlib.sha256(body).hexdigest()
    assert (batch.rows_read, batch.inserted, batch.skipped) == (2, 2, 0)

    raw_bytes = await session.scalar(
        select(ImportBatch.raw_bytes).where(ImportBatch.id == batch_id)
    )
    assert raw_bytes == body

    batch_ids = await session.scalars(
        select(Transaction.import_batch_id).where(Transaction.account_id == account.id)
    )
    assert set(batch_ids) == {batch_id}


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
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account.id)
    )
    assert count == 0


async def test_malformed_row_after_a_flush_persists_nothing(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batches already flushed roll back with the bad one."""
    account = await make_account(session, bank_name="abn_amro")
    monkeypatch.setattr(main, "import_rows", partial(import_rows, batch_size=1))
    lines = [HEADER, BEA_LINE, TRTP_LINE, "not,enough,fields"]

    response = await client.post(
        f"/accounts/{account.id}/import",
        files=_csv_upload(lines),
        headers=AUTH,
    )

    assert response.status_code == 422
    count = await session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account.id)
    )
    assert count == 0
    batches = await session.scalar(
        select(func.count()).select_from(ImportBatch).where(ImportBatch.account_id == account.id)
    )
    assert batches == 0


async def test_same_balance_and_amount_on_another_date_is_imported(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A balance revisited on a later date is a new transaction, not a duplicate."""
    account = await make_account(session)
    account_id = account.id
    first = await client.post(
        f"/accounts/{account_id}/import",
        files=_csv_upload([HEADER, BEA_LINE]),
        headers=AUTH,
    )
    assert first.status_code == 200

    # Same balance and amount as BEA_LINE, one month later.
    september = "12345678,EUR,20260907,20260907,197.15,190.60,-6.55,BEA something else"
    response = await client.post(
        f"/accounts/{account_id}/import",
        files=_csv_upload([HEADER, september], name="september.csv"),
        headers=AUTH,
    )

    assert response.status_code == 200

    count = await session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account_id)
    )
    assert count == 2


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


async def test_import_cp1252_file_is_imported(client: AsyncClient, session: AsyncSession) -> None:
    """A cp1252 export decodes through the profile's fallback and keeps its accents."""
    account = await make_account(session, bank_name="abn_amro")
    body = "\r\n".join([HEADER, BEA_LINE.replace("Google", "Café")]).encode("cp1252")

    response = await client.post(
        f"/accounts/{account.id}/import",
        files={"file": ("export.csv", io.BytesIO(body), "text/csv")},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["inserted"] == 1
    raw_description = await session.scalar(
        select(Transaction.raw_description).where(Transaction.account_id == account.id)
    )
    assert raw_description is not None
    assert "Café Pay" in raw_description


async def test_import_undecodable_file_is_400(client: AsyncClient, session: AsyncSession) -> None:
    """A file neither UTF-8 nor the fallback decodes is refused before any batch row is written."""
    account = await make_account(session, bank_name="abn_amro")
    body = "\r\n".join([HEADER, BEA_LINE]).encode() + b"\x81"

    response = await client.post(
        f"/accounts/{account.id}/import",
        files={"file": ("export.csv", io.BytesIO(body), "text/csv")},
        headers=AUTH,
    )

    assert response.status_code == 400
    count = await session.scalar(
        select(func.count()).select_from(ImportBatch).where(ImportBatch.account_id == account.id)
    )
    assert count == 0


async def test_import_integrity_error_is_409(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unique-key race during import rolls back and asks for a retry."""
    account = await make_account(session, bank_name="abn_amro")

    async def collide(*args: object, **kwargs: object) -> None:
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    monkeypatch.setattr(main, "import_rows", collide)
    response = await client.post(
        f"/accounts/{account.id}/import",
        files=_csv_upload(TWO_ROWS),
        headers=AUTH,
    )

    assert response.status_code == 409
    assert "retry" in response.json()["detail"]
