"""phase20.1: materialized read models for the prod incident

Four new tables backing the "materialize, don't compute per request"
architecture:

* ``fixtures_cache``      — raw official fixtures array (JSON, vaastav source)
* ``news_cache``          — BBC Sport RSS headlines (JSON)
* ``element_facts``       — per-element snapshot facts from players_raw.csv
* ``predictions_current`` — precomputed per-player xPTS, next 5 GWs

None of these touch Phase 1-19 tables.

Revision ID: 0018_materialized_read_models
Revises: 0017_sync_track_record
Create Date: 2026-08-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_materialized_read_models"
down_revision = "0017_sync_track_record"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fixtures_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "news_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("headline_count", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "element_facts",
        sa.Column("element_id", sa.Integer(), primary_key=True),
        sa.Column("web_name", sa.String(120), nullable=True),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("minutes", sa.Integer(), nullable=True),
        sa.Column("selected_by_percent", sa.String(20), nullable=True),
        sa.Column("cost_change_event", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=True),
        sa.Column("news", sa.String(500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "predictions_current",
        sa.Column("gameweek", sa.Integer(), primary_key=True),
        sa.Column("element_id", sa.Integer(), primary_key=True),
        sa.Column("expected_points", sa.Float(), nullable=False),
        sa.Column("minutes_estimate", sa.Float(), nullable=True),
        sa.Column("start_prob", sa.Float(), nullable=True),
        sa.Column("xg_per_90", sa.Float(), nullable=True),
        sa.Column("xa_per_90", sa.Float(), nullable=True),
        sa.Column("source", sa.String(60), nullable=True),
        sa.Column("data_quality", sa.String(60), nullable=True),
        sa.Column("breakdown", sa.JSON(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("gameweek", "element_id", name="uq_pred_current_gw_element"),
    )
    op.create_index(
        "ix_predictions_current_element",
        "predictions_current",
        ["element_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_predictions_current_element", table_name="predictions_current")
    op.drop_table("predictions_current")
    op.drop_table("element_facts")
    op.drop_table("news_cache")
    op.drop_table("fixtures_cache")
