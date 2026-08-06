# Phase 8 — Temporal Feasibility Analysis

**Date:** 2026-08-06  
**Scope:** Determine whether each Phase 8 tactical signal can be historically
acquired, normalized, temporally validated, and empirically tested.  
**Framework:** Per `docs/temporal-integrity.md`, all backtesting uses
`InformationAccessPolicy.STRICT_REPRODUCIBILITY` by default, which requires
both `available_at <= cutoff` AND `ingested_at <= cutoff`.

---

## 1. Temporal Definitions

| Field | Meaning | Used for |
|-------|---------|----------|
| `event_time` | When the football event occurred (match kickoff, injury occurrence, etc.) | Outcome alignment |
| `published_at` | When the source published the information (e.g., news article timestamp) | Pre-deadline availability (PUBLIC_AVAILABILITY) |
| `available_at` | Earliest timestamp our system can legitimately claim access (e.g., after club press conference) | Pre-deadline availability (PUBLIC_AVAILABILITY) |
| `ingested_at` | When our pipeline actually collected the data | System-level constraint (SYSTEM_AVAILABILITY) |
| `cutoff` | Gameweek deadline time (from `gameweeks.deadline_time` in DB) | The hard backtest boundary |

### 1.1 Decision Cutoff

Per `src/fpl_intelligence/backtesting/cutoff.py`, the decision cutoff is
derived from the gameweek deadline time, adjusted by a configurable offset.
The default policy is `STRICT_REPRODUCIBILITY`:

```
Condition: available_at <= cutoff AND ingested_at <= cutoff
```

For a signal to be **empirically testable** in this Phase, it must satisfy:

1. **Real data exists** for at least one development season (2022-23, 2023-24,
   2024-25).
2. **Temporal validity:** `published_at` or `available_at` can be established
   and proven to be before the gameweek deadline.
3. **Entity resolution:** The signal's player/team IDs map to canonical IDs in
   the existing database.
4. **Reproducibility:** The raw data is cached and re-importable.
5. **No look-ahead:** The signal does not use `players_raw.csv`-style terminal
   snapshots as pre-deadline intelligence.

---

## 2. Signal-by-Signal Feasibility

### Phase 8 Signals (from `docs/phase8-scope.md` §3)

| # | Signal | Empirically testable? | Temporal barrier | Notes |
|---|--------|----------------------|------------------|-------|
| 1 | Starting lineups | No | No pre-deadline confirmed XI source | `gws/gwN.csv starts` is post-match outcome |
| 2 | Formations | No | No historical formation source | FBref has data but post-match |
| 3 | Player positions (tactical) | No | FPL `position` is roster, not tactical | Would need on-pitch position data |
| 4 | Player role changes | No | Requires tactical position data | No source tracks role shifts |
| 5 | Positional role context | No | No source for tactical role within formation | Would need formation + position data |
| 6 | Set-piece takers — penalties | No | `players_raw.csv` is `UNSAFE_LOOKAHEAD` | `penalties_order` is terminal snapshot |
| 7 | Set-piece takers — free kicks | No | `players_raw.csv` is `UNSAFE_LOOKAHEAD` | `direct_freekicks_order` is terminal snapshot |
| 8 | Set-piece takers — corners | No | `players_raw.csv` is `UNSAFE_LOOKAHEAD` | `corners_and_indirect_freekicks_order` is terminal snapshot |
| 9 | Manager changes | No | No manager data in any source | Not in FPL API; would need club archives |
| 10 | Manager formation tendencies | No | No manager + formation data | Requires sources 3.2/3.4 + manager data |
| 11 | Manager rotation tendencies | No | Requires per-match lineup data | No historical lineup source with timing |
| 12 | Team style indicators | No | FPL team strength is `HISTORICAL_OUTCOME_ONLY` | Season-end aggregates, not tactical style |
| 13 | Opponent tactical matchup | No | Requires formation + style data | No source provides both with timing |
| 14 | Minutes risk from role changes | No | Requires lineup + position data | No pre-deadline lineup source |
| 15 | Differential from tactical shifts | No | Requires all above | No base data |

**Summary: 0 of 15 signals are empirically testable with current data.**

---

## 3. Current Data Source Temporal Classification

### 3.1 FPL Bootstrap `players_raw.csv` — `UNSAFE_LOOKAHEAD`

