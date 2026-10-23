"""Synthetic ABN AMRO exports: seeded, deterministic, with valid balance chains."""

import csv
import io
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from itertools import accumulate

from app.ingest import ABN_AMRO, ImportProfile, normalize_text

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

# One holds the delimiter, so every file exercises quoting; one is non-ASCII, so cp1252 differs.
MERCHANTS = (
    "BEA, Google Pay  ALBERT HEIJN 1234,PAS410",
    "BEA, Google Pay  Café de Jaren,PAS410",
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


@dataclass(frozen=True)
class Distribution:
    """How generated rows are spread: dates, directions, amounts and merchants."""

    rows_per_day: float = 2.5
    income_share: float = 0.1
    income_cents: tuple[int, int] = (20_000, 300_000)
    spend_cents: tuple[int, int] = (1, 15_000)
    merchants: tuple[tuple[str, float], ...] = tuple((name, 1.0) for name in MERCHANTS)

    def __post_init__(self) -> None:
        if self.rows_per_day < 1:
            raise ValueError(f"rows_per_day must be at least 1, got {self.rows_per_day}")
        if not 0 <= self.income_share <= 1:
            raise ValueError(f"income_share must be between 0 and 1, got {self.income_share}")
        for field, (low, high) in (
            ("income_cents", self.income_cents),
            ("spend_cents", self.spend_cents),
        ):
            if not 1 <= low <= high:
                raise ValueError(f"{field} must satisfy 1 <= low <= high, got {(low, high)}")
        if not self.merchants or any(weight <= 0 for _, weight in self.merchants):
            raise ValueError("merchants needs at least one entry, each with a positive weight")


DEFAULT_DISTRIBUTION = Distribution()


def generate(
    seed: int,
    count: int,
    start: date,
    opening_cents: int = 100_000,
    distribution: Distribution = DEFAULT_DISTRIBUTION,
) -> Iterator[SynthRow]:
    """Yield `count` chained rows from `start`: the same rows for the same seed and spread."""
    rng = random.Random(seed)
    names = [name for name, _ in distribution.merchants]
    cum_weights = list(accumulate(weight for _, weight in distribution.merchants))
    next_day = 1 / distribution.rows_per_day
    booked_on = start
    balance = opening_cents
    for _ in range(count):
        if rng.random() < distribution.income_share:
            amount, description = rng.randint(*distribution.income_cents), INCOME
        else:
            amount = -rng.randint(*distribution.spend_cents)
            [description] = rng.choices(names, cum_weights=cum_weights)
        yield SynthRow(booked_on, balance, amount, balance + amount, description)
        balance += amount
        if rng.random() < next_day:
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


def render(
    rows: Iterable[SynthRow], profile: ImportProfile = ABN_AMRO, date_format: str = "%Y%m%d"
) -> Iterator[str]:
    """Write rows as ABN AMRO export lines, header first, amounts in `profile`'s number format."""
    yield _line(HEADER, profile.delimiter)
    for row in rows:
        day = row.booked_on.strftime(date_format)
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


def to_bytes(lines: Iterable[str], encoding: str = "utf-8") -> bytes:
    """Join lines into an export file's bytes, CRLF-separated."""
    return "\r\n".join(lines).encode(encoding)


def gap(rows: Sequence[SynthRow], start: int, length: int) -> list[SynthRow]:
    """Drop `length` rows from `start`, leaving one break where the chain resumes."""
    if length < 1 or start < 1 or start + length >= len(rows):
        raise ValueError(
            f"a gap needs rows on both sides: start={start}, length={length}, rows={len(rows)}"
        )
    if sum(row.amount_cents for row in rows[start : start + length]) == 0:
        raise ValueError("the dropped rows net to zero, so the chain would show no break")
    return [*rows[:start], *rows[start + length :]]


def fork(rows: Sequence[SynthRow], index: int, amount_cents: int) -> list[SynthRow]:
    """Restate one amount and shift every later balance by the difference."""
    original = rows[index]
    if amount_cents in (0, original.amount_cents):
        raise ValueError(f"restating {original.amount_cents} as {amount_cents} is not a fork")
    shift = amount_cents - original.amount_cents
    restated = replace(
        original,
        amount_cents=amount_cents,
        balance_after_cents=original.balance_after_cents + shift,
    )
    later = [
        replace(
            row,
            balance_before_cents=row.balance_before_cents + shift,
            balance_after_cents=row.balance_after_cents + shift,
        )
        for row in rows[index + 1 :]
    ]
    return [*rows[:index], restated, *later]


def _with_description(rows: Sequence[SynthRow], index: int, description: str) -> list[SynthRow]:
    """A copy of `rows` with one description replaced."""
    return [*rows[:index], replace(rows[index], description=description), *rows[index + 1 :]]


def cosmetic(rows: Sequence[SynthRow], index: int) -> list[SynthRow]:
    """Re-case and pad one description: different raw text, same normalized text."""
    return _with_description(rows, index, f"{rows[index].description.upper()}    ")


def reword(rows: Sequence[SynthRow], index: int, description: str) -> list[SynthRow]:
    """Replace one description with text that normalizes differently: a real disagreement."""
    if normalize_text(description) == normalize_text(rows[index].description):
        raise ValueError(f"{description!r} only differs cosmetically from the original")
    return _with_description(rows, index, description)


def overlap(
    rows: Sequence[SynthRow], first_end: int, second_start: int
) -> tuple[list[SynthRow], list[SynthRow]]:
    """Split rows into two files that share rows[second_start:first_end]."""
    if not 0 <= second_start < first_end <= len(rows):
        raise ValueError(
            f"files ending at {first_end} and starting at {second_start} do not overlap"
        )
    return list(rows[:first_end]), list(rows[second_start:])
