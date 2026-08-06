# Phase 8 — Tactical Intelligence Source Audit

**Date:** 2026-08-06  
**Scope:** Source audit only. No model implementation, no training, no holdout evaluation.  
**Methodology:** Each candidate source was evaluated against the 9 required fields
and the 4 temporal categories. Sources are categorized by what they can
currently provide, not what they could provide if integrated.

---

## 1. Audit Framework

### 1.1 Required Fields Per Source

For each source, the following fields are recorded:

- **source name** — human-readable identifier
- **data type** — what kind of tactical data this source provides
- **historical coverage** — seasons/years covered
- **temporal granularity** — event-level, daily, gameweek, seasonal
- **publication/availability timing** — when data becomes available relative to the decision deadline
- **strict-backtest safety** — `STRICT_BACKTEST_SAFE`, `HISTORICAL_EVENT_ONLY`, `LIVE_ONLY`, `UNSAFE_LOOKAHEAD`, or `MOCK_ENGINEERING_ONLY`
- **licensing/access constraints** — legal and technical access limitations
- **expected reliability** — assessment of data quality
- **integration complexity** — effort to wire into the existing ingestion pipeline

### 1.2 Temporal Category Definitions

| Category | When applicable |
|----------|----------------|
| `STRICT_BACKTEST_SAFE` | Publication/availability timestamp exists and precedes the gameweek deadline; source is reproducible and cached |
| `HISTORICAL_EVENT_ONLY` | Event occurred in the historical record but publication/availability timing cannot be established relative to the deadline |
| `LIVE_ONLY` | Data is only produced live during a season; no historical archive available |
| `UNSAFE_LOOKAHEAD` | Terminal season-end snapshot; timestamp is a look-ahead signal for in-season decisions |
| `MOCK_ENGINEERING_ONLY` | Synthetic/generated data for pipeline testing; never used as real evidence |

### 1.3 The `event_time` / `published_at` / `available_at` / `ingested_at` Test

Each source is evaluated for whether each of its signals can be represented
with the four temporal fields defined in `docs/temporal-integrity.md`:

- `event_time` — when the football event occurred
- `published_at` — when the source published the information
- `available_at` — earliest timestamp our system can legitimately claim access
- `ingested_at` — when our pipeline actually collected it

---

## 2. Currently Available Project Data Sources

### 2.1 FPL Bootstrap `players_raw.csv` (vaastav mirror)

| Field | Value |
|-------|-------|
| **source name** | vaastav/Fantasy-Premier-League `players_raw.csv` |
| **data type** | Set-piece taker orders, availability status, basic player metadata |
| **historical coverage** | 2022-23, 2023-24, 2024-25 (development); 2025-26 (locked holdout) |
| **temporal granularity** | Season-end snapshot (one row per player per season) |
| **publication/availability timing** | Terminal season-end snapshot; `news_added` is look-ahead contaminated |
| **strict-backtest safety** | `UNSAFE_LOOKAHEAD` (per Phase 7.2 forensic re-audit) |
| **licensing/access constraints** | Public GitHub (MIT license); raw.githubusercontent.com; cached locally |
| **expected reliability** | High for set-piece orders (FPL-published); terminal snapshot for availability |
| **integration complexity** | Already integrated under provider `real_fpl_bootstrap` |

**Signals provided:**

| Signal # | Signal | Coverage | Temporal class |
|----------|--------|----------|----------------|
| 6 | Set-piece takers — penalties | `penalties_order` populated: 60/778 (2022-23), 52/865 (2023-24), 50/804 (2024-25) | `UNSAFE_LOOKAHEAD` (season-end snapshot) |
| 7 | Set-piece takers — free kicks | `direct_freekicks_order` populated: 68/778, 61/865, 65/804 | `UNSAFE_LOOKAHEAD` |
| 8 | Set-piece takers — corners | `corners_and_indirect_freekicks_order` populated: 77/778, 74/865, 75/804 | `UNSAFE_LOOKAHEAD` |

**Temporal field availability:**

| Field | Available? | Notes |
|-------|-----------|-------|
| `event_time` | No | No event_time; single terminal snapshot per player-season |
| `published_at` | Yes (`news_added`) | Look-ahead — terminal snapshot, not pre-deadline |
| `available_at` | No | Not separately tracked; conflated with `news_added` |
| `ingested_at` | No | Not tracked per-player in current schema |

