"""Stage 2B.1: venue-specific team strength dimensions.

Revision ID: 0022_team_strength_dimensions
Revises: 0021_local_squad_state
"""

from alembic import op
import sqlalchemy as sa

revision = "0022_team_strength_dimensions"
down_revision = "0021_local_squad_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in (
        "home_attack_strength",
        "away_attack_strength",
        "home_defence_strength",
        "away_defence_strength",
    ):
        op.add_column("team_strengths", sa.Column(name, sa.Float(), nullable=True))


def downgrade() -> None:
    for name in (
        "away_defence_strength",
        "home_defence_strength",
        "away_attack_strength",
        "home_attack_strength",
    ):
        op.drop_column("team_strengths", name)