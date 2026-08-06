# Temporal Integrity

## Overview

Temporal integrity is the cornerstone of valid backtesting. It ensures that
every prediction made during a backtest uses only data that was actually
available at the historical decision cutoff. Violating temporal integrity
introduces **look-ahead bias**, which can make a model appear to perform
better than it actually would in production.

## Temporal Fields

The FPL Intelligence Engine tracks five temporal fields on all performance
and snapshot tables:

| Field | Description |
|-------|-------------|
| `event_time` | When the football event occurred (e.g., match kickoff). |
| `published_at` | When the source published the information. |
| `available_at` | The earliest timestamp at which our system can legitimately be considered to have accessed the information. |
| `ingested_at` | When our pipeline actually collected the data. |
| `source_last_modified_at` | When the source last modified the underlying record. |

### Key Distinction: `available_at` vs `ingested_at`

The backtester must reason about `available_at`, not merely `published_at`
or `ingested_at`:

- **`available_at`** represents when the information became publicly available.
  A model could theoretically have accessed this information even if our
  pipeline didn't actually ingest it.
- **`ingested_at`** represents when our pipeline actually collected the data.
  This is a system-level constraint, not a theoretical one.

For strict reproducibility, both conditions must be met: the data must have
been both publicly available AND actually in our system before the cutoff.

## Information-Access Policies

Three policies govern what data is available at a given cutoff:

### PUBLIC_AVAILABILITY

```
Condition: available_at <= cutoff
```

Uses information if it was publicly available at or before the cutoff,
regardless of whether our pipeline actually ingested it. This is the most
permissive policy and assumes the system could have accessed any public
information.

**Use case:** Simulating an idealized system that could access all public
information.

### SYSTEM_AVAILABILITY

```
Condition: ingested_at <= cutoff
```

Uses information only if our pipeline actually collected it before the
cutoff, regardless of when it was published. This is a system-level
constraint.

**Use case:** Simulating the actual system's capabilities, including
pipeline delays.

### STRICT_REPRODUCIBILITY (Default)

```
Condition: available_at <= cutoff AND ingested_at <= cutoff
```

Uses information only if it was both publicly available AND actually in
our system before the cutoff. This is the strictest and most conservative
policy, ensuring that the backtest only uses information that was both
publicly available and actually collected.

**Use case:** Default for all backtesting. Ensures results are
reproducible and conservative.

## Decision Cutoff

A `DecisionCutoff` represents the point in time at which a prediction
decision must be made. It is derived from the Gameweek deadline time,
adjusted by a configurable offset.

```python
from fpl_intelligence.backtesting.cutoff import get_gameweek_decision_cutoff

cutoff = get_gameweek_decision_cutoff(
    db_session,
    season="2025-26",
    gameweek=1,
    offset=timedelta(hours=1),  # Decide 1 hour before deadline
)
```

The cutoff time is the **hard boundary**: any data with `available_at`
or `ingested_at` after this time must not be used in feature computation
or prediction.

## Temporal Query Helpers

### `as_of(cutoff_time, column)`

Returns a SQLAlchemy filter condition for `column <= cutoff_time`.
This is the basic temporal filter.

### `apply_policy(model, policy, cutoff_time)`

Returns a SQLAlchemy filter condition that enforces the given
information-access policy on the model. Raises `ValueError` if the
model lacks the required temporal columns.

### `TemporalQueryBuilder`

Wraps a SQLAlchemy query with temporal cutoff filters. Ensures that
all queries respect the no-look-ahead rule.

```python
builder = TemporalQueryBuilder(db, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY)
results = builder.query_with_filter(
    FPLSnapshot,
    FPLSnapshot.player_id == 123,
)
```

### `is_record_available(record, cutoff_time, policy)`

Checks if a single record is available under the given policy.
Used for filtering in-memory lists of entities.

## PREDICTION_TIME vs OUTCOME_TIME Separation

The backtest engine enforces a **clear separation** between:

1. **PREDICTION_TIME** (Steps 1-5):
   - Determine the decision cutoff
   - Freeze all available information as-of that cutoff
   - Compute features using only data available at the cutoff
   - Generate predictions using the configured prediction model
   - Store predictions with the cutoff timestamp

2. **OUTCOME_TIME** (Steps 6-7):
   - Reveal actual outcomes **only for evaluation**
   - Calculate evaluation metrics

Outcome data is **never** allowed to flow backward into prediction features.
This is enforced by:
- Using `TemporalQueryBuilder` for all data access
- Applying `InformationAccessPolicy` filters on every query
- Storing predictions with a frozen cutoff timestamp
- Separating feature computation from outcome evaluation

## Feature Cache and Temporal Integrity

The `FeatureCache` includes the cutoff time in its cache key, ensuring
that cached values are never reused across different cutoffs. This
prevents a subtle form of look-ahead bias where a feature computed
for a later cutoff is incorrectly used for an earlier one.

Cache key components:
- `feature_name`
- `feature_version`
- `entity_id`
- `cutoff_time` (ISO format)
- `data_version` (optional)

## Testing Temporal Integrity

The test suite includes dedicated tests for temporal integrity:

- `tests/unit/test_temporal_queries.py`: Tests for policy enforcement,
  cutoff boundary conditions, and temporal query helpers.
- `tests/unit/test_leakage.py`: Tests that verify no-look-ahead is
  enforced at the query level and in feature calculators.

Key test patterns:
1. **Future data exclusion**: Verify that data with `available_at > cutoff`
   is excluded from queries.
2. **Policy enforcement**: Verify that each policy correctly filters
   based on its conditions.
3. **Calculator isolation**: Verify that feature calculators don't
   use data from after the cutoff.
4. **Cache isolation**: Verify that cache keys include the cutoff time.

## Common Pitfalls

1. **Using `published_at` instead of `available_at`**: `published_at`
   represents when the source published the information, but `available_at`
   represents when our system could legitimately use it. Always use
   `available_at` for backtesting.

2. **Forward-filling across unknown periods**: When a player has no
   data at a cutoff, don't forward-fill from future data. Mark the
   feature as missing instead.

3. **Using final fixture results for pre-match features**: Fixture
   results (goals, scores) should not be used when constructing
   pre-match features. Only use data that was known before the match.

4. **Cache reuse across cutoffs**: Never reuse cached feature values
   across different cutoff times. The cache key must include the cutoff.

5. **Random train/test splits**: Never use random splitting for
   time-series data. Always use walk-forward validation.
