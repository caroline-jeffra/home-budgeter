"""Operator commands run outside the API, e.g. `python -m app.cli void 12 --reason "..."`."""

import argparse
import asyncio
import sys

from app.db import SessionLocal, engine
from app.void import void_batch


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument parser, separate so a test can use it without a database."""
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    void = commands.add_parser("void", help="delete a batch's transactions, keeping the batch")
    void.add_argument("batch_id", type=int)
    void.add_argument("--reason", required=True)
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


def main(argv: list[str] | None = None) -> int:
    """Runs the parsed command, returning the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        deleted = asyncio.run(_void(args.batch_id, args.reason))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"voided batch {args.batch_id}: {deleted} transactions deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
