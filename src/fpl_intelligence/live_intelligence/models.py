"""Phase 9 DB models: live source registry, temporal ledger, tactical evidence.

Design rules
------------

* **The ledger is append-only.** ``live_intelligence_raw_items`` rows are never
  updated in place. Re-capturing the same text from the same source is a
  no-op (content-hash dedupe), never an overwrite.

* **Four temporal fields are mandatory-by-contract** on every ledger row:
  ``published_at`` (nullable, because we refuse to fabricate it),
  ``scraped_at``, ``available_at`` and ``ingested_at`` (all NOT NULL).
  ``event_time`` is carried as a fifth, optional marker for the event the text
  describes, matching :mod:`fpl_intelligence.features.temporal`.

* **Phase 7 tables are not modified.** Availability evidence extracted by the
  LLM is written to the existing ``availability_evidence`` table, and its
  provenance back to the ledger is carried by the Phase 9-owned link table
  ``live_availability_evidence_links``. Tactical evidence lives in the new
  Phase 9-owned ``tactical_evidence`` table, which carries its provenance
  columns inline.

* **Provenance of the reasoning layer is first-class.** Every extraction is
  recorded in ``llm_extraction_runs`` with the provider, model, prompt template
  id, prompt hash and whether it was a mock. An evidence row whose extraction
  run is ``is_mock`` can never be reported as real validation evidence.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum, StrEnum

from sqlalchemy import (
    Boolean,
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

from fpl_intelligence.availability.models import SourceReliability
from fpl_intelligence.db.base import Base


def _enum_values(enum_cls: type[Enum]) -> list[str]:
    """Return the string values of an enum for SAEnum storage.

    Mirrors the Phase 7 convention: the database stores the lowercase string
    values (``"press_conference"``), not the Python member names.
    """
    return [member.value for member in enum_cls]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LiveSourceType(StrEnum):
    """Kind of unstructured live source feeding the accumulator."""

    PRESS_CONFERENCE = "press_conference"
    CLUB_OFFICIAL = "club_official"
    JOURNALIST = "journalist"
    NEWS_ARTICLE = "news_article"
    SOCIAL_POST = "social_post"
    PODCAST_TRANSCRIPT = "podcast_transcript"
    AGGREGATOR = "aggregator"
    FPL_OFFICIAL = "fpl_official"
    OTHER = "other"


class CaptureMethod(StrEnum):
    """How the raw text entered the ledger.

    ``MOCK_FIXTURE`` exists so that scaffold/test rows are self-identifying and
    can never be mistaken for captured reality.
    """

    MANUAL_PASTE = "manual_paste"
    RSS = "rss"
    API = "api"
    HTML_SCRAPE = "html_scrape"
    TRANSCRIPT_UPLOAD = "transcript_upload"
    MOCK_FIXTURE = "mock_fixture"


class LedgerTemporalClass(StrEnum):
    """Deadline eligibility of a ledger row, decided purely from timestamps.

    This says nothing about whether the row is *real*. Realness is carried
    separately by :attr:`LiveIntelligenceSource.environment`, so that a
    perfectly-timed mock row is never silently promoted to evidence.
    """

    #: The configured information-access policy is satisfied against the
    #: gameweek deadline. Usable as pre-deadline intelligence.
    PRE_DEADLINE = "pre_deadline"
    #: The information became available (or was ingested) after the deadline.
    #: Never usable as pre-deadline intelligence for that gameweek.
    POST_DEADLINE = "post_deadline"
    #: No gameweek deadline has been resolved for this row yet. Eligibility is
    #: undecided; the row is NOT usable until a deadline is attached.
    NO_DEADLINE_CONTEXT = "no_deadline_context"


class TacticalEvidenceType(StrEnum):
    """Phase 8 tactical signal taxonomy (see ``docs/phase8-scope.md`` §3)."""

    STARTING_LINEUP_HINT = "starting_lineup_hint"
    FORMATION = "formation"
    PLAYER_POSITION = "player_position"
    ROLE_CHANGE = "role_change"
    POSITIONAL_ROLE_CONTEXT = "positional_role_context"
    SET_PIECE_PENALTIES = "set_piece_penalties"
    SET_PIECE_FREEKICKS = "set_piece_freekicks"
    SET_PIECE_CORNERS = "set_piece_corners"
    MANAGER_CHANGE = "manager_change"
    MANAGER_FORMATION_TENDENCY = "manager_formation_tendency"
    ROTATION_TENDENCY = "rotation_tendency"
    TEAM_STYLE = "team_style"
    MATCHUP_CONTEXT = "matchup_context"
    MINUTES_RISK_ROLE_CHANGE = "minutes_risk_role_change"
    DIFFERENTIAL_SIGNAL = "differential_signal"
    UNKNOWN = "unknown"


class TacticalDirection(StrEnum):
    """Qualitative direction of a tactical signal's effect on a player."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class ExtractionStatus(StrEnum):
    """Terminal status of a single LLM extraction run."""

    OK = "ok"
    EMPTY = "empty"
    PARSE_FAILED = "parse_failed"
    SCHEMA_REJECTED = "schema_rejected"
    GROUNDING_REJECTED = "grounding_rejected"
    PROVIDER_ERROR = "provider_error"


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------


