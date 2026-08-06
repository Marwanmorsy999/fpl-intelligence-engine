# Phase 3 — Preflight Audit

> **Date:** 2026-07-31
> **Auditor:** Automated preflight audit
> **Scope:** Existing Phase 2 codebase before Phase 3 implementation

---

## 1. Schema / Model Mismatches

### 1.1 Migration 0001 Creates Columns Not in ORM Models

#### Teams Table — Orphaned Columns

Migration `0001_initial` creates `teams` with:

```sql
provider VARCHAR(100) NOT NULL
provider_team_id INTEGER NOT NULL
UNIQUE(provider, provider_team_id)
```

The current ORM model (`Team` in `db/models.py`) does **not** have `provider` or `provider_team_id` columns. The model uses a separate `TeamExternalId` table instead (added in migration 0002).

**Impact:** These columns exist in the database but are never read or written by the ORM. They are dead columns. This is not a critical correctness issue, but it is a maintenance burden.

**Location:** Migration 0001 lines 32-38 vs. `Team` model lines 49-58.

#### Players Table — Orphaned Columns

Migration `0001_initial` creates `players` with:

```sql
provider VARCHAR(100) NOT NULL
provider_player_id INTEGER NOT NULL
current_team_id INTEGER REFERENCES teams(id)
UNIQUE(provider, provider_player_id)
```

The current ORM model (`Player` in `db/models.py`) does **not** have `provider`, `provider_player_id`, or `current_team_id` columns. The model uses `PlayerExternalId` instead (added in migration 0002).

**Impact:** Same as above — dead columns in the database.

**Location:** Migration 0001 lines 39-50 vs. `Player` model lines 81-93.

### 1.2 Type Mismatch: provider_team_id

| Layer | Type | File |
|-------|------|------|
| Migration 0001 | `Integer` (NOT NULL) | `0001_initial.py:34` |
| ORM Model (`TeamExternalId`) | `String(100)` | `db/models.py:40` |
| Canonical normalization | `str` | `domain/canonical.py:33` |

**Severity:** CRITICAL

**Risk:** If a database was created from migration 0001, the `teams.provider_team_id` column is `INTEGER`. The ingestion code passes strings (from `str(data.get("provider_team_id", ""))`). This would cause a type error in PostgreSQL but would work in SQLite (which is permissive).

Since new code uses `TeamExternalId` (which has `String(100)`), this only affects the orphaned `teams.provider_team_id` column. However, the migration for `TeamExternalId` (0002) correctly uses `String(100)`.

### 1.3 Type Mismatch: provider_player_id

| Layer | Type | File |
|-------|------|------|
| Migration 0001 | `Integer` (NOT NULL) | `0001_initial.py:43` |
| ORM Model (`PlayerExternalId`) | `String(100)` | `db/models.py:72` |
| Canonical normalization | `str` | `domain/canonical.py:33` |

**Severity:** CRITICAL

Same issue as 1.2 — the orphaned column in `players` is `INTEGER`, but the new code uses `String(100)`.

### 1.4 Missing Columns in Gameweek Model vs. Migration 0001

Migration 0001 creates `gameweeks` with only `deadline_time`. The `start_time`, `end_time`, and `status` columns were added in migration 0002. The ORM model has all three. This is **consistent** after migration 0002.

### 1.5 Missing Columns in Fixture Model vs. Migration 0001

Migration 0001 creates `fixtures` without `status` or `postponed`. These were added in migration 0002. The ORM model has both. This is **consistent** after migration 0002.

### 1.6 Missing Columns in IngestionRun vs. Migration 0001

Migration 0001 creates `ingestion_runs` without `season_code`. This was added in migration 0002. The ORM model has it. This is **consistent** after migration 0002.

### 1.7 Missing Columns in RawRecord vs. Migration 0001

Migration 0001 creates `raw_records` without `provider` or `season_code`. These were added in migration 0002. The ORM model has both. This is **consistent** after migration 0002.

---

## 2. Migration Inconsistencies

### 2.1 Non-Destructive Migration Pattern

Migration 0002 adds columns but never removes the orphaned columns created by migration 0001 (`teams.provider`, `teams.provider_team_id`, `players.provider`, `players.provider_player_id`, `players.current_team_id`).

**Impact:** These columns are dead weight. They don't cause errors because the ORM doesn't reference them, but they waste space and could cause confusion.

**Recommendation:** Create a migration 0003 that drops these orphaned columns. This is safe because:
- The ORM models don't use them.
- The `TeamExternalId` and `PlayerExternalId` tables handle provider ID mapping.
- No production code references these columns.

### 2.2 Downgrade Paths

