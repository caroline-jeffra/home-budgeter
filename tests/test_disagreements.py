"""Tests for disagreement classification: exact, cosmetic and real matches per P2-D2."""

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest import (
    NORMALIZER_VERSION,
    Disagreement,
    ImportSummary,
    MatchKind,
    import_rows,
    match_kind,
    normalize_text,
)
from app.models import Account, ImportBatch, Transaction
from app.synth import SynthRow, cosmetic, generate, render, reword
from tests.factories import make_account, make_import_batch
from tests.test_continuity import _upload
from tests.test_ingest import AUTH

START = date(2026, 1, 1)
REWORDED = "Jumbo Amsterdam"


def _rows(seed: int) -> list[SynthRow]:
    return list(generate(seed=seed, count=50, start=START))


def _line_no(index: int) -> int:
    """The CSV line a generated row lands on: line 1 is the header."""
    return index + 2


# match_kind: stored raw text against incoming raw text, both normalized with the current rules.


@pytest.mark.parametrize(
    ("stored", "incoming", "kind"),
    [
        pytest.param("Albert Heijn", "Albert Heijn", MatchKind.EXACT, id="identical"),
        pytest.param("Albert Heijn", "ALBERT HEIJN   ", MatchKind.COSMETIC, id="case-and-padding"),
        pytest.param("Albert Heijn 1234", "Albert Heijn 9876", MatchKind.COSMETIC, id="digit-run"),
        pytest.param("Café de Jaren", "Café de Jaren", MatchKind.COSMETIC, id="nfc"),
        pytest.param("BEA, Albert Heijn", "Albert Heijn", MatchKind.COSMETIC, id="noise-word"),
        pytest.param("Albert Heijn", "Jumbo", MatchKind.REAL, id="reworded"),
        pytest.param("BEA 1234", "BEA 1234", MatchKind.EXACT, id="identical-noise-only"),
        pytest.param("BEA 1234", "SEPA 5678", MatchKind.REAL, id="both-noise-only"),
        pytest.param("BEA 1234", "Jumbo", MatchKind.REAL, id="one-noise-only"),
    ],
)
def test_match_kind(stored: str, incoming: str, kind: MatchKind) -> None:
    assert match_kind(stored, incoming) is kind


def test_two_noise_only_descriptions_both_normalize_to_empty() -> None:
    """The premise of the both-noise-only case: an empty match is no evidence of agreement."""
    assert normalize_text("BEA 1234") == normalize_text("SEPA 5678") == ""


# import_rows: every dedup-key match is classified, and the stored row never changes.


async def _import_twice(
    session: AsyncSession, first: list[SynthRow], second: list[SynthRow], batch_size: int = 500
) -> tuple[Account, ImportSummary]:
    """Imports `first`, then `second` into the same account, returning the second summary."""
    account = await make_account(session)
    one = await make_import_batch(session, account)
    two = await make_import_batch(session, account)
    await import_rows(session, account, list(render(first)), import_batch_id=one.id)
    summary = await import_rows(
        session, account, list(render(second)), import_batch_id=two.id, batch_size=batch_size
    )
    return account, summary


async def test_a_first_import_classifies_nothing(session: AsyncSession) -> None:
    account = await make_account(session)
    batch = await make_import_batch(session, account)

    summary = await import_rows(session, account, list(render(_rows(31))), import_batch_id=batch.id)

    assert summary == ImportSummary(rows_read=50, inserted=50, cosmetic=0, disagreements=())


async def test_an_identical_reimport_is_all_exact(session: AsyncSession) -> None:
    rows = _rows(32)

    _, summary = await _import_twice(session, rows, rows)

    assert summary == ImportSummary(rows_read=50, inserted=0, cosmetic=0, disagreements=())


async def test_a_cosmetic_difference_is_counted_but_not_listed(session: AsyncSession) -> None:
    rows = _rows(33)

    _, summary = await _import_twice(session, rows, cosmetic(rows, 10))

    assert summary.inserted == 0
    assert summary.cosmetic == 1
    assert summary.disagreements == ()


async def test_a_real_disagreement_is_listed(session: AsyncSession) -> None:
    rows = _rows(34)

    _, summary = await _import_twice(session, rows, reword(rows, 10, REWORDED))

    assert summary.inserted == 0
    assert summary.cosmetic == 0
    assert summary.disagreements == (
        Disagreement(
            booked_on=rows[10].booked_on,
            amount_cents=rows[10].amount_cents,
            line_no=_line_no(10),
            stored_description=rows[10].description,
            incoming_description=REWORDED,
        ),
    )


