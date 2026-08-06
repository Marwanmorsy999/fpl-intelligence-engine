# Phase 7 — Empirical Validation Report

**Status:** BLOCKED — INSUFFICIENT HISTORICAL AVAILABILITY DATA
**Date:** 2026-08-05
**Phase 7 engineering:** COMPLETE (verified)
**Phase 7 empirical value:** NOT DETERMINED (data-availability gap, not a model deficiency)
**Production database verification:** VERIFIED (2026-08-06)

---

## Executive Summary

Phase 7 engineering is complete and verified (323 tests, 36 Phase 7 tests,
PostgreSQL integration, Alembic migration 0006/0007, Ruff and mypy clean). The
remaining objective is to determine whether availability/news intelligence
improves real out-of-sample FPL decisions.

**Production database verification (2026-08-06): VERIFIED.** The PostgreSQL
service was restored (`fpl-intelligence-engine-foundation-db-1`, postgres:16-alpine),
`alembic upgrade head` applied revision `0007_historical_availability`, and all
9 Phase 7 availability tables exist with **0 rows**. The production-path
availability importer confirms `fetched=2447, matched=0, unmatched=2447,
persisted=0` — no real availability rows and no seeded-harness contamination
in the production database.

This report documents the **empirical validation readiness** and the outcome of
the Phase 7.1 **historical availability data feasibility spike**.

The central finding is a **data-availability gap**, but it is more specific
than "the provider has no availability ingestion path". An availability adapter
**does** exist (`RealFPLAvailabilityProvider`, provider name `real_fpl_bootstrap`)
and it fetches **2,447 real rows** from the FPL mirror's `players_raw.csv`
(news/status/chance_of_playing/`news_added`). However:

1. **In the production validation path the 2,447 records resolve to ZERO players.**
   `run_phase7_validation.py` ingests canonical players under the provider name
   `real_fpl` (via `RealFPLProvider`), but the availability importer resolves
   players under `real_fpl_bootstrap`. `PlayerExternalId` is keyed by
   `(provider, provider_player_id)`, so the two distinct provider names never
   match and every availability record is UNMATCHED. Forensic resolver audit
   (`docs/phase7-2-forensic-audit.json`, Scenario A):
   `fetched=2447, matched=0, unmatched=2447, persisted=0`.
2. **Even if resolution succeeded, the source is temporally unfit for strict
   backtesting.** `players_raw.csv` is a **terminal season-end snapshot**; its
   `news_added` timestamp is a *look-ahead* signal for every in-season decision
   point, so it cannot be used as strict pre-deadline availability intelligence.

As a direct consequence, after importing the real historical seasons the Phase 7
availability tables (`availability_events`, `availability_evidence`,
`player_injuries`, `player_suspensions`, `training_reports`,
`press_conferences`, `player_mentions`, `availability_articles`,
`availability_sources`) are **empty** in the production path. With no
availability events, the `DBAvailabilityProvider` returns `UNKNOWN / confidence
0.0` for every player, both wrappers (`AvailabilityAwareMinutesModel`,
`AvailabilityAwarePredictionProvider`) pass predictions through unchanged, and
therefore **PHASE7 ≡ BASELINE**.

We will **NOT** report `PHASE7 == BASELINE` as a meaningful empirical result.
That would be a fabrication of "no improvement" (class **A**) from a mere lack
of data. The honest classification is:

> **BLOCKED — INSUFFICIENT HISTORICAL AVAILABILITY DATA**

This is a data pipeline gap, not a Phase 7 model deficiency. The evaluation
infrastructure has been built, tested, and is ready; it fails clearly rather
than silently reporting a degenerate comparison.

---

## 1. Dataset Coverage

The real historical FPL datasets (2022-23 through 2025-26) are imported and
verified. Coverage is for the **structured FPL mirror only**:

| Season | Fixtures | Players | Player-Gameweeks | Availability events |
|--------|----------|---------|------------------|---------------------|
| 2022-23 | Imported | Imported | Imported | **0** |
| 2023-24 | Imported | Imported | Imported | **0** |
| 2024-25 | Imported | Imported | Imported | **0** |
| 2025-26 (holdout) | Imported | Imported | Imported | **0** |

The structured FPL mirror contains no availability/news/intelligence dimension.

## 2. Availability Coverage

Per the `audit_availability_coverage` audit, for every season:

