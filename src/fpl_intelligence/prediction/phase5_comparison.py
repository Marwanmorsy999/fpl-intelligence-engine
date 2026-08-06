"""Phase 5 model comparison against Phase 4 baselines.

Compares the advanced player model against:
A. Recent form (baseline_a)
B. Minutes-adjusted (baseline_b)
C. Fixture-adjusted (baseline_c)
D. Phase 4 integrated baseline
E. Phase 5 advanced player model

Evaluation metrics:
- MAE, RMSE
- Spearman correlation
- top-5, top-10, top-20 capture
- calibration
- distribution calibration
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from fpl_intelligence.prediction.advanced_player.player_model import AdvancedPlayerModel
from fpl_intelligence.prediction.baselines import (
    FixtureAdjustedBaselineModel,
    MinutesAdjustedBaselineModel,
    RecentFormBaselineModel,
)
from fpl_intelligence.prediction.pipeline import PlayerBaselinePipeline


@dataclass
class ComparisonResult:
    """Result of comparing multiple models."""

    model_name: str
    n: int = 0
    mae: float = 0.0
    rmse: float = 0.0
    spearman: float = 0.0
    top5: float = 0.0
    top10: float = 0.0
    top20: float = 0.0
    calibration_error: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "n": self.n,
            "mae": round(self.mae, 4),
            "rmse": round(self.rmse, 4),
            "spearman": round(self.spearman, 4),
            "top5": round(self.top5, 4),
            "top10": round(self.top10, 4),
            "top20": round(self.top20, 4),
            "calibration_error": round(self.calibration_error, 4),
        }


class Phase5Comparison:
    """Compare Phase 5 advanced model against Phase 4 baselines."""

    def __init__(self) -> None:
        self._baselines = {
            "baseline_a": RecentFormBaselineModel(),
            "baseline_b": MinutesAdjustedBaselineModel(),
            "baseline_c": FixtureAdjustedBaselineModel(),
        }
        self._advanced_model = AdvancedPlayerModel()
        self._pipeline = PlayerBaselinePipeline()

    def compare(
        self,
        predictions: dict[str, dict[int, float]],
        actuals: dict[int, float],
    ) -> dict[str, ComparisonResult]:
        """Compare all models against actuals.

        Args:
            predictions: Dict mapping model_name -> {player_id: predicted_points}.
            actuals: Dict mapping player_id -> actual points.

        Returns:
            Dict mapping model_name -> ComparisonResult.
        """
        results: dict[str, ComparisonResult] = {}
        for model_name, preds in predictions.items():
            result = self._evaluate_model(model_name, preds, actuals)
            results[model_name] = result
        return results

    def _evaluate_model(
        self,
        model_name: str,
        predictions: dict[int, float],
        actuals: dict[int, float],
    ) -> ComparisonResult:
        """Evaluate a single model."""
        common = sorted(set(predictions) & set(actuals))
        if not common:
            return ComparisonResult(model_name=model_name)

        pred_vals = np.array([predictions[p] for p in common], dtype=float)
        actual_vals = np.array([actuals[p] for p in common], dtype=float)

        mae = float(np.mean(np.abs(pred_vals - actual_vals)))
        rmse = float(np.sqrt(np.mean((pred_vals - actual_vals) ** 2)))

        # Spearman correlation.
        spearman = self._spearman(pred_vals, actual_vals) if len(pred_vals) > 1 else 0.0

        # Top-k capture.
        n = len(common)
        top5 = self._top_k_capture(pred_vals, actual_vals, k=5)
        top10 = self._top_k_capture(pred_vals, actual_vals, k=10)
        top20 = self._top_k_capture(pred_vals, actual_vals, k=20)

        # Simple calibration error (MAE of predicted vs actual averages in bins).
        cal_error = self._calibration_error(pred_vals, actual_vals)

        return ComparisonResult(
            model_name=model_name,
            n=n,
            mae=mae,
            rmse=rmse,
            spearman=spearman,
            top5=top5,
            top10=top10,
            top20=top20,
            calibration_error=cal_error,
        )

    def _spearman(self, x: np.ndarray, y: np.ndarray) -> float:
        """Compute Spearman correlation."""
        if len(x) < 2:
            return 0.0
        rank_x = np.argsort(np.argsort(x)) + 1
        rank_y = np.argsort(np.argsort(y)) + 1
        d = rank_x - rank_y
        n = len(x)
        return 1.0 - (6.0 * np.sum(d ** 2)) / (n * (n ** 2 - 1))

    def _top_k_capture(self, preds: np.ndarray, actuals: np.ndarray, k: int) -> float:
        """Compute top-k capture rate."""
        if len(preds) == 0:
            return 0.0
        k = min(k, len(preds))
        top_pred_idx = np.argsort(preds)[-k:]
        top_actual_idx = np.argsort(actuals)[-k:]
        overlap = len(set(top_pred_idx) & set(top_actual_idx))
        return overlap / k

    def _calibration_error(self, preds: np.ndarray, actuals: np.ndarray) -> float:
        """Simple calibration error."""
        if len(preds) == 0:
            return 0.0
        bins = 10
        edges = np.linspace(0, max(preds.max(), 1.0), bins + 1)
        error = 0.0
        count = 0
        for i in range(bins):
            mask = (preds >= edges[i]) & (preds < edges[i + 1])
            if mask.sum() > 0:
                pred_mean = preds[mask].mean()
                actual_mean = actuals[mask].mean()
                error += abs(pred_mean - actual_mean)
                count += 1
        return error / count if count > 0 else 0.0
