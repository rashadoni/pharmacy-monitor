"""add reversible purchase-cost import batches

Revision ID: 0015_cost_import_batches
Revises: 0014_audit_logs
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_cost_import_batches"
down_revision: str | None = "0014_audit_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "cost_import_batches" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "cost_import_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("rows_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_imported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("changes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("rolled_back_at", sa.DateTime(), nullable=True),
        sa.Column("rolled_back_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["actor_user_id"], ["tenant_users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["rolled_back_by_user_id"], ["tenant_users.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_cost_import_batches_tenant_id", "cost_import_batches", ["tenant_id"])
    op.create_index("ix_cost_import_batches_created_at", "cost_import_batches", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if "cost_import_batches" in sa.inspect(bind).get_table_names():
        op.drop_table("cost_import_batches")