| Metric | Value |
|--------|-------|
| Total player-gameweeks | `n` (from `PlayerGameweekPerformance`) |
| Player-gameweeks with availability evidence | 0 |
| Coverage % | 0.0% |
| Availability events | 0 |
| Confirmed injuries | 0 |
| Training reports | 0 |
| Press conferences | 0 |
| News/evidence records | 0 |

Coverage is **not** assumed complete — it is measured and reported as zero.

## 3. Temporal Integrity

The `audit_temporal_availability` audit enforces the strict reproducibility
policy: an event is eligible only if its `valid_from` is well-defined and does
not exceed the gameweek deadline (or the season's latest deadline when no
gameweek is linked). With zero events imported, the audit reports:

| Metric | Value |
|--------|-------|
| Total events | 0 |
| Eligible events | 0 |
| Excluded future events | 0 |
| Missing-timestamp events | 0 |
| Excluded ambiguous events | 0 |

The temporal-eligibility logic is implemented and unit-tested, but there is no
event data to which it can be applied at this time.

## 4. Baseline Definition

The `BASELINE` is the Phase 6 decision engine over the quantitative prediction
provider with **no** Phase 7 availability/news intelligence. It is frozen and
is not modified during evaluation. Baseline configuration is defined in the
evaluation runner and holdout policy.

## 5. Phase 7 Definition

The `PHASE7` variant adds availability events, injury intelligence, suspension
information, training evidence, press-conference availability evidence, source
corroboration, and availability state via the confidence-weighted wrappers. The
only intended difference from `BASELINE` is the availability/news information.

Because no availability data is present, the two variants are effectively
identical in the current database.

## 6. Availability Metrics

The real metrics are implemented in `availability/metrics.py`:

- Start probability Brier score
- Start probability log loss
- Expected minutes MAE / RMSE
- 60+ minute probability calibration (Brier + ECE)

Each metric returns `None` (rendered `NOT_AVAILABLE`) when it cannot be
computed from available data. It **never** returns `0.0` for "not calculated".
With no predictions vs actuals available through the availability-gated path,
all values are `NOT_AVAILABLE`.

## 7. Prediction Metrics

Implemented in `availability/metrics.py`:

- Expected points MAE / RMSE
- Spearman rank correlation

These are computed from predicted vs actual FPL points. They are reported as
`NOT_AVAILABLE` until a valid availability dataset allows the PHASE7 path to
diverge from BASELINE.

## 8. Decision ROI

The `DecisionBacktester` computes real transfer/captain/hit ROI from the
populated database using actual historical points. With no availability data,
BASELINE and PHASE7 decisions are identical, so no ROI delta can be attributed
to Phase 7. Any such delta would be zero only because the data is absent — it
is **not** evidence of no effect.

## 9. Decision-Change Attribution

The `phase7_decision_changes` dataset (baseline decision, Phase 7 decision,
triggering event, source, confidence, expected impact, actual outcome) is
intended but cannot be populated: there are no triggering availability events.

## 10. Early-Information Value

Measuring value by time-to-deadline (>72h, 48–72h, 24–48h, 12–24h, 6–12h, <6h)
is not possible without availability events carrying valid timestamps.

## 11. Source Value

Measuring value by source type (official club, manager/press conference,
trusted journalist, secondary media) requires persisted sources tied to
events. None are present. No source-quality inference is made from zero events.

## 12. Conflict Value

Measuring conflict handling (no conflict, resolved conflict, unresolved
conflict) requires corroborated multi-source evidence. None is present.

## 13. Development Results

Not computable. The development seasons (2022-23, 2023-24, 2024-25) have no
availability events, so BASELINE vs PHASE7 cannot diverge.

## 14. Final Holdout Results

Not computable. The locked 2025-26 holdout has no availability events either.
The holdout policy is respected: no tuning is performed against holdout data.

## 15. Statistical Uncertainty

Bootstrap confidence intervals and per-gameweek paired comparisons are
specified for the comparison. They cannot be produced without a valid
availability dataset. No 0.1-point-scale claims are made.

## 16. Phase 7 Classification

Classification is **not** **A** (no measurable value). The correct label is:

> **BLOCKED — INSUFFICIENT HISTORICAL AVAILABILITY DATA**

We explicitly do **not** classify as **A** merely because no data was tested.
Neither **B** (prediction improvement) nor **C** (decision improvement) can be
established.

## 17. Known Limitations

1. **Production entity-resolution key mismatch (`real_fpl` vs
   `real_fpl_bootstrap`).** The real structured ingestion and the availability
   adapter register the same FPL `element` IDs under two different
   `PlayerExternalId.provider` values, so the availability importer resolves
   nothing in the production path. This must be fixed (aliased mapping or a
   shared provider key) before any real availability import can succeed.
