"""Verify Phase 7 tables exist in PostgreSQL with expected schema."""
from sqlalchemy import create_engine, inspect, text

URL = "postgresql+psycopg://fpl:fpl@localhost:5432/fpl"
engine = create_engine(URL)

expected_tables = [
    "availability_sources",
    "availability_articles",
    "availability_evidence",
    "availability_events",
    "player_injuries",
    "player_suspensions",
    "training_reports",
    "press_conferences",
    "player_mentions",
]

insp = inspect(engine)
tables = set(insp.get_table_names())
print("=== TABLES ===")
for t in expected_tables:
    print(f"  {t}: {'OK' if t in tables else 'MISSING'}")

print("\n=== COLUMNS (availability_events) ===")
cols = {c['name'] for c in insp.get_columns('availability_events')}
print("  ", sorted(cols))

print("\n=== FOREIGN KEYS (availability_evidence) ===")
for fk in insp.get_foreign_keys('availability_evidence'):
    print(f"  {fk['constrained_columns']} -> {fk['referred_table']}.{fk['referred_columns']}")

print("\n=== UNIQUE CONSTRAINTS ===")
for t in ['availability_evidence', 'training_reports', 'player_mentions', 'availability_sources']:
    for uc in insp.get_unique_constraints(t):
        print(f"  {t}: {uc['name']} {uc['column_names']}")

print("\n=== INDEXES (availability_events) ===")
for ix in insp.get_indexes('availability_events'):
    print(f"  {ix['name']}: {ix['column_names']} unique={ix['unique']}")

print("\n=== COUNT ===")
with engine.connect() as conn:
    for t in expected_tables:
        n = conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
        print(f"  {t}: {n} rows")
    # Verify migration version
    ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    print(f"\nAlembic version: {ver}")
