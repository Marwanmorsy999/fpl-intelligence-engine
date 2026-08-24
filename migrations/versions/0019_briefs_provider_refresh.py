"""phase21.1: assistant briefs persistence + provider refresh store

Two new tables:

* ``assistant_briefs``  — persisted pre-generated briefs (daily cron writes,
                          request paths read; survives serverless cold starts)
* ``provider_refresh``  — refreshed provider snapshots fetched via the egress
                          masks (Understat 2026/27), latest row per source

Revision ID: 0019_briefs_provider_refresh
Revises: 0018_materialized_read_models
Create Date: 2026-08-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_briefs_provider_refresh"
down_revision = "0018_materialized_read_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_briefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.String(255), nullable=False),
        sa.Column("gameweek", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "gameweek", name="uq_brief_session_gw"),
    )
    op.create_index("ix_assistant_briefs_session", "assistant_briefs", ["session_id"])

    op.create_table(
        "provider_refresh",
        sa.Column("source", sa.String(60), primary_key=True),
        sa.Column("season_label", sa.String(40), nullable=True),
        sa.Column("player_count", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("provider_refresh")
    op.drop_index("ix_assistant_briefs_session", table_name="assistant_briefs")
    op.drop_table("assistant_briefs")
