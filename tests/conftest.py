"""Shared pytest fixtures for the test suite."""

from collections.abc import AsyncGenerator

from httpx import ASGITransport, AsyncClient
from pytest_asyncio import fixture

from app.main import app


@fixture
async def client() -> AsyncGenerator[AsyncClient]:
    """Yields an HTTP client wired to the app, no network."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
