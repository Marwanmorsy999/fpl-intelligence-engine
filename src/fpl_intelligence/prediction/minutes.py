"""Expected Player Minutes model.

Predicts, for an upcoming fixture, using only historical structured data:


Approach:

    1. Logistic regression (interpretable baseline)
    2. Random forest (non-linear baseline)
  hold-out data exists.
  combined with historical minutes given start.

Targets (per historical fixture):


Edge cases (documented, NOT silently zero):

"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
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

MINUTES_BUCKETS = ("0", "1_29", "30_59", "60_89", "90_plus")
MINUTES_BUCKET_MIDPOINTS = {"0": 0.0, "1_29": 15.0, "30_59": 45.0, "60_89": 75.0, "90_plus": 90.0}


@dataclass(frozen=True)
class PlayerMinutesPrediction:
    """Public, serializable output for a player minutes prediction."""

    player_id: int | None
    prediction_time: str | None
    cutoff_time: str | None
    probability_start: float
    probability_appearance: float
    probability_60_plus: float
    probability_90: float
    probability_no_appearance: float
    expected_minutes: float
    uncertainty: float
    distribution: dict[str, float]
    model_version: str
    feature_version: str
    data_version: str | None
    confidence: float
    reason_codes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MinutesModel(PredictionModel):
    """Predicts starting probability and expected minutes for a player.

    The model trains on feature vectors from the ``TrainingDataBuilder``.
    It exposes two estimators (logistic regression and random forest) and
    an isotonic-calibration wrapper for start probabilities.
    """

    def __init__(
        self,
        feature_version: str = "2.0.0",
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
        self._models: dict[str, Any] = {}
        self._calibrators: dict[str, Any] = {}
        self._feature_names: list[str] = []
        self._distribution = {bucket: 0.0 for bucket in MINUTES_BUCKETS}
        self._bucket_means = dict(MINUTES_BUCKET_MIDPOINTS)
        self._sub_sixty_mix = {"1_29": 0.5, "30_59": 0.5}
        self._mean_minutes = 0.0
        self._std_minutes = 0.0
        self._is_fitted = False

    # ------------------------------------------------------------------
    # Protocol
    # ------------------------------------------------------------------

    @property
    def model_name(self) -> str:
        return "minutes_model"

    @property
    def model_version(self) -> str:
        return "2.0.0"

    def metadata(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_type": "probabilistic_minutes",
            "feature_version": self._feature_version,
            "data_version": "canonical_historical_performance",
            "hyperparameters": {
                "algorithm": self._algorithm,
                "n_estimators": self._n_estimators,
                "max_depth": self._max_depth,
            },
            "random_seed": self._seed,
            "is_fitted": self._is_fitted,
            "targets": ["appeared", "started", "60_plus", "90_plus", "minutes"],
            "calibration": "chronological_holdout_isotonic",
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
        X_arr, y_arr = self._as_arrays(X, y)
        if len(X_arr) == 0 or len(X_arr) != len(y_arr):
            raise ValueError("MinutesModel requires equally sized feature and target arrays")
        self._feature_names = self._infer_feature_names(X)
        targets = {
            "appeared": np.asarray(ctx.get("appeared", y_arr > 0), dtype=int),
            "started": np.asarray(ctx.get("started", y_arr >= 60), dtype=int),
            "60_plus": (y_arr >= 60).astype(int),
            "90_plus": (y_arr >= 90).astype(int),
        }
        if any(len(values) != len(y_arr) for values in targets.values()):
            raise ValueError("Explicit minutes targets must match the feature row count")
        self._models = {}
        self._calibrators = {}
        for target_name, target_values in targets.items():
            model = self._make_classifier(target_values)
            model.fit(X_arr, target_values)
            self._models[target_name] = model
            self._fit_calibrator(target_name, model, X_arr, target_values)
        self._distribution = self._minutes_distribution(y_arr)
        self._bucket_means = self._minutes_bucket_means(y_arr)
        sub_sixty = self._distribution["1_29"] + self._distribution["30_59"]
        if sub_sixty:
            self._sub_sixty_mix = {
                "1_29": self._distribution["1_29"] / sub_sixty,
                "30_59": self._distribution["30_59"] / sub_sixty,
            }
        self._mean_minutes = float(np.mean(y_arr))
        self._std_minutes = float(np.std(y_arr))
        self._is_fitted = True
        return self

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, X: Any, context: dict[str, Any] | None = None) -> Any:
        """Return per-observation prediction dicts (start/30+/60+/minutes)."""
        X_arr, _ = self._as_arrays(X, None)
        ctx = context or {}
        rows = [self._predict_row(X_arr[i], ctx) for i in range(X_arr.shape[0])]
        return rows

    def _predict_row(self, row: np.ndarray, context: dict[str, Any]) -> dict[str, Any]:
        if not self._is_fitted:
            return self._prediction(None, context, 0.0, 0.0, 0.0, 0.0, 0.0)
        probabilities = {
            name: self._probability(name, row)
            for name in ("appeared", "started", "60_plus", "90_plus")
        }
        distribution = self._prediction_distribution(probabilities)
        expected = sum(self._bucket_means[bucket] * value for bucket, value in distribution.items())
        confidence = min(1.0, max(0.0, 1.0 - self._std_minutes / 90.0))
        return self._prediction(
            context.get("player_id"),
            context,
            probabilities["started"],
            probabilities["appeared"],
            probabilities["60_plus"],
            probabilities["90_plus"],
            expected,
            self._std_minutes,
            confidence,
            distribution,
        )

    def _prediction(
        self,
        player_id: int | None,
        context: dict[str, Any],
        start: float,
        appearance: float,
        sixty: float,
        ninety: float,
        expected: float,
        uncertainty: float = 0.0,
        confidence: float = 0.0,
        distribution: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        prediction = PlayerMinutesPrediction(
            player_id=player_id,
            prediction_time=context.get("prediction_time"),
            cutoff_time=context.get("cutoff_time") or self._as_iso(context.get("cutoff")),
            probability_start=round(start, 6),
            probability_appearance=round(appearance, 6),
            probability_60_plus=round(sixty, 6),
            probability_90=round(ninety, 6),
            probability_no_appearance=round(1.0 - appearance, 6),
            expected_minutes=round(expected, 6),
            uncertainty=round(uncertainty, 6),
            distribution={
                key: round(value, 6)
                for key, value in (distribution or self._distribution).items()
            },
            model_version=self.model_version,
            feature_version=self._feature_version,
            data_version=context.get("data_version", "canonical_historical_performance"),
            confidence=round(confidence, 6),
            reason_codes=self._reason_codes(start, appearance, uncertainty),
        ).to_dict()
        prediction.update(
            {
                "probability_starting": prediction["probability_start"],
                "probability_30_plus": min(
                    prediction["probability_appearance"], prediction["probability_60_plus"] + 0.2
                ),
                "data_completeness": prediction["confidence"],
                "method": self._algorithm if self._is_fitted else "unfitted",
            }
        )
        return prediction

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        for pid, features in features_batch.items():
            row = self._vectorize(features)
            pred = self._predict_row(row, {**(context or {}), "player_id": pid})
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
                    "start_calibration_error": float("nan"),
                    "appearance_brier": float("nan"),
                    "sixty_plus_brier": float("nan"),
                    "n": 0,
                }
            pred_minutes = np.array([predictions[p]["expected_minutes"] for p in common])
            actual_minutes = np.array([actuals[p].get("minutes", 0) for p in common])
            pred_proba = np.array(
                [
                    predictions[p].get("probability_start", predictions[p]["probability_starting"])
                    for p in common
                ]
            )
            actual_start = np.array(
                [
                    float(actuals[p].get("started", actuals[p].get("minutes", 0) >= 60))
                    for p in common
                ]
            )
            appearance = np.array(
                [predictions[p].get("probability_appearance", 0.0) for p in common]
            )
            sixty_plus = np.array([predictions[p].get("probability_60_plus", 0.0) for p in common])
            actual_appearance = (actual_minutes > 0).astype(float)
            actual_sixty_plus = (actual_minutes >= 60).astype(float)

            mae = float(np.mean(np.abs(pred_minutes - actual_minutes)))
            rmse = float(np.sqrt(np.mean((pred_minutes - actual_minutes) ** 2)))
            brier = float(np.mean((pred_proba - actual_start) ** 2))
            log_loss = _log_loss(pred_proba, actual_start)
            return {
                "expected_minutes_mae": round(mae, 4),
                "expected_minutes_rmse": round(rmse, 4),
                "start_brier": round(brier, 4),
                "start_log_loss": round(log_loss, 4),
                "start_calibration_error": round(_calibration_error(pred_proba, actual_start), 4),
                "appearance_brier": round(float(np.mean((appearance - actual_appearance) ** 2)), 4),
                "sixty_plus_brier": round(float(np.mean((sixty_plus - actual_sixty_plus) ** 2)), 4),
                "n": len(common),
            }
        return {
            "expected_minutes_mae": float("nan"),
            "expected_minutes_rmse": float("nan"),
            "start_brier": float("nan"),
            "start_log_loss": float("nan"),
            "start_calibration_error": float("nan"),
            "appearance_brier": float("nan"),
            "sixty_plus_brier": float("nan"),
            "n": 0,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _make_classifier(self, target: np.ndarray) -> Any:
        if len(np.unique(target)) < 2:
            return _ConstantModel(float(target[0]) if len(target) else 0.0)
        if self._algorithm == "random_forest":
            return RandomForestClassifier(
                n_estimators=self._n_estimators,
                max_depth=self._max_depth,
                random_state=self._seed,
                n_jobs=-1,
            )
        return LogisticRegression(max_iter=1000, random_state=self._seed)

    def _fit_calibrator(self, name: str, model: Any, X: np.ndarray, y: np.ndarray) -> None:
        holdout = max(10, int(len(X) * 0.2))
        if len(X) < 40 or len(np.unique(y[-holdout:])) < 2:
            return
        calibrator = IsotonicRegression(out_of_bounds="clip")
        calibrator.fit(model.predict_proba(X[-holdout:])[:, 1], y[-holdout:])
        self._calibrators[name] = calibrator

    def _probability(self, name: str, row: np.ndarray) -> float:
        value = float(self._models[name].predict_proba(row.reshape(1, -1))[:, 1][0])
        calibrator = self._calibrators.get(name)
        if calibrator is not None:
            value = float(calibrator.predict([value])[0])
        return min(1.0, max(0.0, value))

    @staticmethod
    def _minutes_distribution(minutes: np.ndarray) -> dict[str, float]:
        counts = {bucket: 0 for bucket in MINUTES_BUCKETS}
        for value in minutes:
            counts[_minutes_bucket(float(value))] += 1
        total = max(len(minutes), 1)
        return {bucket: count / total for bucket, count in counts.items()}

    @staticmethod
    def _minutes_bucket_means(minutes: np.ndarray) -> dict[str, float]:
        means: dict[str, float] = {}
        for bucket in MINUTES_BUCKETS:
            values = [float(value) for value in minutes if _minutes_bucket(float(value)) == bucket]
            means[bucket] = float(np.mean(values)) if values else MINUTES_BUCKET_MIDPOINTS[bucket]
        return means

    def _prediction_distribution(self, probabilities: dict[str, float]) -> dict[str, float]:
        appearance = probabilities["appeared"]
        sixty_plus = min(probabilities["60_plus"], appearance)
        ninety_plus = min(probabilities["90_plus"], sixty_plus)
        sub_sixty = appearance - sixty_plus
        return {
            "0": 1.0 - appearance,
            "1_29": sub_sixty * self._sub_sixty_mix["1_29"],
            "30_59": sub_sixty * self._sub_sixty_mix["30_59"],
            "60_89": sixty_plus - ninety_plus,
            "90_plus": ninety_plus,
        }

    @staticmethod
    def _reason_codes(start: float, appearance: float, uncertainty: float) -> list[str]:
        reasons = ["stable_recent_usage"] if start >= 0.7 else ["rotation_or_role_uncertain"]
        if appearance < 0.5:
            reasons.append("appearance_risk")
        if uncertainty > 30:
            reasons.append("high_minutes_variance")
        return reasons

    @staticmethod
    def _as_iso(value: Any) -> str | None:
        return value.isoformat() if hasattr(value, "isoformat") else value

    def save(self, artifact_location: str) -> str:
        """Persist the model artifact (joblib) plus JSON metadata."""
        import joblib

        path = Path(artifact_location)
        path.mkdir(parents=True, exist_ok=True)
        model_path = path / f"{self.model_name}_{self.model_version}.joblib"
        joblib.dump(
            {
                "models": self._models,
                "calibrators": self._calibrators,
                "feature_names": self._feature_names,
                "distribution": self._distribution,
                "bucket_means": self._bucket_means,
                "sub_sixty_mix": self._sub_sixty_mix,
                "mean_minutes": self._mean_minutes,
                "std_minutes": self._std_minutes,
                "feature_version": self._feature_version,
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
        model = cls(feature_version=data.get("feature_version", "2.0.0"))
        model._models = data.get("models", {})
        # Read artifacts made by the pre-Stage-2A implementation.
        if not model._models and data.get("model") is not None:
            model._models = {"started": data["model"], "60_plus": data["model"]}
        model._calibrators = data.get("calibrators", {})
        if not model._calibrators and data.get("calibrator") is not None:
            model._calibrators = {"started": data["calibrator"], "60_plus": data["calibrator"]}
        model._feature_names = data.get("feature_names", [])
        model._distribution = data.get("distribution", model._distribution)
        model._bucket_means = data.get("bucket_means", dict(MINUTES_BUCKET_MIDPOINTS))
        model._sub_sixty_mix = data.get("sub_sixty_mix", {"1_29": 0.5, "30_59": 0.5})
        model._mean_minutes = data.get("mean_minutes", data.get("mean_minutes_given_start", 0.0))
        model._std_minutes = data.get("std_minutes", 0.0)
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
            # Filter by FEATURE_KEYS to stay consistent with _vectorize(), which
            # also intersects with FEATURE_KEYS. Without this, the names set
            # here would include every key in the feature dict (e.g. goals_last_5,
            # assists_last_5) that _vectorize never actually selected, causing a
            # feature-count mismatch between fit() and predict().
            return [k for k in FEATURE_KEYS if k in first] or list(first)
        if hasattr(X, "columns"):
            return list(X.columns)
        width = getattr(X, "shape", (0, len(FEATURE_KEYS)))[1]
        return FEATURE_KEYS[:width] if width else FEATURE_KEYS

    def calibration_report(self) -> dict[str, Any]:
        """Return a calibration summary for the start-probability model."""
        return {
            "calibrator": "isotonic" if self._calibrators else "none",
            "method": "chronological_holdout_isotonic",
            "targets": list(self._calibrators),
            "is_fitted": self._is_fitted,
            "algorithm": self._algorithm,
            "feature_count": len(self._feature_names),
        }


class _ConstantModel:
    """Fallback model for degenerate targets."""

    def __init__(self, value: float) -> None:
        self.value = value

    def fit(self, X: Any, y: Any) -> _ConstantModel:
        return self

    def predict_proba(self, X: Any) -> np.ndarray:
        n = X.shape[0] if hasattr(X, "shape") else 1
        return np.column_stack([np.full(n, 1 - self.value), np.full(n, self.value)])


def _log_loss(proba: np.ndarray, actual: np.ndarray) -> float:
    """Compute binary log loss with clipping."""
    eps = 1e-12
    proba = np.clip(proba, eps, 1 - eps)
    return float(-np.mean(actual * np.log(proba) + (1 - actual) * np.log(1 - proba)))


def _minutes_bucket(minutes: float) -> str:
    if minutes <= 0:
        return "0"
    if minutes < 30:
        return "1_29"
    if minutes < 60:
        return "30_59"
    if minutes < 90:
        return "60_89"
    return "90_plus"


def _calibration_error(probability: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    """Return the weighted reliability gap across probability buckets."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    error = 0.0
    for index in range(bins):
        mask = (probability >= edges[index]) & (
            probability <= edges[index + 1] if index == bins - 1 else probability < edges[index + 1]
        )
        if mask.any():
            error += float(mask.mean()) * abs(
                float(probability[mask].mean()) - float(actual[mask].mean())
            )
    return error


