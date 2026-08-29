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
- `--import` targets the existing validation Supabase project only.
- Ordinary `validation_session_factory()` connections remain read-only.
- Controlled import uses a separate `validation_write_session_factory()` and the CLI refuses `--commit` unless `DATABASE_URL` identifies the approved validation project reference.
- `--commit` is allowed only after snapshot, chronology, signal, and entity-resolution gates pass inside the same transaction.
- The PIT GitHub API discovery path uses the Actions token when available; CI supplies `GITHUB_TOKEN` explicitly to avoid unauthenticated GitHub API throttling.
- No live decision-chain wiring is included.

## Current components

| Component | Path | State |
|---|---|---|
| Immutable PIT provider | `src/fpl_intelligence/availability/historical/pit_fplcache.py` | Implemented |
| DB deadline loader | `src/fpl_intelligence/availability/historical/deadlines.py` | Implemented; skips missing DB deadlines rather than guessing |
| Verified deadline catalog | `src/fpl_intelligence/availability/historical/verified_deadlines.py` | Implemented; 10 source-provenanced cutoffs |
| Snapshot materializer | `src/fpl_intelligence/availability/historical/materialize_pit.py` | Implemented; authenticated GitHub discovery |
| Chronological gate | `src/fpl_intelligence/availability/historical/chronological.py` | Implemented, fail-closed |
| Minutes signal-lift | `src/fpl_intelligence/availability/historical/signal_lift.py` | Implemented with same-GW unflagged control |
| Validation DB evidence audit | `src/fpl_intelligence/availability/historical/pit_audit.py` + `scripts/audit_availability_pit.py` | Implemented |
| Dry-run evaluator | `scripts/evaluate_availability_pit.py` | Implemented |
| Controlled import | `scripts/materialize_availability_pit.py` | Implemented; validation-target guarded and opt-in |
| Read-only validation session | `src/fpl_intelligence/db/session.py` | Preserved |
| Write-capable validation session | `src/fpl_intelligence/db/session.py` | Implemented; import-only |
| CI | `.github/workflows/availability-pit.yml` | Implemented; checkout@v6 + setup-python@v7 |
| Unit tests | PIT provider/materializer/chronology/audit/control/deadline tests | Implemented, including GitHub-token regression |

## Source-provenanced deadline catalog

The validation branch carries the first five FPL deadlines for both 2024-25 and
2025-26 in `verified_deadlines.py`. The catalog stores UTC cutoffs and the
Premier League publication URL used to verify each deadline. This is validation
metadata only; it does not alter database deadline rows.

The catalog contains 10 verified cutoffs spanning GW1-GW5 for each season.

## Expanded read-only PIT evidence

The expanded preflight exercises all 10 verified cutoffs from the catalog. Each
cutoff has a resolvable immutable snapshot and the materializer produces **1,659
availability observations**:

- 2024-25: **793** observations across GW1-GW5.
- 2025-26: **866** observations across GW1-GW5.
- **1,659/1,659** observations were chronologically eligible.
- **0** missing information timestamps.
- **0** post-deadline observations.

The PIT preflight passes targeted correctness lint and the targeted PIT unit suite,
including deadline, chronology, control-group, audit, and GitHub-token regression
safeguards.

## Validation DB evidence observed on 2026-08-29

The connected validation Supabase project contains 5 seasons, 189 gameweeks,
1,466 players, 1,466 player external-ID mappings, and 126,167 player-gameweek
performance rows. Before the expanded controlled import, it contained 210
`fplcache_pit` availability events: 91 for 2024-25 GW1 and 119 for 2025-26 GW1.

All 210 existing PIT rows are linked to gameweeks, classified
`STRICT_BACKTEST_SAFE`, and have a non-null `valid_from` timestamp. The realized
minutes join matches all 210 rows. A direct duplicate-key audit over
`(provider, provider_event_id, season_id, player_id)` returns zero duplicate groups
and zero duplicate rows.

The provider emits flagged/non-default availability states rather than an
explicit `available` row for every player. The signal evaluator therefore uses
an unflagged player-gameweek control group from the same gameweek.

For the current two-GW1 database sample:

| Season | Restricted N | Restricted mean min | Restricted 60+ start rate | Control N | Control mean min | Control 60+ start rate |
|---|---:|---:|---:|---:|---:|---:|
| 2024-25 | 90 | 1.3333 | 0.011111 | 526 | 37.1084 | 0.395437 |
| 2025-26 | 117 | 2.8462 | 0.025641 | 573 | 33.8115 | 0.364747 |
| Combined | 207 | 2.1884 | 0.019324 | 1,099 | 35.3894 | 0.379436 |

Combined control-minus-restricted mean-minutes delta is **+33.2010 minutes**;
start-rate delta is **+0.360112**. The hard-OUT sanity check remains strong:
152 sampled `out` rows have 0.00 mean realized minutes and 0% 60+ starts; 9
suspended rows also have 0.00 mean realized minutes.

The 1,659-observation expanded sample is read-only until the one-shot controlled
validation import succeeds.

## CI history

- **Run 46 — green**: expanded 10-cutoff PIT preflight.
- **Run 47 — green**: expanded PIT preflight.
- **Run 48 — initial attempt failed** on GitHub API 403 rate limiting during remote snapshot discovery; retry passed.
- **Run 50 — green**: authenticated GitHub snapshot discovery fix.
- **Run 52 — green**: restored legacy coverage/entity-resolution semantics plus expanded preflight.
- **Run 53 — green**: Node24-compatible Actions (`checkout@v6`, `setup-python@v7`).
- **Run 54 — green**: GitHub-token regression test plus expanded preflight.
- **Run 55 — green**: status-record preflight.
- **Run 57 — failed intentionally at Gate 6** because the existing validation factory enforced `SET TRANSACTION READ ONLY`; no import rows were committed.

The write-session fix is now on the branch and this commit is the one-shot controlled-import trigger.

## Promotion gates

A future promotion decision requires real evidence for:

1. Snapshot coverage across multiple seasons and gameweeks.
2. Every emitted event has a valid information timestamp.
3. Entity resolution is complete or explicitly accounted for; no silent guesses.
4. Every event is eligible at its historical deadline.
5. Availability-restricted statuses demonstrate measurable suppression in actual minutes.
6. A controlled validation import is independently reviewed and verified idempotent.
7. An explicit human decision to wire the feature into the live decision chain.

Gates 1-5 are green on the read-only evidence path. Gate 6 is now being exercised
once against the approved validation database with a write-capable session that is
isolated from ordinary read-only validation.

A successful controlled validation import does **not** permit production promotion.
The live decision chain remains untouched until a separate explicit decision.

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

### Validation DB evidence audit

```bash
python scripts/audit_availability_pit.py \
  --season 2024-25 --season 2025-26 \
  --require-signal
```

### Controlled validation-DB import

```bash
python scripts/materialize_availability_pit.py \
  --from-verified-deadlines \
  --season-code 2024-25 --season-code 2025-26 \
  --gw-min 1 --gw-max 5 \
  --cache-root data/fplcache_pit \
  --import --commit --evaluate
```
