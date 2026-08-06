# Phase 7.2 — Historical Availability Coverage Report

**Status:** SOURCE AUDIT / DATA ACQUISITION / INTEGRATED — **RE-OPENED & FORENSICALLY CORRECTED**
**Imported source:** `real_fpl_bootstrap` (FPL bootstrap availability, real)

> This report is generated from the persisted real import result
> (`docs/phase7-2-import-result.json`) produced by
> `run_phase72_import --provider real_fpl_bootstrap`. Event coverage and
> strict-safe coverage are reported **separately**, per the Phase 7.2 design.

> **⚠ FORENSIC RE-AUDIT — READ FIRST.** The figures below were produced by
> `run_phase72_import.py`, which **pre-seeds** canonical players under the
> provider name `real_fpl_bootstrap`. The **production** validation path
> (`run_phase7_validation.py`) does **not** seed that provider name, so in
> production **every** one of these 2,447 records is *unmatched* and
> `availability_events = 0`. Furthermore, the "strict-safe" classification is
> **invalid for backtesting**: `players_raw.csv` is a terminal season-end
> snapshot, so its `news_added` timestamp is a look-ahead signal. See §9. The
> "PARTIALLY TESTABLE" conclusion in §6/§8 is therefore **revoked** — Phase 7 is
> **BLOCKED** (see `docs/phase7-empirical-validation-report.md`).

---

## 1. Summary

| Metric | Value |
|---|---|
| Provider | `real_fpl_bootstrap` |
| Seasons imported | 2022-23, 2023-24, 2024-25 |
| Total events | **2,447** |
| Strict-backtest-safe events | **1,852** |
| Historical-event-only events | 0 |
| Unknown temporal events | 595 |
| Eligible-before-cutoff events | 0 (strict eligibility requires the Phase 7 deadline model; see Section 5) |
| Events skipped | 0 |
| Mock events | 0 |
| Environments | `real`: 2,447 |

---

## 2. Entity Resolution

> **Production path vs seeded harness.** The "Matched = 2,447" below is from the
> seeded `run_phase72_import.py` harness. In the production validation path
> (`run_phase7_validation.py`) the resolver audit is `matched=0, unmatched=2447`
> because `PlayerExternalId` is keyed by provider name and the production path
> only registers the `real_fpl` provider, not `real_fpl_bootstrap`.

| Outcome | Seeded harness (`run_phase72_import`) | Production path (`run_phase7_validation`) |
|---|---|---|
| Matched players | 2,447 | **0** |
| Unmatched players | 0 | **2,447** |
| Ambiguous players | 0 | 0 |
| Manual overrides | 0 | 0 |

In the seeded harness, all 2,447 events resolved to canonical players by
provider player ID (team + season context as supporting evidence). In the
production path, **no** event resolves (provider-name key mismatch) — no
unresolved entity is silently dropped; the failure is explicit and auditable.

---

## 3. Per-Season Coverage

| Season | Total events | Strict-safe | Event-only | Unknown | Unique players | Missing timestamps |
|---|---|---|---|---|---|---|
| 2022-23 | 778 | 592 | 0 | 186 | 778 | 0 |
| 2023-24 | 865 | 657 | 0 | 208 | 865 | 0 |
| 2024-25 | 804 | 603 | 0 | 201 | 804 | 0 |

### Articles / evidence / sources

| Season | Articles | Evidence records |
|---|---|---|
| All seasons | 2,447 | 2,447 |

The canonical pipeline (source → article → evidence → event) is now complete:
**2,447 evidence records** are persisted alongside the 2,447 events. Each event
carries a `provider`, `provider_event_id`, and `temporal_class`.

### Structured records

| Season | Injuries | Suspensions | Training reports | Press conferences | Player mentions |
|---|---|---|---|---|---|
| 2022-23 | 0 | 0 | 0 | 0 | 0 |
| 2023-24 | 0 | 0 | 0 | 0 | 0 |
| 2024-25 | 0 | 0 | 0 | 0 | 0 |

The FPL bootstrap source provides availability status / chance-of-playing
(Layer B surrogate) but **not** structured injury/suspension/training/press
records. This is a documented source limitation, not an omission.

---

## 4. Strict-Safe Coverage (per season)

| Season | Strict-safe events | Total events | Strict-safe coverage % |
|---|---|---|---|
| 2022-23 | 592 | 778 | **76.1%** |
| 2023-24 | 657 | 865 | **76.0%** |
| 2024-25 | 603 | 804 | **75.0%** |
| **Total** | **1,852** | **2,447** | **75.7%** |

Strict-safe coverage is the fraction of events with a `temporal_class` of
`STRICT_BACKTEST_SAFE` — i.e. events for which the availability/publication time
can be reconstructed and compared against the decision deadline.

> **REVOKED for backtesting.** The 1,852 "strict-safe" events are an artefact of
> the prior temporal rule that treated any `news_added` timestamp as
> `STRICT_BACKTEST_SAFE`. Because `players_raw.csv` is a **terminal season-end
> snapshot**, each `news_added` is the *last* news for that player at season end
> and is therefore a **look-ahead** signal for every in-season decision point.
> The forensic importer now rejects 169 out-of-window timestamps as
> `skipped_temporal_invalid`; the remaining 2,278 persisted events are still
> look-ahead contaminated and must **not** be used as strict pre-deadline
> intelligence. The 1,852 figure is retained for provenance only.

