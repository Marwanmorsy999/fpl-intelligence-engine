# PROJECT_STATUS.md — Authoritative Source of Truth

**Last updated:** 2026-08-13 (Phase 9.1 — OFFICIALLY CLOSED: full suite + live PostgreSQL verification)
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
**Phase 9.1:** OFFICIALLY CLOSED (see "Phase 9.1 — Final Verification and Closure")

---

## Phase 9.1 — Smart API-key/Provider Assignment

**Status:** OFFICIALLY CLOSED (2026-08-13; implementation complete 2026-08-06).
Verification evidence is recorded in "Phase 9.1 — Final Verification and
Closure" below. Adds safe, task-aware routing of
extraction work across real LLM providers without hardcoding a single key and
without touching Phases 1–8.

**Delivered:**

- **`provider_router.py` — `ProviderRouter`.** Implements `LLMProvider` so it is a
  drop-in for the extraction pipeline. Task-based routing
  (availability → Groq, tactical/combined → Gemini), automatic fallback to the
  next provider on rate-limit (429) or auth (401/403) errors, optional
  round-robin load balancing, and a `route()`/`complete()` API. It never holds
  API keys — it delegates to `ProviderFactory`, which reads them from the
  git-ignored `.env` via `LLMSettings`.
- **Routing metadata wired end-to-end.** `routing_strategy`
  (`task_based`/`fallback`/`round_robin`) is stamped onto `LLMResponse` and
  `ExtractionProvenance`, and persisted on every `llm_extraction_runs` row
  (migration `0010_phase91_routing_strategy`). The audit trail records *how* a
  provider was reached, not just which provider answered.
- **Secure config + free-tier guards.** `llm_settings.py` holds keys as
  `SecretStr` and prints SHA-256 fingerprints only. Real providers go through
  the response cache, a `max_tokens` cap, a rate limiter and a per-process call
  budget (cache hits cost zero quota).
- **Dry-run tool.** `scripts/live_dry_run_extraction.py` gains `--router` to
  exercise task-based routing with fallback against real providers.
- **`__init__.py`** re-exports `ProviderRouter`, `RouteDecision`,
  `RoutingStrategy`, `ProviderRoutingError` and `DEFAULT_TASK_ROUTES`.

**Constraints honoured:** no keys hardcoded (env only, git-ignored); no live API
calls in pytest (mock/offline doubles); Phases 1–8 untouched; free-tier controls
on; full suite **404 passed** (was 394); `ruff` and `mypy` clean on all Phase 9.1
modules.

**Known limitations:** no live scraping wired yet; entity resolution is a
pass-through; empirical backtest still blocked awaiting data. **Not classified
A/B/C.**

---

## Phase 9.1.1 — Live Dry-Run Schema Reconciliation

**Status:** COMPLETE (2026-08-06). Reconciles the Phase 9.1 live dry-run's schema
rejection of a legitimate `status_mentioned = "available"` payload against
Groq, without weakening any Phase 9 invariant.

