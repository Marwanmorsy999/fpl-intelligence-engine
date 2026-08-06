# Decision Backtesting

The `DecisionBacktester` exists to validate the optimization logic over historical seasons using lock-forward constraints.

## Goal
A good predictive model can still result in bad decisions if the optimizer is flawed (e.g. taking too many hits). Backtesting tests explicitly:
- Always roll
- Transfer on highest expected gain
- No hits vs Hits
- Simple fixture captain vs EV captain

## Implementation
`DecisionRecorder` logs every recommendation immutably.
`DecisionBacktester` replays gameweeks and tracks net transfer ROI, points gained, and downside variance.
