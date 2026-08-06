# Feature Store

## Overview

The FPL Intelligence Engine's feature store provides versioned, temporally-aware
feature computation with strict no-look-ahead enforcement for historical
backtesting. It ensures that every feature value used in a backtest was
actually available at the historical decision cutoff.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    FeatureRegistry                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ FeatureCache │  │ FeatureDef   │  │ FeatureCalc  │ │
│  │ (in-memory)  │  │ (metadata)   │  │ (computable) │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
│         │              │                   │          │
│         ▼              ▼                   ▼          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ FeatureSnap  │  │ FeatureLine  │  │ Calculators  │ │
│  │ (snapshots)  │  │ (lineage)    │  │ (player_form,│ │
│  └──────────────┘  └──────────────┘  │ market, etc) │ │
│                                      └──────────────┘ │
└─────────────────────────────────────────────────────────┘
```

## Core Components

### FeatureDefinition

Immutable definition of a feature. Once a feature definition is used in a
historical backtest, it must not be modified. Create a new version for
changed logic.

| Field | Type | Description |
|-------|------|-------------|
| `feature_name` | String | Canonical name, e.g. "player_form" |
| `version` | String | Semantic version, e.g. "1.0.0" |
| `data_type` | String | "float", "int", "json", etc. |
| `entity_type` | String | "player", "team", "fixture" |
| `calculation_method` | String | Description of how the feature is computed |
| `is_active` | Boolean | Whether this version is currently active |

### FeatureSnapshot

A computed feature value at a specific cutoff time. Snapshots are immutable
once created.

| Field | Type | Description |
|-------|------|-------------|
| `entity_id` | Integer | ID of the entity |
| `feature_name` | String | Name of the feature |
| `feature_version` | String | Version of the feature definition |
| `cutoff_time` | DateTime | The historical decision cutoff |
| `value` | JSON | The computed feature value |
| `is_missing` | Boolean | Whether the value is missing |
| `completeness_score` | Float | 0.0 to 1.0 data completeness |
| `source_count` | Integer | Number of source records used |
| `latest_source_time` | DateTime | Timestamp of most recent source |

### FeatureLineage

Records the source data used to compute a feature, enabling reproducibility
and auditing.

| Field | Type | Description |
|-------|------|-------------|
| `feature_name` | String | Name of the feature |
| `feature_version` | String | Version of the feature definition |
| `entity_id` | Integer | ID of the entity |
| `source_table` | String | Name of the source table |
| `source_record_ids` | JSON | List of source record IDs |
| `calculation_version` | String | Version of the calculation logic |
| `cutoff_time` | DateTime | The cutoff time for this computation |

## Feature Calculators

### PlayerFormCalculator

Computes rolling form features for a player using historical gameweek
performance data.

**Features:**
- `rolling_points_3gw`, `rolling_points_5gw`, `rolling_points_8gw`
- `rolling_minutes_3gw`, `rolling_minutes_5gw`
- `rolling_goals_3gw`, `rolling_assists_3gw`
- `rolling_xg_3gw`, `rolling_xa_3gw`
- `rolling_bps_3gw`, `rolling_bonus_3gw`
- `form_weighted_points` (recency-weighted)
- `consistency_score` (coefficient of variation)
- `games_started_ratio`
- `recent_goals_per_90`, `recent_assists_per_90`

### MarketFeaturesCalculator

Computes market features using FPL snapshots available before the cutoff.

**Features:**
- `price`, `ownership`, `transfers_in`, `transfers_out`
- `total_points`, `form`, `points_per_game`
- `ownership_change`, `transfer_velocity`, `price_movement`
- `snapshot_count`, `latest_snapshot_time`, `time_gap_hours`

### FixtureFeaturesCalculator

Computes fixture context features using only historical data.

**Features:**
- `opponent`, `home/away`, `fixture_difficulty`
- `opponent_attack_strength`, `opponent_defensive_strength`
- `fixture_difficulty_model` (derived)
- `days_of_rest`, `fixture_congestion`, `upcoming_fixtures`

### TeamFeaturesCalculator

Computes team-level features using historical match data.

**Features:**
- `avg_goals_scored`, `avg_goals_conceded`
- `avg_xg`, `avg_xg_conceded`
- `avg_shots`, `avg_shots_on_target`, `avg_possession`
- `clean_sheet_rate`, `home_advantage`
- `attack_strength`, `defensive_strength`

### PlayerAvailabilityCalculator

Computes player availability features. Currently returns null features
with `is_missing=True` because the repository does not yet have trustworthy
historical injury data.

**Limitation:** When injury data becomes available, this calculator should
be updated to query the injury data source.

## Feature Cache

The `FeatureCache` provides thread-safe caching of computed feature snapshots.
Cache keys include:
- `feature_name`
- `feature_version`
- `entity_id`
- `cutoff_time` (ISO format)
- `data_version` (optional)

This ensures that cached values are never reused across different cutoffs
or feature versions, preventing look-ahead leakage.

## Usage

```python
from fpl_intelligence.features.registry import FeatureRegistry
from fpl_intelligence.features.calculators.player_form import PlayerFormCalculator
from fpl_intelligence.features.calculators.market_features import MarketFeaturesCalculator
from fpl_intelligence.backtesting.cutoff import DecisionCutoff

# Create registry and register calculators
registry = FeatureRegistry(db_session)
registry.register(PlayerFormCalculator())
registry.register(MarketFeaturesCalculator())

# Compute features at a historical cutoff
cutoff = DecisionCutoff(
    cutoff_time=datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC),
    gameweek=1,
    season="2025-26",
)
features = registry.compute_features(db_session, cutoff)
# features: {player_id: {feature_name_value: float, ...}}
```

## Temporal Integrity

All feature calculations use the `TemporalQueryBuilder` and
`InformationAccessPolicy` to enforce the no-look-ahead rule. See
[temporal-integrity.md](temporal-integrity.md) for details.
