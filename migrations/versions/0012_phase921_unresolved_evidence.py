"""phase9.2.1: unresolved live evidence persistence

Adds the Phase 9-owned ``unresolved_live_evidence`` table. Live evidence
ingestion is robust against unresolved entities: when the extractor names a
player/team that resolves to no canonical id (or is ambiguous), the raw item
survives and the gap is recorded here for triage rather than silently dropped.

This table does **not** touch any Phase 7 table. It carries a single new native
enum ``resolutionstatus`` (created once, idempotently) plus FKs back to the
Phase 9.1 ledger / extraction-run tables.

Revision ID: 0012_phase921_unresolved_evidence
Revises: 0011_phase912_availability_enum
Create Date: 2026-08-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_phase921_unresolved_evidence"
down_revision = "0011_phase912_availability_enum"
branch_labels = None
depends_on = None

_RESOLUTION_STATUS = [
    "resolved",
    "resolved_by_external_id",
    "resolved_by_name_team",
    "resolved_by_name_unique",
    "resolved_by_alias",
    "unresolved_player",
    "unresolved_team",
    "ambiguous_player",
]


def _enum_ref(bind: sa.engine.Connection, values: list[str], type_name: str) -> sa.types.TypeEngine:
    """Reference a native PostgreSQL enum without re-emitting CREATE TYPE.

    The enum type is created exactly once, explicitly, above. Column definitions
    must only *reference* it, so ``create_type=False`` is set on the
    dialect-specific :class:`postgresql.ENUM`. Non-PostgreSQL dialects render
    ``sa.Enum`` as VARCHAR plus a CHECK, emitting no CREATE TYPE, so the plain
    generic type is already safe there.
    """
    if bind.dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=type_name, create_type=False)
    return sa.Enum(*values, name=type_name)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "ALTER TABLE alembic_version "
                "ALTER COLUMN version_num TYPE VARCHAR(64)"
            )
        )
    sa.Enum(*_RESOLUTION_STATUS, name="resolutionstatus").create(bind, checkfirst=True)
    resolution_status = _enum_ref(bind, _RESOLUTION_STATUS, "resolutionstatus")

    op.create_table(
        "unresolved_live_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "raw_item_id",
            sa.Integer(),
            sa.ForeignKey("live_intelligence_raw_items.id"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("live_intelligence_sources.id"),
            nullable=False,
        ),
        sa.Column(
            "extraction_run_id",
            sa.Integer(),
            sa.ForeignKey("llm_extraction_runs.id"),
            nullable=True,
        ),
        sa.Column("evidence_type", sa.String(50), nullable=True),
        sa.Column("player_name", sa.String(200), nullable=True),
        sa.Column("team_name", sa.String(200), nullable=True),
        sa.Column("status_mentioned", sa.String(50), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("prompt_hash", sa.String(64), nullable=True),
        sa.Column("provider_name", sa.String(100), nullable=True),
        sa.Column("team_hint", sa.String(200), nullable=True),
        sa.Column(
            "resolution_status",
            resolution_status,
            nullable=False,
            server_default="unresolved_player",
        ),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_unresolved_live_evidence_raw_item_id",
        "unresolved_live_evidence",
        ["raw_item_id"],
    )
    op.create_index(
        "ix_unresolved_live_evidence_source_id",
        "unresolved_live_evidence",
        ["source_id"],
    )
    op.create_index(
        "ix_unresolved_live_evidence_run_id",
        "unresolved_live_evidence",
        ["extraction_run_id"],
    )
    op.create_index(
        "ix_unresolved_live_evidence_evidence_type",
        "unresolved_live_evidence",
        ["evidence_type"],
    )
    op.create_index(
        "ix_unresolved_live_evidence_prompt_hash",
        "unresolved_live_evidence",
        ["prompt_hash"],
    )
    op.create_index(
        "ix_unresolved_evidence_run_type",
        "unresolved_live_evidence",
        ["extraction_run_id", "evidence_type"],
    )


def downgrade() -> None:
    op.drop_table("unresolved_live_evidence")
    op.execute(sa.text("DROP TYPE IF EXISTS resolutionstatus"))
