"""phase11.2: squad state persistence

Adds the ``squad_state`` table that backs the Phase 11.2 PostgreSQL-persisted
:class:`~fpl_intelligence.squad.service.SquadService`. The full
:class:`~fpl_intelligence.squad.models.SquadStateResponse` payload is stored as
JSON against a unique ``session_id`` key, so the personalised squad survives
restarts and is shared safely across multiple workers.

This table does **not** touch any Phase 1–10 table.

Revision ID: 0013_squad_state_persistence
Revises: 0012_phase921_unresolved_evidence
Create Date: 2026-08-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_squad_state_persistence"
down_revision = "0012_phase921_unresolved_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "squad_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.String(255), nullable=False),
        sa.Column("squad_json", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.UniqueConstraint("session_id", name="uq_squad_state_session"),
    )
    op.create_index(
        "ix_squad_state_session_id", "squad_state", ["session_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_squad_state_session_id", table_name="squad_state")
    op.drop_table("squad_state")


