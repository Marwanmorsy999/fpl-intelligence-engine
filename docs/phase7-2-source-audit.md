# Phase 7.2 — Historical Availability Data Source Audit

**Date:** Phase 7.2
**Status:** SOURCE AUDIT / DATA ACQUISITION / INTEGRATED

> This document records the feasibility audit of candidate historical
> availability/news/injury data sources for Phase 7, the selection decision,
> the temporal classification of each candidate, and the licensing, coverage,
> and reproducibility notes required to use the data honestly.

---

## 1. Objective

Phase 7 availability intelligence requires a **historical** dataset that lets
the engine reconstruct, per player and per gameweek, when availability
information became available **before** the decision deadline. The priority is
not maximum volume but:

1. temporal integrity
2. historical coverage
3. source reliability
4. reproducibility
5. legal/usage suitability
6. entity resolution

---

## 2. Candidate Sources

### 2.1 FPL bootstrap availability (`players_raw.csv`) — SELECTED

| Attribute | Value |
|---|---|
| Provider | vaastav/Fantasy-Premier-League mirror (open GitHub) |
| Source URL | `https://github.com/vaastav/Fantasy-Premier-League` |
| Data ownership/licensing | Public GitHub repository of scraped FPL history. Verify upstream licensing before any commercial use. |
| Seasons covered | 2022-23, 2023-24, 2024-25 (development); 2025-26 (locked holdout, isolated) |
| Premier League coverage | Yes (all 20 teams per season) |
| Player coverage | All FPL players per season (~650–800) |
| Event types | Availability status, chance-of-playing, news blurb (injury/suspension/illness signals) |
| Injury coverage | Partial — presence/absence implied by status + chance-of-playing |
| Suspension coverage | Partial — implied by status/news |
| Training coverage | None directly |
| Press-conference coverage | None directly |
| Historical publication timestamp availability | **No** — `players_raw.csv` is a **terminal season-end snapshot**. `news_added` is populated for almost all rows but reflects the *last* known news at season end (max dates fall in May, the season-final month; `sum(minutes)` ≈ 749k/season confirms full-season cumulative stats). It is NOT a pre-deadline feed. |
| Event timestamp availability | Only the terminal snapshot timestamp; no per-gameweek news history exists in the file. |
| Reproducibility | Yes — cached raw file under `data/raw/real_fpl/<season>/players/players_raw.csv` |
| Access method | Public HTTP (raw.githubusercontent.com) via `DiskCachingFetcher` |
| Rate-limit/access constraints | GitHub raw; cached locally; no significant rate limit for these files |

**Temporal classification:** Previously asserted as `STRICT_BACKTEST_SAFE` for
the reconstructed availability events. The Phase 7.2 forensic re-audit
(`docs/phase7-2-forensic-audit.json`) **reverses this**: `players_raw.csv` is a
**terminal season snapshot**, not a pre-deadline news feed. Every row carries
the *last* news value for that player at season end, so its `news_added`
timestamp is a **look-ahead signal** relative to every in-season decision point.
It therefore does **not** support `STRICT_BACKTEST_SAFE` per-gameweek
intelligence and cannot back the BASELINE-vs-PHASE7 experiment. See §9.

**Why selected:** It is the only source that is already wired into the
existing ingestion path, is real (not synthetic), is reproducible (cached raw
files), has a clear provenance/licensing note, and provides publication/event
timestamps usable for strict eligibility. It is the honest first real source
for Phase 7.2.

**Known limitation (documented, not hidden):** The FPL bootstrap source is a
**proxy** for availability news. It does **not** provide the full Layer B
press-conference / training / journalist-report signal set. As such the
imported dataset supports **partial** empirical testing (`PARTIALLY TESTABLE`),
not a full A/B/C verdict. This is stated explicitly in the coverage report and
PROJECT_STATUS.

---

### 2.2 Transfermarkt-derived injury/absence data — AUDITED, NOT WIRED