2. **FPL `players_raw.csv` is a terminal season snapshot, not a news feed.**
   Its `news_added` timestamp is look-ahead contaminated and cannot support
   `STRICT_BACKTEST_SAFE` per-gameweek intelligence.
3. **Phase 7 tables are empty after real import (production path).** Baseline and
   Phase 7 are indistinguishable in the current database.
4. **`_sources_for_event` provenance is single-source.** The schema models a
   direct event→primary-source link; multi-source provenance would require a
   future event↔evidence association migration.
5. **Status→start-probability / minutes-factor calibration** is derived from
   historical FPL data (2022-23 through 2024-25) and asserted exactly in tests.
6. **A live news-ingestion pipeline is not yet wired** to a production source.

---

## 18. Forensic Re-Audit — Resolution of the "2,447 records" Contradiction

This re-audit (re-opening Phase 7.2 per the forensic mandate) reconciles the
two prior logs and the "REAL fetched 2,447 but all skipped" vs "availability
tables empty" contradiction.

### 18.1 What the 2,447 records are

- **Real or mock?** **Real.** Produced by `RealFPLAvailabilityProvider` from
  `data/raw/real_fpl/<season>/players/players_raw.csv`. `environments.real=2447`,
  `mock_events=0`.
- **Availability events or structured stats?** They are **availability** rows
  (news/status/chance_of_playing/`news_added`), emitted **one per player-season**
  (778+865+804 = 2,447 for 2022-23..2024-25). They are **not** the Phase 7
  structured prediction/pricing stats (which come from `RealFPLProvider` under
  `real_fpl`). They are **not** news articles.
- **Misclassified?** **Yes, temporally.** Previously labelled
  `STRICT_BACKTEST_SAFE`; they are in fact a terminal look-ahead snapshot
  (Section 17.2).

### 18.2 Root cause of the contradiction

Two logs, same 2,447 records, different DB seeding:

- `_phase72_real_import.txt` (`events_imported=2447, matched=2447`) comes from
  `run_phase72_import.py`, which **pre-seeds** `PlayerExternalId` under
  `real_fpl_bootstrap` before importing. Test/seed harness — not production.
- `_real_import.txt` (`events_imported=0, skipped=2447, unmatched=2447`) comes
  from the **production** `run_phase7_validation.py`, which only seeds
  `real_fpl`. The availability importer resolves under `real_fpl_bootstrap` →
  no match.

The contradiction is fully explained: the "successful" log is a seeding
artefact; the "failed" log is the faithful production outcome.

### 18.3 Full accounting (no silent loss)

Both forensic scenarios satisfy record conservation
`fetched == persisted + failed_persist + normalization_failed +
skipped_duplicate + skipped_invalid + skipped_temporal_invalid + ambiguous +
unmatched`:

- **Production path:** `fetched=2447, matched=0, unmatched=2447, persisted=0`
  (`conservation_ok=true`). 9 availability tables all **0**.
- **Seeded harness:** `fetched=2447, matched=2447, skipped_temporal_invalid=169,
  persisted=2278`. The 5 structured derived tables (`player_injuries`,
  `player_suspensions`, `training_reports`, `press_conferences`,
  `player_mentions`) remain **0** — `players_raw.csv` has no such columns.

Unresolved raw records persisted to
`audit/raw/phase72_scenarioA_unmatched_raw.json`.

### 18.4 BLOCKED support checklist (forensic mandate)

| Required BLOCKED support | Status |
|---|---|
| Zero real availability rows (production) | ✅ `availability_events=0` |
| Full accounting of 2,447 records | ✅ conservation_ok=true; none silently dropped |
| Explicit evidence provider lacks historical availability data | ✅ terminal snapshot + look-ahead + production key mismatch |
| No silent entity-resolution failure | ✅ explicit `matched=0, unmatched=2447` |

The BLOCKED conclusion is **supported**. Phase 7 is **not** classified A/B/C.

---

## Recommended Next Phase

Acquire and ingest a **historical availability/injury/news dataset** for the
development seasons, then re-run `run_phase7_validation.py`. The evaluation
infrastructure — real metrics, coverage and temporal audits, BLOCKED fail-fast
gate, and the BASELINE vs PHASE7 decision backtest — is complete and ready.
Only then can Phase 7 value be classified (B or C) on real out-of-sample data.

Do not start Phase 8 until this data gap is resolved.
