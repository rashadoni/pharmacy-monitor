"""PostgreSQL integration coverage for the session-level scrape lock."""

from __future__ import annotations

import os

import click
import pytest
from sqlalchemy import text

from src import main as main_mod
from src import storage


def _postgres_session_factory():
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL is required")
    return storage.make_session(database_url)


def _scrape_lock_rows(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT activity.state, activity.xact_start, activity.backend_xid
                FROM pg_locks AS locks
                JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
                WHERE locks.locktype = 'advisory'
                  AND locks.granted
                  AND locks.database = (
                      SELECT oid FROM pg_database WHERE datname = current_database()
                  )
                  AND locks.classid::bigint = CASE
                      WHEN hashtext(:key) < 0 THEN 4294967295::bigint
                      ELSE 0::bigint
                  END
                  AND locks.objid::bigint = (
                      hashtext(:key)::bigint & 4294967295::bigint
                  )
                  AND locks.objsubid = 1
                """
            ),
            {"key": main_mod._SCRAPE_ADVISORY_LOCK_KEY},
        ).all()


def test_scrape_lock_serializes_real_postgres_connections_without_open_transaction():
    session_factory = _postgres_session_factory()
    engine = session_factory.kw["bind"]
    first_context = click.Context(click.Command("first"))

    with first_context:
        assert main_mod._hold_scrape_lock_until_command_exit(
            session_factory,
            wait=False,
        )

        with click.Context(click.Command("second")):
            assert not main_mod._hold_scrape_lock_until_command_exit(
                session_factory,
                wait=False,
            )

        rows = _scrape_lock_rows(engine)
        assert len(rows) == 1
        assert rows[0].state == "idle"
        assert rows[0].xact_start is None
        assert rows[0].backend_xid is None

    assert _scrape_lock_rows(engine) == []

    with click.Context(click.Command("third")):
        assert main_mod._hold_scrape_lock_until_command_exit(
            session_factory,
            wait=False,
        )

    assert _scrape_lock_rows(engine) == []
