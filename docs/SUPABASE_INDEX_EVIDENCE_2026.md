# Supabase index evidence — 2026
Captured: 2026-09-02T22:15:08.518958+00:00

Host: aws-1-eu-west-3.pooler.supabase.com:5432/postgres

## Row counts (live)

| table | exists | rows |
|---|:---:|---:|
| `predictions_current` | yes | 10325 |
| `availability_events` | yes | 1659 |
| `availability_sources` | yes | 1 |
| `player_team_memberships` | yes | 3889 |
| `fixtures` | yes | 1900 |
| `gameweeks` | yes | 189 |
| `seasons` | yes | 5 |
| `transfer_log` | yes | 8 |
| `players` | yes | 1466 |
| `teams` | yes | 24 |

## Existing indexes (live)

### `predictions_current`

| index | definition |
|---|---|
| `ix_predictions_current_element` | CREATE INDEX ix_predictions_current_element ON public.predictions_current USING btree (element_id) |
| `uq_pred_current_gw_element` | CREATE UNIQUE INDEX uq_pred_current_gw_element ON public.predictions_current USING btree (gameweek, element_id) |

### `availability_events`

| index | definition |
|---|---|
| `availability_events_pkey` | CREATE UNIQUE INDEX availability_events_pkey ON public.availability_events USING btree (id) |
| `ix_availability_events_gameweek_id` | CREATE INDEX ix_availability_events_gameweek_id ON public.availability_events USING btree (gameweek_id) |
| `ix_availability_events_is_current` | CREATE INDEX ix_availability_events_is_current ON public.availability_events USING btree (is_current) |
| `ix_availability_events_player_id` | CREATE INDEX ix_availability_events_player_id ON public.availability_events USING btree (player_id) |
| `ix_availability_events_season_id` | CREATE INDEX ix_availability_events_season_id ON public.availability_events USING btree (season_id) |
| `ix_availability_events_valid_from` | CREATE INDEX ix_availability_events_valid_from ON public.availability_events USING btree (valid_from) |
| `ix_availability_events_valid_to` | CREATE INDEX ix_availability_events_valid_to ON public.availability_events USING btree (valid_to) |
| `ix_events_player_season_current` | CREATE INDEX ix_events_player_season_current ON public.availability_events USING btree (player_id, season_id, is_current) |
| `ix_events_status_validfrom` | CREATE INDEX ix_events_status_validfrom ON public.availability_events USING btree (valid_from, valid_to) |
| `ix_events_temporal_provider` | CREATE INDEX ix_events_temporal_provider ON public.availability_events USING btree (temporal_class, provider) |

### `player_team_memberships`

| index | definition |
|---|---|
| `ix_player_team_memberships_player_id` | CREATE INDEX ix_player_team_memberships_player_id ON public.player_team_memberships USING btree (player_id) |
| `ix_player_team_memberships_season_id` | CREATE INDEX ix_player_team_memberships_season_id ON public.player_team_memberships USING btree (season_id) |
| `ix_player_team_memberships_team_id` | CREATE INDEX ix_player_team_memberships_team_id ON public.player_team_memberships USING btree (team_id) |
| `player_team_memberships_pkey` | CREATE UNIQUE INDEX player_team_memberships_pkey ON public.player_team_memberships USING btree (id) |
| `uq_player_team_season` | CREATE UNIQUE INDEX uq_player_team_season ON public.player_team_memberships USING btree (player_id, team_id, season_id, valid_from) |

### `fixtures`

| index | definition |
|---|---|
| `fixtures_pkey` | CREATE UNIQUE INDEX fixtures_pkey ON public.fixtures USING btree (id) |
| `ix_fixtures_away_team_id` | CREATE INDEX ix_fixtures_away_team_id ON public.fixtures USING btree (away_team_id) |
| `ix_fixtures_gameweek_id` | CREATE INDEX ix_fixtures_gameweek_id ON public.fixtures USING btree (gameweek_id) |
| `ix_fixtures_home_team_id` | CREATE INDEX ix_fixtures_home_team_id ON public.fixtures USING btree (home_team_id) |
| `ix_fixtures_season_id` | CREATE INDEX ix_fixtures_season_id ON public.fixtures USING btree (season_id) |
| `uq_fixture_season_provider` | CREATE UNIQUE INDEX uq_fixture_season_provider ON public.fixtures USING btree (season_id, provider_fixture_id) |

