"""switch Aloe product identity from slug to product number

Revision ID: 0024_aloe_product_numbers
Revises: 0023_snapshot_confirmed_run
Create Date: 2026-10-08

Until this revision an Aloe row in ``products`` was keyed by the product's URL
slug.  Aloe.az gives one slug to several different products (581 slugs in its
sitemap on 2026-10-08, other manufacturer, country and price), so they were
written as one row.  From this revision on the key is the product number the
site itself uses (``id`` in the listing, "Məhsul kodu" on the product page) and
``url`` is ``https://aloe.az/{number}/#{slug}``.

``upgrade`` moves no rows: only a scrape knows a product's number, so rows are
re-keyed by the scrape itself when it sees the product again
(``main._adopt_aloe_product_numbers``).  What ``upgrade`` installs is a trigger
on ``products``, and it does two jobs.

* It is the switch.  The application keys Aloe products by number only while
  the trigger exists (``main._aloe_number_identity_active``).  Code that is
  deployed before this migration is applied keeps writing slug identifiers, and
  ``downgrade`` switches the rule off again without deploying older code.
* It is the guard.  Code from before the switch looks an Aloe product up by
  slug: against re-keyed rows it finds nothing and would insert the whole Aloe
  catalogue a second time.  The trigger rejects exactly that insert — a row
  under a slug whose product is already stored under a number — whatever path
  the stale code arrived by.  The scrape fails loudly instead of duplicating.

``downgrade`` drops the trigger and gives the rows their slug identifiers back.
Stop the scrape timers first.  While this revision is the head, run it as
``alembic downgrade``; once another migration sits on top of it, see
docs/RUNBOOK.md, «Идентификаторы aloe: номер товара».

What ``downgrade`` cannot undo: products that got a row of their own because
they shared a slug keep that row under its number.  Slug-keyed code does not
see such rows; they go stale with the history they collected.

SQLite (development, tests) gets no trigger; there the rule is always on.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op

revision: str = "0024_aloe_product_numbers"
down_revision: str | None = "0023_snapshot_confirmed_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration.aloe_product_numbers")

# `main.ALOE_NUMBER_IDENTITY_TRIGGER` looks the trigger up by this name.
_TRIGGER = "products_aloe_number_identity"
# The old scraper wrote the first hundred characters of the slug.
_LEGACY_ID_CUT = 100
_BATCH = 500

# The lookup names the table through TG_TABLE_SCHEMA: the function must work
# whatever the caller's search_path is (a restore script sets it to '').
_CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {_TRIGGER}() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    slug text;
    taken boolean;
BEGIN
    IF NEW.site = 'aloe' AND NEW.external_id !~ '^[0-9]+$' THEN
        -- https://aloe.az/slug/ as slug-keyed code writes it.
        slug := substring(regexp_replace(NEW.url, '^https?://[^/]+/(ru/)?', '') from '^[^/?#]+');
        IF slug IS NOT NULL THEN
            -- Only a row keyed by number carries its slug after "/#".
            EXECUTE 'SELECT EXISTS (SELECT 1 FROM ' || quote_ident(TG_TABLE_SCHEMA)
                || '.products p WHERE p.site = $1'
                || ' AND right(p.url, length($2) + 2) = $3 || $2)'
                INTO taken USING 'aloe', slug, '/#';
            IF taken THEN
                RAISE EXCEPTION USING
                    MESSAGE = 'aloe product "' || NEW.external_id
                              || '" is already stored under its site number',
                    ERRCODE = 'check_violation',
                    HINT = 'Code older than migration {revision} is writing to this '
                           'database. See docs/RUNBOOK.md, aloe identifiers.';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END
$$
"""

# The scrape holds this advisory lock for its whole run (`src/run_lock.py`).
_SCRAPE_LOCK = "SELECT pg_try_advisory_xact_lock(hashtext('pharmacy_monitor_scrape'))"


def _products() -> sa.TableClause:
    return sa.table(
        "products",
        sa.column("id", sa.Integer()),
        sa.column("site", sa.String()),
        sa.column("external_id", sa.String()),
        sa.column("url", sa.String()),
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Verbatim, not through `text()`: the body has colons of its own. It must
    # not contain a percent sign either — the driver would read a placeholder.
    bind.exec_driver_sql(_CREATE_FUNCTION)
    # OR REPLACE rather than DROP + CREATE: dropping a trigger takes an ACCESS
    # EXCLUSIVE lock on `products`, creating one does not block readers.
    bind.exec_driver_sql(
        f"CREATE OR REPLACE TRIGGER {_TRIGGER} BEFORE INSERT ON products "
        f"FOR EACH ROW EXECUTE FUNCTION {_TRIGGER}()"
    )
    log.info("aloe products are keyed by number from the next scrape on")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # A scrape that started under the rule must not finish without it.
        if not bind.exec_driver_sql(_SCRAPE_LOCK).scalar():
            raise RuntimeError(
                "the scrape lock is held — a scrape is running, or a report is reading "
                "prices this very second: stop the timers, wait and run the downgrade again"
            )
        # Dropping the trigger needs an ACCESS EXCLUSIVE lock on `products`: do
        # not queue behind a long reader (the nightly dump) with every other
        # query piling up behind this one.
        bind.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
        # First: from here on the application writes slug identifiers again.
        bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON products")
        bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {_TRIGGER}()")
    products = _products()
    rows = bind.execute(
        sa.select(products.c.id, products.c.external_id, products.c.url)
        .where(products.c.site == "aloe")
        .order_by(products.c.id)
    ).all()
    taken = {row.external_id for row in rows}
    restored: list[dict[str, object]] = []
    kept_number = 0
    for row in rows:
        parts = urlsplit(row.url or "")
        slug = parts.fragment
        numbered = (
            row.external_id.isascii()
            and row.external_id.isdigit()
            and parts.path.strip("/") == row.external_id
        )
        if not numbered:
            continue
        legacy_id = slug[:_LEGACY_ID_CUT]
        if not slug or legacy_id in taken:
            # No slug to go back to, or the slug already names a row: an older
            # one that was never re-keyed, or the first of several products
            # sharing the slug.
            kept_number += 1
            continue
        taken.add(legacy_id)
        restored.append(
            {
                "row_id": row.id,
                "legacy_id": legacy_id,
                "legacy_url": f"{parts.scheme}://{parts.netloc}/{slug}/",
            }
        )
    statement = (
        products.update()
        .where(products.c.id == sa.bindparam("row_id"))
        .values(external_id=sa.bindparam("legacy_id"), url=sa.bindparam("legacy_url"))
    )
    for start in range(0, len(restored), _BATCH):
        bind.execute(statement, restored[start : start + _BATCH])
    log.warning(
        "aloe rows back under slug identifiers: %d; left under their number "
        "(no slug in the address, or the slug already names another row): %d",
        len(restored),
        kept_number,
    )
