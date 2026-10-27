"""Idempotency keys for manual writes."""

import hashlib
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IdempotencyKey


class KeyReusedError(Exception):
    """The key was already used with a different request body."""


@dataclass(frozen=True)
class SavedResponse:
    """The response a previous request saved under a key."""

    status_code: int
    body: dict[str, Any]


def request_hash(payload: BaseModel) -> str:
    """Hashes the validated model, so key order, whitespace and defaults don't matter."""
    return hashlib.sha256(payload.model_dump_json().encode()).hexdigest()


async def claim(session: AsyncSession, key: str, payload_hash: str) -> SavedResponse | None:
    """Claims `key` for this request. None means this request owns it."""
    claimed = await session.scalar(
        insert(IdempotencyKey)
        .values(key=key, request_hash=payload_hash)
        .on_conflict_do_nothing(index_elements=[IdempotencyKey.key])
        .returning(IdempotencyKey.key)
    )
    if claimed is not None:
        return None

    saved = await session.scalar(select(IdempotencyKey).where(IdempotencyKey.key == key))
    if saved is None or saved.status_code is None or saved.response_body is None:
        raise RuntimeError(f"Idempotency key {key!r} was claimed but never recorded")
    if saved.request_hash != payload_hash:
        raise KeyReusedError(key)
    return SavedResponse(status_code=saved.status_code, body=saved.response_body)


async def record(session: AsyncSession, key: str, status_code: int, body: dict[str, Any]) -> None:
    """Saves the response under a key this request claimed; commits with the caller."""
    await session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.key == key)
        .values(status_code=status_code, response_body=body)
    )
