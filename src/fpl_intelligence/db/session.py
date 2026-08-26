from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.config import get_settings

settings = get_settings()

#: Built-in placeholder the Settings model uses when no DATABASE_URL was set.
_DEFAULT_PG_PLACEHOLDER = "postgresql+psycopg://fpl:fpl@localhost:5432/fpl"
#: Local dev fallback so a fresh clone runs with zero configuration.
_DEFAULT_DEV_SQLITE = "sqlite:///./fpl_local.db"


def _effective_database_url() -> str:
    """Resolve the single source-of-truth DATABASE_URL with a graceful dev fallback.

    ``DATABASE_URL`` (via settings) is the one source of truth. When nothing was
    configured (the settings object still carries its built-in placeholder) and
    we are not in a production context, the app drops back to a local SQLite
    file so development keeps working with zero configuration. Production always
    requires an explicit DATABASE_URL.
    """
    url = settings.database_url
    if url == _DEFAULT_PG_PLACEHOLDER and settings.app_env != "production":
        return _DEFAULT_DEV_SQLITE
    return url


_db_url = _effective_database_url()
# Supabase exposes PostgreSQL through a transaction-mode pooler (port 6543).
# psycopg3 auto-prepares repeated statements, which collides on a pooled
# connection ("prepared statement already exists"). Disable prepared statements.
# The flag is psycopg-specific; passing it to other dialects (sqlite in tests)
# raises, so scope it to postgres URLs.
_connect_args = {"prepare_threshold": None} if _db_url.startswith("postgres") else {}
engine = create_engine(
    _db_url,
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
