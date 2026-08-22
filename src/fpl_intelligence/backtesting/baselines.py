"""Baseline prediction models for the FPL Intelligence Engine.

These are simple, interpretable baselines that serve as reference points
for more sophisticated models. Each baseline implements the PredictionModel
protocol expected by the BacktestEngine.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import PlayerGameweekPerformance
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
    InformationAccessPolicy,
    apply_policy,
)


class RecentFormBaseline:
    """Baseline: predict based on recent form (last 3 gameweeks).

    Predicts expected points as the average of the player's points
    over their last 3 gameweeks, weighted by recency.
    """

    @property
    def model_name(self) -> str:
        return "recent_form_baseline"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Predict expected points based on recent form."""
        ctx = context or {}
        db: Session = ctx.get("db")  # type: ignore[assignment]
        if db is None:
            raise ValueError("context must contain 'db' session")

        cutoff_time = cutoff.cutoff_time
        policy: InformationAccessPolicy = ctx.get("policy", DEFAULT_POLICY)

        stmt = select(PlayerGameweekPerformance).where(
            PlayerGameweekPerformance.player_id == player_id,
        )
        try:
            condition = apply_policy(PlayerGameweekPerformance, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass

        perfs = list(db.execute(stmt).scalars().all())
        perfs_sorted = sorted(perfs, key=lambda p: p.gameweek_id or 0)
        recent = perfs_sorted[-3:]

        if not recent:
            return {
                "predicted_expected_points": 0.0,
                "confidence": 0.0,
                "data_completeness": 0.0,
                "method": "no_data",
            }

        # Weighted average: more recent = higher weight
        weights = list(range(1, len(recent) + 1))
        total_weight = sum(weights)
        weighted_points = sum(
            (p.total_points or 0) * w for p, w in zip(recent, weights, strict=True)
        )
        predicted = weighted_points / total_weight if total_weight > 0 else 0.0

        # Confidence based on data availability
        confidence = min(1.0, len(recent) / 3.0)
        completeness = min(1.0, len(perfs_sorted) / 8.0)

        return {
            "predicted_expected_points": predicted,
            "confidence": confidence,
            "data_completeness": completeness,
            "method": "recent_form",
        }

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        """Predict for multiple players."""
        results: dict[int, dict[str, Any]] = {}
        for player_id, features in features_batch.items():
            results[player_id] = self.predict(player_id, 0, features, cutoff, context)
        return results


class PointsPer90Baseline:
    """Baseline: predict based on points per 90 minutes.

    Predicts expected points as the player's historical points per 90
    minutes, adjusted for fixture difficulty.
    """

    @property
    def model_name(self) -> str:
        return "points_per_90_baseline"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Predict expected points based on points per 90."""
        ctx = context or {}
        db: Session = ctx.get("db")  # type: ignore[assignment]
        if db is None:
            raise ValueError("context must contain 'db' session")

        cutoff_time = cutoff.cutoff_time
        policy: InformationAccessPolicy = ctx.get("policy", DEFAULT_POLICY)

        stmt = select(PlayerGameweekPerformance).where(
            PlayerGameweekPerformance.player_id == player_id,
        )
        try:
            condition = apply_policy(PlayerGameweekPerformance, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass

        perfs = list(db.execute(stmt).scalars().all())

        if not perfs:
            return {
                "predicted_expected_points": 0.0,
                "confidence": 0.0,
                "data_completeness": 0.0,
                "method": "no_data",
            }

        total_points = sum(p.total_points or 0 for p in perfs)
        total_minutes = sum(p.minutes or 0 for p in perfs)

        if total_minutes == 0:
            return {
                "predicted_expected_points": 0.0,
                "confidence": 0.0,
                "data_completeness": 0.0,
                "method": "no_minutes",
            }

        pp90 = total_points / total_minutes * 90

        # Adjust for fixture difficulty if available
        difficulty = features.get("fixture_features_fixture_difficulty_model", 0.5)
        adjustment = 1.0 - (difficulty - 0.5) * 0.2  # ±10% based on difficulty

        predicted = pp90 * adjustment

        confidence = min(1.0, len(perfs) / 8.0)
        completeness = min(1.0, len(perfs) / 8.0)

        return {
            "predicted_expected_points": predicted,
            "confidence": confidence,
            "data_completeness": completeness,
            "method": "points_per_90",
        }

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        for player_id, features in features_batch.items():
            results[player_id] = self.predict(player_id, 0, features, cutoff, context)
        return results


class RollingExpectedPointsBaseline:
    """Baseline: predict using rolling expected points.

    Uses the player's recent expected points (ep_this/ep_next) from
    FPL snapshots, weighted by recency.
    """

    @property
    def model_name(self) -> str:
        return "rolling_expected_points_baseline"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Predict expected points using rolling ep_this."""
        ctx = context or {}
        db: Session = ctx.get("db")  # type: ignore[assignment]
        if db is None:
            raise ValueError("context must contain 'db' session")

        cutoff_time = cutoff.cutoff_time
        policy: InformationAccessPolicy = ctx.get("policy", DEFAULT_POLICY)

        from fpl_intelligence.db.models import FPLSnapshot

        stmt = select(FPLSnapshot).where(
            FPLSnapshot.player_id == player_id,
        )
        try:
            condition = apply_policy(FPLSnapshot, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass

        snapshots = list(db.execute(stmt).scalars().all())
        snapshots_sorted = sorted(snapshots, key=lambda s: s.event_time)

        if not snapshots_sorted:
            return {
                "predicted_expected_points": 0.0,
                "confidence": 0.0,
                "data_completeness": 0.0,
                "method": "no_data",
            }

        latest = snapshots_sorted[-1]
        predicted = latest.ep_this or latest.ep_next or 0.0

        confidence = min(1.0, len(snapshots_sorted) / 5.0)
        completeness = min(1.0, len(snapshots_sorted) / 5.0)

        return {
            "predicted_expected_points": predicted,
            "confidence": confidence,
            "data_completeness": completeness,
            "method": "rolling_expected_points",
        }

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        for player_id, features in features_batch.items():
            results[player_id] = self.predict(player_id, 0, features, cutoff, context)
        return results


class FixtureAdjustedBaseline:
    """Baseline: predict using fixture-adjusted form.

    Combines recent form with fixture difficulty to produce
    a fixture-adjusted expected points prediction.
    """

    @property
    def model_name(self) -> str:
        return "fixture_adjusted_baseline"

    @property
    def model_version(self) -> str:
        return "1.0.0"

    def predict(
        self,
        player_id: int,
        fixture_id: int,
        features: dict[str, float],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Predict expected points using fixture-adjusted form."""
        ctx = context or {}
        db: Session = ctx.get("db")  # type: ignore[assignment]
        if db is None:
            raise ValueError("context must contain 'db' session")

        cutoff_time = cutoff.cutoff_time
        policy: InformationAccessPolicy = ctx.get("policy", DEFAULT_POLICY)

        # Get recent form
        stmt = select(PlayerGameweekPerformance).where(
            PlayerGameweekPerformance.player_id == player_id,
        )
        try:
            condition = apply_policy(PlayerGameweekPerformance, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass

        perfs = list(db.execute(stmt).scalars().all())
        perfs_sorted = sorted(perfs, key=lambda p: p.gameweek_id or 0)
        recent = perfs_sorted[-3:]

        if not recent:
            return {
                "predicted_expected_points": 0.0,
                "confidence": 0.0,
                "data_completeness": 0.0,
                "method": "no_data",
            }

        # Average points per game
        avg_points = sum(p.total_points or 0 for p in recent) / len(recent)

        # Fixture difficulty adjustment
        difficulty = features.get("fixture_features_fixture_difficulty_model", 0.5)
        # difficulty is 0-1, where 1 is hardest
        # Adjust: easier fixture = higher predicted points
        adjustment = 1.0 + (0.5 - difficulty) * 0.3  # ±15% based on difficulty

        predicted = avg_points * adjustment

        confidence = min(1.0, len(recent) / 3.0)
        completeness = min(1.0, len(perfs_sorted) / 8.0)

        return {
            "predicted_expected_points": predicted,
            "confidence": confidence,
            "data_completeness": completeness,
            "method": "fixture_adjusted",
        }

    def predict_batch(
        self,
        features_batch: dict[int, dict[str, float]],
        cutoff: Any,
        context: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        for player_id, features in features_batch.items():
            results[player_id] = self.predict(player_id, 0, features, cutoff, context)
        return results
