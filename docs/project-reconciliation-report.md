# Project Reconciliation Report

**Date:** 2026-08-05
**Auditor:** Reconciliation Agent (post-interruption)
**Method:** Direct code/test/DB inspection — previous agent reports treated as claims, not facts.

> **Note:** This repository has **no git history** (`.git` absent). All evidence is
> derived from the working tree, test execution, and real-data pipeline runs.

---

## Phase-by-Phase Status

| Phase | Status | Evidence | Missing | Action |
| ----- | ------ | -------- | ------- | ------ |
| 1 — Foundation | COMPLETE | `db/base.py`, `db/models.py`, `db/session.py`, SQLAlchemy 2.0 Mapped types, Alembic migrations, 287 tests pass | — | None |
| 2 — Historical + Canonical Data | COMPLETE | `providers/` (RealFPLProvider, DiskCachingFetcher), `ingestion/historical.py`, canonical models, entity resolution, provenance manifests, 4 seasons of real raw data cached | — | None |
| 3 — Time-Aware Feature Store | PARTIALLY_COMPLETE | WalkForwardValidator, TemporalQueryBuilder with `as_of()`/`apply_policy()`, temporal fields on FPLSnapshot. **Limitation:** Gameweek-ordering enforcement only; `ingested_at` not historically accurate | DB-level timestamp reproducibility not fully validated | Document limitation |
| 4 — Quantitative Prediction Engine | COMPLETE | MinutesModel, TeamStrengthModel, ModelRegistry, WalkForwardTrainer, PredictionModel ABC | — | None |
| 4.5 — Quantitative Edge Validation | VALIDATED | `docs/phase4-5-quantitative-edge-report.md`, 2520 featured player-gameweeks | — | None |

---

## Part 2 — Conflicting Phase Reports

### Phase 5
**Previous claim:** "COMPLETE" (`docs/phase5-final-report.md`).
**Actual status:** `IMPLEMENTED_NOT_VALIDATED`.

All engineering components exist with tests (goal, assist, clean-sheet, bonus, defensive models, AdvancedPlayerModel, DistributionEngine, GameweekSimulator, JointSimulator, holdout policy with 17 tests). However, the **final 2025-26 locked holdout was NOT run after model freeze.** No post-freeze holdout evaluation evidence exists. Distribution calibration and Monte Carlo convergence were tested with unit tests only, not on real data.

**ENGINEERING COMPLETE ≠ EMPIRICALLY VALIDATED.**

### Phase 6
**Previous claim:** "COMPLETE" (`docs/phase6-completion-report.md`).
**Actual status:** `COMPLETE` (after reconciliation fixes).

All components functional. Fixed during reconciliation:
1. **Bench Boost** had `expected_score_with_chip=0.0` (hardcoded zero) — fixed to compute XI + bench EV from provider.

## Part 3 — Real Data Foundation

| Dataset | Raw Data | Imported | Synthetic? |
| ------- | -------- | -------- | ---------- |
| 2022-23 | ✅ `data/raw/real_fpl/2022-23/` | ✅ | No |
| 2023-24 | ✅ `data/raw/real_fpl/2023-24/` | ✅ | No |
| 2024-25 | ✅ `data/raw/real_fpl/2024-25/` | ✅ | No |
| 2025-26 | ✅ `data/raw/real_fpl/2025-26/` | ✅ | No |

**Synthetic contamination check:** `detect_contamination()` in `validation/real_data.py` — checks mock-provider players don't appear in real seasons. Result: **PASS**. The check is real and functional.

**2022-23 anomaly:** Backtest produced only 164 pts (vs 1696–2150 for other seasons). Data-format/player-mapping issue in oldest cached dataset. Pipeline ran correctly; data quality is the issue.

## Part 4 — Temporal Integrity

**Temporal fields present:** FPLSnapshot (`event_time`, `published_at`, `ingested_at`, `available_at`), PlayerTeamMembership (`valid_from`, `valid_to`), Gameweek (`deadline_time`, `start_time`, `end_time`), Fixture (`kickoff_time`).

