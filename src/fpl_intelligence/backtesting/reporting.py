"""Backtest reporting and result aggregation.

Provides utilities for generating human-readable reports from
backtest results, including summary statistics and per-gameweek breakdowns.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fpl_intelligence.backtesting.evaluation import BacktestEvaluator
from fpl_intelligence.backtesting.models import (
    BacktestRun,
    PlayerPrediction,
)


class BacktestReport:
    """Generates reports from backtest results.

    Aggregates gameweek-level results into season-level summaries
    and provides formatted output for human consumption.
    """

    def __init__(self, db: Session) -> None:
        self._db = db
        self._evaluator = BacktestEvaluator()

    def generate_report(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        """Generate a comprehensive report for a backtest run.

        Args:
            run_id: The unique identifier of the backtest run.

        Returns:
            Dict with report data including summary, per-gameweek results,
            and aggregate metrics.

        Raises:
            ValueError: If the run is not found.
        """
        run = self._db.scalar(select(BacktestRun).where(BacktestRun.run_id == run_id))
        if run is None:
            raise ValueError(f"BacktestRun {run_id!r} not found.")

        config = run.config
        gw_results = run.gameweek_results

        # Aggregate metrics across all gameweeks
        all_metrics: list[dict[str, float]] = []
        per_gw_metrics: list[dict[str, Any]] = []

        for gw in gw_results:
            metrics = gw.evaluation_metrics or {}
            all_metrics.append(metrics)
            per_gw_metrics.append(
                {
                    "season": gw.season,
                    "gameweek": gw.gameweek,
                    "decision_cutoff": gw.decision_cutoff.isoformat()
                    if gw.decision_cutoff
                    else None,
                    "metrics": metrics,
                }
            )

        # Compute aggregate metrics
        summary = self._aggregate_metrics(all_metrics)

        # Get prediction count
        pred_count = (
            self._db.scalar(
                select(func.count())
                .select_from(PlayerPrediction)
                .where(PlayerPrediction.run_id == run.id)
            )
            or 0
        )

        return {
            "run_id": run_id,
            "status": run.status,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "config": {
                "season": config.season if config else None,
                "start_gameweek": config.start_gameweek if config else None,
                "end_gameweek": config.end_gameweek if config else None,
                "information_access_policy": config.information_access_policy if config else None,
                "feature_version": config.feature_version if config else None,
                "model_version": config.model_version if config else None,
            },
            "summary": summary,
            "per_gameweek": per_gw_metrics,
            "total_predictions": pred_count,
            "total_gameweeks": len(gw_results),
        }

    def _aggregate_metrics(
        self,
        all_metrics: list[dict[str, float]],
    ) -> dict[str, float]:
        """Aggregate metrics across gameweeks.

        Args:
            all_metrics: List of per-gameweek metric dicts.

        Returns:
            Dict with averaged metrics.
        """
        if not all_metrics:
            return {}

        # Collect all metric keys
        keys: set[str] = set()
        for m in all_metrics:
            keys.update(m.keys())

        # Average each metric
        summary: dict[str, float] = {}
        for key in keys:
            values = [m[key] for m in all_metrics if key in m and isinstance(m[key], (int, float))]
            if values:
                summary[key] = sum(values) / len(values)

        return summary

    def print_report(
        self,
        run_id: str,
        output: str | None = None,
    ) -> str:
        """Generate and print a human-readable report.

        Args:
            run_id: The backtest run ID.
            output: Optional file path to write the report to.

        Returns:
            The formatted report string.
        """
        report = self.generate_report(run_id)

        lines: list[str] = []
        lines.append("=" * 70)
        lines.append("BACKTEST REPORT")
        lines.append("=" * 70)
        lines.append(f"Run ID: {report['run_id']}")
        lines.append(f"Status: {report['status']}")
        lines.append(f"Created: {report['created_at']}")
        lines.append("")

        config = report["config"]
        lines.append("Configuration:")
        lines.append(f"  Season: {config['season']}")
        lines.append(f"  Gameweeks: {config['start_gameweek']} - {config['end_gameweek']}")
        lines.append(f"  Policy: {config['information_access_policy']}")
        lines.append(f"  Feature Version: {config['feature_version']}")
        lines.append(f"  Model Version: {config['model_version']}")
        lines.append("")

        lines.append("Summary Metrics:")
        summary = report["summary"]
        for key, value in sorted(summary.items()):
            if isinstance(value, float):
                lines.append(f"  {key}: {value:.4f}")
            else:
                lines.append(f"  {key}: {value}")
        lines.append("")

        lines.append("Per-Gameweek Results:")
        header = (
            f"  {'GW':>4}  {'MAE':>8}  {'RMSE':>8}  "
            f"{'Spearman':>10}  {'Top1':>6}  {'Top3':>6}  "
            f"{'Top5':>6}  {'Top10':>6}"
        )
        lines.append(header)
        for gw in report["per_gameweek"]:
            m = gw["metrics"]
            lines.append(
                f"  {gw['gameweek']:>4}  "
                f"{m.get('mae', 0):>8.4f}  "
                f"{m.get('rmse', 0):>8.4f}  "
                f"{m.get('spearman', 0):>10.4f}  "
                f"{m.get('top1_hit_rate', 0):>6.2f}  "
                f"{m.get('top3_hit_rate', 0):>6.2f}  "
                f"{m.get('top5_hit_rate', 0):>6.2f}  "
                f"{m.get('top10_hit_rate', 0):>6.2f}"
            )
        lines.append("")
        lines.append(f"Total Predictions: {report['total_predictions']}")
        lines.append(f"Total Gameweeks: {report['total_gameweeks']}")
        lines.append("=" * 70)

        report_text = "\n".join(lines)

        if output:
            with open(output, "w") as f:
                f.write(report_text)

        return report_text
