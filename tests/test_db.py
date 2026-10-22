"""Tests for the connection-level timeouts set in app/db.py."""

import pytest
from sqlalchemy import text

from app.db import SessionLocal, engine


@pytest.mark.parametrize(
    ("name", "milliseconds"),
    [
        ("lock_timeout", "5000"),
        ("statement_timeout", "30000"),
        ("idle_in_transaction_session_timeout", "60000"),
    ],
)
async def test_engine_connections_carry_timeout(name: str, milliseconds: str) -> None:
    """Test that every connection from the app engine starts with the P2-D5 timeout."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT setting, unit FROM pg_settings WHERE name = :name"), {"name": name}
        )

    assert tuple(result.one()) == (milliseconds, "ms")


async def test_app_sessions_carry_timeouts() -> None:
    """Test that sessions handed to endpoints use the same configured connections."""
    async with SessionLocal() as session:
        result = await session.execute(text("SHOW lock_timeout"))

    assert result.scalar_one() == "5s"
