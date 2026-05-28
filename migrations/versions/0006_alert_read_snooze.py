"""alert_events.is_read, snoozed_until, read_at

Revision ID: 0006_alert_inbox
Revises: 0005_roi_cache
Create Date: 2026-05-17

P1.4 (PO Audit 2026-05-17): 500 alerts накопилось в БД, никто не читает.
Email доставка отключена, Telegram не настроен → инбокс мёртвый. Добавляем
in-app inbox с mark-read / snooze для управления потоком.

Поля on alert_events (global, не per-user — single-tenant pilot):
- is_read: bool, default false
- read_at: datetime, nullable
- snoozed_until: datetime, nullable

Multi-user сценарий вынесем в отдельную таблицу alert_user_state когда
будет нужно.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_alert_inbox"
down_revision: str | None = "0005_roi_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(bind, table: str, column: str) -> bool:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()

    if not _column_exists(bind, "alert_events", "is_read"):
        op.add_column(
            "alert_events",
            sa.Column("is_read", sa.Boolean, nullable=False, server_default="false"),
        )
    if not _column_exists(bind, "alert_events", "read_at"):
        op.add_column(
            "alert_events",
            sa.Column("read_at", sa.DateTime(timezone=False), nullable=True),
        )
    if not _column_exists(bind, "alert_events", "snoozed_until"):
        op.add_column(
            "alert_events",
            sa.Column("snoozed_until", sa.DateTime(timezone=False), nullable=True, index=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "alert_events", "snoozed_until"):
        op.drop_column("alert_events", "snoozed_until")
    if _column_exists(bind, "alert_events", "read_at"):
        op.drop_column("alert_events", "read_at")
    if _column_exists(bind, "alert_events", "is_read"):
        op.drop_column("alert_events", "is_read")
