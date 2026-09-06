import os
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from fpl_intelligence.config import get_settings

settings = get_settings()

#: Built-in placeholder the Settings model uses when no DATABASE_URL was set.
_DEFAULT_PG_PLACEHOLDER = "postgresql+psycopg://fpl:fpl@localhost:5432/fpl"
#: Local dev fallback so a fresh clone runs with zero configuration.
_DEFAULT_DEV_SQLITE = "sqlite:///./fpl_local.db"


def _normalize_postgres_driver(url: str) -> str:
    if url.startswith("postgresql+psycopg2://"):
        raise RuntimeError("DATABASE_URL must use the Psycopg 3 SQLAlchemy driver.")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _is_production_runtime() -> bool:
    return (
        settings.app_env.lower() == "production"
        or os.getenv("VERCEL") == "1"
        or os.getenv("VERCEL_ENV") == "production"
    )


def _effective_database_url() -> str:
    """Resolve the single source-of-truth DATABASE_URL with a graceful dev fallback.

    ``DATABASE_URL`` (via settings) is the one source of truth. When nothing was
    configured and we are not in a production context, development falls back to
    a local SQLite file. Production always requires an explicit PostgreSQL URL.
    """
    url = settings.database_url.strip()
    if url == _DEFAULT_PG_PLACEHOLDER or url.startswith("sqlite"):
        if _is_production_runtime():
            raise RuntimeError(
                "Production requires an explicit PostgreSQL DATABASE_URL; "
                "SQLite/dev fallback is disabled."
            )
        if url == _DEFAULT_PG_PLACEHOLDER:
            return _DEFAULT_DEV_SQLITE
    return _normalize_postgres_driver(url)


def validation_database_url() -> str:
    """Return the explicitly configured PostgreSQL URL for read-only validation."""
    url = settings.database_url.strip()
    if not url or url == _DEFAULT_PG_PLACEHOLDER:
        raise RuntimeError("DATABASE_URL is not configured for this validation run.")
    if url.startswith("sqlite"):
        raise RuntimeError("DATABASE_URL must point to PostgreSQL for this validation run.")
    url = _normalize_postgres_driver(url)
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("DATABASE_URL must use a PostgreSQL SQLAlchemy URL for validation.")
    return url


def _make_engine(url: str):
    """Create an engine safe for short-lived/serverless processes.

    Supabase transaction-mode pooling should own connection reuse. SQLAlchemy's
    client-side QueuePool can retain connections across warm serverless workers
    and multiply pressure on the pooler, so production PostgreSQL uses NullPool.
    """
    connect_args = {"prepare_threshold": None} if url.startswith("postgres") else {}
    pool_kwargs = {"poolclass": NullPool} if url.startswith("postgres") else {}
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args=connect_args,
        **pool_kwargs,
    )


def _validation_engine():
    """Build the engine shared by validation session factories."""
    return _make_engine(validation_database_url())


def validation_session_factory() -> sessionmaker[Session]:
    """Build a read-only session factory for the configured validation database."""
    validation_engine = _validation_engine()
    if validation_engine.url.drivername.startswith("postgres"):

        @event.listens_for(validation_engine, "begin")
        def _set_validation_transaction_read_only(connection) -> None:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    return sessionmaker(
        bind=validation_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def validation_write_session_factory() -> sessionmaker[Session]:
    """Build a write-capable session factory for controlled validation imports."""
    return sessionmaker(
        bind=_validation_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


_db_url = _effective_database_url()
engine = _make_engine(_db_url)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
