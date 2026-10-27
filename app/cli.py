"""Operator commands run outside the API, e.g. `python -m app.cli void 12 --reason "..."`."""

import argparse
import asyncio
import sys

from app.backfill import BackfillPreview, backfill_batch, preview_backfill
from app.db import SessionLocal, engine
from app.ingest import NORMALIZER_VERSION
from app.void import void_batch

SAMPLE_SIZE = 5


def _positive_int(raw: str) -> int:
    """An argparse type for a count that must be at least 1."""
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument parser, separate so a test can use it without a database."""
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    void = commands.add_parser("void", help="delete a batch's transactions, keeping the batch")
    void.add_argument("batch_id", type=int)
    void.add_argument("--reason", required=True)
    backfill = commands.add_parser(
        "backfill", help="rewrite descriptions left by an older normalizer from their raw text"
    )
    backfill.add_argument(
        "--dry-run", action="store_true", help="report counts and samples, write nothing"
    )
    backfill.add_argument("--batch-size", type=_positive_int, default=500)
    return parser


async def _void(batch_id: int, reason: str) -> int:
    """Voids one batch in its own transaction and commits it."""
    try:
        async with SessionLocal() as session:
            deleted = await void_batch(session, batch_id, reason)
            await session.commit()
            return deleted
    finally:
        await engine.dispose()


async def _backfill(batch_size: int) -> int:
    """Rewrites stale rows one committed batch at a time, returning the total."""
    total = 0
    try:
        async with SessionLocal() as session:
            while rewritten := await backfill_batch(session, limit=batch_size):
                await session.commit()
                total += rewritten
                print(f"rewrote {total} rows")
        return total
    finally:
        await engine.dispose()


async def _preview(sample_size: int) -> BackfillPreview:
    """Reads what a backfill would do, writing nothing."""
    try:
        async with SessionLocal() as session:
            return await preview_backfill(session, sample_size=sample_size)
    finally:
        await engine.dispose()


def _run_backfill(dry_run: bool, batch_size: int) -> int:
    """Runs the backfill or its dry run and prints the outcome, returning the exit code."""
    if not dry_run:
        total = asyncio.run(_backfill(batch_size))
        print(f"backfill done: {total} rows now at normalizer v{NORMALIZER_VERSION}")
        return 0
    preview = asyncio.run(_preview(SAMPLE_SIZE))
    print(
        f"{preview.stale} rows below normalizer v{NORMALIZER_VERSION}, "
        f"{preview.changed} would change"
    )
    for change in preview.samples:
        print(f" #{change.transaction_id}: {change.before!r} -> {change.after!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Runs the parsed command, returning the process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "backfill":
        return _run_backfill(args.dry_run, args.batch_size)
    try:
        deleted = asyncio.run(_void(args.batch_id, args.reason))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"voided batch {args.batch_id}: {deleted} transactions deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
