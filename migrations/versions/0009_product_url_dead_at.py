"""products.url_dead_at — phantom-404 link validation

Revision ID: 0009_product_url_dead_at
Revises: 0008_pricing_config
Create Date: 2026-05-30

aptekonline JSON API lists "phantom" catalog items whose product page returns
404. `validate-links` CLI sets this timestamp; comparison hides such products.

Idempotent: the prod column was first added via a manual ALTER TABLE (before this
migration existed — audit remediation), so guard on the existing column. After
deploy: `alembic stamp 0009_product_url_dead_at` if the column already exists.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_product_url_dead_at"
down_revision: str | None = "0008_pricing_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(bind, table: str, column: str) -> bool:
    inspector = sa.inspect(bind)
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "products", "url_dead_at"):
        return
    op.add_column("products", sa.Column("url_dead_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "products", "url_dead_at"):
        op.drop_column("products", "url_dead_at")
