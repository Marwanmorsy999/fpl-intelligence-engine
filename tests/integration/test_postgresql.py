"""PostgreSQL integration tests for the FPL Intelligence Engine.

These tests verify that the Phase 3 feature store and backtesting
components work correctly with a real PostgreSQL database, including
temporal query enforcement, feature computation, and backtest execution.

**These tests are destructive.** The ``pg_engine`` fixture drops and recreates
the entire ``public`` schema, so it must never be pointed at a real database.
See the "Disposable test database" section below for the two guards that
enforce this.

Run with: pytest tests/integration/test_postgresql.py
Override the server (not the database name) with ``POSTGRES_URL``.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.backtesting.models import BacktestConfig
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import (
    FPLSnapshot,
    Gameweek,
    Player,
    PlayerExternalId,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
    Season,
    Team,
    TeamExternalId,
)
from fpl_intelligence.features.calculators.market_features import MarketFeaturesCalculator
from fpl_intelligence.features.calculators.player_form import PlayerFormCalculator
from fpl_intelligence.features.models import FeatureDefinition, FeatureSnapshot
from fpl_intelligence.features.registry import FeatureRegistry
from fpl_intelligence.features.temporal import (
    InformationAccessPolicy,
    TemporalQueryBuilder,
    is_record_available,
)

# ---------------------------------------------------------------------------
# Disposable test database
# ---------------------------------------------------------------------------
# The deployment database is `fpl` (docker-compose.yml, alembic.ini). Running
# this module against it destroys the migrated schema, including
# `alembic_version` and the native `availabilitystatus` enum, which silently
# undoes `alembic upgrade head`. These tests therefore always run against a
# dedicated, disposable database, protected two independent ways:
#
#   1. The database name is forced to `fpl_intelligence_test`. `POSTGRES_URL`
#      only supplies the server and credentials; its database component is
#      replaced. Set `POSTGRES_TEST_URL` to choose a different test database.
#   2. `_assert_disposable()` refuses to hand out an engine unless the target
#      database name ends with `_test`, and hard-blocks known real names. It
#      runs at import time and again inside the destructive fixture, so an
#      operator cannot aim the DROP SCHEMA at `fpl` even by overriding env vars.
#
# A guard violation is a hard error, never a skip: silently skipping would let
# the isolation regress unnoticed.

#: Dedicated database for these destructive tests.
DEFAULT_TEST_DB = "fpl_intelligence_test"

#: Maintenance database used only to issue ``CREATE DATABASE``.
ADMIN_DB = "postgres"

#: Database names that must never be targeted by destructive DDL.
PROTECTED_DB_NAMES = frozenset({"fpl", "postgres", "template0", "template1"})

#: Test database names must match this and end with ``_test``.
_SAFE_DB_NAME = re.compile(r"^[A-Za-z0-9_]+$")

# Use the modern psycopg (v3) dialect. The legacy `postgresql://` scheme maps to
# psycopg2, which is not installed. The project's SQLAlchemy strategy uses
# `postgresql+psycopg://` (see `src/fpl_intelligence/config/settings.py` and
# `docker-compose.yml`).
DEFAULT_SERVER_URL = "postgresql+psycopg://fpl:fpl@localhost:5432/fpl"


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when destructive fixtures are aimed at a non-disposable database."""


def _resolve_test_url() -> URL:
    """Return the URL of the disposable test database.

    ``POSTGRES_TEST_URL`` is used verbatim when set. Otherwise the server and
    credentials come from ``POSTGRES_URL`` (or the local default) and the
    database name is *replaced* with :data:`DEFAULT_TEST_DB`, so pointing
    ``POSTGRES_URL`` at the deployment database cannot leak into these tests.
    """
    explicit = os.environ.get("POSTGRES_TEST_URL")
    if explicit:
        return make_url(explicit)
    server = make_url(os.environ.get("POSTGRES_URL") or DEFAULT_SERVER_URL)
    return server.set(database=DEFAULT_TEST_DB)


def _assert_disposable(url: URL) -> None:
    """Raise unless *url* names a database that is safe to drop and recreate."""
    name = url.database
    if not name:
        raise UnsafeTestDatabaseError(
            "Refusing to run destructive PostgreSQL tests: no database name in URL."
        )
    if name in PROTECTED_DB_NAMES:
        raise UnsafeTestDatabaseError(
            f"Refusing to run destructive PostgreSQL tests against protected "
            f"database {name!r}. These tests DROP SCHEMA public CASCADE. Use a "
            f"disposable database such as {DEFAULT_TEST_DB!r}."
        )
    if not name.endswith("_test"):
        raise UnsafeTestDatabaseError(
            f"Refusing to run destructive PostgreSQL tests against {name!r}: the "
            f"test database name must end with '_test'."
        )
    if not _SAFE_DB_NAME.match(name):
        raise UnsafeTestDatabaseError(
            f"Unsafe test database identifier {name!r}; expected [A-Za-z0-9_]+."
        )


