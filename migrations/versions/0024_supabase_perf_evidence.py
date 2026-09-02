"""v2.7.11 — evidence-driven index and PK additions for hot paths.

Revision ID: 0024_supabase_perf_evidence
Revises: 0023_performance_security_cleanup
Create Date: 2026-09-02

Evidence: ``docs/SUPABASE_INDEX_EVIDENCE_2026.md`` (captured 2026-09-02
against live Supabase project ``fpl-intelligence-engine``,
``hnsoektotpqgvpqshusi``). All changes are backed by EXPLAIN ANALYZE
captured from the production-shaped schema and the documented hot
queries in the engine.

Each change is independently justified:

1. ``predictions_current`` PRIMARY KEY on ``(gameweek, element_id)``.

   Promote the existing UNIQUE constraint to a PRIMARY KEY so the table
   has a stable identity. Currently flagged as "no primary key" by the
   Supabase database advisor. The UNIQUE index already exists as
   ``uq_pred_current_gw_element``; no functional index change is
   needed, only the constraint promotion.

2. ``predictions_current_computed_at_idx`` on ``predictions_current(computed_at DESC)``.

   Covers the ``data_sources`` endpoint ``SELECT computed_at FROM
   predictions_current ORDER BY computed_at DESC LIMIT 1`` (Q2 in the
   evidence report). Captured at **178.602 ms** with a Seq Scan +
   top-N Sort over 10,325 rows (348 buffer hits). The partial index
   keeps the cost O(1) for any future row count.

3. ``ix_availability_events_primary_source_id`` on
   ``availability_events(primary_source_id)``.

   The FK to ``availability_sources(id)`` is declared but Postgres does
   not auto-index FK columns. ``DBAvailabilityProvider`` (Phase 7) joins
   events to sources by ``primary_source_id`` on every availability
   lookup. With 1,659 rows today and Phase 7 still BLOCKED the
   measurable hit is small, but the index is cheap, evidence-backed
   (Q4 in the evidence report uses this join path), and removes a
   future leak.

Out of scope (deliberately deferred — not yet evidence-justified):

- Composite indexes on ``fixtures(gameweek_id, home_team_id)`` and
  ``fixtures(gameweek_id, away_team_id)``. Q7 currently runs in 0.671 ms
  via ``ix_fixtures_gameweek_id`` + filter; revisit if row count or
  query latency grows.
- Partial index ``availability_events(valid_from) WHERE is_current``.
  Q5 currently 12.089 ms on 1,659 rows; revisit when Phase 7 row count
  grows past tens of thousands.
- Dropping any existing "unused" indexes. Per issue #24 hard rule: no
  drops without query-level evidence.
"""

from __future__ import annotations

from alembic import op

revision = "0024_supabase_perf_evidence"
down_revision = "0023_performance_security_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Promote UNIQUE -> PRIMARY KEY on predictions_current.
    op.execute(
        """
        ALTER TABLE public.predictions_current
        ADD CONSTRAINT predictions_current_pkey
        PRIMARY KEY USING INDEX uq_pred_current_gw_element
        """
    )

    # 2. Cover the data-sources last-computed lookup.
    op.create_index(
        "predictions_current_computed_at_idx",
        "predictions_current",
        ["computed_at"],
        unique=False,
    )

    # 3. Cover the availability_events -> availability_sources FK join.
    op.create_index(
        "ix_availability_events_primary_source_id",
        "availability_events",
        ["primary_source_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_availability_events_primary_source_id",
        table_name="availability_events",
    )
    op.drop_index(
        "predictions_current_computed_at_idx",
        table_name="predictions_current",
    )
    op.execute("ALTER TABLE public.predictions_current DROP CONSTRAINT predictions_current_pkey")
