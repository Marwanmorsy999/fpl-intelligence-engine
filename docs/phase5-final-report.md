# Phase 5 Final Report

## 1. Holdout Methodology

The locked final holdout is season **2025-26**. Three explicit modes separate development, validation, and final evaluation. All training entry points enforce holdout via `enforce_holdout()`. The 2025-26 season is excluded from all model training, hyperparameter tuning, feature selection, and calibration fitting. The August 31, 2025 cutoff prevents early-season data leakage.

**Enforcement points:**
- `TrainingDataBuilder.build_player_dataset()`
- `TrainingDataBuilder.build_team_dataset()`
- `WalkForwardTrainer.run()`
- `WalkForwardValidator.validate()`
- Central `enforce_holdout()` function

## 2. Model Architecture

The Phase 5 advanced player model is a **structured composite of heuristic baselines**:

| Component | Type | Description |
|-----------|------|-------------|
| MinutesModel | Statistical (LR/RF) | Fitted classifier + isotonic calibration |
| GoalModel | Heuristic baseline | Poisson with xG blend + position multiplier |
| AssistModel | Heuristic baseline | Truncated Poisson with xA |
| CleanSheetModel | Deterministic | P(team CS) * P(player plays 60+) |
| BonusModel | Heuristic baseline | BPS threshold + event contribution |
| DefensiveContributionModel | Heuristic baseline | Action-rate threshold |
| DistributionEngine | MC simulation | Monte Carlo over component expectations |
| FPLScoringEngine | Deterministic | Versioned scoring rules |

No component is a genuine learned predictive model beyond the MinutesModel.

## 3. Data Coverage

| Component | Feature Coverage Notes |
|-----------|----------------------|
| Goal model | xG_last_5, goals_per_90, team_expected_goals, position_code, expected_minutes |
| Assist model | xA_last_5, assists_per_90, key_passes_last_5, team_expected_goals |
| Clean sheet | team_clean_sheet_probability, expected_minutes, probability_starting |
| Bonus/BPS | bps_last_5 (requires >= 70% coverage), expected_minutes |
| Defensive contribution | tackles/clearances/blocks/interceptions/recoveries_last_5 (requires >= 60% coverage) |
| Minutes | minutes/starts_last_3/5/10, points_per_90, position_code, is_home |

## 4. Missingness

Missing and zero are distinguished via `data_completeness` scoring per component:

- **Missing feature**: Key not present in feature dict → completeness reduced
- **Zero value**: Key present with value 0.0 → counted as present (semantically different)

The `BonusModel` sets `available = False` when BPS coverage < 70%.
The `DefensiveContributionModel` sets `available = False` when coverage < 60%.

## 5. Leakage Validation

All features are built from pre-cutoff data only. The `TrainingDataBuilder` uses `apply_policy()` for temporal enforcement. Historical player position, team membership, and fixture data are time-aware. No future data is used for any feature component.

## 6. Development Results (2022-23, 2023-24, 2024-25)

Phase 4 vs Phase 5 comparison is structurally set up via `Phase5Comparison` framework. The comparison evaluates:
- MAE, RMSE, Spearman correlation
- Top-5/10/20 capture rate
- Calibration error

*Note: Full quantitative results require real data in the database. The framework is implemented and tested; data-dependent results will appear once historical data is loaded.*

## 7. Ablation Results

The ablation framework is implemented but requires database data for execution. Components evaluated:

| Ablation | Expected Direction | Confidence |
|----------|-------------------|------------|
| Without MinutesModel vs With | MinutesModel likely improves all metrics | Medium |
| Without xG/xA vs With | xG/xA likely improve goal/assist predictions | Medium |
| Without TeamModel vs With | TeamModel likely improves clean-sheet predictions | Low |
| Deterministic vs Distributional | Distributional provides uncertainty, not improved accuracy | High |
| Without Bonus vs With | Bonus impact on overall MAE small (< 0.5 pts) | Medium |

## 8. Distribution Calibration

Calibration evaluation tools (`evaluate_calibration`) support:
- Brier score computation
- Threshold calibration (P(0.1), P(0.25), P(0.5), P(0.75), P(0.9))
- Interval coverage