def _ensure_database(url: URL) -> None:
    """Create the disposable test database if it does not exist yet."""
    _assert_disposable(url)
    admin = create_engine(url.set(database=ADMIN_DB), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            ).scalar()
            if not exists:
                # Identifier already validated against _SAFE_DB_NAME above;
                # CREATE DATABASE cannot take a bind parameter.
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        admin.dispose()


TEST_URL = _resolve_test_url()

#: Backwards-compatible alias. Always the disposable test database, never `fpl`.
POSTGRES_URL = TEST_URL.render_as_string(hide_password=False)

# Fail loudly on a guard violation; skip only when PostgreSQL is unreachable.
_assert_disposable(TEST_URL)
try:
    _ensure_database(TEST_URL)
    _probe = create_engine(TEST_URL)
    with _probe.connect() as _conn:
        _conn.execute(text("SELECT 1"))
    _probe.dispose()
except UnsafeTestDatabaseError:
    raise
except Exception as exc:  # pragma: no cover - depends on local environment
    pytest.skip(
        f"PostgreSQL not available for {TEST_URL.database!r}: {exc}",
        allow_module_level=True,
    )


@pytest.fixture
def pg_engine():
    """Create an engine on the disposable test database.

    SQLite integration tests pass with `Base.metadata.drop_all(engine)` because
    SQLite does not enforce foreign keys by default. On PostgreSQL, tables
    that reference other tables through FK constraints (e.g. the migrated
    ``match_predictions`` table referencing ``fixtures``) cannot be dropped by
    ``drop_all`` if they are not present in the local ``Base.metadata``.

    We therefore tear down by recreating the public schema with CASCADE, which
    is deterministic and engine-correct on PostgreSQL. The guard is re-checked
    here so the destructive DDL can never reach a non-disposable database.
    """
    _assert_disposable(TEST_URL)
    engine = create_engine(TEST_URL)
    # Drop any leftover schema so the test starts from a clean slate regardless
    # of whether Alembic migrations have populated it beforehand.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(engine)
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture
def pg_session(pg_engine) -> Session:
    """Create a PostgreSQL session for testing."""
    SessionLocal = sessionmaker(bind=pg_engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def pg_populated_db(pg_session: Session) -> Session:
    """Create a PostgreSQL database with test data."""
    db = pg_session

    season = Season(
        code="2025-26",
        display_name="2025/26",
        start_date=datetime(2025, 8, 1, tzinfo=UTC),
        end_date=datetime(2026, 5, 31, tzinfo=UTC),
        competition="Premier League",
    )
    db.add(season)
    db.flush()

    teams = []
    for i, name in enumerate(["Arsenal", "Chelsea", "Liverpool", "Man City"]):
        team = Team(name=name, short_name=name[:3].upper())
        db.add(team)
        db.flush()
        db.add(TeamExternalId(team_id=team.id, provider="mock", provider_team_id=f"mock_team_{i}"))
        teams.append(team)
    db.flush()

    players = []
    for i, name in enumerate(["Alisson", "Saliba", "Odegaard", "Haaland"]):
        player = Player(
            first_name=name, second_name="Test", web_name=name.lower(), position_code=i + 1
        )
        db.add(player)
        db.flush()
        db.add(
            PlayerExternalId(
                player_id=player.id, provider="mock", provider_player_id=f"mock_player_{i}"
            )
        )
        db.add(
            PlayerTeamMembership(
                player_id=player.id,
                team_id=teams[i % 4].id,
                season_id=season.id,
                valid_from=season.start_date,
            )
        )
        players.append(player)
    db.flush()

    for gw_num in range(1, 4):
        gw = Gameweek(
            season_id=season.id,
            provider_event_id=gw_num,
            name=f"Gameweek {gw_num}",
            deadline_time=datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=(gw_num - 1) * 7),
            start_time=datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=(gw_num - 1) * 7, hours=2),
            end_time=datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=(gw_num - 1) * 7 + 2),
            status="scheduled",
        )
        db.add(gw)
    db.flush()

    for gw_num in range(1, 3):
        gw = db.scalar(select(Gameweek).where(Gameweek.provider_event_id == gw_num))
        gw_time = datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=(gw_num - 1) * 7)
        for player_idx, player in enumerate(players):
            db.add(
                PlayerGameweekPerformance(
                    player_id=player.id,
                    gameweek_id=gw.id,
                    season_id=season.id,
                    team_id=teams[player_idx % 4].id,
                    total_points=10 + player_idx * 2,
                    minutes=90,
                    goals_scored=1 if player_idx == 3 else 0,
                    assists=1 if player_idx == 2 else 0,
                    ingested_at=gw_time + timedelta(hours=2),
                    available_at=gw_time + timedelta(hours=2),
                )
            )
    db.flush()

    for gw_num in range(1, 3):
        gw = db.scalar(select(Gameweek).where(Gameweek.provider_event_id == gw_num))
        snap_time = datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=(gw_num - 1) * 7, hours=-1)
        for player_idx, player in enumerate(players):
            db.add(
                FPLSnapshot(
                    player_id=player.id,
                    season_id=season.id,
                    gameweek_id=gw.id,
                    event_time=snap_time,
                    published_at=snap_time,
                    available_at=snap_time,
                    ingested_at=snap_time + timedelta(minutes=5),
                    source_last_modified_at=snap_time,
                    price=8.0 - player_idx * 0.5,
                    selected_by_percent=20.0 - player_idx * 3,
                    total_points=10 + player_idx * 2,
                    form=3.5 + player_idx * 0.5,
                    ep_this=3.0,
                    ep_next=3.5,
                )
            )
    db.commit()
    return db


