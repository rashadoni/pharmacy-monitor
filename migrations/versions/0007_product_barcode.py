"""products.barcode + idx_products_barcode

Revision ID: 0007_product_barcode
Revises: 0006_alert_inbox
Create Date: 2026-05-27

Phase 2.1 of quality roadmap (~/.claude/plans/rosy-launching-lamport.md).

Adds `barcode` column to Product. In pharma, barcode, GTIN, EAN-13, UPC all
represent the same identifier — we collapse them into one canonical string
column, indexed for matcher v2 lookup.

Format: digits-only string, no whitespace, no leading-zero stripping (so
"04607017950021" stays distinct from "4607017950021" until the matcher
normalises). Nullable — older rows scraped before Phase 2.2 have no value.

`barcode_source` is OUT of scope for now — if we ever need to debug which
scraper provided which value, we can add a separate column or audit via
git blame on the scraper.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_product_barcode"
down_revision: str | None = "0006_alert_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(bind, table: str, column: str) -> bool:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def _index_exists(bind, table: str, index_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(ix["name"] == index_name for ix in inspector.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()

    if not _column_exists(bind, "products", "barcode"):
        op.add_column(
            "products",
            sa.Column("barcode", sa.String(length=40), nullable=True),
        )
    # Use a NAMED index so downgrade can find it portably across SQLite/Postgres.
    if not _index_exists(bind, "products", "ix_products_barcode"):
        op.create_index("ix_products_barcode", "products", ["barcode"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if _index_exists(bind, "products", "ix_products_barcode"):
        op.drop_index("ix_products_barcode", table_name="products")
    if _column_exists(bind, "products", "barcode"):
        op.drop_column("products", "barcode")
