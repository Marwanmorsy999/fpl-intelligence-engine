# Availability Point-in-Time (PIT) status

## Scope

This branch adds an isolated point-in-time availability path using immutable
`Randdalf/fplcache` bootstrap snapshots. The snapshot capture timestamp is the
information-availability timestamp; football event time is never substituted.
Randdalf/fplcache documents the cache layout as `{year}/{month}/{day}/{time}.json.xz`
and says it is updated four times daily at six-hour intervals.

## Safety boundary

- `master` is unchanged by this work.
- Materialization is dry-run by default.
- `--import` targets the existing validation DB session factory only.
- `--commit` is allowed only after snapshot, chronology, signal, and entity-resolution gates pass inside the same transaction.
- The CI database import job is disabled unless repository variable `AVAILABILITY_PIT_ENABLE_IMPORT=true`.
- No live decision-chain wiring is included.

## Current components

| Component | Path | State |
|---|---|---|
| Immutable PIT provider | `src/fpl_intelligence/availability/historical/pit_fplcache.py` | Implemented |
| DB deadline loader | `src/fpl_intelligence/availability/historical/deadlines.py` | Implemented; skips missing DB deadlines rather than guessing |
| Verified deadline catalog | `src/fpl_intelligence/availability/historical/verified_deadlines.py` | Implemented; 10 source-provenanced cutoffs |
| Snapshot materializer | `src/fpl_intelligence/availability/historical/materialize_pit.py` | Implemented |
| Chronological gate | `src/fpl_intelligence/availability/historical/chronological.py` | Implemented, fail-closed |
| Minutes signal-lift | `src/fpl_intelligence/availability/historical/signal_lift.py` | Implemented with same-GW unflagged control |
| Validation DB evidence audit | `src/fpl_intelligence/availability/historical/pit_audit.py` + `scripts/audit_availability_pit.py` | Implemented |
| Dry-run evaluator | `scripts/evaluate_availability_pit.py` | Implemented |
| Controlled import | `scripts/materialize_availability_pit.py` | Implemented, opt-in |
| CI | `.github/workflows/availability-pit.yml` | Implemented |
| Unit tests | PIT provider/materializer/chronology/audit/control/deadline tests | Implemented |

## Source-provenanced deadline catalog

The validation branch now carries the first five FPL deadlines for both
2024-25 and 2025-26 in `verified_deadlines.py`. The catalog stores UTC cutoffs
and the Premier League publication URL used to verify each deadline. This is
validation metadata only; it does not alter database deadline rows.

The verified times are:

| Season | GW | Deadline (UTC) |
|---|---:|---|
| 2024-25 | 1 | 2024-08-16 17:30 |
| 2024-25 | 2 | 2024-08-24 10:00 |
| 2024-25 | 3 | 2024-08-31 10:00 |
| 2024-25 | 4 | 2024-09-14 10:00 |
| 2024-25 | 5 | 2024-09-21 10:00 |
| 2025-26 | 1 | 2025-08-15 17:30 |
| 2025-26 | 2 | 2025-08-22 17:30 |
| 2025-26 | 3 | 2025-08-30 10:00 |
| 2025-26 | 4 | 2025-09-13 10:00 |
| 2025-26 | 5 | 2025-09-20 10:00 |

The corresponding Premier League publications explicitly state the relevant
BST deadlines for these gameweeks, so the branch no longer relies on duplicated
hand-entered CI timestamps.

## Validation DB evidence observed on 2026-08-29

The connected validation Supabase project contains 5 seasons, 189 gameweeks,
1,466 players, 1,466 player external-ID mappings, and 126,167 player-gameweek
performance rows. It currently contains 210 `fplcache_pit` availability events:
91 for 2024-25 GW1 and 119 for 2025-26 GW1.

All 210 imported PIT rows are linked to gameweeks, classified
`STRICT_BACKTEST_SAFE`, and have a non-null `valid_from` timestamp. The realized
minutes join matches all 210 rows.

The provider emits flagged/non-default availability states rather than an
explicit `available` row for every player. The signal evaluator therefore uses
an unflagged player-gameweek control group from the same gameweek, avoiding a
false comparison against a category that the provider does not emit.

For the current two-GW1 sample:

| Season | Restricted N | Restricted mean min | Restricted 60+ start rate | Control N | Control mean min | Control 60+ start rate |
|---|---:|---:|---:|---:|---:|---:|
| 2024-25 | 90 | 1.3333 | 0.011111 | 526 | 37.1084 | 0.395437 |
| 2025-26 | 117 | 2.8462 | 0.025641 | 573 | 33.8115 | 0.364747 |
| Combined | 207 | 2.1884 | 0.019324 | 1,099 | 35.3894 | 0.379436 |

The hard-OUT sanity check is also strong: the 152 sampled `out` rows have
0.00 mean realized minutes and a 0% 60+ start rate; the 9 suspended rows also
have 0.00 mean realized minutes.

This is strong evidence that the PIT flags carry useful suppression signal,
but it is still a limited two-GW1 database sample. The expanded CI path now
materializes the first five gameweeks for both seasons read-only; those extra
rows have not been written into the validation database.

## Promotion gates

A future promotion decision requires real evidence for:

1. Snapshot coverage across multiple seasons and gameweeks.
2. Every emitted event has a valid information timestamp.
3. Entity resolution is complete or explicitly accounted for; no silent guesses.
4. Every event is eligible at its historical deadline.
5. Availability-restricted statuses demonstrate measurable suppression in actual minutes.
6. A controlled validation import is independently reviewed and verified idempotent.
7. An explicit human decision to wire the feature into the live decision chain.

A passing unit test suite or a green read-only workflow is not, by itself, evidence
that the historical coverage and live-promotion gates have passed.

## CI status

The latest PIT workflow run (`28`) completed successfully after exercising:

- targeted PIT linting
- PIT unit tests
- source-provenanced deadline materialization over the first five gameweeks of
  both 2024-25 and 2025-26

The validation-DB import job remains deliberately skipped unless the repository
variable `AVAILABILITY_PIT_ENABLE_IMPORT=true` is explicitly enabled.

## Commands

### Read-only verified-deadline materialization

```bash
python scripts/materialize_availability_pit.py \
  --from-verified-deadlines \
  --season-code 2024-25 --season-code 2025-26 \
  --gw-min 1 --gw-max 5 \
  --cache-root data/fplcache_pit \
  --evaluate
```

### Read-only DB-deadline materialization

```bash
python scripts/materialize_availability_pit.py \
  --from-db-deadlines --season-code 2024-25 --gw-min 1 --gw-max 10 --evaluate
```

### Validation DB evidence audit

```bash
python scripts/audit_availability_pit.py \
  --season 2024-25 --season 2025-26
```

### Controlled validation-DB import

Only run this after the gates above are independently reviewed:

```bash
python scripts/materialize_availability_pit.py \
  --from-verified-deadlines \
  --season-code 2024-25 --season-code 2025-26 \
  --gw-min 1 --gw-max 5 \
  --import --commit --evaluate
```
