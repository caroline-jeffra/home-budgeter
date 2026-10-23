"""CSV ingestion: a lazy pipeline to parse, normalize, dedup then persist."""

import csv
import io
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import batched

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BankName, Transaction


def _date_yyyymmdd(raw: str) -> date:
    """Convert a date like '20260315' to a date object."""
    return datetime.strptime(raw.strip(), "%Y%m%d").date()


@dataclass(frozen=True)
class ImportProfile:
    """How one bank's export maps onto ParsedRow."""

    name: str
    delimiter: str
    fallback_encoding: str | None
    has_header: bool
    booked_on: int
    amount: int
    balance_before: int | None
    balance_after: int | None
    description: int
    parse_date: Callable[[str], date]
    decimal_separator: str
    thousands_separator: str | None

    @property
    def amount_pattern(self) -> re.Pattern[str]:
        """What a valid amount looks like: optional sign, whole part, up to two decimals."""
        whole = r"\d+"
        if self.thousands_separator is not None:
            group = re.escape(self.thousands_separator)
            whole = rf"(?:\d{{1,3}}(?:{group}\d{{3}})+|\d+)"
        point = re.escape(self.decimal_separator)
        return re.compile(rf"[+-]?{whole}(?:{point}\d{{1,2}})?")


ABN_AMRO = ImportProfile(
    name="ABN AMRO",
    delimiter=",",
    fallback_encoding="cp1252",
    has_header=True,
    booked_on=2,
    amount=6,
    balance_before=4,
    balance_after=5,
    description=7,
    parse_date=_date_yyyymmdd,
    decimal_separator=".",
    thousands_separator=None,
)

PROFILES: dict[BankName, ImportProfile] = {BankName.ABN_AMRO: ABN_AMRO}
if set(PROFILES) != set(BankName):
    raise RuntimeError(
        f"BankName members without an ImportProfile: {set(BankName) - set(PROFILES)}"
    )


def parse_cents(raw: str, profile: ImportProfile) -> int:
    """Convert an amount in `profile`'s format, like '-1.234,56', to cents."""
    value = raw.strip()
    if not profile.amount_pattern.fullmatch(value):
        raise ValueError(f"amount {raw!r} is not in {profile.name}'s format")
    if profile.thousands_separator is not None:
        value = value.replace(profile.thousands_separator, "")
    value = value.replace(profile.decimal_separator, ".")
    return int(Decimal(value) * 100)


@dataclass(frozen=True)
class Disagreement:
    """A matched row whose incoming description really differs from the stored one."""

    booked_on: date
    amount_cents: int
    line_no: int | None
    stored_description: str
    incoming_description: str


@dataclass(frozen=True)
class ImportSummary:
    """How many rows a file held, how many were new, and how matches compared."""

    rows_read: int
    inserted: int
    cosmetic: int = 0
    disagreements: tuple[Disagreement, ...] = ()


DedupKey = tuple[date, int | None, int, int]


@dataclass(frozen=True)
class ParsedRow:
    """One transaction parsed from a CSV, before it becomes a Transaction."""

    booked_on: date
    amount_cents: int
    balance_after_cents: int | None
    description: str
    raw_description: str
    balance_before_cents: int | None
    line_no: int | None
    occurrence: int = 0

    @property
    def dedup_key(self) -> DedupKey:
        """The account-scoped part of the dedup key."""
        return (self.booked_on, self.balance_after_cents, self.amount_cents, self.occurrence)


def open_text(raw: bytes, profile: ImportProfile) -> io.TextIOWrapper:
    """Pick the first encoding that decodes all of `raw`, then stream it line by line."""
    encoding = "utf-8-sig"
    try:
        raw.decode(encoding)
    except UnicodeDecodeError:
        if profile.fallback_encoding is None:
            raise
        encoding = profile.fallback_encoding
        raw.decode(encoding)
    return io.TextIOWrapper(io.BytesIO(raw), encoding=encoding, newline="")


def parse(lines: Iterable[str], profile: ImportProfile) -> Iterator[ParsedRow]:
    """Parse export lines into rows according to `profile`."""
    reader = csv.reader(lines, delimiter=profile.delimiter)

    indices = [profile.booked_on, profile.amount, profile.description]
    if profile.balance_before is not None:
        indices.append(profile.balance_before)
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
            row = ParsedRow(
                booked_on=profile.parse_date(fields[profile.booked_on]),
                amount_cents=parse_cents(fields[profile.amount], profile),
                balance_after_cents=(
                    None
                    if profile.balance_after is None
                    else parse_cents(fields[profile.balance_after], profile)
                ),
                description=fields[profile.description],
                raw_description=fields[profile.description],
                balance_before_cents=(
                    None
                    if profile.balance_before is None
                    else parse_cents(fields[profile.balance_before], profile)
                ),
                line_no=lineno,
            )
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"line {lineno}: {exc!r} in {fields!r}") from exc
        if (
            row.balance_before_cents is not None
            and row.balance_after_cents is not None
            and row.balance_before_cents + row.amount_cents != row.balance_after_cents
        ):
            raise ValueError(
                f"line {lineno}: startsaldo {row.balance_before_cents} + amount "
                f"{row.amount_cents} != endsaldo {row.balance_after_cents}"
            )
        yield row


NORMALIZER_VERSION = 1
_SEPARATORS = str.maketrans(",:/", "   ")

