# Stage 2A Minutes Validation

## Executive result
Candidate rows: N = 76792. Promotion decision: **KEEP AS CANDIDATE**.
The candidate is promoted only when it wins both expected-minutes MAE and start Brier score against every required baseline; this report does not tune the model.

## Data
Dataset: `canonical_historical_performance`. Seasons requested: 2022-23, 2023-24, 2024-25.
Model version: `2.0.0`. Feature version: `2.0.0`. Policy: `strict_reproducibility`.
Only `PlayerGameweekPerformance` rows passing both `available_at <= cutoff` and `ingested_at <= cutoff` were used as features. No FPL snapshots, season totals, future ownership, prices, xP, transfers, availability, lineups, or post-match data were substituted.
Total evaluated candidate rows: N = 76792. Baseline rows: N = 76792 for each required baseline. Fold prediction total: N = 76792. Excluded rows: 1154 ({'no_temporal_provenance': 0, 'insufficient_training_rows': 1154}).

## Temporal policy
Each fold trains on all prior chronological folds and predicts the next gameweek. The cutoff is one hour before the canonical gameweek deadline. No random split is used.
Evaluation folds: 107. First folds reserved for initial training: 3.

## Metrics
All metric rows use the evaluated candidate denominator N = 76792. Baselines are scored on the same evaluated rows; no baseline-only rows are included.
| Model | N | MAE | RMSE | Start Brier | Start Log loss | Start ECE | Appearance Brier | Appearance Log loss | 60+ Brier | 60+ Log loss |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| recent_minutes | 76792 | 16.0302 | 30.2698 | 0.1231 | 1.2411 | 0.0759 | 0.1848 | 5.1076 | 0.1231 | 1.2411 |
| recent_start | 76792 | 16.2700 | 35.1800 | 0.1628 | 4.4988 | 0.1628 | 0.2176 | 6.0136 | 0.1628 | 4.4988 |
| rolling_average | 76792 | 23.8000 | 40.7039 | 0.2023 | 0.8002 | 0.1892 | 0.2664 | 7.3597 | 0.2023 | 0.8002 |
| candidate | 76792 | 18.0451 | 28.2397 | 0.1104 | 0.4097 | 0.0113 | 0.1275 | 0.4766 | 0.1104 | 0.4097 |

## Expected-Minutes Ensemble

The ensemble is implemented as:

`E_blend = w * E_model + (1 - w) * E_recent`

For each outer fold, the available historical training window is split
chronologically. A model is fitted on the inner training prefix, candidate
weights `0.0, 0.1, ..., 1.0` are scored by inner-validation MAE, and the
lowest-MAE weight is frozen before scoring the outer unseen fold. Ties are
resolved deterministically in favor of the lower weight. Outer-fold actual
minutes cannot influence selection. The current implementation uses a global
weight by default; conditional position weights are only retained when they
have at least 20 inner rows and improve against both the global weight for that
group and the aggregate global result.

| Estimator | MAE | RMSE | Status |
|---|---:|---:|---|
| `E_model` / candidate | 18.0451 | 28.2397 | Existing outer result |
| `E_recent` / recent-minutes | 16.0302 | 30.2698 | Existing outer result |
| `E_blend` | pending | pending | Requires canonical outer rerun |

The blend exposes `expected_minutes_method = "walkforward_blend"` and records
the selected weight, model version, feature version, inner training window, and
inner validation window. Probability outputs are copied from the candidate
unchanged. Season, position, and minutes-tier blend breakdowns, including
tiers `0`, `1-29`, `30-59`, `60-89`, and `90+`, remain pending until the
canonical validation command is run with `DATABASE_URL` configured. No locked
2025-26 holdout or 2026-27 current-season data was used.

