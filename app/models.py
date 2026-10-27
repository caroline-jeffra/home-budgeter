"""Declarative base and ORM models."""

import enum
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    MetaData,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
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


class BankName(enum.StrEnum):
    """Banks with an import profile. The value is the stored `accounts.bank_name`."""

    ABN_AMRO = "abn_amro"


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
    distinct under a unique constraint.
    `bank_name` selects the import profile in `app.ingest`."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    bank_name: Mapped[BankName] = mapped_column(
        Enum(
            BankName,
            native_enum=False,
            create_constraint=True,
            name="bank_name",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    iban: Mapped[str | None] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ImportBatch(Base):
    """One batch of transactions from a bank or credit card imported in a single CSV."""

    __tablename__ = "import_batches"
    __table_args__ = (
        UniqueConstraint("account_id", "file_sha256"),
        CheckConstraint("(voided_at IS NULL) = (void_reason IS NULL)", name="void_reason"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    filename: Mapped[str]
    size: Mapped[int]
    file_sha256: Mapped[str]
    raw_bytes: Mapped[bytes] = mapped_column(deferred=True)
    uploaded_at: Mapped[datetime] = mapped_column(server_default=func.now())
    bank_profile: Mapped[BankName] = mapped_column(
        Enum(
            BankName,
            native_enum=False,
            create_constraint=True,
            name="bank_name",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    normalizer_version: Mapped[int]
    rows_read: Mapped[int]
    inserted: Mapped[int]
    skipped: Mapped[int]
    cosmetic_matches: Mapped[int] = mapped_column(server_default="0")
    real_disagreements: Mapped[int] = mapped_column(server_default="0")
    voided_at: Mapped[datetime | None]
    void_reason: Mapped[str | None]


class IdempotencyKey(Base):
    """A client-supplied Idempotency-Key and the response saved for it."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(primary_key=True)
    request_hash: Mapped[str]
    status_code: Mapped[int | None]
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Transaction(Base):
    """One transaction derived from a bank or credit card CSV import."""

    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "booked_on", "balance_after_cents", "amount_cents", "occurrence"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    booked_on: Mapped[date]
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    balance_before_cents: Mapped[int | None] = mapped_column(BigInteger)
    balance_after_cents: Mapped[int | None] = mapped_column(BigInteger)
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
    raw_description: Mapped[str]
    normalizer_version: Mapped[int]
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    line_no: Mapped[int | None]
    occurrence: Mapped[int] = mapped_column(server_default="0")
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
