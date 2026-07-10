"""fill missing Azerbaijani category labels

Revision ID: 0012_category_az_labels
Revises: 0011_tracked_categories
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_category_az_labels"
down_revision: str | None = "0011_tracked_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LABELS = {
    "bestsellers_aloe": "Aloe.az bestsellerləri",
    "bakterial_aptek": "Antibakterial vasitələr",
}


def upgrade() -> None:
    categories = sa.table(
        "categories",
        sa.column("key", sa.String()),
        sa.column("label_az", sa.String()),
    )
    for key, label_az in _LABELS.items():
        op.execute(
            categories.update()
            .where(categories.c.key == key)
            .where(
                sa.or_(
                    categories.c.label_az.is_(None),
                    sa.func.trim(categories.c.label_az) == "",
                )
            )
            .values(label_az=label_az)
        )


def downgrade() -> None:
    categories = sa.table(
        "categories",
        sa.column("key", sa.String()),
        sa.column("label_az", sa.String()),
    )
    for key, label_az in _LABELS.items():
        op.execute(
            categories.update()
            .where(categories.c.key == key)
            .where(categories.c.label_az == label_az)
            .values(label_az=None)
        )
