# Stage 2B.1 Team Strength Historical Validation

Status: **VALIDATED FOR METHOD COMPARISON — CANDIDATE NOT PROMOTED**.

Run: GitHub Actions `Historical Model Validation #7`
Commit: `a543d1e72fb974dfc518d0e0a8e2c52619a3a6e0`
Validation date: 2026-08-29

## Scope

Validation uses only canonical historical seasons:

- 2022-23
- 2023-24
- 2024-25

The locked 2025-26 final holdout and 2026-27 current-season data are excluded.

The evaluator requires real `team_match_performances` rows and fails closed when the source is empty or temporally unusable. This prevents the previous zero-history fallback from being scored as a model comparison.

Historical source coverage used by Run #7:

- 2,280 team-match rows total
- 760 rows per historical season
- 2,280/2,280 rows have both `available_at` and `ingested_at`
- 2,130 rows have fixture-level team xG attributable without ambiguous double-gameweek aggregation

## Temporal protocol

For each fixture, the model cutoff is the fixture kickoff time.

A historical team-match row is eligible only when:

1. `event_time < cutoff`
2. `available_at <= cutoff`
3. `ingested_at <= cutoff`

The engine therefore cannot consume the fixture being predicted or any later event. Historical seasons are treated as one chronological stream, so earlier-season evidence may remain available when evaluating a later season. This is deliberate continuous-history behavior, not a random split.

## Method comparison

| Method | MAE | RMSE | Multiclass Log Loss | Home-win Brier |
|---|---:|---:|---:|---:|
| `ewma` | **1.10349** | **1.40350** | **1.06957** | **0.24228** |
| `poisson` | 1.23036 | 1.59920 | 1.19723 | 0.25953 |
| `rolling_xg` | 1.23178 | 1.59940 | 1.19683 | 0.25912 |
| `rolling_goals` | 1.32474 | 1.70571 | 1.27473 | 0.28338 |

EWMA is the best method on all four primary metrics in this development-season evaluation.

## Regression protection

`tests/unit/test_team_strength_engine.py` now verifies, on a controlled chronological fixture history, that:

- all four methods produce different strength signatures; and
- all four methods produce different fixture-probability signatures.

This specifically guards against the failure mode where multiple method names are routed through identical predictions.

## Reproducibility

Run #7 passed the complete validation workflow, including:

- Team Strength unit-test gate
- read-only canonical preflight
- Minutes walk-forward validation
- Minutes deterministic rerun comparison
- Team Strength deterministic rerun comparison
- validation artifact generation

The workflow completed successfully in 17m 10s.

## Promotion decision

**Team Strength: KEEP AS CANDIDATE (EWMA).**

The development-season comparison is scientifically usable and identifies EWMA as the current candidate. It is **not promoted to production** in this stage because the 2025-26 final holdout remains locked and has not been consumed for a post-freeze promotion evaluation.

**Minutes: KEEP AS CANDIDATE.**

The existing Minutes validation result does not satisfy its promotion gate because the candidate improves calibration/Brier performance but does not beat the required baseline on raw expected-minutes MAE.

No model was promoted and `main` was not modified.
