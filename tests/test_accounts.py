"""Tests relating to Accounts endpoints."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from tests.factories import make_account

AUTH = {"Authorization": f"Bearer {settings.api_token}"}


async def test_create_then_list_round_trip(client: AsyncClient) -> None:
    """A created account comes back from the list endpoint."""
    created = await client.post("/accounts", json={"name": "Checking"}, headers=AUTH)
    assert created.status_code == 201
    assert created.json()["name"] == "Checking"

    listed = await client.get("/accounts", headers=AUTH)
    assert listed.status_code == 200
    assert [a["name"] for a in listed.json()] == ["Checking"]


async def test_duplicate_account_name_is_409(client: AsyncClient) -> None:
    """The unique name constraint surfaces as a conflict, not a 500."""
    first = await client.post(
        "/accounts", json={"name": "Savings", "iban": "IBAN123443211234"}, headers=AUTH
    )
    assert first.status_code == 201

    second = await client.post(
        "/accounts", json={"name": "Savings", "iban": "IBAN432112344321"}, headers=AUTH
    )
    assert second.status_code == 409


async def test_duplicate_account_iban_is_409(client: AsyncClient) -> None:
    """The unique iban constraint surfaces as a conflict, not a 500."""
    first = await client.post(
        "/accounts", json={"name": "Savings", "iban": "IBAN123443211234"}, headers=AUTH
    )
    assert first.status_code == 201

    second = await client.post(
        "/accounts", json={"name": "Checking", "iban": "IBAN123443211234"}, headers=AUTH
    )
    assert second.status_code == 409


async def test_multiple_account_null_ibans_succeed(client: AsyncClient) -> None:
    """Accounts with NULL IBANs do not collide from the unique constraint."""
    first = await client.post("/accounts", json={"name": "Savings"}, headers=AUTH)
    assert first.status_code == 201

    second = await client.post("/accounts", json={"name": "Checking"}, headers=AUTH)
    assert second.status_code == 201


async def test_list_returns_factory_rows(client: AsyncClient, session: AsyncSession) -> None:
    """Rows created directly through a factory are visible to the endpoint."""
    await make_account(session, name="Savings")
    listed = await client.get("/accounts", headers=AUTH)
    assert [a["name"] for a in listed.json()] == ["Savings"]


async def test_accounts_require_auth(client: AsyncClient) -> None:
    """Both accounts endpoints sit on the authenticated router."""
    assert (await client.get("/accounts")).status_code == 401
    assert (
        await client.get("/accounts", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
