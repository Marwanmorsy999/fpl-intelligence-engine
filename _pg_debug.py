"""Debug SQLAlchemy vs psycopg direct connection."""
import sqlalchemy as sa
from sqlalchemy import inspect

# Direct psycopg
import psycopg
c = psycopg.connect("postgresql://fpl:fpl@localhost:5432/fpl")
cur = c.cursor()
cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
direct = [r[0] for r in cur.fetchall()]
print("PSYCOPG_DIRECT count:", len(direct))
print("HAS availability_sources:", "availability_sources" in direct)
c.close()

# SQLAlchemy
engine = sa.create_engine("postgresql+psycopg://fpl:fpl@localhost:5432/fpl")
try:
    insp = inspect(engine)
    names = insp.get_table_names()
    print("SQLALCHEMY get_table_names count:", len(names))
    print("HAS availability_sources:", "availability_sources" in names)
except Exception as e:  # noqa: BLE001
    print("SQLALCHEMY ERROR:", repr(e))

# Try 127.0.0.1 explicitly
engine2 = sa.create_engine("postgresql+psycopg://fpl:fpl@127.0.0.1:5432/fpl")
try:
    insp2 = inspect(engine2)
    names2 = insp2.get_table_names()
    print("SQLALCHEMY(127.0.0.1) count:", len(names2))
    print("HAS availability_sources:", "availability_sources" in names2)
except Exception as e:  # noqa: BLE001
    print("SQLALCHEMY(127.0.0.1) ERROR:", repr(e))
print("DEBUG_DONE")
