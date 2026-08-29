# Stage 2A Data Blocker

Status: blocked in the current operator environment (2026-08-28).

The strict minutes validation run is blocked because `DATABASE_URL` is not
configured in the validation environment. The preflight and runner fail closed
with:

`DATABASE_URL is not configured for this validation run.`

The preflight result in this environment is:

```text
source: unavailable
availability: unavailable (DATABASE_URL is not configured for this validation run.)
```

## Configuration path

The validation scripts use the existing `Settings` configuration and its
`.env` support, or the process environment variable `DATABASE_URL`. The
validation session accepts only an explicitly configured PostgreSQL URL and
does not use the local development fallback.

The application settings default to the development placeholder
`postgresql+psycopg://fpl:fpl@localhost:5432/fpl`. In a non-production app
context, `src/fpl_intelligence/db/session.py` converts that placeholder to the
local SQLite development fallback. Validation deliberately bypasses that
conversion: it rejects the placeholder and SQLite, so a missing remote URL
cannot silently become a localhost or local-file validation run.

## Deployment source

Production Vercel receives `DATABASE_URL` from its deployment environment.
GitHub Actions receives the same variable from the repository Actions secret
named `DATABASE_URL`, as used by the existing sync workflow. The value is not
stored in source control or this document. The canonical source is the
PostgreSQL `player_gameweek_performances` table, joined to `gameweeks` and
`seasons` by the existing `TrainingDataBuilder` and strict walk-forward
evaluator.

## Local operator action

Run the preflight and evaluator from a shell where the existing remote
Supabase PostgreSQL connection is supplied as `DATABASE_URL`:

```text
python scripts/preflight_minutes_validation.py
python scripts/evaluate_minutes_walkforward.py --report docs/STAGE_2A_MINUTES_VALIDATION.md
```

The preflight performs `SELECT`-only queries and must report, for each required
season, player count, fixture count, Gameweek count, and historical performance
row count. It also reports temporal timestamp coverage, player/team/fixture
mapping failures, duplicate rows, missing critical values, and invalid
timestamps before the evaluator is run. Do not run migrations as part of this
validation.

## Canonical historical data

Remote canonical row counts and season coverage could not be verified from
this local environment because the remote connection variable was absent.
Therefore the presence of `2022-23`, `2023-24`, and `2024-25` in the remote
database is **unverified**, not asserted either way. The required source
remains the existing `player_gameweek_performances` table, joined to
`gameweeks` and `seasons`; no substitute or fabricated data is permitted.

## Required operator action

From a shell with the existing remote canonical PostgreSQL URL configured by
the deployment operator, run:

```text
python scripts/preflight_minutes_validation.py
python scripts/evaluate_minutes_walkforward.py --report docs/STAGE_2A_MINUTES_VALIDATION.md
```

Proceed to evaluation only when preflight reports all three seasons, usable
temporal provenance, and no schema or entity-resolution blocker. The commands
perform no writes, migrations, deployment, or production job triggers.

Code changes made for this stage: the validation URL boundary was already
present; the preflight was expanded to cover the required checks and the
blocker is documented here. No model, statistical method, production
configuration, or deployment behavior was changed.