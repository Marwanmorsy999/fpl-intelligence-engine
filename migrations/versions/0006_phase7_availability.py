"""phase7: availability intelligence tables

Creates the 9 Phase 7 tables for news/availability intelligence:
availability_sources, availability_articles, availability_evidence,
availability_events, player_injuries, player_suspensions, training_reports,
press_conferences, and player_mentions.

These tables preserve full provenance and temporal fidelity:
- Every record carries source / ingested_at / available_at timestamps.
- Historical records are never overwritten; new evidence inserts new rows.
- Availability states are computed at query time from accumulated evidence.

Revision ID: 0006_phase7_availability
Revises: 0005_phase4_prediction_models
Create Date: 2026-08-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_phase7_availability"
down_revision = "0005_phase4_prediction_models"
branch_labels = None
depends_on = None

# Enum value sets (must match the SQLAlchemy models' values_callable ordering).
_SOURCE_RELIABILITY = [
    "official", "verified_journalist", "reliable_journalist", "unverified",
]
_AVAILABILITY_STATUS = [
    "start", "bench", "doubtful", "questionable",
    "suspect", "out", "suspended", "unknown",
]
_EVIDENCE_TYPE = [
    "injury", "suspension", "fitness", "training",
    "manager_quote", "lineup_hint", "recovery_update", "transfer_news",
]


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Sources and articles
    # ------------------------------------------------------------------
    op.create_table(
        "availability_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("url", sa.String(500)),
        sa.Column(
            "reliability",
            sa.Enum(*_SOURCE_RELIABILITY, name="sourcereliability"),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column(
            "is_official_club",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "availability_articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("availability_sources.id"),
            nullable=False,
        ),
        sa.Column("url", sa.String(500), nullable=False, unique=True),
        sa.Column("headline", sa.Text()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "scraped_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("ingested_at", sa.DateTime(timezone=True)),
        sa.Column("content", sa.Text()),
    )
    op.create_index(
        "ix_availability_articles_source_id", "availability_articles", ["source_id"],
    )
    op.create_index(
        "ix_availability_articles_published_at",
        "availability_articles", ["published_at"],
    )
    op.create_index(
        "ix_articles_published_source",
        "availability_articles", ["published_at", "source_id"],
    )

    # ------------------------------------------------------------------
    # Evidence and events
    # ------------------------------------------------------------------
    op.create_table(
        "availability_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("availability_articles.id"),
        ),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("gameweek_id", sa.Integer(), sa.ForeignKey("gameweeks.id")),
        sa.Column(
            "evidence_type",
            sa.Enum(*_EVIDENCE_TYPE, name="evidencetype"),
            nullable=False,
        ),
        sa.Column(
            "status_mentioned",
            sa.Enum(*_AVAILABILITY_STATUS, name="availabilitystatus"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("description", sa.Text()),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.UniqueConstraint(
            "player_id", "gameweek_id", "evidence_type", "valid_from",
            name="uq_evidence_player_gw_type_time",
        ),
    )
    op.create_index(
        "ix_availability_evidence_article_id", "availability_evidence", ["article_id"],
    )
    op.create_index(
        "ix_availability_evidence_player_id", "availability_evidence", ["player_id"],
    )
    op.create_index(
        "ix_availability_evidence_season_id", "availability_evidence", ["season_id"],
    )
    op.create_index(
        "ix_availability_evidence_gameweek_id",
        "availability_evidence", ["gameweek_id"],
    )
    op.create_index(
        "ix_availability_evidence_extracted_at",
        "availability_evidence", ["extracted_at"],
    )
    op.create_index(
        "ix_availability_evidence_valid_from", "availability_evidence", ["valid_from"],
    )
    op.create_index(
        "ix_availability_evidence_valid_to", "availability_evidence", ["valid_to"],
    )
    op.create_index(
        "ix_availability_evidence_is_active", "availability_evidence", ["is_active"],
    )

    op.create_table(
        "availability_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("gameweek_id", sa.Integer(), sa.ForeignKey("gameweeks.id")),
        sa.Column(
            "status",
            sa.Enum(*_AVAILABILITY_STATUS, name="availabilitystatus"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "primary_source_id",
            sa.Integer(),
            sa.ForeignKey("availability_sources.id"),
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_index(
        "ix_availability_events_player_id", "availability_events", ["player_id"],
    )
    op.create_index(
        "ix_availability_events_season_id", "availability_events", ["season_id"],
    )
    op.create_index(
        "ix_availability_events_gameweek_id", "availability_events", ["gameweek_id"],
    )
    op.create_index(
        "ix_availability_events_valid_from", "availability_events", ["valid_from"],
    )
    op.create_index(
        "ix_availability_events_valid_to", "availability_events", ["valid_to"],
    )
    op.create_index(
        "ix_availability_events_is_current", "availability_events", ["is_current"],
    )
    op.create_index(
        "ix_events_player_season_current",
        "availability_events", ["player_id", "season_id", "is_current"],
    )
    op.create_index(
        "ix_events_status_validfrom",
        "availability_events", ["valid_from", "valid_to"],
    )

    # ------------------------------------------------------------------
    # Structured injury / suspension / training records
    # ------------------------------------------------------------------
    op.create_table(
        "player_injuries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("injury_type", sa.String(100), nullable=False),
        sa.Column("body_part", sa.String(100)),
        sa.Column("severity", sa.String(50)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_return_at", sa.DateTime(timezone=True)),
        sa.Column("actual_return_at", sa.DateTime(timezone=True)),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "evidence_id",
            sa.Integer(),
            sa.ForeignKey("availability_evidence.id"),
        ),
    )
    op.create_index(
        "ix_player_injuries_player_id", "player_injuries", ["player_id"],
    )
    op.create_index(
        "ix_player_injuries_started_at", "player_injuries", ["started_at"],
    )
    op.create_index(
        "ix_player_injuries_expected_return_at",
        "player_injuries", ["expected_return_at"],
    )
    op.create_index(
        "ix_player_injuries_is_active", "player_injuries", ["is_active"],
    )

    op.create_table(
        "player_suspensions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("gameweek_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("returns_at", sa.DateTime(timezone=True)),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "evidence_id",
            sa.Integer(),
            sa.ForeignKey("availability_evidence.id"),
        ),
    )
    op.create_index(
        "ix_player_suspensions_player_id", "player_suspensions", ["player_id"],
    )
    op.create_index(
        "ix_player_suspensions_started_at", "player_suspensions", ["started_at"],
    )
    op.create_index(
        "ix_player_suspensions_returns_at", "player_suspensions", ["returns_at"],
    )
    op.create_index(
        "ix_player_suspensions_is_active", "player_suspensions", ["is_active"],
    )

    op.create_table(
        "training_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("session_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("participated", sa.Boolean(), nullable=False),
        sa.Column("training_load", sa.Float()),
        sa.Column(
            "limited",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "reported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "evidence_id",
            sa.Integer(),
            sa.ForeignKey("availability_evidence.id"),
        ),
        sa.UniqueConstraint("player_id", "session_at", name="uq_training_player_session"),
    )
    op.create_index(
        "ix_training_reports_player_id", "training_reports", ["player_id"],
    )
    op.create_index(
        "ix_training_reports_session_at", "training_reports", ["session_at"],
    )

    op.create_table(
        "press_conferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("gameweek_id", sa.Integer(), sa.ForeignKey("gameweeks.id")),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("transcript", sa.Text()),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_press_conferences_team_id", "press_conferences", ["team_id"],
    )
    op.create_index(
        "ix_press_conferences_gameweek_id", "press_conferences", ["gameweek_id"],
    )
    op.create_index(
        "ix_press_conferences_held_at", "press_conferences", ["held_at"],
    )

    op.create_table(
        "player_mentions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "press_conference_id",
            sa.Integer(),
            sa.ForeignKey("press_conferences.id"),
            nullable=False,
        ),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("sentiment", sa.String(50)),
        sa.Column(
            "extracted_status",
            sa.Enum(*_AVAILABILITY_STATUS, name="availabilitystatus"),
        ),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.UniqueConstraint(
            "press_conference_id", "player_id", name="uq_press_player",
        ),
    )
    op.create_index(
        "ix_player_mentions_press_conference_id",
        "player_mentions", ["press_conference_id"],
    )
    op.create_index(
        "ix_player_mentions_player_id", "player_mentions", ["player_id"],
    )


def downgrade() -> None:
    for table in [
        "player_mentions",
        "press_conferences",
        "training_reports",
        "player_suspensions",
        "player_injuries",
        "availability_events",
        "availability_evidence",
        "availability_articles",
        "availability_sources",
    ]:
        op.drop_table(table)
    # Drop the enums created for native PostgreSQL enum types.
    for enum_name in ("availabilitystatus", "evidencetype", "sourcereliability"):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))
