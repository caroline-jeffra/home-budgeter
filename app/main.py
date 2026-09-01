"""FastAPI application and route definitions."""

from collections.abc import Sequence
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Category, Transaction, TransactionSplit
from app.schemas import (
    AccountCreate,
    AccountRead,
    CategoryCreate,
    CategoryRead,
    TransactionCreate,
    TransactionRead,
)

app = FastAPI(title="Home Budgeter")

router = APIRouter(dependencies=[Depends(require_auth)])

app.include_router(router)

Session = Annotated[AsyncSession, Depends(get_session)]


@app.get("/health")
async def health_check(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    """Simple round trip execution to supply database connection health check."""
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.post("/accounts", response_model=AccountRead, status_code=201)
async def create_account(payload: AccountCreate, session: Session) -> Account:
    """Creates an account with unique name and unique IBAN. Duplicates are a 409."""
    account = Account(**payload.model_dump())
    session.add(account)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account name or IBAN already exists.",
        ) from exc
    await session.refresh(account)
    return account


@router.get("/accounts", response_model=list[AccountRead])
async def list_accounts(session: Session) -> Sequence[Account]:
    """Lists all accounts, oldest first."""
    result = await session.scalars(select(Account).order_by(Account.id))
    return result.all()


@router.post("/categories", response_model=CategoryRead, status_code=201)
async def create_category(payload: CategoryCreate, session: Session) -> Category:
    """Creates a category. The name is unique; a duplicate is a 409."""
    category = Category(**payload.model_dump())
    session.add(category)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Category name already exists",
        ) from exc
    await session.refresh(category)
    return category


@router.get("/categories", response_model=list[CategoryRead])
async def list_categories(session: Session) -> Sequence[Category]:
    """Lists all categories, alphabetically."""
    result = await session.scalars(select(Category).order_by(Category.name))
    return result.all()


@router.post("/transactions", response_model=TransactionRead, status_code=201)
async def create_transaction(payload: TransactionCreate, session: Session) -> Transaction:
    """Creates a transaction and its single full-amount split in one unit of work."""
    transaction = Transaction(**payload.model_dump())
    session.add(transaction)
    try:
        await session.flush()

        session.add(
            TransactionSplit(
                transaction_id=transaction.id,
                category_id=transaction.category_id,
                amount_cents=transaction.amount_cents,
            )
        )

        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unknown account_id or category_id",
        ) from exc
    await session.refresh(transaction)
    return transaction


@router.get("/transactions", response_model=list[TransactionRead])
async def list_transactions(
    session: Session,
    account_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> Sequence[Transaction]:
    """Lists transactions newest-first, capped at `limit`."""
    query = select(Transaction)
    if account_id is not None:
        query = query.where(Transaction.account_id == account_id)
    if date_from is not None:
        query = query.where(Transaction.booked_on >= date_from)
    if date_to is not None:
        query = query.where(Transaction.booked_on <= date_to)

    query = query.order_by(Transaction.booked_on.desc(), Transaction.id.desc()).limit(limit)
    result = await session.scalars(query)
    return result.all()
