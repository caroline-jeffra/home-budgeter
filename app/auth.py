"""Authentication seam for the API."""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

bearer_scheme = HTTPBearer(auto_error=False)


async def require_auth(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> None:
    """Reject any request without a valid bearer token.

    `auto_error=False` keeps the
    missing-credentials case inside this function rather than in FastAPI's
    security scheme, so milestone 2 swaps the comparison below for JWT
    verification without touching a caller."""
    if creds is None or not secrets.compare_digest(creds.credentials, settings.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )
