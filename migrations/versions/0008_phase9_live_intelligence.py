"""phase9: live intelligence accumulator + LLM reasoning layer

Creates the Phase 9 schema. Phase 1-8 tables are NOT modified: availability
evidence produced by the LLM is written into the existing Phase 7
``availability_evidence`` table, and its provenance back to the temporal ledger
is carried by the new, Phase 9-owned ``live_availability_evidence_links`` table.

New tables:
- live_intelligence_sources       registry of live unstructured sources, with a
                                  real/mock environment marker and a declaration
                                  of whether the source's publication timestamp
                                  can be trusted.
- live_intelligence_raw_items     the append-only temporal ledger. Carries
                                  event_time / published_at / scraped_at /
                                  available_at / ingested_at plus the deadline
                                  snapshot and access policy used to classify it.
- llm_extraction_runs             provenance for every LLM extraction call,
                                  including failures, prompt hash and is_mock.
- tactical_evidence               Phase 8 tactical signals extracted from text,
                                  with temporal fields inherited from the ledger.
- live_availability_evidence_links  provenance bridge to Phase 7 evidence.

Revision ID: 0008_phase9_live_intelligence
Revises: 0007_historical_availability
Create Date: 2026-08-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_phase9_live_intelligence"
down_revision = "0007_historical_availability"
branch_labels = None
depends_on = None

_LIVE_SOURCE_TYPE = [
    "press_conference",
    "club_official",
    "journalist",
    "news_article",
    "social_post",
    "podcast_transcript",
    "aggregator",
    "fpl_official",
    "other",
]

_CAPTURE_METHOD = [
    "manual_paste",
    "rss",
    "api",
    "html_scrape",
    "transcript_upload",
    "mock_fixture",
]

_LEDGER_TEMPORAL_CLASS = [
    "pre_deadline",
    "post_deadline",
    "no_deadline_context",
]

_TACTICAL_EVIDENCE_TYPE = [
    "starting_lineup_hint",
    "formation",
    "player_position",
    "role_change",
    "positional_role_context",
    "set_piece_penalties",
    "set_piece_freekicks",
    "set_piece_corners",
    "manager_change",
    "manager_formation_tendency",
    "rotation_tendency",
    "team_style",
    "matchup_context",
    "minutes_risk_role_change",
    "differential_signal",
    "unknown",
]

_TACTICAL_DIRECTION = ["positive", "negative", "neutral", "unknown"]

_EXTRACTION_STATUS = [
    "ok",
    "empty",
    "parse_failed",
    "schema_rejected",
    "grounding_rejected",
    "provider_error",
]

# The Phase 7 sourcereliability enum type already exists; reuse it without
# attempting to re-create it.
_SOURCE_RELIABILITY = sa.Enum(
    "official",
    "verified_journalist",
    "reliable_journalist",
    "unverified",
    name="sourcereliability",
    create_type=False,
)


def upgrade() -> None:
    live_source_type = sa.Enum(*_LIVE_SOURCE_TYPE, name="livesourcetype")
    capture_method = sa.Enum(*_CAPTURE_METHOD, name="capturemethod")
    ledger_temporal_class = sa.Enum(*_LEDGER_TEMPORAL_CLASS, name="ledgertemporalclass")
    tactical_evidence_type = sa.Enum(*_TACTICAL_EVIDENCE_TYPE, name="tacticalevidencetype")
    tactical_direction = sa.Enum(*_TACTICAL_DIRECTION, name="tacticaldirection")
    extraction_status = sa.Enum(*_EXTRACTION_STATUS, name="extractionstatus")

    bind = op.get_bind()
    for enum_type in (
        live_source_type,
        capture_method,
        ledger_temporal_class,
        tactical_evidence_type,
        tactical_direction,
        extraction_status,
    ):
        enum_type.create(bind, checkfirst=True)

    # -- sources -----------------------------------------------------------
    op.create_table(
        "live_intelligence_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("source_type", live_source_type, nullable=False, server_default="other"),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column(
            "reliability", _SOURCE_RELIABILITY, nullable=False, server_default="unverified"
        ),
        sa.Column(
            "capture_method", capture_method, nullable=False, server_default="manual_paste"
        ),
        sa.Column("is_official_club", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("environment", sa.String(10), nullable=False, server_default="mock"),
        sa.Column(
            "publication_timestamp_trusted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("typical_capture_lag_seconds", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_live_sources_type_env", "live_intelligence_sources", ["source_type", "environment"])
    op.create_index(
        "ix_live_intelligence_sources_is_active", "live_intelligence_sources", ["is_active"]
    )

    # -- temporal ledger ----------------------------------------------------
    op.create_table(
        "live_intelligence_raw_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("live_intelligence_sources.id"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("url", sa.String(1000), nullable=True),
        sa.Column("content_type", sa.String(50), nullable=False, server_default="text"),
        sa.Column("language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("team_hint", sa.String(200), nullable=True),
        sa.Column("player_hints", sa.Text(), nullable=True),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=True),
        sa.Column("gameweek_id", sa.Integer(), sa.ForeignKey("gameweeks.id"), nullable=True),
        # Temporal ledger fields.
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "publication_established",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "temporal_class",
            ledger_temporal_class,
            nullable=False,
            server_default="no_deadline_context",
        ),
        sa.Column(
            "access_policy",
            sa.String(50),
            nullable=False,
            server_default="strict_reproducibility",
        ),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.UniqueConstraint("source_id", "content_hash", name="uq_live_raw_source_hash"),
    )
    for col in ("source_id", "content_hash", "season_id", "gameweek_id", "event_time",
                "published_at", "available_at", "ingested_at", "deadline_at"):
        op.create_index(f"ix_live_intelligence_raw_items_{col}", "live_intelligence_raw_items", [col])
    op.create_index(
        "ix_live_raw_available_ingested",
        "live_intelligence_raw_items",
        ["available_at", "ingested_at"],
    )
    op.create_index(
        "ix_live_raw_gw_class",
        "live_intelligence_raw_items",
        ["gameweek_id", "temporal_class"],
    )

    # -- LLM extraction provenance -----------------------------------------
    op.create_table(
        "llm_extraction_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "raw_item_id",
            sa.Integer(),
            sa.ForeignKey("live_intelligence_raw_items.id"),
            nullable=False,
        ),
        sa.Column("extractor_name", sa.String(100), nullable=False),
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("prompt_template_id", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(20), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(50), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", extraction_status, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.Column(
            "availability_evidence_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("tactical_evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unresolved_entities", sa.Text(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_llm_extraction_runs_raw_item_id", "llm_extraction_runs", ["raw_item_id"])
    op.create_index("ix_llm_extraction_runs_prompt_hash", "llm_extraction_runs", ["prompt_hash"])
    op.create_index("ix_llm_extraction_runs_is_mock", "llm_extraction_runs", ["is_mock"])
    op.create_index("ix_extraction_runs_status_mock", "llm_extraction_runs", ["status", "is_mock"])

    # -- tactical evidence --------------------------------------------------
    op.create_table(
        "tactical_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "raw_item_id",
            sa.Integer(),
            sa.ForeignKey("live_intelligence_raw_items.id"),
            nullable=False,
        ),
        sa.Column(
            "extraction_run_id",
            sa.Integer(),
            sa.ForeignKey("llm_extraction_runs.id"),
            nullable=True,
        ),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=True),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=True),
        sa.Column("gameweek_id", sa.Integer(), sa.ForeignKey("gameweeks.id"), nullable=True),
        sa.Column("subject_hint", sa.String(200), nullable=True),
        sa.Column("evidence_type", tactical_evidence_type, nullable=False),
        sa.Column("value_text", sa.String(300), nullable=True),
        sa.Column("numeric_value", sa.Float(), nullable=True),
        sa.Column("direction", tactical_direction, nullable=False, server_default="unknown"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("source_quote", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "temporal_class",
            ledger_temporal_class,
            nullable=False,
            server_default="no_deadline_context",
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint(
            "raw_item_id",
            "evidence_type",
            "subject_hint",
            "value_text",
            name="uq_tactical_evidence_item_type_subject_value",
        ),
    )
    for col in ("raw_item_id", "extraction_run_id", "team_id", "player_id", "season_id",
                "gameweek_id", "published_at", "available_at", "ingested_at",
                "valid_from", "valid_to", "is_active"):
        op.create_index(f"ix_tactical_evidence_{col}", "tactical_evidence", [col])
    op.create_index(
        "ix_tactical_evidence_team_gw", "tactical_evidence", ["team_id", "gameweek_id", "is_active"]
    )
    op.create_index(
        "ix_tactical_evidence_player_gw",
        "tactical_evidence",
        ["player_id", "gameweek_id", "is_active"],
    )
    op.create_index(
        "ix_tactical_evidence_temporal", "tactical_evidence", ["available_at", "ingested_at"]
    )

    # -- provenance bridge to Phase 7 --------------------------------------
    op.create_table(
        "live_availability_evidence_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "availability_evidence_id",
            sa.Integer(),
            sa.ForeignKey("availability_evidence.id"),
            nullable=False,
        ),
        sa.Column(
            "raw_item_id",
            sa.Integer(),
            sa.ForeignKey("live_intelligence_raw_items.id"),
            nullable=False,
        ),
        sa.Column(
            "extraction_run_id",
            sa.Integer(),
            sa.ForeignKey("llm_extraction_runs.id"),
            nullable=True,
        ),
        sa.Column("source_quote", sa.Text(), nullable=True),
        sa.Column(
            "temporal_class",
            ledger_temporal_class,
            nullable=False,
            server_default="no_deadline_context",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "availability_evidence_id",
            "raw_item_id",
            name="uq_live_avail_link_evidence_item",
        ),
    )
    for col in ("availability_evidence_id", "raw_item_id", "extraction_run_id"):
        op.create_index(
            f"ix_live_availability_evidence_links_{col}",
            "live_availability_evidence_links",
            [col],
        )


def downgrade() -> None:
    op.drop_table("live_availability_evidence_links")
    op.drop_table("tactical_evidence")
    op.drop_table("llm_extraction_runs")
    op.drop_table("live_intelligence_raw_items")
    op.drop_table("live_intelligence_sources")
    for type_name in (
        "extractionstatus",
        "tacticaldirection",
        "tacticalevidencetype",
        "ledgertemporalclass",
        "capturemethod",
        "livesourcetype",
    ):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {type_name}"))
