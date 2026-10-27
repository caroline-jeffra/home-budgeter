"""Tests for the normalizer backfill command (P2-D3)."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.backfill import DescriptionChange, backfill_batch, preview_backfill
from app.cli import build_parser
from app.ingest import NORMALIZER_VERSION, normalize_text
from app.models import CategorizedBy, Transaction
from tests.factories import make_account, make_category, make_transaction

RAW = "BEA, Albert Heijn 1234 Amsterdam"
NORMALIZED = "albert heijn amsterdam"
STALE_VERSION = NORMALIZER_VERSION - 1


async def _stale(session: AsyncSession, raw: str = RAW, **kwargs: Any) -> Transaction:
    """A row normalized by an older version, its description out of date."""
    return await make_transaction(
        session,
        raw_description=raw,
        description="old normalized text",
        normalizer_version=STALE_VERSION,
        **kwargs,
    )


async def _reload(session: AsyncSession, row: Transaction) -> Transaction:
    """Rereads the row's columns from the database."""
    await session.refresh(row)
    return row


def test_fixture_raw_text_normalizes_as_expected() -> None:
    assert normalize_text(RAW) == NORMALIZED


async def test_backfill_rewrites_a_stale_description_from_the_raw_text(
    session: AsyncSession,
) -> None:
    row = await _stale(session)

    rewritten = await backfill_batch(session, limit=500)

    assert rewritten == 1
    row = await _reload(session, row)
    assert row.description == NORMALIZED
    assert row.normalizer_version == NORMALIZER_VERSION
    assert row.raw_description == RAW


async def test_backfill_leaves_current_rows_alone(session: AsyncSession) -> None:
    current = await make_transaction(
        session,
        raw_description=RAW,
        description="hand-checked",
        normalizer_version=NORMALIZER_VERSION,
    )

    assert await backfill_batch(session, limit=500) == 0

    current = await _reload(session, current)
    assert current.description == "hand-checked"


async def test_backfill_never_touches_categories(session: AsyncSession) -> None:
    category = await make_category(session)
    row = await _stale(session, category_id=category.id, categorized_by=CategorizedBy.MANUAL)

    await backfill_batch(session, limit=500)

    row = await _reload(session, row)
    assert row.category_id == category.id
    assert row.categorized_by is CategorizedBy.MANUAL


async def test_backfill_stops_at_the_limit_and_resumes(session: AsyncSession) -> None:
    account = await make_account(session)
    rows = [await _stale(session, account=account) for _ in range(3)]

    assert await backfill_batch(session, limit=2) == 2
    versions = [(await _reload(session, row)).normalizer_version for row in rows]
    assert sorted(versions) == [STALE_VERSION, NORMALIZER_VERSION, NORMALIZER_VERSION]

    assert await backfill_batch(session, limit=2) == 1
    assert await backfill_batch(session, limit=2) == 0
    for row in rows:
        assert (await _reload(session, row)).description == NORMALIZED


async def test_backfill_bumps_rows_whose_text_does_not_change(session: AsyncSession) -> None:
    row = await make_transaction(
        session, raw_description=RAW, description=NORMALIZED, normalizer_version=STALE_VERSION
    )

    assert await backfill_batch(session, limit=500) == 1

    assert (await _reload(session, row)).normalizer_version == NORMALIZER_VERSION


async def test_dry_run_reports_counts_and_samples_without_writing(session: AsyncSession) -> None:
    changing = await _stale(session)
    same = await make_transaction(
        session, raw_description=RAW, description=NORMALIZED, normalizer_version=STALE_VERSION
    )

    preview = await preview_backfill(session, sample_size=5)

    assert preview.stale == 2
    assert preview.changed == 1
    assert preview.samples == [
        DescriptionChange(
            transaction_id=changing.id, before="old normalized text", after=NORMALIZED
        )
    ]
    for row in (changing, same):
        assert (await _reload(session, row)).normalizer_version == STALE_VERSION


async def test_dry_run_caps_the_samples(session: AsyncSession) -> None:
    account = await make_account(session)
    for _ in range(4):
        await _stale(session, account=account)

    preview = await preview_backfill(session, sample_size=2)

    assert preview.changed == 4
    assert len(preview.samples) == 2


async def test_dry_run_on_a_current_database_reports_nothing(session: AsyncSession) -> None:
    await make_transaction(session)

    preview = await preview_backfill(session, sample_size=5)

    assert (preview.stale, preview.changed, preview.samples) == (0, 0, [])


def test_backfill_parser_defaults() -> None:
    args = build_parser().parse_args(["backfill"])

    assert args.command == "backfill"
    assert args.dry_run is False
    assert args.batch_size == 500


def test_backfill_parser_flags() -> None:
    args = build_parser().parse_args(["backfill", "--dry-run", "--batch-size", "50"])

    assert args.dry_run is True
    assert args.batch_size == 50


@pytest.mark.parametrize("size", ["0", "-1"])
def test_backfill_parser_rejects_a_non_positive_batch_size(size: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["backfill", "--batch-size", size])
