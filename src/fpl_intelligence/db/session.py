from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.config import get_settings

settings = get_settings()
# Supabase exposes PostgreSQL through a transaction-mode pooler (port 6543).
# psycopg3 auto-prepares repeated statements, which collides on a pooled
# connection ("prepared statement already exists"). Disable prepared statements.
# The flag is psycopg-specific; passing it to other dialects (sqlite in tests)
# raises, so scope it to postgres URLs.
_connect_args = (
    {"prepare_threshold": None} if settings.database_url.startswith("postgres") else {}
)
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args=_connect_args,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
