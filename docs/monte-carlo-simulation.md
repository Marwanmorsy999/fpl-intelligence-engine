# Monte Carlo Simulation

## Overview

Monte Carlo simulation is used at multiple levels in the Phase 5 prediction pipeline:
1. Match simulation (Poisson goal sampling)
2. Player simulation (component-level sampling)
3. Joint simulation (match + player)
4. Gameweek simulation (squad + autosub + captain)

## Match Simulator

The `MatchSimulator` samples goals from independent Poisson distributions:
```
home_goals ~ Poisson(lambda_home)
away_goals ~ Poisson(lambda_away)
```

Outputs include scoreline distribution, home/draw/away probabilities, and clean-sheet probabilities.

## Player Simulation

The `DistributionEngine` samples per-player outcomes:
```
goals ~ Poisson(expected_goals)
assists ~ Poisson(expected_assists)
minutes ~ Normal(appearance_minutes, σ) clipped [0, 90]
clean_sheet ~ Bernoulli(expected_clean_sheet)
bonus ~ Bernoulli(prob) * sample([1,2,3])
def_contrib ~ Bernoulli(def_prob)
```

## Joint Simulation

The `JointSimulator` combines match and player simulation, preserving dependencies:
- Player goals are conditional on team scores
- Clean sheets are derived from match outcomes
- Player-level events respect match-level constraints

## Gameweek Simulation

The `AdvancedGameweekSimulator` adds:
- Autosub rules (bench substitution when starters have low minutes)
- Captain multiplier tracking
- Vice-captain fallback
- Multi-gameweek support

## Convergence

Simulation convergence should be validated by comparing results at 1k, 10k, and 50k simulations. Key metrics to monitor:
- Expected points stability
- P10/P90 stability
- Tail probability (P15+) stability

## Production Recommendation

Default: 10,000 simulations per prediction.

For high-stakes decisions (captain selection, chip strategy), 50,000+ simulations may be warranted to reduce sampling variance in tail probabilities.