## Calibration
### start
| Probability bucket | Predicted | Observed | N |
|---|---:|---:|---:|
| 0.0-0.1 | 0.0270 | 0.0367 | 38372 |
| 0.1-0.2 | 0.1463 | 0.1625 | 6137 |
| 0.2-0.3 | 0.2421 | 0.2406 | 4842 |
| 0.3-0.4 | 0.3459 | 0.3640 | 3225 |
| 0.4-0.5 | 0.4481 | 0.4589 | 3127 |
| 0.5-0.6 | 0.5490 | 0.5524 | 3447 |
| 0.6-0.7 | 0.6503 | 0.6598 | 3959 |
| 0.7-0.8 | 0.7491 | 0.7576 | 6754 |
| 0.8-0.9 | 0.8489 | 0.8331 | 5626 |
| 0.9-1.0 | 0.9492 | 0.8726 | 1303 |
### appearance
| Probability bucket | Predicted | Observed | N |
|---|---:|---:|---:|
| 0.0-0.1 | 0.0315 | 0.0540 | 26669 |
| 0.1-0.2 | 0.1427 | 0.1506 | 6516 |
| 0.2-0.3 | 0.2406 | 0.2532 | 4250 |
| 0.3-0.4 | 0.3427 | 0.3495 | 4149 |
| 0.4-0.5 | 0.4469 | 0.4552 | 3058 |
| 0.5-0.6 | 0.5510 | 0.5528 | 3752 |
| 0.6-0.7 | 0.6479 | 0.6413 | 4499 |
| 0.7-0.8 | 0.7511 | 0.7670 | 5768 |
| 0.8-0.9 | 0.8547 | 0.8541 | 13737 |
| 0.9-1.0 | 0.9319 | 0.8987 | 4394 |
### 60_plus
| Probability bucket | Predicted | Observed | N |
|---|---:|---:|---:|
| 0.0-0.1 | 0.0270 | 0.0367 | 38372 |
| 0.1-0.2 | 0.1463 | 0.1625 | 6137 |
| 0.2-0.3 | 0.2421 | 0.2406 | 4842 |
| 0.3-0.4 | 0.3459 | 0.3640 | 3225 |
| 0.4-0.5 | 0.4481 | 0.4589 | 3127 |
| 0.5-0.6 | 0.5490 | 0.5524 | 3447 |
| 0.6-0.7 | 0.6503 | 0.6598 | 3959 |
| 0.7-0.8 | 0.7491 | 0.7576 | 6754 |
| 0.8-0.9 | 0.8489 | 0.8331 | 5626 |
| 0.9-1.0 | 0.9492 | 0.8726 | 1303 |

## Season breakdown
| Group | Model | N | MAE | Start Brier | Appearance Brier | 60+ Brier |
|---|---|---:|---:|---:|---:|---:|
| 2022-23 | recent_minutes | 22258 | 16.6726 | 0.1219 | 0.1763 | 0.1219 |
| 2022-23 | recent_start | 22258 | 16.8684 | 0.1625 | 0.2161 | 0.1625 |
| 2022-23 | rolling_average | 22258 | 20.7692 | 0.1434 | 0.2314 | 0.1434 |
| 2022-23 | candidate | 22258 | 18.3211 | 0.1115 | 0.1224 | 0.1115 |
| 2023-24 | recent_minutes | 27919 | 15.6570 | 0.1208 | 0.1872 | 0.1208 |
| 2023-24 | recent_start | 27919 | 15.7745 | 0.1588 | 0.2113 | 0.1588 |
| 2023-24 | rolling_average | 27919 | 24.0629 | 0.2079 | 0.2794 | 0.2079 |
| 2023-24 | candidate | 27919 | 17.7035 | 0.1063 | 0.1265 | 0.1063 |
| 2024-25 | recent_minutes | 26615 | 15.8845 | 0.1265 | 0.1895 | 0.1265 |
| 2024-25 | recent_start | 26615 | 16.2893 | 0.1672 | 0.2256 | 0.1672 |
| 2024-25 | rolling_average | 26615 | 26.0590 | 0.2457 | 0.2819 | 0.2457 |
| 2024-25 | candidate | 26615 | 18.1726 | 0.1136 | 0.1329 | 0.1136 |

