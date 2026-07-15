"""persist per-run scrape quality telemetry

Revision ID: 0013_run_quality
Revises: 0012_category_az_labels
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_run_quality"
down_revision: str | None = "0012_category_az_labels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(bind, table: str, column: str) -> bool:
    inspector = sa.inspect(bind)
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "runs", "run_quality"):
        op.add_column("runs", sa.Column("run_quality", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "runs", "run_quality"):
        op.drop_column("runs", "run_quality")
