"""serialize dashboard cost upserts with a database uniqueness invariant

Revision ID: 0016_supplier_price_unique
Revises: 0015_cost_import_batches
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_supplier_price_unique"
down_revision: str | None = "0015_cost_import_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_unique(bind) -> bool:
    inspector = sa.inspect(bind)
    return any(
        constraint.get("name") == "uq_supplier_price_product_supplier"
        for constraint in inspector.get_unique_constraints("supplier_prices")
    )


def upgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            "SELECT product_id, supplier_name FROM supplier_prices "
            "WHERE product_id IS NOT NULL GROUP BY product_id, supplier_name "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            "supplier_prices contains duplicate (product_id, supplier_name); "
            "resolve before applying 0016"
        )
    if not _has_unique(bind):
        with op.batch_alter_table("supplier_prices") as batch:
            batch.create_unique_constraint(
                "uq_supplier_price_product_supplier",
                ["product_id", "supplier_name"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_unique(bind):
        with op.batch_alter_table("supplier_prices") as batch:
            batch.drop_constraint("uq_supplier_price_product_supplier", type_="unique")
