"""Availability policies for the backtesting engine.

These policies determine which players and fixtures are available
for prediction at a given decision cutoff. They enforce the no-look-ahead
rule by filtering out entities whose data was not yet available.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import (
    Fixture,
    Player,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
)
from fpl_intelligence.features.temporal import (
    DEFAULT_POLICY,
    InformationAccessPolicy,
    apply_policy,
)


class AvailabilityPolicy:
    """Determines which entities are available at a given cutoff.

    A player is considered available for prediction if:
        1. They have a team membership valid at the cutoff time.
        2. They have at least one performance record before the cutoff.
        3. They are not ruled out (if injury data is available).

    A fixture is considered available if:
        1. It was scheduled/known before the cutoff.
        2. Its kickoff time is after the cutoff (i.e., it hasn't happened yet).
    """

    def __init__(
        self,
        policy: InformationAccessPolicy = DEFAULT_POLICY,
    ) -> None:
        self._policy = policy

    def is_available(
        self,
        entity: Any,
        cutoff_time: datetime,
    ) -> bool:
        """Check if a single entity is available at the cutoff.

        Args:
            entity: A model instance (Player, Fixture, etc.).
            cutoff_time: The historical decision cutoff.

        Returns:
            True if the entity is available.
        """
        from fpl_intelligence.features.temporal import is_record_available

        return is_record_available(entity, cutoff_time, self._policy)

    def filter_available(
        self,
        entities: list[Any],
        cutoff_time: datetime,
    ) -> list[Any]:
        """Filter a list of entities to only those available at the cutoff.

        Args:
            entities: List of model instances.
            cutoff_time: The historical decision cutoff.

        Returns:
            List of available entities.
        """
        return [e for e in entities if self.is_available(e, cutoff_time)]

    def get_available_players(
        self,
        db: Session,
        cutoff_time: datetime,
        team_id: int | None = None,
    ) -> list[Player]:
        """Get all players available at the cutoff.

        Args:
            db: Database session.
            cutoff_time: The historical decision cutoff.
            team_id: Optional team ID to filter by.

        Returns:
            List of available Player instances.
        """
        stmt = select(Player)
        if team_id is not None:
            stmt = stmt.where(Player.id.in_(
                select(PlayerTeamMembership.player_id)
                .where(PlayerTeamMembership.team_id == team_id)
                .where(PlayerTeamMembership.valid_from <= cutoff_time)
                .where(
                    (PlayerTeamMembership.valid_to.is_(None))
                    | (PlayerTeamMembership.valid_to > cutoff_time)
                )
            ))

        players = list(db.execute(stmt).scalars().all())
        return self.filter_available(players, cutoff_time)

    def get_available_fixtures(
        self,
        db: Session,
        cutoff_time: datetime,
        gameweek_id: int | None = None,
    ) -> list[Fixture]:
        """Get all fixtures available at the cutoff.

        A fixture is available if its kickoff is after the cutoff
        (i.e., the match hasn't happened yet from the decision perspective).

        Args:
            db: Database session.
            cutoff_time: The historical decision cutoff.
            gameweek_id: Optional gameweek ID to filter by.

        Returns:
            List of available Fixture instances.
        """
        stmt = select(Fixture).where(
            Fixture.kickoff_time > cutoff_time
        )
        if gameweek_id is not None:
            stmt = stmt.where(Fixture.gameweek_id == gameweek_id)

        fixtures = list(db.execute(stmt).scalars().all())
        return self.filter_available(fixtures, cutoff_time)

    def get_player_performance_count(
        self,
        db: Session,
        player_id: int,
        cutoff_time: datetime,
    ) -> int:
        """Count how many performance records a player has before the cutoff.

        Args:
            db: Database session.
            player_id: Player ID.
            cutoff_time: The historical decision cutoff.

        Returns:
            Number of performance records.
        """
        stmt = select(PlayerGameweekPerformance).where(
            PlayerGameweekPerformance.player_id == player_id,
        )
        try:
            condition = apply_policy(
                PlayerGameweekPerformance, self._policy, cutoff_time
            )
            stmt = stmt.where(condition)
        except ValueError:
            pass

        return len(list(db.execute(stmt).scalars().all()))
