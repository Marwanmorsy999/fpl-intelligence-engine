# Minutes validation rerun

This branch exists to execute the canonical historical Minutes walk-forward validation against the current production code, including the already-implemented expected-minutes blend. It does not alter production behavior.

The promotion gate remains unchanged: a candidate must beat every required baseline on expected-minutes MAE and start Brier on the same canonical outer rows.
