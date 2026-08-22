"""Phase 7 DB models: news, evidence, and availability intelligence.

Preserves full provenance and temporal fidelity:
- Every record has a source, ingested_at, and available_at timestamp.
- Historical records are never overwritten; new evidence creates new rows.
- Availability states are computed at query time from accumulated evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum, StrEnum

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fpl_intelligence.db.base import Base


def _enum_values(enum_cls: type[Enum]) -> list[str]:
    """Return the string values of an enum for SAEnum storage.

    Phase 7 stores lowercase string values (e.g. "official", "start",
    "injury") in the database, matching the strings used throughout the
    evidence/state modules.
    """
    return [member.value for member in enum_cls]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SourceReliability(StrEnum):
    """Reliability tiers for news/availability sources."""

    OFFICIAL = "official"
    VERIFIED_JOURNALIST = "verified_journalist"
    RELIABLE_JOURNALIST = "reliable_journalist"
    UNVERIFIED = "unverified"


class AvailabilityStatus(StrEnum):
    """Canonical availability states for a player-gameweek."""

    START = "start"
    BENCH = "bench"
    #: Player is reported fit/available/in contention, but not explicitly
    #: confirmed to start. Added in Phase 9.1.1 so the live extraction layer can
    #: express "fit but not sure about the starting XI" without collapsing it
    #: into a semantically wrong status. Maps onto the conservative state
    #: heuristics in :mod:`fpl_intelligence.availability.state`.
    AVAILABLE = "available"
    DOUBTFUL = "doubtful"
    QUESTIONABLE = "questionable"
    SUSPECT = "suspect"
    OUT = "out"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


class EvidenceType(StrEnum):
    """Types of availability evidence extracted from sources."""

    INJURY = "injury"
    SUSPENSION = "suspension"
    FITNESS = "fitness"
    TRAINING = "training"
    MANAGER_QUOTE = "manager_quote"
    LINEUP_HINT = "lineup_hint"
    RECOVERY_UPDATE = "recovery_update"
    TRANSFER_NEWS = "transfer_news"


# ---------------------------------------------------------------------------
# Sources and articles
# ---------------------------------------------------------------------------


class AvailabilitySource(Base):
    """A source of availability information (news outlet, official site, etc.)."""

    __tablename__ = "availability_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    url: Mapped[str | None] = mapped_column(String(500))
    reliability: Mapped[str] = mapped_column(
        SAEnum(SourceReliability, values_callable=_enum_values),
        default=SourceReliability.UNVERIFIED,
    )
    is_official_club: Mapped[bool] = mapped_column(default=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    articles: Mapped[list[AvailabilityArticle]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class AvailabilityArticle(Base):
    """A news article or statement that may contain availability evidence."""

    __tablename__ = "availability_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("availability_sources.id"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    headline: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content: Mapped[str | None] = mapped_column(Text)

    source: Mapped[AvailabilitySource] = relationship(back_populates="articles")
    evidence: Mapped[list[AvailabilityEvidence]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_articles_published_source", "published_at", "source_id"),)


# ---------------------------------------------------------------------------
# Evidence and events
# ---------------------------------------------------------------------------


class AvailabilityEvidence(Base):
    """A single piece of evidence about a player's availability.

    Extracted from an article or structured API. Evidence is never
    overwritten; new evidence creates new rows.
    """

    __tablename__ = "availability_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("availability_articles.id"), index=True
    )
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    gameweek_id: Mapped[int | None] = mapped_column(ForeignKey("gameweeks.id"), index=True)
    evidence_type: Mapped[str] = mapped_column(
        SAEnum(EvidenceType, values_callable=_enum_values), nullable=False
    )
    status_mentioned: Mapped[str] = mapped_column(
        SAEnum(AvailabilityStatus, values_callable=_enum_values), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    description: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    is_active: Mapped[bool] = mapped_column(default=True, index=True)

    article: Mapped[AvailabilityArticle | None] = relationship(back_populates="evidence")

    __table_args__ = (
        UniqueConstraint(
            "player_id",
            "gameweek_id",
            "evidence_type",
            "valid_from",
            name="uq_evidence_player_gw_type_time",
        ),
    )


class TemporalClass(StrEnum):
    """Temporal classification for historical availability events (Phase 7.2).

    Distinguishes when information became available (publication/availability
    time) from when the event merely occurred. This is the critical no-look-ahead
    distinction: an event that happened before a deadline but whose information was
    NOT available before the deadline must NOT be used as strict pre-deadline
    intelligence.
    """

    STRICT_BACKTEST_SAFE = "STRICT_BACKTEST_SAFE"
    HISTORICAL_EVENT_ONLY = "HISTORICAL_EVENT_ONLY"
    OUTCOME_ONLY = "OUTCOME_ONLY"
    UNKNOWN = "UNKNOWN"


class AvailabilityEvent(Base):
    """Aggregated availability event derived from multiple evidence items.

    Evidence from multiple sources is corroborated and assigned a consolidated
    confidence. Events are immutable once created; new events are inserted.

    Phase 7.2 adds temporal classification and provider provenance:
    - ``temporal_class`` records whether the event is STRICT_BACKTEST_SAFE
      (information was available before the decision cutoff), HISTORICAL_EVENT_ONLY
      (event occurred but availability timing cannot be established), OUTCOME_ONLY,
      or UNKNOWN.
    - ``provider`` / ``provider_event_id`` record which provider adapter produced the
      event and the provider's own identifier (for idempotent re-import and entity
      resolution).
    """

    __tablename__ = "availability_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    gameweek_id: Mapped[int | None] = mapped_column(ForeignKey("gameweeks.id"), index=True)
    status: Mapped[str] = mapped_column(
        SAEnum(AvailabilityStatus, values_callable=_enum_values), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, default=1)
    primary_source_id: Mapped[int | None] = mapped_column(ForeignKey("availability_sources.id"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    is_current: Mapped[bool] = mapped_column(default=True, index=True)
    # Phase 7.2: temporal classification + provider provenance.
    temporal_class: Mapped[str] = mapped_column(
        SAEnum(TemporalClass, values_callable=_enum_values),
        default=TemporalClass.UNKNOWN,
    )
    provider: Mapped[str | None] = mapped_column(String(100))
    provider_event_id: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (
        Index(
            "ix_events_player_season_current",
            "player_id",
            "season_id",
            "is_current",
        ),
        Index("ix_events_status_validfrom", "valid_from", "valid_to"),
        Index("ix_events_temporal_provider", "temporal_class", "provider"),
    )


# ---------------------------------------------------------------------------
# Structured injury / suspension / training records
# ---------------------------------------------------------------------------


class PlayerInjury(Base):
    """Structured injury record for a player."""

    __tablename__ = "player_injuries"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    injury_type: Mapped[str] = mapped_column(String(100), nullable=False)
    body_part: Mapped[str | None] = mapped_column(String(100))
    severity: Mapped[str | None] = mapped_column(String(50))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expected_return_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    actual_return_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(default=True, index=True)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("availability_evidence.id"))


class PlayerSuspension(Base):
    """Suspension record with gameweek count and known return date."""

    __tablename__ = "player_suspensions"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    reason: Mapped[str] = mapped_column(String(100))
    gameweek_count: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    returns_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    is_active: Mapped[bool] = mapped_column(default=True, index=True)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("availability_evidence.id"))


class TrainingReport(Base):
    """Training session participation status."""

    __tablename__ = "training_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    session_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    participated: Mapped[bool] = mapped_column(nullable=False)
    training_load: Mapped[float | None] = mapped_column(Float)
    limited: Mapped[bool] = mapped_column(default=False)
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("availability_evidence.id"))

    __table_args__ = (
        UniqueConstraint("player_id", "session_at", name="uq_training_player_session"),
    )


class PressConference(Base):
    """Structured press conference transcript with manager quotes."""

    __tablename__ = "press_conferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    gameweek_id: Mapped[int | None] = mapped_column(ForeignKey("gameweeks.id"), index=True)
    held_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    transcript: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class PlayerMention(Base):
    """A player mention in a press conference transcript."""

    __tablename__ = "player_mentions"

    id: Mapped[int] = mapped_column(primary_key=True)
    press_conference_id: Mapped[int] = mapped_column(
        ForeignKey("press_conferences.id"), nullable=False, index=True
    )
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    sentiment: Mapped[str | None] = mapped_column(String(50))
    extracted_status: Mapped[str | None] = mapped_column(
        SAEnum(AvailabilityStatus, values_callable=_enum_values)
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.5)

    __table_args__ = (UniqueConstraint("press_conference_id", "player_id", name="uq_press_player"),)