| Signal | Field | Temporal class | Barrier |
|--------|-------|----------------|---------|
| Set-piece takers (penalties) | `penalties_order` | `UNSAFE_LOOKAHEAD` | Terminal season-end snapshot; no per-GW timing |
| Set-piece takers (free kicks) | `direct_freekicks_order` | `UNSAFE_LOOKAHEAD` | Same |
| Set-piece takers (corners) | `corners_and_indirect_freekicks_order` | `UNSAFE_LOOKAHEAD` | Same |

**Evidence:** The Phase 7.2 forensic re-audit (`docs/phase7-2-forensic-audit.json`,
reproduced in `docs/phase7-2-source-audit.md` §9) confirmed that
`players_raw.csv` is a **terminal season-end snapshot**. The `news_added`
timestamp reflects the *last* known news at season end (max dates fall in May,
the season-final month; `sum(minutes)` ≈ 749k/season confirms full-season
cumulative stats). While `news_added` is a real timestamp, it is a
**look-ahead signal** for every in-season decision point.

The same applies to set-piece orders: `penalties_order`,
`direct_freekicks_order`, and `corners_and_indirect_freekicks_order` are
captured once per season-end snapshot. A player traded as a penalty taker
mid-season, or a new penalty taker appointed by a new manager, would not be
reflected — the snapshot shows only the terminal state.

**Can the set-piece data be salvaged?** Only if per-gameweek snapshots of
`players_raw.csv` were available (i.e., the vaastav mirror's `gws/gwN.csv`
files also contained set-piece order columns). The current `gws/gwN.csv`
schema (`name, position, team, xP, assists, bonus, bps, ...`) does NOT
include set-piece order fields. This means there is **no per-gameweek set-piece
data** in any cached source.

### 3.2 FPL Gameweek CSVs (`gws/gwN.csv`) — `HISTORICAL_OUTCOME_ONLY`

| Signal | Field | Temporal class | Barrier |
|--------|-------|----------------|---------|
| Starting lineups | `starts` (0/1) | `HISTORICAL_OUTCOME_ONLY` | Post-match outcome, not pre-deadline |
| Player positions (FPL) | `position` (GK/DEF/MID/FWD) | `HISTORICAL_OUTCOME_ONLY` | Roster position, not tactical position |

**Evidence:** Each `gwN.csv` file contains per-player rows for that gameweek
with `kickoff_time`, `minutes`, `starts`, `position`, `opponent_team`. The
`starts` field reveals, *in hindsight*, whether a player started the match.
The `position` field is the FPL roster position (GK/DEF/MID/FWD), not the
player's on-pitch tactical position within a formation (e.g., LW, LCM, RCB).

The `kickoff_time` field is useful for temporal alignment (`event_time`), and
`opponent_team` provides opponent context — but these are already available
via `fixtures.csv` and already implemented as features in
`src/fpl_intelligence/features/calculators/fixture_features.py`.

### 3.3 FPL Fixtures CSV — `STRICT_BACKTEST_SAFE` (but no tactical signal)

| Signal | Field | Temporal class | Barrier |
|--------|-------|----------------|---------|
| Opponent context (difficulty) | `team_h_difficulty`, `team_a_difficulty` | `STRICT_BACKTEST_SAFE` | Proxy for opponent strength, not tactical style |

**Evidence:** The fixture schedule (including kickoff times and FPL fixture
difficulty ratings) is published in pre-season and known before any gameweek
deadline. This is genuinely `STRICT_BACKTEST_SAFE`. However, the difficulty
rating is a **single integer (1-5)** derived from FPL's internal team strength
model — it does not encode formations, tactical style, or matchup context.
This signal is already implemented as a feature (`fixture_features.py`), so it
is not a new Phase 8 signal.

### 3.4 FPL Teams CSV — `HISTORICAL_OUTCOME_ONLY`

| Signal | Field | Temporal class | Barrier |
|--------|-------|----------------|---------|
| Team style | `strength_overall_home/away`, `strength_attack_home/away`, `strength_defence_home/away` | `HISTORICAL_OUTCOME_ONLY` (season-end aggregates) | Not pre-deadline; no tactical style |

**Evidence:** Team strength fields are season-end aggregates. They summarize
performance over the full season and are not available as pre-deadline
indicators of tactical approach.

---

## 4. Candidate Source Temporal Feasibility

### 4.1 FBref Match Reports — `HISTORICAL_EVENT_ONLY`

| Temporal field | Available? | Value |
|----------------|-----------|-------|
| `event_time` | Yes | Match kickoff time (in report) |
| `published_at` | No | FBref publishes post-match; no explicit publication timestamp |
| `available_at` | No | Would need to infer from club lineup announcement (~60 min pre-kickoff) |
| `ingested_at` | At scrape time | Would be set when the scraper retrieves the page |