Migration 0001 downgrade drops all tables. This is destructive but acceptable for initial development.

Migration 0002 downgrade correctly reverses all changes (drops added tables, drops added columns).

The downgrade path is **consistent** and functional.

### 2.3 Server Defaults

Migration 0001 uses `server_default="0"` for `records_processed` and `records_failed` on `ingestion_runs`. The ORM model uses `default=0` (Python-level default). These are consistent in behavior.

Migration 0002 consistently uses `server_default` for integer columns and `server_default=sa.text("false")` for boolean columns. The ORM models use Python-level defaults.

**Note:** Python-level defaults (`default=0`) are not applied at the database level. If a raw SQL INSERT is performed without specifying the column, PostgreSQL would fail on NOT NULL columns. The `server_default` in migrations handles this.

---

## 3. SQLite / PostgreSQL Compatibility Risks

### 3.1 DateTime with Timezone

SQLite stores `DateTime(timezone=True)` as text strings without timezone awareness. PostgreSQL stores them as `TIMESTAMPTZ` with proper timezone handling.

**Risk:** LOW. SQLite comparisons work correctly as long as all timestamps use the same timezone format (UTC). All ingestion code uses `datetime.now(UTC)`.

### 3.2 JSON Column Type

SQLite stores JSON as text. PostgreSQL has native `JSONB`.

**Risk:** LOW. SQLAlchemy handles the abstraction. No JSON querying is done at the database level.

### 3.3 Enum/String Types

The project uses `String` types for status fields (`status: Mapped[str]`). PostgreSQL would use `VARCHAR`, SQLite would use `TEXT`. This is compatible.

### 3.4 Foreign Key Enforcement

SQLite does not enforce foreign key constraints by default. The test configuration uses `sqlite:///:memory:` without `PRAGMA foreign_keys = ON`.

**Risk:** MEDIUM. Tests pass in SQLite despite FK violations that would fail in PostgreSQL. This means the test suite provides false confidence about referential integrity.

### 3.5 UniqueConstraint Null Behavior

SQLite treats NULL values in unique constraints as distinct (allowing multiple NULLs). PostgreSQL also treats NULLs as distinct. This is **consistent**.

### 3.6 Auto-Increment

SQLite and PostgreSQL both auto-increment integer primary keys. Consistent.

---

## 4. Type-Checking Issues

### 4.1 mypy Configuration

The project uses `mypy` with `strict = false`. This means many type errors are not caught.

**Recommendation:** For Phase 3, maintain `strict = false` but fix all new type errors introduced by Phase 3 code. Document pre-existing errors separately.

### 4.2 Provider Fixture ID Type Confusion

**File:** `src/fpl_intelligence/ingestion/historical.py:218-224`

The `_fixture_id_to_int()` function converts string provider fixture IDs to integers using MD5 hashing. The `Fixture` model has `provider_fixture_id: Mapped[int]`.

**Issue:** The original provider fixture ID (a string like `"fpl_fixture_0_0"`) is lost after hashing. The `RawRecord` preserves the original data, but the canonical `Fixture` table only has the hash.

**Impact:** If a provider changes its fixture ID scheme, the hashing would produce different integers. This is fragile but works for the current mock provider.

### 4.3 Optional Fields in Models

Many model fields are `Mapped[int | None]` with `default=0`. The canonical normalization functions (e.g., `normalize_player_match_stats`) convert missing values to `0` using `or 0`. This means:

- `None` from the database = the field was never set
- `0` from the database = the field was explicitly set to 0

But the normalization layer conflates these by using `or 0`. This is a **data quality issue** — we cannot distinguish "no data" from "zero data" after normalization.

### 4.4 Inconsistent Type in `get_fpl_snapshots`

The `HistoricalFootballDataProvider` protocol defines `get_fpl_snapshots(self, season: str, gameweek: int | None = None)`. The mock provider implements this. But the ingestion code in `historical.py` calls `ns.get("ingested_at")` even though the canonical normalization doesn't return `ingested_at` — it's set directly in the ingestion code at line 574.

---

## 5. Incomplete Temporal Semantics

### 5.1 Missing `available_at` Field

The `FPLSnapshot` model has:
- `event_time` — when the event occurred
- `published_at` — when the source published it
- `ingested_at` — when our pipeline collected it

**Missing:** `available_at` — the earliest timestamp at which the system can legitimately be considered to have had access to the information.

**Impact:** The backtester cannot distinguish between "publicly available" and "actually available to our system." This is critical for strict reproducibility.

### 5.2 Missing `source_last_modified_at` Field