**Enforcement:** TemporalQueryBuilder with `as_of()`, `apply_policy()` (falls back through `available_at` → `published_at` → `event_time`), `is_record_available()`.

**Known limitation (documented, not hidden):** System enforces no-look-ahead primarily via **Gameweek ordering** (walk-forward), not strict timestamp-based information-availability. TODO.md: "no-look-ahead audit passes by Gameweek-ordering; DB-level STRICT_REPRODUCIBILITY not exercisable on mock data." `ingested_at` set to `datetime.now()` not historical time.

**Leakage tests:** `tests/unit/test_temporal_integrity.py` — all pass.

## Part 5 — Prediction Quality Infrastructure

| Component | Type | Notes |
| --------- | ---- | ----- |

## Part 6 — Decision Optimization Path Audit

**Pipeline:** PREDICTION → DISTRIBUTION → SIMULATION → DECISION → OPTIMIZATION → RECOMMENDATION

**Issues found and fixed:**
1. **DecisionBacktester** — returned hardcoded constants. **Fixed:** Real walk-forward backtester on populated DB; raises `RuntimeError` without DB.
2. **Bench Boost** — `expected_score_with_chip=0.0`. **Fixed:** XI EV + bench EV from provider.
3. **Free Hit** — hardcoded `65.0`. **Fixed:** Top-11 EV from provider pool.
4. **Wildcard** — hardcoded `55.0/GW`. **Fixed:** Top-11 EV from provider pool over horizon.
5. **Schema mismatch** — queried non-existent `Gameweek.number` and `PlayerTeamMembership.position_code`. **Fixed:** Uses `Gameweek.provider_event_id` and `Player.position_code`.

**Remaining:** `simulate_decision` correctly uses numpy distributions (verified by test). Captain logic uses EV. Transfer logic uses EV gain thresholds. No normal-distribution placeholders or random dummy values in production decision path after fixes.

## Part 7 — Testing State (current run)

| Suite | Result |
| ----- | ------ |
| Full test suite | **287 passed, 0 failed, 0 skipped** |
| Ruff | 216 errors (pre-existing style) |
| Mypy | 68 errors in 13 files (pre-existing); **0 in changed files** |

**Newly introduced failures:** None.

## Part 8 — PostgreSQL

- Docker PostgreSQL started via `docker-compose up -d db`.
- **Alembic migrations ran successfully** from clean DB (`alembic upgrade head`).
- PostgreSQL integration tests passed.
- Critical temporal queries and persistence tested through integration suite.

## Part 9 — Critical Gaps Fixed

| Gap | Severity | Fix |
| --- | -------- | --- |
| DecisionBacktester returned hardcoded constants | CRITICAL | Real walk-forward backtester on DB |
| Bench Boost zeroed score fields | HIGH | Compute XI + bench EV from provider |
| Free Hit hardcoded 65.0 | HIGH | Top-11 EV from provider pool |
| Wildcard hardcoded 55.0/GW | HIGH | Top-11 EV from provider pool over horizon |
| Backtester queried non-existent columns | HIGH | Use `provider_event_id` and `Player.position_code` |
| Phase 6.5 claimed Class C without frozen optimizer | HIGH | Downgraded; real backtest documented |

## Part 10 — Empirical Validation Status

| Validation | Status |
| ---------- | ------ |
| Phase 4.5 real-data baseline | VALIDATED |
| Phase 5 development comparison | NOT_RUN |
| Phase 5 ablations | NOT_RUN |
| Phase 5 distribution calibration | NOT_RUN |
| Phase 5 Monte Carlo convergence | NOT_RUN |
| Phase 5 final 2025-26 holdout | NOT_RUN (post-freeze) |
| Phase 6.5 transfer backtest (dev) | VALIDATED (2023-24: 1696, 2024-25: 2150) |
| Phase 6.5 hit strategy | VALIDATED (hit_roi: -1.79 to -2.67) |
| Phase 6.5 captain backtest | VALIDATED (174–279 captain pts) |
| Phase 6.5 starting XI backtest | VALIDATED (formation-valid XI by EV) |
| Phase 6.5 bench/chip backtests | NOT_RUN |
| Phase 6.5 locked holdout | RUN_BUT_NOT_FROZEN (2025-26: 1719 pts, baseline provider) |