**Barrier:** FBref match reports contain formations and confirmed starting XI,
but they are published **after** the match. The lineup was confirmed by clubs
~60 min before kickoff, but FBref does not record the *announcement time* —
only the match result time. Without a `published_at` or `available_at`
preceding the FPL deadline, these data are `HISTORICAL_EVENT_ONLY`.

**Feasibility:** To make FBref data `STRICT_BACKTEST_SAFE`, one would need to
either (a) scrape the club websites' lineup announcements separately (which
is candidate 4.5), or (b) assume a fixed offset (e.g., "lineups published 60
min before kickoff") — but this assumption would need to be validated against
real pre-deadline announcement times, which FBref does not provide.

### 4.2 football-data.org API — `HISTORICAL_EVENT_ONLY`

| Temporal field | Available? | Value |
|----------------|-----------|-------|
| `event_time` | Yes | Fixture `kickoff_time` |
| `published_at` | No | No publication timestamp on lineup data |
| `available_at` | No | Not tracked by the API |
| `ingested_at` | At API call time | Would be set when the pipeline calls the API |

**Barrier:** Even if football-data.org provides lineup data (on paid tiers),
the API returns the latest confirmed lineup state without a pre-deadline
publication timestamp. Lineups are confirmed ~60 min before kickoff, but the
API does not expose when the lineup was first available.

### 4.3 TheStatsAPI — `HISTORICAL_EVENT_ONLY`

| Temporal field | Available? | Value |
|----------------|-----------|-------|
| `event_time` | Yes | Fixture kickoff time |
| `published_at` | No | No publication timestamp exposed |
| `available_at` | No | Would need to infer from club lineup announcement |
| `ingested_at` | At API call time | Would be set when the pipeline calls the API |

**Barrier:** Same as FBref — confirmed lineups and formations are available
historically, but without a pre-deadline publication timestamp, they are
`HISTORICAL_EVENT_ONLY`. The API documentation states "confirmed lineups
typically publish around an hour before kickoff" but does not expose the
exact publication time as a data field.

### 4.4 Transfermarkt — `HISTORICAL_EVENT_ONLY`

| Temporal field | Available? | Value |
|----------------|-----------|-------|
| `event_time` | Partial | Injury/return date recorded |
| `published_at` | No | No publication timestamp |
| `available_at` | No | Not tracked |
| `ingested_at` | At scrape time | Would be set when scraped |

**Barrier:** Transfermarkt provides injury/suspension records with return
dates, but no publication timestamps. Injury occurrence dates are recorded,
but when the information was *announced* (the key question for FPL decisions)
is not tracked.

### 4.5 Club Official Archives — Potentially `STRICT_BACKTEST_SAFE`

| Temporal field | Available? | Value |
|----------------|-----------|-------|
| `event_time` | Yes | Press conference / announcement time |
| `published_at` | Yes | Article publication timestamp (on club websites) |
| `available_at` | Yes (inferred) | Press conference time ≈ public availability |
| `ingested_at` | At scrape time | Would be set when the pipeline scrapes the archive |

**Barrier:** Club press conferences are held 1–2 days before matches, and
club websites publish articles with explicit publication timestamps. If these
timestamps are preserved during scraping, the data would be
`STRICT_BACKTEST_SAFE`. However:

1. **No clean bulk archive:** Each club has its own website structure;
   no single endpoint aggregates all clubs' archives across seasons.
2. **Historical depth:** Most club websites only archive 1–2 seasons deep;
   older seasons may require Wayback Machine archival.
3. **Entity resolution:** Club-reported player names must be matched to
   canonical FPL player IDs (the existing `entity_resolution/resolver.py`
   handles name+team+position matching but would need tuning).
4. **Coverage completeness:** Not all clubs publish pre-match press
   conferences; some managers hold conferences at different times.

**Feasibility:** This is the **only** source that could potentially provide
`STRICT_BACKTEST_SAFE` pre-deadline tactical intelligence (starting XI hints,
formation preferences, manager rotation patterns). However, it requires:
- A per-club scraping adapter with publication timestamp preservation.
- Wayback Machine backup for seasons where club archives are unavailable.
- Entity resolution for player name → FPL player ID mapping.
- Validation that press conference timing consistently precedes FPL deadlines.

### 4.6 Community-Predicted Lineup Sources — `HISTORICAL_EVENT_ONLY`

| Temporal field | Available? | Value |
|----------------|-----------|-------|
| `event_time` | No | Prediction deadline, not football event |
| `published_at` | Usually yes | Article publication timestamp |
| `available_at` | Yes (inferred) | Article publication time ≈ availability |
| `ingested_at` | At scrape time | Would be set when scraped |

**Barrier:** Community sources (Fantasy Football Scout, FPL360, DraftFC)
publish *predicted* lineups before deadlines with article publication
timestamps. However:

1. **No confirmed accuracy:** Predictions are not ground truth; accuracy
   varies by source and requires calibration.
2. **Limited historical archives:** Most community sites do not maintain
   clean historical archives with publication timestamps.
3. **No formation detail:** Predicted lineups show XI but rarely encode
   formation shape or tactical positions.

**Feasibility:** Could provide `STRICT_BACKTEST_SAFE` *prediction* signals
(published before deadline), but only if archives with publication timestamps
exist and prediction accuracy can be calibrated against confirmed lineups
(from FBref/TheStatsAPI, which are `HISTORICAL_EVENT_ONLY`).

---

## 5. Temporal Field Representation Summary

### Current data — can each signal be represented with the 4 temporal fields?

| Signal | `event_time` | `published_at` | `available_at` | `ingested_at` | Verdict |
|--------|-------------|----------------|----------------|---------------|---------|
| Starting lineups | ✅ (match kickoff) | ❌ | ❌ | ❌ | Engineering-only |
| Formations | ❌ | ❌ | ❌ | ❌ | Not currently available |
| Player positions (tactical) | ❌ | ❌ | ❌ | ❌ | FPL position ≠ tactical position |
| Player role changes | ❌ | ❌ | ❌ | ❌ | Not tracked |
| Positional role context | ❌ | ❌ | ❌ | ❌ | Not tracked |
| Set-piece — penalties | ❌ | ❌ (`news_added` is look-ahead) | ❌ | ❌ | `UNSAFE_LOOKAHEAD` |
| Set-piece — free kicks | ❌ | ❌ | ❌ | ❌ | `UNSAFE_LOOKAHEAD` |
| Set-piece — corners | ❌ | ❌ | ❌ | ❌ | `UNSAFE_LOOKAHEAD` |
| Manager changes | ❌ | ❌ | ❌ | ❌ | Not tracked |
| Manager formation tendencies | ❌ | ❌ | ❌ | ❌ | Not tracked |
| Manager rotation | ❌ | ❌ | ❌ | ❌ | Not tracked |
| Team style indicators | ❌ | ❌ | ❌ | ❌ | Season-end aggregate |
| Opponent matchup | ✅ (kickoff) | ❌ | ❌ | ❌ | Proxy only (difficulty rating) |
| Minutes risk from role changes | ❌ | ❌ | ❌ | ❌ | Not tracked |
| Differential from tactical shifts | ❌ | ❌ | ❌ | ❌ | Not tracked |

### Candidate sources — can each signal be represented with the 4 temporal fields?

| Source | Signal | `event_time` | `published_at` | `available_at` | `ingested_at` | Temporal class |
|--------|--------|-------------|----------------|----------------|---------------|----------------|
| Official FPL API | Set-piece orders | ❌ | ❌ | ❌ | ❌ | `LIVE_ONLY` |
| FBref | Starting lineups, formations | ✅ | ❌ | ❌ | ⚠️ (scrape) | `HISTORICAL_EVENT_ONLY` |
| football-data.org | Starting lineups | ✅ | ❌ | ❌ | ⚠️ (API call) | `HISTORICAL_EVENT_ONLY` |
| TheStatsAPI | Starting lineups, formations | ✅ | ❌ | ❌ | ⚠️ (API call) | `HISTORICAL_EVENT_ONLY` |
| Transfermarkt | Injuries, suspensions | ⚠️ (event date) | ❌ | ❌ | ⚠️ (scrape) | `HISTORICAL_EVENT_ONLY` |
| Club archives | All pre-match tactical | ✅ | ✅ | ✅ | ⚠️ (scrape) | Would be `STRICT_BACKTEST_SAFE` |
| Community predictors | Predicted lineups | ❌ | ✅ | ✅ (inferred) | ⚠️ (scrape) | `HISTORICAL_EVENT_ONLY` |

---

## 6. Empirically Testable vs Engineering-Only

### Empirically testable with current data: **NONE (0/15)**

No Phase 8 signal has real historical data with pre-deadline publication
timestamps that can pass `InformationAccessPolicy.STRICT_REPRODUCIBILITY`.
The closest candidates are:

- **Set-piece orders** in `players_raw.csv` — real and populated across all
  3 development seasons (60–77 penalty takers, 61–77 corner takers, 65–68
  free-kick takers per season), but `UNSAFE_LOOKAHEAD` (terminal snapshot, no
  per-GW timing).
- **Starting lineups** via `gws/gwN.csv` `starts` column — real outcome data,
  but `HISTORICAL_OUTCOME_ONLY` (post-match, not pre-deadline).

### Engineering-only until live data accumulation begins: **ALL (15/15)**

Every Phase 8 signal is engineering-only. The infrastructure to *represent*
these signals exists:

- The `AvailabilityTimestamps` dataclass
  (`availability/historical/temporal.py`) supports `event_time`,
  `published_at`, `available_at`, `ingested_at`.
- The `TemporalClass` enum supports all classifications.
- The entity-resolution infrastructure can map provider player IDs to
  canonical IDs (with the Phase 7 `real_fpl` vs `real_fpl_bootstrap` mismatch
  as a cautionary example).
- The `TemporalQueryBuilder` and `InformationAccessPolicy` enforce no-look-ahead.

But **no real data source** currently provides pre-deadline tactical
information with publication timestamps.

---

## 7. The Fundamental Temporal Gap

The core problem is a **timing gap**:

- **FPL deadlines** fall ~90 minutes before the first kickoff of the gameweek
  (or, for midweek fixtures, before the first match of that gameweek).
- **Tactical information** (confirmed lineups, formations, training
  participation) is typically announced ~60 minutes before kickoff — that is,
  **after** the FPL deadline.

This means that even for **live** data, the tactical information is published
*after* the FPL decision deadline. The only pre-deadline tactical information
comes from:

1. **Press conferences** (held 1–2 days before matches) — but only for the
   upcoming fixture, not all fixtures in the gameweek.
2. **Injury news** (breaks at any time) — covered by Phase 7 (blocked).
3. **Manager statements** (post-press conference) — club archives.

The vaastav mirror captures nothing of this pre-deadline information flow.
The `news_added` field in `players_raw.csv` is the *terminal* news state,
not the pre-deadline news state.

---

## 8. Recommendations for Making Signals Empirically Testable

To move any Phase 8 signal from engineering-only to empirically testable:

1. **Acquire per-gameweek set-piece snapshot data.** The vaastav `gws/gwN.csv`
   files do not include `penalties_order`, `corners_and_inddirect_freekicks_order`,
   or `direct_freekicks_order`. If a per-GW snapshot of these fields existed
   (i.e., the FPL API was polled at each deadline), the set-piece signals
   could become `STRICT_BACKTEST_SAFE`.

2. **Acquire pre-deadline lineup sources.** Club press conference archives
   (candidate 4.5) are the only path to `STRICT_BACKTEST_SAFE` confirmed
   lineup data. This requires building a per-club scraping infrastructure
   with publication timestamp preservation and entity resolution.

3. **Acquire manager data.** No available source provides manager tenure,
   formation history, or rotation patterns. A dedicated manager database
   (e.g., from Transfermarkt or the Premier League official site) would be
   required — but these are `HISTORICAL_EVENT_ONLY` without publication
   timestamps.

4. **Build a publication-timestamp-aware ingestion pipeline.** Every tactical
   candidate source (FBref, TheStatsAPI, football-data.org) lacks explicit
   publication timestamps. A pipeline that records
   `published_at`/`available_at` for every scraped/queried record is needed
   before any candidate can achieve `STRICT_BACKTEST_SAFE` status.

5. **Validate timing assumptions.** The assumption that "lineups are
   confirmed ~60 min before kickoff" must be validated against real club
   announcement times. Without this validation, inferred timestamps are
   `UNSAFE_LOOKAHEAD`.

---

## 9. Conclusion

**No Phase 8 tactical signal can be empirically tested with current data.**
All 15 signals are engineering-only, pending the acquisition of a
`STRICT_BACKTEST_SAFE` historical source with pre-deadline publication
timestamps.

The temporal infrastructure is ready: the `AvailabilityTimestamps` dataclass,
`TemporalClass` enum, `InformationAccessPolicy`, and `TemporalQueryBuilder`
can all represent and enforce the temporal constraints for tactical signals.
What is missing is the **data** — specifically, historical records with
publication/availability timestamps that precede FPL gameweek deadlines.

Phase 8 remains in scope-audit-only mode. No tactical models may be
implemented until a `STRICT_BACKTEST_SAFE` source for at least one signal is
identified, integrated, and validated against the existing temporal integrity
framework.
