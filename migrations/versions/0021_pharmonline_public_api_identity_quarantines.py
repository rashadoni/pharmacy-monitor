"""retain quarantined Pharmonline public-API identity splits

Revision ID: 0021_public_api_quarantines
Revises: 0020_public_api_admissions
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_public_api_quarantines"
down_revision: str | None = "0020_public_api_admissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _index_names(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if name not in _index_names(table):
        op.create_index(name, table, columns)


def upgrade() -> None:
    bind = op.get_bind()
    table = "pharmonline_public_api_identity_quarantines"
    if table not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            table,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("legacy_product_id", sa.Integer(), nullable=False),
            sa.Column("replacement_product_id", sa.Integer(), nullable=False),
            sa.Column("quarantine_kind", sa.String(length=40), nullable=False),
            sa.Column("legacy_external_id", sa.String(length=200), nullable=False),
            sa.Column("archived_external_id", sa.String(length=200), nullable=False),
            sa.Column("legacy_canonical_url", sa.String(length=500), nullable=False),
            sa.Column("public_api_external_id", sa.String(length=200), nullable=False),
            sa.Column("public_api_canonical_url", sa.String(length=500), nullable=False),
            sa.Column("proof_version", sa.String(length=40), nullable=False),
            sa.Column("source_manifest_sha256", sa.String(length=64), nullable=False),
            sa.Column("catalog_fingerprint_sha256", sa.String(length=64), nullable=False),
            sa.Column("source_transport", sa.String(length=40), nullable=False),
            sa.Column("preflight_run_ref", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["legacy_product_id"], ["products.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(
                ["replacement_product_id"], ["products.id"], ondelete="RESTRICT"
            ),
            sa.CheckConstraint(
                "legacy_product_id <> replacement_product_id",
                name="ck_pharmonline_public_api_quarantine_distinct_products",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "legacy_product_id",
                name="uq_pharmonline_public_api_quarantine_legacy_product",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "replacement_product_id",
                name="uq_pharmonline_public_api_quarantine_replacement_product",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "public_api_external_id",
                name="uq_pharmonline_public_api_quarantine_public_id",
            ),
        )
    for name, columns in (
        ("ix_pharmonline_public_api_quarantines_tenant_id", ["tenant_id"]),
        ("ix_pharmonline_public_api_quarantines_legacy_product_id", ["legacy_product_id"]),
        (
            "ix_pharmonline_public_api_quarantines_replacement_product_id",
            ["replacement_product_id"],
        ),
        ("ix_pharmonline_public_api_quarantines_public_id", ["public_api_external_id"]),
        ("ix_pharmonline_public_api_quarantines_created_at", ["created_at"]),
    ):
        _create_index_if_missing(name, table, columns)


def downgrade() -> None:
    table = "pharmonline_public_api_identity_quarantines"
    if table in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table(table)
