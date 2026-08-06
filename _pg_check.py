from sqlalchemy import create_engine, text

url = "postgresql+psycopg://fpl:fpl@localhost:5432/fpl"
e = create_engine(url)
with e.connect() as c:
    print("version:", c.execute(text("SELECT version()")).scalar()[:40])
    n = c.execute(
        text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
    ).scalar()
    print("public tables:", n)
e.dispose()
