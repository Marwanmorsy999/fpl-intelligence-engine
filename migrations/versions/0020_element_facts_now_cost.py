"""phase23 L3: element_facts.now_cost — absolute price in 0.1m units

Prod DBs created on migration 0018 lack this column, so every
SELECT on element_facts (including the drawer) 500s with
UndefinedColumn. The daily materialize already self-seals it, but a
proper alembic migration makes the schema deterministic and fixes
cold DBs before their first cron.

Revision ID: 0020_element_facts_now_cost
Revises: 0019_briefs_provider_refresh
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_element_facts_now_cost"
down_revision = "0019_briefs_provider_refresh"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use raw SQL with IF NOT EXISTS so re-running on an already-patched
    # prod DB (where the daily job's ALTER already ran) is a no-op.
    op.execute(sa.text("ALTER TABLE element_facts ADD COLUMN IF NOT EXISTS now_cost INTEGER"))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE element_facts DROP COLUMN IF EXISTS now_cost"))
