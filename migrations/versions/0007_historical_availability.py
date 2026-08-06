"""phase7.2: historical availability provenance + source reliability metadata

Adds temporal classification, provider provenance, and a source-reliability
metadata store for the historical availability import path.

New columns on availability_events:
- temporal_class: STRICT_BACKTEST_SAFE / HISTORICAL_EVENT_ONLY /
  OUTCOME_ONLY / UNKNOWN -- distinguishes when information was available
  (publication/availability time) from when the event merely occurred.
- provider: which provider adapter produced the event.
- provider_event_id: the provider's own event identifier (for idempotent
  re-import and entity resolution).

New table:
- source_reliability_metadata: per-source reliability metadata initialised
  with a neutral prior (no invented historical accuracy scores).

Revision ID: 0007_historical_availability
Revises: 0006_phase7_availability
Create Date: 2026-08-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_historical_availability"
down_revision = "0006_phase7_availability"
branch_labels = None
depends_on = None

_TEMPORAL_CLASS = [
    "STRICT_BACKTEST_SAFE",
    "HISTORICAL_EVENT_ONLY",
    "OUTCOME_ONLY",
    "UNKNOWN",
]


def upgrade() -> None:
    # Create the native PostgreSQL enum type explicitly before using it in an
    # ALTER TABLE ADD COLUMN. When an enum is declared inline inside
    # op.create_table, SQLAlchemy creates the type as part of the table DDL.
    # But op.add_column on an existing table issues the ALTER TABLE before the
    # type has been created, so we must create the type first (idempotently).
    temporalclass = sa.Enum(*_TEMPORAL_CLASS, name="temporalclass")
    temporalclass.create(op.get_bind(), checkfirst=True)

    # Existing events fall back to UNKNOWN until re-classified by the importer.
    op.add_column(
        "availability_events",
        sa.Column(
            "temporal_class",
            temporalclass,
            nullable=False,
            server_default="UNKNOWN",
        ),
    )
    op.add_column(
        "availability_events",
        sa.Column("provider", sa.String(100), nullable=True),
    )
    op.add_column(
        "availability_events",
        sa.Column("provider_event_id", sa.String(200), nullable=True),
    )
    op.create_index(
        "ix_events_temporal_provider",
        "availability_events", ["temporal_class", "provider"],
    )

    op.create_table(
        "source_reliability_metadata",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_type", sa.String(100), nullable=False),
        sa.Column("source_name", sa.String(200), nullable=False),
        sa.Column("reliability_level", sa.String(50), nullable=False),
        sa.Column("event_types_supported", sa.Text(), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=True),
        sa.Column("timestamp_reliability", sa.String(50), nullable=False),
        sa.Column("verified_accuracy", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "source_type", "source_name", name="uq_source_reliability_meta",
        ),
    )


def downgrade() -> None:
    op.drop_table("source_reliability_metadata")
    op.drop_index("ix_events_temporal_provider", table_name="availability_events")
    op.drop_column("availability_events", "provider_event_id")
    op.drop_column("availability_events", "provider")
    op.drop_column("availability_events", "temporal_class")
    op.execute(sa.text("DROP TYPE IF EXISTS temporalclass"))
