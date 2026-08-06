# Transfer Optimization

The `TransferOptimizer` evaluates the expected net gain of a candidate transfer over a multi-gameweek planning horizon.

## Roll vs Transfer
The engine specifically compares the EV of using a transfer against rolling it. Hits are only recommended if their EV gain over the planning horizon explicitly beats the hit cost plus the value of flexibility loss.

## Multi-transfer Planner
Uses heuristic candidate filtering to avoid combinatorial explosion before identifying the best multi-transfer plan using beam search or ranked evaluations.
