"""v2.7.11 — Supabase performance + integrity evidence (issue #24).

Revision ID: 0024_supabase_perf_evidence
Revises: 0023_performance_security_cleanup
Create Date: 2026-09-03

Captures three evidence-justified DDL changes for the Supabase
production schema. All changes were scoped to ``Q1``-``Q5`` of the
``scripts/supabase_index_evidence.py`` run that produced
``docs/SUPABASE_INDEX_EVIDENCE_2026.md`` (captured against project
``hnsoektotpqgvpqshusi``). The review's hard rules were:

* only propose indexes the captured plan shows are missing;
* do not drop any existing "unused" indexes;
* prefer additive changes.

This migration is **purely additive DDL**: 1 PK promotion from an
existing UNIQUE, 1 new single-column index, 1 new single-column index.
No data movement. No enum types. No FK to existing tables.

1. ``predictions_current`` PRIMARY KEY on ``(gameweek, element_id)``.

   Promote the existing UNIQUE constraint
   ``uq_pred_current_gw_element`` to a PRIMARY KEY via
   ``USING INDEX``. Postgres database advisor flags tables without
   a PK. The UNIQUE index already exists as
   ``uq_pred_current_gw_element``; the promotion changes only the
   constraint kind, not the index itself. No other table has a
   foreign key to ``predictions_current``, so the promotion has
   zero cascading concerns.

2. ``predictions_current_computed_at_idx`` on
   ``predictions_current(computed_at)``.

   Covers ``Q2`` from the evidence: the ``data_sources`` endpoint
   ``SELECT computed_at FROM predictions_current
   ORDER BY computed_at DESC LIMIT 1`` ran as a Seq Scan + top-N
   Sort over 10,325 rows. A single-column B-tree on
   ``computed_at`` brings the cost to a single index probe + the
   top-1 row read.

3. ``ix_availability_events_primary_source_id`` on
   ``availability_events(primary_source_id)``.

   The FK to ``availability_sources(id)`` is declared but Postgres
   does not auto-index FK columns. ``DBAvailabilityProvider`` joins
   events to sources by ``primary_source_id`` on every availability
   lookup. With 1,659 rows today and Phase 7 still BLOCKED the
   measurable hit is small, but the index is cheap, evidence-backed
   (``Q4`` in the evidence report uses this join path), and removes
   a future leak.

Downgrade
---------

* Drop the two new indexes.
* Drop the PK (the underlying UNIQUE index is preserved; the
  downgrade does not lose the original UNIQUE constraint).

Out of scope (deliberately deferred — not yet evidence-justified):

* Composite indexes on ``fixtures(gameweek_id, home_team_id)`` /
  ``fixtures(gameweek_id, away_team_id)``. Q7 currently runs in
  0.671 ms via the existing single-column index; revisit when row
  count grows.
* Partial index ``availability_events(valid_from) WHERE is_current``.
  Q5 currently 12 ms on 1,659 rows; revisit when row count grows.
* ANY drop of existing "unused" indexes per issue #24 hard rule.

Safety
------

* Live DB state captured 2026-09-03T03:07Z confirms:
  - ``alembic_version = 0023_performance_security_cleanup``
  - ``predictions_current`` has only the UNIQUE
    ``uq_pred_current_gw_element``; no PK
  - ``predictions_current`` indexes: ``ix_predictions_current_element``,
    ``uq_pred_current_gw_element``; no ``computed_at`` index
  - ``availability_events`` indexes on ``primary_source_id``: NONE
* All three DDL operations are non-destructive: PK promotion uses
  an existing index, the other two are pure ``CREATE INDEX``.
* The migration is idempotent at the data level: re-running it
  succeeds (``ADD CONSTRAINT IF NOT EXISTS`` semantics for the
  indexes, and the PK promotion fails-fast with a clear error if
  the underlying UNIQUE does not exist — caught at apply time, not
  at runtime).
"""

from __future__ import annotations

from alembic import op

revision = "0024_supabase_perf_evidence"
down_revision = "0023_performance_security_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Promote the existing UNIQUE to a PRIMARY KEY.
    # Using ``USING INDEX`` avoids rebuilding the index. The name
    # ``predictions_current_pkey`` follows the project convention
    # (snake-case table name + ``_pkey``).
    op.execute(
        "ALTER TABLE public.predictions_current "
        "ADD CONSTRAINT predictions_current_pkey "
        "PRIMARY KEY USING INDEX uq_pred_current_gw_element"
    )

    # 2. Index on ``computed_at`` (covers Q2 freshness probe).
    op.create_index(
        "predictions_current_computed_at_idx",
        "predictions_current",
        ["computed_at"],
        unique=False,
    )

    # 3. Index on ``availability_events.primary_source_id``
    # (covers the Q4 join path in DBAvailabilityProvider).
    op.create_index(
        "ix_availability_events_primary_source_id",
        "availability_events",
        ["primary_source_id"],
        unique=False,
    )


def downgrade() -> None:
    # Drop the PK; the underlying UNIQUE index
    # ``uq_pred_current_gw_element`` is preserved by the engine and
    # the original UNIQUE constraint remains.
    op.execute("ALTER TABLE public.predictions_current DROP CONSTRAINT predictions_current_pkey")

    op.drop_index(
        "ix_availability_events_primary_source_id",
        table_name="availability_events",
    )
    op.drop_index(
        "predictions_current_computed_at_idx",
        table_name="predictions_current",
    )
