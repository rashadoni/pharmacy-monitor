"""Phase 4.1 — per-tenant pricing config table

Revision ID: 0008_pricing_config
Revises: 0007_product_barcode
Create Date: 2026-05-27

Replaces hardcoded ROI thresholds in compute_actions() with DB-backed
per-tenant settings. Single row per tenant_id (UNIQUE constraint). Default
seed for tenant_id=1 matches the previous hardcoded values exactly, so
behaviour is bit-identical until the user edits via settings UI.

Default values (from previous compute_actions() signature):
  raise_threshold_pct = 5.0
  undercut_threshold_pct = 3.0
  max_spread_pct = 80.0
  min_margin_pct = 10.0  (was inline in _undercut_threats; now configurable)
  max_per_type = 10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_pricing_config"
down_revision: str | None = "0007_product_barcode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind, table: str) -> bool:
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "pricing_config"):
        return

    op.create_table(
        "pricing_config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, nullable=False, index=True, unique=True),
        sa.Column("raise_threshold_pct", sa.Float, nullable=False, server_default="5.0"),
        sa.Column("undercut_threshold_pct", sa.Float, nullable=False, server_default="3.0"),
        sa.Column("max_spread_pct", sa.Float, nullable=False, server_default="80.0"),
        sa.Column("min_margin_pct", sa.Float, nullable=False, server_default="10.0"),
        sa.Column("max_per_type", sa.Integer, nullable=False, server_default="10"),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    # Seed default row for the single existing tenant.
    op.execute(
        "INSERT INTO pricing_config (tenant_id, raise_threshold_pct, "
        "undercut_threshold_pct, max_spread_pct, min_margin_pct, max_per_type) "
        "VALUES (1, 5.0, 3.0, 80.0, 10.0, 10) "
        "ON CONFLICT (tenant_id) DO NOTHING"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "pricing_config"):
        op.drop_table("pricing_config")
