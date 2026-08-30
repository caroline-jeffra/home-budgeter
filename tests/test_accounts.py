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
