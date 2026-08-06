import psycopg

c = psycopg.connect("postgresql://fpl:fpl@localhost:5432/fpl")
cur = c.cursor()
cur.execute(
    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
    "AND (tablename LIKE 'availability_%' "
    "OR tablename IN ('player_injuries','player_suspensions',"
    "'training_reports','press_conferences','player_mentions')) "
    "ORDER BY tablename"
)
tables = [r[0] for r in cur.fetchall()]
print("PHASE7_TABLES:", sorted(tables))
expected = {
    "availability_sources", "availability_articles", "availability_evidence",
    "availability_events", "player_injuries", "player_suspensions",
    "training_reports", "press_conferences", "player_mentions",
}
print("ALL_9_PRESENT:", set(tables) >= expected)
cur.execute("SELECT version_num FROM alembic_version")
print("ALEMBIC_VERSION:", cur.fetchone()[0])
# Verify enum types exist
cur.execute(
    "SELECT typname FROM pg_type WHERE typname IN "
    "('sourcereliability','availabilitystatus','evidencetype')"
)
print("ENUM_TYPES:", sorted(r[0] for r in cur.fetchall()))
c.close()
