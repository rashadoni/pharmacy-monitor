"""retain immutable Pharmonline public-API recovery proofs

Revision ID: 0019_public_api_reconcile
Revises: 0018_product_manual_category
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_public_api_reconcile"
down_revision: str | None = "0018_product_manual_category"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _index_names(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if name not in _index_names(table):
        op.create_index(name, table, columns)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    reconciliation_table = "pharmonline_public_api_identity_reconciliations"
    if reconciliation_table not in tables:
        op.create_table(
            reconciliation_table,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("legacy_external_id", sa.String(length=200), nullable=False),
            sa.Column("public_api_external_id", sa.String(length=200), nullable=False),
            sa.Column("legacy_canonical_url", sa.String(length=500), nullable=False),
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
                name="uq_pharmonline_public_api_reconciliation_product",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "legacy_external_id",
                name="uq_pharmonline_public_api_reconciliation_legacy_id",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "public_api_external_id",
                name="uq_pharmonline_public_api_reconciliation_public_id",
            ),
        )
    for name, columns in (
        ("ix_pharmonline_public_api_reconciliations_tenant_id", ["tenant_id"]),
        ("ix_pharmonline_public_api_reconciliations_product_id", ["product_id"]),
        (
            "ix_pharmonline_public_api_reconciliations_public_id",
            ["public_api_external_id"],
        ),
        ("ix_pharmonline_public_api_reconciliations_created_at", ["created_at"]),
    ):
        _create_index_if_missing(name, reconciliation_table, columns)

    baseline_table = "pharmonline_public_api_catalog_baselines"
    if baseline_table not in tables:
        op.create_table(
            baseline_table,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("catalog_item_count", sa.Integer(), nullable=False),
            sa.Column("minimum_catalog_item_count", sa.Integer(), nullable=False),
            sa.Column("verified_identity_count", sa.Integer(), nullable=False),
            sa.Column("trusted_ddp_item_count", sa.Integer(), nullable=False),
            sa.Column("retired_ddp_item_count", sa.Integer(), nullable=False),
            sa.Column("reconciled_item_count", sa.Integer(), nullable=False),
            sa.Column("proof_version", sa.String(length=40), nullable=False),
            sa.Column("source_manifest_sha256", sa.String(length=64), nullable=False),
            sa.Column("catalog_fingerprint_sha256", sa.String(length=64), nullable=False),
            sa.Column("source_transport", sa.String(length=40), nullable=False),
            sa.Column("preflight_run_ref", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    for name, columns in (
        ("ix_pharmonline_public_api_catalog_baselines_tenant_id", ["tenant_id"]),
        ("ix_pharmonline_public_api_catalog_baselines_created_at", ["created_at"]),
    ):
        _create_index_if_missing(name, baseline_table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in (
        "pharmonline_public_api_catalog_baselines",
        "pharmonline_public_api_identity_reconciliations",
    ):
        if table in tables:
            op.drop_table(table)
