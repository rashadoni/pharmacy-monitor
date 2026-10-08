"""queue of ROI cache refreshes that do not need a scrape

Revision ID: 0024_roi_refresh_requests
Revises: 0023_snapshot_confirmed_run
Create Date: 2026-10-07

The ROI recommendations cache is written at the end of a verified full scan,
and full scans are weekly.  Anything that changes the recommendations between
scans (pricing thresholds, purchase costs, a manual match edit) records a row
here; the server watcher drains the queue with
``pharmacy-monitor roi refresh --pending``.

Schema only.  The guard on an existing table is not decoration: every CLI
command calls ``storage.init_db()`` → ``create_all``, so on production the
table can be created by the first watcher tick after the code is rsynced,
before this migration runs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_roi_refresh_requests"
down_revision: str | None = "0023_snapshot_confirmed_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "roi_refresh_requests"
_INDEXES = {
    "ix_roi_refresh_requests_tenant_id": ["tenant_id"],
    "ix_roi_refresh_requests_status": ["status"],
    "ix_roi_refresh_requests_requested_at": ["requested_at"],
}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("reason", sa.String(length=40), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("requested_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("run_id", sa.Integer(), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
        )
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    for name, columns in _INDEXES.items():
        if name not in existing:
            op.create_index(name, _TABLE, columns)


def downgrade() -> None:
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(_TABLE)
