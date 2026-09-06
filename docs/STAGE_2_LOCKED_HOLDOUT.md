# Stage 2 — Locked 2025-26 Holdout

Status document for the frozen Stage 2 promotion gate on seasons 2022-23 through 2024-25 development data with a locked 2025-26 holdout.

## Frozen candidates (pre-holdout)

- Minutes: model version `2.0.0`.
- Team Strength: model version `2.0.0`, feature version `team-strength-2.0.0`; frozen method **EWMA**, window `5`, decay `0.9`.

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

**Minutes: NOT PROMOTABLE.**

## Production activation note

**Runtime status: ACTIVATED (production).**

Holdout-approved Team Strength EWMA is wired into the live prediction chain:

* Module: `fpl_intelligence.prediction.team_strength_live`
* Integration: `get_prediction_provider` in `api/deps.py` wraps `resolve_chain` so EWMA fixture multipliers are applied after the quantitative chain resolves.
* Hyperparameters remain frozen: method `ewma`, window `5`, decay `0.9`, model version `2.0.0`, feature version `team-strength-2.0.0`.
* Effect: player xPTS are scaled by fixture-relative expected goals derived from EWMA team strengths, clamped to a conservative band (`0.78`–`1.28`) so the model modulates rather than replaces the chain.
* Provenance: `meta.chain.notes.team_strength` reports method, window, decay, teams adjusted, and holdout status.
* Registry: idempotent active entry for `team_strength` `2.0.0` with holdout metrics.
* Minutes remains **NOT PROMOTABLE** and is not activated.

If team-match history is missing, multipliers stay neutral and the note reports `status=unavailable` — the decisions endpoint does not fail closed.
