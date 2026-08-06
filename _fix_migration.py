# Carefully wrap E501 long lines in the migration file without changing semantics.
path = "migrations/versions/0006_phase7_availability.py"
with open(path, encoding="utf-8") as f:
    lines = f.readlines()

out = []
for line in lines:
    stripped = line.rstrip("\n")
    # Only wrap lines > 100 chars that are known Column/Constraint/Index lines.
    if len(stripped) > 100:
        # sa.Column with is_official_club
        if stripped.startswith('        sa.Column("is_official_club"'):
            out.append('        sa.Column(\n            "is_official_club",\n            sa.Boolean(), nullable=False,\n            server_default=sa.text("false"),\n        )\n')
            continue
        # sa.Column source_id articles
        if stripped.startswith('        sa.Column("source_id", sa.Integer(), sa.ForeignKey("availability_sources.id")'):
            out.append('        sa.Column(\n            "source_id",\n            sa.Integer(), sa.ForeignKey("availability_sources.id"), nullable=False,\n        )\n')
            continue
        # sa.Column scraped_at
        if stripped.startswith('        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())'):
            out.append('        sa.Column(\n            "scraped_at",\n            sa.DateTime(timezone=True), nullable=False,\n            server_default=sa.func.now(),\n        )\n')
            continue
        # op.create_index articles source_id
        if stripped.startswith('    op.create_index("ix_availability_articles_source_id"'):
            out.append('    op.create_index(\n        "ix_availability_articles_source_id", "availability_articles", ["source_id"],\n    )\n')
            continue
        # op.create_index articles published_at
        if stripped.startswith('    op.create_index("ix_availability_articles_published_at"'):
            out.append('    op.create_index(\n        "ix_availability_articles_published_at", "availability_articles", ["published_at"],\n    )\n')
            continue
        # op.create_index articles published_source
        if stripped.startswith('    op.create_index("ix_articles_published_source"'):
            out.append('    op.create_index(\n        "ix_articles_published_source", "availability_articles", ["published_at", "source_id"],\n    )\n')
            continue
        # sa.Column status_mentioned evidence
        if stripped.startswith('        sa.Column("status_mentioned", sa.Enum(*_AVAILABILITY_STATUS'):
            out.append('        sa.Column(\n            "status_mentioned",\n            sa.Enum(*_AVAILABILITY_STATUS, name="availabilitystatus"), nullable=False,\n        )\n')
            continue
        # sa.Column extracted_at evidence
        if stripped.startswith('        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())'):
            out.append('        sa.Column(\n            "extracted_at",\n            sa.DateTime(timezone=True), nullable=False,\n            server_default=sa.func.now(),\n        )\n')
            continue
        # op.create_index evidence gameweek_id
        if stripped.startswith('    op.create_index("ix_availability_evidence_gameweek_id"'):
            out.append('    op.create_index(\n        "ix_availability_evidence_gameweek_id", "availability_evidence", ["gameweek_id"],\n    )\n')
            continue
        # op.create_index evidence extracted_at
        if stripped.startswith('    op.create_index("ix_availability_evidence_extracted_at"'):
            out.append('    op.create_index(\n        "ix_availability_evidence_extracted_at", "availability_evidence", ["extracted_at"],\n    )\n')
            continue
        # op.create_index evidence valid_from
        if stripped.startswith('    op.create_index("ix_availability_evidence_valid_from"'):
            out.append('    op.create_index(\n        "ix_availability_evidence_valid_from", "availability_evidence", ["valid_from"],\n    )\n')
            continue
        # op.create_index evidence valid_to
        if stripped.startswith('    op.create_index("ix_availability_evidence_valid_to"'):
            out.append('    op.create_index(\n        "ix_availability_evidence_valid_to", "availability_evidence", ["valid_to"],\n    )\n')
            continue
        # sa.Column status events
        if stripped.startswith('        sa.Column("status", sa.Enum(*_AVAILABILITY_STATUS'):
            out.append('        sa.Column(\n            "status",\n            sa.Enum(*_AVAILABILITY_STATUS, name="availabilitystatus"), nullable=False,\n        )\n')
            continue
        # sa.Column created_at events
        if stripped.startswith('        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())'):
            out.append('        sa.Column(\n            "created_at",\n            sa.DateTime(timezone=True), nullable=False,\n            server_default=sa.func.now(),\n        )\n')
            continue
        # op.create_index injuries expected_return_at
        if stripped.startswith('    op.create_index("ix_player_injuries_expected_return_at"'):
            out.append('    op.create_index(\n        "ix_player_injuries_expected_return_at", "player_injuries", ["expected_return_at"],\n    )\n')
            continue
        # sa.Column reported_at training
        if stripped.startswith('        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())'):
            out.append('        sa.Column(\n            "reported_at",\n            sa.DateTime(timezone=True), nullable=False,\n            server_default=sa.func.now(),\n        )\n')
            continue
        # sa.Column recorded_at press
        if stripped.startswith('        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())'):
            out.append('        sa.Column(\n            "recorded_at",\n            sa.DateTime(timezone=True), nullable=False,\n            server_default=sa.func.now(),\n        )\n')
            continue
        # sa.Column press_conference_id mentions
        if stripped.startswith('        sa.Column("press_conference_id", sa.Integer(), sa.ForeignKey("press_conferences.id")'):
            out.append('        sa.Column(\n            "press_conference_id",\n            sa.Integer(), sa.ForeignKey("press_conferences.id"), nullable=False,\n        )\n')
            continue
        # op.create_index mentions press_conference_id
        if stripped.startswith('    op.create_index("ix_player_mentions_press_conference_id"'):
            out.append('    op.create_index(\n        "ix_player_mentions_press_conference_id", "player_mentions", ["press_conference_id"],\n    )\n')
            continue
    out.append(line)

with open(path, "w", encoding="utf-8") as f:
    f.writelines(out)
print("migration wrapped")
