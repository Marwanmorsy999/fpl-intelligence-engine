# Stage 2A Historical Backfill — Dry Run Report

Status: **dry run passed 2026-08-28; sequential import in progress** (this document is
updated after each season import). Production database: canonical Supabase
PostgreSQL (Session Pooler, Psycopg 3). No schema changes; no existing rows
modified; only additive inserts for the three missing historical seasons via the
existing idempotent `import_season()` pipeline.

## SOURCE

* Provider: `RealFPLProvider` (`provider_name="real_fpl"`) — the repository's
  intended historical provider (`src/fpl_intelligence/providers/real_fpl.py`).
* Source: public `vaastav/Fantasy-Premier-League` mirror (raw.githubusercontent.com),
  fetched through `DiskCachingFetcher` (disk cache under `data/raw/real_fpl/`,
  hashed payloads, `RawRecord` provenance, 0.35s polite delay).
* Datasets used: `teams.csv`, `players_raw.csv`, `fixtures.csv`, `gws/gw{1..38}.csv`.

## SEASONS

`2022-23`, `2023-24`, `2024-25` — all three present in the provider and all three
are configured `DEVELOPMENT_SEASONS`/`VALIDATION_SEASONS` in
`src/fpl_intelligence/config/holdout.py` (the locked `2025-26` holdout is NOT
touched).

## DRY RUN (production, 2026-08-28, zero writes)

| Season | Teams | Players | Fixtures | FPL player-GW rows | Expected canonical player-GW rows | Rejected | Unmatched (teams/players) | Critical errors |
|---|---|---|---|---|---|---|---|---|
| 2022-23 | 20 | 778 | 380 | 26,505 | 24,957 | 0 | 0 / 0 | 0 |
| 2023-24 | 20 | 865 | 380 | 29,725 | 28,742 | 0 | 0 / 0 | 0 |
| 2024-25 | 20 | 804 | 380 | 27,605 | 27,231 | 0 | 0 / 0 | 0 |

Expected canonical rows are lower than raw FPL rows because the pipeline
aggregates the provider's multiple fixture rows per (player, gameweek) into the
canonical Gameweek-level outcome (double gameweeks summed, blank gameweeks
carry the Gameweek-level snapshot value) — existing semantics preserved.

## ENTITY RESOLUTION (identity architecture)

* Players: `PlayerExternalId(provider="real_fpl", provider_player_id=<FPL
  element id>)` — historical FPL element IDs are NEVER assumed equal to the
  live `official_fpl` IDs or internal canonical IDs; they live in a separate
  provider namespace. Unresolvable references are rejected by
  `validate_fpl_history` / `reconcile_fixtures` and counted in
  `ReconciliationReport` — never silently attached to another player.
* Teams: `TeamExternalId(provider="real_fpl", provider_team_id=<FPL team id>)`,
  with a name-based fallback for spelling/short-name differences, so renamed or
  differently-spelled clubs do not create duplicate teams.
* Known documented limitation: the same human player may exist as two canonical
  players across seasons if FPL re-issued their element id. Minutes-model
  features are within-season and gameweek-ordered, so this does not affect
  validation; no automatic cross-namespace identity merging was attempted
  (explicitly avoided to prevent silent mis-attachment).

## TEMPORAL SAFETY

* Historical outcome fields (minutes, points, goals, assists, cards, saves,
  bonus, BPS, ICT, xG/xA) — `OUTCOME_DATA_ONLY` (repository equivalent of
  `DatasetClass.HISTORICAL_OUTCOME_ONLY`): legitimate finalized Gameweek
  outcomes, usable as targets and as features for strictly later gameweeks via
  documented gameweek ordering. They are NOT pre-deadline intelligence.
* Gameweek-end snapshot fields (price, `selected`, transfers in/out) —
  `HISTORICAL_OUTCOME_ONLY` at gameweek-end timing; must never be treated as
  pre-deadline state. The mirror publishes no per-GW `selected_by_percent`
  (stored NULL, never fabricated).
