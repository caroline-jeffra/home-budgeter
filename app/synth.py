"""Synthetic ABN AMRO exports: seeded, deterministic, with valid balance chains."""

import csv
import io
import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta

from app.ingest import ABN_AMRO, ImportProfile

ACCOUNT_NUMBER = "12345678"
HEADER = (
    "accountNumber",
    "mutationcode",
    "transactiondate",
    "valuedate",
    "startsaldo",
    "endsaldo",
    "amount",
    "description",
)

# One holds the delimiter, so every file exercises quoting.
MERCHANTS = (
    "BEA, Google Pay  ALBERT HEIJN 1234,PAS410",
    "/TRTP/SEPA Incasso algemeen doorlopend/NAME/Vattenfall",
    "/TRTP/iDEAL/NAME/Bol.com",
)
INCOME = "/TRTP/SEPA OVERBOEKING/NAME/Werkgever BV/REMI/Salaris"


@dataclass(frozen=True)
class SynthRow:
    """One generated transaction, before it is written as a line."""

    booked_on: date
    balance_before_cents: int
    amount_cents: int
    balance_after_cents: int
    description: str


def generate(
    seed: int, count: int, start: date, opening_cents: int = 100_000
) -> Iterator[SynthRow]:
    """Yield `count` chained rows from `start`: the same rows for the same seed."""
    rng = random.Random(seed)
    booked_on = start
    balance = opening_cents
    for _ in range(count):
        if rng.random() < 0.1:
            amount, description = rng.randint(20_000, 300_000), INCOME
        else:
            amount, description = -rng.randint(1, 15_000), rng.choice(MERCHANTS)
        yield SynthRow(booked_on, balance, amount, balance + amount, description)
        balance += amount
        if rng.random() < 0.4:
            booked_on += timedelta(days=1)


def format_cents(cents: int, profile: ImportProfile) -> str:
    """Write cents in `profile`'s format, like -123456 as '-1.234,56'; parse_cents reverses it."""
    sign = "-" if cents < 0 else ""
    whole, fraction = divmod(abs(cents), 100)
    digits = str(whole)
    if profile.thousands_separator is not None:
        digits = f"{whole:,}".replace(",", profile.thousands_separator)
    return f"{sign}{digits}{profile.decimal_separator}{fraction:02d}"


def _line(fields: Iterable[str], delimiter: str) -> str:
    """One CSV line, with a field quoted only when it needs it."""
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=delimiter, lineterminator="").writerow(fields)
    return buffer.getvalue()


def render(rows: Iterable[SynthRow], profile: ImportProfile = ABN_AMRO) -> Iterator[str]:
    """Write rows as ABN AMRO export lines, header first, amounts in `profile`'s format."""
    yield _line(HEADER, profile.delimiter)
    for row in rows:
        day = row.booked_on.strftime("%Y%m%d")
        yield _line(
            (
                ACCOUNT_NUMBER,
                "EUR",
                day,
                day,
                format_cents(row.balance_before_cents, profile),
                format_cents(row.balance_after_cents, profile),
                format_cents(row.amount_cents, profile),
                row.description,
            ),
            profile.delimiter,
        )