| Attribute | Value |
|---|---|
| Provider | Transfermarkt (web) |
| Source URL | `https://www.transfermarkt.com` |
| Data ownership/licensing | Scraped third-party data; commercial use restricted; requires careful licensing review |
| Seasons covered | Broad (many years) |
| Premier League coverage | Good |
| Player coverage | Good |
| Event types | Injury, illness, return dates |
| Injury coverage | Good (structured injury records) |
| Suspension coverage | Partial |
| Training coverage | None |
| Press-conference coverage | None |
| Publication timestamp availability | Weak — exact publication/availability time often not recorded → HISTORICAL_EVENT_ONLY |
| Reproducibility | Moderate — scraping requires rate-limit handling and ToS compliance |
| Access method | Scraping (not a clean open dataset) |
| Rate-limit/access constraints | Aggressive rate limiting; ToS concerns |

**Temporal classification:** `HISTORICAL_EVENT_ONLY` unless a provider-specific
publication timestamp can be established. Not usable as strict pre-deadline
intelligence without that timing.

**Decision:** Not wired in this phase. Would require a dedicated scraping
adapter, licensing review, and timestamp reconstruction before use.

---

### 2.3 Public historical injury datasets — AUDITED, NOT WIRED

| Attribute | Value |
|---|---|
| Provider | Various public GitHub / Kaggle football injury datasets |
| Data ownership/licensing | Varies; must verify per dataset |
| Seasons covered | Varies |
| Premier League coverage | Varies |
| Player coverage | Varies |
| Event types | Injury, absence, return |
| Injury coverage | Variable |
| Publication timestamp availability | Usually absent → HISTORICAL_EVENT_ONLY / OUTCOME_ONLY |
| Reproducibility | Depends on hosting |

**Temporal classification:** `HISTORICAL_EVENT_ONLY` / `OUTCOME_ONLY` in most
cases. Usable for exploratory analysis or outcome labeling, **not** strict
pre-deadline features.

**Decision:** Not wired in this phase. No single dataset with reliable
publication timing was identified.

---

### 2.4 Public club / press-conference archives — AUDITED, NOT WIRED

| Attribute | Value |
|---|---|
| Provider | Club official sites, press-conference transcripts |
| Data ownership/licensing | Club-owned content; licensing/ToS restrictions |
| Publication timestamp availability | Good (article publication times) |
| Reproducibility | Hard — no clean bulk export; per-club scraping required |
| Rate-limit/access constraints | Varies; ToS concerns |

**Temporal classification:** Would be `STRICT_BACKTEST_SAFE` if publication
timestamps were preserved, but no clean historical archive of all clubs across
all seasons was available.

**Decision:** Not wired in this phase. A future `ClubArchiveAvailabilityProvider`
/ `PressConferenceArchiveProvider` remains an extension point.

---

### 2.5 Curated football analytics repositories — AUDITED, NOT WIRED

| Attribute | Value |
|---|---|
| Provider | Various analytics repos / newsletters |
| Data ownership/licensing | Varies; often subscription-gated |
| Publication timestamp availability | Varies |
| Reproducibility | Varies |

**Temporal classification:** Depends on the specific dataset's provenance.

**Decision:** No open, reproducible, license-suitable candidate was identified
for this phase.

---

## 3. Temporal Classification Summary

| Candidate | Classification | Usable as strict pre-deadline? |
|---|---|---|
| FPL bootstrap availability (real) | `STRICT_BACKTEST_SAFE` | Yes (selected) |
| Transfermarkt-derived injury data | `HISTORICAL_EVENT_ONLY` | No |
| Public injury datasets | `HISTORICAL_EVENT_ONLY` / `OUTCOME_ONLY` | No |
| Club / press-conference archives | `STRICT_BACKTEST_SAFE` (if timing preserved) | Not yet available |
| Curated analytics repositories | Varies | Not yet available |

---

## 4. Layer A vs Layer B

Per the Phase 7.2 design, the pipeline distinguishes:

- **Layer A — Event history** (injury occurred, player missed match, suspension
  occurred, player returned): used for outcome analysis, exploratory research,
  model targets.
- **Layer B — Information availability** (official announcement, manager
  statement, journalist report, article publication, public training report):
  used for strict historical backtesting and information-timing analysis.

The FPL bootstrap source provides a **Layer B surrogate** — the availability
status / chance-of-playing is a public, published signal carrying a
reconstruction of when it was available (`valid_from`). The two layers are
**not** conflated: the importer records `temporal_class` per event and only
`STRICT_BACKTEST_SAFE` events are eligible for strict pre-deadline use.

