"""phase13.5: pending auto-sync queue

Adds the ``pending_sync`` table that stores a queued FPL squad auto-sync
request: when ``POST /api/v1/squad/from-fpl`` fails with a transient 503 the
manager's ``entry_id`` is recorded here (with ``auto_sync=true``) so both the
existing run-scheduler cron and the public ``/api/v1/squad/retry-sync``
endpoint can retry the import later. ``status`` tracks PENDING / SYNCED /
FAILED.

This table does **not** touch any Phase 1–12 table.

Revision ID: 0014_pending_sync
Revises: 0013_squad_state_persistence
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_pending_sync"
down_revision = "0013_squad_state_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_sync",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("auto_sync", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pending_sync_entry_id", "pending_sync", ["entry_id"])


def downgrade() -> None:
    op.drop_index("ix_pending_sync_entry_id", table_name="pending_sync")
    op.drop_table("pending_sync")
