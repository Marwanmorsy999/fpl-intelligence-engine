# Phase 5 Holdout Policy

## Overview

The holdout policy enforces a strict separation between development/validation data and the locked final holdout season. The policy prevents data leakage from the holdout season into model training, hyperparameter tuning, feature selection, calibration fitting, and model selection.

## Season Assignments

| Role | Seasons |
|------|---------|
| Development | 2022-23, 2023-24, 2024-25 |
| Validation | 2022-23, 2023-24, 2024-25 |
| Locked Final Holdout | 2025-26 |

## Modes

Three explicit modes control what data is accessible:

### Development Mode (`HoldoutMode.DEVELOPMENT`)
- Allowed seasons: 2022-23, 2023-24, 2024-25
- Access to 2025-26 raises `HoldoutViolationError`
- Used for: model training, hyperparameter tuning, feature selection, calibration fitting, model selection

### Validation Mode (`HoldoutMode.VALIDATION`)
- Allowed seasons: 2022-23, 2023-24, 2024-25
- Access to 2025-26 raises `HoldoutViolationError`
- Used for: temporal validation, development metric computation

### Final Holdout Evaluation Mode (`HoldoutMode.FINAL_HOLDOUT_EVALUATION`)
- Read-only access to 2025-26 observations
- Training on 2025-26 data is blocked (raises `HoldoutViolationError`)
- Used for: frozen model evaluation only

## Enforcement Points

Every training entry point enforces the holdout:

1. **`TrainingDataBuilder.build_player_dataset()`** — checks target gameweek's season code
2. **`TrainingDataBuilder.build_team_dataset()`** — checks target gameweek's season code
3. **`WalkForwardTrainer.run()`** — checks season at entry
4. **`WalkForwardValidator.validate()`** — checks season at entry
5. **`enforce_holdout()`** — central enforcement function used by all above

## Cutoff Date

2025-26 holdout cutoff: August 31, 2025

Observations with target dates on or after this cutoff in the 2025-26 season are blocked in development/validation modes. This prevents any early-2025-26 data from leaking into development.

## Tests

17 tests in `TestHoldoutPolicy` and `TestHoldoutSemantics` verify:
- Development seasons are allowed
- Holdout season is blocked in development and validation
- Holdout season cannot be trained on even in evaluation mode
- Cutoff date blocks pre-holdout-window data
- Multiple holdout seasons are blocked together
- Preprocessing, feature selection, and calibration cannot use holdout data