### `gameweeks`

| index | definition |
|---|---|
| `gameweeks_pkey` | CREATE UNIQUE INDEX gameweeks_pkey ON public.gameweeks USING btree (id) |
| `ix_gameweeks_season_id` | CREATE INDEX ix_gameweeks_season_id ON public.gameweeks USING btree (season_id) |
| `uq_gameweek_season_event` | CREATE UNIQUE INDEX uq_gameweek_season_event ON public.gameweeks USING btree (season_id, provider_event_id) |

### `transfer_log`

| index | definition |
|---|---|
| `ix_transfer_log_entry_id` | CREATE INDEX ix_transfer_log_entry_id ON public.transfer_log USING btree (entry_id) |
| `ix_transfer_log_gameweek` | CREATE INDEX ix_transfer_log_gameweek ON public.transfer_log USING btree (gameweek) |
| `transfer_log_pkey` | CREATE UNIQUE INDEX transfer_log_pkey ON public.transfer_log USING btree (id) |
| `uq_transfer_entry_tid` | CREATE UNIQUE INDEX uq_transfer_entry_tid ON public.transfer_log USING btree (entry_id, transfer_id) |

## EXPLAIN ANALYZE for hot queries

### Q1: predictions_current lookup by gameweek (planner/targets/league routes)

```sql
SELECT element_id, expected_points
FROM predictions_current
WHERE gameweek = 1
```

```json
[
  {
    "Plan": {
      "Node Type": "Index Scan",
      "Parallel Aware": false,
      "Async Capable": false,
      "Scan Direction": "Forward",
      "Index Name": "uq_pred_current_gw_element",
      "Relation Name": "predictions_current",
      "Alias": "predictions_current",
      "Startup Cost": 0.29,
      "Total Cost": 265.77,
      "Plan Rows": 1475,
      "Plan Width": 12,
      "Actual Startup Time": 2.833,
      "Actual Total Time": 28.22,
      "Actual Rows": 1475,
      "Actual Loops": 1,
      "Index Cond": "(gameweek = 1)",
      "Rows Removed by Index Recheck": 0,
      "Shared Hit Blocks": 40,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning": {
      "Shared Hit Blocks": 8,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning Time": 1.753,
    "Triggers": [],
    "Execution Time": 28.31
  }
]
```

### Q2: predictions_current last-computed (data sources endpoint)

```sql
SELECT computed_at
FROM predictions_current
ORDER BY computed_at DESC
LIMIT 1
```

```json
[
  {
    "Plan": {
      "Node Type": "Limit",
      "Parallel Aware": false,
      "Async Capable": false,
      "Startup Cost": 499.88,
      "Total Cost": 499.88,
      "Plan Rows": 1,
      "Plan Width": 8,
      "Actual Startup Time": 156.24,
      "Actual Total Time": 156.241,
      "Actual Rows": 1,
      "Actual Loops": 1,
      "Shared Hit Blocks": 348,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0,
      "Plans": [
        {
          "Node Type": "Sort",
          "Parent Relationship": "Outer",
          "Parallel Aware": false,
          "Async Capable": false,
          "Startup Cost": 499.88,
          "Total Cost": 525.69,
          "Plan Rows": 10325,
          "Plan Width": 8,
          "Actual Startup Time": 156.238,
          "Actual Total Time": 156.239,
          "Actual Rows": 1,
          "Actual Loops": 1,
          "Sort Key": [
            "computed_at DESC"
          ],
          "Sort Method": "top-N heapsort",
          "Sort Space Used": 25,
          "Sort Space Type": "Memory",
          "Shared Hit Blocks": 348,
          "Shared Read Blocks": 0,
          "Shared Dirtied Blocks": 0,
          "Shared Written Blocks": 0,
          "Local Hit Blocks": 0,
          "Local Read Blocks": 0,
          "Local Dirtied Blocks": 0,
          "Local Written Blocks": 0,
          "Temp Read Blocks": 0,
          "Temp Written Blocks": 0,
          "Plans": [
            {
              "Node Type": "Seq Scan",
              "Parent Relationship": "Outer",
              "Parallel Aware": false,
              "Async Capable": false,
              "Relation Name": "predictions_current",
              "Alias": "predictions_current",
              "Startup Cost": 0.0,
              "Total Cost": 448.25,
              "Plan Rows": 10325,
              "Plan Width": 8,
              "Actual Startup Time": 1.022,
              "Actual Total Time": 155.614,
              "Actual Rows": 10325,
              "Actual Loops": 1,
              "Shared Hit Blocks": 345,
              "Shared Read Blocks": 0,
              "Shared Dirtied Blocks": 0,
              "Shared Written Blocks": 0,
              "Local Hit Blocks": 0,
              "Local Read Blocks": 0,
              "Local Dirtied Blocks": 0,
              "Local Written Blocks": 0,
              "Temp Read Blocks": 0,
              "Temp Written Blocks": 0
            }
          ]
        }
      ]
    },
    "Planning": {
      "Shared Hit Blocks": 17,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning Time": 2.765,
    "Triggers": [],
    "Execution Time": 156.266
  }
]
```

