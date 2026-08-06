# Phase 7 Readiness Report — Engineering Completion

**Date:** 2026-08-05
**Status:** ENGINEERING COMPLETE / EMPIRICAL VALIDATION BLOCKED

> **Phase 7.1 update:** The empirical validation infrastructure is complete
> (real metrics, coverage + temporal audits, fail-fast runner). The real FPL
> provider supplies only structured FPL stats, so the Phase 7 availability
> tables are empty after real import and the empirical experiment is
> **BLOCKED — INSUFFICIENT HISTORICAL AVAILABILITY DATA** (NOT class A).
> See `docs/phase7-empirical-validation-report.md`.

---

## Phase 7 Modules

| Module | Responsibility |
|--------|----------------|
| `availability/models.py` | 9 SQLAlchemy models: `AvailabilitySource`, `AvailabilityArticle`, `AvailabilityEvidence`, `AvailabilityEvent`, `PlayerInjury`, `PlayerSuspension`, `TrainingReport`, `PressConference`, `PlayerMention`. Enums for source reliability, availability status, and evidence type. Preserves provenance and temporal fidelity. |
| `availability/providers.py` | Abstract provider layer: `NewsSource`, `NewsProvider`, `RawEvidence`, `AvailabilityProvider`. Decouples data acquisition (news, training, press) from evidence corroboration and state derivation. |
| `availability/evidence.py` | Evidence corroboration engine. Computes per-item confidence from source reliability × evidence type, aggregates with diminishing-returns union, applies official-source boost, and produces immutable `AvailabilityEvent` records. |
| `availability/state.py` | Availability state derivation. Maps status → start-probability and minutes-factor (calibrated from historical FPL data). `get_current_state`, `get_state_with_confidence`, `state_to_adjustment`. |
| `availability/db_provider.py` | `DBAvailabilityProvider` (queries `availability_events` at cutoff) and `DBNewsProvider`/`DBNewsSource` (DB-backed news provider). |
| `availability/minutes_integration.py` | `AvailabilityAwareMinutesModel` — confidence-weighted blend of base `MinutesModel` output with availability factors. Does not overwrite model probabilities with arbitrary constants. |
| `availability/prediction_wrapper.py` | `AvailabilityAwarePredictionProvider` — wraps `DecisionPredictionProvider`, adjusts start probability, expected minutes, expected points, distribution, floor/ceiling. |
| `availability/evaluation.py` | `Phase7EvaluationResult` + `evaluate_phase7()` — real-data baseline vs Phase 7 comparison via `DecisionBacktester`. Raises without a populated DB (no fabrication). |

## Database Schema (9 Phase 7 tables)

| Table | Purpose |
|-------|---------|
| `availability_sources` | News/availability sources with reliability tier and official-club flag. |
| `availability_articles` | Raw news articles; provenance fields (`source_id`, `url`, `published_at`, `scraped_at`, `ingested_at`). |
| `availability_evidence` | Individual evidence items; composite unique `(player, gameweek, type, valid_from)`; never-overwrite semantics. |
| `availability_events` | Corroborated aggregate events; `is_current`, `valid_from`/`valid_to`, composite indexes for temporal queries. |
| `player_injuries` | Structured injury records with `started_at`, `expected/actual_return_at`. |
| `player_suspensions` | Suspension records with `gameweek_count`, `returns_at`. |
| `training_reports` | Training participation with `session_at`, `participated`, `training_load`, `limited`. |
| `press_conferences` | Structured press conference transcripts (`held_at`, `recorded_at`). |
| `player_mentions` | Player mentions within press conferences; composite unique `(press_conference_id, player_id)`. |

Migration: `0006_phase7_availability` (revises `0005_phase4_prediction_models`), with downgrade support.

## Provider Architecture

SOURCE → RAW DATA → EVIDENCE → EVENT → CONFIDENCE → AVAILABILITY STATE → MODEL UPDATE

- `NewsSource` abstracts a single data source (reliability tier, `fetch_articles`).
- `NewsProvider` aggregates multiple `NewsSource` instances.
- `AvailabilityProvider` is the interface the prediction layer consults for per-player availability at a cutoff time (with batch support and training-limited queries).
- `DBAvailabilityProvider` implements `AvailabilityProvider` against `availability_events`.
- `AvailabilityAwareMinutesModel` and `AvailabilityAwarePredictionProvider` consume the provider to adjust predictions via confidence-weighted blending.

## Evidence Flow