class TestPostgreSQLTemporalQueries:
    """Test temporal query enforcement on PostgreSQL."""

    def test_strict_policy_excludes_future_data(self, pg_session, pg_populated_db) -> None:
        """Test that STRICT_REPRODUCIBILITY excludes future data on PostgreSQL."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)

        # Add a future snapshot
        future = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            price=50.0,
        )
        pg_session.add(future)
        pg_session.commit()

        builder = TemporalQueryBuilder(
            pg_session, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )
        results = builder.query_with_filter(FPLSnapshot)
        for r in results:
            assert r.available_at <= cutoff
            assert r.ingested_at <= cutoff

    def test_public_policy_includes_future_ingested(self, pg_session, pg_populated_db) -> None:
        """Test that PUBLIC_AVAILABILITY includes data ingested after cutoff."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)

        snapshot = FPLSnapshot(
            player_id=1,
            season_id=1,
            event_time=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            available_at=datetime(2025, 8, 10, 12, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2025, 8, 20, 12, 0, 0, tzinfo=UTC),
            price=15.0,
        )
        pg_session.add(snapshot)
        pg_session.commit()

        assert is_record_available(snapshot, cutoff, InformationAccessPolicy.PUBLIC_AVAILABILITY)
        assert not is_record_available(
            snapshot, cutoff, InformationAccessPolicy.STRICT_REPRODUCIBILITY
        )


class TestPostgreSQLFeatureStore:
    """Test feature store operations on PostgreSQL."""

    def test_feature_definition_persistence(self, pg_session) -> None:
        """Test that feature definitions are persisted to PostgreSQL."""
        definition = FeatureDefinition(
            feature_name="test_pg_feature",
            description="Test feature for PostgreSQL",
            data_type="json",
            entity_type="player",
            version="1.0.0",
            calculation_method="TestCalculator",
        )
        pg_session.add(definition)
        pg_session.commit()

        from sqlalchemy import select

        result = pg_session.scalar(
            select(FeatureDefinition).where(FeatureDefinition.feature_name == "test_pg_feature")
        )
        assert result is not None
        assert result.version == "1.0.0"

    def test_feature_snapshot_persistence(self, pg_session) -> None:
        """Test that feature snapshots are persisted to PostgreSQL."""
        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        snapshot = FeatureSnapshot(
            entity_id=1,
            feature_name="test_pg_feature",
            feature_version="1.0.0",
            cutoff_time=cutoff,
            value={"test": 1.0},
            is_missing=False,
            completeness_score=1.0,
            source_count=5,
        )
        pg_session.add(snapshot)
        pg_session.commit()

        from sqlalchemy import select

        result = pg_session.scalar(
            select(FeatureSnapshot).where(
                FeatureSnapshot.entity_id == 1,
                FeatureSnapshot.feature_name == "test_pg_feature",
            )
        )
        assert result is not None
        assert result.value["test"] == 1.0

    def test_feature_registry_on_postgresql(self, pg_session, pg_populated_db) -> None:
        """Test feature registry operations on PostgreSQL."""
        registry = FeatureRegistry(pg_session)
        registry.register(PlayerFormCalculator())
        registry.register(MarketFeaturesCalculator())

        cutoff = datetime(2025, 8, 15, 12, 0, 0, tzinfo=UTC)
        result = registry.compute("player_form", 1, cutoff)
        assert "value" in result
        assert "is_missing" in result

        # Verify snapshot was persisted
        from sqlalchemy import select

        snapshots = pg_session.scalars(
            select(FeatureSnapshot).where(
                FeatureSnapshot.entity_id == 1,
                FeatureSnapshot.feature_name == "player_form",
            )
        ).all()
        assert len(snapshots) > 0


class TestPostgreSQLBacktestModels:
    """Test backtest model persistence on PostgreSQL."""

    def test_backtest_config_persistence(self, pg_session) -> None:
        """Test that backtest configs are persisted to PostgreSQL."""
        config = BacktestConfig(
            season="2025-26",
            start_gameweek=1,
            end_gameweek=3,
            random_seed=42,
        )
        pg_session.add(config)
        pg_session.commit()

        from sqlalchemy import select

        result = pg_session.scalar(select(BacktestConfig).where(BacktestConfig.season == "2025-26"))
        assert result is not None
        assert result.start_gameweek == 1
        assert result.end_gameweek == 3
