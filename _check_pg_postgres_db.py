"""Check which database actually has the Phase 7 tables."""
import psycopg

for dbname in ("postgres", "fpl"):
    try:
        c = psycopg.connect(f"postgresql://fpl:fpl@localhost:5432/{dbname}")
        cur = c.cursor()
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )
        tables = [r[0] for r in cur.fetchall()]
        print(f"\n=== DB: {dbname} — {len(tables)} public tables ===")
        for t in tables:
            print("   ", t)
        try:
            cur.execute("SELECT version_num FROM alembic_version")
            print("   alembic_version:", cur.fetchone()[0])
        except Exception as e:
            print("   alembic_version: ERR", e)
        c.close()
    except Exception as e:
        print(f"\n=== DB: {dbname} — CONNECT ERROR: {e}")
