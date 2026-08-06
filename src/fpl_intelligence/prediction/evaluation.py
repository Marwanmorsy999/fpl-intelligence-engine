"""Model evaluation utilities for Phase 4 prediction models.

Provides:

- Calibration evaluation (reliability curves, ECE, MCE)
- Model comparison (MAE, RMSE, Brier, LogLoss for each model)
- Context breakdown (by season, Gameweek, position, price, home/away)
- Feature importance (for tree-based models)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class CalibrationReport:
    """Calibration evaluation for a probabilistic model.

    Attributes:
        bins: List of (bin_center, observed_fraction, count) tuples.
            Each bin represents the actual outcome rate for predictions
            falling in that probability range.
        expected_calibration_error: ECE (mean absolute calibration error).
        max_calibration_error: MCE (max absolute calibration error).
        brier_score: Brier score (mean squared probability error).
        log_loss: Binary log loss.
        n_samples: Number of samples evaluated.
    """

    bins: list[dict[str, float]] = field(default_factory=list)
    expected_calibration_error: float = 0.0
    max_calibration_error: float = 0.0
    brier_score: float = 0.0
    log_loss: float = 0.0
    n_samples: int = 0


@dataclass
class ModelComparisonReport:
    """Comparison report across multiple models.

    Attributes:
        model_names: List of model names compared.
        metrics: Dict mapping model_name -> {metric_name: value}.
        best_per_model: Dict mapping metric -> model_name (best performer).
        n_models: Number of models compared.
    """

    model_names: list[str] = field(default_factory=list)
    metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    best_per_model: dict[str, str] = field(default_factory=dict)
    n_models: int = 0


@dataclass
class ContextBreakdown:
    """Performance breakdown by context.

    Attributes:
        dimension: The breakdown dimension (season, gameweek, position, etc.).
        groups: Dict mapping group_label -> metrics dict.
    """

    dimension: str = ""
    groups: dict[str, dict[str, float]] = field(default_factory=dict)


# ------------------------------------------------------------------
# Calibration
# ------------------------------------------------------------------


def calibration_report(
    probabilities: np.ndarray,
    outcomes: np.ndarray,
    n_bins: int = 10,
) -> CalibrationReport:
    """Compute calibration metrics for binary probabilistic predictions.

    Args:
        probabilities: Predicted probabilities (between 0 and 1).
        outcomes: Binary actual outcomes (0 or 1).
        n_bins: Number of equal-width bins.

    Returns:
        A ``CalibrationReport``.
    """
    if len(probabilities) == 0:
        return CalibrationReport(
            n_samples=0,
            expected_calibration_error=float("nan"),
            max_calibration_error=float("nan"),
        )

    bins: list[dict[str, float]] = []
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)

    ece = 0.0
    mce = 0.0
    n = len(probabilities)

    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        mask = (probabilities >= lo) & (probabilities < hi)
        count = int(mask.sum())
        if count == 0:
            continue
        bin_center = (lo + hi) / 2.0
        observed = float(outcomes[mask].mean())
        bin_acc = abs(observed - bin_center)
        ece += bin_acc * (count / n)
        mce = max(mce, bin_acc)
        bins.append(
            {
                "bin_center": round(bin_center, 4),
                "observed_fraction": round(observed, 4),
                "count": count,
                "calibration_error": round(bin_acc, 4),
            }
        )

    brier = float(np.mean((probabilities - outcomes) ** 2))
    log_loss_val = _log_loss(probabilities, outcomes)

    return CalibrationReport(
        bins=bins,
        expected_calibration_error=round(ece, 4),
        max_calibration_error=round(mce, 4),
        brier_score=round(brier, 4),
        log_loss=round(log_loss_val, 4),
        n_samples=n,
    )


# ------------------------------------------------------------------
# Model comparison
# ------------------------------------------------------------------


def compare_models(
    predictions: dict[str, np.ndarray],
    actuals: np.ndarray,
    evaluate_classification: bool = False,
) -> ModelComparisonReport:
    """Compare multiple models over the same actual outcomes.

    Args:
        predictions: Dict mapping model_name -> predicted values.
        actuals: Actual outcome array (same ordering as predictions).
        evaluate_classification: If True, also compute Brier and log-loss.

    Returns:
        A ``ModelComparisonReport``.
    """
    model_names = list(predictions.keys())
    metrics: dict[str, dict[str, float]] = {}
    best_per_metric: dict[str, tuple[str, float]] = {}

    for name in model_names:
        pred = np.asarray(predictions[name], dtype=float)
        if len(pred) != len(actuals) or len(pred) == 0:
            metrics[name] = {"mae": float("nan"), "rmse": float("nan")}
            continue

        mae = float(np.mean(np.abs(pred - actuals)))
        rmse = float(np.sqrt(np.mean((pred - actuals) ** 2)))
        model_metrics: dict[str, float] = {
            "mae": round(mae, 4),
            "rmse": round(rmse, 4),
        }

        if evaluate_classification:
            brier = float(np.mean((pred - actuals) ** 2))
            log_loss_val = _log_loss(pred, actuals)
            model_metrics["brier"] = round(brier, 4)
            model_metrics["log_loss"] = round(log_loss_val, 4)

        metrics[name] = model_metrics

        # Track best per metric.
        for metric_key, value in model_metrics.items():
            if np.isnan(value):
                continue
            if metric_key not in best_per_metric:
                best_per_metric[metric_key] = (name, value)
            else:
                # Lower is better for MAE, RMSE, Brier, LogLoss.
                _, best_val = best_per_metric[metric_key]
                if value < best_val:
                    best_per_metric[metric_key] = (name, value)
                elif value == best_val and name not in [
                    n for n, _ in [best_per_metric[metric_key]]
                ]:
                    pass  # Tie: keep original.

    best = {k: v[0] for k, v in best_per_metric.items()}

    return ModelComparisonReport(
        model_names=model_names,
        metrics=metrics,
        best_per_model=best,
        n_models=len(model_names),
    )


# ------------------------------------------------------------------
# Context breakdown
# ------------------------------------------------------------------


def breakdown_by_context(
    predictions: np.ndarray,
    actuals: np.ndarray,
    context_labels: list[str],
    dimension: str = "custom",
) -> ContextBreakdown:
    """Compute metrics broken down by context labels.

    Args:
        predictions: Predicted values.
        actuals: Actual values.
        context_labels: Per-observation context labels (same length).
        dimension: Name of the breakdown dimension.

    Returns:
        A ``ContextBreakdown``.
    """
    groups: dict[str, dict[str, float]] = {}
    unique_labels = sorted(set(context_labels))

    for label in unique_labels:
        mask = [lbl == label for lbl in context_labels]
        pred_sub = np.array([p for p, m in zip(predictions, mask, strict=True) if m], dtype=float)
        actual_sub = np.array([a for a, m in zip(actuals, mask, strict=True) if m], dtype=float)
        if len(pred_sub) == 0:
            continue
        mae = float(np.mean(np.abs(pred_sub - actual_sub)))
        rmse = float(np.sqrt(np.mean((pred_sub - actual_sub) ** 2)))
        groups[label] = {
            "mae": round(mae, 4),
            "rmse": round(rmse, 4),
            "n": len(pred_sub),
        }

    return ContextBreakdown(dimension=dimension, groups=groups)


# ------------------------------------------------------------------
# Feature importance (tree-based)
# ------------------------------------------------------------------


def extract_feature_importance(
    model: Any, feature_names: list[str] | None = None
) -> dict[str, float]:
    """Extract feature importance from a fitted tree-based model.

    Args:
        model: A fitted sklearn model with ``feature_importances_`` attribute.
        feature_names: Optional list of feature names. If ``None``, uses
            positional indices.

    Returns:
        Dict mapping feature_name -> importance (summing to 1.0).
    """
    if not hasattr(model, "feature_importances_"):
        return {}
    importances = model.feature_importances_
    if importances is None or len(importances) == 0:
        return {}
    if feature_names is None or len(feature_names) != len(importances):
        feature_names = [f"feature_{i}" for i in range(len(importances))]
    total = float(importances.sum())
    if total <= 0:
        return {}
    return {
        name: round(float(imp) / total, 6)
        for name, imp in sorted(
            zip(feature_names, importances, strict=True),
            key=lambda x: x[1],
            reverse=True,
        )
    }


def _log_loss(probabilities: np.ndarray, outcomes: np.ndarray) -> float:
    """Compute binary log loss with clipping."""
    eps = 1e-12
    proba = np.clip(probabilities, eps, 1 - eps)
    return float(-np.mean(outcomes * np.log(proba) + (1 - outcomes) * np.log(1 - proba)))
