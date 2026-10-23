"""Tests for the synthetic export generator: seeded, deterministic, with valid balance chains."""

import io
import random
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime, timedelta
from itertools import islice, pairwise
from unittest.mock import ANY

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.continuity import chain_breaks
from app.ingest import (
    ABN_AMRO,
    ImportProfile,
    ImportSummary,
    ParsedRow,
    import_rows,
    normalize_text,
    open_text,
    parse,
    parse_cents,
)
from app.models import Transaction
from app.synth import (
    SynthRow,
    cosmetic,
    fork,
    format_cents,
    gap,
    generate,
    overlap,
    render,
    reword,
    to_bytes,
)
from tests.factories import make_account, make_import_batch
from tests.test_continuity import _upload
from tests.test_ingest import AUTH, DUTCH_STYLE, HEADER

START = date(2026, 1, 1)


def _chain(*amounts: int, opening: int = 10_000) -> list[SynthRow]:
    """Hand-built rows, one a day from START, each opening where the last closed."""
    rows: list[SynthRow] = []
    balance = opening
    for day, amount in enumerate(amounts):
        rows.append(
            SynthRow(
                booked_on=START + timedelta(days=day),
                balance_before_cents=balance,
                amount_cents=amount,
                balance_after_cents=balance + amount,
                description=f"Shop {day}",
            )
        )
        balance += amount
    return rows


def _assert_chained(rows: list[SynthRow]) -> None:
    for row in rows:
        assert row.balance_before_cents + row.amount_cents == row.balance_after_cents
    for previous, row in pairwise(rows):
        assert row.balance_before_cents == previous.balance_after_cents


def test_generate_yields_count_rows() -> None:
    assert len(list(generate(seed=1, count=250, start=START))) == 250


def test_generate_is_deterministic_per_seed() -> None:
    first = list(generate(seed=7, count=100, start=START))
    assert list(generate(seed=7, count=100, start=START)) == first


def test_generate_differs_across_seeds() -> None:
    first = list(generate(seed=7, count=100, start=START))
    assert list(generate(seed=8, count=100, start=START)) != first


def test_generate_does_not_share_the_global_random() -> None:
    """Draws from `random` mid-file must not change the rest of it."""
    expected = list(generate(seed=7, count=50, start=START))
    rows = generate(seed=7, count=50, start=START)
    head = list(islice(rows, 25))
    random.random()
    assert head + list(rows) == expected


def test_generate_chains_every_balance() -> None:
    rows = list(generate(seed=3, count=500, start=START, opening_cents=12_345))
    assert rows[0].balance_before_cents == 12_345
    for row in rows:
        assert row.balance_before_cents + row.amount_cents == row.balance_after_cents
    for previous, row in pairwise(rows):
        assert row.balance_before_cents == previous.balance_after_cents


def test_generate_books_in_date_order_from_start() -> None:
    rows = list(generate(seed=3, count=500, start=START))
    assert rows[0].booked_on == START
    assert all(previous.booked_on <= row.booked_on for previous, row in pairwise(rows))
    assert rows[-1].booked_on > START


def test_generate_moves_money_both_ways_and_never_zero() -> None:
    amounts = [row.amount_cents for row in generate(seed=3, count=500, start=START)]
    assert 0 not in amounts
    assert any(amount > 0 for amount in amounts)
    assert any(amount < 0 for amount in amounts)


def test_generate_is_lazy() -> None:
    """A billion rows only costs what is taken."""
    rows = generate(seed=1, count=10**9, start=START)
    assert len(list(islice(rows, 3))) == 3


@pytest.mark.parametrize(
    ("profile", "cents", "raw"),
    [
        (ABN_AMRO, 123456, "1234.56"),
        (ABN_AMRO, -655, "-6.55"),
        (ABN_AMRO, 15000, "150.00"),
        (ABN_AMRO, 5, "0.05"),
        (ABN_AMRO, -5, "-0.05"),
        (ABN_AMRO, 0, "0.00"),
        (DUTCH_STYLE, 123456, "1.234,56"),
        (DUTCH_STYLE, -123456789, "-1.234.567,89"),
        (DUTCH_STYLE, 99999, "999,99"),
        (DUTCH_STYLE, -5, "-0,05"),
    ],
)
def test_format_cents_writes_the_profile_format(
    profile: ImportProfile, cents: int, raw: str
) -> None:
    assert format_cents(cents, profile) == raw


