# Stage 2A Data Blocker

Status: **historical database is present; validation preflight corrected 2026-08-29**.

## What the latest Actions run proved

GitHub Actions successfully connected to the configured canonical PostgreSQL database and reported the required historical seasons. The previous blocker text saying `DATABASE_URL` was missing is stale and no longer describes the operator environment.

Observed historical coverage from the run:

- `2022-23`: 778 players, 380 fixtures, 37 gameweeks, 24,957 canonical player-GW rows
- `2023-24`: 865 players, 380 fixtures, 38 gameweeks, 28,742 canonical player-GW rows
- `2024-25`: 804 players, 380 fixtures, 38 gameweeks, 27,231 canonical player-GW rows

The database therefore contains the expected canonical historical source for Stage 2A.

## Temporal provenance

The historical backfill intentionally stores real gameweek-end `available_at` and `ingested_at` timestamps and leaves `Gameweek.deadline_time` NULL because the historical mirror does not publish genuine FPL deadlines. The minutes validator already implements the documented conservative fallback: the decision boundary is derived from the latest genuine kickoff of the previous gameweek rather than inventing a deadline.

The current-season 2026-27 data contains the pre-existing 15,899-row `available_at > ingested_at` condition. That live-path condition is outside Stage 2A historical validation and must not be treated as a defect in the imported 2022-23 through 2024-25 validation dataset.

The preflight has therefore been corrected to:

- scope timestamp, entity-resolution, duplicate, and critical-value checks to the required historical seasons;
- allow the documented NULL historical gameweek deadlines and report that the conservative ordering fallback will be used;
- continue to inspect only the canonical PostgreSQL data with SELECT-only queries.

## Workflow behavior

The validation workflow now fails immediately when the historical preflight reports a genuine blocker. It no longer continues into a 45-minute minutes-validation job after a failed preflight. The workflow timeout was also increased to 90 minutes to avoid discarding a valid run solely because the chronological model evaluation is expensive.

The minutes evaluator uses the optimized cached implementation on the validation branch while preserving the existing model, feature, cutoff, and scoring semantics.

## Required next step

Run `Historical Model Validation` on `feature/intelligence-validation` with `validation_target=minutes` first. A successful preflight should complete quickly and then the optimized minutes walk-forward evaluation should run. Only after the minutes run completes should the full `all` target be used.

No production database writes, migrations, or holdout-season data changes are part of this validation workflow.
