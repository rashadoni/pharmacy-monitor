"""Alembic env. Reads DATABASE_URL from environment, supports SQLite + Postgres."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from src.storage import Base, create_engine_hiding_statement_data  # noqa: E402

# Load .env if present so local dev "just works"
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

config = context.config

# Override sqlalchemy.url from env
db_url = os.environ.get("DATABASE_URL", "sqlite:///data/db.sqlite")
config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


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
    connectable = create_engine_hiding_statement_data(db_url, poolclass=pool.NullPool)
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
