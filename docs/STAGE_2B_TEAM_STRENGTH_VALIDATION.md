# Stage 2B.1 Team Strength Historical Validation

Status: **HOLDOUT-APPROVED — EWMA**.

Development validation and the locked 2025-26 final holdout have both been completed for the frozen Team Strength candidate.

## Scope

Development validation uses only canonical historical seasons:

- 2022-23
- 2023-24
- 2024-25

The final holdout is 2025-26. The 2026-27 current-season data is excluded from validation.

The evaluator requires real `team_match_performances` rows and fails closed when the source is empty or temporally unusable. This prevents the previous zero-history fallback from being scored as a model comparison.

Historical development source coverage:

- 2,280 team-match rows total
- 760 rows per historical season
- 2,280/2,280 rows have usable temporal provenance
- 2,130 rows have fixture-level team xG attributable without ambiguous double-gameweek aggregation

## Temporal protocol

For each fixture, the model cutoff is the fixture kickoff time.

A historical team-match row is eligible only when:

1. `event_time < cutoff`
2. `available_at <= cutoff`
3. `ingested_at <= cutoff`

The engine therefore cannot consume the fixture being predicted or any later event. Historical seasons are treated as one chronological stream, so earlier-season evidence may remain available when evaluating a later season.

## Development method comparison

| Method | MAE | RMSE | Multiclass Log Loss | Home-win Brier |
|---|---:|---:|---:|---:|
| `ewma` | **1.10349** | **1.40350** | **1.06957** | **0.24228** |
| `poisson` | 1.23036 | 1.59920 | 1.19723 | 0.25953 |
| `rolling_xg` | 1.23178 | 1.59940 | 1.19683 | 0.25912 |
| `rolling_goals` | 1.32474 | 1.70571 | 1.27473 | 0.28338 |

EWMA was best on all four primary development metrics and was frozen as the candidate before holdout evaluation.

## Locked 2025-26 holdout

GitHub Actions `Historical Model Validation #43` evaluated the frozen configuration:

- model version `2.0.0`
- feature version `team-strength-2.0.0`
- method `ewma`
- window `5`
- decay `0.9`

The holdout contained 380 fixtures. The evaluator was run twice and the outputs were byte-identical.

| Metric | EWMA candidate | Rolling-goals baseline | Result |
|---|---:|---:|---|
| MAE | **0.9602** | 1.1483 | PASS |
| RMSE | **1.1980** | 1.4501 | PASS |
| Multiclass log loss | **1.0903** | 1.2684 | PASS |
| Home-win Brier | **0.2424** | 0.2830 | PASS |
| Clean-sheet Brier | **0.2155** | 0.2718 | PASS |

`promotion_gate_passed: true`.

Artifact: `locked-2025-26-holdout` (ID `9717764102`)  
SHA-256: `62c97c558a6e524a868bde6875e57e80a8c2c47473a9a0fe972e57fc85ea67b1`

## Regression protection

`tests/unit/test_team_strength_engine.py` verifies, on controlled chronological history, that all four methods produce distinct strength signatures and distinct fixture-probability signatures. The holdout importer also has a regression test preventing double-hashing of canonical fixture identifiers.

## Promotion decision

**Team Strength: HOLDOUT-APPROVED — EWMA.**

The candidate passed every primary holdout comparison without selecting or tuning against 2025-26.

**Minutes: NOT PROMOTABLE.**

The Minutes 2.0 candidate remains unpromoted because its 2025-26 raw expected-minutes MAE is worse than the required baseline, despite better Start Brier and Start log loss.

## Production activation boundary

This document records the scientific/model-selection approval only. The production `model_registry` currently has no registered entries, and the current player baseline pipeline does not route fixture predictions through `TeamStrengthEngine`. No synthetic registry row or false runtime-activation claim is made here. Runtime activation belongs to the production integration step when the validated Team Strength engine is wired into the prediction path.
