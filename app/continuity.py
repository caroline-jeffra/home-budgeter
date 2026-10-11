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
    
    The chain is the account's imported rows in orer. Manual transactions have no balance and sit 
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