@pytest.mark.parametrize("profile", [ABN_AMRO, DUTCH_STYLE], ids=["abn_amro", "dutch_style"])
def test_format_cents_round_trips_through_parse_cents(profile: ImportProfile) -> None:
    for cents in [*range(-100_000, 100_000, 997), 10**12, -(10**12)]:
        assert parse_cents(format_cents(cents, profile), profile) == cents


def test_render_starts_with_the_abn_amro_header() -> None:
    assert next(render(generate(seed=1, count=1, start=START))) == HEADER


def test_render_writes_an_abn_amro_line() -> None:
    """Pins the column order, the date format, and quoting a description holding the delimiter."""
    row = SynthRow(
        booked_on=START,
        balance_before_cents=19715,
        amount_cents=-655,
        balance_after_cents=19060,
        description="BEA, Google Pay  Kiosk,PAS410",
    )
    [_, line] = render([row])
    assert line == (
        '12345678,EUR,20260101,20260101,197.15,190.60,-6.55,"BEA, Google Pay  Kiosk,PAS410"'
    )


Fields = tuple[date, int | None, int, int | None, str]


def _fields(rows: Iterable[SynthRow | ParsedRow]) -> list[Fields]:
    """What a generated row and its parsed line must agree on."""
    return [
        (
            row.booked_on,
            row.balance_before_cents,
            row.amount_cents,
            row.balance_after_cents,
            row.raw_description if isinstance(row, ParsedRow) else row.description,
        )
        for row in rows
    ]


def test_render_round_trips_through_parse() -> None:
    rows = list(generate(seed=5, count=300, start=START))
    assert _fields(parse(render(rows), ABN_AMRO)) == _fields(rows)


def test_render_is_lazy() -> None:
    lines = render(generate(seed=1, count=10**9, start=START))
    assert len(list(islice(lines, 3))) == 3


async def test_a_generated_file_imports_with_an_unbroken_chain(session: AsyncSession) -> None:
    account = await make_account(session)
    first = await make_import_batch(session, account)
    second = await make_import_batch(session, account)
    lines = list(render(generate(seed=11, count=1_200, start=START)))

    summary = await import_rows(session, account, lines, import_batch_id=first.id)
    assert summary == ImportSummary(rows_read=1_200, inserted=1_200)
    assert await chain_breaks(session, account.id) == []

    again = await import_rows(session, account, lines, import_batch_id=second.id)
    assert again == ImportSummary(rows_read=1_200, inserted=0)


# Mutators: each takes a clean chain and introduces exactly one fault.


def test_gap_drops_a_run_of_rows() -> None:
    rows = _chain(-100, -200, -300, -400, -500)
    assert gap(rows, start=1, length=2) == [rows[0], rows[3], rows[4]]


def test_mutators_leave_their_input_alone() -> None:
    rows = _chain(-100, -200, -300, -400, -500)
    untouched = list(rows)
    gap(rows, start=1, length=2)
    fork(rows, index=1, amount_cents=-250)
    cosmetic(rows, index=1)
    reword(rows, index=1, description="Jumbo Amsterdam")
    overlap(rows, first_end=3, second_start=1)
    assert rows == untouched


@pytest.mark.parametrize(
    ("start", "length"),
    [
        (0, 1),  # nothing before the gap: the file just opens later
        (3, 2),  # nothing after the gap: the file just closes earlier
        (1, 0),  # no rows dropped
    ],
)
def test_gap_needs_rows_on_both_sides(start: int, length: int) -> None:
    with pytest.raises(ValueError):
        gap(_chain(-100, -200, -300, -400, -500), start=start, length=length)


def test_gap_rejects_a_run_that_nets_to_zero() -> None:
    """-200 then +200 leaves the balance where it was, so the chain would show no break."""
    with pytest.raises(ValueError):
        gap(_chain(-100, -200, 200, -300), start=1, length=2)