**Assessment:** The set-piece order columns are **real, published, FPL-official**
data with a clear provider player ID (`element`/`id`) that maps to the existing
canonical schema. However, they are **season-end snapshots** — set-piece taker
designations can change mid-season (transfers, manager changes, player
form). The `players_raw.csv` file captures the *terminal* state, not the state
at each gameweek deadline. This makes the set-piece data
`UNSAFE_LOOKAHEAD` for strict backtesting, even though the underlying *fact*
(a player was the penalty taker for GW10) is real.

**Can be used as:** Engineering-only. The set-piece order schema exists in the
FPL mirror but the temporal snapshotting prevents strict pre-deadline use.
If a per-gameweek snapshot were available (see candidate 3.2), this signal
would become `STRICT_BACKTEST_SAFE`.

### 2.2 FPL Gameweek CSVs (`gws/gwN.csv`)

| Field | Value |
|-------|-------|
| **source name** | vaastav/Fantasy-Premier-League `gws/gwN.csv` |
| **data type** | Per-gameweek player performance, opponent context |
| **historical coverage** | 2022-23, 2023-24, 2024-25, 2025-26 |
| **temporal granularity** | Per-gameweek (post-match outcome) |
| **publication/availability timing** | Gameweek-end snapshot (post-deadline outcomes) |
| **strict-backtest safety** | `HISTORICAL_OUTCOME_ONLY` for price/ownership; `STRICT_BACKTEST_SAFE` for outcome stats via gameweek ordering (per `real_fpl.py` docstring) |
| **licensing/access constraints** | Public GitHub (MIT license); cached locally |
| **expected reliability** | High for performance statistics; gameweek-end timing for snapshots |
| **integration complexity** | Already integrated under provider `real_fpl` |

**Signals provided:**

| Signal # | Signal | Coverage | Temporal class |
|----------|--------|----------|----------------|
| 1 | Starting lineups | `starts` (binary 0/1), `minutes` per GW | Outcome only — reveals who started *after* the match |
| 3 | Player positions | `position` (GK/DEF/MID/FWD) per GW | Outcome only — FPL position, not tactical role |

**Temporal field availability:**

| Field | Available? | Notes |
|-------|-----------|-------|
| `event_time` | Yes (`kickoff_time`) | When the match occurred |
| `published_at` | No | No separate publication timestamp |
| `available_at` | No | Snapshot is post-match |
| `ingested_at` | No | Not tracked per-GW in current schema |

**Assessment:** The gameweek CSVs contain `starts` and `minutes`, which retroactively
reveal who started each match — but this is **outcome data published after the
fixture**. There is no pre-deadline lineup prediction or confirmed starting XI.
The `position` column is the FPL roster position (GK/DEF/MID/FWD), not the
tactical on-pitch position within a formation. These support **outcome-based
analysis** (e.g., "did this tactical shift correlate with starts?") but cannot
drive pre-deadline decisions.

**Can be used as:** Engineering-only (for outcome analysis, not pre-deadline features).

### 2.3 FPL Fixtures CSV

| Field | Value |
|-------|-------|
| **source name** | vaastav/Fantasy-Premier-League `fixtures.csv` |
| **data type** | Fixture metadata, kickoff times, FPL fixture difficulty |
| **historical coverage** | 2022-23, 2023-24, 2024-25, 2025-26 |
| **temporal granularity** | Per-fixture |
| **publication/availability timing** | Kickoff time known well before deadline (fixture schedule published in pre-season) |
| **strict-backtest safety** | `STRICT_BACKTEST_SAFE` for fixture schedule and kickoff time |
| **licensing/access constraints** | Public GitHub (MIT license) |
| **expected reliability** | High |
| **integration complexity** | Already integrated |

**Signals provided:**

| Signal # | Signal | Temporal class |
|----------|--------|----------------|
| 13 | Opponent tactical matchup context | `STRICT_BACKTEST_SAFE` (fixture schedule, kickoff known pre-deadline) — but **no** tactical style data, only fixture difficulty rating |

**Assessment:** The `team_h_difficulty` / `team_a_difficulty` fields provide FPL's
fixture difficulty rating, which already exists as a feature in
`src/fpl_intelligence/features/calculators/fixture_features.py`. This is a
**proxy** for opponent strength, not opponent tactical style. No formation,
lineup, or tactical matchup data is present.

