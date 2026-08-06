# Historical Data

## Supported Datasets

The platform supports importing the following historical datasets:

| Dataset | Description | Provider Methods |
|---------|-------------|-----------------|
| `seasons` | Season metadata (name, dates, competition) | `get_seasons()` |
| `teams` | Team information across seasons | `get_teams(season)` |
| `players` | Player information across seasons | `get_players(season)` |
| `fixtures` | Match fixtures with scores and status | `get_fixtures(season)` |
| `stats` | Player match and gameweek performance statistics | `get_fpl_history(season)` |
| `fpl` | FPL snapshots (price, ownership, form) | `get_fpl_snapshots(season)` |

## Provider Architecture

```
External Data Source (e.g. FPL API, Understat, FBref)
    |
    v
Provider Adapter (implements HistoricalFootballDataProvider protocol)
    |
    v
Normalization Layer (domain/canonical.py)
    |  Converts provider-specific schemas to canonical format
    |  Supports multiple schema versions (v1, v2, etc.)
    v
Reconciliation Engine (ingestion/reconciliation.py)
    |  Validates data quality
    |  Detects missing/unmatched entities
    |  Reports duplicates and anomalies
    v
Database Persistence (ingestion/historical.py)
    |  Idempotent, resumable import
    |  Preserves raw records with provenance
    v
PostgreSQL Canonical Schema
```

### Provider Protocol

All historical data providers implement the `HistoricalFootballDataProvider` protocol:

```python
class HistoricalFootballDataProvider(Protocol):
    @property
    def provider_name(self) -> str: ...
    def get_seasons(self) -> Sequence[Mapping[str, object]]: ...
    def get_teams(self, season: str) -> Sequence[Mapping[str, object]]: ...
    def get_players(self, season: str) -> Sequence[Mapping[str, object]]: ...
    def get_fixtures(self, season: str) -> Sequence[Mapping[str, object]]: ...
    def get_fpl_history(self, season: str) -> Sequence[Mapping[str, object]]: ...
    def get_fpl_snapshots(self, season: str, gameweek=None) -> Sequence[Mapping[str, object]]: ...
```

Different providers may implement different subsets of methods. The normalization layer combines multiple providers into one canonical database.

## Canonical Schema

### Entity Resolution

Players and teams are identified by **internal canonical IDs**, not by provider-specific IDs or names.

- `players` table: Internal `id` is the canonical player identifier
- `player_external_ids` table: Maps `(provider, provider_player_id)` → `internal_player_id`
- `teams` table: Internal `id` is the canonical team identifier
- `team_external_ids` table: Maps `(provider, provider_team_id)` → `internal_team_id`

This design supports:
- Multiple providers with different ID schemes
- Player name changes across seasons
- Team name changes and renames
- Player transfers between teams
- Promoted/relegated teams
- Duplicate player names

### Temporal Fields

Every time-varying record preserves appropriate timestamps:

| Table | Temporal Fields | Purpose |
|-------|----------------|---------|
| `fpl_snapshots` | `event_time`, `published_at`, `ingested_at` | When data was true, published, ingested |
| `player_team_memberships` | `valid_from`, `valid_to` | When player belonged to team |
| `gameweeks` | `deadline_time`, `start_time`, `end_time` | Gameweek timing |
| `fixtures` | `kickoff_time` | Match kickoff |
| `raw_records` | `retrieved_at` | When raw data was fetched |
| `ingestion_runs` | `started_at`, `finished_at` | Import timing |

The distinction between `event_time`, `published_at`, and `ingested_at` is critical for backtesting:
- **event_time**: When the data point was true in the real world
- **published_at**: When the provider published this information
- **ingested_at**: When our system retrieved and stored it

## Raw Data Storage

Every external data ingestion preserves the raw source payload:

```python
RawRecord:
    source: str          # e.g. "official_fpl", "understat"
    provider: str        # Provider name
    endpoint: str        # API endpoint or dataset name
    retrieved_at: datetime  # When we fetched it
    payload_hash: str    # SHA-256 of payload (for deduplication)
    payload: JSON        # Complete raw payload
    season_code: str     # Season this data belongs to
```

Raw data is the **evidence**. Canonical data is **derived** from raw data.

## Import Process

### CLI Command

```bash
python -m fpl_intelligence.scripts.backfill --season 2024-25
```

Options:
- `--season` (required): Season code, e.g. `2024-25`
- `--provider`: Data provider name (default: `mock_provider`)
- `--dataset`: Dataset to import: `all`, `teams`, `players`, `fixtures`, `stats`, `fpl`
- `--dry-run`: Validate without persisting
- `--force`: Re-import even if previously completed
- `--resume`: Resume interrupted import
- `--validate`: Run validation checks after import
- `--log-level`: DEBUG, INFO, WARNING, ERROR

### Idempotency

The import is **idempotent**:
- Running the same import twice does not create duplicate records
- Completed imports are tracked in `ingestion_runs` table
- Use `--force` to re-import
- Use `--resume` to continue an interrupted import

### Resumability

If the import process stops after partial completion:
1. Records already persisted remain in the database
2. Re-running the import skips already-imported records
3. The `ingestion_runs` table tracks what was completed

## Reconciliation

The reconciliation engine produces a report for each import:

```
Reconciliation Report: mock_provider/all (2024-25)
  Received: 1000
  Accepted: 950
  Rejected: 50
  Unmatched teams: 2
  Unmatched players: 5
  Duplicate candidates: 1
  Warnings: 3
  Critical errors: 1
```

### Data Quality Rules

| Rule | Severity | Description |
|------|----------|-------------|
| Missing entity ID | Critical | Record has no provider ID |
| Invalid team reference | Critical | Fixture references unknown team |
| Duplicate fixture ID | Warning | Same fixture ID appears twice |
| Unmatched player | Warning | Player not found in known players |
| Impossible minutes | Warning | Minutes < 0 or > 120 |
| Negative score | Warning | Score value is negative |
| Invalid gameweek | Warning | Gameweek number outside 1-38 |
| Home = Away | Warning | Same team listed as home and away |

Critical errors cause the import to fail. Warnings are recorded but do not stop ingestion.