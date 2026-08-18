# Home Budgeter

**Stack**

FastAPI · SQLAlchemy 2.0 · Postgres 16 · Docker Compose + Caddy · uv

## Introduction

Home Budgeter is a household budgeting API for a single household user. Transaction data is loaded via importing CSVs of transaction records. These imports are deduplicated, categorized, tracked against monthly envelopes with a view to budget to zero - all planned income has a planned destination. 

This project came out of a budgeting practice my family uses which has been useful for maintaining good spending habits but had become prohibitively time consuming. Building an API to improve automation and reduce the monthly time commitment devoted to budgeting will mean that we can continue with these good habits while reducing the cognitive load and time commitment it currently represents.

## Milestones

Two milestones are planned, dividing API from frontend. A discarded test spike has already settled the basic product requirements, which allows for full focus on creating a full featured API to support a polished frontend later on. 

**Milestone 1** is the deployed API with OpenAPI docs as the interface. This favors building a complete working backend over making an abbreviated but usable frontend + backend.

**Milestone 2** is a friendly frontend UI for household use on multiple device types. This milestone delivers a household-usable application. 

## Out of Scope

A number of features are currently out of scope of the core project. These fall into one of two categories: cut, or deferred.

### Cut

- Bank API Integrations
- Forecasting/Charts
- Receipts
- Multi-currency
- ML/fuzzy matching
- PDF statement parsing 

### Deferred

- Transaction splitting: This will allow cash withdrawals and multi-category transactions to have subtotals be attributed to differing categories. A withdrawal to buy fruit at the market and pay a babysitter could be categorized as Groceries (10€) and Childcare (30€).
- Hierarchical categories: Budgeting might be set at the Category level, or derived from the totals of constituent Subcategories. 
- Import profiles: The structure of transaction download can differ from one institution to another, and creating an import profile to select as those transactions are loaded will be a quality of life improvement. Not strictly necessary for the first Milestone as our household banks with a single institution.
- Rule derivation from corrections: When suggested categories are incorrect, deriving a rule from that correction will be a time saver, making it worth scheduling after Milestone 1 is complete.

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