def test_fork_restates_one_amount_and_shifts_every_later_balance() -> None:
    rows = _chain(-100, -200, -300, -400)
    forked = fork(rows, index=1, amount_cents=-250)

    assert forked[0] == rows[0]
    assert forked[1] == replace(
        rows[1], amount_cents=-250, balance_after_cents=rows[1].balance_after_cents - 50
    )
    for original, shifted in zip(rows[2:], forked[2:], strict=True):
        assert shifted == replace(
            original,
            balance_before_cents=original.balance_before_cents - 50,
            balance_after_cents=original.balance_after_cents - 50,
        )
    _assert_chained(forked)


@pytest.mark.parametrize("amount_cents", [-200, 0], ids=["unchanged", "zero"])
def test_fork_rejects_a_restatement_that_is_not_one(amount_cents: int) -> None:
    with pytest.raises(ValueError):
        fork(_chain(-100, -200, -300), index=1, amount_cents=amount_cents)


def test_cosmetic_changes_the_raw_text_but_not_the_normalized() -> None:
    rows = _chain(-100, -200, -300)
    rows[1] = replace(rows[1], description="BEA, Google Pay  ALBERT HEIJN 1234,PAS410")
    mutated = cosmetic(rows, index=1)

    assert mutated[1].description != rows[1].description
    assert normalize_text(mutated[1].description) == normalize_text(rows[1].description)
    assert mutated[1] == replace(rows[1], description=mutated[1].description)
    assert (mutated[0], mutated[2]) == (rows[0], rows[2])


def test_cosmetic_changes_an_already_upper_case_description() -> None:
    rows = _chain(-100, -200, -300)
    rows[1] = replace(rows[1], description="ALBERT HEIJN")
    assert cosmetic(rows, index=1)[1].description != "ALBERT HEIJN"


def test_reword_changes_the_normalized_text() -> None:
    rows = _chain(-100, -200, -300)
    mutated = reword(rows, index=1, description="Jumbo Amsterdam")
    assert mutated == [rows[0], replace(rows[1], description="Jumbo Amsterdam"), rows[2]]


def test_reword_rejects_a_change_that_is_only_cosmetic() -> None:
    rows = _chain(-100, -200, -300)
    with pytest.raises(ValueError):
        reword(rows, index=1, description=f"  {rows[1].description.upper()}  ")


def test_overlap_splits_into_two_files_that_share_rows() -> None:
    rows = _chain(-100, -200, -300, -400, -500)
    first, second = overlap(rows, first_end=3, second_start=1)
    assert (first, second) == (rows[:3], rows[1:])


@pytest.mark.parametrize(
    ("first_end", "second_start"),
    [
        (2, 2),  # adjacent: nothing shared
        (2, 3),  # a hole between them
        (6, 1),  # first file runs past the rows
        (3, -1),  # negative index
    ],
)
def test_overlap_rejects_ranges_that_do_not_overlap(first_end: int, second_start: int) -> None:
    with pytest.raises(ValueError):
        overlap(
            _chain(-100, -200, -300, -400, -500), first_end=first_end, second_start=second_start
        )


# Mutated files through the import endpoint: each fault is seen the way 2.4 reports it.


async def test_a_gapped_file_opens_one_gap_and_the_full_file_closes_it(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)
    rows = list(generate(seed=21, count=200, start=START))

    # Drop one whole day: a gap ending mid-day fills as a conflict, a known continuity bug.
    day = rows[82].booked_on
    start = next(i for i, row in enumerate(rows) if row.booked_on == day)
    end = next(i for i, row in enumerate(rows) if row.booked_on > day)

    gapped = gap(rows, start=start, length=end - start)
    first = await _upload(client, account.id, list(render(gapped)))
    assert first.status_code == 200
    assert first.json()["gaps_opened"] == [
        {
            "transaction_id": ANY,
            "booked_on": rows[end].booked_on.isoformat(),
            "import_batch_id": first.json()["batch_id"],
            "expected_before_cents": rows[start - 1].balance_after_cents,
            "actual_before_cents": rows[end].balance_before_cents,
        }
    ]

    second = await _upload(client, account.id, list(render(rows)))
    assert second.status_code == 200
    assert second.json()["inserted"] == end - start
    assert second.json()["gaps_closed"] == first.json()["gaps_opened"]
    assert second.json()["gaps_opened"] == []


