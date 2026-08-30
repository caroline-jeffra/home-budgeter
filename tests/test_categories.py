"""Tests relating to Categories endpoints."""

from httpx import AsyncClient

from app.config import settings

AUTH = {"Authorization": f"Bearer {settings.api_token}"}


async def test_create_then_list_round_trip(client: AsyncClient) -> None:
    """A created category comes back from the list endpoint."""
    created = await client.post("/categories", json={"name": "Utilities"}, headers=AUTH)
    assert created.status_code == 201
    assert created.json()["is_recurring"] is False

    listed = await client.get("/categories", headers=AUTH)
    assert listed.status_code == 200
    assert [c["name"] for c in listed.json()] == ["Utilities"]


async def test_categories_require_auth(client: AsyncClient) -> None:
    """Both category endpoints sit on the authenticated router."""
    assert (await client.get("/categories")).status_code == 401
    assert (await client.post("/categories", json={"name": "Leaked"})).status_code == 401


async def test_duplicate_category_name_is_409(client: AsyncClient) -> None:
    """The unique constraint surfaces as a conflict, not a 500."""
    first = await client.post("/categories", json={"name": "Groceries"}, headers=AUTH)
    assert first.status_code == 201

    second = await client.post("/categories", json={"name": "Groceries"}, headers=AUTH)
    assert second.status_code == 409