class LiveIntelligenceSource(Base):
    """A registered live source of unstructured football intelligence.

    Distinct from Phase 7's ``availability_sources``: that table describes
    sources of *availability* news only, and is keyed into the Phase 7 article
    model. This table registers any source of raw text (press conferences,
    tweets, articles, transcripts) that can yield availability *or* tactical
    evidence, and it carries the extra metadata the temporal ledger needs
    (capture method, publication-timestamp trust, environment marker).
    """

    __tablename__ = "live_intelligence_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(
        SAEnum(LiveSourceType, values_callable=_enum_values),
        nullable=False,
        default=LiveSourceType.OTHER,
    )
    url: Mapped[str | None] = mapped_column(String(500))
    reliability: Mapped[str] = mapped_column(
        SAEnum(SourceReliability, values_callable=_enum_values),
        nullable=False,
        default=SourceReliability.UNVERIFIED,
    )
    capture_method: Mapped[str] = mapped_column(
        SAEnum(CaptureMethod, values_callable=_enum_values),
        nullable=False,
        default=CaptureMethod.MANUAL_PASTE,
    )
    is_official_club: Mapped[bool] = mapped_column(Boolean, default=False)
    #: ``"real"`` or ``"mock"`` (:class:`~fpl_intelligence.domain.environment.DataEnvironment`).
    #: Rows from a ``mock`` source are engineering artefacts and are excluded
    #: from every validation-evidence query.
    environment: Mapped[str] = mapped_column(String(10), nullable=False, default="mock")
    #: Whether this source's ``published_at`` can be trusted as the true
    #: publication instant. When False the ledger falls back to ``scraped_at``.
    publication_timestamp_trusted: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Observed lag between publication and our capture, in seconds. Recorded
    #: for auditing pipeline delay; never used to backdate ``available_at``.
    typical_capture_lag_seconds: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw_items: Mapped[list[LiveIntelligenceRawItem]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_live_sources_type_env", "source_type", "environment"),
    )


# ---------------------------------------------------------------------------
# Temporal ledger
# ---------------------------------------------------------------------------


