"""FastAPI application and route definitions."""

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Category
from app.schemas import AccountCreate, AccountRead, CategoryCreate, CategoryRead

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
    """Creates an account."""
    account = Account(**payload.model_dump())
    session.add(account)
    await session.commit()
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