async def test_a_forked_file_is_a_conflict_and_writes_nothing(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)
    account_id = account.id
    rows = list(generate(seed=22, count=200, start=START))
    restated = rows[120].amount_cents - 1_000

    assert (await _upload(client, account_id, list(render(rows)))).status_code == 200
    second = await _upload(
        client, account_id, list(render(fork(rows, index=120, amount_cents=restated)))
    )

    assert second.status_code == 409
    assert second.json()["detail"]["conflicts"] != []
    stored = await session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account_id)
    )
    assert stored == 200


async def test_overlapping_files_import_each_row_once_with_no_gaps(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)
    account_id = account.id
    rows = list(generate(seed=23, count=200, start=START))
    first, second = overlap(rows, first_end=120, second_start=80)

    assert (await _upload(client, account_id, list(render(first)))).status_code == 200
    response = await _upload(client, account_id, list(render(second)))

    assert response.status_code == 200
    assert response.json()["inserted"] == 80
    assert response.json()["gaps_opened"] == []
    assert await chain_breaks(session, account_id) == []


# P2-D6 format variants: test-only profiles the generator writes and the parser must read.


def _date_ddmmyyyy(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%d-%m-%Y").date()


VARIANTS = {
    "semicolon": (replace(ABN_AMRO, delimiter=";"), "%Y%m%d", "utf-8"),
    "dutch_decimals": (
        replace(ABN_AMRO, decimal_separator=",", thousands_separator="."),
        "%Y%m%d",
        "utf-8",
    ),
    "day_first": (replace(ABN_AMRO, parse_date=_date_ddmmyyyy), "%d-%m-%Y", "utf-8"),
    "cp1252": (ABN_AMRO, "%Y%m%d", "cp1252"),
    "all_at_once": (replace(DUTCH_STYLE, parse_date=_date_ddmmyyyy), "%d-%m-%Y", "cp1252"),
}


def test_generate_writes_some_non_ascii_descriptions() -> None:
    """Without one, a cp1252 file is byte-for-byte UTF-8 and the fallback is never tried."""
    assert any(not row.description.isascii() for row in generate(seed=31, count=300, start=START))


def test_render_writes_the_date_format_it_is_given() -> None:
    [row] = _chain(-655)
    [_, line] = render([row], date_format="%d-%m-%Y")
    assert line.startswith("12345678,EUR,01-01-2026,01-01-2026,")


def test_to_bytes_joins_lines_with_crlf() -> None:
    assert to_bytes(["a", "b"]) == b"a\r\nb"


def test_to_bytes_encodes_with_the_encoding_given() -> None:
    assert to_bytes(["Café"], "cp1252") == b"Caf\xe9"


def test_a_cp1252_file_is_not_utf8() -> None:
    """So decoding it really does go through the profile's fallback encoding."""
    body = to_bytes(render(generate(seed=31, count=300, start=START)), "cp1252")
    with pytest.raises(UnicodeDecodeError):
        body.decode("utf-8")


@pytest.mark.parametrize(("profile", "date_format", "encoding"), VARIANTS.values(), ids=VARIANTS)
def test_a_variant_file_round_trips_through_open_text_and_parse(
    profile: ImportProfile, date_format: str, encoding: str
) -> None:
    rows = list(generate(seed=31, count=300, start=START))
    body = to_bytes(render(rows, profile, date_format=date_format), encoding)
    assert _fields(parse(open_text(body, profile), profile)) == _fields(rows)


async def test_a_generated_cp1252_file_imports_through_the_endpoint(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)
    account_id = account.id
    rows = list(generate(seed=31, count=300, start=START))
    body = to_bytes(render(rows), "cp1252")

    response = await client.post(
        f"/accounts/{account_id}/import",
        files={"file": ("export.csv", io.BytesIO(body), "text/csv")},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["inserted"] == 300
    stored = set(
        await session.scalars(
            select(Transaction.raw_description).where(Transaction.account_id == account_id)
        )
    )
    assert stored == {row.description for row in rows}
