"""manual canonical category override for products

Revision ID: 0018_product_manual_category
Revises: 0017_product_identity_offer
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_product_manual_category"
down_revision: str | None = "0017_product_identity_offer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("products")}
    if "manual_category_key" not in columns:
        op.add_column(
            "products",
            sa.Column("manual_category_key", sa.String(100), nullable=True),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("products")}
    if "ix_products_manual_category_key" not in indexes:
        op.create_index(
            "ix_products_manual_category_key",
            "products",
            ["manual_category_key"],
        )


def downgrade() -> None:
    op.drop_index("ix_products_manual_category_key", table_name="products")
    op.drop_column("products", "manual_category_key")
