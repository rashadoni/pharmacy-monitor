"""products.brand_verified — recovered REAL consumer/manufacturer brand

Revision ID: 0010_product_brand_verified
Revises: 0009_product_url_dead_at
Create Date: 2026-05-31

The legacy `products.brand` column is polluted: `extract_brand()` falls back to
the product's first name-word when the catalog misses, so 92% of pharmonline /
81% of aptekonline rows store the GENERIC name ("Alaqanqal") instead of the firm
("Biola" / "Herba Flora"). That makes the matcher cluster different-brand
commodities (milk-thistle oil by Biola vs Herba Flora) into one match.

`brand_verified` holds the brand recovered from an AUTHORITATIVE source:
  - pharmonline: the brand token in the URL slug (matched to the brand vocab)
  - aptekonline: the `"brand":{...}` JSON embedded in the product page
  - aloe:        the already-clean scraped brand field

The matcher's brand-conflict guard only fires when BOTH sides have a confident
*consumer* brand here (manufacturer-company names are treated as
non-discriminating, so trade-name drugs like Konkor=Merck/Nycomed still match).

Idempotent (guard on existing column). After deploy:
`alembic stamp 0010_product_brand_verified` if the column already exists.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_product_brand_verified"
down_revision: str | None = "0009_product_url_dead_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(bind, table: str, column: str) -> bool:
    inspector = sa.inspect(bind)
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "products", "brand_verified"):
        return
    op.add_column("products", sa.Column("brand_verified", sa.String(length=200), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "products", "brand_verified"):
        op.drop_column("products", "brand_verified")