### 2.4 FPL Teams CSV

| Field | Value |
|-------|-------|
| **source name** | vaastav/Fantasy-Premier-League `teams.csv` |
| **data type** | Team strength metrics (FPL difficulty ratings) |
| **historical coverage** | 2022-23, 2023-24, 2024-25, 2025-26 |
| **temporal granularity** | Seasonal snapshot |
| **publication/availability timing** | Season-end snapshot |
| **strict-backtest safety** | `HISTORICAL_OUTCOME_ONLY` (strength metrics are end-of-season aggregates) |
| **licensing/access constraints** | Public GitHub (MIT license) |
| **expected reliability** | High for FPL strength metrics |
| **integration complexity** | Already integrated via `RealFPLProvider.get_teams()` |

**Signals provided:**

| Signal # | Signal | Temporal class |
|----------|--------|----------------|
| 12 | Team style indicators | `UNSAFE_LOOKAHEAD` — `strength_overall_home/away`, `strength_attack_home/away`, `strength_defence_home/away` are season-end aggregates, not pre-deadline indicators of tactical style |

**Assessment:** The team strength fields are **season-end aggregates**, useful
for opponent difficulty but not for real-time tactical style. No formation
preference, pressing style, or tactical approach is encoded.

### 2.5 DB Schema (`db/models.py`)

The existing DB schema in `src/fpl_intelligence/db/models.py` contains no
tables for formations, lineups, manager data, or tactical roles. The Phase 7
availability schema (`availability/models.py`) defines tables for
`AvailabilitySource`, `AvailabilityArticle`, `AvailabilityEvidence`,
`AvailabilityEvent`, `PlayerInjury`, `PlayerSuspension`, `TrainingReport`,
`PressConference`, `PlayerMention` — none of which cover tactical intelligence
(lineups, formations, set-piece assignments, manager tendencies).

---

## 3. Candidate Additional Sources

### 3.1 Official FPL API (`fantasy.premierleague.com/api/`)

| Field | Value |
|-------|-------|
| **source name** | Official FPL API (bootstrap-static, fixtures, element-summary, entry picks) |
| **data type** | Player metadata, set-piece orders, availability, gameweek fixtures |
| **historical coverage** | Current season only (live API); historical data via community archives |
| **temporal granularity** | Real-time / per-gameweek |
| **publication/availability timing** | Live API; no historical archive endpoint |
| **strict-backtest safety** | `LIVE_ONLY` for real-time; `HISTORICAL_OUTCOME_ONLY` for archived data |
| **licensing/access constraints** | No auth required but undocumented/tou-warning; Terms of Use restrict automated access |
| **expected reliability** | High for current data; no historical archive |
| **integration complexity** | Low (existing `OfficialFPLDataProvider` adapter in `collectors/official_fpl.py`) |

**Signals provided:**

