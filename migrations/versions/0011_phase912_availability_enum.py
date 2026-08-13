"""phase9.1.2: add 'available' to the availabilitystatus PostgreSQL enum

Phase 9.1.1 added ``AVAILABLE = "available"`` to the Python
:class:`~fpl_intelligence.availability.models.AvailabilityStatus` enum and wired
it into the Phase 7 state/derivation tables, but the **native PostgreSQL enum
type** ``availabilitystatus`` (created by migration ``0006``) was not updated.
On PostgreSQL a native enum cannot store a value it does not know about, so any
attempt to persist an ``available`` status — including from the live extraction
path — would hit a constraint violation.

This migration adds the missing value with ``ALTER TYPE ... ADD VALUE IF NOT
EXISTS 'available'``. PostgreSQL does not allow ``ALTER TYPE ... ADD VALUE``
inside a normal transaction block, so this uses Alembic's
``autocommit_block`` to run the DDL in its own autocommit context. SQLite
(supports ``ALTER TYPE`` only in PostgreSQL-mode builds) and other non-PostgreSQL
dialects are skipped: their ``SAEnum`` is recreated from the model metadata on
``create_all`` in tests, and they are not used for production availability
persistence.

The value is inserted **after** ``start`` so the enum ordering remains
logically consistent (start < available < bench ... out/suspended), matching
``_STATUS_ORDER`` in :mod:`fpl_intelligence.availability.evidence`.

Revision ID: 0011_phase912_availability_enum
Revises: 0010_phase91_routing_strategy
Create Date: 2026-08-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_phase912_availability_enum"
down_revision = "0010_phase91_routing_strategy"
branch_labels = None
depends_on = None

#: The native PostgreSQL enum type name used by all availability-status columns
#: created in migration 0006. Must match exactly.
ENUM_TYPE_NAME = "availabilitystatus"

#: The value being added, and the enum value it should sort after.
NEW_VALUE = "available"
AFTER_VALUE = "start"


def upgrade() -> None:
    """Add 'available' to the availabilitystatus PostgreSQL enum.

    Uses ``autocommit_block`` because PostgreSQL requires ``ALTER TYPE ... ADD
    VALUE`` to run outside of an explicit transaction. The ``IF NOT EXISTS``
    guard makes the migration idempotent — re-running it on an enum that already
    contains the value is a no-op.
    """
    bind = op.get_bind()

    if bind.dialect.name != "postgresql":
        # SQLite, MySQL, etc. do not have the same native enum semantics.
        # The SQLAlchemy models drive enum creation on create_all in tests,
        # so no DDL is needed for those dialects.
        return

    # Idempotency: check whether the value already exists before attempting
    # the ALTER TYPE. This prevents a hard error if the migration was partially
    # applied or re-run manually.
    existing_values = _enum_values(bind)
    if NEW_VALUE in existing_values:
        return

    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                f"ALTER TYPE {ENUM_TYPE_NAME} "
                f"ADD VALUE IF NOT EXISTS '{NEW_VALUE}' "
                f"AFTER '{AFTER_VALUE}'"
            )
        )


def downgrade() -> None:
    """No-op: PostgreSQL cannot remove an enum value that is in use.

    Dropping a value from a native PostgreSQL enum type (``ALTER TYPE ... DROP
    VALUE``) is only possible when no rows reference the value. Because
    ``availability_evidence`` and ``availability_events`` are append-only and
    never modified, historical rows may legitimately carry ``"available"``,
    making a safe downgrade impossible without first scanning and rewriting
    every consuming row.

    Rather than silently corrupt data, the downgrade is a documented no-op.
    To remove the value in a development environment, drop and recreate the
    database from scratch with migrations from the pre-add enum state.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    existing_values = _enum_values(bind)
    if NEW_VALUE not in existing_values:
        return

    # Intentionally do NOT drop the enum value. If a downgrade is genuinely
    # required in a disposable environment, the operator must:
    #   1. Back up the database.
    #   2. Rewrite or delete every row referencing 'available'.
    #   3. Recreate the enum without the value (DROP TYPE + CREATE TYPE).
    #   4. Re-create all dependent columns.
    # This is destructive by nature; a no-op downgrade is the safe default.
    return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enum_values(bind: sa.engine.Connection) -> list[str]:
    """Return the current value list of the ``availabilitystatus`` enum type.

    Queries ``pg_enum`` joined to ``pg_type`` to enumerate the labels in
    definition order. Returns an empty list if the type does not exist yet.
    """
    result = bind.execute(
        sa.text(
            """
            SELECT enumlabel
            FROM pg_enum e
            JOIN pg_type t ON e.enumtypid = t.oid
            WHERE t.typname = :type_name
            ORDER BY e.enumsortorder
            """
        ),
        {"type_name": ENUM_TYPE_NAME},
    )
    return [row[0] for row in result]
