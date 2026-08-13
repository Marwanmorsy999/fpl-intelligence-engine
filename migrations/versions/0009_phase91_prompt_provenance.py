"""phase9.1: prompt versioning and free-tier provenance on extracted evidence

Phase 9.1 introduces real LLM providers. Two consequences for the schema, both
about being able to answer "how was this produced?" months later:

1. **Prompt versioning.** ``llm_extraction_runs`` already stored the hash of
   the *rendered* prompt. It now also stores ``prompt_template_hash`` — the
   SHA-256 of the unrendered template plus its schema version — so every run
   that shared a prompt *design* can be grouped regardless of its input.

2. **Free-tier accounting.** ``from_cache``, ``prompt_tokens``,
   ``completion_tokens`` and ``max_output_tokens`` record what a call actually
   cost. A cached replay consumed no quota and must not be counted as if it
   had.

The extracted evidence rows themselves (``tactical_evidence`` and the Phase 7
link table ``live_availability_evidence_links``) gain ``prompt_hash``,
``provider_name`` and ``model_name``. This is a deliberate denormalisation: the
first question asked of any extracted claim is which prompt and which model
produced it, and an evidence row should be able to answer that on its own.

Phase 1-8 tables are NOT modified. ``availability_evidence`` in particular is
untouched; its Phase 9 provenance lives on the Phase 9-owned link table.

Revision ID: 0009_phase91_prompt_provenance
Revises: 0008_phase9_live_intelligence
Create Date: 2026-08-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_phase91_prompt_provenance"
down_revision = "0008_phase9_live_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- llm_extraction_runs: prompt versioning + free-tier accounting ------
    op.add_column(
        "llm_extraction_runs",
        sa.Column("prompt_template_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "llm_extraction_runs",
        sa.Column("from_cache", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("llm_extraction_runs", sa.Column("prompt_tokens", sa.Integer(), nullable=True))
    op.add_column(
        "llm_extraction_runs", sa.Column("completion_tokens", sa.Integer(), nullable=True)
    )
    op.add_column(
        "llm_extraction_runs", sa.Column("max_output_tokens", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_llm_extraction_runs_prompt_template_hash",
        "llm_extraction_runs",
        ["prompt_template_hash"],
    )

    # -- tactical_evidence: method provenance -------------------------------
    op.add_column(
        "tactical_evidence",
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "tactical_evidence", sa.Column("provider_name", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "tactical_evidence", sa.Column("model_name", sa.String(length=200), nullable=True)
    )
    op.create_index("ix_tactical_evidence_prompt_hash", "tactical_evidence", ["prompt_hash"])

    # -- live_availability_evidence_links: method provenance ----------------
    op.add_column(
        "live_availability_evidence_links",
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "live_availability_evidence_links",
        sa.Column("provider_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "live_availability_evidence_links",
        sa.Column("model_name", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "ix_live_availability_evidence_links_prompt_hash",
        "live_availability_evidence_links",
        ["prompt_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_live_availability_evidence_links_prompt_hash",
        table_name="live_availability_evidence_links",
    )
    for column in ("model_name", "provider_name", "prompt_hash"):
        op.drop_column("live_availability_evidence_links", column)

    op.drop_index("ix_tactical_evidence_prompt_hash", table_name="tactical_evidence")
    for column in ("model_name", "provider_name", "prompt_hash"):
        op.drop_column("tactical_evidence", column)

    op.drop_index(
        "ix_llm_extraction_runs_prompt_template_hash", table_name="llm_extraction_runs"
    )
    for column in (
        "max_output_tokens",
        "completion_tokens",
        "prompt_tokens",
        "from_cache",
        "prompt_template_hash",
    ):
        op.drop_column("llm_extraction_runs", column)
