"""Final PostgreSQL verification for Phase 7 migration."""
import psycopg

try:
    c = psycopg.connect("postgresql://fpl:fpl@localhost:5432/fpl")
except Exception as e:  # noqa: BLE001
    print("CONNECTION_ERROR:", e)
    raise SystemExit(1)

cur = c.cursor()
cur.execute("SELECT current_database(), version()")
print("DB_CONNECTION:", cur.fetchone()[0], "| server up")

cur.execute(
    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
)
tables = [r[0] for r in cur.fetchall()]
print("PUBLIC_TABLE_COUNT:", len(tables))
print("ALL_TABLES:", tables)

phase7 = {
    "availability_sources", "availability_articles", "availability_evidence",
    "availability_events", "player_injuries", "player_suspensions",
    "training_reports", "press_conferences", "player_mentions",
}
print("PHASE7_PRESENT:", phase7.issubset(set(tables)))
print("MISSING:", sorted(phase7 - set(tables)))

# alembic version
cur.execute("SELECT version_num FROM alembic_version")
print("ALEMBIC_VERSION:", cur.fetchone()[0])

# enum types
cur.execute(
    "SELECT typname FROM pg_type WHERE typname IN "
    "('sourcereliability','availabilitystatus','evidencetype')"
)
print("ENUM_TYPES:", sorted(r[0] for r in cur.fetchall()))
c.close()
print("PG_VERIFY_DONE")
