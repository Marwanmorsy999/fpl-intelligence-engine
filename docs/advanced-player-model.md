# Advanced Player Model

## Model Architecture

The Phase 5 advanced player model is a **structured composite model** that combines component-level predictions through the `FPLScoringEngine` to produce FPL point distributions.

## Component Classification

| Component | Classification | Description |
|-----------|---------------|-------------|
| `MinutesModel` | Statistical model | Logistic regression or random forest with isotonic calibration |
| `GoalModel` | Heuristic baseline | Poisson distribution with position/fixture adjustments using xG |
| `AssistModel` | Heuristic baseline | Truncated Poisson with xA and key-pass factors |
| `CleanSheetModel` | Deterministic transform | `P(team CS) * P(player plays 60+ min)` |
| `BonusModel` | Heuristic baseline | BPS-based threshold lookup with event contribution |
| `DefensiveContributionModel` | Heuristic baseline | Action-rate threshold model |
| `DistributionEngine` | Deterministic transform | Monte Carlo simulation over component expectations |
| `FPLScoringEngine` | Deterministic transform | Converts statistical outcomes to FPL points |

## Honest Assessment

The current Phase 5 components are **NOT genuine predictive models** in the statistical sense. They are **structured heuristic baselines** that combine:

1. Historical player rates (goals/90, assists/90, BPS/90)
2. Fixture context (team xG, opponent strength, home/away)
3. Expected minutes from the minutes model
4. Position-specific multipliers

The `GoalModel` uses a weighted blend of historical goal rate and xG with a fixture factor, applies Poisson, then multiplies by position factor. This is not a learned predictive model — it is a deterministic formula.

True advanced predictive models would require:
- Training on historical goal events with learned coefficients
- Cross-validated feature selection
- Calibrated probability outputs validated against holdout data
- Model comparison against simpler alternatives

## Components vs Architecture

```
Player Features
    |
    +---> MinutesModel (statistical)
    |       └─ expected_minutes, prob_starting
    |
    +---> GoalModel (heuristic baseline)
    |       └─ Poisson(xG + goals/90, position, fixture)
    |
    +---> AssistModel (heuristic baseline)
    |       └─ Poisson(xA + assists/90, position)
    |
    +---> CleanSheetModel (deterministic)
    |       └─ P(team CS) * P(player plays 60+)
    |
    +---> BonusModel (heuristic baseline)
    |       └─ BPS threshold + goal/assist bonus
    |
    +---> DefensiveContributionModel (heuristic baseline)
    |       └─ Action-rate threshold
    |
    +---> FPLScoringEngine (deterministic)
    |       └─ Versioned scoring rules
    |
    +---> DistributionEngine (Monte Carlo)
            └─ Full point distribution
```

## Data Completeness

Completeness is computed per-component and aggregated. Each component reports:
- `available`: Whether sufficient data exists for reliable prediction
- `data_completeness`: Fraction of required features present
- Feature coverage thresholds are documented in each component

## Uncertainty

Uncertainty is decomposed into:
- `minutes_uncertainty`: Based on starting probability
- `performance_uncertainty`: Based on goal expectation
- `assist_uncertainty`: Based on assist expectation

Note: This decomposition is a heuristic and may not correspond to actual prediction error differences. Validation is required.
