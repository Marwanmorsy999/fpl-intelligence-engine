"""Statistical uncertainty utilities for the Phase 4.5 edge validation.

The unit of comparison is the Gameweek (not individual player observations),
because player observations within a Gameweek are correlated through shared
fixtures and match context.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np


def bootstrap_ci(
    values: list[float],
    metric_fn: Callable[[np.ndarray], float],
    n_bootstraps: int = 1000,
    seed: int = 42,
    ci_level: float = 0.95,
) -> dict[str, float]:
    """Compute a bootstrap confidence interval for a metric.

    Args:
        values: Observed samples (typically per-Gameweek metric values).
        metric_fn: Function mapping an array of samples to a scalar metric.
        n_bootstraps: Number of bootstrap resamples.
        seed: Random seed for reproducibility.
        ci_level: Confidence level (default 0.95).

    Returns:
        Dict with ``lower``, ``upper``, ``point``, ``se``, ``n``.
        All NaN if ``values`` is empty.
    """
    if not values:
        return {
            "lower": float("nan"),
            "upper": float("nan"),
            "point": float("nan"),
            "se": float("nan"),
            "n": 0,
        }

    arr = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    point = float(metric_fn(arr))

    n = len(arr)
    boot = np.empty(n_bootstraps, dtype=float)
    for i in range(n_bootstraps):
        sample = rng.choice(arr, size=n, replace=True)
        try:
            boot[i] = metric_fn(sample)
        except (ValueError, FloatingPointError, ZeroDivisionError):
            boot[i] = float("nan")

    boot = boot[np.isfinite(boot)]
    if len(boot) == 0:
        return {
            "lower": float("nan"),
            "upper": float("nan"),
            "point": point,
            "se": float("nan"),
            "n": n,
        }

    alpha = 1.0 - ci_level
    lower = float(np.percentile(boot, 100 * alpha / 2))
    upper = float(np.percentile(boot, 100 * (1 - alpha / 2)))
    se = float(np.std(boot, ddof=1))

    return {
        "lower": round(lower, 4),
        "upper": round(upper, 4),
        "point": round(point, 4),
        "se": round(se, 4),
        "n": n,
    }


def _mean_metric(values: np.ndarray) -> float:
    return float(np.mean(values))


def paired_gameweek_diff(
    gameweek_a: dict[int, float],
    gameweek_b: dict[int, float],
    higher_is_better: bool = False,
    n_bootstraps: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """Compare two models on a per-Gameweek paired basis.

    Args:
        gameweek_a: Per-Gameweek metric values for model A.
        gameweek_b: Per-Gameweek metric values for model B.
        higher_is_better: If False (lower-is-better metrics such as MAE/RMSE
            or log loss), a positive ``mean_diff`` means model A is worse.
        n_bootstraps: Number of bootstrap resamples.
        seed: Random seed.

    Returns:
        Dict with overlapping Gameweek count, mean difference (A - B),
        bootstrap CI for the mean difference, paired sign-test p-value,
        and per-Gameweek differences.
    """
    common_gws = sorted(set(gameweek_a) & set(gameweek_b))
    if not common_gws:
        return {
            "n_gameweeks": 0,
            "mean_diff": float("nan"),
            "ci_lower": float("nan"),
            "ci_upper": float("nan"),
            "p_value": float("nan"),
            "higher_is_better": higher_is_better,
            "differences": {},
            "conclusion": "insufficient_paired_data",
        }

    diffs = [gameweek_a[gw] - gameweek_b[gw] for gw in common_gws]
    diff_arr = np.asarray(diffs, dtype=float)

    mean_diff = float(np.mean(diff_arr))
    # Bootstrap CI for the mean difference.
    rng = np.random.default_rng(seed)
    n = len(diff_arr)
    boot_means = np.empty(n_bootstraps, dtype=float)
    for i in range(n_bootstraps):
        sample = rng.choice(diff_arr, size=n, replace=True)
        boot_means[i] = float(np.mean(sample))
    alpha = 0.05
    ci_lower = float(np.percentile(boot_means, 100 * alpha / 2))
    ci_upper = float(np.percentile(boot_means, 100 * (1 - alpha / 2)))

    # Paired sign test (non-parametric): fraction of Gameweeks where A beats B.
    if higher_is_better:
        wins_a = int(np.sum(diff_arr > 0))
        wins_b = int(np.sum(diff_arr < 0))
    else:
        wins_a = int(np.sum(diff_arr < 0))
        wins_b = int(np.sum(diff_arr > 0))
    ties = n - wins_a - wins_b

    # Binomial p-value under H0: P(A wins) = 0.5 (excluding ties).
    p_value: float = float("nan")
    if wins_a + wins_b > 0:
        from scipy.stats import binomtest

        p_value = binomtest(wins_a, n=wins_a + wins_b, p=0.5).pvalue

    # Interpret: positive mean_diff favors A (higher-is-better) or A (lower metric).
    if abs(mean_diff) < 1e-12:
        conclusion = "no_difference"
    elif higher_is_better:
        conclusion = "a_better" if mean_diff > 0 and ci_lower > 0 else "b_better" if mean_diff < 0 and ci_upper < 0 else "inconclusive"
    else:
        conclusion = "a_better" if mean_diff < 0 and ci_upper < 0 else "b_better" if mean_diff > 0 and ci_lower > 0 else "inconclusive"

    return {
        "n_gameweeks": n,
        "mean_diff": round(mean_diff, 4),
        "ci_lower": round(ci_lower, 4),
        "ci_upper": round(ci_upper, 4),
        "p_value": round(p_value, 4) if not math.isnan(p_value) else None,
        "wins_a": wins_a,
        "wins_b": wins_b,
        "ties": ties,
        "higher_is_better": higher_is_better,
        "differences": {str(gw): round(d, 4) for gw, d in zip(common_gws, diffs, strict=True)},
        "conclusion": conclusion,
    }
