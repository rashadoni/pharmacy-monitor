"""scrape Aloe's cosmetics and hygiene sections

Revision ID: 0022_aloe_cosmetics_hygiene
Revises: 0021_public_api_quarantines
Create Date: 2026-10-07

Aloe.az has six top-level sections; the full scan walks the rows of
``categories`` that carry an ``aloe_slug`` and only three of the six were ever
entered (``dermanlar``, ``bad``, ``usaq-dunyasi`` plus the sub-sections
``tibbi-vasitələr``/``uşaq-qidası`` and the bestseller filter).  ``kosmetika``
(149 listing pages) and ``gigiyena`` (26) were never scraped — about 1,950
products measured on 2026-10-07.  ``novooptika-lcsye`` (frames and glasses) is
left out on purpose: it is not pharmacy assortment.

The rows are data, not schema, but they live in a migration so that the write
reaches production through ``deploy.yml`` with ``apply_migrations`` — behind
its verified backup — instead of a hand-typed statement.

It only EXTENDS an Aloe route list that already exists.  On a database with no
Aloe route at all (a fresh server, CI) it inserts nothing: two rows there would
be the whole list, and a scan of cosmetics and hygiene alone would pass as a
verified full catalogue where an empty table used to refuse to run.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0022_aloe_cosmetics_hygiene"
down_revision: str | None = "0021_public_api_quarantines"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Child of Alembic's own logger, so the lines land in the deploy log next to
# "Running upgrade …".
log = logging.getLogger("alembic.runtime.migration.aloe_sections")


# (key, label_ru, label_az, aloe_slug) — keys follow `aloe_dermanlar`.
_ROUTES = (
    ("aloe_kosmetika", "Косметика", "Kosmetika", "kosmetika"),
    ("aloe_gigiyena", "Гигиена", "Gigiyena", "gigiyena"),
)


def _categories() -> sa.TableClause:
    return sa.table(
        "categories",
        sa.column("key", sa.String()),
        sa.column("label_ru", sa.String()),
        sa.column("label_az", sa.String()),
        sa.column("pharmonline_slug", sa.String()),
        sa.column("aptekonline_slug", sa.String()),
        sa.column("aloe_slug", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime()),
    )


def upgrade() -> None:
    categories = _categories()
    bind = op.get_bind()
    # Naive UTC, like `Category.created_at` written by the application; the
    # production server is not on UTC, so a database-side now() would differ.
    created_at = datetime.now(UTC).replace(tzinfo=None)
    configured = bind.scalar(
        sa.select(sa.func.count())
        .select_from(categories)
        .where(categories.c.aloe_slug.is_not(None))
    )
    if not configured:
        log.warning("no aloe route in categories yet: cosmetics and hygiene not added")
        return
    for key, label_ru, label_az, aloe_slug in _ROUTES:
        # An operator may already have entered the section by hand (`category
        # add`, the dashboard), possibly switched off or under another key.
        # Leave any such row exactly as it is: overwriting it would undo a
        # deliberate choice, and a second row with the same slug would make the
        # scan walk the section twice.
        taken = bind.scalar(
            sa.select(sa.func.count())
            .select_from(categories)
            .where(sa.or_(categories.c.key == key, categories.c.aloe_slug == aloe_slug))
        )
        if taken:
            # Say so: a deploy that goes green while the section is still switched
            # off, or filed under another key, must not look like a new route.
            log.warning("aloe section %s already present in categories, left as is", aloe_slug)
            continue
        log.info("aloe section %s added as category %s", aloe_slug, key)
        bind.execute(
            categories.insert().values(
                key=key,
                label_ru=label_ru,
                label_az=label_az,
                aloe_slug=aloe_slug,
                is_active=True,
                created_at=created_at,
            )
        )


def downgrade() -> None:
    categories = _categories()
    for key, label_ru, label_az, aloe_slug in _ROUTES:
        # Remove only a row that still looks exactly like the one inserted
        # above; anything an operator has since edited or switched off stays.
        # The delete cascades to `tracked_categories`: a pin the client put on
        # the section goes with it. Products already scraped keep their slug.
        op.execute(
            categories.delete()
            .where(categories.c.key == key)
            .where(categories.c.aloe_slug == aloe_slug)
            .where(categories.c.label_ru == label_ru)
            .where(categories.c.label_az == label_az)
            .where(categories.c.pharmonline_slug.is_(None))
            .where(categories.c.aptekonline_slug.is_(None))
            .where(categories.c.is_active.is_(True))
        )
