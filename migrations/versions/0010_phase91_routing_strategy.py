"""phase9.1: routing strategy recorded on every extraction run

The ProviderRouter decides *which* provider handles an extraction task
(task-based routing, automatic fallback on rate-limit/auth errors, optional
round-robin). To make that decision auditable the same way provider/model/
prompt provenance already is, ``llm_extraction_runs`` gains a
``routing_strategy`` column: ``task_based`` / ``fallback`` / ``round_robin``,
or NULL when the provider was chosen directly without routing.

Phase 1-8 tables are NOT modified. ``llm_extraction_runs`` is a Phase 9-owned
table.

Revision ID: 0010_phase91_routing_strategy
Revises: 0009_phase91_prompt_provenance
Create Date: 2026-08-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_phase91_routing_strategy"
down_revision = "0009_phase91_prompt_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_extraction_runs",
        sa.Column("routing_strategy", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_extraction_runs", "routing_strategy")
