# FPL Point Distributions

## Architecture

The distribution engine converts expected component outcomes into full FPL point distributions via Monte Carlo simulation.

## Flow

```
Component Expectations (expected_goals, expected_assists, ...)
    |
    v
Monte Carlo Sampling (n simulations with deterministic seed)
    |
    v
Per-Simulation FPL Scoring (through FPLScoringEngine)
    |
    v
Point Distribution (percentiles, tail probs, floor/ceiling)
```

## Implementation

The `DistributionEngine` performs:
1. Poisson sampling for goals and assists
2. Normal sampling (clipped) for minutes
3. Bernoulli sampling for clean sheet
4. Threshold-based bonus probability with 3-tier bonus points
5. Defensive contribution Bernoulli sampling
6. Per-simulation FPL points computation via `FPLScoringEngine`

## PointDistribution Fields

| Field | Description |
|-------|-------------|
| `expected_points` | Mean of all simulations |
| `p10` | 10th percentile |
| `p25` | 25th percentile |
| `p50` | Median |
| `p75` | 75th percentile |
| `p90` | 90th percentile |
| `p_2_plus` | P(points >= 2) |
| `p_5_plus` | P(points >= 5) |
| `p_10_plus` | P(points >= 10) |
| `p_15_plus` | P(points >= 15) |
| `floor` | 5th percentile |
| `ceiling` | 95th percentile |
| `samples` | Raw simulation array (for calibration) |

## Reproducibility

Same inputs + same seed + same simulation count = identical distributions.

Seeds are configurable. The default seed is 42.

## Version-Specific Rules

Scoring rules are loaded from the `FPLScoringEngine`, which supports different rules versions (e.g., `config/fpl_rules/2026-27.yaml`). The 2026/27 rule configuration is isolated from historical seasons via the `with_rules()` method.