## Position breakdown
| Group | Model | N | MAE | Start Brier | Appearance Brier | 60+ Brier |
|---|---|---:|---:|---:|---:|---:|
| DEF | recent_minutes | 26204 | 17.3320 | 0.1329 | 0.1986 | 0.1329 |
| DEF | recent_start | 26204 | 17.3012 | 0.1746 | 0.2216 | 0.1746 |
| DEF | rolling_average | 26204 | 25.9171 | 0.2189 | 0.2790 | 0.2189 |
| DEF | candidate | 26204 | 19.2805 | 0.1185 | 0.1347 | 0.1185 |
| FWD | recent_minutes | 9256 | 15.4309 | 0.1184 | 0.1764 | 0.1184 |
| FWD | recent_start | 9256 | 16.0601 | 0.1569 | 0.2350 | 0.1569 |
| FWD | rolling_average | 9256 | 22.4352 | 0.1930 | 0.2542 | 0.1930 |
| FWD | candidate | 9256 | 17.3480 | 0.1058 | 0.1249 | 0.1058 |
| GK | recent_minutes | 8149 | 13.1979 | 0.0995 | 0.1612 | 0.0995 |
| GK | recent_start | 8149 | 13.0482 | 0.1297 | 0.1762 | 0.1297 |
| GK | rolling_average | 8149 | 20.3381 | 0.1707 | 0.2368 | 0.1707 |
| GK | candidate | 8149 | 15.6966 | 0.0889 | 0.1130 | 0.0889 |
| MID | recent_minutes | 33183 | 15.8650 | 0.1224 | 0.1821 | 0.1224 |
| MID | recent_start | 33183 | 16.3054 | 0.1633 | 0.2198 | 0.1633 |
| MID | rolling_average | 33183 | 23.3592 | 0.1996 | 0.2670 | 0.1996 |
| MID | candidate | 33183 | 17.8406 | 0.1105 | 0.1262 | 0.1105 |

## Minutes-tier breakdown
| Group | Model | N | MAE | Start Brier | Appearance Brier | 60+ Brier |
|---|---|---:|---:|---:|---:|---:|
| 0 | recent_minutes | 45388 | 8.9569 | 0.0659 | 0.2494 | 0.0659 |
| 0 | recent_start | 45388 | 6.9957 | 0.0777 | 0.0777 | 0.0777 |
| 0 | rolling_average | 45388 | 3.4934 | 0.0115 | 0.4255 | 0.0115 |
| 0 | candidate | 45388 | 11.3088 | 0.0459 | 0.1049 | 0.0459 |
| 1-29 | recent_minutes | 6951 | 22.6830 | 0.1883 | 0.1936 | 0.1883 |
| 1-29 | recent_start | 6951 | 26.8648 | 0.2292 | 0.7708 | 0.2292 |
| 1-29 | rolling_average | 6951 | 11.6023 | 0.0317 | 0.0803 | 0.0317 |
| 1-29 | candidate | 6951 | 20.1523 | 0.1369 | 0.3140 | 0.1369 |
| 30-59 | recent_minutes | 2815 | 28.1483 | 0.3461 | 0.1176 | 0.3461 |
| 30-59 | recent_start | 2815 | 43.9968 | 0.4210 | 0.5790 | 0.4210 |
| 30-59 | rolling_average | 2815 | 34.5201 | 0.0572 | 0.0469 | 0.0572 |
| 30-59 | candidate | 2815 | 20.6600 | 0.2469 | 0.2098 | 0.2469 |
| 60-89 | recent_minutes | 6412 | 28.9296 | 0.2759 | 0.0678 | 0.2759 |
| 60-89 | recent_start | 6412 | 38.9471 | 0.4153 | 0.4153 | 0.4153 |
| 60-89 | rolling_average | 6412 | 59.3432 | 0.7212 | 0.0232 | 0.7212 |
| 60-89 | candidate | 6412 | 26.5489 | 0.2978 | 0.1347 | 0.2978 |
| 90+ | recent_minutes | 15226 | 26.4058 | 0.1583 | 0.0501 | 0.1583 |
| 90+ | recent_start | 15226 | 24.4035 | 0.2321 | 0.2321 | 0.2321 |
| 90+ | rolling_average | 15226 | 72.9519 | 0.6573 | 0.0200 | 0.6573 |
| 90+ | candidate | 15226 | 33.0991 | 0.1863 | 0.0918 | 0.1863 |

## Failure modes
Inspect the highest-error breakdowns above; small groups must not be treated as stable evidence.

## Statistical limitations
Metrics are descriptive and have no confidence intervals. Fold-level dependence and player-level repeated observations limit independence. Empty or small groups are reported with N and are not used for promotion claims.

## Promotion decision
KEEP AS CANDIDATE

## Reproduction
Run `python scripts/evaluate_minutes_walkforward.py --report docs/STAGE_2A_MINUTES_VALIDATION.md` against the configured canonical database.
