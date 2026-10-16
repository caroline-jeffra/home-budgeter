"""Balance continuity: the chain of imported rows, and where it breaks."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Transaction


@dataclass(frozen=True)
class ChainBreak:
    """A row whose opening balance is not the previous row's closing balance."""

    transaction_id: int
    booked_on: date
    import_batch_id: int
    expected_before_cents: int
    actual_before_cents: int


async def chain_breaks(session: AsyncSession, account_id: int) -> list[ChainBreak]:
    """Every break in an account's balance chain, in chain order.

    The chain is the account's imported rows in order. Manual transactions have no balance and sit
    outside it."""
    previous_after = (
        func.lag(Transaction.balance_after_cents)
        .over(order_by=(Transaction.booked_on, Transaction.import_batch_id, Transaction.line_no))
        .label("previous_after")
    )
    chain = (
        select(
            Transaction.id,
            Transaction.booked_on,
            Transaction.import_batch_id,
            Transaction.line_no,
            Transaction.balance_before_cents,
            previous_after,
        )
        .where(
            Transaction.account_id == account_id,
            Transaction.balance_before_cents.is_not(None),
        )
        .subquery()
    )
    result = await session.execute(
        select(chain)
        .where(chain.c.previous_after != chain.c.balance_before_cents)
        .order_by(chain.c.booked_on, chain.c.import_batch_id, chain.c.line_no)
    )
    return [
        ChainBreak(
            transaction_id=row.id,
            booked_on=row.booked_on,
            import_batch_id=row.import_batch_id,
            expected_before_cents=row.previous_after,
            actual_before_cents=row.balance_before_cents,
        )
        for row in result
    ]


@dataclass(frozen=True)
class ContinuityReport:
    """What one import did to an account's balance chain."""

    gaps_opened: list[ChainBreak]
    gaps_closed: list[ChainBreak]
    conflicts: list[ChainBreak]


def classify(
    before: list[ChainBreak],
    after: list[ChainBreak],
    overlap: tuple[date, date] | None,
) -> ContinuityReport:
    """Sort the breaks an import created or removed.

    New breaks dated inside `overlap` are conflicts; the rest are gaps.
    Breaks present before and gone after are closed."""
    before_ids = {b.transaction_id for b in before}
    after_ids = {b.transaction_id for b in after}
    opened = [b for b in after if b.transaction_id not in before_ids]
    conflicts = [
        b for b in opened if overlap is not None and overlap[0] <= b.booked_on <= overlap[1]
    ]
    return ContinuityReport(
        gaps_opened=[b for b in opened if b not in conflicts],
        gaps_closed=[b for b in before if b.transaction_id not in after_ids],
        conflicts=conflicts,
    )


async def overlap_window(
    session: AsyncSession, account_id: int, import_batch_id: int
) -> tuple[date, date] | None:
    """The dates on which this batch's rows meet rows already stored.

    The span of earlier imported rows inside the batch's own date range; None when
    the batch only extends the account or fills a hole."""
    batch_start, batch_end = (
        await session.execute(
            select(func.min(Transaction.booked_on), func.max(Transaction.booked_on)).where(
                Transaction.import_batch_id == import_batch_id
            )
        )
    ).one()
    if batch_start is None:
        return None
    start, end = (
        await session.execute(
            select(func.min(Transaction.booked_on), func.max(Transaction.booked_on)).where(
                Transaction.account_id == account_id,
                Transaction.balance_before_cents.is_not(None),
                Transaction.import_batch_id != import_batch_id,
                Transaction.booked_on.between(batch_start, batch_end),
            )
        )
    ).one()
    return None if start is None else (start, end)
