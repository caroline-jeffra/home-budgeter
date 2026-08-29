"""Shared pytest fixtures for the test suite."""

import asyncio
from collections.abc import AsyncGenerator

from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from pytest_asyncio import fixture

from alembic import command
from app.main import app


@fixture
async def client() -> AsyncGenerator[AsyncClient]:
    """Yields an HTTP client wired to the app, no network."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@fixture(scope="session")
async def _schema() -> AsyncGenerator[None]:
    """Build the test schema by running the migrations, then tear it down.

    Schema comes from Alembic to ensure build mirrors production."""
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "head")
    yield
    await asyncio.to_thread(command.downgrade, config, "base")
