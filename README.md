# Home Budgeter
[![CI](https://github.com/caroline-jeffra/home-budgeter/actions/workflows/ci.yml/badge.svg)](https://github.com/caroline-jeffra/home-budgeter/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.13-blue)

**Stack**

FastAPI · SQLAlchemy 2.0 · Postgres 16 · Docker Compose + Caddy · uv

## Introduction

Home Budgeter is a household budgeting API for a single household user. Transaction data is loaded via importing CSVs of transaction records. These imports are deduplicated, categorized, tracked against monthly envelopes with a view to budget to zero - all planned income has a planned destination. 

This project came out of a budgeting practice my family uses which has been useful for maintaining good spending habits but had become prohibitively time consuming. Building an API to improve automation and reduce the monthly time commitment devoted to budgeting will mean that we can continue with these good habits while reducing the cognitive load and time commitment it currently represents.

## Ingestion

Transactions are ingested by merging a bank's CSV export against what is already stored for that account. Uploading the same file twice changes nothing, rows already stored are skipped, and an import whose balances disagree with stored rows is rejected wholesale rather than half-applied.

Bank exports are messy in predictable ways: consecutive downloads overlap, the same file gets downloaded twice, a missed month leaves a hole, and a bank occasionally restates history so an old row comes back with different figures. Ingestion has a defined answer for each of these.

### The dedup key

Each row is matched against stored rows by a key made of booking date, balance after the transaction, amount, and occurrence, scoped to the account.

- Date and amount alone are not unique: two 3.50€ coffees on the same day would collapse into one.
- Balance after separates them: the first coffee leaves 96.50€, the second 93.00€, so both survive. The running balance is the bank's own record of order, and every row's `start balance + amount == end balance` is checked when the file is parsed.
- Occurrence covers the rare case the balance alone cannot: a refund and a purchase that return the balance to the same value on the same day for the same amount. It is the row's position among rows in the same file that share date, balance and amount.

The description is deliberately not part of the key, because banks reword descriptions between exports (see Descriptions below).

Banks that export no running balance are not supported yet; their import fails loudly rather than deduplicating on a weaker key.

### Overlaps, gaps and conflicts

The account's imported rows form a balance chain: each row's opening balance should equal the previous row's closing balance. A break in that chain is how the system knows something is missing or wrong.

| Case | What it is | How it is detected | What the import does |
| --- | --- | --- | --- |
| Duplicate file | The exact same file uploaded again | File hash matches a stored batch | Returns the original batch with `already_imported: true`; nothing is written |
| Overlap | The end of one export repeats the start of the next | Rows match stored rows on the dedup key | Skips the matched rows, imports only the new ones |
| Gap | Rows missing between two imports, e.g. a skipped month | A new chain break outside the dates already stored | Imports the file and reports the break in `gaps_opened` |
| Gap filled | A later file supplies the missing rows | A previous chain break disappears | Imports the rows and reports the break in `gaps_closed` |
| Conflict | The file disagrees with stored rows on dates they share, e.g. a restated row | A new chain break dated inside the overlap with stored rows | Rejects the whole file with `409`; nothing is imported |

Gaps are not errors: the import succeeds, and `GET /accounts/{id}/reconciliation` lists every gap still open. Conflicts are never stored. To accept the bank's restated version, void the stored batch (`python -m app.cli void <batch_id> --reason "..."`) and import the new file.

### Descriptions

When an incoming row matches a stored row on the dedup key, their descriptions are compared:

- **Exact**: the raw text is identical.
- **Cosmetic**: the text differs only in case, spacing, noise words or reference numbers, so it is identical once normalized. These are counted in `cosmetic_matches`.
- **Real**: the text genuinely differs. These are listed in `disagreements` with both versions.

Neither kind blocks the import, and the stored row is never overwritten. The key already proved it is the same transaction; the description difference is reported so a person can look.

### Raw vs normalized

Every row keeps the bank's `raw_description` exactly as exported, alongside the normalized text used for matching and categorizing, and the `normalizer_version` that produced it. Normalization (Unicode NFC, case folding, dropping noise words and digit runs) can be improved later and rows re-normalized from the raw text, without re-importing anything.

### Idempotency keys

The file hash makes uploading a file safe to repeat. Manual writes get the same protection from an `Idempotency-Key` header on `POST /transactions`: a retry with the same key and body replays the saved response instead of creating a second transaction, and reusing a key with a different body is rejected with `422`.

### No fuzzy matching

Rows match exactly on the dedup key or not at all. A fuzzy match would occasionally merge two real transactions, silently and in a way that is hard to find later. An exact key plus a reported disagreement keeps every decision visible and leaves the judgment calls to a person.

## Milestones

Two milestones are planned, dividing API from frontend. A discarded test spike has already settled the basic product requirements, which allows for full focus on creating a full featured API to support a polished frontend later on. 

**Milestone 1** is the deployed API with OpenAPI docs as the interface. This favors building a complete working backend over making an abbreviated but usable frontend + backend.

**Milestone 2** is a friendly frontend UI for household use on multiple device types. This milestone delivers a household-usable application. 

## Authentication

This app is single-user by design via a bearer token rather than a permission model. `Authorization: Bearer <API_TOKEN>` is required for every endpoint except `/health`. This will be replaced with a shared household login during Milestone 2. The FastAPI dependency `require_auth` with `auto_error=False` will facilitate a swap to JWT verification within one function without modifying any callers. 

## Data Model

Six tables:

- `accounts` The sources that transactions are imported from. Unique: `name` and `iban`
- `categories` Spending buckets. Self referencing to support a hierarchy later.
- `transactions` Belong to an account and express their amounts as an integer number of cents (not a float).
- `transaction_splits` Assigns portions of a single transaction to different categories in support of making cash withdrawals divisible and categorizable.
- `budget_periods` A single month's income/outgoing period during which allocations are planned.
- `transfer_matches` Links the two sides of a transfer between owned accounts.

Enum-like columns are `VARCHAR` with a CHECK constraint instead of a native Postgres enum. As a result, adding a value is a migration rather than a type alteration.

## Running the project

**Prerequisites**

To run locally, the following are required:

- uv
- Docker (with Compose v2)

**Setup**

Run 

```bash
uv sync
```

Make a local .env

```bash
cp .env.example .env
```

Generate a password for Postgres and set it in the .env as `POSTGRES_PASSWORD` and within the `DATABASE_URL` at `postgresql+psycopg://budget:<POSTGRES_PASSWORD>@localhost:5432/budget`

```bash
openssl rand -hex 32 
```

Generate a second value for `API_TOKEN` and set it in the .env. Every endpoint except `/health` requires it as `Authorization: Bearer <API_TOKEN>`, and the tests authenticate with the same variable, so leaving it empty fails the authenticated tests with a 401.

```bash
openssl rand -hex 32
```

Create a local override for Docker's database ports

```bash
touch docker-compose.override.yml
```

With the following content:

```yml
services:
  db:
    ports:
      - "5432:5432"
```

Spin up the project in Docker

```bash
docker compose up -d db
```

Apply the migrations to the development database. Nothing migrates on startup, so this is needed on a fresh machine and after any pull that adds a migration.

```bash
uv run alembic upgrade head
```

Make a local test config

```bash
cp .env.test.example .env.test
```

Set the same `POSTGRES_PASSWORD` you used above. This and `tests/__init__.py` keep the test suite pointed at `budget_test` instead of your development data.

Create the test database. Compose only creates `budget`, so `budget_test` is made once per machine; the suite builds and tears down its schema with Alembic on every run.

```bash
docker compose exec db psql -U budget -d postgres -c 'CREATE DATABASE budget_test OWNER budget;'
```

Finally run the tests and code checks

```bash
uv run ruff check && uv run mypy && uv run pytest 
```

Boot the app

```bash
uv run uvicorn app.main:app --reload
```

The API will be available at http://localhost:8000/docs

**Deployment Topology**

When deploying, spin up the api, Postgres and Caddy on the server.

```bash
docker compose up -d
```
