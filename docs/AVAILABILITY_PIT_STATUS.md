# Availability Point-in-Time (PIT) status

## Scope

This branch adds an isolated point-in-time availability path using immutable
`Randdalf/fplcache` bootstrap snapshots. The snapshot capture timestamp is the
information-availability timestamp; football event time is never substituted.
Randdalf/fplcache documents the cache layout as `{year}/{month}/{day}/{time}.json.xz`
and says it is updated four times daily at six-hour intervals. Source: `https://github.com/Randdalf/fplcache`.

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
| Deadline loader | `src/fpl_intelligence/availability/historical/deadlines.py` | Implemented |
| Snapshot materializer | `src/fpl_intelligence/availability/historical/materialize_pit.py` | Implemented |
| Chronological gate | `src/fpl_intelligence/availability/historical/chronological.py` | Implemented, fail-closed |
| Minutes signal-lift | `src/fpl_intelligence/availability/historical/signal_lift.py` | Implemented |
| Validation DB evidence audit | `src/fpl_intelligence/availability/historical/pit_audit.py` + `scripts/audit_availability_pit.py` | Implemented |
| Dry-run evaluator | `scripts/evaluate_availability_pit.py` | Implemented |
| Controlled import | `scripts/materialize_availability_pit.py` | Implemented, opt-in |
| CI | `.github/workflows/availability-pit.yml` | Implemented |
| Unit tests | PIT provider/materializer/chronology/audit tests | Implemented |

## Validation DB evidence observed on 2026-08-29

The connected validation Supabase project contains 5 seasons, 189 gameweeks,
1,466 players, 1,466 player external-ID mappings, and 126,167 player-gameweek
performance rows. It currently contains 210 `fplcache_pit` availability events:
91 for 2024-25 GW1 and 119 for 2025-26 GW1.

All 210 imported PIT rows are linked to gameweeks, classified
`STRICT_BACKTEST_SAFE`, and have a non-null `valid_from` timestamp. The realized
minutes join matches all 210 rows. For the sampled GW1 rows, `out` events have
mean realized minutes of 0.00 in both seasons, and suspended rows also have 0.00
mean minutes. This establishes a strong hard-out suppression signal on this
sample, but it is not yet sufficient to claim broad multi-gameweek coverage.

The validation DB currently has `deadline_time` unset for 2024-25 GW1 and
2025-26 GW1. Therefore the evidence above uses the explicitly supplied historical
PIT cutoffs in the read-only materializer, not fabricated DB deadlines. The
DB-deadline path remains a separate coverage gate until those deadline records
are populated from a verified source.

## Promotion gates

A future promotion decision requires real evidence for:

1. Snapshot coverage across multiple seasons and gameweeks.
2. Every emitted event has a valid information timestamp.
3. Entity resolution is complete or explicitly accounted for; no silent guesses.
4. Every event is eligible at its historical deadline.
5. Availability-restricted statuses demonstrate measurable suppression in actual minutes.
6. An explicit human decision to wire the feature into the live decision chain.

A passing unit test suite is not evidence that the historical coverage or signal-lift gates have passed.

## Commands

### Read-only fixed-cutoff evaluation

```bash
python scripts/evaluate_availability_pit.py \
  --cutoff 2024-08-16T16:00:00Z --season 2024-25 --gameweek 1 \
  --cutoff 2025-08-15T16:00:00Z --season 2025-26 --gameweek 1
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
  --from-db-deadlines --season-code 2024-25 --gw-min 1 --gw-max 5 \
  --import --commit --evaluate
```
