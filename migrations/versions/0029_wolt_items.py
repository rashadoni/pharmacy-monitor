"""Pharmacies on Wolt: their own items and scrape attempts.

Revision ID: 0029_wolt_items
Revises: 0028_site_routes
"""

from alembic import op
import sqlalchemy as sa

revision = "0029_wolt_items"
down_revision = "0028_site_routes"
branch_labels = None
depends_on = None

PREVIOUS_RELEASE_COMPATIBLE = (
    "Upgrade only creates two new empty tables. The previous release neither reads nor writes them."
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("wolt_items"):
        # 0001 builds the current model schema on an empty database.
        op.create_table(
            "wolt_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("venue", sa.String(100), nullable=False),
            sa.Column("item_id", sa.String(64), nullable=False),
            sa.Column("name", sa.String(500), nullable=False),
            sa.Column("price", sa.Float(), nullable=False),
            sa.Column("original_price", sa.Float(), nullable=True),
            sa.Column("in_stock", sa.Boolean(), nullable=False),
            sa.Column("in_catalog", sa.Boolean(), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("price_changed_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("venue", "item_id", name="uq_wolt_items_venue_item"),
        )
        op.create_index("ix_wolt_items_venue", "wolt_items", ["venue"])
    if not inspector.has_table("wolt_scrapes"):
        op.create_table(
            "wolt_scrapes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("venue", sa.String(100), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("items", sa.Integer(), nullable=True),
            sa.Column("error", sa.String(500), nullable=True),
        )
        op.create_index("ix_wolt_scrapes_venue", "wolt_scrapes", ["venue"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("wolt_scrapes"):
        op.drop_index("ix_wolt_scrapes_venue", table_name="wolt_scrapes")
        op.drop_table("wolt_scrapes")
    if inspector.has_table("wolt_items"):
        op.drop_index("ix_wolt_items_venue", table_name="wolt_items")
        op.drop_table("wolt_items")
