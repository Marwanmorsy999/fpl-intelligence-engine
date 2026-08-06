"""Evaluation metrics for backtesting.

Provides standard regression and ranking metrics for evaluating
prediction quality, as well as FPL-specific metrics like top-k hit rates.
"""

from __future__ import annotations

from typing import Any

import numpy as np


class BacktestEvaluator:
    """Evaluates backtest predictions against actual outcomes.

    Computes:
        - MAE (Mean Absolute Error)
        - RMSE (Root Mean Square Error)
        - Spearman rank correlation
        - Top-k hit rates (top-1, top-3, top-5, top-10)
        - Coverage (fraction of players with predictions)
    """

    def evaluate(
        self,
        predictions: dict[int, dict[str, Any]],
        actuals: dict[int, dict[str, Any]],
    ) -> dict[str, float]:
        """Evaluate predictions against actuals.

        Args:
            predictions: Dict mapping player_id -> prediction dict.
                Each prediction dict should have 'predicted_expected_points'.
            actuals: Dict mapping player_id -> actual dict.
                Each actual dict should have 'actual_points'.

        Returns:
            Dict of evaluation metrics.
        """
        # Align predictions and actuals
        common_ids = set(predictions.keys()) & set(actuals.keys())

        if not common_ids:
            return {
                "mae": float("nan"),
                "rmse": float("nan"),
                "spearman": float("nan"),
                "coverage": 0.0,
                "n_predictions": len(predictions),
                "n_actuals": len(actuals),
                "n_common": 0,
                "top1_hit_rate": 0.0,
                "top3_hit_rate": 0.0,
                "top5_hit_rate": 0.0,
                "top10_hit_rate": 0.0,
            }

        pred_values = []
        actual_values = []
        for pid in common_ids:
            pred = predictions[pid].get("predicted_expected_points", 0.0)
            actual = actuals[pid].get("actual_points", 0.0)
            pred_values.append(float(pred))
            actual_values.append(float(actual))

        pred_arr = np.array(pred_values)
        actual_arr = np.array(actual_values)

        # MAE
        mae = float(np.mean(np.abs(pred_arr - actual_arr)))

        # RMSE
        rmse = float(np.sqrt(np.mean((pred_arr - actual_arr) ** 2)))

        # Spearman rank correlation
        spearman = self._spearman_rank(pred_arr, actual_arr)

        # Top-k hit rates
        top_k_metrics = self._top_k_hit_rates(pred_values, actual_values)

        # Coverage
        coverage = len(common_ids) / len(actuals) if actuals else 0.0

        return {
            "mae": mae,
            "rmse": rmse,
            "spearman": spearman,
            "coverage": coverage,
            "n_predictions": len(predictions),
            "n_actuals": len(actuals),
            "n_common": len(common_ids),
            **top_k_metrics,
        }

    def _spearman_rank(
        self, pred: np.ndarray, actual: np.ndarray
    ) -> float:
        """Compute Spearman rank correlation coefficient."""
        if len(pred) < 2:
            return 0.0

        # Rank the values
        pred_ranks = self._rankdata(pred)
        actual_ranks = self._rankdata(actual)

        # Spearman = 1 - 6*sum(d^2) / (n*(n^2-1))
        n = len(pred)
        d_squared = sum(
            (p - a) ** 2 for p, a in zip(pred_ranks, actual_ranks, strict=True)
        )
        denominator = n * (n ** 2 - 1)
        if denominator == 0:
            return 0.0
        return 1.0 - (6.0 * d_squared / denominator)

    def _rankdata(self, arr: np.ndarray) -> list[float]:
        """Compute ranks of array elements (average rank for ties)."""
        n = len(arr)
        indexed = sorted(range(n), key=lambda i: arr[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and arr[indexed[j + 1]] == arr[indexed[i]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1  # 1-based average rank
            for k in range(i, j + 1):
                ranks[indexed[k]] = avg_rank
            i = j + 1
        return ranks

    def _top_k_hit_rates(
        self, pred_values: list[float], actual_values: list[float]
    ) -> dict[str, float]:
        """Compute top-k hit rates.

        A "hit" means the player is in the top-k by prediction AND
        in the top-k by actual performance.
        """
        n = len(pred_values)
        results: dict[str, float] = {}

        for k in [1, 3, 5, 10]:
            if n < k:
                results[f"top{k}_hit_rate"] = 0.0
                continue

            # Get top-k indices by prediction and by actual
            pred_top_k = set(
                sorted(range(n), key=lambda i: pred_values[i], reverse=True)[:k]
            )
            actual_top_k = set(
                sorted(range(n), key=lambda i: actual_values[i], reverse=True)[:k]
            )

            hits = len(pred_top_k & actual_top_k)
            results[f"top{k}_hit_rate"] = hits / k

        return results

    def evaluate_by_season(
        self,
        all_predictions: dict[str, dict[int, dict[str, Any]]],
        all_actuals: dict[str, dict[int, dict[str, Any]]],
    ) -> dict[str, dict[str, float]]:
        """Evaluate metrics per season.

        Args:
            all_predictions: Dict mapping season -> {player_id -> prediction}.
            all_actuals: Dict mapping season -> {player_id -> actual}.

        Returns:
            Dict mapping season -> metrics.
        """
        results: dict[str, dict[str, float]] = {}
        for season in all_predictions:
            if season in all_actuals:
                results[season] = self.evaluate(
                    all_predictions[season], all_actuals[season]
                )
        return results

    def evaluate_by_gameweek(
        self,
        all_predictions: dict[int, dict[int, dict[str, Any]]],
        all_actuals: dict[int, dict[int, dict[str, Any]]],
    ) -> dict[int, dict[str, float]]:
        """Evaluate metrics per gameweek.

        Args:
            all_predictions: Dict mapping gameweek -> {player_id -> prediction}.
            all_actuals: Dict mapping gameweek -> {player_id -> actual}.

        Returns:
            Dict mapping gameweek -> metrics.
        """
        results: dict[int, dict[str, float]] = {}
        for gw in all_predictions:
            if gw in all_actuals:
                results[gw] = self.evaluate(
                    all_predictions[gw], all_actuals[gw]
                )
        return results

    def evaluate_by_position(
        self,
        predictions: dict[int, dict[str, Any]],
        actuals: dict[int, dict[str, Any]],
        player_positions: dict[int, int],
    ) -> dict[str, dict[str, float]]:
        """Evaluate metrics by player position.

        Args:
            predictions: Dict mapping player_id -> prediction.
            actuals: Dict mapping player_id -> actual.
            player_positions: Dict mapping player_id -> position_code.

        Returns:
            Dict mapping position name -> metrics.
        """
        position_names = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
        results: dict[str, dict[str, float]] = {}

        for pos_code, pos_name in position_names.items():
            pos_predictions = {
                pid: pred for pid, pred in predictions.items()
                if player_positions.get(pid) == pos_code
            }
            pos_actuals = {
                pid: act for pid, act in actuals.items()
                if player_positions.get(pid) == pos_code
            }
            if pos_predictions and pos_actuals:
                results[pos_name] = self.evaluate(pos_predictions, pos_actuals)

        return results

    def evaluate_by_price_range(
        self,
        predictions: dict[int, dict[str, Any]],
        actuals: dict[int, dict[str, Any]],
        player_prices: dict[int, float],
    ) -> dict[str, dict[str, float]]:
        """Evaluate metrics by player price range.

        Args:
            predictions: Dict mapping player_id -> prediction.
            actuals: Dict mapping player_id -> actual.
            player_prices: Dict mapping player_id -> price.

        Returns:
            Dict mapping price range -> metrics.
        """
        ranges = {
            "cheap": (0, 5.0),
            "mid": (5.0, 8.0),
            "expensive": (8.0, float("inf")),
        }
        results: dict[str, dict[str, float]] = {}

        for range_name, (low, high) in ranges.items():
            range_predictions = {
                pid: pred for pid, pred in predictions.items()
                if low <= player_prices.get(pid, 0) < high
            }
            range_actuals = {
                pid: act for pid, act in actuals.items()
                if low <= player_prices.get(pid, 0) < high
            }
            if range_predictions and range_actuals:
                results[range_name] = self.evaluate(range_predictions, range_actuals)

        return results
