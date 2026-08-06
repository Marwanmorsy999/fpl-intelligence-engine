# PROJECT_STATUS.md — Authoritative Source of Truth

**Last updated:** 2026-08-06 (Phase 9.0.5 Repository Verification — infrastructure locked)
**Maintained by:** Reconciliation Agent

> This file is the single source of truth for project status. Do not rely on
> individual phase completion reports without cross-referencing this file.

---

## Phase 1 — Foundation

```
Implementation: 100%
Automated Testing: 100%
Real-Data Validation: N/A
Final Holdout: N/A
Status: COMPLETE
```
**Tests:** All unit/integration tests pass (287 total).
**Known limitations:** None.
**Remaining tasks:** None.

## Phase 2 — Historical Data + Canonical Data Layer

```
Implementation: 100%
Automated Testing: 100%
Real-Data Validation: 100% (4 seasons imported)
Final Holdout: N/A
Status: COMPLETE
```
**Tests:** Provider normalization, entity resolution, historical import tests pass.
**Known limitations:** 2022-23 data has a format anomaly (low backtest score).
**Remaining tasks:** Investigate 2022-23 data-format issue.

## Phase 3 — Time-Aware Feature Store + No-Look-Ahead Backtesting

```
Implementation: 90%
Automated Testing: 100%
Real-Data Validation: 50%
Final Holdout: N/A
Status: PARTIALLY_COMPLETE
```
**Tests:** Temporal integrity tests pass; walk-forward validator tests pass.
**Known limitations:**
- No-look-ahead enforced via Gameweek ordering, not strict timestamp availability.
- `ingested_at` set to `datetime.now()` not historical time.
- DB-level STRICT_REPRODUCIBILITY not fully validated on real data.
**Remaining tasks:**
- Set `ingested_at` to historical timestamps during import.
- Validate timestamp-based reproducibility on real data.

## Phase 4 — Quantitative Prediction Engine

```
Implementation: 100%
Automated Testing: 100%
Real-Data Validation: 80%
Final Holdout: N/A
Status: COMPLETE
```
**Tests:** MinutesModel, TeamStrengthModel, ModelRegistry, WalkForwardTrainer tests pass.
**Known limitations:** Models are heuristic baselines, not learned ML models.
**Remaining tasks:** None critical.

## Phase 4.5 — Quantitative Edge Validation

```
Implementation: 100%
Automated Testing: 100%
Real-Data Validation: 100%
Final Holdout: N/A
Status: VALIDATED
```
**Tests:** Edge evaluation tests pass.
**Known limitations:** None.
**Remaining tasks:** None.

## Phase 4.75 — Real Historical Data Integration

```
Implementation: 100%
Automated Testing: 100%
Real-Data Validation: 100% (4 real seasons)
Final Holdout: N/A
Status: VALIDATED
```
**Tests:** Contamination check PASS, coverage matrix, entity resolution.
**Known limitations:** 2022-23 data anomaly.
**Remaining tasks:** Investigate 2022-23.

## Phase 5 — Advanced Player Models + Distributions + Monte Carlo

```
Implementation: 100%
Automated Testing: 100%
Real-Data Validation: 0%
Final Holdout: Pending
Status: IMPLEMENTED_NOT_VALIDATED
```
**Tests:** 17 holdout policy tests, goal/assist/CS/bonus model tests, distribution engine tests, simulation tests — all pass.
**Known limitations:**
- Final 2025-26 holdout NOT run after model freeze.
- Distribution calibration not validated on real data.
- Monte Carlo convergence not demonstrated on real data.
- Models are heuristic (xG-based), not learned.
**Remaining tasks:**
1. Run development-season comparison on real data.
2. Run distribution calibration on real data.
3. Demonstrate Monte Carlo convergence.
4. Freeze models.
5. Run final 2025-26 locked holdout (read-only, post-freeze).

## Phase 6 — Decision Optimization

```
Implementation: 100%
Automated Testing: 100%
Real-Data Validation: N/A
Final Holdout: N/A
Status: COMPLETE
```
**Tests:** Optimization tests pass (5), Phase 6.5 tests pass (2), chip tests pass.
**Known limitations:** None after reconciliation fixes.
**Remaining tasks:** None.

## Phase 6.5 — Decision Optimization Validation

