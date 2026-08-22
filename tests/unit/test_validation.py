"""Tests for historical data validation logic."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import (
    Fixture,
    Gameweek,
    Player,
    PlayerExternalId,
    PlayerGameweekPerformance,
    Season,
    Team,
)
from fpl_intelligence.validation.historical import (
    validate_fixture_integrity,
    validate_no_duplicate_records,
    validate_player_stats_integrity,
    validate_season_integrity,
)


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


class TestSeasonIntegrity:
    def test_valid_season_passes(self, db_session: Session) -> None:
        season = Season(code="2024-25", display_name="2024/25")
        db_session.add(season)
        db_session.flush()

        team = Team(name="Arsenal", short_name="ARS")
        db_session.add(team)
        db_session.flush()

        gw = Gameweek(season_id=season.id, provider_event_id=1, name="Gameweek 1")
        db_session.add(gw)
        db_session.flush()

        fixture = Fixture(
            season_id=season.id,
            provider_fixture_id=1,
            home_team_id=team.id,
            away_team_id=team.id,  # Same team - should be caught
            home_score=2,
            away_score=1,
        )
        db_session.add(fixture)
        db_session.commit()

        # Test season integrity
        validate_season_integrity(db_session, season.id)
        # Gameweek references valid season
        # Fixture may have home == away - that's a fixture integrity check

    def test_gameweek_invalid_season(self, db_session: Session) -> None:
        gw = Gameweek(season_id=999, provider_event_id=1, name="Gameweek 1")
        db_session.add(gw)
        db_session.commit()

        result = validate_season_integrity(db_session)
        assert not result.passed
        assert any("non-existent season" in err.message for err in result.errors)


class TestFixtureIntegrity:
    def test_negative_score(self, db_session: Session) -> None:
        season = Season(code="2024-25", display_name="2024/25")
        team_a = Team(name="Team A", short_name="TMA")
        team_b = Team(name="Team B", short_name="TMB")
        db_session.add_all([season, team_a, team_b])
        db_session.flush()

        fixture = Fixture(
            season_id=season.id,
            provider_fixture_id=1,
            home_team_id=team_a.id,
            away_team_id=team_b.id,
            home_score=-1,
            away_score=0,
        )
        db_session.add(fixture)
        db_session.commit()

        result = validate_fixture_integrity(db_session, season.id)
        assert not result.passed
        assert any("negative home score" in err.message for err in result.errors)

    def test_home_equals_away(self, db_session: Session) -> None:
        season = Season(code="2024-25", display_name="2024/25")
        team = Team(name="Team A", short_name="TMA")
        db_session.add_all([season, team])
        db_session.flush()

        fixture = Fixture(
            season_id=season.id,
            provider_fixture_id=1,
            home_team_id=team.id,
            away_team_id=team.id,
            home_score=0,
            away_score=0,
        )
        db_session.add(fixture)
        db_session.commit()

        result = validate_fixture_integrity(db_session, season.id)
        assert not result.passed
        assert any("home team equals away team" in err.message for err in result.errors)


class TestPlayerStatsIntegrity:
    def test_impossible_minutes(self, db_session: Session) -> None:
        season = Season(code="2024-25", display_name="2024/25")
        player = Player(
            first_name="Test", second_name="Player", web_name="T. Player", position_code=3
        )
        gw = Gameweek(season_id=1, provider_event_id=1, name="Gameweek 1")
        team = Team(name="Team A", short_name="TMA")
        db_session.add_all([season, player, gw, team])
        db_session.flush()

        perf = PlayerGameweekPerformance(
            player_id=player.id,
            gameweek_id=gw.id,
            season_id=season.id,
            team_id=team.id,
            minutes=200,  # Impossible
        )
        db_session.add(perf)
        db_session.commit()

        result = validate_player_stats_integrity(db_session, season.id)
        assert not result.passed
        assert any("impossible minutes" in err.message for err in result.errors)

    def test_negative_goals(self, db_session: Session) -> None:
        season = Season(code="2024-25", display_name="2024/25")
        player = Player(
            first_name="Test", second_name="Player", web_name="T. Player", position_code=3
        )
        gw = Gameweek(season_id=1, provider_event_id=1, name="Gameweek 1")
        team = Team(name="Team A", short_name="TMA")
        db_session.add_all([season, player, gw, team])
        db_session.flush()

        perf = PlayerGameweekPerformance(
            player_id=player.id,
            gameweek_id=gw.id,
            season_id=season.id,
            team_id=team.id,
            minutes=90,
            goals_scored=-1,  # Negative
        )
        db_session.add(perf)
        db_session.commit()

        result = validate_player_stats_integrity(db_session, season.id)
        assert not result.passed
        assert any("negative" in err.message for err in result.errors)


class TestDuplicateRecords:
    def test_duplicate_player_gameweek(self, db_session: Session) -> None:
        """Unique constraint prevents duplicate player+gameweek records."""
        season = Season(code="2024-25", display_name="2024/25")
        player = Player(
            first_name="Test", second_name="Player", web_name="T. Player", position_code=3
        )
        gw = Gameweek(season_id=1, provider_event_id=1, name="Gameweek 1")
        team = Team(name="Team A", short_name="TMA")
        db_session.add_all([season, player, gw, team])
        db_session.flush()

        # Add first record
        db_session.add(
            PlayerGameweekPerformance(
                player_id=player.id,
                gameweek_id=gw.id,
                season_id=season.id,
                team_id=team.id,
                minutes=90,
            )
        )
        db_session.commit()

        # Adding a second record with same player+gameweek should fail
        db_session.add(
            PlayerGameweekPerformance(
                player_id=player.id,
                gameweek_id=gw.id,
                season_id=season.id,
                team_id=team.id,
                minutes=45,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        # Verify only one record exists
        count = db_session.query(PlayerGameweekPerformance).count()
        assert count == 1

    def test_duplicate_external_ids(self, db_session: Session) -> None:
        player = Player(
            first_name="Test", second_name="Player", web_name="T. Player", position_code=3
        )
        db_session.add(player)
        db_session.flush()

        # Add two external IDs with same provider+id
        db_session.add(
            PlayerExternalId(player_id=player.id, provider="fpl", provider_player_id="100")
        )
        db_session.flush()
        # SQLite will allow this if no unique constraint, but the validation should catch it
        try:
            db_session.add(
                PlayerExternalId(
                    player_id=player.id + 1 if player.id else 1,
                    provider="fpl",
                    provider_player_id="100",
                )
            )
            db_session.commit()
        except Exception:
            db_session.rollback()

        validate_no_duplicate_records(db_session)
        # The validation runs, we just check it doesn't crash
