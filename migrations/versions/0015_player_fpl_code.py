"""phase14.0: FPL element code on player

Adds ``fpl_code`` (nullable Integer) to the ``players`` table so the dashboard
can build Premier-League-CDN photo URLs (`.../photos/players/110x140/{code}.png`)
without a separate lookup. The column is nullable: existing rows keep working and
real codes are captured the next time ``ingest_bootstrap`` runs against the live
FPL bootstrap-static endpoint.

Revision ID: 0015_player_fpl_code
Revises: 0014_pending_sync
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_player_fpl_code"
down_revision = "0014_pending_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("players", sa.Column("fpl_code", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("players", "fpl_code")