### Q3: predictions_current existence + counts per gameweek

```sql
SELECT gameweek, COUNT(*)
FROM predictions_current
GROUP BY gameweek
ORDER BY gameweek
```

```json
[
  {
    "Plan": {
      "Node Type": "Aggregate",
      "Strategy": "Sorted",
      "Partial Mode": "Simple",
      "Parallel Aware": false,
      "Async Capable": false,
      "Startup Cost": 0.29,
      "Total Cost": 272.85,
      "Plan Rows": 7,
      "Plan Width": 12,
      "Actual Startup Time": 1.012,
      "Actual Total Time": 21.623,
      "Actual Rows": 7,
      "Actual Loops": 1,
      "Group Key": [
        "gameweek"
      ],
      "Shared Hit Blocks": 60,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0,
      "Plans": [
        {
          "Node Type": "Index Only Scan",
          "Parent Relationship": "Outer",
          "Parallel Aware": false,
          "Async Capable": false,
          "Scan Direction": "Forward",
          "Index Name": "uq_pred_current_gw_element",
          "Relation Name": "predictions_current",
          "Alias": "predictions_current",
          "Startup Cost": 0.29,
          "Total Cost": 221.16,
          "Plan Rows": 10325,
          "Plan Width": 4,
          "Actual Startup Time": 0.01,
          "Actual Total Time": 20.646,
          "Actual Rows": 10325,
          "Actual Loops": 1,
          "Heap Fetches": 0,
          "Shared Hit Blocks": 60,
          "Shared Read Blocks": 0,
          "Shared Dirtied Blocks": 0,
          "Shared Written Blocks": 0,
          "Local Hit Blocks": 0,
          "Local Read Blocks": 0,
          "Local Dirtied Blocks": 0,
          "Local Written Blocks": 0,
          "Temp Read Blocks": 0,
          "Temp Written Blocks": 0
        }
      ]
    },
    "Planning": {
      "Shared Hit Blocks": 4,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning Time": 0.085,
    "Triggers": [],
    "Execution Time": 21.659
  }
]
```

### Q4: availability_events joined to source by primary_source_id (DBAvailabilityProvider)

```sql
SELECT s.name, s.reliability, s.is_official_club
FROM availability_events e
JOIN availability_sources s ON s.id = e.primary_source_id
WHERE e.player_id = 1
```