---

## 5. Data Quality

| Check | Result |
|---|---|
| Duplicate events | 0 |
| Impossible dates | 0 |
| Player mismatch | 0 |
| Team mismatch | 0 |
| Duplicate articles | 0 |
| Duplicate evidence | 0 |
| Conflicting event states | 0 |
| Missing timestamps | 0 |
| Future events | 0 |
| Events outside season bounds | 0 |
| **Total issues** | **0** |

No data-quality issues were raised. Ambiguous data is never silently corrected;
it would be surfaced as issues.

---

## 6. Evaluation Readiness

Per the Phase 7.2 evaluation-readiness gate
(`docs/phase7-2-evaluation-gate.json`):

- **Phase 7 empirical:** `PARTIALLY TESTABLE`
- **Classification:** `PARTIALLY_TESTABLE`
- **Per-season strict-safe coverage above the 10% threshold:** yes (76%, 76%,
  75%)
- **Holdout (2025-26):** isolated; not used for tuning or strict-safe coverage.

The dataset is **NOT** sufficiently populated or temporally valid in the
production path to make a real BASELINE vs PHASE7 comparison meaningful:
in production `availability_events = 0`, and even in the seeded harness the
persisted events are look-ahead contaminated and unfit as strict pre-deadline
intelligence. Because the source is a Layer B surrogate (availability status /
chance-of-playing) rather than full press-conference/training/news coverage, and
because the production path resolves nothing, the verdict is **BLOCKED**, not
PARTIALLY TESTABLE. See `docs/phase7-empirical-validation-report.md`.

---

## 7. Event vs Strict-Safe Coverage (separated)

- **Event coverage:** 2,447 availability events across 3 development seasons.
- **Strict-safe coverage:** 1,852 events (75.7%) eligible for strict
  pre-deadline use.

These are reported separately and are **not** conflated. The 1,852 strict-safe
events are retained for provenance only and are **not** usable as strict
pre-deadline intelligence (look-ahead contamination — see §4 banner).

---

## 8. Conclusion

The real FPL bootstrap availability dataset is integrated **only** in the
seeded `run_phase72_import.py` harness:

- seeded harness: 2,447 events persisted (2,278 after removing 169
  out-of-window timestamps), 0 unresolved entities, 0 data-quality issues,
  reproducible from cached raw files, clear source/licensing provenance.
- **production path:** `availability_events = 0` (all 2,447 unmatched due to the
  `real_fpl` vs `real_fpl_bootstrap` provider-key mismatch).

This does **not** make Phase 7 testable. Phase 7 is **BLOCKED** — see
`docs/phase7-empirical-validation-report.md` and §9.

---

## 9. Forensic Re-Audit — 9-table Row Counts & Resolver Audit

The forensic runner (`fpl_intelligence/scripts/forensic_phase72_audit.py`)
reconstructed both paths and recorded the 9 availability-related table counts:

**SCENARIO A — production path** (`run_phase7_validation.py` → `real_fpl`
ingest → `real_fpl_bootstrap` import):

| Availability table | Row count |
|---|---|
| availability_sources | 0 |
| availability_articles | 0 |
| availability_evidence | 0 |
| availability_events | **0** |
| player_injuries | 0 |
| player_suspensions | 0 |
| training_reports | 0 |
| press_conferences | 0 |
| player_mentions | 0 |

Resolver audit (conservation `fetched == Σ terminal`, `conservation_ok=true`):
`fetched=2447, normalized=2447, matched=0, unmatched=2447, persisted=0,
failed_persist=0, normalization_failed=0, skipped_duplicate=0,
skipped_invalid=0, skipped_temporal_invalid=0, ambiguous=0`.

**SCENARIO B — seeded harness** (`run_phase72_import.py`):
`fetched=2447, matched=2447, skipped_temporal_invalid=169, persisted=2278`;
`availability_events = 2278` but the 5 structured derived tables remain **0**
(no injury/suspension/training/press/mention columns exist in `players_raw.csv`).

**Live PostgreSQL verification** (2026-08-06). PostgreSQL is reachable
(alembic revision `0007_historical_availability`), all 9 Phase 7
availability tables exist, and **all contain 0 rows**. The production
path yields `fetched=2447, matched=0, unmatched=2447, persisted=0`.
The earlier forensic audit observation that the live DB carried zero
real availability rows is confirmed and strengthened by the live
verification.

Unresolved raw records are persisted to
`audit/raw/phase72_scenarioA_unmatched_raw.json` (2,447 entries) and the
seeded-run receipt to `audit/raw/phase72_scenarioB_persisted_raw.json`.

**BLOCKED support check (per the forensic mandate):**
1. zero real availability rows in production ✅
2. full accounting of 2,447 records (conservation_ok=true, none silently
   dropped) ✅
3. explicit evidence the provider lacks historical availability data usable for
   strict backtesting (terminal snapshot + look-ahead + production key mismatch)
   ✅
4. no silent entity-resolution failure (explicit `matched=0, unmatched=2447`)
   ✅