---

## 5. Entity Resolution

Source players are mapped to canonical entities by **provider player ID**
(the FPL `element` ID), never by name alone. Team context and season context
are used as supporting evidence. The importer's `HistoricalEntityResolver`
produces a reconciliation report with matched / unmatched / ambiguous / manual
mappings.

> **CRITICAL CORRECTION (forensic re-audit).** The figure "2,447 matched, 0
> unmatched" that previously appeared here was **not** produced by the production
> validation path. It came from `run_phase72_import.py`, which *pre-seeds*
> canonical players under the provider name **`real_fpl_bootstrap`** before
> importing. The production runner (`run_phase7_validation.py`) ingests the real
> FPL structured stats under the provider name **`real_fpl`** and then runs the
> availability importer against **`real_fpl_bootstrap`**. Because no
> `PlayerExternalId` rows exist under `real_fpl_bootstrap` in that path, **every
> one of the 2,447 availability records is UNMATCHED** (resolver audit:
> `fetched=2447, matched=0, unmatched=2447`). The two provider names refer to
> the same FPL `element` ID space but are distinct keys in `PlayerExternalId`,
> so the availability importer silently fails to resolve any player. See §9.

---

## 6. Source Reliability Metadata

Reliability is initialized as a **neutral prior** (no invented historical
accuracy scores). The selected source is registered under the official-FPL
bootstrap tier; its reliability is intended to be *learned later* from Phase 7
evaluation, not asserted now.

---

## 7. Licensing / Usage Status

- **FPL bootstrap (selected):** vaastav/Fantasy-Premier-League is a public
  GitHub repository of FPL historical data. **Verify upstream licensing before
  any commercial use.** Stored/used for research/evaluation in this engine.
- **Transfermarkt / club archives / analytics repos:** licensing/ToS review
  required before any use; not used in this phase.

---

## 8. Conclusion

The **real FPL bootstrap availability source** was selected and integrated as
the first honest historical availability dataset for Phase 7. It provides
temporal integrity (reconstructable availability time), historical coverage
(2022-23..2024-25 development + isolated 2025-26 holdout), source reliability
(neutral prior), reproducibility (cached raw files), and a clear licensing
note. It is a Layer B surrogate and therefore supports **partial** empirical
testing (`PARTIALLY TESTABLE`), not a full A/B/C verdict.

Remaining extension points (not wired in this phase): Transfermarkt-derived
injury data, public injury datasets, club/press-conference archives, curated
analytics repositories.

---

## 9. Forensic Re-Audit of the "2,447 records" (Phase 7.2 re-open)

This section resolves the contradiction in the prior project state:

- `_phase72_real_import.txt`: `events_imported=2447, events_skipped=0,
  resolution.matched_players=2447` (apparent SUCCESS).
- `_real_import.txt`: `events_imported=0, events_skipped=2447,
  resolution.unmatched_players=2447` (apparent FAILURE).

Both logs describe the **same 2,447 records** produced by
`RealFPLAvailabilityProvider.fetch_events()`, which emits **one row per
`players_raw.csv` player per season** (778 + 865 + 804 = 2,447 for
2022-23..2024-25). They are **REAL** rows from the public vaastav mirror — not
mock, not fabricated, not news articles. They are, however, **player-season
snapshot rows**, not discrete availability *events*: only 291/778, 317/865,
243/804 rows in each season actually carry a `news` blurb; the rest are the
default status `a` (available) with no incident.

### 9.1 What the 2,447 records actually are

| Question | Forensic answer |
|---|---|
| Real or mock? | **Real** — sourced from `data/raw/real_fpl/<season>/players/players_raw.csv`. `environments.real = 2447`, `mock_events = 0`. |
| Availability events or structured stats? | They come from the **availability** provider (`real_fpl_bootstrap`), derived from the `news`/`status`/`chance_of_playing_*`/`news_added` columns. They are **not** the Phase 7 structured prediction/pricing stats (those are produced by `RealFPLProvider` = `real_fpl`). |
| Misclassified? | **Yes, by the temporal layer.** The prior audit labelled them `STRICT_BACKTEST_SAFE`. Because `players_raw.csv` is a terminal season-end snapshot, the `news_added` timestamp is a **look-ahead** signal and must not be used as strict pre-deadline intelligence. |

