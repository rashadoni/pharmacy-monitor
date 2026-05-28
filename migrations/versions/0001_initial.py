"""initial schema (snapshot of pilot DB models)

This migration is the baseline. It uses `Base.metadata.create_all` strategy
because the pilot SQLite DB was built without alembic. Subsequent migrations
will be auto-generated via `alembic revision --autogenerate`.

Idempotent: if tables already exist (pilot DB), this is a no-op.

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-29
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from src.storage import Base

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all tables defined in src.storage.Base if they don't exist."""
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    """Drop all tables. Use with extreme caution — this nukes data."""
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
