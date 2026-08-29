# Availability Point-in-Time (PIT) status

## Scope

This branch adds an isolated point-in-time availability path using immutable
`Randdalf/fplcache` bootstrap snapshots. The snapshot capture timestamp is the
information-availability timestamp; football event time is never substituted.
Randdalf/fplcache documents the cache layout as `{year}/{month}/{day}/{time}.json.xz`
and says it is updated four times daily at six-hour intervals. citeturn218102search1

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
| Dry-run evaluator | `scripts/evaluate_availability_pit.py` | Implemented |
| Controlled import | `scripts/materialize_availability_pit.py` | Implemented, opt-in |
| CI | `.github/workflows/availability-pit.yml` | Implemented |
| Unit tests | `tests/unit/test_*pit*`, `test_chronological_and_lift.py` | Implemented |

## Promotion gates

A future promotion decision requires real evidence for:

1. Snapshot coverage across multiple seasons/gameweeks.
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

### Controlled validation-DB import

Only run this after the gates above are independently reviewed:

```bash
python scripts/materialize_availability_pit.py \
  --from-db-deadlines --season-code 2024-25 --gw-min 1 --gw-max 5 \
  --import --commit --evaluate
```