NOISE_WORDS = frozenset(
    {
        # transaction type
        "bea",
        "sepa",
        "trtp",
        "incasso",
        "algemeen",
        "doorlopend",
        "overboeking",
        "ideal",
        "wero",
        # sepa field labels
        "naam",
        "omschrijving",
        "iban",
        "bic",
        "kenmerk",
        "machtiging",
        "incassant",
        "land",
        "nr",
        # placeholders
        "notprovided",
        # legal form
        "b.v.",
        "bv",
        "nv",
    }
)

_LONG_DIGIT_RUN = re.compile(r"\d{4,}")


def _is_noise(token: str) -> bool:
    """True for noise words, letterless tokens, and long digit runs."""
    return (
        token in NOISE_WORDS
        or not any(char.isalpha() for char in token)
        or _LONG_DIGIT_RUN.search(token) is not None
    )


def normalize_text(text: str) -> str:
    """Casefold, compose to NFC, split on , : / then drop noise tokens."""
    folded = unicodedata.normalize("NFC", text.casefold()).translate(_SEPARATORS)
    return " ".join(token for token in folded.split() if not _is_noise(token))


class MatchKind(StrEnum):
    """How an incoming row's description compares with the stored row it matched."""

    EXACT = "exact"
    COSMETIC = "cosmetic"
    REAL = "real"


def match_kind(stored_raw: str, incoming_raw: str) -> MatchKind:
    """Compare raw texts, then both re-normalized with today's rules; empty agrees with nothing."""
    if stored_raw == incoming_raw:
        return MatchKind.EXACT
    normalized = normalize_text(stored_raw)
    if normalized and normalized == normalize_text(incoming_raw):
        return MatchKind.COSMETIC
    return MatchKind.REAL


def normalize(rows: Iterator[ParsedRow]) -> Iterator[ParsedRow]:
    """Normalize each row's description from its raw text."""
    for row in rows:
        yield replace(row, description=normalize_text(row.raw_description))


def assign_occurrences(rows: Iterator[ParsedRow]) -> Iterator[ParsedRow]:
    """Number rows sharing date, balance and amount, 0-based in file order."""
    counts: Counter[tuple[date, int | None, int]] = Counter()
    for row in rows:
        same = (row.booked_on, row.balance_after_cents, row.amount_cents)
        yield replace(row, occurrence=counts[same])
        counts[same] += 1


def dedup(rows: Iterator[ParsedRow], existing: Iterable[DedupKey]) -> Iterator[ParsedRow]:
    """Drop rows already stored, and duplicates within this file."""
    seen = set(existing)
    for row in rows:
        if row.dedup_key in seen:
            continue
        seen.add(row.dedup_key)
        yield row


async def stored_descriptions(
    session: AsyncSession, account_id: int, keys: list[DedupKey]
) -> dict[DedupKey, str]:
    """Return the stored raw description for each of `keys` already in this account."""
    columns = (
        Transaction.booked_on,
        Transaction.balance_after_cents,
        Transaction.amount_cents,
        Transaction.occurrence,
    )
    stmt = select(*columns, Transaction.raw_description).where(
        Transaction.account_id == account_id,
        tuple_(*columns).in_(keys),
    )
    result = await session.execute(stmt)
    return {
        (booked, balance, amount, occ): raw for booked, balance, amount, occ, raw in result.all()
    }


async def import_rows(
    session: AsyncSession,
    account: Account,
    lines: Iterable[str],
    import_batch_id: int,
    batch_size: int = 500,
) -> ImportSummary:
    """Import transactions for one account, returning the number inserted."""
    profile = PROFILES[BankName(account.bank_name)]

    if profile.balance_after is None:
        raise NotImplementedError(
            f"{profile.name} exports no running balance; dedup Path 2 "
            "(content hash + occurrence) is designed but not built."
        )

    rows_read = 0
    inserted = 0
    cosmetic = 0
    disagreements: list[Disagreement] = []
    for chunk in batched(
        assign_occurrences(normalize(parse(lines, profile))), batch_size, strict=False
    ):
        rows_read += len(chunk)
        stored = await stored_descriptions(session, account.id, [row.dedup_key for row in chunk])
        for row in chunk:
            stored_raw = stored.get(row.dedup_key)
            if stored_raw is None:
                continue
            kind = match_kind(stored_raw, row.raw_description)
            if kind is MatchKind.COSMETIC:
                cosmetic += 1
            elif kind is MatchKind.REAL:
                disagreements.append(
                    Disagreement(
                        booked_on=row.booked_on,
                        amount_cents=row.amount_cents,
                        line_no=row.line_no,
                        stored_description=stored_raw,
                        incoming_description=row.raw_description,
                    )
                )
        for row in dedup(iter(chunk), stored.keys()):
            session.add(
                Transaction(
                    account_id=account.id,
                    booked_on=row.booked_on,
                    amount_cents=row.amount_cents,
                    balance_before_cents=row.balance_before_cents,
                    balance_after_cents=row.balance_after_cents,
                    description=row.description,
                    raw_description=row.raw_description,
                    line_no=row.line_no,
                    occurrence=row.occurrence,
                    normalizer_version=NORMALIZER_VERSION,
                    import_batch_id=import_batch_id,
                )
            )
            inserted += 1
        await session.flush()

    return ImportSummary(
        rows_read=rows_read,
        inserted=inserted,
        cosmetic=cosmetic,
        disagreements=tuple(disagreements),
    )
