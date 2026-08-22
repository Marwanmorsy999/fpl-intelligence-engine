"""Tests for entity resolution across providers.

Tests that players and teams from different providers can be properly
resolved to the same canonical entity, and that duplicate names are handled.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import Player, PlayerExternalId, Team, TeamExternalId


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


class TestTeamResolution:
    """Test that the same team from different providers resolves to one canonical entity."""

    def test_same_team_two_providers(self, db_session: Session) -> None:
        """Two providers with different IDs for the same team should map to one canonical team."""
        # Create canonical team
        team = Team(name="Arsenal", short_name="ARS")
        db_session.add(team)
        db_session.flush()

        # Add external IDs from two providers
        db_session.add(
            TeamExternalId(team_id=team.id, provider="provider_a", provider_team_id="A_1")
        )
        db_session.add(
            TeamExternalId(team_id=team.id, provider="provider_b", provider_team_id="B_100")
        )
        db_session.commit()

        # Resolve from provider A
        ext_a = (
            db_session.query(TeamExternalId)
            .filter(
                TeamExternalId.provider == "provider_a",
                TeamExternalId.provider_team_id == "A_1",
            )
            .first()
        )
        assert ext_a is not None
        assert ext_a.team_id == team.id
        assert ext_a.team.name == "Arsenal"

        # Resolve from provider B
        ext_b = (
            db_session.query(TeamExternalId)
            .filter(
                TeamExternalId.provider == "provider_b",
                TeamExternalId.provider_team_id == "B_100",
            )
            .first()
        )
        assert ext_b is not None
        assert ext_b.team_id == team.id
        assert ext_b.team.name == "Arsenal"

        # Both should resolve to the same canonical team
        assert ext_a.team_id == ext_b.team_id


class TestPlayerResolution:
    """Test that the same player from different providers resolves to one canonical entity."""

    def test_same_player_two_providers(self, db_session: Session) -> None:
        """Two providers with different IDs for the same player resolve to one entity."""
        player = Player(
            first_name="Mohamed",
            second_name="Salah",
            web_name="M. Salah",
            position_code=3,
        )
        db_session.add(player)
        db_session.flush()

        db_session.add(
            PlayerExternalId(player_id=player.id, provider="provider_a", provider_player_id="P_10")
        )
        db_session.add(
            PlayerExternalId(player_id=player.id, provider="provider_b", provider_player_id="B_50")
        )
        db_session.commit()

        ext_a = (
            db_session.query(PlayerExternalId)
            .filter(
                PlayerExternalId.provider == "provider_a",
                PlayerExternalId.provider_player_id == "P_10",
            )
            .first()
        )
        assert ext_a is not None
        assert ext_a.player_id == player.id
        assert ext_a.player.web_name == "M. Salah"

        ext_b = (
            db_session.query(PlayerExternalId)
            .filter(
                PlayerExternalId.provider == "provider_b",
                PlayerExternalId.provider_player_id == "B_50",
            )
            .first()
        )
        assert ext_b is not None
        assert ext_b.player_id == player.id

        assert ext_a.player_id == ext_b.player_id

    def test_duplicate_player_names(self, db_session: Session) -> None:
        """Two players with identical names should be distinct canonical entities."""
        player1 = Player(
            first_name="James",
            second_name="Wilson",
            web_name="J. Wilson",
            position_code=4,
        )
        db_session.add(player1)
        db_session.flush()

        player2 = Player(
            first_name="James",
            second_name="Wilson",
            web_name="J. Wilson",
            position_code=4,
        )
        db_session.add(player2)
        db_session.flush()

        db_session.add(
            PlayerExternalId(player_id=player1.id, provider="fpl", provider_player_id="100")
        )
        db_session.add(
            PlayerExternalId(player_id=player2.id, provider="fpl", provider_player_id="200")
        )
        db_session.commit()

        # Both players exist with different canonical IDs
        assert player1.id != player2.id

        # Each should resolve via its own external ID
        ext1 = (
            db_session.query(PlayerExternalId)
            .filter(
                PlayerExternalId.provider_player_id == "100",
            )
            .first()
        )
        ext2 = (
            db_session.query(PlayerExternalId)
            .filter(
                PlayerExternalId.provider_player_id == "200",
            )
            .first()
        )
        assert ext1 is not None
        assert ext2 is not None
        assert ext1.player_id != ext2.player_id

    def test_player_transfer(self, db_session: Session) -> None:
        """Player transfer between teams should be representable via external IDs."""
        from fpl_intelligence.db.models import PlayerTeamMembership, Season

        team_a = Team(name="Liverpool", short_name="LIV")
        team_b = Team(name="Chelsea", short_name="CHE")
        season = Season(code="2024-25", display_name="2024/25")
        db_session.add_all([team_a, team_b, season])
        db_session.flush()

        player = Player(
            first_name="Mohamed",
            second_name="Salah",
            web_name="M. Salah",
            position_code=3,
        )
        db_session.add(player)
        db_session.flush()

        # Player at team A (first half of season)
        db_session.add(
            PlayerTeamMembership(
                player_id=player.id,
                team_id=team_a.id,
                season_id=season.id,
                valid_from=datetime(2024, 8, 1, tzinfo=UTC),
                valid_to=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )

        # Player at team B (second half of season)
        db_session.add(
            PlayerTeamMembership(
                player_id=player.id,
                team_id=team_b.id,
                season_id=season.id,
                valid_from=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        db_session.commit()

        memberships = (
            db_session.query(PlayerTeamMembership)
            .filter(
                PlayerTeamMembership.player_id == player.id,
            )
            .all()
        )
        assert len(memberships) == 2
        assert memberships[0].team_id == team_a.id
        assert memberships[1].team_id == team_b.id