```json
[
  {
    "Plan": {
      "Node Type": "Nested Loop",
      "Parallel Aware": false,
      "Async Capable": false,
      "Join Type": "Inner",
      "Startup Cost": 1.55,
      "Total Cost": 8.04,
      "Plan Rows": 3,
      "Plan Width": 223,
      "Actual Startup Time": 1.058,
      "Actual Total Time": 1.059,
      "Actual Rows": 0,
      "Actual Loops": 1,
      "Inner Unique": true,
      "Shared Hit Blocks": 2,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0,
      "Plans": [
        {
          "Node Type": "Bitmap Heap Scan",
          "Parent Relationship": "Outer",
          "Parallel Aware": false,
          "Async Capable": false,
          "Relation Name": "availability_events",
          "Alias": "e",
          "Startup Cost": 1.4,
          "Total Cost": 4.65,
          "Plan Rows": 3,
          "Plan Width": 4,
          "Actual Startup Time": 1.058,
          "Actual Total Time": 1.058,
          "Actual Rows": 0,
          "Actual Loops": 1,
          "Recheck Cond": "(player_id = 1)",
          "Rows Removed by Index Recheck": 0,
          "Exact Heap Blocks": 0,
          "Lossy Heap Blocks": 0,
          "Shared Hit Blocks": 2,
          "Shared Read Blocks": 0,
          "Shared Dirtied Blocks": 0,
          "Shared Written Blocks": 0,
          "Local Hit Blocks": 0,
          "Local Read Blocks": 0,
          "Local Dirtied Blocks": 0,
          "Local Written Blocks": 0,
          "Temp Read Blocks": 0,
          "Temp Written Blocks": 0,
          "Plans": [
            {
              "Node Type": "Bitmap Index Scan",
              "Parent Relationship": "Outer",
              "Parallel Aware": false,
              "Async Capable": false,
              "Index Name": "ix_events_player_season_current",
              "Startup Cost": 0.0,
              "Total Cost": 1.4,
              "Plan Rows": 3,
              "Plan Width": 0,
              "Actual Startup Time": 1.053,
              "Actual Total Time": 1.053,
              "Actual Rows": 0,
              "Actual Loops": 1,
              "Index Cond": "(player_id = 1)",
              "Shared Hit Blocks": 2,
              "Shared Read Blocks": 0,
              "Shared Dirtied Blocks": 0,
              "Shared Written Blocks": 0,
              "Local Hit Blocks": 0,
              "Local Read Blocks": 0,
              "Local Dirtied Blocks": 0,
              "Local Written Blocks": 0,
              "Temp Read Blocks": 0,
              "Temp Written Blocks": 0
            }
          ]
        },
        {
          "Node Type": "Memoize",
          "Parent Relationship": "Inner",
          "Parallel Aware": false,
          "Async Capable": false,
          "Startup Cost": 0.15,
          "Total Cost": 2.0,
          "Plan Rows": 1,
          "Plan Width": 227,
          "Actual Startup Time": 0.0,
          "Actual Total Time": 0.0,
          "Actual Rows": 0,
          "Actual Loops": 0,
          "Cache Key": "e.primary_source_id",
          "Cache Mode": "logical",
          "Shared Hit Blocks": 0,
          "Shared Read Blocks": 0,
          "Shared Dirtied Blocks": 0,
          "Shared Written Blocks": 0,
          "Local Hit Blocks": 0,
          "Local Read Blocks": 0,
          "Local Dirtied Blocks": 0,
          "Local Written Blocks": 0,
          "Temp Read Blocks": 0,
          "Temp Written Blocks": 0,
          "Plans": [
            {
              "Node Type": "Index Scan",
              "Parent Relationship": "Outer",
              "Parallel Aware": false,
              "Async Capable": false,
              "Scan Direction": "Forward",
              "Index Name": "availability_sources_pkey",
              "Relation Name": "availability_sources",
              "Alias": "s",
              "Startup Cost": 0.14,
              "Total Cost": 1.99,
              "Plan Rows": 1,
              "Plan Width": 227,
              "Actual Startup Time": 0.0,
              "Actual Total Time": 0.0,
              "Actual Rows": 0,
              "Actual Loops": 0,
              "Index Cond": "(id = e.primary_source_id)",
              "Rows Removed by Index Recheck": 0,
              "Shared Hit Blocks": 0,
              "Shared Read Blocks": 0,
              "Shared Dirtied Blocks": 0,
              "Shared Written Blocks": 0,
              "Local Hit Blocks": 0,
              "Local Read Blocks": 0,
              "Local Dirtied Blocks": 0,
              "Local Written Blocks": 0,
              "Temp Read Blocks": 0,
              "Temp Written Blocks": 0
            }
          ]
        }
      ]
    },
    "Planning": {
      "Shared Hit Blocks": 55,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning Time": 4.98,
    "Triggers": [],
    "Execution Time": 1.096
  }
]
```

### Q5: availability_events range by valid_from (cutoff lookups)

```sql
SELECT id, player_id, primary_source_id, valid_from, valid_to
FROM availability_events
WHERE valid_from <= NOW()
  AND (valid_to IS NULL OR valid_to > NOW())
```

```json
[
  {
    "Plan": {
      "Node Type": "Seq Scan",
      "Parallel Aware": false,
      "Async Capable": false,
      "Relation Name": "availability_events",
      "Alias": "availability_events",
      "Startup Cost": 0.0,
      "Total Cost": 65.18,
      "Plan Rows": 1659,
      "Plan Width": 28,
      "Actual Startup Time": 1.607,
      "Actual Total Time": 12.436,
      "Actual Rows": 1659,
      "Actual Loops": 1,
      "Filter": "((valid_from <= now()) AND ((valid_to IS NULL) OR (valid_to > now())))",
      "Rows Removed by Filter": 0,
      "Shared Hit Blocks": 32,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning": {
      "Shared Hit Blocks": 3,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning Time": 0.116,
    "Triggers": [],
    "Execution Time": 12.528
  }
]
```

