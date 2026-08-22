"""v1.5.1: official FPL element ID on player

Adds ``fpl_element_id`` (nullable, unique, indexed Integer) to the ``players``
table. ``POST /api/v1/squad/from-fpl`` stores raw FPL element IDs as squad
player_ids, and those ids must resolve to the correct players when rendering
decisions. Previously there was only the indirect ``player_external_ids``
mapping, and the enrichment path joined element ids against our internal
auto-increment ``players.id`` — resolving e.g. Haaland (element 445) to
whatever player happened to occupy internal row 445.

The column is nullable: existing rows keep working and real element ids are
backfilled the next time the seed replay / ``ingest_bootstrap`` runs.

Revision ID: 0016_player_fpl_element_id
Revises: 0015_player_fpl_code
Create Date: 2026-08-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_player_fpl_element_id"
down_revision = "0015_player_fpl_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("players", sa.Column("fpl_element_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_players_fpl_element_id"),
        "players",
        ["fpl_element_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_players_fpl_element_id"), table_name="players")
    op.drop_column("players", "fpl_element_id")