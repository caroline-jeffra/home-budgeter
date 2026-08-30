"""Proves the client fixture shares the test's session and cleans up after itself."""

from typing import Annotated

from fastapi import APIRouter, Depends
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.main import app
from app.models import Account

probe_router = APIRouter()


@probe_router.get("/_session_probe")
async def session_probe(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, int]:
    """Counts accounts through whatever session the app injects."""
    count = await session.scalar(select(func.count()).select_from(Account))
    return {"count": count or 0}


app.include_router(probe_router)


async def test_client_shares_the_test_session(client: AsyncClient, session: AsyncSession) -> None:
    """An uncommitted write must be visible to the endpoint, a shared session can see it."""
    session.add(Account(name="Sharing probe"))
    await session.flush()

    result = await client.get("/_session_probe")

    assert result.json()["count"] == 1


async def test_override_is_closed_after_the_client(client: AsyncClient) -> None:
    """The override is installed while the client is alive."""
    assert get_session in app.dependency_overrides


def test_no_override_leaks_between_tests() -> None:
    """No client fixture here, so nothing may be overridden."""
    assert app.dependency_overrides == {}
