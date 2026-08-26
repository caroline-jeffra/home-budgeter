"""Test to validate authentication configuration."""

from fastapi import APIRouter, Depends
from httpx import AsyncClient

from app.auth import require_auth
from app.config import settings
from app.main import app

probe_router = APIRouter(dependencies=[Depends(require_auth)])


@probe_router.get("/_probe")
async def probe() -> dict[str, str]:
    """Minimal protected route, exists only to exercise require_auth."""
    return {"status": "protected"}


app.include_router(probe_router)


async def test_missing_token_is_401(client: AsyncClient) -> None:
    """An absent token should be handled and return a 401 rather than FastAPI's default 403."""
    result = await client.get("/_probe")
    assert result.status_code == 401


async def test_wrong_token_is_401(client: AsyncClient) -> None:
    """An incorrect token should be handled and return a 401."""
    result = await client.get("/_probe", headers={"Authorization": "Bearer wrong"})
    assert result.status_code == 401
    assert result.headers["www-authenticate"] == "Bearer"


async def test_correct_token_is_200(client: AsyncClient) -> None:
    """A correct authentication token returns a 200."""
    result = await client.get("/_probe", headers={"Authorization": f"Bearer {settings.api_token}"})
    assert result.status_code == 200
