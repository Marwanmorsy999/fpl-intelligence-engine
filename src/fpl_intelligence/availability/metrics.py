"""Real evaluation metrics for Phase 7 availability intelligence.

These functions compute honest metrics from model predictions vs actual
historical outcomes. They NEVER return ``0.0`` for "not calculated": a metric
that cannot be computed from the available data returns ``None`` (rendered as
``NOT_AVAILABLE`` downstream). Zero is reserved exclusively for a genuine
zero-valued metric.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np


def _log_loss(proba: list[float], actual: list[float]) -> float:
    """Binary log loss with clipping."""
    eps = 1e-12
    p = np.clip(np.asarray(proba, dtype=float), eps, 1 - eps)
    y = np.asarray(actual, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _rank(x: np.ndarray) -> np.ndarray:
    """Average ranks for a numeric series, handling ties correctly."""
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=float)
    i = 0
    n = len(x)
    while i < n:
        j = i
        while j + 1 < n and x[order[j + 1]] == x[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(a: list[float], b: list[float]) -> float:
    """Spearman rank correlation between two paired sequences.

    Returns NaN when fewer than two distinct ranks exist in either series
    (e.g. a constant series), so a degenerate input never yields a spurious
    non-zero correlation.
    """
    if len(a) != len(b) or len(a) < 2:
        return float("nan")
    x = np.asarray(a, dtype=float)
    y = np.asarray(b, dtype=float)
    if np.all(x == x[0]) or np.all(y == y[0]):
        return float("nan")
    rx = _rank(x)
    ry = _rank(y)
    return float(np.corrcoef(rx, ry)[0, 1])


def availability_metrics(
    start_prob: list[float],
    actual_started: list[float],
    expected_minutes: list[float],
    actual_minutes: list[float],
) -> dict[str, Any]:
    """Compute availability-layer metrics.

    Args:
        start_prob: Predicted probability of starting (0..1) per observation.
        actual_started: 1.0 if the player actually started (minutes >= 60),
            else 0.0.
        expected_minutes: Predicted expected minutes per observation.
        actual_minutes: Actual minutes played per observation.

    Returns a dict with keys:
        start_brier, start_log_loss, minutes_mae, minutes_rmse,
        prob60_brier, prob60_calibration_ece, n.
    Each metric is ``None`` when it cannot be computed. ``n`` is always an int.
    """
    n = len(start_prob)
    out: dict[str, Any] = {"n": n}
    if n == 0:
        for k in (
            "start_brier", "start_log_loss", "minutes_mae", "minutes_rmse",
            "prob60_brier", "prob60_calibration_ece",
        ):
            out[k] = None
        return out

    # Start probability Brier + log loss.
    sp = [min(1.0, max(0.0, float(p))) for p in start_prob]
    out["start_brier"] = round(
        float(np.mean((np.asarray(sp) - np.asarray(actual_started)) ** 2)), 4
    )
    out["start_log_loss"] = round(_log_loss(sp, list(actual_started)), 4)

    # Expected-minutes MAE/RMSE.
    em = [float(m) for m in expected_minutes]
    am = [float(m) for m in actual_minutes]
    diff = np.asarray(em) - np.asarray(am)
    out["minutes_mae"] = round(float(np.mean(np.abs(diff))), 4)
    out["minutes_rmse"] = round(float(np.sqrt(np.mean(diff ** 2))), 4)

    # 60+ minute probability: use start probability as P(minutes>=60) estimate.
    actual60 = [1.0 if m >= 60 else 0.0 for m in am]
    sp60 = [min(1.0, max(0.0, float(x))) for x in sp]
    out["prob60_brier"] = round(
        float(np.mean((np.asarray(sp60) - np.asarray(actual60)) ** 2)), 4
    )
    out["prob60_calibration_ece"] = round(_calibration_ece(sp60, actual60), 4)

    return out


def prediction_metrics(
    expected_points: list[float],
    actual_points: list[float],
) -> dict[str, Any]:
    """Compute player-prediction-layer metrics.

    Args:
        expected_points: Predicted expected points per observation.
        actual_points: Actual FPL points per observation.

    Returns: ``points_mae``, ``points_rmse``, ``spearman``, ``n``.
    Each metric is ``None`` when it cannot be computed; ``n`` is always an int.
    """
    n = len(expected_points)
    out: dict[str, Any] = {
        "n": n, "points_mae": None, "points_rmse": None, "spearman": None,
    }
    if n == 0:
        return out
    ep = [float(v) for v in expected_points]
    ap = [float(v) for v in actual_points]
    diff = np.asarray(ep) - np.asarray(ap)
    out["points_mae"] = round(float(np.mean(np.abs(diff))), 4)
    out["points_rmse"] = round(float(np.sqrt(np.mean(diff ** 2))), 4)
    sp = _spearman(ep, ap)
    out["spearman"] = round(sp, 4) if not math.isnan(sp) else None
    return out


def _calibration_ece(
    probabilities: list[float], actual: list[float], n_bins: int = 10
) -> float:
    """Expected calibration error for a binary probability predictor.

    Returns NaN when the sample is empty or every prediction falls in one bin.
    """
    p = np.clip(np.asarray(probabilities, dtype=float), 0.0, 1.0)
    y = np.asarray(actual, dtype=float)
    if len(p) == 0:
        return float("nan")
    ece = 0.0
    n_bins = min(n_bins, max(1, len(p)))
    thresholds = np.linspace(0.0, 1.0, n_bins + 1)
    for i in range(n_bins):
        lo, hi = thresholds[i], thresholds[i + 1]
        mask = (p >= lo) & (p < hi)
        if np.sum(mask) == 0:
            continue
        bin_conf = float(np.mean(p[mask]))
        bin_acc = float(np.mean(y[mask]))
        ece += (np.sum(mask) / len(p)) * abs(bin_conf - bin_acc)
    return ece
