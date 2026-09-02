# Phase 23 — Background-first decision computation (issue #23)

Pre-computes decision reports OFF the request path. The user request
path will eventually serve `/api/v1/decisions` from a durable artifact
instead of running the Phase 6 decision chain on every hit.

## What was added

### Storage

- **`decision_snapshots` table** — single canonical row per
  `(session_id, gameweek, model_version, data_snapshot_id)`. Carries
  `payload` (JSON), `refresh_state`, `generated_at`, and
  `refresh_window_seconds`.
- **Migration `0025_decision_snapshots`** — creates the table and its
  indexes (`session_id + gameweek`, `generated_at`). No backfill; the
  table starts empty.

### Library (`src/fpl_intelligence/sync/decision_snapshots.py`)

- `RefreshState` enum: `fresh | refreshing | stale | unavailable`.
- `DecisionSnapshot` dataclass with `effective_state()` (returns
  `stale` when a `fresh` row ages past its window).
- `InMemoryBackend` — used by tests.
- `PostgresBackend` — production. Uses `pg_try_advisory_lock` for
  per-`(session_id, gameweek)` rebuild guards and `INSERT ... ON
  CONFLICT DO UPDATE` for atomic writes.
- `DecisionSnapshotStore` — read-through facade.
- `RebuildSlot` — context manager wrapping the per-snapshot rebuild
  lock. `acquire_rebuild_slot()` returns `None` when another worker
  is already refreshing, so the caller can short-circuit.

### Job (`scripts/build_decision_snapshot.py`)

CLI / GitHub Actions entry point. Rebuilds decision snapshots for:

- a single `--entry <id> --gameweek <n>`, OR
- every saved squad via `--all-saved`.

Flags:

- `--dry-run` — build the payload but skip writing to the store.
- `--refresh-window-seconds` — override the per-snapshot freshness
  window (default 900s = 15 minutes).

Records a one-line summary:
`summary: rebuilt=N skipped=M failed=K total=T` and exits non-zero
on any failure.

## How a request path should use it (future wiring)

```python
state, snap = store.get_latest(session_id, gameweek)
if state is RefreshState.UNAVAILABLE or state is RefreshState.STALE:
    slot = store.acquire_rebuild_slot(
        session_id, gameweek,
        model_version="v2.7.12",
        data_snapshot_id=snapshot_id_now(),
    )
    if slot is None:
        # Another worker is rebuilding; serve the existing snapshot
        # with state=`refreshing` so the UI can show "refreshing".
        return existing_snapshot_with_state(RefreshState.REFRESHING)
    with slot:
        payload = build_payload(...)
        store.store(session_id, gameweek, payload=payload, ...)
        return payload
return snap.payload  # state is fresh
```

## Concurrency guarantees

- Two workers requesting the same `(session_id, gameweek)` cannot
  both enter `refreshing`. The second sees `None` from
  `acquire_rebuild_slot()` and short-circuits.
- Advisory locks are released via the `RebuildSlot` context manager
  (`__exit__` and explicit `.release()`).
- A worker that crashes mid-rebuild leaves the snapshot in
  `refreshing`. The next worker that calls `acquire_rebuild_slot()`
  will still see the existing lock for that key. Mitigation: the
  rebuild script calls `pg_advisory_unlock` after exceptions, and
  the job has a hard time budget so a stuck build cannot block the
  next run forever.

## Validation done locally (no deploy)

- 14 unit tests on the in-memory store + state lifecycle + concurrency
- 4 unit tests on the migration DDL contract
- 18 new tests total — full unit suite: **1252 passed, 0 failed**
- `ruff check` + `ruff format` clean
- `mypy` clean on the new module
- Alembic chain: single head `0025_decision_snapshots`
- Migration **NOT** applied to Supabase
- Script **NOT** wired into GitHub Actions cron yet
- Branch **NOT** pushed

## Out of scope (follow-ups)

- Wiring `/api/v1/decisions` to read from this store. That requires a
  small change in the route handler — outside the abstraction change.
- The GitHub Actions cron itself. The repo already has
  `.github/workflows/phase0-instrumentation.yml` etc.; adding
  `.github/workflows/decision-snapshot-build.yml` is a one-file
  follow-up.
- Stale-eviction sweeper: a periodic job that marks `fresh` snapshots
  older than their window as `stale`. Today `effective_state()`
  computes this on read; an explicit column flip would help
  dashboards.

## Settings used (existing)

- `app_env`, `database_url` (from `Settings`)
- `local_squad_state` table is read by `--all-saved`

No new settings required.