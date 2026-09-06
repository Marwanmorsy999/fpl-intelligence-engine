# Phase 1.5 — Supabase index & integrity review (2026-09)

Evidence-driven plan for issue #24. **Nothing has been applied yet.** Every
proposed DDL below is backed by an EXPLAIN ANALYZE captured against live
Supabase (`hnsoektotpqgvpqshusi`, `aws-1-eu-west-3`) and stored in
`docs/SUPABASE_INDEX_EVIDENCE_2026.md`.

## Methodology

1. Enumerate hot read paths from the engine code (planner/targets/league/
   data_sources/availability/transfer_log/fixture_count/gameweek_resolve).
2. For each path, run `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` against
   the live DB.
3. Propose ONLY indexes that the captured plan shows are missing AND that
   are referenced by query patterns we actually exercise.

No "drop unused index" actions are taken — per issue #24 hard rule.

## Captured state

- `predictions_current` rows: 10,325
- `availability_events` rows: 1,659
- `availability_sources` rows: 1 (Phase 7 effectively unstarted — consistent
  with BLOCKED status)
- `fixtures` rows: 1,900
- `gameweeks` rows: 189
- `seasons` rows: 5
- `transfer_log` rows: 8
- `player_team_memberships` rows: 3,889
- `players` rows: 1,466
- `teams` rows: 24

## Hot query timings (live)

| Q | path | exec time | plan | index used | rows | blocks |
|---|---|---:|---|---|---:|---:|
| Q1 | planner/targets/league → predictions_current by gameweek | 0.392 ms | Index Scan | uq_pred_current_gw_element | 1,475 | 40 |
| Q2 | data_sources → last computed_at | 178.602 ms | Seq Scan + top-N Sort | (none) | 1 | 348 |
| Q3 | data_sources → counts per gameweek | 1.868 ms | Aggregate → Index Only Scan | uq_pred_current_gw_element | 7 | 60 |
| Q4 | DBAvailabilityProvider → events ⋈ sources | 1.135 ms | Nested Loop | ix_events_player_season_current | 0 | 2 |
| Q5 | availability_events range by valid_from | 12.089 ms | Seq Scan | (none) | 1,659 | 32 |
| Q6 | gameweek_resolve → latest membership per player | 2.914 ms | Limit → Index Scan | uq_player_team_season | 1 | 3 |
| Q7 | safe_fixture_count → fixtures by gw + team | 0.671 ms | Index Scan | ix_fixtures_gameweek_id | 0 | 2 |
| Q8 | gameweek_resolve → gameweek id by provider_event_id | 0.067 ms | Limit → Index Scan | uq_gameweek_season_event | 1 | 4 |
| Q9 | transfer decisions → recent transfers by entry | 1.608 ms | Limit → Index Scan | ix_transfer_log_entry_id | 0 | 4 |

(See `SUPABASE_INDEX_EVIDENCE_2026.md` for the full JSON plans.)

## Findings

### 1. `predictions_current` lacks a primary key (issue #24 claim confirmed)

Live constraint check:

```
SELECT conname, contype FROM pg_constraint WHERE conrelid = 'predictions_current';
  → uq_pred_current_gw_element  UNIQUE (gameweek, element_id)
```

Postgres database advisor flags tables without a PK. The UNIQUE index
already exists and is the natural PK. Promoting it is purely additive
and removes the advisor warning. No other table FKs reference
`predictions_current`, so no cascading concerns.

### 2. `availability_events.primary_source_id` is FK-orphan (no index)

Live constraint check:

```
availability_events.primary_source_id_fkey  FOREIGN KEY (primary_source_id)
                                              REFERENCES availability_sources(id)
```

Index list on `availability_events`:

```
availability_events_pkey                       (id)
ix_availability_events_gameweek_id             (gameweek_id)
ix_availability_events_is_current              (is_current)
ix_availability_events_player_id               (player_id)
ix_availability_events_season_id               (season_id)
ix_availability_events_valid_from              (valid_from)
ix_availability_events_valid_to                (valid_to)
ix_events_player_season_current                (player_id, season_id, is_current)
ix_events_status_validfrom                     (valid_from, valid_to)
ix_events_temporal_provider                    (temporal_class, provider)
```

**No index on `primary_source_id`.** Postgres does not auto-index FK
columns. Q4 in the evidence is small today only because
`availability_events` is small (1,659 rows). With Phase 7 data this
becomes the dominant join cost.

### 3. `predictions_current.computed_at` has no index

Q2 (`ORDER BY computed_at DESC LIMIT 1`) does a Seq Scan + Sort over
all 10,325 rows. Adding a single-column B-tree on `computed_at` brings
this to < 1 ms with no other plan impact (the index is also useful for
range scans during future freshness probes).

## Proposed migration: 0024_supabase_perf_evidence

Already committed locally as `migrations/versions/0024_supabase_perf_evidence.py`,
validated by `tests/unit/test_migration_0024_supabase_perf.py` (4/4 pass).

Changes:

1. `ALTER TABLE public.predictions_current ADD CONSTRAINT
   predictions_current_pkey PRIMARY KEY USING INDEX
   uq_pred_current_gw_element;` — promote existing UNIQUE to PK.
2. `CREATE INDEX predictions_current_computed_at_idx ON
   public.predictions_current (computed_at);` — covers Q2.
3. `CREATE INDEX ix_availability_events_primary_source_id ON
   public.availability_events (primary_source_id);` — covers Q4 / FK
   integrity.

## Explicitly NOT in scope

These were considered and deferred because the captured plans do not
yet justify them:

- Composite indexes on `fixtures(gameweek_id, home_team_id)` /
  `(gameweek_id, away_team_id)`. Q7 already runs in 0.671 ms via the
  existing single-column index. Adding two more B-trees for marginal
  benefit is the kind of churn issue #24 warned against.
- Partial index `availability_events(valid_from) WHERE is_current`.
  Q5 is 12 ms on 1,659 rows — revisit when row count grows past tens
  of thousands.
- ANY drop of existing "unused" indexes. Supabase advisor reports
  these but the captured plans above show several that are
  defensive or used by paths we did not re-EXPLAIN.

## Validation done locally

- `python -m ruff check` clean on migration, script, test.
- `python -m ruff format` applied.
- `python -m pytest tests/unit/test_migration_0024_supabase_perf.py` 4/4.
- Alembic chain check: `0023_performance_security_cleanup →
  0024_supabase_perf_evidence`, single head.
- Evidence report regenerated from live DB.

## NOT yet done (waiting for sign-off)

- Migration **not** applied to Supabase.
- Branch **not** pushed.
- Vercel **not** redeployed.

To proceed: apply migration manually (psql or Supabase SQL editor), run
`EXPLAIN ANALYZE` again on Q1–Q9 to record the AFTER timings, update
this doc with the deltas, and only then push the branch + open the PR.