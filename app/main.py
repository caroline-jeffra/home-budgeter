"""FastAPI application and route definitions."""

from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session

app = FastAPI(title="Home Budgeter")


@app.get("/health")
async def health_check(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    """Simple round trip execution to supply database connection health check."""
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