class LiveIntelligenceRawItem(Base):
    """One immutable, timestamped unit of captured raw text — the ledger row.

    This is the single point at which time enters the Phase 9 pipeline.
    Everything downstream (LLM evidence, analyst narratives) inherits its
    temporal fields from here and may not invent its own.

    Temporal contract (validated by
    :func:`fpl_intelligence.live_intelligence.temporal_ledger.validate_timestamps`):

    ==================  ==========================================================
    ``event_time``      When the football event described happened. Optional.
    ``published_at``    When the source published it. Optional — we never
                        fabricate it; ``publication_established`` records
                        whether it was genuinely obtained.
    ``scraped_at``      When our capture step actually saw the text. Required.
    ``available_at``    Earliest instant we can legitimately claim access.
                        Derived, never earlier than ``published_at``, and
                        never earlier than ``scraped_at`` under the
                        conservative policy. Required.
    ``ingested_at``     When this row was written to the ledger. Required.
    ==================  ==========================================================
    """

    __tablename__ = "live_intelligence_raw_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("live_intelligence_sources.id"), nullable=False, index=True
    )

    # -- content ---------------------------------------------------------
    #: SHA-256 of the whitespace-normalised raw text. Unique per source, which
    #: makes re-capture idempotent instead of duplicating the ledger.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(String(1000))
    content_type: Mapped[str] = mapped_column(String(50), nullable=False, default="text")
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")

    # -- unresolved entity hints ------------------------------------------
    # Deliberately free text. Phase 7's empirical blockage was caused by an
    # entity-resolution key mismatch, so Phase 9 stores the *hint* verbatim and
    # resolves it in a separate, auditable step rather than guessing at ingest.
    team_hint: Mapped[str | None] = mapped_column(String(200))
    player_hints: Mapped[str | None] = mapped_column(Text)

    # -- canonical context (nullable until resolved) -----------------------
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"), index=True)
    gameweek_id: Mapped[int | None] = mapped_column(ForeignKey("gameweeks.id"), index=True)

    # -- temporal ledger ---------------------------------------------------
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: True only when ``published_at`` was genuinely obtained from the source.
    publication_established: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The gameweek deadline this row was evaluated against, snapshotted at
    #: ingest so the audit stays valid if the fixture list later changes.
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    temporal_class: Mapped[str] = mapped_column(
        SAEnum(LedgerTemporalClass, values_callable=_enum_values),
        nullable=False,
        default=LedgerTemporalClass.NO_DEADLINE_CONTEXT,
    )
    #: The :class:`InformationAccessPolicy` value used to decide
    #: ``temporal_class``. Recorded so the decision is reproducible.
    access_policy: Mapped[str] = mapped_column(
        String(50), nullable=False, default="strict_reproducibility"
    )
    metadata_json: Mapped[str | None] = mapped_column(Text)

    source: Mapped[LiveIntelligenceSource] = relationship(back_populates="raw_items")
    extraction_runs: Mapped[list[LLMExtractionRun]] = relationship(
        back_populates="raw_item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("source_id", "content_hash", name="uq_live_raw_source_hash"),
        Index("ix_live_raw_available_ingested", "available_at", "ingested_at"),
        Index("ix_live_raw_gw_class", "gameweek_id", "temporal_class"),
    )


# ---------------------------------------------------------------------------
# LLM reasoning-layer provenance
# ---------------------------------------------------------------------------


class LLMExtractionRun(Base):
    """Provenance record for one LLM extraction call over one ledger row.

    Recorded whether or not the call succeeded. A failed or rejected run is as
    important as a successful one: it is the audit trail that explains why a
    ledger row produced no evidence.
    """

    __tablename__ = "llm_extraction_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(
        ForeignKey("live_intelligence_raw_items.id"), nullable=False, index=True
    )
    extractor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_template_id: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)
    #: SHA-256 of the fully rendered prompt. Two runs with the same hash and
    #: same model are reproducible.
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: SHA-256 of the *unrendered* template (Phase 9.1 prompt registry). Groups
    #: every run that shared a prompt design, independent of its input.
    prompt_template_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float)
    #: True when produced by a test double. Mock runs never yield validation
    #: evidence.
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    #: True when the response came from the local response cache instead of the
    #: provider. Cached runs consumed no free-tier quota; recording it keeps the
    #: usage audit honest rather than double-counting replays as API calls.
    from_cache: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Token accounting reported by the provider, when it reports any.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    #: The generation cap that was enforced on this call.
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        SAEnum(ExtractionStatus, values_callable=_enum_values), nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[str | None] = mapped_column(Text)
    availability_evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    tactical_evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    #: JSON array of entity hints the extractor produced but could not resolve
    #: to canonical ids. Never silently discarded.
    unresolved_entities: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    #: How this run's provider was selected (Phase 9.1): one of ``task_based``,
    #: ``fallback`` or ``round_robin``, or NULL when the provider was chosen
    #: directly without routing. Records *how* the model was reached, so the
    #: audit trail states the routing decision, not just the provider.
    routing_strategy: Mapped[str | None] = mapped_column(String(20))

    raw_item: Mapped[LiveIntelligenceRawItem] = relationship(back_populates="extraction_runs")
    tactical_evidence: Mapped[list[TacticalEvidence]] = relationship(
        back_populates="extraction_run"
    )

    __table_args__ = (
        Index("ix_extraction_runs_status_mock", "status", "is_mock"),
    )


