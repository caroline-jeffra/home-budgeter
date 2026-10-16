"""Voiding an import batch: delete its transactions, keep the batch as the audit record."""

from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ImportBatch, Transaction, TransactionSplit, TransferMatch


async def void_batch(session: AsyncSession, batch_id: int, reason: str) -> int:
    """Delete one import batch's transactions and stamp the batch voided, returning the count

    Does not commit; the caller owns the transaction."""
    reason = reason.strip()
    if not reason:
        raise ValueError("a void needs a reason")

    batch = await session.get(ImportBatch, batch_id, with_for_update=True)
    if batch is None:
        raise ValueError(f"no import batch {batch_id}")
    if batch.voided_at is not None:
        raise ValueError(
            f"import batch {batch_id} was already voided at {batch.voided_at:%Y-%m-%d %H:%M}:"
            f"{batch.void_reason}"
        )

    await session.execute(select(func.set_config("app.void_batch_id", str(batch_id), True)))
    batch_rows = select(Transaction.id).where(Transaction.import_batch_id == batch_id)
    await session.execute(
        delete(TransferMatch).where(
            TransferMatch.source_id.in_(batch_rows) | TransferMatch.destination_id.in_(batch_rows)
        )
    )
    await session.execute(
        delete(TransactionSplit).where(TransactionSplit.transaction_id.in_(batch_rows))
    )
    result = cast(
        CursorResult[Any],
        await session.execute(delete(Transaction).where(Transaction.import_batch_id == batch_id)),
    )

    batch.voided_at = func.now()
    batch.void_reason = reason
    await session.flush()
    return result.rowcount
