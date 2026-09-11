"""CSV ingestion: a lazy pipeline to parse, normalize, dedup then persist."""

import csv
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BankName, Transaction


def _cents_decimal_point(raw: str) -> int:
    """Convert an amount like '1234.56' to 123456."""
    return int(Decimal(raw.strip()) * 100)


def _date_yyyymmdd(raw: str) -> date:
    """Convert a date like '20260315' to a date object."""
    return datetime.strptime(raw.strip(), "%Y%m%d").date()


@dataclass(frozen=True)
class ImportProfile:
    """How one bank's export maps onto ParsedRow."""

    name: str
    delimiter: str
    has_header: bool
    booked_on: int
    amount: int
    balance_after: int | None
    description: int
    parse_date: Callable[[str], date]
    parse_amount: Callable[[str], int]


ABN_AMRO = ImportProfile(
    name="ABN AMRO",
    delimiter=",",
    has_header=True,
    booked_on=2,
    amount=6,
    balance_after=5,
    description=7,
    parse_date=_date_yyyymmdd,
    parse_amount=_cents_decimal_point,
)

PROFILES: dict[BankName, ImportProfile] = {BankName.ABN_AMRO: ABN_AMRO}
if set(PROFILES) != set(BankName):
      raise RuntimeError(
          f"BankName members without an ImportProfile: {set(BankName) - set(PROFILES)}"
        )

@dataclass(frozen=True)
class ImportSummary:
    """How many rows a file held, and how many were new."""

    rows_read: int
    inserted: int


@dataclass(frozen=True)
class ParsedRow:
    """One transaction parsed from a CSV, before it becomes a Transaction."""

    booked_on: date
    amount_cents: int
    balance_after_cents: int | None
    description: str

    @property
    def dedup_key(self) -> tuple[int | None, int]:
        """The account-scoped half of the dedup key."""
        return (self.balance_after_cents, self.amount_cents)


def parse(lines: Iterable[str], profile: ImportProfile) -> Iterator[ParsedRow]:
    """Parse export lines into rows according to `profile`."""
    reader = csv.reader(lines, delimiter=profile.delimiter)

    indices = [profile.booked_on, profile.amount, profile.description]
    if profile.balance_after is not None:
        indices.append(profile.balance_after)
    required = max(indices) + 1

    for lineno, fields in enumerate(reader, start=1):
        if lineno == 1 and profile.has_header:
            continue
        if not fields:
            continue
        if len(fields) < required:
            raise ValueError(
                f"line {lineno}: expected at least {required} columns, got {len(fields)}"
            )
        try:
            yield ParsedRow(
                booked_on=profile.parse_date(fields[profile.booked_on]),
                amount_cents=profile.parse_amount(fields[profile.amount]),
                balance_after_cents=(
                    None
                    if profile.balance_after is None
                    else profile.parse_amount(fields[profile.balance_after])
                ),
                description=fields[profile.description],
            )
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"line {lineno}: {exc!r} in {fields!r}") from exc


def normalize(rows: Iterator[ParsedRow]) -> Iterator[ParsedRow]:
    """Collapse whitespace in descriptions."""
    for row in rows:
        yield replace(row, description=" ".join(row.description.split()))


def dedup(rows: Iterator[ParsedRow], existing: set[tuple[int | None, int]]) -> Iterator[ParsedRow]:
    """Drop rows already stored, and duplicates within this file."""
    seen = set(existing)
    for row in rows:
        if row.dedup_key in seen:
            continue
        seen.add(row.dedup_key)
        yield row


async def existing_keys(
    session: AsyncSession, account_id: int, start: date, end: date
) -> set[tuple[int | None, int]]:
    """Load dedup keys already stored for this account in a date range."""
    stmt = select(Transaction.balance_after_cents, Transaction.amount_cents).where(
        Transaction.account_id == account_id,
        Transaction.balance_after_cents.is_not(None),
        Transaction.booked_on.between(start, end),
    )
    result = await session.execute(stmt)
    return {(balance, amount) for balance, amount in result.all()}


async def import_rows(
    session: AsyncSession,
    account: Account,
    lines: Iterable[str],
    batch_size: int = 500,
) -> ImportSummary:
    """Import transactions for one account, returning the number inserted."""
    profile = PROFILES[BankName(account.bank_name)]

    if profile.balance_after is None:
        raise NotImplementedError(
            f"{profile.name} exports no running balance; dedup Path 2 "
            "(content hash + occurrence) is designed but not built."
        )

    rows = list(normalize(parse(lines, profile)))
    if not rows:
        return ImportSummary(rows_read=0, inserted=0)

    start = min(row.booked_on for row in rows)
    end = max(row.booked_on for row in rows)
    existing = await existing_keys(session, account.id, start, end)

    inserted = 0
    for row in dedup(iter(rows), existing):
        session.add(
            Transaction(
                account_id=account.id,
                booked_on=row.booked_on,
                amount_cents=row.amount_cents,
                balance_after_cents=row.balance_after_cents,
                description=row.description,
            )
        )
        inserted += 1
        if inserted % batch_size == 0:
            await session.flush()

    await session.flush()
    return ImportSummary(rows_read=len(rows), inserted=inserted)
