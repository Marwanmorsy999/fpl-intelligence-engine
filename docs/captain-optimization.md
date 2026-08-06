# Captain Optimization

The `CaptainOptimizer` evaluates candidates using the predictive distributions rather than simple point estimates. 

## Objectives
- **Protect Mode**: Minimizes downside relative to a benchmark or rival. Selects a safe captain with the highest P10 floor.
- **Balanced Mode**: Maximizes EV.
- **Chase Mode**: Maximizes probability of a rank swing by selecting differentials with the highest P90 ceiling and highest probability of scoring 15+ points.

Returns a recommendation with full confidence intervals (`base_case`, `downside_case`, `upside_case`).