**Root cause:** the live model returned a semantically honest status ("trained
fully and is available, but will he start?") that the Phase 7/9 canonical
`AvailabilityStatus` vocabulary did not contain, so the strict
`status_mentioned` enum rejected the row. The fix reconciles the vocabulary —
the model's output was correct; the status list was incomplete.

**Fix (vocabulary, not the model):**

- Added `AVAILABLE = "available"` to `AvailabilityStatus` (Phase 7 models),
  meaning "fit / available / in contention, but not explicitly confirmed to
  start."
- Wired `available` through the Phase 7 status tables:
  `_STATUS_ORDER` (severer than `start`, less severe than `out`),
  `state._STATUS_START_PROB = 0.80`, `state._STATUS_MINUTES_FACTOR = 0.85`.
  These two numerics are **conservative live-engineering heuristics, marked as
  such in code + tests, pending empirical calibration** (no historical Phase 9
  data exists to fit them).
- Remapped `HistoricalEventType.AVAILABLE` → `AvailabilityStatus.AVAILABLE`
  (was `START`).
- Updated the extraction prompt (v1.1.0) to enumerate the full canonical
  status list and give worked normalisation examples; invalid values such as
  `flying` are still rejected.
- Raised `max_output_tokens` 1024 → 2048 (`FREE_TIER_MAX_OUTPUT_TOKENS` +
  `LLMSettings.llm_max_output_tokens`); added a truncation warning in the
  dry-run when `completion_tokens >= max_output_tokens`.
- `ProviderRouter` now reports true free-tier usage (`live_calls` accumulated
  across task-based, retry and fallback calls) and records fallback diagnostics
  (`RouteFailure`: primary provider + coarse reason).
- `RouteFailure` re-exported from `fpl_intelligence.live_intelligence`.

**Delivered (tests, +10):** extractor accepts `available` / rejects `flying`;
prompt templates v1.1.0 with a current (drift-free) hash lock; router
`live_calls` accounting + fallback diagnostics; Phase 7 `available` state/
corroboration/event-type mapping. **404 → 414 passed; ruff + mypy clean.**

---

## Phase 9.1 — Final Verification and Closure

**Status:** OFFICIALLY CLOSED (2026-08-13). This section closes the two
verification gaps left open by the Phase 9.1.2 acceptance review: (1) only a
256-test subset had been run, and (2) the enum migration had been verified on
SQLite but never applied to a live PostgreSQL database.

### Gap 1 — Full test suite (was: 256 of 414)

`pytest -q` run from the repository root, which collects the entire `tests/`
tree per the `testpaths` setting in `pyproject.toml`:

| Directory | Tests |
| --------- | ----- |
| `tests/unit/` | 258 |
| `tests/prediction/` | 145 |
| `tests/integration/` | 6 |
| `tests/optimization/` | 5 |
| **Total** | **414** |

**Result: 414 passed, 0 failed, 0 errored, 0 skipped (exit code 0).** Counts are
taken from a `--junit-xml` report (`tests=414 failures=0 errors=0 skipped=0`) and
were reproduced per-directory, so the total is machine-verified rather than read
off a terminal summary. The suite was run twice — before and after the migration
fix described below — with identical results.

> Note on the earlier 256-test figure: it was a partial run. Nothing in the
> repository restricts collection; `pytest -q` alone reaches all 414 tests.

### Gap 2 — Migration applied to live PostgreSQL

Target: the Docker Compose `db` service (`postgres:16-alpine`), database
`fpl`, reached via the `alembic.ini` URL
`postgresql+psycopg://fpl:fpl@localhost:5432/fpl`. Container state confirmed
`Up (healthy)` and `pg_isready` → `accepting connections` before migrating.

The live `fpl` database was found **completely unmigrated** (no
`alembic_version` table, no `availabilitystatus` type), so `alembic upgrade head`
applied the entire chain `0001 → 0011` from scratch. This is a stronger check
than upgrading an existing database: it verifies the whole migration chain
against a clean PostgreSQL instance.

**Result:** all 11 migrations applied, exit code 0. 42 tables created.

```
alembic current
0011_phase912_availability_enum (head)

SELECT version_num FROM alembic_version;
           version_num
---------------------------------
 0011_phase912_availability_enum
```

Re-running `alembic upgrade head` emits no further `Running upgrade` steps and
exits 0, confirming idempotency at the chain level.

**Operational caveat — `pytest` destroys the live PostgreSQL schema.** The
`pg_engine` fixture in `tests/integration/test_postgresql.py` runs
`DROP SCHEMA IF EXISTS public CASCADE`, then `Base.metadata.create_all`, then
`DROP SCHEMA public CASCADE` on teardown — against the same `fpl` database that
`alembic.ini` targets (both default to
`postgresql+psycopg://fpl:fpl@localhost:5432/fpl`). Running the suite therefore
leaves the live database with **no schema at all**: no `alembic_version`, no
`availabilitystatus` type. This was observed directly during this verification —
the enum queries began failing with `type "availabilitystatus" does not exist`
after the suite was re-run, until the chain was re-applied.

Consequences to respect:

- **Migrate last.** Any PostgreSQL migration verification must run *after* the
  test suite, or it will be silently undone. The recorded final state below was
  captured after the last `pytest` invocation.
- The full chain was applied from scratch **twice** in this session (once before
  the suite, once after), both times cleanly — independent reproduction of the
  `0008` fix.
- A future fix should point the integration tests at a dedicated throwaway
  database rather than the deployment database.

The live database's final resting state, captured after the last `pytest` run:

```
$ alembic upgrade head          # full chain 0001 -> 0011 re-applied, exit 0
$ alembic current
0011_phase912_availability_enum (head)

fpl=# SELECT version_num FROM alembic_version;
           version_num
---------------------------------
 0011_phase912_availability_enum
(1 row)

fpl=# SELECT 'available' = ANY(enum_range(NULL::availabilitystatus)::text[]);
 t
```

### Gap 3 (discovered) — Migration `0008` could not build a fresh PostgreSQL database

Applying the chain to real PostgreSQL for the first time exposed a **blocking
pre-existing bug** in `0008_phase9_live_intelligence.py`. It aborted with:

```
psycopg.errors.DuplicateObject: type "livesourcetype" already exists
[SQL: CREATE TYPE livesourcetype AS ENUM (...)]
```

Two distinct defects, both invisible on SQLite (which has no native enum types)
and invisible to the test suite (no test executes Alembic migrations):

1. **Double creation of the six Phase 9 enum types.** `upgrade()` pre-created
   each type with `.create(bind, checkfirst=True)` and *also* passed the same
   `sa.Enum` objects as column types to `op.create_table`. SQLAlchemy emits
   `CREATE TYPE` for an inline enum when the owning table is created, using
   `checkfirst=False`, so the second attempt raised `DuplicateObject`.
   Migration `0006` survives the same pattern only because it never
   pre-creates: it relies on SQLAlchemy's per-DDL-runner memo, which suppresses
   repeat `CREATE TYPE` calls within one migration run. An explicit
   `.create()` call does not populate that memo, so pre-creating defeats it.
2. **`create_type=False` was silently ineffective.** The pre-existing
   `sourcereliability` type (owned by `0006`) was referenced via
   `sa.Enum(..., create_type=False)`. `create_type` is **not** a generic
   `sa.Enum` argument — it is swallowed, and the PostgreSQL type it adapts to
   still defaults to `create_type=True` (verified: `sa.Enum(...,
   create_type=False).dialect_impl(postgresql.dialect()).create_type` is
   `True`). Had defect 1 not aborted first, this would have raised
   `DuplicateObject` on `sourcereliability`.

**Fix (in `0008` only; no schema change, no new revision):** each enum type is
created exactly once, explicitly and idempotently (`checkfirst=True`), and all
enum *column references* go through a new `_enum_ref()` helper that returns
`postgresql.ENUM(..., create_type=False)` on PostgreSQL — the dialect-specific
type that actually honours the flag — and a plain `sa.Enum` elsewhere.
`sourcereliability` is referenced only, never created. Rationale and the
`create_type` pitfall are documented inline.

This fix is confined to migration DDL construction: no ORM model, no
application code, and no migration revision identifier changed. The full suite
was re-run afterwards (414 passed) and `ruff` reports the same two pre-existing
`E501` warnings as at `HEAD` — no new lint findings.

### Enum verification in PostgreSQL

```
fpl=# SELECT enum_range(NULL::availabilitystatus);
                                 enum_range
-----------------------------------------------------------------------------
 {start,available,bench,doubtful,questionable,suspect,out,suspended,unknown}
(1 row)

fpl=# SELECT unnest(enum_range(NULL::availabilitystatus)) AS enum_value;
  enum_value
--------------
 start
 available
 bench
 doubtful
 questionable
 suspect
 out
 suspended
 unknown
(9 rows)
```

`available` is present. Supporting checks:

- **Ordering as designed.** `pg_enum.enumsortorder` places `available` at
  `1.5`, immediately after `start` (`1`) and before `bench` (`2`), matching the
  `AFTER 'start'` clause and `_STATUS_ORDER` in `availability/evidence.py`
  (`START=1, AVAILABLE=2, BENCH=3`).
- **Functional persistence, not just type introspection.** Inserting
  `'available'` into a real `availabilitystatus` column succeeds and reads back
  as `available` with `pg_typeof = availabilitystatus`. This is the exact
  operation that previously raised a constraint violation.
- **Python ↔ PostgreSQL parity.** All 9 `AvailabilityStatus` members exist in
  the PostgreSQL type and vice versa (0 missing, 0 extra). Declaration order
  differs (Python lists `bench` before `available`) but this is cosmetic: the
  Python enum is persisted by value and no query orders by the native enum.
- **Consumers confirmed.** Three columns use the type:
  `availability_events.status`, `availability_evidence.status_mentioned`,
  `player_mentions.extracted_status`.

### Code-review remediation — findings 1–7 (PREREQ: integration-test isolation)

After closure, a `/review uncommitted` pass found 10 issues in the Phase 9.1
layer; findings 8–10 (dead code, unused indexes, per-row flush) are deferred to
a later tech-debt pass. Findings 1–7 were remediated in source and covered by
regression tests in `tests/unit/test_phase9_finding_remediations.py` (13 new
tests; full suite 421 unit/optimization/prediction + 6 integration, all green).

| # | Severity | Finding | Fix |
| - | -------- | ------- | --- |
| 1 | CRITICAL | `ProviderRouter._build_provider` built each provider with **no** budget/limiter/cache, so `ProviderFactory.create()` minted fresh per-call instances and the `LLM_MAX_CALLS_PER_RUN` ceiling was never enforced across routed calls. | `ProviderRouter.__init__` now owns one shared `CallBudget`, `RateLimiter` and `ResponseCache` (injected or built from settings) and threads them through `_build_provider` → `factory.create(...)` for every primary, retry and fallback. Exposed as `router.budget`/`rate_limiter`/`cache` for the dry-run. |
| 2 | MEDIUM | `MockLLMProvider` matched `"available"` *inside* `"unavailable"` (substring `in`). | Keyword matching now uses word boundaries (`\b<keyword>\b`); added an explicit `"unavailable" → DOUBTFUL` rule so the negative signal is recognised. |
| 3 | WARNING | `evidence.py _STATUS_ORDER` ranked `AVAILABLE(2) > START(1)`, so a vague "available" overrode an explicit "start". | Swapped to `AVAILABLE(1) < START(2)`; an explicit start now wins. `OUT`/`SUSPENDED` still outrank both. |
| 4 | MEDIUM | `LLMSettings.model_for()` applied the global `LLM_MODEL` override to **any** provider, leaking a primary-only pin onto fallback providers. | Override now applies only when `target == self.llm_provider` (or unspecified); fallbacks use `DEFAULT_MODELS[target]`. |
| 5 | HIGH | The live-call budget was charged once per `complete()`, under-counting retries (up to `max_retries+1` real requests). | `RealLLMProvider` no longer pre-charges; `_consume_request_slot()` claims one budget slot **per HTTP attempt** inside `_invoke_with_retry()`. `live_calls` now aliases `live_requests` (counts every request, success or fail). |
| 6 | WARNING | Historical FPL code `"a"` split-brain: `_map_fpl_status` → `START` while `_event_type_from_status` and `event_types.py` → `AVAILABLE`. | `_map_fpl_status("a")` now → `AVAILABLE`, consistent with the event-type path. |
| 7 | MEDIUM | `Retry-After: 0` (or any non-positive value) was parsed as "retry now"; backoff had no floor. | `_retry_after_seconds` returns `None` for non-positive/invalid values; `_backoff` floors the delay at the configured pacing interval and still caps at 60s. |

> Constraint honoured: no live API calls in `pytest`; no API keys hardcoded;
> Phases 1–6 quantitative logic untouched. Integration tests were isolated to a
> dedicated `fpl_intelligence_test` database (with an `UnsafeTestDatabaseError`
> guard) in a prerequisite step, so the 6 integration tests run only against a
> disposable DB and never the live `fpl`.

### Corrections to previously recorded status

- `tests/unit/` is **258** tests, not 248. The old breakdown still summed to the
  superseded 404 total and was never updated when Phase 9.1.1 added 10 tests.
- The claim of "1 migration idempotency test" is **withdrawn**. No test in the
  suite executes Alembic; migration idempotency is verified manually (recorded
  above), which is precisely why the `0008` bug reached this point undetected.

### Exit criteria

| Criterion | Result |
| --------- | ------ |
| Full suite ≥ 414 tests, 0 failures | 414 passed / 0 failed / 0 errored |
| PostgreSQL container healthy | `Up (healthy)`, `pg_isready` OK |
| `alembic upgrade head` on PostgreSQL | Chain `0001 → 0011` applied, exit 0 |
| `0011_phase912_availability_enum` applied | `alembic_version` = `0011_…` (head) |
| Enum contains `available` | Confirmed, plus functional insert |
| `ruff` regressions | None (2 pre-existing `E501`, unchanged) |

**Residual risks (open, none blocking closure):**

1. **The migration chain has no automated coverage.** No test executes Alembic,
   which is exactly why the `0008` defect survived from Phase 9 until the first
   real PostgreSQL apply. A future migration can break fresh provisioning the
   same way. Recommended follow-up: a CI test running `alembic upgrade head`
   (and `downgrade`) against a throwaway PostgreSQL database.
2. **The integration tests share the deployment database and drop its schema**
   (see the operational caveat under Gap 2). Any migrated state in `fpl` is
   destroyed by a `pytest` run. Recommended follow-up: give the integration
   tests their own disposable database via `POSTGRES_URL`.
3. **Uncalibrated heuristics, unchanged from Phase 9.1.1:**
   `_STATUS_START_PROB = 0.80` and `_STATUS_MINUTES_FACTOR = 0.85` for
   `available` remain engineering estimates awaiting empirical data.
4. **Phase 9.1/9.1.2 work is uncommitted.** Migrations `0009`–`0011` and the
   Phase 9.1 modules are still untracked/modified in the working tree; closure
   is recorded against the working tree, not a commit or tag.

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
| 9.1 | 100 (implementation) | 100 | N/A (awaiting data) | **OFFICIALLY CLOSED** (2026-08-13) / not A/B/C |
| 9.1.1 | 100 | 100 | N/A (heuristics pending calibration) | COMPLETE — availability vocabulary reconciliation |
| 9.1.2 | 100 | 100 | N/A (verified on live PostgreSQL) | COMPLETE — PostgreSQL `availabilitystatus` enum now includes `available` |

**Full test suite (authoritative, 2026-08-13):** `pytest -q` → **414 passed,
0 failed, 0 skipped, 0 errored** (exit 0), confirmed from a `--junit-xml`
report. 21 files across `tests/unit/` (258), `tests/prediction/` (145),
`tests/integration/` (6), `tests/optimization/` (5).
Composition: 343 pre-existing (Phases 1–7) + 71 Phase 9 (51 foundation + 10
Phase 9.1 ProviderRouter + 10 Phase 9.1.1 availability-vocabulary tests).
`ruff` and `mypy` clean on all touched modules. No test executes Alembic
migrations — migration behaviour is verified manually (see "Phase 9.1 — Final
Verification and Closure").

**Phase 9.1.2 migration:** `0011_phase912_availability_enum.py` — `alembic
upgrade head` **applied to the live Docker PostgreSQL database** (`fpl` on
`postgres:16-alpine`); `alembic_version` = `0011_phase912_availability_enum`
(head); `SELECT enum_range(NULL::availabilitystatus)` returns
`{start,available,bench,doubtful,questionable,suspect,out,suspended,unknown}`;
inserting `'available'` into an `availabilitystatus` column succeeds;
idempotency verified by re-running `upgrade head`. Also passes on SQLite, where
the PostgreSQL guard is a correct no-op. ruff + mypy clean.

**Migration `0008` fix (2026-08-13):** applying the chain to real PostgreSQL for
the first time exposed a blocking `DuplicateObject: type "livesourcetype"
already exists` failure that made fresh PostgreSQL provisioning impossible.
Root cause was double enum creation plus an ineffective `create_type=False` on a
generic `sa.Enum`. Fixed in `0008` (DDL construction only; no schema change, no
new revision). Details in "Phase 9.1 — Final Verification and Closure".


**PHASE_9_0_5_READY = TRUE** (repository verified, infrastructure locked, Phase 9.1 unblocked)

**PHASE_9_1_CLOSED = TRUE** (full 414-test suite green; migration `0011` applied
and enum verified on live PostgreSQL — Phase 9.2 is unblocked)