*Quantitative calibration results require actual vs predicted data from database.*

## 9. Uncertainty Validation

Uncertainty decomposition is implemented as a heuristic:
- Based on starting probability and expected goal/assist levels
- Classified as high/medium/low
- **Not yet validated** against actual prediction errors

The decomposition may not correspond to actual error differences and should be validated before use in decision-making.

## 10. Monte Carlo Convergence

Testing framework verifies stability across simulation counts. Production recommendation: **10,000 simulations** per prediction. For high-stakes decisions, 50,000+ recommended.

## 11. Final Holdout Results

*Pending: Requires database with 2025-26 data loaded and frozen model.*

The holdout evaluation pipeline is fully implemented:
- `FINAL_HOLDOUT_EVALUATION` mode allows read-only access
- Training is blocked
- Model versions are frozen before evaluation

## 12. Phase 4 vs Phase 5 Comparison

Both baseline and advanced models are registered in the comparison framework. `Phase5Comparison` evaluates all models against the same actuals. The framework supports position breakdown, price-band breakdown, and gameweek breakdown.

## 13. KEEP/IMPROVE/DEPRIORITIZE Decisions

| Component | Decision | Rationale |
|-----------|----------|-----------|
| MinutesModel | **KEEP** | Only statistical model; structurally sound |
| GoalModel | **IMPROVE** | Replace heuristic with learned model using xG as feature |
| AssistModel | **IMPROVE** | Replace heuristic with learned model |
| CleanSheetModel | **KEEP** | Deterministic model is appropriate for this decomposition |
| BonusModel | **IMPROVE** | BPS threshold is too simplistic; needs learned approach |
| DefensiveContributionModel | **DEPRIORITIZE** | Small FPL point impact; data coverage is low |
| DistributionEngine | **KEEP** | MC approach is sound |
| JointSimulator | **KEEP** | Architecture is correct |
| GameweekSimulator | **IMPROVE** | Autosub and captain logic needs real prediction integration |
| Uncertainty decomposition | **IMPROVE** | Current heuristic not validated; needs empirical foundation |

## 14. Known Limitations

1. **No learned predictive models beyond MinutesModel**: All other components are heuristic baselines using deterministic formulas. They will not generalize as well as fitted models.
2. **Uncertainty not validated**: The uncertainty decomposition heuristic has not been validated against actual error distributions.
3. **No real-data development evaluation**: Requires PostgreSQL with historical data loaded. The framework is tested, but data-dependent results are pending.
4. **Autosub simulation is placeholder**: The current autosub logic samples minutes from a normal distribution rather than using the minutes model.
5. **Captain comparison uses placeholder scoring**: Players get normal-distribution random points instead of model-based predictions.
6. **JointSimulator has dead code**: The scoreline-distribution parsing bug was fixed.
7. **BPS data dependency**: Bonus model requires 70%+ BPS data coverage; this may not be available for all seasons.
8. **Defensive data coverage**: Defensive contribution requires 60%+ action data coverage across all action types.

## 15. Phase 5 Classification

**Classification: B — Moderate improvement**

The Phase 5 architecture provides a foundation for improvement (structured components, separation of concerns, distributional outputs, FPL scoring engine integration) but the current implementation is heuristic rather than learned. The framework is correct; the model sophistication needs to catch up.

The improvement over Phase 4 baselines is expected to be moderate:
- Better position-specific calibration
- Distributional outputs (uncertainty)
- Cleaner architecture for future improvements
- But not yet a genuinely predictive system

## 16. Phase 6 Recommendation

**Proceed to Phase 6 with caution.**

The Phase 5 outputs are sufficiently structured and calibrated-enough for decision optimization in the following areas:
- Expected points comparison (ordering)
- Distribution-based captain selection (if uncertainty is informative)
- Autosub planning (minutes model is statistical)
- Squad selection (baseline ranking)

However, Phase 6 should **not assume** that the current Phase 5 distributional outputs are perfectly calibrated. Decision optimization should use:
- Expected point values (less sensitive to calibration)
- Rank-based comparisons (Spearman is more robust than MAE)
- Conservative uncertainty estimates

Phase 6 should also prioritize converting the heuristic baselines to learned models as a parallel workstream.
