"""phase3: feature store and backtest tables

Creates tables for the feature store and backtest engine.

Revision ID: 0004_phase3_feature_store
Revises: 0003_phase3_schema_cleanup
Create Date: 2026-07-31
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_phase3_feature_store"
down_revision = "0003_phase3_schema_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ============================================================
    # Feature Store Tables
    # ============================================================

    op.create_table(
        "feature_definitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("feature_name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("data_type", sa.String(50), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("calculation_method", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.UniqueConstraint("feature_name", "version", name="uq_feature_name_version"),
    )
    op.create_index("ix_feature_definitions_feature_name", "feature_definitions", ["feature_name"])

    op.create_table(
        "feature_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("feature_name", sa.String(200), nullable=False),
        sa.Column("feature_version", sa.String(20), nullable=False),
        sa.Column("cutoff_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.JSON()),
        sa.Column("is_missing", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("completeness_score", sa.Float()),
        sa.Column("source_count", sa.Integer()),
        sa.Column("latest_source_time", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "entity_id", "feature_name", "feature_version", "cutoff_time",
            name="uq_feature_snapshot_entity_cutoff",
        ),
    )
    op.create_index("ix_feature_snapshots_entity_id", "feature_snapshots", ["entity_id"])
    op.create_index("ix_feature_snapshots_feature_name", "feature_snapshots", ["feature_name"])
    op.create_index("ix_feature_snapshots_cutoff_time", "feature_snapshots", ["cutoff_time"])
    op.create_index(
        "ix_feature_snapshot_lookup",
        "feature_snapshots",
        ["feature_name", "feature_version", "cutoff_time"],
    )

    op.create_table(
        "feature_lineage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("feature_name", sa.String(200), nullable=False),
        sa.Column("feature_version", sa.String(20), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("source_table", sa.String(200), nullable=False),
        sa.Column("source_record_ids", sa.JSON()),
        sa.Column("calculation_version", sa.String(20), nullable=False),
        sa.Column("cutoff_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_feature_lineage_feature_name", "feature_lineage", ["feature_name"])
    op.create_index(
        "ix_feature_lineage_lookup",
        "feature_lineage",
        ["feature_name", "entity_id", "cutoff_time"],
    )

    # ============================================================
    # Backtest Engine Tables
    # ============================================================

    op.create_table(
        "backtest_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("season", sa.String(20), nullable=False),
        sa.Column("start_gameweek", sa.Integer(), nullable=False),
        sa.Column("end_gameweek", sa.Integer(), nullable=False),
        sa.Column("decision_timing", sa.String(50), server_default="deadline"),
        sa.Column("information_access_policy", sa.String(50), server_default="strict_reproducibility"),
        sa.Column("feature_version", sa.String(20), server_default="1.0.0"),
        sa.Column("model_version", sa.String(20), server_default="baseline"),
        sa.Column("random_seed", sa.Integer()),
        sa.Column("simulation_count", sa.Integer(), server_default="1"),
        sa.Column("config_data", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False, unique=True),
        sa.Column("config_id", sa.Integer(), sa.ForeignKey("backtest_configs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.String(30), server_default="running"),
        sa.Column("feature_version", sa.String(20), server_default="1.0.0"),
        sa.Column("model_version", sa.String(20), server_default="baseline"),
        sa.Column("error_summary", sa.Text()),
    )
    op.create_index("ix_backtest_runs_config_id", "backtest_runs", ["config_id"])
    op.create_index("ix_backtest_runs_run_id", "backtest_runs", ["run_id"])

    op.create_table(
        "backtest_gameweek_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("backtest_runs.id"), nullable=False),
        sa.Column("season", sa.String(20), nullable=False),
        sa.Column("gameweek", sa.Integer(), nullable=False),
        sa.Column("decision_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("predictions", sa.JSON()),
        sa.Column("actual_outcomes", sa.JSON()),
        sa.Column("evaluation_metrics", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_backtest_gw_results_run_id", "backtest_gameweek_results", ["run_id"])
    op.create_index(
        "ix_backtest_gw_results_run_gw",
        "backtest_gameweek_results",
        ["run_id", "season", "gameweek"],
    )

    op.create_table(
        "player_predictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("backtest_runs.id"), nullable=False),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("fixture_id", sa.Integer(), sa.ForeignKey("fixtures.id")),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("predicted_expected_points", sa.Float()),
        sa.Column("prediction_interval_lower", sa.Float()),
        sa.Column("prediction_interval_upper", sa.Float()),
        sa.Column("feature_version", sa.String(20), server_default="1.0.0"),
        sa.Column("model_version", sa.String(20), server_default="baseline"),
        sa.Column("confidence", sa.Float()),
        sa.Column("data_completeness", sa.Float()),
        sa.Column("is_frozen", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_player_predictions_run_id", "player_predictions", ["run_id"])
    op.create_index("ix_player_predictions_player_id", "player_predictions", ["player_id"])
    op.create_index("ix_player_predictions_fixture_id", "player_predictions", ["fixture_id"])
    op.create_index(
        "ix_player_predictions_run_player",
        "player_predictions",
        ["run_id", "player_id"],
    )


def downgrade() -> None:
    op.drop_table("player_predictions")
    op.drop_table("backtest_gameweek_results")
    op.drop_table("backtest_runs")
    op.drop_table("backtest_configs")
    op.drop_table("feature_lineage")
    op.drop_table("feature_snapshots")
    op.drop_table("feature_definitions")