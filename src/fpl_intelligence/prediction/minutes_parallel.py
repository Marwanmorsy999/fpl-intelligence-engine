"""Parallel fit wrapper for the MinutesModel.

The MinutesModel trains four independent binary classifiers. This module
runs those independent fits concurrently while preserving exactly the same
estimator classes, targets, random seed, calibration rule, and prediction
assembly. It changes scheduling only; model/statistical semantics are kept
unchanged.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from typing import Any

import numpy as np
from sklearn.calibration import IsotonicRegression

from fpl_intelligence.prediction.base import PredictionModel
from fpl_intelligence.prediction.minutes import MinutesModel

try:
    from threadpoolctl import threadpool_limits
except ImportError:  # pragma: no cover - sklearn normally supplies this dependency
    threadpool_limits = None


class ParallelMinutesModel(MinutesModel):
    """MinutesModel with concurrent independent target fits."""

    def fit(self, X: Any, y: Any, context: dict[str, Any] | None = None) -> PredictionModel:
        """Fit the same MinutesModel targets concurrently."""
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

        target_items = list(targets.items())
        workers = max(1, min(len(target_items), int(os.getenv("MINUTES_FIT_WORKERS", "4"))))

        def fit_target(item: tuple[str, np.ndarray]) -> tuple[str, Any, Any]:
            target_name, target_values = item
            limits = threadpool_limits(limits=1) if threadpool_limits else nullcontext()
            with limits:
                model = self._make_classifier(target_values)
                model.fit(X_arr, target_values)
                calibrator = None
                holdout = max(10, int(len(X_arr) * 0.2))
                if len(X_arr) >= 40 and len(np.unique(target_values[-holdout:])) >= 2:
                    calibrator = IsotonicRegression(out_of_bounds="clip")
                    calibrator.fit(
                        model.predict_proba(X_arr[-holdout:])[:, 1],
                        target_values[-holdout:],
                    )
                return target_name, model, calibrator

        with ThreadPoolExecutor(max_workers=workers) as executor:
            fitted = list(executor.map(fit_target, target_items))

        self._models = {name: model for name, model, _ in fitted}
        self._calibrators = {
            name: calibrator for name, _, calibrator in fitted if calibrator is not None
        }
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
