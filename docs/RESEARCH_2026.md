# FPL Intelligence Engine — 2026 Research Brief

Date: 2026-08-27

## Product thesis

ANALYZER should compete on decision quality, not raw stat count. The primary question is: **what should I do before the deadline, why, how confident are we, and what changes the answer?**

## Market observations

Official FPL now includes richer planning/price/ranking functionality, so reproducing native features alone is not a moat. Third-party tools demonstrate demand for multi-week projections, effective ownership, live rank, player comparison and consolidated statistics.

Important current references:
- Official FPL: https://fantasy.premierleague.com/
- 2026/27 overview: https://www.premierleague.com/en/news/4679873
- Price Change Predictor: https://www.premierleague.com/en/news/4680462
- BPS changes: https://www.premierleague.com/en/news/4679946
- FPL Team: https://fpl.team/
- LiveFPL: https://www.livefpl.com/
- FPL Effective Ownership: https://www.livefpl.com/blog/fpl-effective-ownership
- Fantasy Football Scout Stats Centre: https://www.fantasyfootballscout.co.uk/2026/08/19/introducing-our-new-fpl-stats-centre
- FPL Review: https://fplreview.com/
- Fantasy Football Hub: https://www.fantasyfootballhub.co.uk/

## Differentiation

Do NOT attempt to win by making another enormous sortable table.

Differentiating capabilities should be:

1. expected-minutes-first forecasting
2. full probability distributions instead of one xP number
3. model consensus/disagreement and calibrated confidence
4. decision simulation (hold vs transfer vs hit)
5. risk-aware captain and chip optimization
6. rank-attack/rank-defense objective functions
7. rival-specific strategy
8. evidence/provenance tied to every important decision
9. time-sensitive intelligence feed
10. transparent track record/backtesting

## Modeling research direction

Use simple, strong statistical baselines first; only add complexity when walk-forward evaluation proves an improvement.

Candidate families:
- rolling-rate baselines
- Poisson / Dixon-Coles team scoring models
- Bayesian hierarchical team/player models
- gradient boosting (LightGBM/XGBoost)
- minutes classifier/regressor
- probability calibration
- ensemble stacking or weighted blending
- Monte Carlo scenario simulation

Required evaluation:
- MAE
- RMSE
- log loss
- Brier score
- calibration curves / reliability
- rank correlation
- top-k asset hit rate
- captain ROI
- transfer decision ROI
- chip ROI

Never use random temporal splits for historical FPL decision modeling.

## Feature hierarchy

Highest expected value:
1. expected minutes
2. fixture/team strength
3. player attacking/defensive rates
4. role/set-piece context
5. availability/injury information
6. ownership/EO
7. price dynamics
8. tactical context
9. weather only if empirically useful

Do not hard-code this hierarchy as weights. Use it as a research priority order and learn/validate weights from temporal data.

## Key prediction outputs

For every player:
- P(start)
- expected minutes
- P(60+)
- P(appearance)
- goals distribution
- assists distribution
- clean-sheet probability
- bonus probability
- defensive-contribution probability
- card probability
- GK save distribution
- expected FPL points
- median/floor/ceiling
- P25/P75/P90
- model confidence
- model disagreement

## Decision outputs

For every relevant decision:
- recommendation
- expected net gain
- downside
- upside
- confidence
- alternative actions
- time sensitivity
- evidence
- assumptions

## Temporal research rule

For each feature document:
- when the underlying information was published
- when it was realistically available
- when the FPL decision cutoff occurs
- whether the feature is safe in strict backtest

Classify data as:
- STRICT_BACKTEST_SAFE
- PRE_DEADLINE_BUT_UNCERTAIN
- POST_DEADLINE_OUTCOME_ONLY
- UNSAFE_LOOKAHEAD

## News/tactical research

Current project audits correctly identify a historical temporal gap: confirmed lineups often occur after the FPL deadline. Therefore confirmed lineups can be live information but should not leak into historical pre-deadline models.

Prioritize pre-deadline signals:
- manager press conferences
- training-return reports
- official injury updates
- suspension status
- role comments
- previous selection patterns
- fixture congestion

Treat rumor sources with lower evidence confidence.

## API strategy

No user-entered API keys.

Server-side provider registry with:
- provider
- enabled
- cost tier
- request quota
- per-minute limit
- freshness
- reliability
- capabilities
- terms status
- cache TTL
- priority

## Research conclusion

The next product milestone should be a **Trusted Decision Engine**. Build fewer features, but ensure every feature shares the same canonical data, model, temporal and provenance contracts.
