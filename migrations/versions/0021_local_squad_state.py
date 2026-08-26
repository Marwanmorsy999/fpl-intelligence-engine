"""v2.7.3-dual-state: local_squad_state — Transfer Planner local override

The base squad_state remains the last FPL-imported truth (from-fpl / squad-push / sync-now).
local_squad_state is written ONLY by the Transfer Planner's "Save to Local Squad"
action — no FPL fetch, no egress mask — and is the source of truth for every
user-facing math path (decisions, captaincy, Alpha, Horizon Planner, Trajectory,
FOMO). When absent the effective squad falls back to the base row.

Revision ID: 0021_local_squad_state
Revises: 0020_element_facts_now_cost
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_local_squad_state"
down_revision = "0020_element_facts_now_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "local_squad_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.String(255), nullable=False),
        sa.Column("squad_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", name="uq_local_squad_state_session"),
    )
    op.create_index(
        "ix_local_squad_state_session_id", "local_squad_state", ["session_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_local_squad_state_session_id", table_name="local_squad_state")
    op.drop_table("local_squad_state")
