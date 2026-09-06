# FPL Intelligence Engine

A data-driven Fantasy Premier League backend for ingestion, time-aware features, prediction, simulation, decision optimization, and analyst outputs.

## Repository status

**Last verified:** 2026-09-06

The repository is actively maintained as a production-oriented Python 3.12/FastAPI service. The codebase uses PostgreSQL with SQLAlchemy/Alembic and keeps FPL rules in versioned configuration rather than hard-coding season mechanics across the application.

## Architecture

```text
raw data -> normalized data -> time-aware features -> models -> predictions
         -> simulation -> decision optimization -> analyst/API outputs
```

Historical decisions must preserve provenance and timing so backtests cannot use information that was unavailable at the decision deadline.

## Current 2026/27 rules

The authoritative season configuration is:

`config/fpl_rules/2026-27.yaml`

It contains the 2026/27 scoring constants, defensive-contribution thresholds, chip availability, free-transfer rollover limit, and later Gameweek lockdown policy. These values are based on the official Premier League FPL documentation published for the 2026/27 season.

## Local development

```bash
python -m pip install -e '.[dev]'
cp .env.example .env

docker compose up -d db
alembic upgrade head

python -m pytest -q
python -m ruff check src tests scripts
```

Start the API with:

```bash
uvicorn fpl_intelligence.api.main:app --reload
```

The package also exposes the `fpl` CLI entry point.

## CI

`.github/workflows/ci.yml` runs for pushes and pull requests targeting `master` or `main`. It performs source compilation, Ruff linting, the full pytest suite, Chromium installation for web tests, and an API import smoke test.

## Branch policy

- `main` is the production branch.
- `master` is the integration branch.
- Feature/fix work should use an isolated branch and merge through a reviewed pull request.
- Do not force-reset `main` or rewrite shared history as part of routine cleanup.

## Configuration and secrets

Runtime secrets belong in environment variables or deployment configuration. Do not commit `.env` files, API tokens, database dumps, generated proof artifacts, or local runtime state.

Generated QA/evidence directories and temporary files are intentionally ignored by `.gitignore`; durable project decisions belong in source, tests, issues, or focused documentation instead.

## Validation notes

The repository contains historical validation work, but validation status is not inferred from old proof files. CI and the current code/tests are the operational source of truth; time-sensitive FPL rules remain versioned under `config/fpl_rules/`.
