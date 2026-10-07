"""let a verified full scan confirm a price it saw unchanged

Revision ID: 0023_snapshot_confirmed_run
Revises: 0022_aloe_cosmetics_hygiene
Create Date: 2026-10-07

``price_snapshots`` is a change log: a row is written only when the price
differs from the product's latest row.  Money-facing readers accept rows of
verified full scans only.  A price first written by a run that is not one (a
run from before the verification existed, a partial tick, a scan that failed
verification) therefore stayed untrusted for as long as it did not change —
85% of the catalogue on 2026-10-07.

``confirmed_run_id`` names the verified full scan that later saw the product at
this very price.  Schema only: the column is filled by the pipeline
(``storage.confirm_prices_observed_by_run``) on the next verified scan.

No index on purpose: readers reach snapshots through ``product_id`` and only
then test trust, and the table is small enough for the ``ON DELETE SET NULL``
scan when a run is deleted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_snapshot_confirmed_run"
down_revision: str | None = "0022_aloe_cosmetics_hygiene"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_NAME = "fk_price_snapshots_confirmed_run_id_runs"


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("price_snapshots")}
    if "confirmed_run_id" in columns:
        return
    if bind.dialect.name == "sqlite":
        # SQLite cannot add a foreign key to an existing table; dev databases
        # get the plain column, the model still declares the relationship.
        op.add_column("price_snapshots", sa.Column("confirmed_run_id", sa.Integer(), nullable=True))
        return
    op.add_column(
        "price_snapshots",
        sa.Column(
            "confirmed_run_id",
            sa.Integer(),
            sa.ForeignKey("runs.id", name=_FK_NAME, ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # A fresh SQLite database gets the column from ``Base.metadata`` with
        # its foreign key inline, and SQLite refuses to drop such a column in
        # place — rebuild the table.
        with op.batch_alter_table("price_snapshots") as batch:
            batch.drop_column("confirmed_run_id")
        return
    # PostgreSQL drops the column's own foreign key with it, whatever its name:
    # on a database built from the models the constraint is auto-named.
    op.drop_column("price_snapshots", "confirmed_run_id")
