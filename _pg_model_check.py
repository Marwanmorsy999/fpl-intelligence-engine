"""Verify SQLAlchemy Phase 7 models match the actual PostgreSQL schema."""
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.orm import create_session

from fpl_intelligence.availability import models as av
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import Fixture, Gameweek, Player, Season, Team

# Ensure all models are registered on Base.metadata
for cls in [
    av.AvailabilitySource, av.AvailabilityArticle, av.AvailabilityEvidence,
    av.AvailabilityEvent, av.PlayerInjury, av.PlayerSuspension,
    av.TrainingReport, av.PressConference, av.PlayerMention,
]:
    assert cls.__tablename__ in Base.metadata.tables, cls.__tablename__

engine = sa.create_engine("postgresql+psycopg://fpl:fpl@localhost:5432/fpl")
insp = inspect(engine)

expected_tables = {
    "availability_sources", "availability_articles", "availability_evidence",
    "availability_events", "player_injuries", "player_suspensions",
    "training_reports", "press_conferences", "player_mentions",
}

print("MODEL_TABLES_IN_METADATA:", len(Base.metadata.tables))
print("DBSYNC: all 9 present in DB:", expected_tables.issubset(set(insp.get_table_names())))

# Check each model's columns exist in DB
for cls in [
    av.AvailabilitySource, av.AvailabilityArticle, av.AvailabilityEvidence,
    av.AvailabilityEvent, av.PlayerInjury, av.PlayerSuspension,
    av.TrainingReport, av.PressConference, av.PlayerMention,
]:
    table = cls.__tablename__
    db_cols = {c["name"] for c in insp.get_columns(table)}
    model_cols = set(Base.metadata.tables[table].columns.keys())
    missing_in_db = model_cols - db_cols
    extra_in_db = db_cols - model_cols
    status = "OK" if not missing_in_db else f"MISSING: {missing_in_db}"
    if extra_in_db:
        status += f" EXTRA: {extra_in_db}"
    print(f"  {table}: {status}")

# Verify FKs
for cls in [av.AvailabilityEvidence, av.AvailabilityEvent]:
    table = cls.__tablename__
    fks = insp.get_foreign_keys(table)
    print(f"  {table} FKs:", [f["constrained_columns"][0] + "->" + list(f["referred_table"])[0] if f["referred_table"] else "?" for f in fks])

# Verify unique constraints
for cls in [av.AvailabilityEvidence, av.TrainingReport, av.PlayerMention]:
    table = cls.__tablename__
    uqs = insp.get_unique_constraints(table)
    print(f"  {table} UNIQUEs:", [(u["name"], u["column_names"]) for u in uqs])

print("MODEL_DB_CHECK_DONE")
