"""Tests for idempotency keys on manual transactions (P2-D7)."""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from httpx import ASGITransport, AsyncClient
from pytest_asyncio import fixture
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal, engine
from app.idempotency import SavedResponse, claim, record
from app.main import app
from app.models import Base, Transaction
from tests.factories import make_account

AUTH = {"Authorization": f"Bearer {settings.api_token}"}


def _body(account_id: int, **overrides: Any) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "booked_on": "2026-03-10",
        "amount_cents": -1005,
        "description": "Groceries",
        **overrides,
    }


def _keyed(key: str) -> dict[str, str]:
    return {**AUTH, "Idempotency-Key": key}


async def _count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Transaction)) or 0


async def test_a_retry_with_the_same_key_replays_the_first_response(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)

    first = await client.post("/transactions", json=_body(account.id), headers=_keyed("k-1"))
    retry = await client.post("/transactions", json=_body(account.id), headers=_keyed("k-1"))

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert await _count(session) == 1


async def test_the_same_key_with_a_different_body_is_422(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)

    await client.post("/transactions", json=_body(account.id), headers=_keyed("k-1"))
    reused = await client.post(
        "/transactions", json=_body(account.id, amount_cents=-2000), headers=_keyed("k-1")
    )

    assert reused.status_code == 422
    assert reused.json()["detail"] == "Idempotency-Key was already used with a different request"
    assert await _count(session) == 1


async def test_the_hash_ignores_key_order_whitespace_and_explicit_defaults(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)
    reordered = dict(reversed(list(_body(account.id, category_id=None).items())))

    first = await client.post("/transactions", json=_body(account.id), headers=_keyed("k-1"))
    retry = await client.post(
        "/transactions",
        content=json.dumps(reordered, indent=4),
        headers={**_keyed("k-1"), "Content-Type": "application/json"},
    )

    assert retry.status_code == 201
    assert retry.json() == first.json()
    assert await _count(session) == 1


async def test_different_keys_create_separate_transactions(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)

    a = await client.post("/transactions", json=_body(account.id), headers=_keyed("k-1"))
    b = await client.post("/transactions", json=_body(account.id), headers=_keyed("k-2"))

    assert a.json()["id"] != b.json()["id"]
    assert await _count(session) == 2


async def test_without_a_key_every_post_creates_a_transaction(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)

    for _ in range(2):
        assert (await client.post("/transactions", json=_body(account.id), headers=AUTH)).is_success

    assert await _count(session) == 2


async def test_a_failed_request_does_not_save_its_key(
    client: AsyncClient, session: AsyncSession
) -> None:
    # Commit the account, or the failed request's rollback takes it out along with the key;
    # read its id first, because that rollback also expires every loaded object.
    account_id = (await make_account(session)).id
    await session.commit()

    failed = await client.post("/transactions", json=_body(999_999), headers=_keyed("k-1"))
    assert failed.status_code == 400

    fixed = await client.post("/transactions", json=_body(account_id), headers=_keyed("k-1"))
    assert fixed.status_code == 201
    assert await _count(session) == 1


async def test_an_overlong_key_is_422(client: AsyncClient, session: AsyncSession) -> None:
    account = await make_account(session)

    response = await client.post("/transactions", json=_body(account.id), headers=_keyed("k" * 256))

    assert response.status_code == 422
    assert await _count(session) == 0


# Concurrency needs two connections that really commit, so these tests bypass the
# rolled-back `session` fixture and truncate every table afterwards.


@fixture
async def committed(_schema: None) -> AsyncGenerator[None]:
    """Lets a test commit through real sessions, then empties every table."""
    try:
        yield
    finally:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


async def test_a_concurrent_claim_waits_for_the_holder_then_replays(committed: None) -> None:
    async with SessionLocal() as holder, SessionLocal() as waiter:
        assert await claim(holder, "k-1", "hash") is None
        waiting = asyncio.create_task(claim(waiter, "k-1", "hash"))
        await asyncio.sleep(0.2)
        assert not waiting.done()

        await record(holder, "k-1", 201, {"id": 1})
        await holder.commit()

        assert await waiting == SavedResponse(status_code=201, body={"id": 1})


async def test_a_concurrent_claim_takes_over_when_the_holder_rolls_back(
    committed: None,
) -> None:
    async with SessionLocal() as holder, SessionLocal() as waiter:
        assert await claim(holder, "k-1", "hash") is None
        waiting = asyncio.create_task(claim(waiter, "k-1", "hash"))
        await asyncio.sleep(0.2)
        assert not waiting.done()

        await holder.rollback()

        assert await waiting is None


async def test_concurrent_posts_with_one_key_create_one_transaction(committed: None) -> None:
    async with SessionLocal() as setup:
        account = await make_account(setup)
        await setup.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        responses = await asyncio.gather(
            *(
                ac.post("/transactions", json=_body(account.id), headers=_keyed("k-1"))
                for _ in range(5)
            )
        )

    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["id"] for r in responses}) == 1
    async with SessionLocal() as check:
        assert await _count(check) == 1
