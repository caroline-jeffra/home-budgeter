"""Tests for the /health endpoint."""

from httpx import AsyncClient


async def test_health_returns_ok(client: AsyncClient) -> None:
    """Test that the /health endpoint returns ok."""
    result = await client.get("/health")

    assert result.json == {"status": "ok"}
