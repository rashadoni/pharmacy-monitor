"""add normalized_attrs/normalized_at/normalize_hash to products + match_strategy to matches

Revision ID: 0004_norm_attrs
Revises: 0003_notif_prefs
Create Date: 2026-05-16
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_norm_attrs"
down_revision: str | None = "0003_notif_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(bind, table: str, column: str) -> bool:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    cols = {c["name"] for c in inspector.get_columns(table)}
    return column in cols


def _index_exists(bind, table: str, index_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()

    with op.batch_alter_table("products", schema=None) as batch_op:
        if not _column_exists(bind, "products", "normalized_attrs"):
            batch_op.add_column(sa.Column("normalized_attrs", sa.JSON, nullable=True))
        if not _column_exists(bind, "products", "normalized_at"):
            batch_op.add_column(sa.Column("normalized_at", sa.DateTime, nullable=True))
        if not _column_exists(bind, "products", "normalize_hash"):
            batch_op.add_column(
                sa.Column("normalize_hash", sa.String(64), nullable=True)
            )

    if not _index_exists(bind, "products", "ix_products_normalized_at"):
        op.create_index(
            "ix_products_normalized_at", "products", ["normalized_at"], unique=False
        )
    if not _index_exists(bind, "products", "ix_products_normalize_hash"):
        op.create_index(
            "ix_products_normalize_hash", "products", ["normalize_hash"], unique=False
        )

    with op.batch_alter_table("matches", schema=None) as batch_op:
        if not _column_exists(bind, "matches", "match_strategy"):
            batch_op.add_column(
                sa.Column("match_strategy", sa.String(30), nullable=True)
            )


def downgrade() -> None:
    bind = op.get_bind()

    if _index_exists(bind, "products", "ix_products_normalize_hash"):
        op.drop_index("ix_products_normalize_hash", table_name="products")
    if _index_exists(bind, "products", "ix_products_normalized_at"):
        op.drop_index("ix_products_normalized_at", table_name="products")

    with op.batch_alter_table("products", schema=None) as batch_op:
        for col in ("normalize_hash", "normalized_at", "normalized_attrs"):
            if _column_exists(bind, "products", col):
                batch_op.drop_column(col)

    with op.batch_alter_table("matches", schema=None) as batch_op:
        if _column_exists(bind, "matches", "match_strategy"):
            batch_op.drop_column("match_strategy")
