from __future__ import annotations

from src import storage


def test_postgres_session_factory_is_cached(monkeypatch):
    storage._SESSION_FACTORY_CACHE.clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://pm:pw@127.0.0.1/db")

    first = storage.make_session()
    second = storage.make_session()

    assert first is second
    first.kw["bind"].dispose()
    storage._SESSION_FACTORY_CACHE.clear()


def test_sqlite_session_factory_is_not_cached():
    first = storage.make_session("sqlite:///:memory:")
    second = storage.make_session("sqlite:///:memory:")

    assert first is not second
    first.kw["bind"].dispose()
    second.kw["bind"].dispose()


def test_postgres_pool_limits_are_configurable(monkeypatch):
    storage._SESSION_FACTORY_CACHE.clear()
    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "2")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "7")
    monkeypatch.setenv("DB_POOL_RECYCLE", "60")

    engine = storage.make_engine("postgresql+psycopg://pm:pw@127.0.0.1/db")

    assert engine.pool.size() == 3
    assert engine.pool._max_overflow == 2
    assert engine.pool._timeout == 7
    assert engine.pool._recycle == 60
    assert engine.pool._pre_ping is True
    engine.dispose()
