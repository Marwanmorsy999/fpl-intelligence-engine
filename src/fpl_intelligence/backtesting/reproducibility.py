"""Backtest reproducibility utilities.

Ensures that backtest runs can be exactly reproduced by recording
all relevant configuration, feature versions, and model versions.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.backtesting.models import BacktestConfig, BacktestRun


class BacktestReproducer:
    """Manages reproducibility of backtest runs.

    Records a fingerprint of the backtest configuration, feature versions,
    and model versions to ensure that results can be exactly reproduced.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def compute_fingerprint(
        self,
        config: BacktestConfig,
        feature_versions: dict[str, str],
        model_version: str,
    ) -> str:
        """Compute a deterministic fingerprint for a backtest configuration.

        Args:
            config: The backtest configuration.
            feature_versions: Dict mapping feature name -> version.
            model_version: The model version string.

        Returns:
            A SHA-256 hash string that uniquely identifies this configuration.
        """
        fingerprint_data = {
            "season": config.season,
            "start_gameweek": config.start_gameweek,
            "end_gameweek": config.end_gameweek,
            "decision_timing": config.decision_timing,
            "information_access_policy": config.information_access_policy,
            "feature_version": config.feature_version,
            "model_version": model_version,
            "random_seed": config.random_seed,
            "simulation_count": config.simulation_count,
            "feature_versions": feature_versions,
            "config_data": config.config_data,
        }

        raw = json.dumps(fingerprint_data, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def reproduce_backtest(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        """Reproduce a backtest run by its run ID.

        Retrieves the original configuration and re-runs the backtest
        with the same parameters.

        Args:
            run_id: The unique identifier of the backtest run to reproduce.

        Returns:
            Dict with reproduction status and results.

        Raises:
            ValueError: If the run is not found.
        """
        run = self._db.scalar(select(BacktestRun).where(BacktestRun.run_id == run_id))
        if run is None:
            raise ValueError(f"BacktestRun {run_id!r} not found.")

        config = run.config
        if config is None:
            raise ValueError(f"BacktestConfig for run {run_id!r} not found.")

        return {
            "run_id": run_id,
            "config": {
                "season": config.season,
                "start_gameweek": config.start_gameweek,
                "end_gameweek": config.end_gameweek,
                "decision_timing": config.decision_timing,
                "information_access_policy": config.information_access_policy,
                "feature_version": config.feature_version,
                "model_version": config.model_version,
                "random_seed": config.random_seed,
                "simulation_count": config.simulation_count,
            },
            "status": run.status,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "gameweek_results": [
                {
                    "season": gw.season,
                    "gameweek": gw.gameweek,
                    "decision_cutoff": gw.decision_cutoff.isoformat()
                    if gw.decision_cutoff
                    else None,
                    "evaluation_metrics": gw.evaluation_metrics,
                }
                for gw in run.gameweek_results
            ],
        }

    def verify_reproducibility(
        self,
        run_id: str,
        feature_versions: dict[str, str],
        model_version: str,
    ) -> bool:
        """Verify that a backtest run can be reproduced with the given versions.

        Args:
            run_id: The run ID to verify.
            feature_versions: Current feature versions.
            model_version: Current model version.

        Returns:
            True if the versions match what was used in the original run.
        """
        run = self._db.scalar(select(BacktestRun).where(BacktestRun.run_id == run_id))
        if run is None:
            return False

        if run.feature_version != config_feature_version(feature_versions):
            return False

        return run.model_version == model_version


def config_feature_version(feature_versions: dict[str, str]) -> str:
    """Compute a combined feature version string from individual versions.

    Args:
        feature_versions: Dict mapping feature name -> version.

    Returns:
        A combined version string.
    """
    if not feature_versions:
        return "1.0.0"
    parts = sorted(f"{k}=={v}" for k, v in feature_versions.items())
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
