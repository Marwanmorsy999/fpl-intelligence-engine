"""Verify the 9 Phase 7 tables exist in PostgreSQL after migration."""
import psycopg

c = psycopg.connect("postgresql://fpl:fpl@localhost:5432/fpl")
cur = c.cursor()
# List all tables in public schema
cur.execute(
    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
)
all_rows = [r[0] for r in cur.fetchall()]
print("TOTAL_PUBLIC_TABLES:", len(all_rows))
for r in all_rows:
    print("  table:", r)

tables = [
    "availability_sources",
    "availability_articles",
    "availability_evidence",
    "availability_events",
    "player_injuries",
    "player_suspensions",
    "training_reports",
    "press_conferences",
    "player_mentions",
    "alembic_version",
]
cur.execute(
    "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename = ANY(%s) ORDER BY tablename",
    (tables,),
)
rows = [r[0] for r in cur.fetchall()]
print("PHASE7+ALEMBIC TABLES FOUND:", len(rows))
for r in rows:
    print("  found:", r)
missing = set(tables) - set(rows)
if missing:
    print("MISSING:", sorted(missing))
else:
    print("ALL_TABLES_PRESENT=True")
try:
    cur.execute("SELECT version_num FROM alembic_version")
    print("ALEMBIC_VERSION:", cur.fetchone()[0])
except Exception as e:
    print("ALEMBIC_VERSION_ERR:", e)
c.close()
