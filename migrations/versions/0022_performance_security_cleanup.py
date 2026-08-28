"""v2.7.10 — performance/security cleanup for production schema.

Removes duplicate indexes reported by Supabase's database advisor and makes
``public.rls_auto_enable()`` non-callable by API roles. The runtime already
uses versioned migrations, so request paths must not recreate schema objects.

Revision ID: 0022_performance_security_cleanup
Revises: 0021_local_squad_state
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op

revision = "0022_performance_security_cleanup"
down_revision = "0021_local_squad_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the unique constraint-backed index on each squad table; the plain
    # unique session index is redundant and only adds write overhead.
    op.execute("DROP INDEX IF EXISTS ix_local_squad_state_session_id")
    op.execute("DROP INDEX IF EXISTS ix_squad_state_session_id")

    # rls_auto_enable is a deployment helper, not a public RPC surface.
    op.execute("REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM PUBLIC")
    op.execute("REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM anon")
    op.execute("REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM authenticated")


def downgrade() -> None:
    op.create_index(
        "ix_local_squad_state_session_id",
        "local_squad_state",
        ["session_id"],
        unique=True,
    )
    op.create_index(
        "ix_squad_state_session_id",
        "squad_state",
        ["session_id"],
        unique=True,
    )
    op.execute("GRANT EXECUTE ON FUNCTION public.rls_auto_enable() TO PUBLIC")