No model has a `source_last_modified_at` field. If a source updates a record (e.g., fixture result changes), there's no way to know when the source last modified it.

### 5.3 No Temporal Fields on PlayerGameweekPerformance

The `PlayerGameweekPerformance` model has **no temporal fields** at all. There's no `ingested_at`, `published_at`, or `available_at`. This means:

- We cannot know when a performance record became available.
- We cannot backtest using these records with strict temporal integrity.
- A future match result could theoretically be used in a pre-match feature calculation.

### 5.4 No Temporal Fields on PlayerMatchPerformance

Same issue as 5.3 — no temporal fields on `PlayerMatchPerformance`.

### 5.5 No Temporal Fields on TeamMatchPerformance

Same issue — no temporal fields.

### 5.6 `ingested_at` Set to Current Time

In `historical.py:574`, `ingested_at` is set to `datetime.now(UTC)`. This is correct for live ingestion, but for historical data import, it means all historical snapshots have `ingested_at` equal to the import time, not the time they were actually ingested historically.

**Impact:** If we import historical data today, all historical snapshots will have `ingested_at = today`. This makes `ingested_at` useless for historical backtesting with strict reproducibility.

---

## 6. Missing Indexes

The following tables lack indexes on foreign key columns, which will impact query performance during feature calculation and backtesting:

| Table | Missing Indexes |
|-------|----------------|
| `PlayerMatchPerformance` | `player_id`, `fixture_id`, `season_id`, `team_id` |
| `PlayerGameweekPerformance` | `player_id`, `gameweek_id`, `season_id`, `team_id` |
| `TeamMatchPerformance` | `team_id`, `fixture_id`, `season_id` |
| `FPLSnapshot` | `player_id`, `gameweek_id`, `season_id` |
| `PlayerTeamMembership` | `player_id`, `team_id`, `season_id` |
| `PlayerExternalId` | `player_id` |
| `TeamExternalId` | `team_id` |
| `Gameweek` | `season_id` |
| `Fixture` | `season_id`, `gameweek_id`, `home_team_id`, `away_team_id` |

**Impact:** Feature calculation will require full table scans for common queries like "get all performances for player X before cutoff Y." This will be slow for production-scale data.

---

## 7. Data That Cannot Be Reconstructed Historically

### 7.1 Pre-Match Snapshot of Player Data

The `PlayerGameweekPerformance` table stores the **final** Gameweek result, not the snapshot available before the deadline. To backtest, we need to know what data was available *before* the deadline, not after.

**Current state:** Only `FPLSnapshot` has pre-deadline data (price, ownership, form). But `PlayerGameweekPerformance` contains the actual performance data (goals, assists, etc.) which is only available *after* the match.

**Cannot reconstruct:** A pre-match feature vector that includes:
- Player form before the match
- Player price before the deadline
- Ownership before the deadline
- Fixture difficulty as known before the deadline

### 7.2 Fixture Schedule Changes

The `Fixture` table stores the current state of each fixture. If a fixture was rescheduled (postponed, moved to a different Gameweek), the historical schedule is lost.

**Cannot reconstruct:** The fixture schedule as it was known at a specific historical cutoff.

### 7.3 Player Transfers Within a Season

The `PlayerTeamMembership` table has `valid_from` and `valid_to` fields, but the current ingestion code only sets `valid_from` (line 393). If a player transfers mid-season, there's no `valid_to` on the old membership.

**Cannot reconstruct:** A player's team at a specific historical cutoff with confidence.

### 7.4 Price Changes

The `FPLSnapshot` table captures price at snapshot times, but there's no guarantee of snapshot frequency. Between snapshots, price changes are unknown.

**Cannot reconstruct:** The exact price of a player at an arbitrary cutoff between snapshots.

---

## 8. Features That Cannot Currently Be Backtested Safely

### 8.1 No Cutoff Enforcement

There is no mechanism in the codebase to enforce a "no look-ahead" cutoff. Any query can access any data regardless of time.

### 8.2 No Feature Store

No feature definitions, versions, snapshots, or lineage exist. Features cannot be versioned or cached.

### 8.3 No Backtest Engine

No backtesting framework exists. There is no way to:
- Define a backtest configuration
- Run a historical backtest
- Store results
- Evaluate performance

### 8.4 No Temporal Query Helpers

No `as_of(cutoff)` or equivalent abstraction exists. Every query must manually filter by time, which is error-prone and not enforced.

### 8.5 No Information-Access Policies

No distinction between "public availability" and "system availability" is implemented. The backtester cannot enforce strict reproducibility.

### 8.6 No Baseline Predictions

No prediction models exist, not even simple baselines.

---

