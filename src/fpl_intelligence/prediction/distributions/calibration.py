"""Distribution calibration utilities for Phase 5."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class CalibrationReport:
    """Calibration evaluation for distribution predictions."""

    threshold_calibration: dict[str, float] = field(default_factory=dict)
    interval_coverage: dict[str, float] = field(default_factory=dict)
    brier_scores: dict[str, float] = field(default_factory=dict)
    n_samples: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold_calibration": self.threshold_calibration,
            "interval_coverage": self.interval_coverage,
            "brier_scores": self.brier_scores,
            "n_samples": self.n_samples,
        }


def evaluate_calibration(
    predicted_probs: np.ndarray,
    outcomes: np.ndarray,
    thresholds: list[float] | None = None,
) -> CalibrationReport:
    """Evaluate calibration of predicted probabilities against outcomes.

    Args:
        predicted_probs: Predicted probabilities (0-1).
        outcomes: Binary outcomes (0 or 1).
        thresholds: List of probability thresholds to evaluate.

    Returns:
        CalibrationReport with threshold calibration and Brier scores.
    """
    if thresholds is None:
        thresholds = [0.1, 0.25, 0.5, 0.75, 0.9]

    threshold_cal: dict[str, float] = {}
    for t in thresholds:
        mask = predicted_probs >= t
        if mask.sum() > 0:
            threshold_cal[f"p_{t}"] = round(float(np.mean(outcomes[mask])), 4)
        else:
            threshold_cal[f"p_{t}"] = 0.0

    # Brier score.
    brier = round(float(np.mean((predicted_probs - outcomes) ** 2)), 4)

    return CalibrationReport(
        threshold_calibration=threshold_cal,
        brier_scores={"overall": brier},
        n_samples=len(outcomes),
    )
