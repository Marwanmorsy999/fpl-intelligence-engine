"""phase4: prediction layer tables

Creates tables for the prediction layer: model registry, immutable
model predictions, team strength estimates, and match predictions.

Revision ID: 0005_phase4_prediction_models
Revises: 0004_phase3_feature_store
Create Date: 2026-08-01
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_phase4_prediction_models"
down_revision = "0004_phase3_feature_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_registry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("model_version", sa.String(20), nullable=False),
        sa.Column("model_type", sa.String(50)),
        sa.Column("feature_version", sa.String(20)),
        sa.Column("training_cutoff", sa.DateTime(timezone=True)),
        sa.Column("training_start", sa.DateTime(timezone=True)),
        sa.Column("training_end", sa.DateTime(timezone=True)),
        sa.Column("hyperparameters", sa.JSON()),
        sa.Column("random_seed", sa.Integer()),
        sa.Column("training_sample_count", sa.Integer()),
        sa.Column("metrics", sa.JSON()),
        sa.Column("artifact_location", sa.Text()),
        sa.Column("status", sa.String(30), server_default="staged"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("model_name", "model_version", name="uq_model_registry_name_version"),
    )
    op.create_index("ix_model_registry_model_name", "model_registry", ["model_name"])

    op.create_table(
        "model_predictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("model_version", sa.String(20), nullable=False),
        sa.Column("feature_version", sa.String(20)),
        sa.Column("cutoff_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("prediction_value", sa.Float()),
        sa.Column("prediction_lower", sa.Float()),
        sa.Column("prediction_upper", sa.Float()),
        sa.Column("prediction_data", sa.JSON()),
        sa.Column("confidence", sa.Float()),
        sa.Column("data_completeness", sa.Float()),
        sa.Column("prediction_timestamp", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("is_frozen", sa.Boolean(), server_default=sa.text("true")),
    )
    op.create_index("ix_model_predictions_cutoff_time", "model_predictions", ["cutoff_time"])
    op.create_index("ix_model_predictions_entity_id", "model_predictions", ["entity_id"])
    op.create_index("ix_model_predictions_model_name", "model_predictions", ["model_name"])
    op.create_index(
        "ix_model_predictions_lookup",
        "model_predictions",
        ["model_name", "model_version", "entity_type", "entity_id", "cutoff_time"],
    )

    op.create_table(
        "team_strengths",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("season", sa.String(20), nullable=False),
        sa.Column("cutoff_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_version", sa.String(20)),
        sa.Column("attack_strength", sa.Float()),
        sa.Column("defence_strength", sa.Float()),
        sa.Column("home_strength", sa.Float()),
        sa.Column("away_strength", sa.Float()),
        sa.Column("sample_size", sa.Integer()),
        sa.Column("completeness", sa.Float()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "team_id", "season", "cutoff_time", "feature_version",
            name="uq_team_strength_cutoff",
        ),
    )
    op.create_index("ix_team_strengths_team_id", "team_strengths", ["team_id"])
    op.create_index("ix_team_strengths_cutoff_time", "team_strengths", ["cutoff_time"])

    op.create_table(
        "match_predictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fixture_id", sa.Integer(), sa.ForeignKey("fixtures.id"), nullable=False),
        sa.Column("season", sa.String(20), nullable=False),
        sa.Column("cutoff_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_version", sa.String(20)),
        sa.Column("model_name", sa.String(100)),
        sa.Column("model_version", sa.String(20)),
        sa.Column("expected_home_goals", sa.Float()),
        sa.Column("expected_away_goals", sa.Float()),
        sa.Column("home_win_probability", sa.Float()),
        sa.Column("draw_probability", sa.Float()),
        sa.Column("away_win_probability", sa.Float()),
        sa.Column("home_clean_sheet_probability", sa.Float()),
        sa.Column("away_clean_sheet_probability", sa.Float()),
        sa.Column("scoreline_distribution", sa.JSON()),
        sa.Column("simulation_count", sa.Integer()),
        sa.Column("random_seed", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "fixture_id", "cutoff_time", "model_name", "model_version",
            name="uq_match_prediction_fixture_cutoff",
        ),
    )
    op.create_index("ix_match_predictions_fixture_id", "match_predictions", ["fixture_id"])
    op.create_index("ix_match_predictions_cutoff_time", "match_predictions", ["cutoff_time"])


def downgrade() -> None:
    op.drop_table("match_predictions")
    op.drop_table("team_strengths")
    op.drop_table("model_predictions")
    op.drop_table("model_registry")