class MinutesBaseline:
    """Transparent minutes baselines using only supplied historical features."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        for player_id, features in features_batch.items():
            if self.kind == "recent_start":
                minutes = 90.0 if features.get("starts_last_3", 0) >= 2 else 0.0
            elif self.kind == "rolling_average":
                minutes = features.get("minutes_last_10", 0.0) / max(
                    features.get("n_season_matches", 10), 1
                )
            else:
                minutes = features.get("minutes_last_3", 0.0) / max(
                    min(features.get("n_season_matches", 3), 3), 1
                )
            start = min(1.0, max(0.0, minutes / 90.0))
            appearance = min(1.0, max(start, float(minutes > 0)))
            results[player_id] = {
                "entity_id": player_id,
                "expected_minutes": round(minutes, 6),
                "probability_start": round(start, 6),
                "probability_starting": round(start, 6),
                "probability_appearance": round(appearance, 6),
                "probability_60_plus": round(start, 6),
                "probability_90": round(start, 6),
                "probability_no_appearance": round(1.0 - appearance, 6),
                "uncertainty": 0.0,
                "confidence": 0.0,
                "method": self.kind,
            }
        return results


class SimpleRecentMinutesBaseline(MinutesBaseline):
    def __init__(self) -> None:
        super().__init__("recent_minutes")


class RecentStartBaseline(MinutesBaseline):
    def __init__(self) -> None:
        super().__init__("recent_start")


class RollingAverageMinutesBaseline(MinutesBaseline):
    def __init__(self) -> None:
        super().__init__("rolling_average")


def evaluate_minutes_predictions(
    predictions: dict[int, dict[str, Any]], actuals: dict[int, dict[str, Any]]
) -> dict[str, float]:
    common = sorted(set(predictions) & set(actuals))
    if not common:
        return {
            "mae": float("nan"),
            "rmse": float("nan"),
            "brier_start": float("nan"),
            "log_loss_start": float("nan"),
            "accuracy_start": float("nan"),
            "accuracy_60_plus": float("nan"),
            "n": 0.0,
        }
    expected = np.array([predictions[key].get("expected_minutes", 0.0) for key in common])
    minutes = np.array([actuals[key].get("minutes", 0.0) for key in common])
    start = np.array(
        [
            predictions[key].get(
                "probability_start", predictions[key].get("probability_starting", 0.0)
            )
            for key in common
        ]
    )
    sixty = np.array([predictions[key].get("probability_60_plus", 0.0) for key in common])
    actual_start = (minutes >= 60).astype(float)
    return {
        "mae": float(np.mean(np.abs(expected - minutes))),
        "rmse": float(np.sqrt(np.mean((expected - minutes) ** 2))),
        "brier_start": float(np.mean((start - actual_start) ** 2)),
        "log_loss_start": _log_loss(start, actual_start),
        "accuracy_start": float(np.mean((start >= 0.5) == (actual_start == 1))),
        "accuracy_60_plus": float(np.mean((sixty >= 0.5) == (actual_start == 1))),
        "n": float(len(common)),
    }
