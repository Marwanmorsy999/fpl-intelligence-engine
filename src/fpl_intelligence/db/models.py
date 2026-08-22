"""SQLAlchemy ORM models for the FPL Intelligence Engine.

All database models are defined here. The schema is managed through Alembic
migrations. Models should always be in sync with the latest migration.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fpl_intelligence.db.base import Base


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    base_url: Mapped[str | None] = mapped_column(String(500))


class Season(Base):
    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(50), nullable=False)
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    competition: Mapped[str | None] = mapped_column(String(100), default="Premier League")


class TeamExternalId(Base):
    """Maps external provider team IDs to internal canonical team IDs.

    Supports multiple providers each having their own identifier for the same team.
    """
    __tablename__ = "team_external_ids"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_team_id: Mapped[str] = mapped_column(String(100), nullable=False)

    team: Mapped["Team"] = relationship(back_populates="external_ids")

    __table_args__ = (
        UniqueConstraint("provider", "provider_team_id", name="uq_team_external_id"),
    )


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(10))

    external_ids: Mapped[list[TeamExternalId]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )


class PlayerExternalId(Base):
    """Maps external provider player IDs to internal canonical player IDs.

    Supports multiple providers each having their own identifier for the same player.
    Enables cross-provider entity resolution without relying on player names.
    """
    __tablename__ = "player_external_ids"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_player_id: Mapped[str] = mapped_column(String(100), nullable=False)

    player: Mapped["Player"] = relationship(back_populates="external_ids")

    __table_args__ = (
        UniqueConstraint("provider", "provider_player_id", name="uq_player_external_id"),
    )


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    second_name: Mapped[str] = mapped_column(String(100), nullable=False)
    web_name: Mapped[str] = mapped_column(String(100), nullable=False)
    position_code: Mapped[int | None] = mapped_column(Integer)
    #: FPL element ``code`` — the numeric key the official Premier League CDN uses
    #: for player photo URLs (``resources.premierleague.com/photos/players/110x140/{code}.png``).
    fpl_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Official FPL element ID (the ``id`` on fantasy.premierleague.com). Entry
    #: picks reference players by THIS id, so squad imports must join on
    #: ``fpl_element_id`` — never on our internal auto-increment ``id``.
    fpl_element_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, unique=True, index=True
    )

    external_ids: Mapped[list[PlayerExternalId]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )


class PlayerTeamMembership(Base):
    """Records which team a player belonged to during a season, with temporal validity.

    Supports player transfers within a season by providing valid_from/valid_to.
    """
    __tablename__ = "player_team_memberships"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    player: Mapped["Player"] = relationship()
    team: Mapped["Team"] = relationship()
    season: Mapped["Season"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "player_id", "team_id", "season_id", "valid_from",
            name="uq_player_team_season",
        ),
    )


class Gameweek(Base):
    __tablename__ = "gameweeks"

    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    provider_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    deadline_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(30), default="scheduled")

    season: Mapped["Season"] = relationship()

    __table_args__ = (
        UniqueConstraint("season_id", "provider_event_id", name="uq_gameweek_season_event"),
    )


class Fixture(Base):
    __tablename__ = "fixtures"

    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    provider_fixture_id: Mapped[int] = mapped_column(Integer, nullable=False)
    gameweek_id: Mapped[int | None] = mapped_column(ForeignKey("gameweeks.id"), index=True)
    kickoff_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(30), default="scheduled")
    postponed: Mapped[bool] = mapped_column(default=False)

    season: Mapped["Season"] = relationship()
    gameweek: Mapped["Gameweek | None"] = relationship()

    __table_args__ = (
        UniqueConstraint("season_id", "provider_fixture_id", name="uq_fixture_season_provider"),
    )


class PlayerMatchPerformance(Base):
    """Per-match player statistics from the source provider.

    Includes temporal fields (ingested_at, available_at) to support
    historical backtesting with strict no-look-ahead enforcement.
    """
    __tablename__ = "player_match_performances"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), nullable=False, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)

    # Performance metrics
    minutes: Mapped[int | None] = mapped_column(Integer)
    goals_scored: Mapped[int | None] = mapped_column(Integer, default=0)
    assists: Mapped[int | None] = mapped_column(Integer, default=0)
    clean_sheets: Mapped[int | None] = mapped_column(Integer, default=0)
    goals_conceded: Mapped[int | None] = mapped_column(Integer, default=0)
    own_goals: Mapped[int | None] = mapped_column(Integer, default=0)
    penalties_saved: Mapped[int | None] = mapped_column(Integer, default=0)
    penalties_missed: Mapped[int | None] = mapped_column(Integer, default=0)
    yellow_cards: Mapped[int | None] = mapped_column(Integer, default=0)
    red_cards: Mapped[int | None] = mapped_column(Integer, default=0)
    saves: Mapped[int | None] = mapped_column(Integer, default=0)
    bonus: Mapped[int | None] = mapped_column(Integer, default=0)
    bps: Mapped[int | None] = mapped_column(Integer, default=0)
    influence: Mapped[float | None] = mapped_column(Float)
    creativity: Mapped[float | None] = mapped_column(Float)
    threat: Mapped[float | None] = mapped_column(Float)
    ict_index: Mapped[float | None] = mapped_column(Float)
    expected_goals: Mapped[float | None] = mapped_column(Float)
    expected_assists: Mapped[float | None] = mapped_column(Float)
    expected_goal_involvements: Mapped[float | None] = mapped_column(Float)
    expected_goals_conceded: Mapped[float | None] = mapped_column(Float)
    total_points: Mapped[int | None] = mapped_column(Integer, default=0)
    was_home: Mapped[bool | None] = mapped_column()
    kickoff_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Temporal fields for backtesting
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    player: Mapped["Player"] = relationship()
    fixture: Mapped["Fixture"] = relationship()
    season: Mapped["Season"] = relationship()
    team: Mapped["Team"] = relationship()

    __table_args__ = (
        UniqueConstraint("player_id", "fixture_id", name="uq_player_match"),
    )


class PlayerGameweekPerformance(Base):
    """Aggregated player performance for a Gameweek.

    Stores the canonical FPL-related data such as FPL points,
    price snapshot, and other Gameweek-level aggregates.

    Includes temporal fields (ingested_at, available_at) to support
    historical backtesting with strict no-look-ahead enforcement.
    """
    __tablename__ = "player_gameweek_performances"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    gameweek_id: Mapped[int] = mapped_column(ForeignKey("gameweeks.id"), nullable=False, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)

    # Performance metrics
    minutes: Mapped[int | None] = mapped_column(Integer, default=0)
    goals_scored: Mapped[int | None] = mapped_column(Integer, default=0)
    assists: Mapped[int | None] = mapped_column(Integer, default=0)
    clean_sheets: Mapped[int | None] = mapped_column(Integer, default=0)
    goals_conceded: Mapped[int | None] = mapped_column(Integer, default=0)
    own_goals: Mapped[int | None] = mapped_column(Integer, default=0)
    penalties_saved: Mapped[int | None] = mapped_column(Integer, default=0)
    penalties_missed: Mapped[int | None] = mapped_column(Integer, default=0)
    yellow_cards: Mapped[int | None] = mapped_column(Integer, default=0)
    red_cards: Mapped[int | None] = mapped_column(Integer, default=0)
    saves: Mapped[int | None] = mapped_column(Integer, default=0)
    bonus: Mapped[int | None] = mapped_column(Integer, default=0)
    bps: Mapped[int | None] = mapped_column(Integer, default=0)
    influence: Mapped[float | None] = mapped_column(Float)
    creativity: Mapped[float | None] = mapped_column(Float)
    threat: Mapped[float | None] = mapped_column(Float)
    ict_index: Mapped[float | None] = mapped_column(Float)
    expected_goals: Mapped[float | None] = mapped_column(Float)
    expected_assists: Mapped[float | None] = mapped_column(Float)
    expected_goal_involvements: Mapped[float | None] = mapped_column(Float)
    expected_goals_conceded: Mapped[float | None] = mapped_column(Float)

    # FPL points and pricing
    total_points: Mapped[int | None] = mapped_column(Integer, default=0)
    value: Mapped[int | None] = mapped_column(Integer)
    transfers_balance: Mapped[int | None] = mapped_column(Integer)
    selected: Mapped[int | None] = mapped_column(Integer)
    transfers_in: Mapped[int | None] = mapped_column(Integer, default=0)
    transfers_out: Mapped[int | None] = mapped_column(Integer, default=0)
    loaned_in: Mapped[int | None] = mapped_column(Integer, default=0)
    loaned_out: Mapped[int | None] = mapped_column(Integer, default=0)

    # Price snapshot at Gameweek
    price: Mapped[float | None] = mapped_column(Float)
    cost_change_event: Mapped[int | None] = mapped_column(Integer)
    cost_change_start: Mapped[int | None] = mapped_column(Integer)
    price_change: Mapped[float | None] = mapped_column(Float)
    price_start: Mapped[float | None] = mapped_column(Float)

    # Form and ownership
    form: Mapped[float | None] = mapped_column(Float)
    form_rank: Mapped[int | None] = mapped_column(Integer)
    points_per_game: Mapped[float | None] = mapped_column(Float)
    selected_by_percent: Mapped[float | None] = mapped_column(Float)
    selected_rank: Mapped[int | None] = mapped_column(Integer)
    ep_this: Mapped[float | None] = mapped_column(Float)
    ep_next: Mapped[float | None] = mapped_column(Float)

    # Temporal fields for backtesting
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    player: Mapped["Player"] = relationship()
    gameweek: Mapped["Gameweek"] = relationship()
    season: Mapped["Season"] = relationship()
    team: Mapped["Team"] = relationship()

    __table_args__ = (
        UniqueConstraint("player_id", "gameweek_id", name="uq_player_gameweek"),
    )


class TeamMatchPerformance(Base):
    """Per-match team-level statistics.

    Includes temporal fields (ingested_at, available_at) to support
    historical backtesting with strict no-look-ahead enforcement.
    """
    __tablename__ = "team_match_performances"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), nullable=False, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    is_home: Mapped[bool] = mapped_column()

    goals_scored: Mapped[int | None] = mapped_column(Integer, default=0)
    goals_conceded: Mapped[int | None] = mapped_column(Integer, default=0)
    expected_goals: Mapped[float | None] = mapped_column(Float)
    expected_goals_conceded: Mapped[float | None] = mapped_column(Float)
    expected_goal_involvements: Mapped[float | None] = mapped_column(Float)
    shots: Mapped[int | None] = mapped_column(Integer)
    shots_on_target: Mapped[int | None] = mapped_column(Integer)
    possession: Mapped[float | None] = mapped_column(Float)
    corners: Mapped[int | None] = mapped_column(Integer)
    fouls: Mapped[int | None] = mapped_column(Integer)
    yellow_cards: Mapped[int | None] = mapped_column(Integer)
    red_cards: Mapped[int | None] = mapped_column(Integer)

    # Temporal fields for backtesting
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    team: Mapped["Team"] = relationship()
    fixture: Mapped["Fixture"] = relationship()
    season: Mapped["Season"] = relationship()

    __table_args__ = (
        UniqueConstraint("team_id", "fixture_id", name="uq_team_match"),
    )


class FPLSnapshot(Base):
    """Historical FPL snapshot for a player at a point in time.

    These snapshots preserve the state of FPL data (price, ownership, form, etc.)
    as it was at a specific point in time. This is critical for backtesting:
    the backtesting system must use the snapshot that was available before
    the Gameweek deadline, not the current value.

    Temporal fields:
    - event_time: When the football/market event occurred
    - published_at: When the source published the information
    - available_at: The earliest timestamp at which our system can legitimately
      be considered to have accessed the information
    - ingested_at: When our pipeline actually collected it
    - source_last_modified_at: When the source last modified the underlying record
    """
    __tablename__ = "fpl_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    gameweek_id: Mapped[int | None] = mapped_column(ForeignKey("gameweeks.id"), index=True)

    # Temporal fields
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.now
    )
    source_last_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Price and ownership
    price: Mapped[float | None] = mapped_column(Float)
    selected_by_percent: Mapped[float | None] = mapped_column(Float)
    transfers_in_event: Mapped[int | None] = mapped_column(Integer, default=0)
    transfers_out_event: Mapped[int | None] = mapped_column(Integer, default=0)
    transfers_in_season: Mapped[int | None] = mapped_column(Integer, default=0)
    transfers_out_season: Mapped[int | None] = mapped_column(Integer, default=0)

    # Performance data at snapshot time
    total_points: Mapped[int | None] = mapped_column(Integer, default=0)
    form: Mapped[float | None] = mapped_column(Float)
    points_per_game: Mapped[float | None] = mapped_column(Float)
    form_rank: Mapped[int | None] = mapped_column(Integer)
    points_per_game_rank: Mapped[int | None] = mapped_column(Integer)
    selected_rank: Mapped[int | None] = mapped_column(Integer)

    # Upcoming
    ep_this: Mapped[float | None] = mapped_column(Float)
    ep_next: Mapped[float | None] = mapped_column(Float)

    player: Mapped["Player"] = relationship()
    season: Mapped["Season"] = relationship()
    gameweek: Mapped["Gameweek | None"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "player_id", "gameweek_id", "event_time",
            name="uq_player_snapshot_time",
        ),
    )


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    job_name: Mapped[str] = mapped_column(String(150), nullable=False)
    season_code: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)


class RawRecord(Base):
    __tablename__ = "raw_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(100))
    endpoint: Mapped[str] = mapped_column(String(300), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    season_code: Mapped[str | None] = mapped_column(String(20))

    __table_args__ = (
        UniqueConstraint("source", "endpoint", "payload_hash", name="uq_raw_payload"),
    )