"""add tenant_id to alert_events, tracked_products, match_rejections, saved_views, promos

These tables had data-level tenant_id added via storage._apply_lightweight_migrations()
during the pilot, but the models themselves didn't have mapped columns. Now the models
do — and this migration ensures the schema matches on freshly-provisioned Postgres
DBs (where the lightweight migration didn't run because the table is created fresh
by alembic, not by SQLAlchemy.create_all + ALTER).

Idempotent: each ADD COLUMN is wrapped in a "does this column exist?" check.

Revision ID: 0002_tenant_id
Revises: 0001_initial
Create Date: 2026-04-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_tenant_id"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLES_NEEDING_TENANT_ID = [
    "alert_events",
    "tracked_products",
    "match_rejections",
    "saved_views",
    "promos",
]


def _column_exists(bind, table: str, column: str) -> bool:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    cols = {c["name"] for c in inspector.get_columns(table)}
    return column in cols


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES_NEEDING_TENANT_ID:
        if _column_exists(bind, table, "tenant_id"):
            continue
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1")
            )
            batch_op.create_index(f"ix_{table}_tenant_id", ["tenant_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in TABLES_NEEDING_TENANT_ID:
        if not _column_exists(bind, table, "tenant_id"):
            continue
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(f"ix_{table}_tenant_id")
            batch_op.drop_column("tenant_id")