### 9.2 Root cause of the contradiction

`PlayerExternalId` is keyed by `(provider, provider_player_id)`. The production
runner `run_phase7_validation.py` ingests canonical players under
`provider = "real_fpl"` (via `RealFPLProvider`). The availability importer
resolves players under `provider = "real_fpl_bootstrap"`
(`RealFPLAvailabilityProvider`). These are **distinct keys** over the same FPL
`element` IDs. The production path therefore registers **zero**
`real_fpl_bootstrap` external IDs, so:

```
resolver_audit (SCENARIO A — production path)
  fetched=2447  normalized=2447  matched=0
  unmatched=2447  persisted=0  conservation_ok=true
```

The "2,447 matched" log came from `run_phase72_import.py`, which **pre-seeds**
`PlayerExternalId` rows under `real_fpl_bootstrap` (and aliases `real_fpl` and
`sample_historical_availability`) in `_seed_canonical()` **before** importing.
That is a *test/seed harness*, not the production ingestion path. With seeding,
all 2,447 resolve — but this masks the production defect.

### 9.3 Resolver-audit accounting (all 2,447 accounted for)

- **SCENARIO A — production path** (`run_phase7_validation.py`): every record
  lands in `unmatched` (provider-name mismatch). `availability_events = 0`.
- **SCENARIO B — seeded harness** (`run_phase72_import.py`): `matched=2447`,
  `persisted=2278`, `skipped_temporal_invalid=169`. The 169 "temporal invalid"
  are rows whose `news_added` falls **outside** the canonical season window
  (e.g. pre-season July dates or season-final May dates captured for the
  *next/previous* season). The 2,278 persisted events are still **look-ahead
  contaminated** (terminal snapshot), so even this run yields no valid strict
  backtest signal — and the derived tables `player_injuries`,
  `player_suspensions`, `training_reports`, `press_conferences`,
  `player_mentions` remain **0** because `players_raw.csv` carries no such
  columns.

The forensic runner
(`fpl_intelligence/scripts/forensic_phase72_audit.py`) enforces record
conservation `fetched == persisted + failed_persist + normalization_failed +
skipped_duplicate + skipped_invalid + skipped_temporal_invalid + ambiguous +
unmatched`; both scenarios report `conservation_ok = true`. No record is
silently dropped. Unresolved raw records are persisted to
`audit/raw/phase72_scenarioA_unmatched_raw.json`.

### 9.4 Why Phase 7 remains BLOCKED (not A/B/C)

1. **Zero real availability rows** in the production path
   (`availability_events = 0`). The live PostgreSQL `fpl` database was
   confirmed at forensic audit time to hold **0 public tables** (no
   availability schema populated); the validation runner uses SQLite
   `:memory:`, so even its state is never persisted. A subsequent
   production verification run (2026-08-06) confirmed PostgreSQL is
   reachable (revision `0007_historical_availability`), all 9 Phase 7
   availability tables exist, and **all contain 0 rows**. The production
   path yields `fetched=2447, matched=0, unmatched=2447, persisted=0`.
2. **Full accounting of 2,447** records exists (above) — none are missing or
   silently dropped.
3. **Explicit evidence the provider lacks historical availability data** usable
   for strict backtesting: `players_raw.csv` is a terminal snapshot with
   look-ahead timestamps, and the production entity-resolution key mismatch
   means the real ingestion produces no availability events at all.
4. **No silent entity-resolution failure** — the failure is explicit and
   auditable (`matched=0, unmatched=2447`, `conservation_ok=true`). The prior
   "SUCCESS" log was an artefact of a seed harness, not production.

Conclusion: the BLOCKED status is **supported**. The 2,447 records are real
player-season snapshot rows, but they are (a) not resolved in the production
path and (b) temporally unsuitable as strict pre-deadline availability
intelligence. Phase 7 cannot proceed to a BASELINE-vs-PHASE7 experiment.

**Production database verification: VERIFIED** (2026-08-06). PostgreSQL is
reachable, alembic revision is `0007_historical_availability`, all 9 Phase 7
tables exist and contain 0 rows, and the production-path availability importer
confirms `fetched=2447, matched=0, unmatched=2447, persisted=0`.

