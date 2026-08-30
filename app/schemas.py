"""Pydantic schemas for request and response bodies."""

from pydantic import BaseModel, ConfigDict


class AccountCreate(BaseModel):
    """Fields accepted when creating an account."""

    name: str
    iban: str | None = None


class AccountRead(BaseModel):
    """An account as returned to the client."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    iban: str | None


class CategoryCreate(BaseModel):
    """Fields accepted when creating a category."""

    name: str
    parent_id: int | None = None
    is_recurring: bool = False


class CategoryRead(BaseModel):
    """A category as returned to the client."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    parent_id: int | None
    is_recurring: bool
