"""retain immutable Pharmonline public-API identity admissions

Revision ID: 0020_public_api_admissions
Revises: 0019_public_api_reconcile
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_public_api_admissions"
down_revision: str | None = "0019_public_api_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _index_names(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if name not in _index_names(table):
        op.create_index(name, table, columns)


def upgrade() -> None:
    bind = op.get_bind()
    table = "pharmonline_public_api_identity_admissions"
    if table not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            table,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("admission_kind", sa.String(length=40), nullable=False),
            sa.Column("public_api_external_id", sa.String(length=200), nullable=False),
            sa.Column("public_api_canonical_url", sa.String(length=500), nullable=False),
            sa.Column("proof_version", sa.String(length=40), nullable=False),
            sa.Column("source_manifest_sha256", sa.String(length=64), nullable=False),
            sa.Column("catalog_fingerprint_sha256", sa.String(length=64), nullable=False),
            sa.Column("source_transport", sa.String(length=40), nullable=False),
            sa.Column("preflight_run_ref", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint(
                "tenant_id",
                "product_id",
                name="uq_pharmonline_public_api_admission_product",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "public_api_external_id",
                name="uq_pharmonline_public_api_admission_public_id",
            ),
        )
    for name, columns in (
        ("ix_pharmonline_public_api_admissions_tenant_id", ["tenant_id"]),
        ("ix_pharmonline_public_api_admissions_product_id", ["product_id"]),
        ("ix_pharmonline_public_api_admissions_public_id", ["public_api_external_id"]),
        ("ix_pharmonline_public_api_admissions_created_at", ["created_at"]),
    ):
        _create_index_if_missing(name, table, columns)


def downgrade() -> None:
    table = "pharmonline_public_api_identity_admissions"
    if table in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table(table)
