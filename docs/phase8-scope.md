# Phase 8.0 — Tactical Intelligence Scope and Source Audit

**Phase:** 8.0  
**Status:** SCOPE / SOURCE AUDIT ONLY — NO model implementation, NO training, NO holdout evaluation  
**Date started:** 2026-08-06  
**Current time:** 2026-08-06T10:07:49+03:00  
**Authorized scope:** Investigate sources; produce scope, source-audit, and temporal-feasibility documents.  

---

## 1. Mandate and Non-Negotiables

Phase 8 is authorized **only** for scope and source audit. The following are
**strictly forbidden** in this phase:

- Do **not** implement tactical models.
- Do **not** modify the Phase 4/5/6 prediction or optimization stack.
- Do **not** start model training.
- Do **not** evaluate the locked 2025/26 holdout.
- Do **not** fabricate tactical coverage.
- Do **not** assign Phase 8 any A/B/C classification.
- Do **not** use mock data as real validation evidence.

## 2. Objective

Determine whether tactical intelligence can be **historically acquired**,
**normalized**, **temporally validated**, and **empirically tested**.

The central question: *Is there enough real, pre-deadline, historically-available
tactical data to support a strict no-look-ahead backtest of tactical signals
against FPL decision outcomes?*

## 3. Phase 8 Signal Inventory (13 signals)

| # | Signal | Definition |
|---|--------|------------|
| 1 | Starting lineups | Confirmed or predicted starting XI per team per fixture |
| 2 | Formations | Team tactical shape (e.g., 4-3-3, 3-5-2) per match |
| 3 | Player positions | On-pitch position / role within formation (e.g., AM, DM, RW) |
| 4 | Player role changes | Position or role shifts (e.g., fullback → CB, 8 → 10) |
| 5 | Positional role context | Role within tactical system (e.g., inverted fullback, Box-to-box midfielder) |
| 6 | Set-piece takers — penalties | Player designated to take penalties |
| 7 | Set-piece takers — free kicks | Player designated for direct/indirect free kicks |
| 8 | Set-piece takers — corners | Player designated to take corners |
| 9 | Manager changes | Date a new manager takes charge of a team |
| 10 | Manager formation tendencies | Preferred formation(s) of the current manager |
| 11 | Manager rotation tendencies | Manager's historical lineup rotation frequency |
| 12 | Team style indicators | Offensive/defensive shape (e.g., high press, low block, possession-based) |
| 13 | Opponent tactical matchup context | How a team's style matches up against an opponent's style |
| 14 | Minutes risk due to role changes | Expected minute reduction from positional/tactical shifts |
| 15 | Differential signals from tactical shifts | Points leverage from tactical information not yet priced in |

## 4. Phase 7 Status (Preserved)

Phase 7 is closed as **engineering-complete but empirically blocked**.

- **Implementation:** 100% (engineering verified)
- **Automated Testing:** 100% (56 Phase 7 tests; 323 total)
- **Real-Data Validation:** 0% (BLOCKED — insufficient historical availability data)
- **Final Holdout:** N/A (still isolated)
- **Status:** ENGINEERING_COMPLETE / BLOCKED (INSUFFICIENT HISTORICAL AVAILABILITY DATA)
- **Classification:** Not A, B, or C — **BLOCKED** (data-availability gap, not a model deficiency)

Phase 7 status is preserved verbatim in `docs/PROJECT_STATUS.md`.

## 5. Phase 8 Classification

Phase 8 is **not classified** A/B/C. It is a **scope and source audit** only.
Any Phase 8 signal that cannot be historically acquired and temporally validated
is marked as **engineering-only** (requires live data accumulation). Any signal
that can be empirically tested with current data is marked as **empirically
testable**.

## 6. Phase 7 Lessons Applied to Phase 8

Phase 7's empirical blockage was caused by:

1. **Provider-key mismatch** (`real_fpl` vs `real_fpl_bootstrap`) — the
   availability importer could not resolve players in the production path.
2. **Terminal season-end snapshots** — `players_raw.csv`'s `news_added` is a
   look-ahead signal, not pre-deadline availability intelligence.
3. **No live news pipeline** — a live ingestion pipeline was not wired to a
   production source.

Phase 8 inherits all three constraints. Any candidate source must pass:

- **Entity resolution:** the player/team IDs must map to the existing canonical
  database (or the source must be wired through the existing provider-key
  system without introducing a divergent key).
- **Temporal integrity:** timestamps must represent **pre-deadline** information
  availability, not terminal snapshots or outcomes.
- **Reproducibility:** the raw data must be cached and re-importable.
- **Honest classification:** sources are categorized as
  `STRICT_BACKTEST_SAFE`, `HISTORICAL_EVENT_ONLY`, `LIVE_ONLY`, or
  `UNSAFE_LOOKAHEAD`.

## 7. Temporal Classification Framework

Per `docs/temporal-integrity.md`, four temporal fields are tracked:

| Field | Description |
|-------|-------------|
| `event_time` | When the football/availability event occurred (match kickoff, news published, etc.) |
| `published_at` | When the source published the information |
| `available_at` | Earliest timestamp at which our system can legitimately be considered to have accessed the information |
| `ingested_at` | When our pipeline actually collected the data |

Three information-access policies govern eligibility:

| Policy | Condition | Use case |
|--------|-----------|----------|
| `PUBLIC_AVAILABILITY` | `available_at <= cutoff` | Idealized system accessing all public info |
| `SYSTEM_AVAILABILITY` | `ingested_at <= cutoff` | Actual system with pipeline delays |
| `STRICT_REPRODUCIBILITY` (default) | `available_at <= cutoff AND ingested_at <= cutoff` | Conservative, reproducible backtesting |

### Phase 8 Temporal Categories

| Category | Definition | Backtest-safe? |
|----------|------------|----------------|
| `LIVE_ONLY` | Data only available during/after a live match (e.g., live lineups published 60 min before kickoff) | No — no historical archive |
| `HISTORICAL_DATA` | Pre-deadline or match-time data with publication timestamps, from a cached historical archive | Yes, if `published_at <= deadline` |
| `DELAYED_DATA` | Data published after the fixture (e.g., Opta match reports 24h later) | No — look-ahead for in-season decisions |
| `UNSAFE_LOOKAHEAD` | Data only available as terminal season-end snapshots (look-ahead contaminated) | No — e.g., `players_raw.csv` `news_added` |
| `MOCK_ENGINEERING_ONLY` | Synthetic data generated for testing the pipeline | No — never used as validation evidence |

## 8. Deliverables

This phase produces exactly three documents and one status update:

1. `docs/phase8-scope.md` — This document.
2. `docs/phase8-source-audit.md` — Per-source audit with all required fields.
3. `docs/phase8-temporal-feasibility.md` — Per-signal temporal feasibility.
4. `docs/PROJECT_STATUS.md` — Updated with Phase 8 entry (scope audit only).

## 9. Out of Scope

- Implementing tactical models (signal 1–15).
- Wiring new data sources into production ingestion.
- Running any backtest or evaluation.
- Modifying Phase 4/5/6/7 code or schemas.
- Evaluating the 2025/26 holdout.
- Classifying Phase 8 as A/B/C.
- Producing mock data as real evidence.
