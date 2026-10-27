"""Normalizer backfill: rewrite stale descriptions from their raw text."""

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest import NORMALIZER_VERSION, normalize_text
from app.models import Transaction

_STALE = Transaction.normalizer_version < NORMALIZER_VERSION


@dataclass(frozen=True)
class DescriptionChange:
    """One row's stored description and what today's normalizer makes of its raw text."""

    transaction_id: int
    before: str
    after: str


@dataclass(frozen=True)
class BackfillPreview:
    """What a backfill would do: stale rows, how many would change, and a few examples."""

    stale: int
    changed: int
    samples: list[DescriptionChange]


async def preview_backfill(session: AsyncSession, *, sample_size: int) -> BackfillPreview:
    """Count the stale rows and sample the ones whose description would change, writing nothing."""
    stmt = (
        select(Transaction.id, Transaction.description, Transaction.raw_description)
        .where(_STALE)
        .order_by(Transaction.id)
    )
    stale = changed = 0
    samples: list[DescriptionChange] = []
    async for row_id, before, raw in await session.stream(stmt):
        stale += 1
        after = normalize_text(raw)
        if after == before:
            continue
        changed += 1
        if len(samples) < sample_size:
            samples.append(DescriptionChange(row_id, before, after))
    return BackfillPreview(stale, changed, samples)


async def backfill_batch(session: AsyncSession, *, limit: int) -> int:
    """Rewrite up to `limit` stale rows, lowest id first, returning how many were rewritten."""
    stmt = (
        select(Transaction.id, Transaction.raw_description)
        .where(_STALE)
        .order_by(Transaction.id)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return 0
    await session.execute(
        update(Transaction),
        [
            {
                "id": row_id,
                "description": normalize_text(raw),
                "normalizer_version": NORMALIZER_VERSION,
            }
            for row_id, raw in rows
        ],
    )
    return len(rows)
