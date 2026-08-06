# Decision Engine Architecture

The Phase 6 Decision Optimization Engine sits on top of the Phase 5 predictive models. It consumes predictive distributions and outputs evaluated FPL decisions.

## Layers

1. **Prediction Interface (`DecisionPredictionProvider`)**: Abstracts the underlying predictive models so the optimizer doesn't hardcode them.
2. **Squad State (`SquadState`)**: Represents the canonical user squad.
3. **FPL Rules (`FPLRules`)**: Defines constraints like budget and formation limits.
4. **Optimizers**: `CaptainOptimizer`, `TransferOptimizer`, `StartingXIOptimizer`, `ChipSimulator`.
5. **Backtester (`DecisionBacktester`)**: Evaluates strategies historically.

## Decision Robustness
Decisions aren't just based on expected points. They include a `base_case`, `upside_case` (P90), and `downside_case` (P10) to understand variance.
