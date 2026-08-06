# Backtesting Engine

## Overview

The FPL Intelligence Engine's backtesting engine simulates historical
prediction decisions with strict no-look-ahead enforcement. It ensures
that predictions are made using only data available at the historical
decision cutoff, and that actual outcomes are revealed only for evaluation.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    BacktestEngine                        │
│                                                          │
│  For each gameweek:                                     │
│  1. Determine decision cutoff                           │
│  2. Freeze available information (as-of cutoff)         │
│  3. Compute features (FeatureRegistry)                  │
│  4. Generate predictions (PredictionModel)              │
│  5. Store predictions (frozen)                          │
│  6. Reveal actual outcomes (evaluation only)            │
│  7. Calculate evaluation metrics                        │
│  8. Store gameweek result                               │
│                                                          │
│  PREDICTION_TIME (steps 1-5) ≠ OUTCOME_TIME (steps 6-7) │
└─────────────────────────────────────────────────────────┘
```

## Key Concepts

### PREDICTION_TIME vs OUTCOME_TIME

The backtest engine enforces a **clear separation** between:
- **PREDICTION_TIME**: Steps 1-5, where features are computed and predictions
  are generated using only data available at the cutoff.
- **OUTCOME_TIME**: Steps 6-7, where actual outcomes are revealed and
  evaluation metrics are calculated.

Outcome data is **never** allowed to flow backward into prediction features.

### Decision Cutoff

A `DecisionCutoff` represents the point in time at which a prediction
decision must be made. It is derived from the Gameweek deadline time,
adjusted by a configurable offset.

```python
from fpl_intelligence.backtesting.cutoff import DecisionCutoff, get_gameweek_decision_cutoff

cutoff = get_gameweek_decision_cutoff(
    db_session,
    season="2025-26",
    gameweek=1,
    offset=timedelta(hours=1),  # Decide 1 hour before deadline
)
```

### Information-Access Policies

Three policies govern what data is available at a cutoff:

| Policy | Condition | Use Case |
|--------|-----------|----------|
| `PUBLIC_AVAILABILITY` | `available_at <= cutoff` | Assumes system could access any public info |
| `SYSTEM_AVAILABILITY` | `ingested_at <= cutoff` | Uses only data actually collected by pipeline |
| `STRICT_REPRODUCIBILITY` | Both conditions | Most conservative; default for all backtesting |

## Backtest Models

### BacktestConfig

Configuration for a backtest run.

| Field | Type | Description |
|-------|------|-------------|
| `season` | String | Season code (e.g., "2025-26") |
| `start_gameweek` | Integer | First gameweek to backtest |
| `end_gameweek` | Integer | Last gameweek to backtest |
| `decision_timing` | String | "deadline", "kickoff", etc. |
| `information_access_policy` | String | Temporal policy to enforce |
| `feature_version` | String | Version of features used |
| `model_version` | String | Version of the prediction model |
| `random_seed` | Integer | Seed for reproducible randomness |
| `simulation_count` | Integer | Number of Monte Carlo simulations |

### BacktestRun

A single execution of a backtest.

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | String (UUID) | Unique identifier |
| `config_id` | Integer | FK to configuration |
| `status` | String | "running", "completed", "failed" |
| `feature_version` | String | Version of features used |
| `model_version` | String | Version of the model used |
| `error_summary` | Text | Error message if failed |

### PlayerPrediction

A single player prediction within a backtest run.

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | Integer | FK to backtest run |
| `player_id` | Integer | FK to player |
| `fixture_id` | Integer | FK to fixture (optional) |
| `cutoff` | DateTime | Decision cutoff time |
| `predicted_expected_points` | Float | Model's prediction |
| `prediction_interval_lower` | Float | Lower bound of interval |
| `prediction_interval_upper` | Float | Upper bound of interval |
| `confidence` | Float | Model confidence (0-1) |
| `data_completeness` | Float | Completeness of input data (0-1) |
| `is_frozen` | Boolean | Whether prediction is immutable |

## Baseline Models

The engine includes four baseline prediction models:

1. **RecentFormBaseline**: Predicts based on recent form (last 3 gameweeks),
   weighted by recency.
2. **PointsPer90Baseline**: Predicts based on historical points per 90 minutes,
   adjusted for fixture difficulty.
3. **RollingExpectedPointsBaseline**: Uses the player's recent expected points
   (ep_this/ep_next) from FPL snapshots.
4. **FixtureAdjustedBaseline**: Combines recent form with fixture difficulty
   to produce a fixture-adjusted prediction.

## Evaluation Metrics

The `BacktestEvaluator` computes:

- **MAE** (Mean Absolute Error)
- **RMSE** (Root Mean Square Error)
- **Spearman rank correlation**
- **Top-k hit rates** (top-1, top-3, top-5, top-10)
- **Coverage** (fraction of players with predictions)

### Segmented Evaluation

Metrics can be computed by:
- Season
- Gameweek
- Player position (GK, DEF, MID, FWD)
- Price range (cheap, mid, expensive)

## Walk-Forward Validation

Walk-forward validation is the correct approach for time-series backtesting.
Unlike random train/test splits, it respects temporal ordering.

```python
from fpl_intelligence.backtesting.walk_forward import WalkForwardValidator

validator = WalkForwardValidator(db, feature_registry, model)
results = validator.validate(
    season="2025-26",
    start_gameweek=1,
    end_gameweek=38,
    min_train_gameweeks=3,
)
```

## Reproducibility

The `BacktestReproducer` ensures that backtest runs can be exactly
reproduced by recording:
- Configuration fingerprint (SHA-256 hash)
- Feature versions
- Model version
- Random seed

```python
from fpl_intelligence.backtesting.reproducibility import BacktestReproducer

reproducer = BacktestReproducer(db_session)
fingerprint = reproducer.compute_fingerprint(config, feature_versions, model_version)
```

## Reporting

The `BacktestReport` generates human-readable reports from backtest results.

```python
from fpl_intelligence.backtesting.reporting import BacktestReport

report = BacktestReport(db_session)
report_text = report.print_report(run_id, output="report.txt")
```

## Usage

```python
from fpl_intelligence.backtesting.engine import BacktestEngine
from fpl_intelligence.backtesting.models import BacktestConfig
from fpl_intelligence.backtesting.baselines import RecentFormBaseline
from fpl_intelligence.features.registry import FeatureRegistry

# Set up components
registry = FeatureRegistry(db_session)
registry.register(PlayerFormCalculator())
registry.register(MarketFeaturesCalculator())

model = RecentFormBaseline()
engine = BacktestEngine(db_session, registry, model)

# Configure and run
config = BacktestConfig(
    season="2025-26",
    start_gameweek=1,
    end_gameweek=3,
    random_seed=42,
)
run = engine.run(config)
print(f"Run {run.run_id} completed with status: {run.status}")
```
