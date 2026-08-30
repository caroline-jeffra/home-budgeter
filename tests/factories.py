"""Factories for building model rows inside a test's transaction."""

from datetime import date
from itertools import count
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Account,
    BudgetPeriod,
    Category,
    PeriodState,
    Transaction,
    TransactionSplit,
    TransferMatch,
)

_counter = count(1)


async def make_account(session: AsyncSession, **kwargs: Any) -> Account:
    """Builds an account. `iban` is nullable and left unset."""
    account = Account(**{"name": f"Account {next(_counter)}", **kwargs})
    session.add(account)
    await session.flush()
    return account


async def make_category(session: AsyncSession, **kwargs: Any) -> Category:
    """Builds a category. `name` is unique, so it is numbered per call."""
    category = Category(**{"name": f"Category {next(_counter)}", **kwargs})
    session.add(category)
    await session.flush()
    return category


async def make_budget_period(session: AsyncSession, **kwargs: Any) -> BudgetPeriod:
    """Builds a budget period. `period` is unique, so it is numbered per call."""
    period = BudgetPeriod(
        **{
            "period": f"2026-{next(_counter) % 12 + 1:02d}",
            "expected_income_cents": 250_000,
            "state": PeriodState.PLANNED,
            **kwargs,
        }
    )
    session.add(period)
    await session.flush()
    return period


async def make_transaction(
    session: AsyncSession, account: Account | None = None, **kwargs: Any
) -> Transaction:
    """Builds a transaction, creating an account if none is given.

    `category_id` and `categorized_by` are both unset. An uncategorized
    transaction should not claim to have been categorized."""
    if account is None:
        account = await make_account(session)
    transaction = Transaction(
        **{
            "account_id": account.id,
            "booked_on": date(2026, 1, 15),
            "amount_cents": -1_250,
            "description": f"Transaction {next(_counter)}",
            **kwargs,
        }
    )
    session.add(transaction)
    await session.flush()
    return transaction


async def make_split(
    session: AsyncSession,
    transaction: Transaction | None = None,
    **kwargs: Any,
) -> TransactionSplit:
    """Builds the single full-amount split."""
    if transaction is None:
        transaction = await make_transaction(session)
    split = TransactionSplit(
        **{
            "transaction_id": transaction.id,
            "amount_cents": transaction.amount_cents,
            **kwargs,
        }
    )
    session.add(split)
    await session.flush()
    return split


async def make_transfer_match(
    session: AsyncSession,
    source: Transaction | None = None,
    destination: Transaction | None = None,
    **kwargs: Any,
) -> TransferMatch:
    """Builds a transfer match between two transactions, creating both if absent."""
    if source is None:
        source = await make_transaction(session)
    if destination is None:
        destination = await make_transaction(session)
    match = TransferMatch(
        **{
            "source_id": source.id,
            "destination_id": destination.id,
            **kwargs,
        }
    )
    session.add(match)
    await session.flush()
    return match
