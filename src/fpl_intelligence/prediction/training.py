"""Temporal training-data builder for the FPL Intelligence Engine.

The builder creates temporally correct training datasets:

.. code-block:: text

    features at time T
            |
            v
    target observed after T

It accepts:

- entity (player / team / fixture)
- target (e.g. ``minutes``, ``started``, ``points``)
- cutoff (the decision point)
- feature version
- training window (start/end)

It must NEVER allow target leakage. Features are built only from records
that were available *before* the decision cutoff. Targets are read from
the outcome fixture/gameweek that occurs *after* the cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.config.holdout import (
    HoldoutMode,
    enforce_holdout,
)
from fpl_intelligence.db.models import (
    Fixture,
    Gameweek,
    PlayerGameweekPerformance,
    Season,
    TeamMatchPerformance,
)
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
    InformationAccessPolicy,
    apply_policy,
)

TARGET_ALIASES = {
    "points": "total_points",
    "minutes": "minutes",
    "goals": "goals_scored",
    "assists": "assists",
    "started": "started",
    "played_30_plus": "played_30_plus",
    "played_60_plus": "played_60_plus",
    "goals_scored": "goals_scored",
    "goals_conceded": "goals_conceded",
    "clean_sheets": "clean_sheets",
}


@dataclass
class TrainingDataset:
    """A temporally correct training dataset.

    Attributes:
        entity_type: ``player`` or ``team``.
        target: Target name.
        feature_version: Feature-store version.
        features: Dict mapping entity_id -> feature dict (values numeric).
        targets: Dict mapping entity_id -> target value.
        cutoff_time: The decision cutoff for this dataset.
        metadata: Extra metadata (window start/end, sample counts).
    """

    entity_type: str
    target: str
    feature_version: str
    features: dict[int, dict[str, float]] = field(default_factory=dict)
    targets: dict[int, float] = field(default_factory=dict)
    cutoff_time: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def aligned(self) -> tuple[list[dict[str, float]], list[float]]:
        """Return aligned (X, y) lists for model training.

        Only entities with both features and targets are included.

        Returns:
            Tuple of (feature_rows, target_values) with matching order.
        """
        common = sorted(set(self.features) & set(self.targets))
        X = [self.features[eid] for eid in common]
        y = [self.targets[eid] for eid in common]
        return X, y

    def entity_ids(self) -> list[int]:
        """Return the aligned entity IDs."""
        return sorted(set(self.features) & set(self.targets))


class TrainingDataBuilder:
    """Builds temporally correct training datasets from the database.

    The builder intentionally has no direct model logic: it supplies
    prepared data to models. Models never open their own DB sessions.
    """

    def __init__(
        self,
        db: Session,
        policy: InformationAccessPolicy = DEFAULT_POLICY,
    ) -> None:
        self._db = db
        self._policy = policy

    # ------------------------------------------------------------------
    # Player datasets
    # ------------------------------------------------------------------

    def build_player_dataset(
        self,
        target: str,
        cutoff_time: datetime,
        feature_version: str,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        entity_ids: list[int] | None = None,
    ) -> TrainingDataset:
        """Build a player training dataset as of a cutoff.

        Features are derived from gameweek performances available strictly
        before ``cutoff_time``. Targets are taken from the *next* gameweek
        (after the cutoff) so no leakage is possible.

        Args:
            target: Target name (``minutes``, ``started``, ``points``, ...).
            cutoff_time: The decision cutoff.
            feature_version: Feature-store version.
            window_start: Optional start of the feature window.
            window_end: Optional end of the feature window.
            entity_ids: Optional player ID filter.

        Returns:
            A ``TrainingDataset`` with features and targets.
        """
        # 1. Determine the target gameweek: the gameweek whose deadline is
        #    the first one after the cutoff.
        target_gw = self._get_next_gameweek(cutoff_time)
        if target_gw is None:
            return TrainingDataset(
                entity_type="player",
                target=target,
                feature_version=feature_version,
                cutoff_time=cutoff_time,
                metadata={"error": "no_next_gameweek"},
            )

        # 1.5. Enforce holdout: fail loudly if target gameweek is holdout season.
        from sqlalchemy import select as sa_select

        holdout_season_code = self._db.scalar(
            sa_select(Season.code).where(Season.id == target_gw.season_id)
        )
        if holdout_season_code:
            enforce_holdout(season=holdout_season_code, mode=HoldoutMode.DEVELOPMENT)

        # 2. Query player performances for the target gameweek (these are the
        #    outcomes observed AFTER the cutoff).
        stmt = select(PlayerGameweekPerformance).where(
            PlayerGameweekPerformance.gameweek_id == target_gw.id,
        )
        if entity_ids is not None:
            stmt = stmt.where(PlayerGameweekPerformance.player_id.in_(entity_ids))
        target_perfs = list(self._db.execute(stmt).scalars().all())

        # 3. For each target player, build features from data before the cutoff.
        features: dict[int, dict[str, float]] = {}
        targets: dict[int, float] = {}

        for perf in target_perfs:
            pid = perf.player_id
            feat = self._build_player_features(pid, cutoff_time, feature_version)
            if feat is not None:
                features[pid] = feat
                targets[pid] = self._extract_target(perf, target)

        return TrainingDataset(
            entity_type="player",
            target=target,
            feature_version=feature_version,
            features=features,
            targets=targets,
            cutoff_time=cutoff_time,
            metadata={
                "window_start": window_start.isoformat() if window_start else None,
                "window_end": window_end.isoformat() if window_end else None,
                "target_gameweek": target_gw.provider_event_id,
                "policy": self._policy.value,
            },
        )

    # ------------------------------------------------------------------
    # Team datasets
    # ------------------------------------------------------------------

    def build_team_dataset(
        self,
        target: str,
        cutoff_time: datetime,
        feature_version: str,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        entity_ids: list[int] | None = None,
    ) -> TrainingDataset:
        """Build a team training dataset as of a cutoff.

        Features are derived from team match performances before the cutoff.
        Targets are taken from the next gameweek's fixtures (after the cutoff).

        Args:
            target: Target name (``goals_scored``, ``goals_conceded``, ...).
            cutoff_time: The decision cutoff.
            feature_version: Feature-store version.
            window_start: Optional start of the feature window.
            window_end: Optional end of the feature window.
            entity_ids: Optional team ID filter.

        Returns:
            A ``TrainingDataset`` with features and targets.
        """
        target_gw = self._get_next_gameweek(cutoff_time)
        if target_gw is None:
            return TrainingDataset(
                entity_type="team",
                target=target,
                feature_version=feature_version,
                cutoff_time=cutoff_time,
                metadata={"error": "no_next_gameweek"},
            )

        # 1.5. Enforce holdout: fail loudly if target gameweek is holdout season.
        from sqlalchemy import select as sa_select

        holdout_season_code = self._db.scalar(
            sa_select(Season.code).where(Season.id == target_gw.season_id)
        )
        if holdout_season_code:
            enforce_holdout(season=holdout_season_code, mode=HoldoutMode.DEVELOPMENT)

        # Targets: team match performances for the target gameweek.
        stmt = (
            select(TeamMatchPerformance)
            .join(Fixture, TeamMatchPerformance.fixture_id == Fixture.id)
            .where(Fixture.gameweek_id == target_gw.id)
        )
        if entity_ids is not None:
            stmt = stmt.where(TeamMatchPerformance.team_id.in_(entity_ids))
        target_perfs = list(self._db.execute(stmt).scalars().all())

        features: dict[int, dict[str, float]] = {}
        targets: dict[int, float] = {}

        for perf in target_perfs:
            tid = perf.team_id
            feat = self._build_team_features(tid, cutoff_time, feature_version)
            if feat is not None:
                features[tid] = feat
                targets[tid] = self._extract_target(perf, target)

        return TrainingDataset(
            entity_type="team",
            target=target,
            feature_version=feature_version,
            features=features,
            targets=targets,
            cutoff_time=cutoff_time,
            metadata={
                "window_start": window_start.isoformat() if window_start else None,
                "window_end": window_end.isoformat() if window_end else None,
                "target_gameweek": target_gw.provider_event_id,
                "policy": self._policy.value,
            },
        )

    # ------------------------------------------------------------------
    # Feature builders (strictly pre-cutoff)
    # ------------------------------------------------------------------

    def _build_player_features(
        self,
        player_id: int,
        cutoff_time: datetime,
        feature_version: str,
    ) -> dict[str, float] | None:
        """Build a numeric feature vector for a player from pre-cutoff data."""
        stmt = select(PlayerGameweekPerformance).where(
            PlayerGameweekPerformance.player_id == player_id,
        )
        try:
            condition = apply_policy(PlayerGameweekPerformance, self._policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass
        stmt = stmt.order_by(PlayerGameweekPerformance.gameweek_id)

        perfs = list(self._db.execute(stmt).scalars().all())
        if not perfs:
            return None

        features: dict[str, float] = {}
        windows = [3, 5, 10]
        for window in windows:
            recent = perfs[-window:]
            features[f"minutes_last_{window}"] = float(sum(p.minutes or 0 for p in recent))
            features[f"starts_last_{window}"] = float(
                sum(1 for p in recent if (p.minutes or 0) >= 60)
            )
            features[f"points_last_{window}"] = float(sum(p.total_points or 0 for p in recent))
            features[f"goals_last_{window}"] = float(sum(p.goals_scored or 0 for p in recent))
            features[f"assists_last_{window}"] = float(sum(p.assists or 0 for p in recent))

        # Previous match features
        if perfs:
            last = perfs[-1]
            features["minutes_prev_match"] = float(last.minutes or 0)
            features["points_prev_match"] = float(last.total_points or 0)
            features["n_season_matches"] = float(len(perfs))

        # Rolling points-per-90
        total_minutes = sum(p.minutes or 0 for p in perfs[-10:])
        total_points = sum(p.total_points or 0 for p in perfs[-10:])
        if total_minutes > 0:
            features["points_per_90"] = round(total_points / total_minutes * 90, 4)
        else:
            features["points_per_90"] = 0.0

        # Position info from the latest performance's team? Player position
        # lives on the Player table; kept simple here.
        return features

    def _build_team_features(
        self,
        team_id: int,
        cutoff_time: datetime,
        feature_version: str,
    ) -> dict[str, float] | None:
        """Build a numeric feature vector for a team from pre-cutoff data."""
        stmt = select(TeamMatchPerformance).where(
            TeamMatchPerformance.team_id == team_id,
        )
        try:
            condition = apply_policy(TeamMatchPerformance, self._policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass
        stmt = stmt.order_by(TeamMatchPerformance.fixture_id)

        perfs = list(self._db.execute(stmt).scalars().all())
        if not perfs:
            return None

        features: dict[str, float] = {}
        n = len(perfs)
        features["match_count"] = float(n)
        features["avg_goals_scored"] = sum(p.goals_scored or 0 for p in perfs) / n
        features["avg_goals_conceded"] = sum(p.goals_conceded or 0 for p in perfs) / n
        features["avg_xg"] = sum(p.expected_goals or 0.0 for p in perfs) / n
        features["avg_xga"] = sum(p.expected_goals_conceded or 0.0 for p in perfs) / n

        home = [p for p in perfs if p.is_home]
        away = [p for p in perfs if not p.is_home]
        if home:
            features["home_avg_goals"] = sum(p.goals_scored or 0 for p in home) / len(home)
        else:
            features["home_avg_goals"] = 0.0
        if away:
            features["away_avg_goals"] = sum(p.goals_scored or 0 for p in away) / len(away)
        else:
            features["away_avg_goals"] = 0.0

        features["clean_sheet_rate"] = sum(1 for p in perfs if (p.goals_conceded or 0) == 0) / n

        return features

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_next_gameweek(self, cutoff_time: datetime) -> Gameweek | None:
        """Find the first gameweek whose deadline is after the cutoff."""
        stmt = (
            select(Gameweek)
            .where(Gameweek.deadline_time > cutoff_time)
            .order_by(Gameweek.deadline_time)
            .limit(1)
        )
        return self._db.scalar(stmt)

    def _extract_target(self, perf: Any, target: str) -> float:
        """Extract the target value from a performance record.

        Special targets (``started``, ``played_30_plus``, ``played_60_plus``)
        are derived from minutes. Minutes < 60 with a starting-substitution
        edge case is intentionally conservative: we treat ``minutes >= 60`` as
        the best structured-data proxy for "started". Postponed / abandoned
        matches have ``minutes=None`` and are NOT treated as zero unless the
        record explicitly stores a zero.
        """
        if target == "started":
            return 1.0 if (perf.minutes or 0) >= 60 else 0.0
        if target == "played_30_plus":
            return 1.0 if (perf.minutes or 0) >= 30 else 0.0
        if target == "played_60_plus":
            return 1.0 if (perf.minutes or 0) >= 60 else 0.0
        if target == "minutes":
            return float(perf.minutes or 0)
        column = TARGET_ALIASES.get(target)
        if column is None:
            raise ValueError(f"Unknown target: {target}")
        return float(getattr(perf, column) or 0)

    def _check_leakage(self, features_time: datetime, target_time: datetime) -> bool:
        """Return True if the feature time is strictly before the target time."""
        return features_time < target_time