## 9. Test Coverage Gaps

### 9.1 No Temporal Integrity Tests

The existing `test_temporal_integrity.py` tests exist but are limited. There are no tests for:
- Look-ahead bias detection
- Cutoff enforcement
- Feature versioning
- Historical snapshot reconstruction

### 9.2 No Integration Tests

The `tests/integration/` directory is empty. There are no PostgreSQL integration tests.

### 9.3 No conftest.py

There are no shared test fixtures. Each test file defines its own fixtures inline. This leads to duplication and inconsistency.

### 9.4 Test Database Uses SQLite

All tests use `sqlite:///:memory:`. This means:
- FK constraints are not enforced
- Type mismatches are not caught
- PostgreSQL-specific features are not tested

---

## 10. Summary of Issues by Severity

### CRITICAL (Must Fix Before Phase 3)

| # | Issue | File |
|---|-------|------|
| 1 | `provider_team_id` type mismatch (Integer vs String) | Migration 0001 vs. `TeamExternalId` model |
| 2 | `provider_player_id` type mismatch (Integer vs String) | Migration 0001 vs. `PlayerExternalId` model |
| 3 | Missing `available_at` field on `FPLSnapshot` | `db/models.py` |
| 4 | No temporal fields on `PlayerGameweekPerformance` | `db/models.py` |
| 5 | No temporal fields on `PlayerMatchPerformance` | `db/models.py` |
| 6 | No temporal fields on `TeamMatchPerformance` | `db/models.py` |
| 7 | `ingested_at` set to current time, not historical time | `ingestion/historical.py:574` |

### HIGH (Should Fix for Phase 3 Correctness)

| # | Issue | File |
|---|-------|------|
| 8 | Orphaned columns in `teams` table (provider, provider_team_id) | Migration 0001 |
| 9 | Orphaned columns in `players` table (provider, provider_player_id, current_team_id) | Migration 0001 |
| 10 | Missing indexes on all FK columns | `db/models.py` |
| 11 | No cutoff enforcement mechanism | Entire codebase |
| 12 | No feature store | Entire codebase |
| 13 | No backtest engine | Entire codebase |
| 14 | No temporal query helpers | Entire codebase |
| 15 | Normalization conflates None and 0 | `domain/canonical.py` |
| 16 | FK constraints not tested (SQLite limitation) | Test suite |

### MEDIUM (Document for Future Phases)

| # | Issue | File |
|---|-------|------|
| 17 | Provider fixture ID lost via hashing | `ingestion/historical.py:218-224` |
| 18 | Fixture schedule changes not tracked | `db/models.py` |
| 19 | Player transfer history incomplete | `ingestion/historical.py:377-395` |
| 20 | mypy not strict (`strict = false`) | `pyproject.toml` |
| 21 | No conftest.py for shared fixtures | Test suite |
| 22 | No PostgreSQL integration tests | Test suite |

---

## 11. Recommended Fixes Before Phase 3

### Fix 1: Migration 0003 — Clean Up Orphaned Columns

Create a migration that:
- Drops `teams.provider`
- Drops `teams.provider_team_id`
- Drops `players.provider`
- Drops `players.provider_player_id`
- Drops `players.current_team_id`
- Adds indexes on all FK columns

### Fix 2: Add `available_at` to FPLSnapshot

Add `available_at: Mapped[datetime | None]` to the `FPLSnapshot` model.

### Fix 3: Add Temporal Fields to Performance Tables

Add `ingested_at` and `available_at` to:
- `PlayerGameweekPerformance`
- `PlayerMatchPerformance`
- `TeamMatchPerformance`

### Fix 4: Fix `ingested_at` in Historical Ingestion

For historical data imports, set `ingested_at` to a reasonable historical timestamp (e.g., match deadline + some delay) rather than `datetime.now(UTC)`. The provider should supply this information.

### Fix 5: Add Indexes

Add indexes on all foreign key columns across all tables.

---

## 12. Decision: Fix Strategy

For Phase 3, we will:

1. **Fix CRITICAL issues** (#1-7) before building new functionality.
2. **Fix HIGH issues** (#8-16) when they affect the correctness of Phase 3:
   - #8-9: Create migration 0003 to clean up orphaned columns
   - #10: Add indexes in migration 0003
   - #11-14: These are the main deliverables of Phase 3
   - #15: Fix normalization to distinguish None from 0
   - #16: Add FK enforcement to test setup
3. **Document MEDIUM issues** (#17-22) as known limitations.

The orphaned columns (#8-9) will be cleaned up in migration 0003, which also adds indexes and temporal fields. This is a non-destructive operation that doesn't affect existing data.