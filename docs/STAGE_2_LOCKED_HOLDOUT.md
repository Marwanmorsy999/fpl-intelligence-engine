# Stage 2 — Locked 2025-26 Holdout Gate

Status: **PREPARED — NOT YET EVALUATED**

This document defines the final holdout gate after Stage 2A/2B development validation. It must never be read as evidence that the holdout passed.

## Season split

| Role | Seasons |
|---|---|
| Development / validation | 2022-23, 2023-24, 2024-25 |
| Locked final holdout | 2025-26 |

The holdout policy forbids 2025-26 from training, hyperparameter tuning, feature selection, calibration fitting, or model selection. Final evaluation is read-only. See `src/fpl_intelligence/config/holdout.py` and `docs/phase5-holdout-policy.md`.

## Frozen candidates

- Minutes: model version `2.0.0`, feature version `2.0.0`; development result remains **KEEP AS CANDIDATE** because its required historical promotion gate is not met.
- Team Strength: model version `2.0.0`, feature version `team-strength-2.0.0`; selected development method is **EWMA**, window `5`, decay `0.9`.

No holdout observation is used to change these selections.

## Materialization

`scripts/backfill_holdout_2025_26.py` uses the existing real `RealFPLProvider` and canonical `import_season()` pipeline. It performs additive, idempotent ingestion and does not update or delete existing rows. Team Strength team-match rows are derived from the real FPL fixture and gameweek xG source, with double-gameweek xG left unset rather than copied across fixtures.

The canonical holdout gate requires:

- 2025-26 season exists;
- 380 fixtures exist;
- all 380 fixtures have genuine kickoff and final scores;
- 760 team-match rows exist;
- all 760 team-match rows have usable temporal provenance.

## Evaluation

`scripts/evaluate_locked_holdout.py` fits Minutes parameters using development seasons only and evaluates them on 2025-26. Team Strength evaluates the frozen EWMA configuration chronologically using only observations available before each fixture cutoff.

The evaluator produces a deterministic JSON artifact. GitHub Actions executes it twice and byte-compares the outputs.

## Current gate state

The holdout evaluator and CI workflow are prepared. The 2025-26 canonical dataset is not yet present in the production Supabase database, so no holdout metric or promotion claim is currently valid.

Promotion remains blocked until a successful holdout artifact is reviewed against the existing project promotion criteria.

Production `main` remains unchanged.
