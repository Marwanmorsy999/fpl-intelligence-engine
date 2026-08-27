# FPL Intelligence Engine — 2026 Audit

Date: 2026-08-27

## Executive assessment

The repository has a strong engineering foundation but should be treated as **production infrastructure / application foundation**, not as a fully empirically validated elite prediction system yet.

The current architecture already separates raw data, normalized data, features, models, optimization and an AI analyst. The dashboard layer already exposes many views, so the next step should be consolidation and intelligence quality rather than adding random pages.

## Verified current strengths

- FastAPI API foundation.
- PostgreSQL + SQLAlchemy/Alembic.
- Official FPL provider abstraction and bootstrap/fixture ingestion.
- Time-aware feature registry with cutoff-time computation and caching.
- Prediction provider/model infrastructure.
- Squad synchronization.
- Price and transfer components.
- Multi-page dashboard surface: Dashboard, My Team, Track Record, Live, Sources, Connect, Assistant, League, Compare, Chips, Crunch, Targets, Planner and Transfers.
- AI analyst is architecturally separated from the quantitative source of truth.
- Environment configuration already has optional provider keys and graceful-degradation intent.
- Tests and deployment tooling are substantially developed.

Evidence: repository README and dashboard implementation.

## Highest-priority gaps

### P0 — Empirical model validation

The project status records that advanced player models and Monte Carlo/distribution work were implemented but not fully validated on real historical holdout data. The optimization holdout was also not yet a final frozen-model validation. This must be resolved before marketing model accuracy claims.

### P0 — Temporal integrity

The repository already supports cutoff-time feature computation, but the project status identifies historical `ingested_at`/availability-timestamp limitations. Historical availability/news snapshots must not use terminal season snapshots as if they were pre-deadline information.

### P0 — Availability/news data

The existing audit identifies a provider-key mismatch in the production availability import and terminal-season `players_raw.csv` snapshots that are not safe historical pre-deadline evidence. Fix the entity-resolution path, but do not call the terminal snapshot historical news evidence.

### P1 — Data provider architecture

`.env.example` already contains optional API-Football, football-data.org, The Odds API and multiple LLM keys, plus FPL proxy/egress controls. Build a formal provider registry with priority, quotas, freshness, reliability, cache TTL and legal/permission status rather than scattering provider logic.

### P1 — Prediction composition

The engine needs a clear central prediction contract: expected minutes first, then team strength, scoring components, FPL-rule transformation, probability distributions, uncertainty, calibration and an ensemble. Avoid independent/duplicated xP calculations across pages.

### P1 — Decision engine

Existing transfer/chip/planner functionality should be consolidated behind a single decision service that can compare Hold vs Transfer, hit vs no-hit, captain alternatives, chip alternatives and multi-week plans.

### P1 — UI consolidation

The project already has many pages. Avoid adding more top-level pages. Reorganize the existing surface around the user's decision flow: Today → My Team → Transfers → Captain → Planner → Live/League → Evidence/AI.

## Architecture direction

Use one canonical flow:

`providers -> raw -> normalized -> temporal feature store -> models -> calibrated ensemble -> decision engine -> evidence/provenance -> AI explanation -> UI`

The LLM should never be the numerical source of truth.

## Existing environment configuration observations

`.env.example` currently includes:

- `API_FOOTBALL_KEY`
- `FOOTBALL_DATA_ORG_KEY`
- `THE_ODDS_API_KEY`
- `GROQ_API_KEY`
- `GOOGLE_API_KEY`
- `OPENROUTER_API_KEY`
- `RSS_FEED_URL`
- `SENTRY_DSN`
- FPL proxy/egress configuration

The next implementation should formalize these as provider capabilities rather than making any one optional provider a hard dependency.

## Audit conclusion

Do not rewrite the project. Preserve the existing contracts where sound. Refactor around canonical provider, feature, prediction and decision contracts; then add missing data and validation. The next meaningful milestone is **trusted intelligence**, not more UI volume.