| Signal # | Signal | Coverage | Temporal class |
|----------|--------|----------|----------------|
| 6 | Set-piece takers — penalties | `penalties_order` in bootstrap-static | `LIVE_ONLY` (no historical archive) |
| 7 | Set-piece takers — free kicks | `direct_freekicks_order` | `LIVE_ONLY` |
| 8 | Set-piece takers — corners | `corners_and_indirect_freekicks_order` | `LIVE_ONLY` |
| 1 | Starting lineups | `/entry/{id}/event/{gw}/picks/` (FPL manager's own XI, not real match XI) | `LIVE_ONLY`, and **not** real match starting XI |
| 9 | Manager changes | Not available (FPL manager ≠ club manager) | N/A |
| 10 | Manager formation tendencies | Not available | N/A |

**Assessment:** The official FPL API does **not** provide real football match
starting XI, formations, club manager data, or tactical positions. The
`/entry/{id}/event/{gw}/picks/` endpoint returns an FPL manager's chosen
squad, not the real match lineup. Set-piece orders exist in `bootstrap-static`
but the API provides **no historical archive** — only live/current season data.
This makes the official FPL API `LIVE_ONLY` for any tactical signal.

**Temporal field availability:**

| Field | Available? | Notes |
|-------|-----------|-------|
| `event_time` | Partial | `kickoff_time` on fixtures |
| `published_at` | No | No publication timestamp on API data |
| `available_at` | No | API has no `available_at` semantics |
| `ingested_at` | No | Not tracked in current code |

### 3.2 FBref Match Reports (Scraper / API)

| Field | Value |
|-------|-------|
| **source name** | FBref.com (Sports Reference) match reports |
| **data type** | Match lineups, formations, substitutions, player positions, captain |
| **historical coverage** | 2010-present (Premier League); deep historical archive |
| **temporal granularity** | Per-match (confirmed lineups at match time) |
| **publication/availability timing** | Match reports published after kickoff; lineups confirmed ~60 min before kickoff |
| **strict-backtest safety** | `HISTORICAL_EVENT_ONLY` — lineups are confirmed pre-kickoff but FBref publication is post-match; no explicit pre-deadline publication timestamp |
| **licensing/access constraints** | Sports Reference Terms of Use restrict automated access; requires rate-limiting (1 req/3s); commercial reuse requires permission |
| **expected reliability** | High — official Opta-sourced data |
| **integration complexity** | High — requires scraping adapter (no clean API); `fbref-api` project on GitHub provides a wrapper |

**Signals provided:**

| Signal # | Signal | Coverage | Temporal class |
|----------|--------|----------|----------------|
| 1 | Starting lineups | `starting_xi` per team per match | `HISTORICAL_EVENT_ONLY` |
| 2 | Formations | `formation`, `opp_formation` in match reports | `HISTORICAL_EVENT_ONLY` |
| 3 | Player positions | On-pitch position within formation | `HISTORICAL_EVENT_ONLY` |

**Assessment:** FBref match reports contain `Formation` and `Opp Formation`
columns, confirmed starting XI, and per-player positions. The data is
historical and deep. However:

- **No publication timestamp:** FBref match reports are published *after* the
  match. The lineup itself was confirmed ~60 min before kickoff (published by
  clubs), but FBref does not record *when* the lineup was published — only
  the match result time. This makes it `HISTORICAL_EVENT_ONLY`, not
  `STRICT_BACKTEST_SAFE`.
- **Scraping constraints:** Sports Reference's ToU explicitly restrict
  automated access. A compliant scraper (rate-limited, with caching, honest
  User-Agent) is possible but legally precarious for commercial use.
- **Integration effort:** FBref has no official API; the `fbref-api` project
  (GitHub) provides a wrapper but requires self-hosting and rate-limiting.

**Temporal field availability:**

| Field | Available? | Notes |
|-------|-----------|-------|
| `event_time` | Yes | Kickoff time in match report |
| `published_at` | No | No publication timestamp on FBref page |
| `available_at` | No | Would need to infer from club lineup announcement timing |
| `ingested_at` | No | Would be set at scrape time |

**Can be used as:** Engineering-only (requires publication timestamp
reconstruction to achieve `STRICT_BACKTEST_SAFE`).

### 3.3 football-data.org API

| Field | Value |
|-------|-------|
| **source name** | football-data.org API v4 (Premier League) |
| **data type** | Fixtures, results, lineups/subs, standings, match stats |
| **historical coverage** | Free tier covers current + recent seasons; historical depth varies by plan |
| **temporal granularity** | Per-match |
| **publication/availability timing** | Lineups available ~60 min pre-kickoff; API returns latest data |
| **strict-backtest safety** | `HISTORICAL_EVENT_ONLY` — no explicit publication timestamp on lineup data |
| **licensing/access constraints** | Free tier requires registration (API key); 10 req/min rate limit; Terms of Use restrict redistribution |
| **expected reliability** | High for covered competitions |
| **integration complexity** | Low — clean REST API; requires API key |

**Signals provided:**

| Signal # | Signal | Coverage | Temporal class |
|----------|--------|----------|----------------|
| 1 | Starting lineups | `/matches/{id}/lineups` (if available on tier) | `HISTORICAL_EVENT_ONLY` |

**Assessment:** football-data.org documents "lineups/subs" as a supported
resource and mentions lineup filtering
(`/persons/{id}/matches?lineup=BENCH`). However, the lineup endpoint is not
explicitly documented in the v4 reference for Premier League, and access
depends on the subscription tier. The free tier covers fixtures and
standings but deeper data (lineups, player stats) requires paid tiers. Even with
access, the API returns the latest lineup state without a pre-deadline
publication timestamp, making it `HISTORICAL_EVENT_ONLY` at best.

**Temporal field availability:**

| Field | Available? | Notes |
|-------|-----------|-------|
| `event_time` | Partial | Fixture `kickoff_time` is available |
| `published_at` | No | No publication timestamp on lineup data |
| `available_at` | No | Not tracked |
| `ingested_at` | No | Would be set at API call time |

### 3.4 TheStatsAPI Football Lineups API

| Field | Value |
|-------|-------|
| **source name** | TheStatsAPI.com Football Lineups API |
| **data type** | Starting XI, bench, formations, player positions |
| **historical coverage** | 10+ years of historical match data |
| **temporal granularity** | Per-match |
| **publication/availability timing** | Lineups confirmed ~60 min pre-kickoff; API returns latest state |
| **strict-backtest safety** | `HISTORICAL_EVENT_ONLY` — no explicit pre-deadline publication timestamp |
| **licensing/access constraints** | Paid API ($50–$379/month); Bearer token auth; rate-limited |
| **expected reliability** | High — commercial provider |
| **integration complexity** | Low — clean REST API; requires paid subscription |

**Signals provided:**

| Signal # | Signal | Coverage | Temporal class |
|----------|--------|----------|----------------|
| 1 | Starting lineups | `starting_xi` with player positions | `HISTORICAL_EVENT_ONLY` |
| 2 | Formations | `formation` field | `HISTORICAL_EVENT_ONLY` |
| 3 | Player positions | `position` in starting XI | `HISTORICAL_EVENT_ONLY` |

**Assessment:** TheStatsAPI's Football Lineups API explicitly returns formations,
starting XI, and per-player positions with player IDs that can be joined to
stats. It covers 10+ years of historical data. The API is clean and documented.
However:

- **No publication timestamp:** The API returns the confirmed lineup state but
  does not record *when* the lineup was published relative to kickoff. Lineups
  are confirmed ~60 min before kickoff by clubs, but the API does not expose
  this timing. Without it, the data is `HISTORICAL_EVENT_ONLY`.
- **Cost:** Paid subscription required ($50+/month minimum).
- **Entity resolution:** Player IDs would need to be mapped to the existing
  canonical FPL player IDs.

**Temporal field availability:**

| Field | Available? | Notes |
|-------|-----------|-------|
| `event_time` | Yes | Fixture kickoff time |
| `published_at` | No | No publication timestamp exposed |
| `available_at` | No | Would need to infer from club lineup announcement (~60 min pre-kickoff) |
| `ingested_at` | No | Would be set at API call time |

### 3.5 Transfermarkt Injury/Absence Data

| Field | Value |
|-------|-------|
| **source name** | Transfermarkt injury & suspension database |
| **data type** | Injury records, suspension records, expected return dates |
| **historical coverage** | Broad — many seasons available on the website |
| **temporal granularity** | Event-level (injury occurrence, return date) |
| **publication/availability timing** | Updated when news breaks; no explicit pre-deadline timestamp |
| **strict-backtest safety** | `HISTORICAL_EVENT_ONLY` — no publication timestamp |
| **licensing/access constraints** | Scraping restricted by ToS; commercial use restricted; requires licensing review |
| **expected reliability** | High for major clubs; variable for smaller clubs |
| **integration complexity** | High — requires scraping adapter with rate-limit handling |

**Signals provided:**

| Signal # | Signal | Temporal class |
|----------|--------|
| 4 | Player role changes | `HISTORICAL_EVENT_ONLY` (injury forces role change) |

**Assessment:** Transfermarkt provides structured injury and suspension records
with expected return dates, which can indirectly indicate role changes (a
player returning from injury may shift positions). However, Transfermarkt does
not expose formations, starting lineups, or tactical positions. The
`_NotWiredProvider` stub in `availability/historical/providers.py` already
documents this adapter as audited but not wired.

### 3.6 Club Official Websites / Press Conference Archives

| Field | Value |
|-------|-------|
| **source name** | Individual Premier League club websites, press conference transcripts |
| **data type** | Pre-match pressing, team news, lineup hints, formation announcements |
| **historical coverage** | Varies by club; most clubs archive press conferences |
| **temporal granularity** | Event-level (press conference timing) |
| **publication/availability timing** | Press conferences held 1-2 days before match; some lineup hints closer to deadline |
| **strict-backtest safety** | Would be `STRICT_BACKTEST_SAFE` if publication timestamps preserved |
| **licensing/access constraints** | Club-owned content; ToS varies; no bulk export available |
| **expected reliability** | High for official announcements; variable for interpretation |
| **integration complexity** | Very high — requires per-club scraping infrastructure |

**Signals provided:**

| Signal # | Signal | Temporal class |
|----------|--------|
| 1 | Starting lineups | Would be `STRICT_BACKTEST_SAFE` if published pre-deadline |
| 2 | Formations | Would be `STRICT_BACKTEST_SAFE` if published pre-deadline |
| 3 | Player positions | Would be `STRICT_BACKTEST_SAFE` if published pre-deadline |
| 9 | Manager changes | Would be `STRICT_BACKTEST_SAFE` if published pre-deadline |
| 10 | Manager formation tendencies | Would be `STRICT_BACKTEST_SAFE` if published pre-deadline |
| 11 | Manager rotation tendencies | Would be `STRICT_BACKTEST_SAFE` if published pre-deadline |
| 12 | Team style indicators | Would be `STRICT_BACKTEST_SAFE` if published pre-deadline |

**Assessment:** This is the **richest** source for tactical intelligence but also
the most complex to integrate. Club press conferences (held ~24-48h pre-match)
often reveal formation preferences, starting XI hints, and manager rotation
patterns. However:

- **No clean bulk historical archive:** Each club has its own website
  structure; no single endpoint aggregates all clubs' archives across seasons.
- **Licensing:** Club content is proprietary; scraping may violate ToS.
- **Entity resolution:** Requires mapping club-reported names to canonical
  player IDs.
- **Temporal integrity:** Would require preserving the exact publication
  timestamp of each press conference and lineup announcement.

The `_NotWiredProvider` stub `ClubArchiveAvailabilityProvider` and
`PressConferenceArchiveProvider` in `availability/historical/providers.py`
already document these as extension points.

### 3.7 Community-Predicted Lineup Aggregators (FPL Sites)

Sources like Fantasy Football Scout, FPL360, DraftFC, and RotoWire provide
**predicted** lineups before deadlines.

| Field | Value |
|-------|-------|
| **source name** | Fantasy Football Scout, FPL360, DraftFC, RotoWire |
| **data type** | Predicted starting XI, predicted formation, start percentages |
| **historical coverage** | Varies; mostly current season with limited archives |
| **temporal granularity** | Per-gameweek (predictions updated before each deadline) |
| **publication/availability timing** | Published before deadline (the signal itself) |
| **strict-backtest safety** | `HISTORICAL_EVENT_ONLY` for existing archives; `LIVE_ONLY` for most |
| **licensing/access constraints** | Website ToS; no clean API; rate-limited scraping |
| **expected reliability** | Community-aggregated predictions; not ground truth |
| **integration complexity** | High — requires scraping + reliability calibration |

**Signals provided:**

| Signal # | Signal | Temporal class |
|----------|--------|
| 1 | Starting lineups (predicted) | `HISTORICAL_EVENT_ONLY` (no publication timestamps in archives) |
| 2 | Formations (predicted) | `HISTORICAL_EVENT_ONLY` |

**Assessment:** These sources provide **predicted** lineups (not confirmed),
published before the deadline — which is the correct temporal direction.
However, they are predictions, not ground truth, and most do not maintain
clean historical archives with publication timestamps. The value of a
predicted lineup signal depends entirely on the predictor's historical
accuracy, which would require extensive calibration against confirmed lineups
(from sources like FBref or TheStatsAPI).

---

## 4. Candidate Source Summary Table

| Source | Set-piece | Lineups | Formations | Positions | Manager | Historical | Temporal class | Access |
|--------|-----------|---------|------------|-----------|---------|------------|----------------|--------|
| FPL `players_raw.csv` (vaastav) | **Yes** (orders) | No | No | No (FPL pos only) | No | 2022-23–2025-26 | `UNSAFE_LOOKAHEAD` | Free, cached |
| FPL `gws/gwN.csv` (vaastav) | No | Yes (outcome) | No | Yes (FPL pos) | No | 2022-23–2025-26 | `HISTORICAL_OUTCOME_ONLY` | Free, cached |
| FPL `fixtures.csv` (vaastav) | No | No | No | No | No | 2022-23–2025-26 | `STRICT_BACKTEST_SAFE` | Free, cached |
| FPL `teams.csv` (vaastav) | No | No | No | No | No | 2022-23–2025-26 | `HISTORICAL_OUTCOME_ONLY` | Free, cached |
| Official FPL API | Yes (orders) | No (FPL XI only) | No | No | No | Live only | `LIVE_ONLY` | Free, undocumented |
| FBref match reports | No | Yes (confirmed) | Yes | Yes | No | 2010-present | `HISTORICAL_EVENT_ONLY` | Scrape, ToU risk |
| football-data.org | No | Conditional | No | No | No | Tier-dependent | `HISTORICAL_EVENT_ONLY` | Free tier, paid for lineups |
| TheStatsAPI | No | Yes (confirmed) | Yes | Yes | No | 10+ years | `HISTORICAL_EVENT_ONLY` | Paid ($50+/mo) |
| Transfermarkt | No | No | No | No | No | Broad | `HISTORICAL_EVENT_ONLY` | Scrape, ToS |
| Club archives | No | Yes (real XI) | Yes | Yes | Yes | Varies | Would be `STRICT_BACKTEST_SAFE` | Scrape, ToS |
| Community predictors | No | Predicted | Predicted | No | No | Limited | `HISTORICAL_EVENT_ONLY` | Scrape, ToS |

---

## 5. Temporal Field Representation Per Source

### Currently available project sources

| Source | `event_time` | `published_at` | `available_at` | `ingested_at` |
|--------|-------------|----------------|----------------|---------------|
| FPL `players_raw.csv` | — | `news_added` (look-ahead) | — | — |
| FPL `gws/gwN.csv` | `kickoff_time` | — | — | — |
| FPL `fixtures.csv` | `kickoff_time` | — | — | — |
| FPL `teams.csv` | — | — | — | — |
| Official FPL API | `kickoff_time` | — | — | — |

### Candidate additional sources

| Source | `event_time` | `published_at` | `available_at` | `ingested_at` |
|--------|-------------|----------------|----------------|---------------|
| FBref match reports | Match kickoff | No (post-match publication) | No | At scrape time |
| football-data.org | Fixture kickoff | No | No | At API call time |
| TheStatsAPI | Fixture kickoff | No | No | At API call time |
| Transfermarkt | Injury event date | No | No | At scrape time |
| Club archives | Press conference time | Yes (article timestamp) | Yes (inferred from conference timing) | At scrape time |
| Community predictors | Prediction deadline | Article publication time | Article publication time | At scrape time |

**Key finding:** The only source that *could* provide a true
`published_at`/`available_at` before the FPL deadline is club press conference
archives (press conferences held 1–2 days before matches). All other sources
either publish post-match (FBref, TheStatsAPI, football-data.org) or are
season-end snapshots (vaastav mirror).

---

## 6. Integration Architecture Assessment

The existing Phase 7 availability infrastructure provides a clear extension
pattern for Phase 8 tactical data:

```
SOURCE → RAW DATA → EVIDENCE → TACTICAL_EVENT → CONFIDENCE → TACTICAL_FEATURE → MODEL UPDATE
```

The `HistoricalAvailabilityProvider` ABC in
`src/fpl_intelligence/availability/historical/providers.py` already defines
the adapter contract (`provider_name`, `source_name`, `seasons_covered`,
`fetch_events`, `environment`). A `HistoricalTacticalProvider` ABC following
the same pattern would allow:

- `FBrefLineupProvider` — implements the ABC, scrapes FBref match reports
- `TheStatsAPILineupProvider` — implements the ABC, queries TheStatsAPI
- `ClubArchiveTacticalProvider` — implements the ABC, scrapes club sites

Each would produce raw event dicts with `provider`, `season_code`, `player_id`,
`team_id`, `event_type`, `status`, `description`, `timestamps`
(`event_time`/`published_at`/`available_at`/`ingested_at`), `source_name`,
`reliability`.

The existing `TemporalClass` enum
(`availability/models.py:TemporalClass`) already supports
`STRICT_BACKTEST_SAFE`, `HISTORICAL_EVENT_ONLY`, `UNSAFE_LOOKAHEAD`, and
`UNKNOWN` — covering all candidate source temporal classifications.

The entity-resolution infrastructure (`entity_resolution/resolver.py`,
`PlayerExternalId` keyed by `(provider, provider_player_id)`) can map any
provider's player IDs to canonical IDs, provided the provider key is
consistent — the Phase 7 `real_fpl` vs `real_fpl_bootstrap` mismatch must not
be repeated.

The existing `FeatureCache` and `TemporalQueryBuilder`
(`features/temporal.py`) enforce no-look-ahead via `InformationAccessPolicy`
and cutoff-time-inclusive cache keys — these would apply equally to tactical
features.

---

## 7. Signal-by-Signal Assessment

### Empirically testable with current data (engineering-only, not validated)

| Signal | Current data | Verdict |
|--------|-------------|---------|
| Set-piece takers (penalties) | `players_raw.csv` `penalties_order` | Engineering-only — data is real but `UNSAFE_LOOKAHEAD` (season-end snapshot, no per-GW timing) |
| Set-piece takers (free kicks) | `players_raw.csv` `direct_freekicks_order` | Engineering-only — same temporal limitation |
| Set-piece takers (corners) | `players_raw.csv` `corners_and_indirect_freekicks_order` | Engineering-only — same temporal limitation |
| Starting lineups (outcome) | `gws/gwN.csv` `starts` column | Engineering-only — outcome data, not pre-deadline signal |
| Player positions (FPL position) | `gws/gwN.csv` `position` column | Engineering-only — FPL roster position, not tactical position |
| Opponent matchup context | `fixtures.csv` difficulty ratings | Already implemented in `fixture_features.py` — proxy only, no tactical style |

### Engineering-only until live data accumulation begins

| Signal | Why engineering-only |
|--------|---------------------|
| Confirmed starting lineups (pre-deadline) | No historical source with pre-deadline confirmed XI; FBref/TheStatsAPI are post-match |
| Formations | No historical source with per-match formations + pre-deadline timing |
| Player tactical positions | FPL `position` is roster position (GK/DEF/MID/FWD), not on-pitch tactical role |
| Player role changes | No historical source tracking role shifts; requires lineup/formation data |
| Manager changes | No manager data in any available source |
| Manager formation tendencies | No manager data in any available source |
| Manager rotation tendencies | No lineup data to infer rotation patterns |
| Team style indicators | FPL team strength fields are season-end aggregates, not tactical style |
| Opponent tactical matchup | Requires formation + style data from sources 3.2/3.3/3.4 |
| Minutes risk from role changes | Requires confirmed lineups + position data from sources 3.2/3.3/3.4 |
| Differential from tactical shifts | Requires all above + confirmed-outcome validation |

---

## 8. Conclusion

**No Phase 8 tactical signal is currently empirically testable** with existing
project data. The vaastav mirror provides real set-piece order data and
outcome-based lineup data, but all of it is either:

1. `UNSAFE_LOOKAHEAD` (season-end snapshots — `players_raw.csv`), or
2. `HISTORICAL_OUTCOME_ONLY` (post-match outcomes — `gws/gwN.csv`), or
3. `STRICT_BACKTEST_SAFE` but a proxy, not tactical intelligence
   (`fixtures.csv` difficulty ratings — already implemented as a feature).

The set-piece order columns in `players_raw.csv` are the closest candidate:
they are real, FPL-published, and map cleanly to the canonical player ID
schema. However, they are season-end snapshots with no per-gameweek timing,
making them `UNSAFE_LOOKAHEAD` for strict backtesting.

The candidate sources (FBref, football-data.org, TheStatsAPI, club archives)
could provide confirmed lineups, formations, and tactical positions, but:

- **FBref and TheStatsAPI** publish post-match — making them
  `HISTORICAL_EVENT_ONLY` at best (no pre-deadline publication timestamp).
- **Club archives** could theoretically be `STRICT_BACKTEST_SAFE` (press
  conferences held 1–2 days before matches) but require per-club scraping
  infrastructure with no existing clean bulk archive.
- **football-data.org** has lineup data but on paid tiers and with no
  publication timestamps.
- **TheStatsAPI** is a paid commercial API with lineup/formation data but
  no pre-deadline publication timing.

**The fundamental gap:** No available or candidate source provides
**pre-deadline confirmed tactical information** (lineups, formations, tactical
positions) with **publication timestamps** that can be mapped against
gameweek deadlines. Club press conferences are the only theoretical source
that could fill this gap, but they are `LIVE_ONLY` for the current season and
require archival infrastructure for historical use.

Phase 8 remains in scope-audit-only mode. No tactical models should be
implemented until a `STRICT_BACKTEST_SAFE` source for at least one signal is
identified and validated.
