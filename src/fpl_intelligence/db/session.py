import os
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from fpl_intelligence.config import get_settings

settings = get_settings()

_DEFAULT_PG_PLACEHOLDER = "postgresql+psycopg://fpl:fpl@localhost:5432/fpl"
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
    url = settings.database_url.strip()
    if not url or url == _DEFAULT_PG_PLACEHOLDER:
        raise RuntimeError("DATABASE_URL is not configured for this validation run.")
    if url.startswith("sqlite"):
        raise RuntimeError("DATABASE_URL must point to PostgreSQL for this validation run.")
    url = _normalize_postgres_driver(url)
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError(
            "DATABASE_URL must use a PostgreSQL SQLAlchemy URL for this validation run."
        )
    return url


def _make_engine(url: str):
    """Create a bounded, serverless-safe SQLAlchemy engine.

    Supabase transaction pooling owns connection reuse. NullPool prevents each
    warm Vercel worker from retaining its own client-side pool. Explicit
    connect/query timeouts also prevent a transient pooler/auth outage from
    consuming the entire Vercel function duration.
    """
    if url.startswith("postgres"):
        connect_args = {
            "prepare_threshold": None,
            "connect_timeout": 4,
            "options": "-c statement_timeout=5000 -c idle_in_transaction_session_timeout=5000",
        }
        return create_engine(
            url,
            pool_pre_ping=True,
            poolclass=NullPool,
            connect_args=connect_args,
        )
    return create_engine(url, pool_pre_ping=True)


def _validation_engine():
    return _make_engine(validation_database_url())


def validation_session_factory() -> sessionmaker[Session]:
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