### Q6: player_team_memberships latest membership for one player (gameweek_resolve)

```sql
SELECT team_id
FROM player_team_memberships
WHERE player_id = 1
ORDER BY valid_from DESC NULLS LAST
LIMIT 1
```

```json
[
  {
    "Plan": {
      "Node Type": "Limit",
      "Parallel Aware": false,
      "Async Capable": false,
      "Startup Cost": 1.45,
      "Total Cost": 1.45,
      "Plan Rows": 1,
      "Plan Width": 12,
      "Actual Startup Time": 1.24,
      "Actual Total Time": 1.241,
      "Actual Rows": 1,
      "Actual Loops": 1,
      "Shared Hit Blocks": 3,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0,
      "Plans": [
        {
          "Node Type": "Sort",
          "Parent Relationship": "Outer",
          "Parallel Aware": false,
          "Async Capable": false,
          "Startup Cost": 1.45,
          "Total Cost": 1.46,
          "Plan Rows": 3,
          "Plan Width": 12,
          "Actual Startup Time": 1.239,
          "Actual Total Time": 1.239,
          "Actual Rows": 1,
          "Actual Loops": 1,
          "Sort Key": [
            "valid_from DESC NULLS LAST"
          ],
          "Sort Method": "quicksort",
          "Sort Space Used": 25,
          "Sort Space Type": "Memory",
          "Shared Hit Blocks": 3,
          "Shared Read Blocks": 0,
          "Shared Dirtied Blocks": 0,
          "Shared Written Blocks": 0,
          "Local Hit Blocks": 0,
          "Local Read Blocks": 0,
          "Local Dirtied Blocks": 0,
          "Local Written Blocks": 0,
          "Temp Read Blocks": 0,
          "Temp Written Blocks": 0,
          "Plans": [
            {
              "Node Type": "Index Only Scan",
              "Parent Relationship": "Outer",
              "Parallel Aware": false,
              "Async Capable": false,
              "Scan Direction": "Forward",
              "Index Name": "uq_player_team_season",
              "Relation Name": "player_team_memberships",
              "Alias": "player_team_memberships",
              "Startup Cost": 0.28,
              "Total Cost": 1.43,
              "Plan Rows": 3,
              "Plan Width": 12,
              "Actual Startup Time": 1.232,
              "Actual Total Time": 1.234,
              "Actual Rows": 1,
              "Actual Loops": 1,
              "Index Cond": "(player_id = 1)",
              "Rows Removed by Index Recheck": 0,
              "Heap Fetches": 0,
              "Shared Hit Blocks": 3,
              "Shared Read Blocks": 0,
              "Shared Dirtied Blocks": 0,
              "Shared Written Blocks": 0,
              "Local Hit Blocks": 0,
              "Local Read Blocks": 0,
              "Local Dirtied Blocks": 0,
              "Local Written Blocks": 0,
              "Temp Read Blocks": 0,
              "Temp Written Blocks": 0
            }
          ]
        }
      ]
    },
    "Planning": {
      "Shared Hit Blocks": 3,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning Time": 0.1,
    "Triggers": [],
    "Execution Time": 1.267
  }
]
```

### Q7: fixtures lookup by gameweek_id + team_id (safe_fixture_count)

```sql
SELECT id
FROM fixtures
WHERE gameweek_id = 1
  AND postponed = false
  AND (home_team_id = 1 OR away_team_id = 1)
```

```json
[
  {
    "Plan": {
      "Node Type": "Index Scan",
      "Parallel Aware": false,
      "Async Capable": false,
      "Scan Direction": "Forward",
      "Index Name": "ix_fixtures_gameweek_id",
      "Relation Name": "fixtures",
      "Alias": "fixtures",
      "Startup Cost": 0.28,
      "Total Cost": 2.73,
      "Plan Rows": 1,
      "Plan Width": 4,
      "Actual Startup Time": 0.026,
      "Actual Total Time": 0.026,
      "Actual Rows": 0,
      "Actual Loops": 1,
      "Index Cond": "(gameweek_id = 1)",
      "Rows Removed by Index Recheck": 0,
      "Filter": "((NOT postponed) AND ((home_team_id = 1) OR (away_team_id = 1)))",
      "Rows Removed by Filter": 0,
      "Shared Hit Blocks": 2,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning": {
      "Shared Hit Blocks": 3,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning Time": 0.826,
    "Triggers": [],
    "Execution Time": 0.058
  }
]
```