```
Implementation: 100%
Automated Testing: 100%
Real-Data Validation: 60%
Final Holdout: RUN_BUT_NOT_FROZEN
Status: IMPLEMENTED_NOT_FULLY_VALIDATED
```
**Tests:** simulate_decision tests pass.
**Real-data results:** 2023-24 (1696 pts), 2024-25 (2150 pts), 2025-26 holdout (1719 pts).
**Known limitations:**
- Holdout ran with baseline recent-form provider, NOT frozen Phase 5 models.
- Classification C cannot be claimed.
- Individual chip backtests (Wildcard/FreeHit/BenchBoost/TripleCaptain) not run.
- DGW/BGW scenarios not specifically tested.
**Remaining tasks:**
1. Freeze Phase 5 advanced models.
2. Re-run holdout with frozen models.
3. Run individual chip backtests on real DGW/BGW data.
4. Run ablation: optimizer ON vs OFF.

## Phase 7 — News + Injury + Availability Intelligence

```
Implementation: 100% (engineering verified)
Automated Testing: 100% (56 Phase 7 tests; 323 total)
Real-Data Validation: 0% (BLOCKED — forensic re-audit, see below)
Final Holdout: N/A (still isolated)
Status: ENGINEERING_COMPLETE / BLOCKED (INSUFFICIENT HISTORICAL AVAILABILITY DATA)
Production DB verification: VERIFIED (2026-08-06 — PostgreSQL reachable, alembic 0007_historical_availability, all 9 Phase 7 tables 0 rows, production-path importer fetched=2447 matched=0 unmatched=2447 persisted=0)
```
**Tests:** Evidence corroboration, state derivation, minutes model wrapper,
prediction provider wrapper, DB providers, real metrics, coverage + temporal
audits, and evaluate_phase7 fixture — 56 tests pass.
**Phase 7.2 (re-opened, forensically corrected):** The availability adapter
(`RealFPLAvailabilityProvider`, provider `real_fpl_bootstrap`) fetches
**2,447 REAL player-season rows** from `players_raw.csv`. They are **real** (not
mock) but are (a) **player-season snapshot rows**, not discrete availability
events, and (b) **terminal season-end snapshots** whose `news_added` timestamp
is a look-ahead signal — temporally unfit for strict backtesting.
**Forensic root cause (the "2,447 matched vs 2,447 skipped" contradiction):**
`run_phase72_import.py` PRE-SEEDS `PlayerExternalId` under `real_fpl_bootstrap`,
so all 2,447 resolve there (harness only). The production runner
`run_phase7_validation.py` ingests canonical players under `real_fpl` only; the
availability importer resolves under `real_fpl_bootstrap`, a **different
`PlayerExternalId` key**, so in production **all 2,447 are UNMATCHED**
(`availability_events = 0`). Both logs describe the same 2,447 records.
**Forensic resolver audit** (`docs/phase7-2-forensic-audit.json`, conservation
`fetched == Σ terminal`, `conservation_ok=true`):
- Production path: `fetched=2447, matched=0, unmatched=2447, persisted=0`.
- Seeded harness: `fetched=2447, matched=2447, skipped_temporal_invalid=169,
  persisted=2278` (5 structured derived tables remain 0 — no such columns in
  `players_raw.csv`).
**Real-data status:** In the production path Phase 7 availability tables are
EMPTY, so BASELINE ≡ PHASE7 and no comparison is possible. The "PARTIALLY
TESTABLE" status from the prior update is **revoked** by the forensic re-audit.
The honest classification is **BLOCKED — INSUFFICIENT HISTORICAL AVAILABILITY
DATA** (not A/B/C). See `docs/phase7-empirical-validation-report.md` §18.
**Known limitations:**
- **Production entity-resolution key mismatch** (`real_fpl` vs
  `real_fpl_bootstrap`) — availability importer resolves nothing in production.
- `players_raw.csv` is a **terminal season snapshot** (look-ahead, not strict
  pre-deadline).
- The availability source is a Layer B surrogate; it does not provide full
  press-conference / training / journalist-report coverage.
- `_sources_for_event` resolves single-source provenance only (primary_source_id).
- A live news-ingestion pipeline is not yet wired to a production source.
**Remaining tasks (BLOCKED until resolved):**
1. Fix the `real_fpl` ↔ `real_fpl_bootstrap` provider-key aliasing so the
    availability importer can resolve players ingested by the production path.
2. Acquire a genuinely historical (pre-deadline) availability/news source, or
   accept that `players_raw.csv` cannot support strict backtesting.
