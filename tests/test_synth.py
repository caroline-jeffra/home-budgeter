"""Tests for the synthetic export generator: seeded, deterministic, with valid balance chains."""

import random
from datetime import date
from itertools import islice, pairwise

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.continuity import chain_breaks
from app.ingest import ABN_AMRO, ImportProfile, ImportSummary, import_rows, parse, parse_cents
from app.synth import SynthRow, format_cents, generate, render
from tests.factories import make_account, make_import_batch
from tests.test_ingest import DUTCH_STYLE, HEADER

START = date(2026, 1, 1)


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


def test_render_round_trips_through_parse() -> None:
    rows = list(generate(seed=5, count=300, start=START))
    parsed = list(parse(render(rows), ABN_AMRO))
    assert [
        (
            p.booked_on,
            p.balance_before_cents,
            p.amount_cents,
            p.balance_after_cents,
            p.raw_description,
        )
        for p in parsed
    ] == [
        (r.booked_on, r.balance_before_cents, r.amount_cents, r.balance_after_cents, r.description)
        for r in rows
    ]


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