### Q8: gameweek resolve by provider_event_id (gameweek_resolve.resolve_gameweek_id)

```sql
SELECT gw.id
FROM gameweeks gw
JOIN seasons s ON s.id = gw.season_id
WHERE gw.provider_event_id = 1
ORDER BY s.code DESC
LIMIT 1
```

```json
[
  {
    "Plan": {
      "Node Type": "Limit",
      "Parallel Aware": false,
      "Async Capable": false,
      "Startup Cost": 0.14,
      "Total Cost": 7.79,
      "Plan Rows": 1,
      "Plan Width": 62,
      "Actual Startup Time": 0.035,
      "Actual Total Time": 0.036,
      "Actual Rows": 1,
      "Actual Loops": 1,
      "Shared Hit Blocks": 4,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0,
      "Plans": [
        {
          "Node Type": "Nested Loop",
          "Parent Relationship": "Outer",
          "Parallel Aware": false,
          "Async Capable": false,
          "Join Type": "Inner",
          "Startup Cost": 0.14,
          "Total Cost": 30.71,
          "Plan Rows": 4,
          "Plan Width": 62,
          "Actual Startup Time": 0.034,
          "Actual Total Time": 0.034,
          "Actual Rows": 1,
          "Actual Loops": 1,
          "Inner Unique": true,
          "Join Filter": "(gw.season_id = s.id)",
          "Rows Removed by Join Filter": 0,
          "Shared Hit Blocks": 4,
          "Shared Read Blocks": 0,
          "Shared Dirtied Blocks": 0,
          "Shared Written Blocks": 0,
          "Local Hit Blocks": 0,
          "Local Read Blocks": 0,
          "Local Dirtied Blocks": 0,
          "Local Written Blocks": 0,
          "Temp Read Blocks": 0,
          "Temp Written Blocks": 0,
          "Plans": [
            {
              "Node Type": "Index Scan",
              "Parent Relationship": "Outer",
              "Parallel Aware": false,
              "Async Capable": false,
              "Scan Direction": "Backward",
              "Index Name": "seasons_code_key",
              "Relation Name": "seasons",
              "Alias": "s",
              "Startup Cost": 0.14,
              "Total Cost": 16.05,
              "Plan Rows": 180,
              "Plan Width": 62,
              "Actual Startup Time": 0.018,
              "Actual Total Time": 0.018,
              "Actual Rows": 1,
              "Actual Loops": 1,
              "Shared Hit Blocks": 2,
              "Shared Read Blocks": 0,
              "Shared Dirtied Blocks": 0,
              "Shared Written Blocks": 0,
              "Local Hit Blocks": 0,
              "Local Read Blocks": 0,
              "Local Dirtied Blocks": 0,
              "Local Written Blocks": 0,
              "Temp Read Blocks": 0,
              "Temp Written Blocks": 0
            },
            {
              "Node Type": "Materialize",
              "Parent Relationship": "Inner",
              "Parallel Aware": false,
              "Async Capable": false,
              "Startup Cost": 0.0,
              "Total Cost": 3.91,
              "Plan Rows": 4,
              "Plan Width": 8,
              "Actual Startup Time": 0.014,
              "Actual Total Time": 0.014,
              "Actual Rows": 1,
              "Actual Loops": 1,
              "Shared Hit Blocks": 2,
              "Shared Read Blocks": 0,
              "Shared Dirtied Blocks": 0,
              "Shared Written Blocks": 0,
              "Local Hit Blocks": 0,
              "Local Read Blocks": 0,
              "Local Dirtied Blocks": 0,
              "Local Written Blocks": 0,
              "Temp Read Blocks": 0,
              "Temp Written Blocks": 0,
              "Plans": [
                {
                  "Node Type": "Seq Scan",
                  "Parent Relationship": "Outer",
                  "Parallel Aware": false,
                  "Async Capable": false,
                  "Relation Name": "gameweeks",
                  "Alias": "gw",
                  "Startup Cost": 0.0,
                  "Total Cost": 3.89,
                  "Plan Rows": 4,
                  "Plan Width": 8,
                  "Actual Startup Time": 0.012,
                  "Actual Total Time": 0.012,
                  "Actual Rows": 1,
                  "Actual Loops": 1,
                  "Filter": "(provider_event_id = 1)",
                  "Rows Removed by Filter": 0,
                  "Shared Hit Blocks": 2,
                  "Shared Read Blocks": 0,
                  "Shared Dirtied Blocks": 0,
                  "Shared Written Blocks": 0,
                  "Local Hit Blocks": 0,
                  "Local Read Blocks": 0,
                  "Local Dirtied Blocks": 0,
                  "Local Written Blocks": 0,
                  "Temp Read Blocks": 0,
                  "Temp Written Blocks": 0
                }
              ]
            }
          ]
        }
      ]
    },
    "Planning": {
      "Shared Hit Blocks": 19,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning Time": 2.043,
    "Triggers": [],
    "Execution Time": 0.072
  }
]
```