* `ep_this` / `ep_next` — **UNSAFE_LOOKAHEAD**: the real provider emits `None`
  for both, `TrainingDataBuilder` features never read them, and a regression
  test asserts no `ep_*` key can appear in a strict feature vector.
* Player-match / team-match stats methods: unsupported by this provider by
  design (documented "unavailable"); not faked.

## PROVENANCE

* Every imported `PlayerGameweekPerformance` row stores `available_at` =
  `ingested_at` = the **gameweek-end reference**: the latest genuine
  `kickoff_time` of that gameweek's fixtures in the source. This is a real
  source timestamp (the earliest moment the gameweek's final outcome state
  could exist), NOT an invented publication time; the two stamps are equal so
  the `available_at <= ingested_at` invariant holds by construction.
* The actual retrieval time is recorded separately and honestly on
  `RawRecord.retrieved_at` and `IngestionRun.started_at/finished_at`.
* Snapshot rows without a genuine source `event_time` are dropped (never
  stamped with `now()`), preserving the `FPLSnapshot` unique key and
  idempotence.
* `Gameweek.deadline_time` is left NULL: the mirror publishes no historical
  FPL deadlines and fabricating them is forbidden. The validation design
  instead derives decision boundaries by **gameweek ordering**
  (`backtesting.cutoff._outcome_ordering_cutoff`): the cutoff for target GW n
  is the latest genuine kickoff of GW n-1's fixtures — strictly earlier than
  the unknown true deadline, so information access is only ever shrunk. The
  `deadline_time` column is never written with this derived value.

## IMPORT (updated sequentially after each season)

*IngestionRun rows below confirm each season was imported through the existing*
*idempotent `import_season()` pipeline, zero writes to anything else.*

### 2022-23 — SUCCESS (2026-08-28)
Accepted 26,885 · Rejected 0 · Duplicate candidates 0 · Critical errors 0.
Verify (canonical): season_id=8, teams_ext=20, players_ext=778, fixtures=380,
gameweeks=37, player_gameweek_performances=24,957, stamped_provenance=24,957,
invalid_available_gt_ingested=0, memberships=778, fpl_snapshots=26,505,
raw_records=6, ingestion_runs: DRY_RUN + SUCCESS(records_processed=26,885).
Gameweeks=37 is genuine: the 2022-23 GW7 was an entirely postponed round
(September 2022) — the upstream mirror ships an empty `gw7.csv` and no event-7
fixtures. Nothing was dropped; this is the real source state.

### 2023-24 — SUCCESS (2026-08-28)
Accepted 30,105 · Rejected 0 · Duplicates 0 · Critical errors 0.
Verify: season_id=9, teams_ext=20, players_ext=866, gameweeks=38, fixtures=380,
player_game_performances=28,742, stamped_provenance=28,742,
invalid_available_gt_ingested=0, memberships=865, fpl_snapshots=29,725,
raw_records=6.

### 2024-25 — SUCCESS (2026-08-28)
Accepted 27,985 · Rejected 0 · Duplicates 0 · Critical errors 0.
Verify: season_id=10, teams_ext=20, players_ext=866, gameweeks=38, fixtures=380,
player_game_performances=27,231, stamped_provenance=27,231,
invalid_available_gt_ingested=0, memberships=804, fpl_snapshots=27,605,
raw_records=6.

## CANONICAL COVERAGE (post-backfill)

| Season | Gameweeks | Teams (ext) | Players (ext) | Fixtures | Player-GW rows | Provenance-stamped | Memberships | Snapshots |
|---|---|---|---|---|---|---|---|---|
| 2022-23 | 37 | 20 | 778 | 380 | 24,957 | 24,957 | 778 | 26,505 |
| 2023-24 | 38 | 20 | 866 | 380 | 28,742 | 28,742 | 865 | 29,725 |
| 2024-25 | 38 | 20 | 866 | 380 | 27,231 | 27,231 | 804 | 27,605 |