3. Re-run `run_phase7_validation.py`; confirm `availability_events > 0` with
   non-look-ahead timestamps before any BASELINE vs PHASE7 evaluation.
4. Do **not** classify Phase 7 as A/B/C until a valid dataset exists.

---

## Phase 8 — Tactical Intelligence Scope and Source Audit

```
Implementation: 0% (SCOPE/SOURCE AUDIT ONLY — NOT authorised for implementation)
Automated Testing: 0% (no code changes)
Real-Data Validation: N/A (not evaluated)
Final Holdout: N/A (not evaluated)
Status: SCOPE_AUDIT_IN_PROGRESS
Classification: NOT A/B/C (scope audit only)
```
**Scope:** Investigate potential sources for: (1) starting lineups, (2)
formations, (3) player positions, (4) player role changes, (5) positional role
context, (6) set-piece takers (penalties/free kicks/corners), (7) manager
changes, (8) manager formation tendencies, (9) manager rotation tendencies,
(10) team style indicators, (11) opponent tactical matchup context, (12)
minutes risk due to role changes, (13) differential signals from tactical
shifts.

**Findings:**
- **Currently available project data sources audited:** FPL bootstrap
  (`players_raw.csv`), gameweek CSVs (`gws/gwN.csv`), fixtures CSV, teams CSV,
  official FPL API. See `docs/phase8-source-audit.md`.
- **0 of 15 signals empirically testable** with current data. No source
  provides pre-deadline confirmed tactical information with publication
  timestamps that can pass `InformationAccessPolicy.STRICT_REPRODUCIBILITY`.
- **All 15 signals engineering-only** pending acquisition of a
  `STRICT_BACKTEST_SAFE` historical source. The temporal infrastructure
  (`AvailabilityTimestamps`, `TemporalClass`, `InformationAccessPolicy`,
  `TemporalQueryBuilder`) is ready to represent and enforce constraints.
- **Set-piece orders** (`penalties_order`, `direct_freekicks_order`,
  `corners_and_indirect_freekicks_text`) are real and populated across all
  development seasons but are `UNSAFE_LOOKAHEAD` (terminal season-end snapshot,
  no per-gameweek timing). No per-GW snapshot of these fields exists in the
  vaastav `gws/gwN.csv` schema.
- **Starting lineups** via `gws/gwN.csv` `starts` column are real but
  `HISTORICAL_OUTCOME_ONLY` (post-match outcome, not pre-deadline signal).
- **Candidate sources audited:** FBref match reports, football-data.org API,
  TheStatsAPI, Transfermarkt, club official archives, community-predicted
  lineup aggregators. See `docs/phase8-source-audit.md` §4–6.
- **Fundamental temporal gap:** Tactical information (confirmed lineups,
  formations) is announced ~60 min before kickoff, which is *after* the FPL
  deadline (~90 min before first kickoff). Only press conferences (1–2 days
  pre-match) provide pre-deadline tactical hints, and no clean historical
  archive of club press conferences exists.
- **Classification:** Phase 8 is NOT classified A/B/C. It is a scope/source
  audit only. No tactical models are implemented or evaluated.

**Remaining tasks:**
1. Complete scope/source audit (this phase).
2. Acquire a `STRICT_BACKTEST_SAFE` historical source for at least one signal
   before any Phase 8 implementation is authorized.
3. Do **not** implement tactical models until a pre-deadline source with
  publication timestamps is validated against the temporal integrity framework.

**Documents produced:**
- `docs/phase8-scope.md`
- `docs/phase8-source-audit.md`
- `docs/phase8-temporal-feasibility.md`

---

## Phase 9 — Live Intelligence Accumulator & LLM Analyst Layer

```
Implementation: 100% (foundation complete)
Automated Testing: 100% (51 Phase 9 tests; 394 total, 0 failed / 0 skipped / 0 errored)
Real-Data Validation: N/A (BLOCKED — no pre-deadline historical source yet)
Final Holdout: N/A
Status: FOUNDATION_CLOSED / AWAITING_EMPIRICAL_DATA
Classification: NOT A/B/C (foundation only, no live scraping, no evaluation)
```

**Tests:** Full suite = **394 passed, 0 failed, 0 skipped, 0 errored** (`pytest -q`,
exit code 0, ~38s). Breakdown: **343 pre-existing** (Phases 1–7 + integration) +
**51 new Phase 9** = 394. Verified via `--junitxml`
(`tests=394 failures=0 errors=0 skipped=0`) and reproduced across three
consecutive clean runs.

