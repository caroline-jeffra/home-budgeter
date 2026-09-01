"""Declarative base and ORM models."""

import enum
from datetime import date, datetime

from sqlalchemy import BigInteger, Enum, ForeignKey, MetaData, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base for all ORM models, carries the constraint naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class PeriodState(enum.StrEnum):
    """Enum for the acceptable state attributes for a budget period."""

    PLANNED = "planned"
    ACTIVE = "active"
    CLOSED = "closed"


class CategorizedBy(enum.StrEnum):
    """Enum for the way that a transaction was categorized."""

    MANUAL = "manual"
    RULE = "rule"


class BudgetPeriod(Base):
    """A budget period, identified by its month/year."""

    __tablename__ = "budget_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(unique=True)
    expected_income_cents: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[PeriodState] = mapped_column(
        Enum(
            PeriodState,
            native_enum=False,
            create_constraint=True,
            name="period_state",
            values_callable=lambda e: [m.value for m in e],
        )
    )


class Account(Base):
    """An account from which transaction data was imported.

    `name` and `iban` are each unique. `iban` is nullable, but NULLs are
    distinct under a unique constraint."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    iban: Mapped[str | None] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Transaction(Base):
    """One transaction derived from a bank or credit card CSV import."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    booked_on: Mapped[date]
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    categorized_by: Mapped[CategorizedBy | None] = mapped_column(
        Enum(
            CategorizedBy,
            native_enum=False,
            create_constraint=True,
            name="categorized_by",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    description: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    splits: Mapped[list["TransactionSplit"]] = relationship(back_populates="transaction")


class Category(Base):
    """A spending category."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    name: Mapped[str] = mapped_column(unique=True)
    is_recurring: Mapped[bool] = mapped_column(default=False)


class TransactionSplit(Base):
    """A portion of a transaction with a specific category."""

    __tablename__ = "transaction_splits"

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    amount_cents: Mapped[int] = mapped_column(BigInteger)

    transaction: Mapped[Transaction] = relationship(back_populates="splits")


class TransferMatch(Base):
    """A pair of transactions representing a transfer of funds between owned accounts."""

    __tablename__ = "transfer_matches"
    __table_args__ = (UniqueConstraint("source_id", "destination_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    destination_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