`players_ext` counts `PlayerExternalId` rows (canonical players linked to the
`real_fpl` provider), shared across seasons for players whose FPL element id is
persistent.

## ENTITY RESOLUTION (results)

* Players: 0 unmatched / 0 rejected across all three seasons.
* Teams: 0 unmatched / 0 rejected.
* Fixtures: 0 unresolved / 0 duplicate candidates, all 380 fixtures linked to a
  Gameweek (`fixtures_gw_unlinked=0`) in every season.
* Identity is preserved via `TeamExternalId`/`PlayerExternalId`
  (`provider`-scoped); historical FPL IDs never collide with the live
  `official_fpl` namespace or internal canonical IDs.

## TEMPORAL SAFETY

* Strict-safe (gameweek-ordering) outcomes: minutes, points, goals, assists,
  cards, saves, bonus, BPS, ICT, xG/xA. Available only as Gameweek-final
  outcomes.
* Unsafe look-ahead: `ep_this`, `ep_next` — never ingested (provider emits
  `None`); regression test asserts they never appear in a strict feature
  vector.
* Gameweek-end snapshot fields (price, ownership, transfers) — not pre-deadline
  state; mirror has no per-GW `selected_by_percent` (stored NULL).
* Gameweek deadlines: **not fabricated**; the validation design derives
  decision boundaries from gameweek ordering (`_outcome_ordering_cutoff`), a
  strictly-conservative boundary that only shrinks information access.

## CURRENT-SEASON SAFETY

* 2026-27 `player_gameweek_performances` remains exactly **15,899** rows — the
  operator-verified pre-import baseline. No existing row was modified, deleted,
  or truncated.
* The pre-existing 2026-27 provenance condition (`available_at >
  ingested_at` on all 15,899 live-path rows) is **outside this stage's scope**
  (it originates in the live sync path, not historical ingestion) and is
  deliberately left untouched per the production rule. All 80,930 imported
  historical rows satisfy the `available_at <= ingested_at` invariant.

## TESTS

* New unit suite `tests/unit/test_stage2a7_backfill.py` (10 tests): provenance
  stamping, timestamp invariant, `import_season` idempotence, unknown-season
  handling double-GW aggregation summation, unknown-player quarantine, snapshot
  event-time no-fabrication, deadline-free cutoff derivation, deadline-based
  behavior-unchanged, next-gameweek fallback, and no-leakage (no `ep_*`
  features). All pass.
* Related suites (idempotent ingestion, reconciliation, snapshots, leakage,
  temporal queries/integrity, validation, Stage 2A minutes): all pass.
* Full backend unit suite `tests/unit`: **exit 0** (0 failures).
* `ruff` on all changed/new files: clean. `compileall`: clean. `mypy`: no new
  errors (existing baseline errors in `ingestion/historical.py` are pre-existing).
* Tests require no production credentials (in-memory SQLite).

## NEXT STEP

It is safe to run the existing Stage 2A minutes walk-forward validator
(`scripts/evaluate_minutes_walkforward.py`) against the now-populated canonical
database. Caveats the validator will surface, by design (not blockers):

* `Gameweek.deadline_time` is NULL for the backfilled seasons, so
  `get_all_gameweek_cutoffs` derives decision boundaries from gameweek ordering
  — the earliest gameweeks (e.g. GW2) have no prior gameweek data and are
  skipped, and the first folds use only strictly prior finalized outcomes.
* Early-gameweek folds after a blank round may have thin or empty samples.
* 2026-27 `invalid_timestamps=15899` is reported by the preflight and is the
  untouched live-path condition, not introduced here.

Per the production rule, the walk-forward validator was **not** run during this
stage; it will be run next only after inspecting the counts and temporal status
documented above.
