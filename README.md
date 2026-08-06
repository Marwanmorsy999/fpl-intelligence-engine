# FPL Intelligence Engine

A free-first, data-driven Fantasy Premier League intelligence platform designed to combine FPL data, football performance data, public news, availability information, statistical models, simulation, optimization, and an AI analyst layer.

## Current milestone

Foundation scaffold:

- Python 3.12 project structure
- Docker Compose with PostgreSQL 16
- FastAPI health endpoint
- SQLAlchemy/Alembic database foundation
- Season-versioned FPL rules configuration
- Official FPL provider adapter abstraction
- Idempotent ingestion run tracking
- Bootstrap/static-data ingestion
- Fixture ingestion
- CLI commands
- Initial unit tests

## Architecture principle

The LLM is not the source of truth. The platform is organized as:

`raw data -> normalized data -> features -> models -> predictions -> optimization -> AI analyst -> outputs`

Every data point that can affect historical decisions must preserve provenance and timing so future backtesting can enforce strict no-look-ahead rules.

## Local setup

1. Copy `.env.example` to `.env`.
2. Start PostgreSQL:

```bash
docker compose up -d db
```

3. Install the project:

```bash
python -m pip install -e '.[dev]'
```

4. Create/update schema:

```bash
alembic upgrade head
```

5. Start API:

```bash
uvicorn fpl_intelligence.api.main:app --reload
```

6. Run ingestion:

```bash
python -m fpl_intelligence.cli fpl-bootstrap
python -m fpl_intelligence.cli fpl-fixtures
python -m fpl_intelligence.cli fpl-all
```

## Important

The official FPL rules are season-dependent. The rules configuration in `config/fpl_rules/` is deliberately versioned so scoring and mechanics can change without rewriting the entire platform.

The 2026/27 implementation must be validated against official Premier League/FPL rules before any production scoring model is built.
