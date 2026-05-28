"""add notification prefs to tenant_users (telegram_chat_id, severity mins, quiet_hours, digest opt-in)

Revision ID: 0003_notif_prefs
Revises: 0002_tenant_id
Create Date: 2026-04-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_notif_prefs"
down_revision: str | None = "0002_tenant_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


NEW_COLUMNS = [
    ("telegram_chat_id", sa.String(50), True),
    ("email_severity_min", sa.String(20), True),
    ("telegram_severity_min", sa.String(20), True),
    ("quiet_hours", sa.String(20), True),
    ("daily_digest", sa.Boolean(), False, "0"),
    ("weekly_digest", sa.Boolean(), False, "0"),
]


def _column_exists(bind, table: str, column: str) -> bool:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    cols = {c["name"] for c in inspector.get_columns(table)}
    return column in cols


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("tenant_users", schema=None) as batch_op:
        for col_def in NEW_COLUMNS:
            name = col_def[0]
            type_ = col_def[1]
            nullable = col_def[2]
            if _column_exists(bind, "tenant_users", name):
                continue
            kwargs = {"nullable": nullable}
            if len(col_def) > 3:
                kwargs["server_default"] = col_def[3]
            batch_op.add_column(sa.Column(name, type_, **kwargs))

    # Create index on telegram_chat_id for reverse lookup (chat_id → user)
    if not any(
        idx["name"] == "ix_tenant_users_telegram_chat_id"
        for idx in sa.inspect(bind).get_indexes("tenant_users")
    ):
        op.create_index(
            "ix_tenant_users_telegram_chat_id",
            "tenant_users",
            ["telegram_chat_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_tenant_users_telegram_chat_id", table_name="tenant_users")
    with op.batch_alter_table("tenant_users", schema=None) as batch_op:
        for col_def in reversed(NEW_COLUMNS):
            batch_op.drop_column(col_def[0])