**Architecture:**
- **Temporal Ledger** (`temporal_ledger.py`): append-only, four mandatory temporal fields, `available_at = max(published_at, scraped_at)` under `CONSERVATIVE` policy. `LedgerItemView` is the only thing the LLM extractor sees.
- **Ingestion Pipeline** (`ingestion.py`): text-in only, content-hash dedupe, temporal invariant validation, deadline classification.
- **LLM Extractor** (`extraction.py` + `prompts.py` + `schemas.py`): renders versioned prompt, calls provider, parses strict JSON, validates against `extra="forbid"` Pydantic models, grounding check, temporal inheritance from ledger.
- **Mock LLM** (`mock_llm.py`): deterministic, rule-based, `is_mock=True`, zero API calls.
- **AI Analyst** (`analyst.py` + `analyst_prompts.py`): reads `DecisionPredictionProvider` read-only, 4 guardrails (restatement, no invented baselines, citation validation, empty-evidence→neutral). `AnalystOutput` separates `quantitative_baseline` from `qualitative_adjustment`.

**Key invariants:**
- LLM has no timestamp fields in any schema → structurally cannot date-shift.
- `is_validation_evidence()` requires `PRE_DEADLINE + real + not-mock-extraction`.
- Phase 7 tables (`availability_evidence`) are **not modified**; Phase 9 writes into them and records provenance in `live_availability_evidence_links`.
- Nothing is silently dropped: unresolved entities, ungrounded quotes, and schema rejections are recorded.