### Q9: transfer_log recent by entry + gameweek (transfer decisions path)

```sql
SELECT element_in, element_out, gameweek, created_at
FROM transfer_log
WHERE entry_id = '1'
ORDER BY gameweek DESC, created_at DESC
LIMIT 20
```

```json
[
  {
    "Plan": {
      "Node Type": "Limit",
      "Parallel Aware": false,
      "Async Capable": false,
      "Startup Cost": 2.37,
      "Total Cost": 2.37,
      "Plan Rows": 1,
      "Plan Width": 20,
      "Actual Startup Time": 0.033,
      "Actual Total Time": 0.034,
      "Actual Rows": 0,
      "Actual Loops": 1,
      "Shared Hit Blocks": 4,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0,
      "Plans": [
        {
          "Node Type": "Sort",
          "Parent Relationship": "Outer",
          "Parallel Aware": false,
          "Async Capable": false,
          "Startup Cost": 2.37,
          "Total Cost": 2.37,
          "Plan Rows": 1,
          "Plan Width": 20,
          "Actual Startup Time": 0.033,
          "Actual Total Time": 0.033,
          "Actual Rows": 0,
          "Actual Loops": 1,
          "Sort Key": [
            "gameweek DESC",
            "created_at DESC"
          ],
          "Sort Method": "quicksort",
          "Sort Space Used": 25,
          "Sort Space Type": "Memory",
          "Shared Hit Blocks": 4,
          "Shared Read Blocks": 0,
          "Shared Dirtied Blocks": 0,
          "Shared Written Blocks": 0,
          "Local Hit Blocks": 0,
          "Local Read Blocks": 0,
          "Local Dirtied Blocks": 0,
          "Local Written Blocks": 0,
          "Temp Read Blocks": 0,
          "Temp Written Blocks": 0,
          "Plans": [
            {
              "Node Type": "Index Scan",
              "Parent Relationship": "Outer",
              "Parallel Aware": false,
              "Async Capable": false,
              "Scan Direction": "Forward",
              "Index Name": "ix_transfer_log_entry_id",
              "Relation Name": "transfer_log",
              "Alias": "transfer_log",
              "Startup Cost": 0.14,
              "Total Cost": 2.36,
              "Plan Rows": 1,
              "Plan Width": 20,
              "Actual Startup Time": 0.02,
              "Actual Total Time": 0.02,
              "Actual Rows": 0,
              "Actual Loops": 1,
              "Index Cond": "((entry_id)::text = '1'::text)",
              "Rows Removed by Index Recheck": 0,
              "Shared Hit Blocks": 1,
              "Shared Read Blocks": 0,
              "Shared Dirtied Blocks": 0,
              "Shared Written Blocks": 0,
              "Local Hit Blocks": 0,
              "Local Read Blocks": 0,
              "Local Dirtied Blocks": 0,
              "Local Written Blocks": 0,
              "Temp Read Blocks": 0,
              "Temp Written Blocks": 0
            }
          ]
        }
      ]
    },
    "Planning": {
      "Shared Hit Blocks": 4,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning Time": 0.1,
    "Triggers": [],
    "Execution Time": 0.054
  }
]
```

## PredictionsCurrent identity check

PK / UNIQUE constraints on `predictions_current`:

- `uq_pred_current_gw_element` (u)

Duplicate (element_id, gameweek) groups in `predictions_current`: 0
