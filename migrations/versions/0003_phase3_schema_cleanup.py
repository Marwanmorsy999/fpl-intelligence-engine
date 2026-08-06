"""phase3: schema cleanup, indexes, temporal fields

Cleans up orphaned columns from migration 0001, adds indexes on foreign key
columns, and adds temporal fields (available_at, ingested_at, source_last_modified_at)
to support historical backtesting with strict no-look-ahead enforcement.

Revision ID: 0003_phase3_schema_cleanup
Revises: 0002_historical_data
Create Date: 2026-07-31
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_phase3_schema_cleanup"
down_revision = "0002_historical_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ============================================================
    # 1. Drop orphaned columns from migration 0001
    # ============================================================

    # teams: provider, provider_team_id (moved to team_external_ids)
    with op.batch_alter_table("teams") as batch_op:
        batch_op.drop_column("provider")
        batch_op.drop_column("provider_team_id")

    # players: provider, provider_player_id, current_team_id (moved to player_external_ids)
    with op.batch_alter_table("players") as batch_op:
        batch_op.drop_column("provider")
        batch_op.drop_column("provider_player_id")
        batch_op.drop_column("current_team_id")

    # ============================================================
    # 2. Add indexes on foreign key columns
    # ============================================================

    # team_external_ids (team_id already indexed if batch added it, but ensure)
    op.create_index(op.f("ix_team_external_ids_team_id"), "team_external_ids", ["team_id"])

    # player_external_ids
    op.create_index(op.f("ix_player_external_ids_player_id"), "player_external_ids", ["player_id"])

    # player_team_memberships
    op.create_index(op.f("ix_player_team_memberships_player_id"), "player_team_memberships", ["player_id"])
    op.create_index(op.f("ix_player_team_memberships_team_id"), "player_team_memberships", ["team_id"])
    op.create_index(op.f("ix_player_team_memberships_season_id"), "player_team_memberships", ["season_id"])

    # gameweeks
    op.create_index(op.f("ix_gameweeks_season_id"), "gameweeks", ["season_id"])

    # fixtures
    op.create_index(op.f("ix_fixtures_season_id"), "fixtures", ["season_id"])
    op.create_index(op.f("ix_fixtures_gameweek_id"), "fixtures", ["gameweek_id"])
    op.create_index(op.f("ix_fixtures_home_team_id"), "fixtures", ["home_team_id"])
    op.create_index(op.f("ix_fixtures_away_team_id"), "fixtures", ["away_team_id"])

    # player_match_performances
    op.create_index(op.f("ix_player_match_performances_player_id"), "player_match_performances", ["player_id"])
    op.create_index(op.f("ix_player_match_performances_fixture_id"), "player_match_performances", ["fixture_id"])
    op.create_index(op.f("ix_player_match_performances_season_id"), "player_match_performances", ["season_id"])
    op.create_index(op.f("ix_player_match_performances_team_id"), "player_match_performances", ["team_id"])

    # player_gameweek_performances
    op.create_index(op.f("ix_player_gameweek_performances_player_id"), "player_gameweek_performances", ["player_id"])
    op.create_index(op.f("ix_player_gameweek_performances_gameweek_id"), "player_gameweek_performances", ["gameweek_id"])
    op.create_index(op.f("ix_player_gameweek_performances_season_id"), "player_gameweek_performances", ["season_id"])
    op.create_index(op.f("ix_player_gameweek_performances_team_id"), "player_gameweek_performances", ["team_id"])

    # team_match_performances
    op.create_index(op.f("ix_team_match_performances_team_id"), "team_match_performances", ["team_id"])
    op.create_index(op.f("ix_team_match_performances_fixture_id"), "team_match_performances", ["fixture_id"])
    op.create_index(op.f("ix_team_match_performances_season_id"), "team_match_performances", ["season_id"])

    # fpl_snapshots
    op.create_index(op.f("ix_fpl_snapshots_player_id"), "fpl_snapshots", ["player_id"])
    op.create_index(op.f("ix_fpl_snapshots_season_id"), "fpl_snapshots", ["season_id"])
    op.create_index(op.f("ix_fpl_snapshots_gameweek_id"), "fpl_snapshots", ["gameweek_id"])

    # ============================================================
    # 3. Add temporal fields
    # ============================================================

    # FPLSnapshot: available_at, source_last_modified_at
    op.add_column("fpl_snapshots", sa.Column("available_at", sa.DateTime(timezone=True)))
    op.add_column("fpl_snapshots", sa.Column("source_last_modified_at", sa.DateTime(timezone=True)))

    # PlayerMatchPerformance: ingested_at, available_at
    op.add_column("player_match_performances", sa.Column("ingested_at", sa.DateTime(timezone=True)))
    op.add_column("player_match_performances", sa.Column("available_at", sa.DateTime(timezone=True)))

    # PlayerGameweekPerformance: ingested_at, available_at
    op.add_column("player_gameweek_performances", sa.Column("ingested_at", sa.DateTime(timezone=True)))
    op.add_column("player_gameweek_performances", sa.Column("available_at", sa.DateTime(timezone=True)))

    # TeamMatchPerformance: ingested_at, available_at
    op.add_column("team_match_performances", sa.Column("ingested_at", sa.DateTime(timezone=True)))
    op.add_column("team_match_performances", sa.Column("available_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    # ============================================================
    # 1. Remove temporal fields
    # ============================================================

    op.drop_column("team_match_performances", "available_at")
    op.drop_column("team_match_performances", "ingested_at")
    op.drop_column("player_gameweek_performances", "available_at")
    op.drop_column("player_gameweek_performances", "ingested_at")
    op.drop_column("player_match_performances", "available_at")
    op.drop_column("player_match_performances", "ingested_at")
    op.drop_column("fpl_snapshots", "source_last_modified_at")
    op.drop_column("fpl_snapshots", "available_at")

    # ============================================================
    # 2. Drop indexes
    # ============================================================

    op.drop_index(op.f("ix_fpl_snapshots_gameweek_id"), table_name="fpl_snapshots")
    op.drop_index(op.f("ix_fpl_snapshots_season_id"), table_name="fpl_snapshots")
    op.drop_index(op.f("ix_fpl_snapshots_player_id"), table_name="fpl_snapshots")

    op.drop_index(op.f("ix_team_match_performances_season_id"), table_name="team_match_performances")
    op.drop_index(op.f("ix_team_match_performances_fixture_id"), table_name="team_match_performances")
    op.drop_index(op.f("ix_team_match_performances_team_id"), table_name="team_match_performances")

    op.drop_index(op.f("ix_player_gameweek_performances_team_id"), table_name="player_gameweek_performances")
    op.drop_index(op.f("ix_player_gameweek_performances_season_id"), table_name="player_gameweek_performances")
    op.drop_index(op.f("ix_player_gameweek_performances_gameweek_id"), table_name="player_gameweek_performances")
    op.drop_index(op.f("ix_player_gameweek_performances_player_id"), table_name="player_gameweek_performances")

    op.drop_index(op.f("ix_player_match_performances_team_id"), table_name="player_match_performances")
    op.drop_index(op.f("ix_player_match_performances_season_id"), table_name="player_match_performances")
    op.drop_index(op.f("ix_player_match_performances_fixture_id"), table_name="player_match_performances")
    op.drop_index(op.f("ix_player_match_performances_player_id"), table_name="player_match_performances")

    op.drop_index(op.f("ix_fixtures_away_team_id"), table_name="fixtures")
    op.drop_index(op.f("ix_fixtures_home_team_id"), table_name="fixtures")
    op.drop_index(op.f("ix_fixtures_gameweek_id"), table_name="fixtures")
    op.drop_index(op.f("ix_fixtures_season_id"), table_name="fixtures")

    op.drop_index(op.f("ix_gameweeks_season_id"), table_name="gameweeks")

    op.drop_index(op.f("ix_player_team_memberships_season_id"), table_name="player_team_memberships")
    op.drop_index(op.f("ix_player_team_memberships_team_id"), table_name="player_team_memberships")
    op.drop_index(op.f("ix_player_team_memberships_player_id"), table_name="player_team_memberships")

    op.drop_index(op.f("ix_player_external_ids_player_id"), table_name="player_external_ids")
    op.drop_index(op.f("ix_team_external_ids_team_id"), table_name="team_external_ids")

    # ============================================================
    # 3. Restore orphaned columns
    # ============================================================

    with op.batch_alter_table("players") as batch_op:
        batch_op.add_column(sa.Column("current_team_id", sa.Integer(), sa.ForeignKey("teams.id")))
        batch_op.add_column(sa.Column("provider_player_id", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("provider", sa.String(100), nullable=False, server_default=""))

    with op.batch_alter_table("teams") as batch_op:
        batch_op.add_column(sa.Column("provider_team_id", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("provider", sa.String(100), nullable=False, server_default=""))