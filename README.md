# FPL Intelligence Engine

A free-first, data-driven Fantasy Premier League intelligence platform designed to combine FPL data, football performance data, public news, availability information, statistical models, simulation, optimization, and an AI analyst layer.

## Branch Strategy

- **`main`** — Production branch (deploys to production)
- **`master`** — Preview branch (preview deployments)
- Feature branches follow pattern: `feat/`, `fix/`, `perf/`, `chore/`

## Architecture

The LLM is not the source of truth. The platform is organized as:

```
raw data -> normalized data -> features -> models -> predictions -> optimization -> AI analyst -> outputs
```

Every data point that can affect historical decisions must preserve provenance and timing so future backtesting can enforce strict no-look-ahead rules.

## Key Modules

| Module | Purpose |
|--------|---------|
| `prediction/` | Minutes prediction, team strength, xG models |
| `optimization/` | Decision optimization (XI, captain, transfers, chips) |
| `backtesting/` | Decision quality metrics, chip scenarios, PIT shadow mode |
| `sync/` | Historical data ingestion, prediction ledger |
| `data_providers/` | FPL API, Understat, API-Football, egress chain |
| `availability/` | Player availability and PIT compliance |
| `squad/` | Squad management, bridge to optimization |
| `cache/` | Shared caching with Upstash Redis backend |

## Local Setup

1. Copy `.env.example` to `.env` and configure
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

## Testing

```bash
# Run all unit tests
python -m pytest tests/unit -q

# Run with coverage
python -m pytest tests/unit --cov=fpl_intelligence

# Run specific test file
python -m pytest tests/unit/test_phase19_sync.py -v
```

## Deployment

- **Vercel** — Preview and production deployments via `vercel.json`
- **Database migrations** — Managed via Alembic, applied automatically on deploy when migration inputs change
- **Daily jobs** — Scheduled via Vercel Cron at `10 6 * * *` UTC

## Important

The official FPL rules are season-dependent. The rules configuration in `config/fpl_rules/` is deliberately versioned so scoring and mechanics can change without rewriting the entire platform.

The 2026/27 implementation must be validated against official Premier League/FPL rules before any production scoring model is built.
