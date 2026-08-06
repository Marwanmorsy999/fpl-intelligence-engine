"""Baseline prediction models for Phase 4.

Three interpretable baselines establish the naive benchmark for the
quantitative prediction layer:

Baseline A — Recent Form:
    Predict future points using the last 3, 5, or 10 gameweeks, supporting
    weighted combinations.

Baseline B — Minutes-Adjusted Form:
    Adjust recent points for minutes. Concepts:
        - points per 90 (pp90)
        - minutes-adjusted rolling points
        - expected points based on recent minutes

Baseline C — Fixture-Adjusted Form:
    Combine recent player performance, opponent strength, home/away, and
    expected minutes. This tests whether fixture adjustment adds value.

These baselines are intentionally simple and transparent. They implement
the ``PredictionModel`` protocol and are usable directly by the backtesting
engine and the walk-forward trainer.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from fpl_intelligence.prediction.base import PredictionModel

FEATURE_WINDOWS = (3, 5, 10)


class RecentFormBaselineModel(PredictionModel):
    """Baseline A: recent form across configurable windows.

    ``expected_points = sum(w_i * avg_points_window_i)`` where the weights
    are normalized across the 3/5/10 gameweek windows. A single window can
    be selected by passing ``window`` to the constructor.
    """

    def __init__(self, window: int | None = None, seed: int = 42) -> None:
        self._window = window
        self._seed = seed

    @property
    def model_name(self) -> str:
        return "baseline_a_recent_form"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def metadata(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_type": "baseline",
            "feature_version": "1.0.0",
            "hyperparameters": {"window": self._window},
            "random_seed": self._seed,
        }

    def _window_average(self, features: dict[str, float], window: int) -> float:
        """Average points per gameweek over the last ``window`` gameweeks."""
        key = f"points_last_{window}"
        if key in features:
            n = features.get("n_season_matches", window)
            # points_last_{window} is a SUM; convert to per-game average.
            actual_n = min(int(n), window)
            if actual_n > 0:
                return features[key] / actual_n
        # Fall back to rolling points fields if available.
        return 0.0

    def fit(self, X: Any, y: Any, context: dict[str, Any] | None = None) -> PredictionModel:
        # Baselines are not trained; return self.
        return self

    def predict(self, X: Any, context: dict[str, Any] | None = None) -> Any:
        """Predict for a single feature dict or a list of feature dicts."""
        if isinstance(X, dict):
            return self._predict_one(X)
        return np.array([self._predict_one(x) for x in X])

    def _predict_one(self, features: dict[str, float]) -> float:
        if self._window is not None:
            return self._window_average(features, self._window)

        # Weighted combination across windows: more recent windows weighted higher.
        weights = {3: 0.5, 5: 0.3, 10: 0.2}
        total = 0.0
        weight_sum = 0.0
        for window, weight in weights.items():
            avg = self._window_average(features, window)
            if avg > 0 or features.get(f"points_last_{window}", 0) > 0:
                total += weight * avg
                weight_sum += weight
        if weight_sum == 0:
            return 0.0
        return total / weight_sum

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        for pid, features in features_batch.items():
            value = self._predict_one(features)
            results[pid] = {
                "predicted_expected_points": round(value, 4),
                "confidence": self._confidence(features),
                "data_completeness": self._completeness(features),
                "method": "baseline_a_recent_form",
            }
        return results

    def _confidence(self, features: dict[str, float]) -> float:
        n = features.get("n_season_matches", 0)
        return min(1.0, n / 10.0)

    def _completeness(self, features: dict[str, float]) -> float:
        present = sum(
            1 for key in ("points_last_3", "points_last_5", "points_last_10")
            if features.get(key, 0) > 0 or key in features
        )
        return present / 3.0

    def evaluate(self, predictions: Any, actuals: Any) -> dict[str, float]:
        return _mean_absolute_error(predictions, actuals)

    def save(self, artifact_location: str) -> str:
        return artifact_location

    @classmethod
    def load(cls, artifact_path: str) -> PredictionModel:
        return cls()


class MinutesAdjustedBaselineModel(PredictionModel):
    """Baseline B: minutes-adjusted form.

    ``expected_points = minutes_adjusted_pp90 * expected_minutes_ratio``

    The model computes points-per-90 over the last 10 gameweeks and scales
    by the player's expected minutes share. If expected minutes are not in
    the feature vector, the last-10-gameweek average minutes / 90 is used.

    Documented caveat: points-per-90 is NOT always predictive. It is used
    strictly as a baseline and can overrate players who score in short
    cameos (e.g. a substitute who scores once in 20 minutes).
    """

    def __init__(self, seed: int = 42) -> None:
        self._seed = seed

    @property
    def model_name(self) -> str:
        return "baseline_b_minutes_adjusted"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def metadata(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_type": "baseline",
            "feature_version": "1.0.0",
            "hyperparameters": {},
            "random_seed": self._seed,
        }

    def fit(self, X: Any, y: Any, context: dict[str, Any] | None = None) -> PredictionModel:
        return self

    def predict(self, X: Any, context: dict[str, Any] | None = None) -> Any:
        if isinstance(X, dict):
            return self._predict_one(X)
        return np.array([self._predict_one(x) for x in X])

    def _predict_one(self, features: dict[str, float]) -> float:
        pp90 = features.get("points_per_90", 0.0)

        expected_minutes = features.get("expected_minutes", 0.0)
        if expected_minutes <= 0:
            minutes_last_10 = features.get("minutes_last_10", 0.0)
            n = min(int(features.get("n_season_matches", 10)), 10)
            avg_minutes = minutes_last_10 / max(n, 1)
            expected_minutes = avg_minutes

        minutes_ratio = expected_minutes / 90.0
        return pp90 * minutes_ratio

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        for pid, features in features_batch.items():
            value = self._predict_one(features)
            results[pid] = {
                "predicted_expected_points": round(value, 4),
                "confidence": self._confidence(features),
                "data_completeness": self._completeness(features),
                "method": "baseline_b_minutes_adjusted",
            }
        return results

    def _confidence(self, features: dict[str, float]) -> float:
        n = features.get("n_season_matches", 0)
        return min(1.0, n / 10.0)

    def _completeness(self, features: dict[str, float]) -> float:
        keys = ["points_per_90", "minutes_last_10"]
        present = sum(1 for k in keys if k in features and features[k] > 0)
        return present / len(keys)

    def evaluate(self, predictions: Any, actuals: Any) -> dict[str, float]:
        return _mean_absolute_error(predictions, actuals)

    def save(self, artifact_location: str) -> str:
        return artifact_location

    @classmethod
    def load(cls, artifact_path: str) -> PredictionModel:
        return cls()


class FixtureAdjustedBaselineModel(PredictionModel):
    """Baseline C: fixture-adjusted form.

    ``expected_points = form_score * fixture_multiplier * expected_minutes_ratio``

    Where:

    - ``form_score``: recent weighted points (Baseline A style).
    - ``fixture_multiplier``: derived from opponent strength and home/away.
      A stronger opponent and/or an away fixture reduces expected points.
    - ``expected_minutes_ratio``: expected minutes / 90.

    This model is interpretable: each multiplier can be inspected directly.
    """

    def __init__(self, seed: int = 42) -> None:
        self._seed = seed

    @property
    def model_name(self) -> str:
        return "baseline_c_fixture_adjusted"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def metadata(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_type": "baseline",
            "feature_version": "1.0.0",
            "hyperparameters": {},
            "random_seed": self._seed,
        }

    def fit(self, X: Any, y: Any, context: dict[str, Any] | None = None) -> PredictionModel:
        return self

    def predict(self, X: Any, context: dict[str, Any] | None = None) -> Any:
        if isinstance(X, dict):
            return self._predict_one(X)
        return np.array([self._predict_one(x) for x in X])

    def _predict_one(self, features: dict[str, float]) -> float:
        # 1. Form score (Baseline A weighted).
        weights = {3: 0.5, 5: 0.3, 10: 0.2}
        form_score = 0.0
        ws = 0.0
        for window, weight in weights.items():
            key = f"points_last_{window}"
            if key in features:
                n = min(int(features.get("n_season_matches", window)), window)
                avg = features[key] / max(n, 1)
                form_score += weight * avg
                ws += weight
        form_score = form_score / max(ws, 1e-9)

        # 2. Fixture multiplier from opponent strength.
        opponent_attack = features.get("opponent_attack_strength", 1.0)
        opponent_defence = features.get("opponent_defensive_strength", 1.0)
        # Higher opponent attack strength => harder fixture => lower points.
        attack_penalty = max(0.6, 1.0 - (opponent_attack - 1.0) * 0.3)
        # Higher opponent defensive strength => harder => lower points.
        defence_penalty = max(0.6, 1.0 - (opponent_defence - 1.0) * 0.3)
        fixture_multiplier = (attack_penalty + defence_penalty) / 2.0

        # 3. Home/away adjustment.
        is_home = features.get("is_home", 0.5)
        if is_home == 1.0:
            fixture_multiplier *= 1.05
        elif is_home == 0.0:
            fixture_multiplier *= 0.95

        # 4. Expected minutes ratio.
        expected_minutes = features.get("expected_minutes", 0.0)
        if expected_minutes <= 0:
            expected_minutes = features.get("minutes_last_10", 0.0) / max(
                min(int(features.get("n_season_matches", 10)), 10), 1
            )
        minutes_ratio = expected_minutes / 90.0
        minutes_ratio = max(0.0, min(1.2, minutes_ratio))

        return form_score * fixture_multiplier * minutes_ratio

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        for pid, features in features_batch.items():
            value = self._predict_one(features)
            results[pid] = {
                "predicted_expected_points": round(value, 4),
                "confidence": self._confidence(features),
                "data_completeness": self._completeness(features),
                "method": "baseline_c_fixture_adjusted",
            }
        return results

    def _confidence(self, features: dict[str, float]) -> float:
        n = features.get("n_season_matches", 0)
        return min(1.0, n / 10.0)

    def _completeness(self, features: dict[str, float]) -> float:
        keys = [
            "points_last_3",
            "opponent_attack_strength",
            "opponent_defensive_strength",
            "is_home",
        ]
        present = sum(1 for k in keys if k in features)
        return present / len(keys)

    def evaluate(self, predictions: Any, actuals: Any) -> dict[str, float]:
        return _mean_absolute_error(predictions, actuals)

    def save(self, artifact_location: str) -> str:
        return artifact_location

    @classmethod
    def load(cls, artifact_path: str) -> PredictionModel:
        return cls()


def _mean_absolute_error(predictions: Any, actuals: Any) -> dict[str, float]:
    """Compute MAE/RMSE between two aligned numeric sequences."""
    if isinstance(predictions, dict):
        pred = np.array([predictions[k] for k in sorted(predictions)])
    else:
        pred = np.asarray(predictions, dtype=float)
    if isinstance(actuals, dict):
        act = np.array([actuals[k] for k in sorted(actuals)])
    else:
        act = np.asarray(actuals, dtype=float)

    if len(pred) == 0 or len(pred) != len(act):
        return {"mae": float("nan"), "rmse": float("nan"), "n": 0}

    mae = float(np.mean(np.abs(pred - act)))
    rmse = float(np.sqrt(np.mean((pred - act) ** 2)))
    return {"mae": mae, "rmse": rmse, "n": len(pred)}


# Convenience aliases for the backtesting engine and reports.
BASELINE_MODELS = {
    "baseline_a": RecentFormBaselineModel,
    "baseline_b": MinutesAdjustedBaselineModel,
    "baseline_c": FixtureAdjustedBaselineModel,
}

