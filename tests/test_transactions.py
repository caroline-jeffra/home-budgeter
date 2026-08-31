"""Tests relating to Transactions endpoints."""

from datetime import date

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import TransactionSplit
from tests.factories import make_account, make_transaction

AUTH = {"Authorization": f"Bearer {settings.api_token}"}


async def test_create_makes_a_transaction_and_one_full_amount_split(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The invariant: exactly one split, equal to the transaction's amount."""
    account = await make_account(session)
    created = await client.post(
        "/transactions",
        json={
            "account_id": account.id,
            "booked_on": "2026-03-10",
            "amount_cents": -1005,
            "description": "Groceries",
        },
        headers=AUTH,
    )
    assert created.status_code == 201

    splits = (await session.scalars(select(TransactionSplit))).all()
    assert len(splits) == 1
    assert splits[0].amount_cents == -1005
    assert splits[0].transaction_id == created.json()["id"]


async def test_amount_survives_as_an_exact_integer(
    client: AsyncClient, session: AsyncSession
) -> None:
    """1005 must not have become 1004.99999 anywhere in the round trip."""
    account = await make_account(session)
    created = await client.post(
        "/transactions",
        json={
            "account_id": account.id,
            "booked_on": "2026-03-10",
            "amount_cents": 1005,
            "description": "Refund",
        },
        headers=AUTH,
    )
    body = created.json()
    assert body["amount_cents"] == 1005
    assert isinstance(body["amount_cents"], int)


async def test_date_filter_bounds_are_inclusive(client: AsyncClient, session: AsyncSession) -> None:
    """A transaction booked exactly on either bound is returned."""
    account = await make_account(session)
    for day in (1, 15, 31):
        await make_transaction(session, account, booked_on=date(2026, 3, day))
    await session.commit()

    listed = await client.get(
        "/transactions",
        params={"date_from": "2026-03-01", "date_to": "2026-03-31"},
        headers=AUTH,
    )
    assert len(listed.json()) == 3


async def test_date_filter_excludes_outside_the_range(
    client: AsyncClient, session: AsyncSession
) -> None:
    """One day either side of the bounds is excluded."""
    account = await make_account(session)
    for day in (1, 15, 31):
        await make_transaction(session, account, booked_on=date(2026, 3, day))
    await session.commit()

    listed = await client.get(
        "/transactions",
        params={"date_from": "2026-03-02", "date_to": "2026-03-30"},
        headers=AUTH,
    )
    assert [t["booked_on"] for t in listed.json()] == ["2026-03-15"]


async def test_account_filter_isolates(client: AsyncClient, session: AsyncSession) -> None:
    """Only the requested account's transactions come back."""
    wanted = await make_account(session)
    other = await make_account(session)
    await make_transaction(session, wanted, description="Mine")
    await make_transaction(session, other, description="Theirs")
    await session.commit()

    listed = await client.get("/transactions", params={"account_id": wanted.id}, headers=AUTH)
    assert [t["description"] for t in listed.json()] == ["Mine"]


async def test_combined_filters_intersect(client: AsyncClient, session: AsyncSession) -> None:
    """Account and date range, not account or date range."""
    wanted = await make_account(session)
    other = await make_account(session)
    await make_transaction(session, wanted, booked_on=date(2026, 3, 15), description="Hit")
    await make_transaction(session, wanted, booked_on=date(2026, 6, 15), description="Wrong date")
    await make_transaction(session, other, booked_on=date(2026, 3, 15), description="Wrong account")
    await session.commit()

    listed = await client.get(
        "/transactions",
        params={"account_id": wanted.id, "date_from": "2026-03-01", "date_to": "2026-03-31"},
        headers=AUTH,
    )
    assert [t["description"] for t in listed.json()] == ["Hit"]


async def test_transactions_require_auth(client: AsyncClient) -> None:
    """Both transaction endpoints sit on the authenticated router."""
    assert (await client.get("/transactions")).status_code == 401
    assert (await client.post("/transactions")).status_code == 401


async def test_unknown_account_id_is_400(client: AsyncClient) -> None:
    """A foreign key violation is a malformed request, not a 500."""
    response = await client.post(
        "/transactions",
        json={
            "account_id": 999_999,
            "booked_on": "2026-03-10",
            "amount_cents": -100,
            "description": "Nowhere",
        },
        headers=AUTH,
    )
    assert response.status_code == 400
