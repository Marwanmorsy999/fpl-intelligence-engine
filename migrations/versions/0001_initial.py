"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-31
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("source_key", sa.String(200), nullable=False, unique=True),
        sa.Column("base_url", sa.String(500)),
    )
    op.create_table(
        "seasons",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(20), nullable=False, unique=True),
        sa.Column("display_name", sa.String(50), nullable=False),
    )
    op.create_table(
        "teams",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("provider_team_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("short_name", sa.String(10)),
        sa.UniqueConstraint("provider", "provider_team_id", name="uq_team_provider_id"),
    )
    op.create_table(
        "players",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("provider_player_id", sa.Integer(), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("second_name", sa.String(100), nullable=False),
        sa.Column("web_name", sa.String(100), nullable=False),
        sa.Column("position_code", sa.Integer()),
        sa.Column("current_team_id", sa.Integer(), sa.ForeignKey("teams.id")),
        sa.UniqueConstraint("provider", "provider_player_id", name="uq_player_provider_id"),
    )
    op.create_table(
        "gameweeks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("provider_event_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("deadline_time", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("season_id", "provider_event_id", name="uq_gameweek_season_event"),
    )
    op.create_table(
        "fixtures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("provider_fixture_id", sa.Integer(), nullable=False),
        sa.Column("gameweek_id", sa.Integer(), sa.ForeignKey("gameweeks.id")),
        sa.Column("kickoff_time", sa.DateTime(timezone=True)),
        sa.Column("home_team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("away_team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("home_score", sa.Integer()),
        sa.Column("away_score", sa.Integer()),
        sa.UniqueConstraint("season_id", "provider_fixture_id", name="uq_fixture_season_provider"),
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("job_name", sa.String(150), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("records_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text()),
    )
    op.create_table(
        "raw_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("endpoint", sa.String(300), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("source", "endpoint", "payload_hash", name="uq_raw_payload"),
    )


def downgrade() -> None:
    for table in ["raw_records", "ingestion_runs", "fixtures", "gameweeks", "players", "teams", "seasons", "data_sources"]:
        op.drop_table(table)