**Phase 9.0 test-suite regression audit (2026-08-06) — NO REGRESSION FOUND:**
A suspected regression ("full suite dropped to 133 tests; ~210 tests from Phases
4–7 missing from discovery") was investigated and **disproven**. No tests were
lost, deleted, or hidden from discovery. Findings:
- **Discovery is intact.** `pytest --collect-only -q` collects **21 test files /
  394 tests** across all four subdirectories: `tests/unit/` (238),
  `tests/prediction/` (145), `tests/integration/` (6), `tests/optimization/` (5).
  Zero collection errors.
- **No test files are missing.** Phase 4/5 (`tests/prediction/test_phase5_complete.py`,
  145 tests), Phase 6/6.5 (`tests/optimization/`, 5 tests) and Phase 7
  (`tests/unit/test_phase7_availability.py`, 56 tests) are all present and
  passing. No restore from history was required, and **no old test was deleted
  or rewritten**.
- **Arithmetic proof:** `pytest --ignore=tests/unit/test_phase9.py` collects
  exactly **343** — identical to the pre-Phase-9 baseline. Phase 9 adds **51**.
  343 + 51 = **394 ≥ 392** target.
- **Root cause:** a **reporting error**, not a code regression. The "133 tests
  (84 existing + 49 new)" and "181+" figures came from scoped/partial `pytest`
  invocations, not a full-suite run, and were copied into this document
  unverified. Those figures are hereby **retracted**.
- **`tests/conftest.py` fixtures were NOT broken.** The shared `db_session`,
  `cutoff_time` and `populated_db` fixtures were never overwritten; all 343
  pre-existing tests that depend on them pass.

**Hardening applied during closure (no test logic changed):**
1. `tests/conftest.py` — removed a **dead** `fpl_intelligence.live_intelligence.models`
   import block added during Phase 9. It was unused (`test_phase9.py` imports
   those models itself and defines its own shadowing `db_session` fixture), and
   it had introduced 6 new lint errors (1×I001, 5×F401) plus needless coupling
   of the shared Phase 1–7 fixture to Phase 9 tables. `conftest.py` is now back
   to exact parity with `ruff_baseline.txt` (2 pre-existing E501s only).
2. `pyproject.toml` — added `testpaths = ["tests"]` under
   `[tool.pytest.ini_options]` so a bare `pytest` always collects the **entire**
   `tests/` tree. This structurally prevents a partial run from being
   misreported as a full-suite result again.

**Known limitations:**
- No live scraping wired yet (manual paste / API ingestion only).
- Entity resolution hook (`resolve_player`, `resolve_team`) is a pass-through; Phase 7 provider-key mismatch must be resolved before real evidence can be persisted.
- No pre-deadline historical source available, so empirical backtest is blocked.
- **NOT classified A/B/C.** No evaluation, no holdout, no live data.

**Phase 9.0 Foundation — CLOSED.** Exit criteria met: implementation complete,
full suite green at 394/394 with the complete pre-Phase-9 suite (343) intact,
lint parity restored against `ruff_baseline.txt`, and discovery pinned via
`testpaths`. **Phase 9.1 (Real LLM Provider / Live Scheduler) is now unblocked
and may begin.**

**Remaining tasks:**
1. Wire a live-scraping or manual-paste ingestion source.
2. Implement canonical entity resolution matching Phase 7 provider keys.
3. Acquire or accumulate a `STRICT_BACKTEST_SAFE` historical source.
4. Run empirical backtest once data is available.
5. Do **not** classify Phase 9 until empirical validation is possible.

**Documents produced:**
- `docs/phase9-architecture.md`

## Phase 9.0.5 — Repository Verification and Infrastructure Lock

```
Implementation: 100%
Automated Testing: 100%
Real-Data Validation: N/A
Final Holdout: N/A
Status: CLOSED
Classification: N/A (infrastructure gate)
```

**Verification results (2026-08-06):**
- **Git status:** clean on `main`
- **Remote:** `origin` → `https://github.com/Marwanmorsy999/fpl-intelligence-engine.git` (`main` tracking `origin/main`)
- **HEAD:** `bf49ea1` Phase 9.0 foundation
- **Tags:** `Phase-9.0-foundation`, `v0.9.0-foundation`
- **Tests:** `pytest -q` → **394 passed, 0 failed, 0 skipped, 0 errored**
- **Ruff baseline:** preserved as-is (see `ruff_baseline.txt`); `--generate-baseline` unavailable in installed ruff 0.16.1, and the remaining entries represent committed source-code and migration violations that are out of scope for this gate.

**Infrastructure changes applied:**
- Renamed default branch from `master` to `main`
- Pushed `main` and tags `Phase-9.0-foundation` and `v0.9.0-foundation` to GitHub
- Removed 52 tracked scratch/junk files (`_*.py`, `_*.txt`, `tmp_fix_eval.py`, `phase475.err`, `phase475.pid`, `src/f`, `src/fpl`)
- Hardened `.gitignore` to block future commits of secrets, scratch files, database dumps, IDE files, and local caches

**Version control:** ACTIVE
**GitHub remote:** CONFIGURED
**Foundation tag:** v0.9.0-foundation
**Phase 9.0:** CLOSED
**Phase 9.1:** UNBLOCKED

---

## Summary

| Phase | Impl % | Test % | Real-Data % | Status |
| ----- | ------ | ------ | ----------- | ------ |
| 1 | 100 | 100 | N/A | COMPLETE |
| 2 | 100 | 100 | 100 | COMPLETE |
| 3 | 90 | 100 | 50 | PARTIALLY_COMPLETE |
| 4 | 100 | 100 | 80 | COMPLETE |
| 4.5 | 100 | 100 | 100 | VALIDATED |
| 4.75 | 100 | 100 | 100 | VALIDATED |
| 5 | 100 | 100 | 0 | IMPLEMENTED_NOT_VALIDATED |
| 6 | 100 | 100 | N/A | COMPLETE |
| 6.5 | 100 | 100 | 60 | IMPLEMENTED_NOT_FULLY_VALIDATED |
| 7 | 100 (eng) | 100 | 0 (BLOCKED, forensic re-audit) | ENGINEERING_COMPLETE / BLOCKED |
| 8 | 0 (scope audit only) | 0 | N/A | SCOPE_AUDIT_IN_PROGRESS (not A/B/C) |
| 9 | 100 (foundation) | 100 | N/A (awaiting data) | FOUNDATION_CLOSED / AWAITING_EMPIRICAL_DATA (not A/B/C) |
| 9.0.5 | 100 | 100 | N/A | CLOSED / INFRASTRUCTURE_LOCKED |

**Full test suite (authoritative, 2026-08-06):** `pytest -q` → **394 passed,
0 failed, 0 skipped, 0 errored** (exit 0). 21 files across `tests/unit/` (238),
`tests/prediction/` (145), `tests/integration/` (6), `tests/optimization/` (5).
Composition: 343 pre-existing (Phases 1–7) + 51 Phase 9.


**PHASE_9_0_5_READY = TRUE** (repository verified, infrastructure locked, Phase 9.1 unblocked)