# ---------------------------------------------------------------------------
# Structured evidence
# ---------------------------------------------------------------------------


class TacticalEvidence(Base):
    """A single structured Phase 8 tactical signal extracted from raw text.

    Immutable and append-only, exactly like Phase 7 availability evidence: a
    later contradicting statement creates a new row and closes the old one via
    ``valid_to`` / ``is_active``, it never rewrites history.

    Every temporal field is inherited from the ledger row; the LLM does not
    supply any of them.
    """

    __tablename__ = "tactical_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(
        ForeignKey("live_intelligence_raw_items.id"), nullable=False, index=True
    )
    extraction_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_extraction_runs.id"), index=True
    )

    # -- subject (at least one of team/player must be set) -----------------
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), index=True)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"), index=True)
    gameweek_id: Mapped[int | None] = mapped_column(ForeignKey("gameweeks.id"), index=True)
    #: Verbatim entity strings as written by the source, kept even when the
    #: canonical id resolves, so resolution can be re-audited.
    subject_hint: Mapped[str | None] = mapped_column(String(200))

    # -- signal ------------------------------------------------------------
    evidence_type: Mapped[str] = mapped_column(
        SAEnum(TacticalEvidenceType, values_callable=_enum_values), nullable=False
    )
    #: The signal value as text, e.g. ``"4-3-3"``, ``"inverted right-back"``,
    #: or the named set-piece taker.
    value_text: Mapped[str | None] = mapped_column(String(300))
    #: Optional numeric reading of the signal (e.g. a stated rotation risk).
    numeric_value: Mapped[float | None] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(
        SAEnum(TacticalDirection, values_callable=_enum_values),
        nullable=False,
        default=TacticalDirection.UNKNOWN,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    #: Verbatim span from the raw text that supports this evidence. Extraction
    #: is rejected when this is not a literal substring of the ledger row.
    source_quote: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    # -- inherited temporal fields (never LLM-supplied) --------------------
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    temporal_class: Mapped[str] = mapped_column(
        SAEnum(LedgerTemporalClass, values_callable=_enum_values),
        nullable=False,
        default=LedgerTemporalClass.NO_DEADLINE_CONTEXT,
    )
    # -- method provenance (Phase 9.1) -------------------------------------
    #: Which prompt and which model produced this signal. Denormalised from
    #: ``llm_extraction_runs`` on purpose: an evidence row must be able to
    #: state the method that created it without a join, because the first
    #: question asked of any extracted claim is "which prompt said that?".
    prompt_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_name: Mapped[str | None] = mapped_column(String(100))
    model_name: Mapped[str | None] = mapped_column(String(200))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    extraction_run: Mapped[LLMExtractionRun | None] = relationship(
        back_populates="tactical_evidence"
    )

    __table_args__ = (
        UniqueConstraint(
            "raw_item_id",
            "evidence_type",
            "subject_hint",
            "value_text",
            name="uq_tactical_evidence_item_type_subject_value",
        ),
        Index("ix_tactical_evidence_team_gw", "team_id", "gameweek_id", "is_active"),
        Index("ix_tactical_evidence_player_gw", "player_id", "gameweek_id", "is_active"),
        Index("ix_tactical_evidence_temporal", "available_at", "ingested_at"),
    )


class ResolutionStatus(StrEnum):
    """Audit status for a single live-intelligence entity-resolution attempt.

    Mirrors :class:`~fpl_intelligence.live_intelligence.entity_resolution.ResolutionStatus`
    so a row can record exactly how (or whether) an entity was resolved without
    importing the resolver into the model layer.
    """

    RESOLVED = "resolved"
    RESOLVED_BY_EXTERNAL_ID = "resolved_by_external_id"
    RESOLVED_BY_NAME_TEAM = "resolved_by_name_team"
    RESOLVED_BY_NAME_UNIQUE = "resolved_by_name_unique"
    RESOLVED_BY_ALIAS = "resolved_by_alias"
    UNRESOLVED_PLAYER = "unresolved_player"
    UNRESOLVED_TEAM = "unresolved_team"
    AMBIGUOUS_PLAYER = "ambiguous_player"


class UnresolvedLiveEvidence(Base):
    """Append-only audit row for an entity the extractor could not resolve.

    Live evidence ingestion is robust against unresolved entities: evidence is
    never silently dropped. When a draft names a player/team that resolves to no
    canonical id (or is ambiguous), the raw item still survives — this row
    records the verbatim hint, the attempted resolution status, and the quote so
    the gap can be triaged and backfilled later.

    Phase 9-owned: it does not touch the Phase 7 tables. The raw item itself is
    persisted by :func:`ingest_raw_text` before extraction, so provenance back
    to the source text is always intact even when no canonical entity exists.
    """

    __tablename__ = "unresolved_live_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(
        ForeignKey("live_intelligence_raw_items.id"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("live_intelligence_sources.id"), nullable=False, index=True
    )
    extraction_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_extraction_runs.id"), index=True
    )
    evidence_type: Mapped[str | None] = mapped_column(String(50), index=True)
    player_name: Mapped[str | None] = mapped_column(String(200))
    team_name: Mapped[str | None] = mapped_column(String(200))
    status_mentioned: Mapped[str | None] = mapped_column(String(50))
    quote: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_name: Mapped[str | None] = mapped_column(String(100))
    team_hint: Mapped[str | None] = mapped_column(String(200))
    resolution_status: Mapped[str] = mapped_column(
        SAEnum(ResolutionStatus, values_callable=_enum_values),
        nullable=False,
        default=ResolutionStatus.UNRESOLVED_PLAYER,
    )
    resolution_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index(
            "ix_unresolved_evidence_run_type",
            "extraction_run_id",
            "evidence_type",
        ),
    )


