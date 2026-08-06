# Phase 4.75 — Final Report

_Generated 2026-08-03T12:47:14.280810+00:00_

## Real data sources

- https://github.com/vaastav/Fantasy-Premier-League
- vaastav/Fantasy-Premier-League public GitHub mirror (teams, fixtures,
  players_raw, per-gameweek gw*.csv with xG/xA, price, ownership-count, transfers).

## Seasons imported

['2022-23', '2023-24', '2024-25', '2025-26']

## Dataset coverage

- 2022-23: 83.3% (FPL=available, xG=available, ownership=available)
- 2023-24: 83.3% (FPL=available, xG=available, ownership=available)
- 2024-25: 83.3% (FPL=available, xG=available, ownership=available)
- 2025-26: 83.3% (FPL=available, xG=available, ownership=available)

## Temporal integrity

- teams: DatasetClass.STRICT_BACKTEST_SAFE — Reference data (teams/fixtures) with stable identifiers....
- players: DatasetClass.STRICT_BACKTEST_SAFE — Reference data (teams/fixtures) with stable identifiers....
- fixtures: DatasetClass.STRICT_BACKTEST_SAFE — Reference data (teams/fixtures) with stable identifiers....
- fpl_history: DatasetClass.STRICT_BACKTEST_SAFE — Finalized gameweek outcomes are published right after each gameweek; legitimatel...
- fpl_snapshots: DatasetClass.HISTORICAL_OUTCOME_ONLY — Mirror snapshots are gameweek-end values; no recorded pre-deadline availability....

## Phase 4.5 revalidation (real)

- baseline_a: MAE=1.1151, Spearman=0.6931
- baseline_b: MAE=1.1038, Spearman=0.6771
- baseline_c: MAE=1.0192, Spearman=0.698

## Predictive edge classification

**B**

## Leakage audit

- synthetic_contamination: PASS
- fixture_ordering_2022-23: ok
- fixture_ordering_2023-24: ok
- fixture_ordering_2024-25: ok
- fixture_ordering_2025-26: ok

## Synthetic contamination

PASS

## GO / CONDITIONAL GO / NO-GO

**CONDITIONAL_GO**

## Recommendation

Preliminary real predictive signal detected (B). More validation required.
Consider a restricted-feature CONDITIONAL GO before Phase 5.