```
SOURCE
  └─ fetch raw content (articles, transcripts, structured APIs)
    └─ RAW DATA (RawEvidence: player, source, reliability, evidence_type, status_mentioned)
      └─ EVIDENCE (availability_evidence rows, immutable, never-overwritten)
        └─ EVENT (corroborated availability_events; evidence_count, primary_source)
          └─ CONFIDENCE (aggregated via diminishing returns + official boost)
            └─ AVAILABILITY STATE (status resolved at query time)
              └─ MODEL UPDATE (confidence-weighted blend into minutes/points/distribution)
```

## Temporal Integrity

- **Cutoff handling:** `DBAvailabilityProvider.get_availability_batch` filters events with `valid_from <= game_time` and orders by `valid_from` desc (most recent available at the cutoff). Events published after the cutoff are excluded — no look-ahead.
- `is_training_limited` filters `TrainingReport.session_at <= cutoff`.
- Every record carries ingested/available timestamps. Historical records are never overwritten; new evidence inserts new rows.
- `evaluate_phase7` enforces the holdout policy via `enforce_holdout` when `seasons_split` is provided, and the unit test verifies future availability events are excluded by cutoff.

## Testing

- **Phase 7 unit tests:** `tests/unit/test_phase7_availability.py` — **56 tests pass.** Covers evidence corroboration (single/multiple/conflicting sources, official boost, diminishing returns, low confidence, duplicates), state derivation (all statuses), `state_to_adjustment`, `AvailabilityAwareMinutesModel` (confidence 0/1/0.5, missing availability, contradictory evidence), `AvailabilityAwarePredictionProvider` (bounds, ordering, floor/ceiling), DB providers (filtering, temporal, cutoff), source resolution (`_sources_for_event`: present / multiple fallback / no source / historical filtering), real metrics (Brier, log loss, MAE/RMSE, 60+ calibration, ECE, Spearman), coverage + temporal audits, and `evaluate_phase7` (miniature deterministic fixture, holdout policy, baseline vs Phase 7 distinguishable).
- **Full suite:** `pytest -q` → **343 passed** (287 pre-existing + 56 new Phase 7), 0 failed, 0 skipped.
- **Integration:** `tests/integration/test_postgresql.py` passes against the running Docker PostgreSQL.

## Code Quality

- **Ruff** on `src/fpl_intelligence/availability/` + `tests/unit/test_phase7_availability.py`: **All checks passed** (0 new errors).
- **mypy** on `src/fpl_intelligence/availability/` (9 files): **0 issues**.
- **mypy** on `optimization/provider.py`, `optimization/backtesting.py`: **0 issues**.
- **mypy** on `prediction/minutes.py`: **0 issues** (excluding pre-existing `import-untyped` warnings for sklearn/joblib, unrelated to Phase 7).

## PostgreSQL Validation

- Docker PostgreSQL running and healthy.
- `alembic upgrade head` executed all 6 migrations against PostgreSQL (exit 0), alembic version = `0006_phase7_availability`.
- Verified all 9 Phase 7 tables exist with correct columns, foreign keys, unique constraints, and indexes.

## Known Limitations

1. **Empirical value BLOCKED by data availability.** The real FPL provider
   (vaastav mirror) supplies only structured FPL stats. The Phase 7
   availability tables are empty after real import, so BASELINE ≡ PHASE7 and
   the empirical experiment is **BLOCKED — INSUFFICIENT HISTORICAL AVAILABILITY
   DATA** (NOT class A). A historical availability/injury/news dataset must be
   acquired and ingested before value can be classified (B or C).
2. **Status→start-probability / minutes-factor calibration** is derived from historical FPL data (2022-23 through 2024-25, excluding the 2025-26 holdout). These are contractual configuration values asserted exactly in tests.
3. **`_sources_for_event` resolves single-source provenance only.** It reads
   `AvailabilityEvent.primary_source_id`. Multi-source provenance would require
   a future event↔evidence association migration. When no primary source is
   present it returns `[]` as an explicit no-source state (not fabricated).
4. **Metrics are `NOT_AVAILABLE` without predictions vs actuals.** The real
   metrics in `availability/metrics.py` return `None` (rendered
   `NOT_AVAILABLE`) when a metric cannot be computed; they never return `0.0`
   for "not calculated". `evaluate_phase7` no longer emits `0.0` placeholders.
5. **Temporal integrity** is enforced at the event `valid_from` cutoff level; a full timestamp-based news-ingestion pipeline (live fetching) is not yet wired to a production source.
