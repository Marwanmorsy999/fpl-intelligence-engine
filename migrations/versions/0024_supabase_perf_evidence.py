"""v2.7.11 — Supabase performance + integrity evidence (issue #24).

Revision ID: 0024_supabase_perf_evidence
Revises: 0023_performance_security_cleanup
Create Date: 2026-09-03

Captures three evidence-justified DDL changes for the Supabase
production schema. The PK change must replace the existing UNIQUE
constraint with a PRIMARY KEY because PostgreSQL does not allow an
index already owned by a UNIQUE constraint to be reused directly
with ``PRIMARY KEY USING INDEX``.

1. ``predictions_current`` PRIMARY KEY on ``(gameweek, element_id)``.

   Build a temporary standalone unique index, drop the existing UNIQUE
   constraint, then promote that standalone index to the PRIMARY KEY.
   The PK preserves the same uniqueness invariant.

2. ``predictions_current_computed_at_idx`` on
   ``predictions_current(computed_at)``.

3. ``ix_availability_events_primary_source_id`` on
   ``availability_events(primary_source_id)``.

Downgrade restores the original UNIQUE constraint after dropping the
PRIMARY KEY and removes the two new performance indexes.
"""

from __future__ import annotations

from alembic import op

revision = "0024_supabase_perf_evidence"
down_revision = "0023_performance_security_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL cannot promote an index that is still owned by a UNIQUE
    # constraint. Create a standalone unique index first, then replace
    # the UNIQUE constraint with a PRIMARY KEY backed by that index.
    op.execute(
        "CREATE UNIQUE INDEX predictions_current_pk_idx "
        'ON public.predictions_current ("gameweek", "element_id")'
    )
    op.execute("ALTER TABLE public.predictions_current DROP CONSTRAINT uq_pred_current_gw_element")
    op.execute(
        "ALTER TABLE public.predictions_current "
        "ADD CONSTRAINT predictions_current_pkey "
        "PRIMARY KEY USING INDEX predictions_current_pk_idx"
    )

    op.create_index(
        "predictions_current_computed_at_idx",
        "predictions_current",
        ["computed_at"],
        unique=False,
    )

    op.create_index(
        "ix_availability_events_primary_source_id",
        "availability_events",
        ["primary_source_id"],
        unique=False,
    )


def downgrade() -> None:
    # Dropping the PK drops its backing index. Recreate the original
    # UNIQUE constraint to restore the pre-migration invariant.
    op.execute("ALTER TABLE public.predictions_current DROP CONSTRAINT predictions_current_pkey")
    op.execute(
        "ALTER TABLE public.predictions_current "
        "ADD CONSTRAINT uq_pred_current_gw_element "
        'UNIQUE ("gameweek", "element_id")'
    )

    op.drop_index(
        "ix_availability_events_primary_source_id",
        table_name="availability_events",
    )
    op.drop_index(
        "predictions_current_computed_at_idx",
        table_name="predictions_current",
    )
