from __future__ import annotations

import pytest

from fpl_intelligence.db import session as db_session

def test_production_rejects_default_or_sqlite_database(monkeypatch):
    monkeypatch.setattr(db_session.settings, 'app_env', 'production')
    monkeypatch.delenv('VERCEL', raising=False)
    monkeypatch.delenv('VERCEL_ENV', raising=False)
    monkeypatch.setattr(db_session.settings, 'database_url', db_session._DEFAULT_PG_PLACEHOLDER)
    with pytest.raises(RuntimeError, match='Production requires an explicit PostgreSQL DATABASE_URL'):
        db_session._effective_database_url()
    monkeypatch.setattr(db_session.settings, 'database_url', 'sqlite:///tmp/fpl.db')
    with pytest.raises(RuntimeError, match='Production requires an explicit PostgreSQL DATABASE_URL'):
        db_session._effective_database_url()

def test_development_keeps_sqlite_fallback(monkeypatch):
    monkeypatch.setattr(db_session.settings, 'app_env', 'development')
    monkeypatch.delenv('VERCEL', raising=False)
    monkeypatch.delenv('VERCEL_ENV', raising=False)
    monkeypatch.setattr(db_session.settings, 'database_url', db_session._DEFAULT_PG_PLACEHOLDER)
    assert db_session._effective_database_url().startswith('sqlite:///')
