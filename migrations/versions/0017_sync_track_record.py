"""phase19.0: sync trio, track record and prediction ledger

Five new tables backing the real-system sync layer:

* ``sync_live_points``   — matchday live points per element (Apps Script push)
* ``ingested_history``   — finalised vaastav/Understat GW results (GH Actions)
* ``recommendation``     — every engine call, auto-scored after ingestion
* ``prediction_ledger``  — predicted vs actual per element/GW (calibration)
* ``sync_log``           — audit trail for /api/v1/sync/status

None of these touch Phase 1-18 tables.

Revision ID: 0017_sync_track_record
Revises: 0016_player_fpl_element_id
Create Date: 2026-08-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_sync_track_record"
down_revision = "0016_player_fpl_element_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_live_points",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gameweek", sa.Integer(), nullable=False),
        sa.Column("element_id", sa.Integer(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=True),
        sa.Column("fixture_text", sa.String(120), nullable=True),
        sa.Column("opponent", sa.String(60), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("gameweek", "element_id", name="uq_sync_live_gw_element"),
    )
    op.create_index("ix_sync_live_points_gameweek", "sync_live_points", ["gameweek"])
    op.create_index("ix_sync_live_points_element_id", "sync_live_points", ["element_id"])

    op.create_table(
        "ingested_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gameweek", sa.Integer(), nullable=False),
        sa.Column("element_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("total_points", sa.Integer(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=True),
        sa.Column("bonus", sa.Integer(), nullable=True),
        sa.Column("goals_scored", sa.Integer(), nullable=True),
        sa.Column("assists", sa.Integer(), nullable=True),
        sa.Column("xgi", sa.Float(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("gameweek", "element_id", name="uq_ingested_gw_element"),
    )
    op.create_index("ix_ingested_history_gameweek", "ingested_history", ["gameweek"])
    op.create_index("ix_ingested_history_element_id", "ingested_history", ["element_id"])

    op.create_table(
        "recommendation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_key", sa.String(255), nullable=False),
        sa.Column("gameweek", sa.Integer(), nullable=False),
        sa.Column("rec_type", sa.String(30), nullable=False),
        sa.Column("subject", sa.JSON(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("score", sa.JSON(), nullable=True),
    )
    op.create_index("ix_recommendation_session_key", "recommendation", ["session_key"])
    op.create_index("ix_recommendation_gameweek", "recommendation", ["gameweek"])

    op.create_table(
        "prediction_ledger",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gameweek", sa.Integer(), nullable=False),
        sa.Column("element_id", sa.Integer(), nullable=False),
        sa.Column("predicted", sa.Float(), nullable=False),
        sa.Column("actual", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("gameweek", "element_id", name="uq_pred_ledger_gw_element"),
    )
    op.create_index("ix_prediction_ledger_gameweek", "prediction_ledger", ["gameweek"])
    op.create_index("ix_prediction_ledger_element_id", "prediction_ledger", ["element_id"])

    op.create_table(
        "sync_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("entry_id", sa.String(255), nullable=True),
        sa.Column("gameweek", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sync_log")
    op.drop_index("ix_prediction_ledger_element_id", table_name="prediction_ledger")
    op.drop_index("ix_prediction_ledger_gameweek", table_name="prediction_ledger")
    op.drop_table("prediction_ledger")
    op.drop_index("ix_recommendation_gameweek", table_name="recommendation")
    op.drop_index("ix_recommendation_session_key", table_name="recommendation")
    op.drop_table("recommendation")
    op.drop_index("ix_ingested_history_element_id", table_name="ingested_history")
    op.drop_index("ix_ingested_history_gameweek", table_name="ingested_history")
    op.drop_table("ingested_history")
    op.drop_index("ix_sync_live_points_element_id", table_name="sync_live_points")
    op.drop_index("ix_sync_live_points_gameweek", table_name="sync_live_points")
    op.drop_table("sync_live_points")
