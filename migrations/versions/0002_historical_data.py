"""add historical data models

Revision ID: 0002_historical_data
Revises: 0001_initial
Create Date: 2026-07-31
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_historical_data"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Add columns to existing tables ---
    op.add_column("seasons", sa.Column("start_date", sa.DateTime(timezone=True)))
    op.add_column("seasons", sa.Column("end_date", sa.DateTime(timezone=True)))
    op.add_column("seasons", sa.Column("competition", sa.String(100), server_default="Premier League"))

    op.add_column("gameweeks", sa.Column("start_time", sa.DateTime(timezone=True)))
    op.add_column("gameweeks", sa.Column("end_time", sa.DateTime(timezone=True)))
    op.add_column("gameweeks", sa.Column("status", sa.String(30), server_default="scheduled"))

    op.add_column("fixtures", sa.Column("status", sa.String(30), server_default="scheduled"))
    op.add_column("fixtures", sa.Column("postponed", sa.Boolean(), server_default=sa.text("false")))

    op.add_column("ingestion_runs", sa.Column("season_code", sa.String(20)))
    op.add_column("raw_records", sa.Column("provider", sa.String(100)))
    op.add_column("raw_records", sa.Column("season_code", sa.String(20)))

    # --- Teams: add external_id support ---
    # Add a new internal-only id column as the canonical PK, keeping existing data
    # The existing teams table has provider+provider_team_id as unique constraint
    # We need to add team_external_ids table that maps to teams.id
    # First make team name nullable so we can migrate
    # Actually, teams already has an 'id' PK. We just need to create the external_ids table.
    # But we need to migrate existing provider columns into the external_ids table.
    # For simplicity, we'll keep the existing teams data as is and create the new structure.
    # The existing provider/provider_team_id columns on teams will be deprecated but kept.
    # New code will use team_external_ids.

    # --- Create new tables ---
    op.create_table(
        "team_external_ids",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("provider_team_id", sa.String(100), nullable=False),
        sa.UniqueConstraint("provider", "provider_team_id", name="uq_team_external_id"),
    )

    op.create_table(
        "player_external_ids",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("provider_player_id", sa.String(100), nullable=False),
        sa.UniqueConstraint("provider", "provider_player_id", name="uq_player_external_id"),
    )

    op.create_table(
        "player_team_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "player_id", "team_id", "season_id", "valid_from",
            name="uq_player_team_season",
        ),
    )

    op.create_table(
        "player_match_performances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("fixture_id", sa.Integer(), sa.ForeignKey("fixtures.id"), nullable=False),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("minutes", sa.Integer()),
        sa.Column("goals_scored", sa.Integer(), server_default="0"),
        sa.Column("assists", sa.Integer(), server_default="0"),
        sa.Column("clean_sheets", sa.Integer(), server_default="0"),
        sa.Column("goals_conceded", sa.Integer(), server_default="0"),
        sa.Column("own_goals", sa.Integer(), server_default="0"),
        sa.Column("penalties_saved", sa.Integer(), server_default="0"),
        sa.Column("penalties_missed", sa.Integer(), server_default="0"),
        sa.Column("yellow_cards", sa.Integer(), server_default="0"),
        sa.Column("red_cards", sa.Integer(), server_default="0"),
        sa.Column("saves", sa.Integer(), server_default="0"),
        sa.Column("bonus", sa.Integer(), server_default="0"),
        sa.Column("bps", sa.Integer(), server_default="0"),
        sa.Column("influence", sa.Float()),
        sa.Column("creativity", sa.Float()),
        sa.Column("threat", sa.Float()),
        sa.Column("ict_index", sa.Float()),
        sa.Column("expected_goals", sa.Float()),
        sa.Column("expected_assists", sa.Float()),
        sa.Column("expected_goal_involvements", sa.Float()),
        sa.Column("expected_goals_conceded", sa.Float()),
        sa.Column("total_points", sa.Integer(), server_default="0"),
        sa.Column("was_home", sa.Boolean()),
        sa.Column("kickoff_time", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("player_id", "fixture_id", name="uq_player_match"),
    )

    op.create_table(
        "player_gameweek_performances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("gameweek_id", sa.Integer(), sa.ForeignKey("gameweeks.id"), nullable=False),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("minutes", sa.Integer(), server_default="0"),
        sa.Column("goals_scored", sa.Integer(), server_default="0"),
        sa.Column("assists", sa.Integer(), server_default="0"),
        sa.Column("clean_sheets", sa.Integer(), server_default="0"),
        sa.Column("goals_conceded", sa.Integer(), server_default="0"),
        sa.Column("own_goals", sa.Integer(), server_default="0"),
        sa.Column("penalties_saved", sa.Integer(), server_default="0"),
        sa.Column("penalties_missed", sa.Integer(), server_default="0"),
        sa.Column("yellow_cards", sa.Integer(), server_default="0"),
        sa.Column("red_cards", sa.Integer(), server_default="0"),
        sa.Column("saves", sa.Integer(), server_default="0"),
        sa.Column("bonus", sa.Integer(), server_default="0"),
        sa.Column("bps", sa.Integer(), server_default="0"),
        sa.Column("influence", sa.Float()),
        sa.Column("creativity", sa.Float()),
        sa.Column("threat", sa.Float()),
        sa.Column("ict_index", sa.Float()),
        sa.Column("expected_goals", sa.Float()),
        sa.Column("expected_assists", sa.Float()),
        sa.Column("expected_goal_involvements", sa.Float()),
        sa.Column("expected_goals_conceded", sa.Float()),
        sa.Column("total_points", sa.Integer(), server_default="0"),
        sa.Column("value", sa.Integer()),
        sa.Column("transfers_balance", sa.Integer()),
        sa.Column("selected", sa.Integer()),
        sa.Column("transfers_in", sa.Integer(), server_default="0"),
        sa.Column("transfers_out", sa.Integer(), server_default="0"),
        sa.Column("loaned_in", sa.Integer(), server_default="0"),
        sa.Column("loaned_out", sa.Integer(), server_default="0"),
        sa.Column("price", sa.Float()),
        sa.Column("cost_change_event", sa.Integer()),
        sa.Column("cost_change_start", sa.Integer()),
        sa.Column("price_change", sa.Float()),
        sa.Column("price_start", sa.Float()),
        sa.Column("form", sa.Float()),
        sa.Column("form_rank", sa.Integer()),
        sa.Column("points_per_game", sa.Float()),
        sa.Column("selected_by_percent", sa.Float()),
        sa.Column("selected_rank", sa.Integer()),
        sa.Column("ep_this", sa.Float()),
        sa.Column("ep_next", sa.Float()),
        sa.UniqueConstraint("player_id", "gameweek_id", name="uq_player_gameweek"),
    )

    op.create_table(
        "team_match_performances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("fixture_id", sa.Integer(), sa.ForeignKey("fixtures.id"), nullable=False),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("is_home", sa.Boolean(), nullable=False),
        sa.Column("goals_scored", sa.Integer(), server_default="0"),
        sa.Column("goals_conceded", sa.Integer(), server_default="0"),
        sa.Column("expected_goals", sa.Float()),
        sa.Column("expected_goals_conceded", sa.Float()),
        sa.Column("expected_goal_involvements", sa.Float()),
        sa.Column("shots", sa.Integer()),
        sa.Column("shots_on_target", sa.Integer()),
        sa.Column("possession", sa.Float()),
        sa.Column("corners", sa.Integer()),
        sa.Column("fouls", sa.Integer()),
        sa.Column("yellow_cards", sa.Integer()),
        sa.Column("red_cards", sa.Integer()),
        sa.UniqueConstraint("team_id", "fixture_id", name="uq_team_match"),
    )

    op.create_table(
        "fpl_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("gameweek_id", sa.Integer(), sa.ForeignKey("gameweeks.id")),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Float()),
        sa.Column("selected_by_percent", sa.Float()),
        sa.Column("transfers_in_event", sa.Integer(), server_default="0"),
        sa.Column("transfers_out_event", sa.Integer(), server_default="0"),
        sa.Column("transfers_in_season", sa.Integer(), server_default="0"),
        sa.Column("transfers_out_season", sa.Integer(), server_default="0"),
        sa.Column("total_points", sa.Integer(), server_default="0"),
        sa.Column("form", sa.Float()),
        sa.Column("points_per_game", sa.Float()),
        sa.Column("form_rank", sa.Integer()),
        sa.Column("points_per_game_rank", sa.Integer()),
        sa.Column("selected_rank", sa.Integer()),
        sa.Column("ep_this", sa.Float()),
        sa.Column("ep_next", sa.Float()),
        sa.UniqueConstraint(
            "player_id", "gameweek_id", "event_time",
            name="uq_player_snapshot_time",
        ),
    )


def downgrade() -> None:
    for table in [
        "fpl_snapshots",
        "team_match_performances",
        "player_gameweek_performances",
        "player_match_performances",
        "player_team_memberships",
        "player_external_ids",
        "team_external_ids",
    ]:
        op.drop_table(table)

    op.drop_column("raw_records", "season_code")
    op.drop_column("raw_records", "provider")
    op.drop_column("ingestion_runs", "season_code")
    op.drop_column("fixtures", "postponed")
    op.drop_column("fixtures", "status")
    op.drop_column("gameweeks", "status")
    op.drop_column("gameweeks", "end_time")
    op.drop_column("gameweeks", "start_time")
    op.drop_column("seasons", "competition")
    op.drop_column("seasons", "end_date")
    op.drop_column("seasons", "start_date")