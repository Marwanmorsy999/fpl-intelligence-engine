# Phase 6.5 Pre-flight Audit

## Inspection Findings

### Core Architecture
- **Optimizer Interfaces**: Implemented. Clean abstractions via `CandidateAction` and `Recommendation`.
- **Simulation Integration**: Placeholder. `simulate_decision` currently just aggregates EV instead of running Monte Carlo sampling over actual prediction distributions.
- **Prediction Provider Integration**: Implemented via `DecisionPredictionProvider` interface.
- **FPL Rule Configuration**: Partially Implemented. Has a basic configuration via `FPLRules`, but lacks the 2026/27 specific chip rules (two sets per half-season).
- **Squad Constraints**: Implemented via `FPLRules` (budget, clubs, formation limits).

### FPL Mechanics
- **Transfer Rules**: Implemented. Models free transfers, rolling (up to 5 banked for modern rules), and hits.
- **Hit Costs**: Implemented (-4 per extra transfer).
- **Chip Rules**: Mock-only/Partially Implemented. Currently only models Bench Boost and Triple Captain using EV heuristics. Missing Wildcard and Free Hit completely.
- **Decision Persistence**: Implemented via `DecisionRecorder`.

### Optimization Logic
- **Captain Optimization**: Partially Implemented. Extracts `p10`, `p90`, and `variance` correctly from distributions if present, but falls back to point estimates if not.
- **Starting XI Optimization**: Partially Implemented. Uses brute-force heuristic over combinations, falling back to EV instead of distributional evaluation for edge cases.
- **Transfer Optimization**: Partially Implemented. Uses basic EV sum over horizon rather than full stochastic simulation. 
- **Roll vs Transfer**: Partially Implemented. Uses linear heuristic (`prob = 0.5 + net_points * 0.05`) instead of true probability derived from prediction distributions.

### Backtesting Integration
- **Historical Backtesting Engine**: Placeholder. `DecisionBacktester` class exists but returns hardcoded 0.0 metrics. No temporal reconstruction or evaluation logic present yet.
- **Decision Objective Handling**: Implemented.

## Summary Status

- **Fully implemented**: `SquadState`, `DecisionObjective`, `DecisionPredictionProvider`, `DecisionRecorder`.
- **Partially implemented**: `CaptainOptimizer`, `StartingXIOptimizer`, `TransferOptimizer`, `MultiTransferPlanner`, FPL Rules constraints.
- **Placeholder**: `simulate_decision()`, `DecisionBacktester`.
- **Mock-only**: `ChipSimulator` (only covers BB/TC heuristically).
- **Real-data validated**: None (awaiting Phase 6.5 backtesting framework execution).

## Required Actions for Phase 6.5
1. **Fix Rules**: Introduce 2 sets of chips per half-season for 2026/27.
2. **Complete Chips**: Implement Wildcard and Free Hit logic; fix BB/TC logic.
3. **Fix Simulation**: Transition `simulate_decision()` to actually use Monte Carlo over `pred.distribution`.
4. **Implement Framework**: Fill out `DecisionBacktester` to reconstruct historical squads and evaluate objectives sequentially without future data leaks.
