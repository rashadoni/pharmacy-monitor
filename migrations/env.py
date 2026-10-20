"""Alembic env. Reads DATABASE_URL from environment, supports SQLite + Postgres."""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import make_url

# This file needs the src of its own commit: the engine factory below came with
# PR #86. deploy.yml therefore runs it from the staged copy of the release and
# never copies it into the live directory on the server, where a rollback could
# leave an older src next to it (tests/test_deploy_workflow.py).
from src.storage import Base, create_engine_hiding_statement_data  # noqa: E402

# Load the checkout's own .env if present so local dev "just works".  The path
# is explicit: without one python-dotenv walks up the directory tree, and a
# staged copy of the code on the server would pick up a file it never shipped
# with.  The process environment wins, as in src/main.py.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

config = context.config

# Override sqlalchemy.url from env
db_url = os.environ.get("DATABASE_URL", "sqlite:///data/db.sqlite")
config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# DDL takes ACCESS EXCLUSIVE table locks.  PostgreSQL's default is to wait for
# them without limit, and while a migration waits behind one long reader every
# other query on that table waits behind the migration: one slow report turns
# an ALTER TABLE into an API outage.  Fail fast instead.  The upgrade is a
# single transaction on PostgreSQL, so an attempt that times out changes
# nothing and can simply be repeated (deploy.yml does, a bounded number of
# times).  MIGRATION_LOCK_TIMEOUT_MS overrides the limit; 0 removes it.
DEFAULT_LOCK_TIMEOUT_MS = 5000


def _lock_timeout_ms() -> int:
    raw = os.environ.get("MIGRATION_LOCK_TIMEOUT_MS", "").strip()
    if not raw:
        return DEFAULT_LOCK_TIMEOUT_MS
    if not raw.isdigit():
        raise ValueError(
            f"MIGRATION_LOCK_TIMEOUT_MS must be a whole number of milliseconds, got {raw!r}"
        )
    return int(raw)


def _connect_args() -> dict[str, str]:
    if not db_url.startswith("postgresql"):
        return {}
    # A startup option rather than a SET statement: a statement would open a
    # transaction on the connection before Alembic begins its own.
    options = f"-c lock_timeout={_lock_timeout_ms()}"
    existing = make_url(db_url).query.get("options")
    if isinstance(existing, str) and existing:
        options = f"{existing} {options}"
    return {"options": options}


def run_migrations_offline() -> None:
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=db_url.startswith("sqlite"),  # SQLite needs batch ops for ALTER
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Not `engine_from_config`: `alembic upgrade` runs from the deploy workflow,
    # whose step log is public, and a failed data migration must not print the
    # row it failed on. alembic.ini sets no engine option besides the URL.
    connectable = create_engine_hiding_statement_data(
        db_url, poolclass=pool.NullPool, connect_args=_connect_args()
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=db_url.startswith("sqlite"),
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