**Real Phase 6.5 results** (`docs/phase6-5-real-results.json`):

| Season | Total | GW Avg | Transfers | Hit Costs | Captain | Variance |
| ------ | ----- | ------ | --------- | --------- | ------- | -------- |
| 2022-23 | 164.0 | 4.32 | 5 | 16 | 22 | 105.89 |
| 2023-24 | 1696.0 | 44.63 | 19 | 72 | 174 | 468.81 |
| 2024-25 | 2150.0 | 56.58 | 22 | 84 | 279 | 500.30 |
| 2025-26 | 1719.0 | 45.24 | 18 | 68 | 200 | 471.71 |

> Genuine results from real FPL data. No fabricated constants.

| PredictionModel (ABC) | Interface | `prediction/models.py` |
| MinutesModel | Heuristic baseline | Uses fixture count, position, historical minutes; NOT a learned ML model |
| TeamStrengthModel | Heuristic baseline | Attack/defence estimates; NOT learned |
| AdvancedPlayerModel | Orchestrator | Combines heuristic sub-models |
| GoalModel | Heuristic (xG-based) | Uses expected goals; NOT learned |
| DistributionEngine | Deterministic | Computes distributions from component probabilities |
| Calibration | Evaluation | `CalibrationReport` dataclass |
| ModelRegistry | Versioning | `prediction/registry.py` + DB model |
| WalkForwardTrainer | Pipeline | `prediction/walkforward.py` |
| WalkForwardValidator | Validation | `backtesting/walk_forward.py` |

**Classification:** MinutesModel, TeamStrengthModel, GoalModel are **heuristic baselines** (formula-based). No component uses learned weights from training data. Correctly implemented but not ML models.

2. **Free Hit** used hardcoded `65.0` constant — fixed to use top-11 EV from provider pool.
3. **Wildcard** used hardcoded `55.0/GW` constant — fixed to use top-11 EV from provider pool over horizon.

2026/27 rules correctly represented: `RULES_2026_27` with `is_half_season_chips=True`, two chip sets, first/second-half inventory, one-chip-per-GW. Tests verify Wildcard permanence and Free Hit temporary restoration.

### Phase 6.5
**Previous claim:** "COMPLETE" with Classification C (`docs/phase6-5-final-report.md`).
**Actual status:** `IMPLEMENTED_NOT_FULLY_VALIDATED`.

`DecisionBacktester.backtest_strategy` was a **placeholder returning hardcoded constants** (`total_points: 2450.0, captain_roi: 45.2`). Replaced with genuine walk-forward backtester. Real backtest ran on 4 seasons, but with a **baseline recent-form provider**, not frozen Phase 5 models. **Classification C cannot be claimed** — holdout was not untouched relative to a frozen optimizer.

| 4.75 — Real Data Integration | VALIDATED | `run_phase475_gate.py`, `detect_contamination()` PASS, 4 real seasons imported | — | None |
| 5 — Advanced Player Models | IMPLEMENTED_NOT_VALIDATED | AdvancedPlayerModel, goal/assist/CS/bonus/defensive models, DistributionEngine, GameweekSimulator, holdout policy (17 tests). **Not validated on real data after freeze.** | Final holdout not run post-freeze; calibration not real-data validated | Run holdout after freeze |
| 6 — Decision Optimization | COMPLETE | DecisionPredictionProvider, SquadState, FPLRules (2026/27), CaptainOptimizer, StartingXIOptimizer, TransferOptimizer, MultiTransferPlanner, RankStrategy, all 4 chips, DecisionBacktester. **Chips fixed during reconciliation.** | — | None |
| 6.5 — Decision Validation | IMPLEMENTED_NOT_FULLY_VALIDATED | Real DecisionBacktester runs on real data (fixed). Dev seasons validated. Holdout ran but **with baseline provider, not frozen optimizer.** Classification C not claimable. | Holdout not run with frozen optimizer; chip backtests not individual | Run with frozen models |
| 7 — News + Injury + Availability | NOT_STARTED | — | All Phase 7 components | Begin after readiness gate |