async def test_the_stored_description_wins(session: AsyncSession) -> None:
    rows = _rows(35)

    account, _ = await _import_twice(session, rows, reword(rows, 10, REWORDED))

    stored = await session.scalar(
        select(Transaction).where(
            Transaction.account_id == account.id,
            Transaction.booked_on == rows[10].booked_on,
            Transaction.balance_after_cents == rows[10].balance_after_cents,
            Transaction.amount_cents == rows[10].amount_cents,
        )
    )
    assert stored is not None
    assert stored.raw_description == rows[10].description
    assert stored.description == normalize_text(rows[10].description)


async def test_disagreements_are_found_in_every_chunk_in_file_order(
    session: AsyncSession,
) -> None:
    rows = _rows(36)
    second = reword(reword(rows, 3, REWORDED), 40, "Kruidvat Utrecht")

    _, summary = await _import_twice(session, rows, second, batch_size=7)

    assert [d.line_no for d in summary.disagreements] == [_line_no(3), _line_no(40)]
    assert [d.incoming_description for d in summary.disagreements] == [
        REWORDED,
        "Kruidvat Utrecht",
    ]


async def test_classification_renormalizes_the_stored_raw_text(session: AsyncSession) -> None:
    """Between a normalizer bump and its backfill, the stored `description` is stale (finding 6)."""
    rows = _rows(37)
    account = await make_account(session)
    one = await make_import_batch(session, account)
    two = await make_import_batch(session, account)
    await import_rows(session, account, list(render(rows)), import_batch_id=one.id)
    await session.execute(
        update(Transaction)
        .where(Transaction.account_id == account.id)
        .values(description="normalized by an older version")
    )

    summary = await import_rows(
        session, account, list(render(cosmetic(rows, 10))), import_batch_id=two.id
    )

    assert summary.cosmetic == 1
    assert summary.disagreements == ()


# The endpoint: counts in the response and on the batch, the list in the response only.


async def test_a_first_upload_reports_no_disagreements(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)

    response = await _upload(client, account.id, list(render(_rows(41))))

    assert response.status_code == 200
    assert response.json()["cosmetic_matches"] == 0
    assert response.json()["real_disagreements"] == 0
    assert response.json()["disagreements"] == []


async def test_the_import_response_counts_and_lists_disagreements(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)
    rows = _rows(42)
    await _upload(client, account.id, list(render(rows)))

    response = await _upload(
        client, account.id, list(render(reword(cosmetic(rows, 5), 10, REWORDED)))
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["inserted"], body["skipped"]) == (0, 50)
    assert body["cosmetic_matches"] == 1
    assert body["real_disagreements"] == 1
    assert body["disagreements"] == [
        {
            "booked_on": rows[10].booked_on.isoformat(),
            "amount_cents": rows[10].amount_cents,
            "line_no": _line_no(10),
            "stored_description": rows[10].description,
            "incoming_description": REWORDED,
        }
    ]


async def test_counts_are_saved_on_the_batch(client: AsyncClient, session: AsyncSession) -> None:
    account = await make_account(session)
    rows = _rows(43)
    await _upload(client, account.id, list(render(rows)))

    response = await _upload(
        client, account.id, list(render(reword(cosmetic(rows, 5), 10, REWORDED)))
    )

    batch = await session.get(ImportBatch, response.json()["batch_id"])
    assert batch is not None
    assert batch.cosmetic_matches == 1
    assert batch.real_disagreements == 1
    assert batch.normalizer_version == NORMALIZER_VERSION


async def test_a_repeat_upload_returns_the_saved_counts_without_the_list(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)
    rows = _rows(44)
    second = list(render(reword(cosmetic(rows, 5), 10, REWORDED)))
    await _upload(client, account.id, list(render(rows)))
    await _upload(client, account.id, second)

    repeat = await _upload(client, account.id, second)

    body = repeat.json()
    assert body["already_imported"] is True
    assert body["cosmetic_matches"] == 1
    assert body["real_disagreements"] == 1
    assert body["disagreements"] is None


async def test_transactions_expose_their_raw_description(
    client: AsyncClient, session: AsyncSession
) -> None:
    account = await make_account(session)
    rows = _rows(45)
    await _upload(client, account.id, list(render(rows)))

    listed = await client.get(
        "/transactions", params={"account_id": account.id, "limit": 50}, headers=AUTH
    )

    assert sorted(t["raw_description"] for t in listed.json()) == sorted(
        row.description for row in rows
    )
    assert all(t["description"] == normalize_text(t["raw_description"]) for t in listed.json())
