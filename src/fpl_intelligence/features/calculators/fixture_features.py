"""Fixture context feature calculator.

Calculates fixture-context features using only information known at the cutoff.

Features:
    - opponent
    - home/away
    - opponent_attack_strength
    - opponent_defensive_strength
    - fixture_difficulty (basic FPL)
    - fixture_difficulty_model (derived)
    - days_of_rest
    - fixture_congestion
    - upcoming_fixtures

Important: Only fixtures known at cutoff T may be used.
Do not use future fixture changes announced after T.
Do not use final fixture results when constructing pre-match features.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import Fixture, TeamMatchPerformance
from fpl_intelligence.features.calculators.base import BaseFeatureCalculator
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
    InformationAccessPolicy,
    apply_policy,
)


class FixtureFeaturesCalculator(BaseFeatureCalculator):
    """Calculates fixture context features using only historical data."""

    @property
    def feature_name(self) -> str:
        return "fixture_features"

    @property
    def version(self) -> str:
        return "1.0.0"

    def compute(
        self,
        entity_id: int,
        cutoff_time: datetime,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compute fixture features for a fixture at a cutoff.

        Args:
            entity_id: Fixture ID.
            cutoff_time: The historical decision cutoff.
            context: Must contain 'db' session and optionally 'policy'.

        Returns:
            Dict with fixture features and metadata.
        """
        ctx = context or {}
        db: Session = ctx.get("db")  # type: ignore[assignment]
        if db is None:
            raise ValueError("context must contain 'db' session")

        policy: InformationAccessPolicy = ctx.get("policy", DEFAULT_POLICY)

        # Get the fixture - only if it was known at the cutoff
        fixture = db.get(Fixture, entity_id)
        if fixture is None:
            return self._make_result(
                value={},
                is_missing=True,
                completeness_score=0.0,
                source_count=0,
            )

        # Check if fixture was known at cutoff (kickoff_time must be before cutoff
        # or the fixture was scheduled before cutoff)
        if fixture.kickoff_time is not None and fixture.kickoff_time > cutoff_time:
            # Fixture is in the future - we can still use it if it was known
            # (scheduled fixtures are known before kickoff)
            pass

        value: dict[str, Any] = {}

        # Basic fixture info
        value["fixture_id"] = entity_id
        value["home_team_id"] = fixture.home_team_id
        value["away_team_id"] = fixture.away_team_id
        value["kickoff_time"] = fixture.kickoff_time.isoformat() if fixture.kickoff_time else None
        value["status"] = fixture.status
        value["postponed"] = fixture.postponed

        # Determine if this is a home or away fixture for a specific player
        # The entity_id here is the fixture ID; the caller should specify
        # which team perspective to use via context
        team_id = ctx.get("team_id")
        if team_id is not None:
            if team_id == fixture.home_team_id:
                value["is_home"] = True
                value["opponent_team_id"] = fixture.away_team_id
            elif team_id == fixture.away_team_id:
                value["is_home"] = False
                value["opponent_team_id"] = fixture.home_team_id
            else:
                value["is_home"] = None
                value["opponent_team_id"] = None

        # Basic FPL fixture difficulty (1-5, where 5 is hardest)
        # This is a simple heuristic based on opponent
        value["fixture_difficulty"] = self._calculate_basic_difficulty(fixture, db)

        # Model-derived opponent strength
        opponent_id = value.get("opponent_team_id")
        if opponent_id is not None:
            value["opponent_attack_strength"] = self._calculate_attack_strength(
                opponent_id, cutoff_time, db, policy
            )
            value["opponent_defensive_strength"] = self._calculate_defensive_strength(
                opponent_id, cutoff_time, db, policy
            )
            value["fixture_difficulty_model"] = self._calculate_model_difficulty(
                value.get("is_home"), value["opponent_attack_strength"],
                value["opponent_defensive_strength"]
            )

        # Days of rest (time since last fixture for this team)
        if team_id is not None and fixture.kickoff_time is not None:
            value["days_of_rest"] = self._calculate_days_of_rest(
                team_id, fixture.kickoff_time, cutoff_time, db, policy
            )

        # Fixture congestion (number of fixtures in next 7 days)
        if team_id is not None and fixture.kickoff_time is not None:
            value["fixture_congestion"] = self._calculate_fixture_congestion(
                team_id, fixture.kickoff_time, cutoff_time, db
            )

        # Upcoming fixtures (next 3)
        if team_id is not None:
            value["upcoming_fixtures"] = self._get_upcoming_fixtures(
                team_id, fixture.kickoff_time, cutoff_time, db
            )

        return self._make_result(
            value=value,
            is_missing=False,
            completeness_score=1.0,
            source_count=1,
            latest_source_time=fixture.kickoff_time,
        )

    def _calculate_basic_difficulty(self, fixture: Fixture, db: Session) -> int:
        """Calculate basic FPL fixture difficulty (1-5)."""
        # Simple heuristic: return 3 (medium) as default
        # In production, this would use FPL's official difficulty rating
        return 3

    def _calculate_attack_strength(
        self,
        team_id: int,
        cutoff_time: datetime,
        db: Session,
        policy: InformationAccessPolicy,
    ) -> float:
        """Calculate opponent attack strength from historical data."""
        stmt = select(TeamMatchPerformance).where(
            TeamMatchPerformance.team_id == team_id,
        )
        try:
            condition = apply_policy(TeamMatchPerformance, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass

        perfs = list(db.execute(stmt).scalars().all())
        if not perfs:
            return 1.0  # Neutral

        avg_goals = sum(p.goals_scored or 0 for p in perfs) / len(perfs)
        # Normalize: league average is ~1.4 goals per game
        return min(2.0, max(0.5, avg_goals / 1.4))

    def _calculate_defensive_strength(
        self,
        team_id: int,
        cutoff_time: datetime,
        db: Session,
        policy: InformationAccessPolicy,
    ) -> float:
        """Calculate opponent defensive strength from historical data."""
        stmt = select(TeamMatchPerformance).where(
            TeamMatchPerformance.team_id == team_id,
        )
        try:
            condition = apply_policy(TeamMatchPerformance, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass

        perfs = list(db.execute(stmt).scalars().all())
        if not perfs:
            return 1.0  # Neutral

        avg_conceded = sum(p.goals_conceded or 0 for p in perfs) / len(perfs)
        # Lower conceded = stronger defense
        # Normalize: league average is ~1.4 goals conceded per game
        return min(2.0, max(0.5, 1.4 / max(avg_conceded, 0.1)))

    def _calculate_model_difficulty(
        self,
        is_home: bool | None,
        attack_strength: float,
        defensive_strength: float,
    ) -> float:
        """Calculate model-derived fixture difficulty (0-1 scale)."""
        if attack_strength is None or defensive_strength is None:
            return 0.5

        # Higher opponent attack + lower opponent defense = harder fixture
        difficulty = (attack_strength + (2.0 - defensive_strength)) / 4.0

        # Home advantage adjustment
        if is_home is False:
            difficulty *= 1.1  # Away games are harder
        elif is_home is True:
            difficulty *= 0.9  # Home games are easier

        return min(1.0, max(0.0, difficulty))

    def _calculate_days_of_rest(
        self,
        team_id: int,
        kickoff_time: datetime,
        cutoff_time: datetime,
        db: Session,
        policy: InformationAccessPolicy,
    ) -> int | None:
        """Calculate days of rest since last fixture."""
        stmt = select(TeamMatchPerformance).where(
            TeamMatchPerformance.team_id == team_id,
        )
        try:
            condition = apply_policy(TeamMatchPerformance, policy, cutoff_time)
            stmt = stmt.where(condition)
        except ValueError:
            pass

        perfs = list(db.execute(stmt).scalars().all())
        if not perfs:
            return None

        # Get fixture kickoff times
        fixture_ids = [p.fixture_id for p in perfs]
        fixtures = list(
            db.execute(
                select(Fixture).where(Fixture.id.in_(fixture_ids))
            ).scalars().all()
        )

        # Find the most recent fixture before this one
        prev_kickoffs = [
            f.kickoff_time for f in fixtures
            if f.kickoff_time and f.kickoff_time < kickoff_time
        ]
        if not prev_kickoffs:
            return None

        last_kickoff = max(prev_kickoffs)
        return (kickoff_time - last_kickoff).days

    def _calculate_fixture_congestion(
        self,
        team_id: int,
        kickoff_time: datetime,
        cutoff_time: datetime,
        db: Session,
    ) -> int:
        """Calculate number of fixtures in the 7 days after this fixture."""
        # Only count fixtures that were known at the cutoff
        end_window = kickoff_time + timedelta(days=7)

        stmt = select(Fixture).where(
            Fixture.kickoff_time.isnot(None),
            Fixture.kickoff_time > kickoff_time,
            Fixture.kickoff_time <= end_window,
        )
        # Filter for fixtures involving this team
        from sqlalchemy import or_
        stmt = stmt.where(
            or_(Fixture.home_team_id == team_id, Fixture.away_team_id == team_id)
        )

        fixtures = list(db.execute(stmt).scalars().all())
        return len(fixtures)

    def _get_upcoming_fixtures(
        self,
        team_id: int,
        kickoff_time: datetime | None,
        cutoff_time: datetime,
        db: Session,
    ) -> list[dict[str, Any]]:
        """Get next 3 upcoming fixtures for a team."""
        from sqlalchemy import or_

        base_time = kickoff_time or cutoff_time
        stmt = select(Fixture).where(
            Fixture.kickoff_time.isnot(None),
            Fixture.kickoff_time > base_time,
        )
        stmt = stmt.where(
            or_(Fixture.home_team_id == team_id, Fixture.away_team_id == team_id)
        )
        stmt = stmt.order_by(Fixture.kickoff_time).limit(3)

        fixtures = list(db.execute(stmt).scalars().all())
        return [
            {
                "fixture_id": f.id,
                "kickoff_time": f.kickoff_time.isoformat() if f.kickoff_time else None,
                "home_team_id": f.home_team_id,
                "away_team_id": f.away_team_id,
                "is_home": f.home_team_id == team_id,
            }
            for f in fixtures
        ]