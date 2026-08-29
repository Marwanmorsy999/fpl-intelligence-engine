# Stage 2 — Locked 2025-26 Holdout Gate

Status: **PASSED — FINAL HOLDOUT EVALUATED**.

This document records the final holdout evidence after Stage 2A/2B development validation. The 2025-26 season remains a locked evaluation set and was not used for model selection, tuning, feature selection, or calibration fitting.

## Season split

| Role | Seasons |
|---|---|
| Development / validation | 2022-23, 2023-24, 2024-25 |
| Locked final holdout | 2025-26 |

The holdout policy forbids 2025-26 from training, hyperparameter tuning, feature selection, calibration fitting, or model selection. Final evaluation is read-only. See `src/fpl_intelligence/config/holdout.py` and `docs/phase5-holdout-policy.md`.

## Frozen candidates

- Minutes: model version `2.0.0`, feature version `2.0.0`; the development result remained **KEEP AS CANDIDATE**.
- Team Strength: model version `2.0.0`, feature version `team-strength-2.0.0`; frozen method **EWMA**, window `5`, decay `0.9`.

These selections were frozen before the 2025-26 evaluation.

## Materialization

`scripts/backfill_holdout_2025_26.py` uses the existing real `RealFPLProvider` and canonical `import_season()` pipeline. It performs additive, idempotent ingestion and verifies the observation layer separately in read-only mode. The final successful run materialized and verified the canonical holdout source without changing model-selection inputs.

The holdout gate required:

- 2025-26 season exists;
- 380 scored fixtures;
- 760 team-match rows;
- 760/760 team-match rows with usable temporal provenance.

## Evaluation

GitHub Actions `Historical Model Validation #43` evaluated the frozen candidates on 2025-26. The evaluator trained Minutes only on development seasons and evaluated Team Strength chronologically using observations available before each fixture cutoff.

The evaluator was run twice and byte-compared with `cmp`; both outputs were identical. The workflow completed successfully after the corrected canonical fixture-ID mapping was deployed.

Artifact: `locked-2025-26-holdout`  
Artifact ID: `9717764102`  
SHA-256: `62c97c558a6e524a868bde6875e57e80a8c2c47473a9a0fe972e57fc85ea67b1`

## Final results

### Team Strength — EWMA

| Metric | Candidate | Baseline | Result |
|---|---:|---:|---|
| MAE | **0.9602** | 1.1483 | PASS |
| RMSE | **1.1980** | 1.4501 | PASS |
| Multiclass log loss | **1.0903** | 1.2684 | PASS |
| Home-win Brier | **0.2424** | 0.2830 | PASS |
| Clean-sheet Brier | **0.2155** | 0.2718 | PASS |

`promotion_gate_passed: true`.

### Minutes — model 2.0.0

| Metric | Candidate | Baseline | Result |
|---|---:|---:|---|
| MAE | 16.4963 | **14.8949** | FAIL |
| RMSE | **26.5042** | 28.7575 | PASS |
| Start Brier | **0.1057** | 0.1197 | PASS |
| Start log loss | **0.4652** | 1.4119 | PASS |

`promotion_gate_passed: false`.

## Promotion decision

**Team Strength: HOLDOUT-APPROVED — EWMA.**

The frozen EWMA candidate beat the rolling-goals baseline on every primary holdout comparison. No alternative method was selected after seeing 2025-26, and the holdout was not used to tune EWMA.

**Minutes: NOT PROMOTABLE.**

The candidate improved probabilistic start metrics but failed the required raw expected-minutes MAE comparison, so it remains a candidate for future development.

## Production activation note

The scientific promotion gate for Team Strength is passed. The repository's `model_registry` currently contains no registered model artifacts, and the current player baseline pipeline does not yet route fixture predictions through `TeamStrengthEngine`. Therefore this stage does **not** fabricate a registry entry or claim that the runtime has been activated with EWMA. Runtime activation must occur through the appropriate production integration when that pipeline is wired to the validated Team Strength engine.

The 2025-26 holdout remains read-only evidence and must not be used for subsequent tuning.