class LiveAvailabilityEvidenceLink(Base):
    """Provenance bridge from Phase 7 ``availability_evidence`` to the ledger.

    Phase 9 writes availability evidence into the *existing* Phase 7 table
    rather than forking the schema. To keep Phase 7 untouched while still
    recording which ledger row and which LLM run produced a given evidence row,
    the link is stored here, in a Phase 9-owned table.
    """

    __tablename__ = "live_availability_evidence_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    availability_evidence_id: Mapped[int] = mapped_column(
        ForeignKey("availability_evidence.id"), nullable=False, index=True
    )
    raw_item_id: Mapped[int] = mapped_column(
        ForeignKey("live_intelligence_raw_items.id"), nullable=False, index=True
    )
    extraction_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_extraction_runs.id"), index=True
    )
    source_quote: Mapped[str | None] = mapped_column(Text)
    temporal_class: Mapped[str] = mapped_column(
        SAEnum(LedgerTemporalClass, values_callable=_enum_values),
        nullable=False,
        default=LedgerTemporalClass.NO_DEADLINE_CONTEXT,
    )
    # -- method provenance (Phase 9.1) -------------------------------------
    #: The Phase 7 evidence row itself is untouched, so the prompt and provider
    #: that produced it are recorded here, on the Phase 9-owned link.
    prompt_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_name: Mapped[str | None] = mapped_column(String(100))
    model_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint(
            "availability_evidence_id",
            "raw_item_id",
            name="uq_live_avail_link_evidence_item",
        ),
    )
