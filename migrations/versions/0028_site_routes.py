"""Sections of a site that a full scan walks, for profiles without `categories`.

Revision ID: 0028_site_routes
Revises: 0027_identity_resolutions
"""

from alembic import op
import sqlalchemy as sa

revision = "0028_site_routes"
down_revision = "0027_identity_resolutions"
branch_labels = None
depends_on = None

TABLE = "site_routes"

PREVIOUS_RELEASE_COMPATIBLE = (
    "Upgrade only creates one new empty table. The previous release neither reads "
    "nor writes it, and the pharmacy instance never fills it: its sections stay in "
    "`categories`."
)


def upgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        # 0001 builds the current model schema on an empty database.
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site", sa.String(40), nullable=False),
        sa.Column("slug", sa.String(500), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("site", "slug", name="uq_site_routes_site_slug"),
    )
    op.create_index("ix_site_routes_site", TABLE, ["site"])


def downgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    op.drop_index("ix_site_routes_site", table_name=TABLE)
    op.drop_table(TABLE)
