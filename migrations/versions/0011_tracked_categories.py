"""tracked categories for watchlist

Revision ID: 0011_tracked_categories
Revises: 0010_product_brand_verified
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_tracked_categories"
down_revision: str | None = "0010_product_brand_verified"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(bind, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "tracked_categories"):
        return
    op.create_table(
        "tracked_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tenant_id", "category_id", name="uq_tracked_category_tenant_category"
        ),
    )
    op.create_index(
        "ix_tracked_categories_tenant_id", "tracked_categories", ["tenant_id"]
    )
    op.create_index(
        "ix_tracked_categories_category_id", "tracked_categories", ["category_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "tracked_categories"):
        op.drop_index("ix_tracked_categories_category_id", table_name="tracked_categories")
        op.drop_index("ix_tracked_categories_tenant_id", table_name="tracked_categories")
        op.drop_table("tracked_categories")
