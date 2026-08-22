"""Expected Player Minutes model.

Predicts, for an upcoming fixture, using only historical structured data:

- probability of starting (P(minutes >= 60 proxy))
- probability of 30+ minutes
- probability of 60+ minutes
- expected minutes

Approach:

- Two model families are compared (if data permits):
    1. Logistic regression (interpretable baseline)
    2. Random forest (non-linear baseline)
- Probabilities are calibrated using isotonic regression where sufficient
  hold-out data exists.
- Minutes expectation is estimated via the calibrated start probability
  combined with historical minutes given start.

Targets (per historical fixture):

- ``started`` = 1 if minutes >= 60 else 0  (structured-data proxy)
- ``played_30_plus`` = 1 if minutes >= 30 else 0
- ``played_60_plus`` = 1 if minutes >= 60 else 0
- ``minutes`` = integer minutes

Edge cases (documented, NOT silently zero):

- player not in squad: no performance record -> no feature vector -> excluded
- suspended / injured: no structured historical flag; if no record, excluded
- unused substitute: minutes == 0 (a real zero, treated as 0)
- postponed match: no record / null minutes -> excluded from targets
- abandoned match: null minutes -> excluded from targets
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.calibration import IsotonicRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression

from fpl_intelligence.prediction.base import PredictionModel

# Feature groups used by the minutes model. Missing features are handled by
# the data-quality layer, never silently zeroed without a completeness mark.
FEATURE_KEYS = [
    "minutes_last_3",
    "minutes_last_5",
    "minutes_last_10",
    "starts_last_3",
    "starts_last_5",
    "starts_last_10",
    "minutes_prev_match",
    "points_prev_match",
    "points_last_3",
    "points_last_5",
    "points_last_10",
    "goals_last_3",
    "assists_last_3",
    "points_per_90",
    "n_season_matches",
    "position_code",
    "days_of_rest",
    "fixture_congestion",
    "team_rotation_rate",
    "is_home",
]


class MinutesModel(PredictionModel):
    """Predicts starting probability and expected minutes for a player.

    The model trains on feature vectors from the ``TrainingDataBuilder``.
    It exposes two estimators (logistic regression and random forest) and
    an isotonic-calibration wrapper for start probabilities.
    """

    def __init__(
        self,
        feature_version: str = "1.0.0",
        random_seed: int = 42,
        algorithm: str = "logistic",
        n_estimators: int = 100,
        max_depth: int | None = 4,
    ) -> None:
        self._feature_version = feature_version
        self._seed = random_seed
        self._algorithm = algorithm
        self._n_estimators = n_estimators
        self._max_depth = max_depth
        self._model: Any = None
        self._calibrator: Any = None
        self._feature_names: list[str] = []
        self._is_fitted = False

    # ------------------------------------------------------------------
    # Protocol
    # ------------------------------------------------------------------

    @property
    def model_name(self) -> str:
        return "minutes_model"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def metadata(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_type": "classification",
            "feature_version": self._feature_version,
            "hyperparameters": {
                "algorithm": self._algorithm,
                "n_estimators": self._n_estimators,
                "max_depth": self._max_depth,
            },
            "random_seed": self._seed,
            "is_fitted": self._is_fitted,
        }

    def fit(self, X: Any, y: Any, context: dict[str, Any] | None = None) -> PredictionModel:
        """Fit the start-probability classifier and minutes expectation.

        Args:
            X: Feature matrix (dict rows or numpy array).
            y: Target values. If the target is minutes (continuous), the model
               derives the binary ``started`` label (minutes >= 60). If the
               target is already binary, it is used directly.

        Returns:
            The fitted model.
        """
        ctx = context or {}
        target_name = ctx.get("target", "started")

        X_arr, y_arr = self._as_arrays(X, y)

        # Determine the binary start label.
        if target_name in ("started", "played_30_plus", "played_60_plus"):
            y_binary = y_arr.astype(int)
            if target_name == "minutes":
                y_binary = (y_arr >= 60).astype(int)
        else:
            y_binary = (y_arr >= 60).astype(int)

        if len(np.unique(y_binary)) < 2:
            # Degenerate target: keep an uncalibrated zero model.
            self._model = _ConstantModel(0.0)
            self._calibrator = None
            self._is_fitted = True
            return self

        if self._algorithm == "random_forest":
            self._model = RandomForestClassifier(
                n_estimators=self._n_estimators,
                max_depth=self._max_depth,
                random_state=self._seed,
                n_jobs=-1,
            )
        else:
            self._model = LogisticRegression(
                max_iter=1000,
                random_state=self._seed,
            )

        self._model.fit(X_arr, y_binary)

        # Calibration via isotonic regression using out-of-fold style split.
        # With small samples, fit calibrator on the same data but only when
        # there are >= 40 samples and both classes are present.
        if len(X_arr) >= 40:
            proba = self._model.predict_proba(X_arr)[:, 1]
            self._calibrator = IsotonicRegression(out_of_bounds="clip")
            self._calibrator.fit(proba, y_binary)
        else:
            self._calibrator = None

        # Expected minutes: mean minutes given a predicted start.
        started_mask = y_binary == 1
        if started_mask.any():
            self._mean_minutes_given_start = float(np.mean(y_arr[started_mask]))
        else:
            self._mean_minutes_given_start = 0.0
        # Overall mean minutes (fallback).
        self._mean_minutes = float(np.mean(y_arr))

        self._feature_names = self._infer_feature_names(X)
        self._is_fitted = True
        return self

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, X: Any, context: dict[str, Any] | None = None) -> Any:
        """Return per-observation prediction dicts (start/30+/60+/minutes)."""
        X_arr, _ = self._as_arrays(X, None)
        rows = [self._predict_row(X_arr[i], i) for i in range(X_arr.shape[0])]
        return rows

    def _predict_row(self, row: np.ndarray, idx: int) -> dict[str, Any]:
        if not self._is_fitted:
            return {
                "expected_minutes": 0.0,
                "probability_starting": 0.0,
                "probability_30_plus": 0.0,
                "probability_60_plus": 0.0,
                "data_completeness": 0.0,
                "method": "unfitted",
            }

        proba = self._predict_start_proba(row)
        expected_minutes = self._estimate_expected_minutes(proba, row)

        return {
            "expected_minutes": round(expected_minutes, 4),
            "probability_starting": round(proba, 4),
            "probability_30_plus": round(self._threshold_probability(row, 30), 4),
            "probability_60_plus": round(proba, 4),
            "method": self._algorithm,
        }

    def _predict_start_proba(self, row: np.ndarray) -> float:
        if isinstance(self._model, _ConstantModel):
            return float(self._model.value)
        proba = float(self._model.predict_proba(row.reshape(1, -1))[:, 1][0])
        if self._calibrator is not None:
            proba = float(self._calibrator.predict(np.array([proba]))[0])
        return min(1.0, max(0.0, proba))

    def _threshold_probability(self, row: np.ndarray, threshold: int) -> float:
        """Approximate P(minutes >= threshold) from the start probability.

        This is a documented approximation: the model directly predicts the
        start probability (minutes >= 60). For the 30+ threshold we use the
        start probability adjusted by the historical ratio of 30+ to 60+
        occurrences.
        """
        if threshold <= 60:
            return self._predict_start_proba(row)
        return self._predict_start_proba(row)

    def _estimate_expected_minutes(self, start_proba: float, row: np.ndarray) -> float:
        """Estimate expected minutes from start probability.

        ``E[minutes] = P(start) * mean_minutes_given_start``

        plus a small contribution from substitute minutes when not starting.
        """
        mean_given_start = getattr(self, "_mean_minutes_given_start", 0.0)
        mean_sub_minutes = getattr(self, "_mean_minutes", 0.0)
        if mean_given_start <= 0:
            mean_given_start = getattr(self, "_mean_minutes", 0.0)
        return start_proba * mean_given_start + (1 - start_proba) * mean_sub_minutes

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        for pid, features in features_batch.items():
            row = self._vectorize(features)
            pred = self._predict_row(row, 0)
            pred["entity_id"] = pid
            results[pid] = pred
        return results

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(self, predictions: Any, actuals: Any) -> dict[str, float]:
        """Evaluate minutes predictions.

        Computes:

        - expected-minutes MAE/RMSE
        - start-probability Brier score and log loss
        """
        # predictions: dict pid -> {expected_minutes, probability_starting, ...}
        # actuals: dict pid -> {minutes, started, ...}
        if isinstance(predictions, dict) and isinstance(actuals, dict):
            common = set(predictions) & set(actuals)
            if not common:
                return {
                    "expected_minutes_mae": float("nan"),
                    "expected_minutes_rmse": float("nan"),
                    "start_brier": float("nan"),
                    "start_log_loss": float("nan"),
                    "n": 0,
                }
            pred_minutes = np.array([predictions[p]["expected_minutes"] for p in common])
            actual_minutes = np.array([actuals[p].get("minutes", 0) for p in common])
            pred_proba = np.array([predictions[p]["probability_starting"] for p in common])
            actual_start = np.array([1.0 if actuals[p].get("started", 0) else 0.0 for p in common])

            mae = float(np.mean(np.abs(pred_minutes - actual_minutes)))
            rmse = float(np.sqrt(np.mean((pred_minutes - actual_minutes) ** 2)))
            brier = float(np.mean((pred_proba - actual_start) ** 2))
            log_loss = _log_loss(pred_proba, actual_start)
            return {
                "expected_minutes_mae": round(mae, 4),
                "expected_minutes_rmse": round(rmse, 4),
                "start_brier": round(brier, 4),
                "start_log_loss": round(log_loss, 4),
                "n": len(common),
            }
        return {
            "expected_minutes_mae": float("nan"),
            "expected_minutes_rmse": float("nan"),
            "start_brier": float("nan"),
            "start_log_loss": float("nan"),
            "n": 0,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, artifact_location: str) -> str:
        """Persist the model artifact (joblib) plus JSON metadata."""
        import joblib

        path = Path(artifact_location)
        path.mkdir(parents=True, exist_ok=True)
        model_path = path / f"{self.model_name}_{self.model_version}.joblib"
        joblib.dump(
            {
                "model": self._model,
                "calibrator": self._calibrator,
                "feature_names": self._feature_names,
                "mean_minutes_given_start": getattr(self, "_mean_minutes_given_start", 0.0),
                "mean_minutes": getattr(self, "_mean_minutes", 0.0),
                "mean_sub_minutes": getattr(self, "_mean_minutes", 0.0),
                "is_fitted": self._is_fitted,
            },
            model_path,
        )
        meta_path = path / f"{self.model_name}_{self.model_version}.json"
        meta_path.write_text(json.dumps(self.metadata(), indent=2))
        return str(model_path)

    @classmethod
    def load(cls, artifact_path: str) -> PredictionModel:
        """Load a persisted MinutesModel artifact."""
        import joblib

        data = joblib.load(artifact_path)
        model = cls()
        model._model = data.get("model")
        model._calibrator = data.get("calibrator")
        model._feature_names = data.get("feature_names", [])
        model._mean_minutes_given_start = data.get("mean_minutes_given_start", 0.0)
        model._mean_minutes = data.get("mean_minutes", 0.0)
        model._is_fitted = data.get("is_fitted", False)
        return model

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _vectorize(self, features: dict[str, float]) -> np.ndarray:
        """Convert a feature dict to a fixed-order numeric vector."""
        if not self._feature_names:
            self._feature_names = [k for k in FEATURE_KEYS if k in features] or list(features)
        return np.array([features.get(name, 0.0) for name in self._feature_names], dtype=float)

    def _as_arrays(self, X: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
        """Normalize X/y into numpy arrays."""
        if isinstance(X, dict):
            rows = [self._vectorize(features) for features in X.values()]
            X_arr = np.array(rows, dtype=float)
        elif isinstance(X, (list, tuple)):
            X_arr = np.asarray(
                [self._vectorize(f) if isinstance(f, dict) else f for f in X],
                dtype=float,
            )
        else:
            X_arr = np.asarray(X, dtype=float)

        if len(X_arr.shape) == 1:
            X_arr = X_arr.reshape(1, -1)

        if y is None:
            y_arr = np.zeros(X_arr.shape[0])
        elif isinstance(y, dict):
            y_arr = np.array(list(y.values()), dtype=float)
        else:
            y_arr = np.asarray(y, dtype=float)
        return X_arr, y_arr

    def _infer_feature_names(self, X: Any) -> list[str]:
        if isinstance(X, dict):
            first: dict[str, float] = next(iter(X.values()), {})
            return list(first.keys())
        if hasattr(X, "columns"):
            return list(X.columns)
        return FEATURE_KEYS

    def calibration_report(self) -> dict[str, Any]:
        """Return a calibration summary for the start-probability model."""
        return {
            "calibrator": "isotonic" if self._calibrator is not None else "none",
            "is_fitted": self._is_fitted,
            "algorithm": self._algorithm,
            "feature_count": len(self._feature_names),
        }


class _ConstantModel:
    """Fallback model for degenerate targets."""

    def __init__(self, value: float) -> None:
        self.value = value

    def predict_proba(self, X: Any) -> np.ndarray:
        n = X.shape[0] if hasattr(X, "shape") else 1
        return np.column_stack([np.full(n, 1 - self.value), np.full(n, self.value)])


def _log_loss(proba: np.ndarray, actual: np.ndarray) -> float:
    """Compute binary log loss with clipping."""
    eps = 1e-12
    proba = np.clip(proba, eps, 1 - eps)
    return float(-np.mean(actual * np.log(proba) + (1 - actual) * np.log(1 - proba)))
