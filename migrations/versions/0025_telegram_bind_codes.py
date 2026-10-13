"""one-time codes that bind a Telegram chat to an account

Revision ID: 0025_telegram_bind_codes
Revises: 0024_aloe_product_numbers
Create Date: 2026-10-09

Until this revision the bot bound a chat to whoever's e-mail address was typed
after ``/start``: knowing the address was enough.  Now the dashboard issues a
one-time code to the signed-in user and the bot accepts only that code
(``src/telegram_binding.py``).

``telegram_bind_codes`` holds the SHA-256 of a pending code, one row per user.
``telegram_bind_attempts`` counts failed attempts per chat, so the limit
survives a restart of the bot.

Schema only, two new tables, nothing existing is touched: code from before this
revision keeps working on the new schema, and the new code on the old schema
fails only in the binding itself.  The guard on an existing table is not
decoration: every CLI command calls ``storage.init_db()`` → ``create_all``, so
on production the tables can be created by the first timer tick after the code
is rsynced, before this migration runs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_telegram_bind_codes"
down_revision: str | None = "0024_aloe_product_numbers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODES = "telegram_bind_codes"
_ATTEMPTS = "telegram_bind_attempts"


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if _CODES not in existing:
        op.create_table(
            _CODES,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("tenant_users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("code_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", name="uq_telegram_bind_codes_user_id"),
            sa.UniqueConstraint("code_hash", name="uq_telegram_bind_codes_code_hash"),
        )
    if _ATTEMPTS not in existing:
        op.create_table(
            _ATTEMPTS,
            sa.Column("chat_id", sa.String(length=50), primary_key=True),
            sa.Column("failures", sa.Integer(), nullable=False),
            sa.Column("window_started_at", sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in (_ATTEMPTS, _CODES):
        if table in existing:
            op.drop_table(table)
