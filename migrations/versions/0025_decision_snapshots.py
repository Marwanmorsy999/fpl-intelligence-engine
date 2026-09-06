"""v2.7.12 — decision_snapshots table for Phase 23 background-first computation.

Revision ID: 0025_decision_snapshots
Revises: 0024_supabase_perf_evidence
Create Date: 2026-09-02

Adds the canonical precomputed-decision-snapshot table called out in
issue #23. The user request path will read this table instead of
running the full Phase 6 decision chain on every hit.

Schema is deliberately narrow:
* ``(session_id, gameweek, model_version, data_snapshot_id)`` UNIQUE
  so the same logical decision never collides with itself across
  schema or data-snapshot changes.
* ``refresh_state`` carries ``fresh | refreshing | stale | unavailable``
  so callers can return an honest state instead of blocking on
  recompute.
* ``generated_at`` is indexed so freshness probes and stale-eviction
  queries are O(log n).
* ``refresh_window_seconds`` is per-snapshot so the cache policy can
  be tuned without schema changes.

No backfill is performed: the table starts empty and the first
background job (cron / GitHub Actions) populates it.

Concurrency: writers take a Postgres ``pg_try_advisory_lock`` keyed
by ``(session_id, gameweek)`` before transitioning to ``refreshing``.
That is enforced in application code (``decision_snapshots.try_mark_refreshing``)
and does not require any additional DDL.
"""

from __future__ import annotations

from alembic import op

revision = "0025_decision_snapshots"
down_revision = "0024_supabase_perf_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_snapshots",
        op.Column("id", op.sa.Integer(), primary_key=True),
        op.Column("session_id", op.sa.String(255), nullable=False),
        op.Column("gameweek", op.sa.Integer(), nullable=False),
        op.Column("model_version", op.sa.String(60), nullable=False),
        op.Column("data_snapshot_id", op.sa.String(80), nullable=False),
        op.Column("payload", op.sa.JSON(), nullable=False),
        op.Column("refresh_state", op.sa.String(20), nullable=False, server_default="fresh"),
        op.Column("generated_at", op.sa.DateTime(timezone=True), nullable=False),
        op.Column(
            "refresh_window_seconds",
            op.sa.Float(),
            nullable=False,
            server_default="900.0",
        ),
        op.UniqueConstraint(
            "session_id",
            "gameweek",
            "model_version",
            "data_snapshot_id",
            name="uq_decision_snapshot_identity",
        ),
    )
    op.create_index(
        "ix_decision_snapshots_session_gameweek",
        "decision_snapshots",
        ["session_id", "gameweek"],
    )
    op.create_index(
        "ix_decision_snapshots_generated_at",
        "decision_snapshots",
        ["generated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_decision_snapshots_generated_at", table_name="decision_snapshots")
    op.drop_index("ix_decision_snapshots_session_gameweek", table_name="decision_snapshots")
    op.drop_table("decision_snapshots")
