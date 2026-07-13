"""trusted country identity and website offer observations

Revision ID: 0017_product_identity_offer
Revises: 0016_supplier_price_unique
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_product_identity_offer"
down_revision: str | None = "0016_supplier_price_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _index_names(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _add_column(table: str, column: sa.Column) -> None:
    if column.name not in _column_names(table):
        op.add_column(table, column)


def _create_index(name: str, table: str, columns: list[str]) -> None:
    if name not in _index_names(table):
        op.create_index(name, table, columns)


def upgrade() -> None:
    _add_column(
        "runs",
        sa.Column(
            "catalog_scope", sa.String(20), nullable=False, server_default="unknown"
        ),
    )
    _add_column("runs", sa.Column("full_catalog_sites", sa.String(200)))
    _add_column(
        "runs",
        sa.Column("catalog_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    _add_column("runs", sa.Column("catalog_verification_reason", sa.String(300)))
    _create_index("ix_runs_catalog_scope", "runs", ["catalog_scope"])
    _create_index("ix_runs_catalog_verified", "runs", ["catalog_verified"])

    _add_column(
        "roi_actions_cache",
        sa.Column(
            "policy_fingerprint",
            sa.String(80),
            nullable=False,
            server_default="legacy",
        ),
    )
    _add_column(
        "roi_actions_cache",
        sa.Column(
            "trust_epoch",
            sa.String(240),
            nullable=False,
            server_default="legacy",
        ),
    )

    _add_column("products", sa.Column("manufacturer_country_code", sa.String(2)))
    _add_column("products", sa.Column("manufacturer_country_raw", sa.String(160)))
    _add_column(
        "products",
        sa.Column(
            "country_resolution_status",
            sa.String(20),
            nullable=False,
            server_default="unknown",
        ),
    )
    _add_column("products", sa.Column("country_source", sa.String(40)))
    _add_column("products", sa.Column("country_observed_at", sa.DateTime()))
    _add_column("products", sa.Column("country_candidate_code", sa.String(2)))
    _add_column(
        "products",
        sa.Column(
            "country_candidate_seen_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    _add_column("products", sa.Column("country_candidate_observed_at", sa.DateTime()))
    _add_column("products", sa.Column("country_candidate_run_id", sa.Integer()))
    _add_column(
        "products",
        sa.Column(
            "offer_availability_status",
            sa.String(20),
            nullable=False,
            server_default="unknown",
        ),
    )
    _add_column("products", sa.Column("offer_quantity", sa.Float()))
    _add_column("products", sa.Column("availability_source", sa.String(40)))
    _add_column("products", sa.Column("availability_observed_at", sa.DateTime()))
    _add_column("products", sa.Column("availability_run_id", sa.Integer()))
    for name, columns in (
        ("ix_products_manufacturer_country_code", ["manufacturer_country_code"]),
        ("ix_products_country_resolution_status", ["country_resolution_status"]),
        ("ix_products_offer_availability_status", ["offer_availability_status"]),
        ("ix_products_availability_run_id", ["availability_run_id"]),
    ):
        _create_index(name, "products", columns)

    _add_column(
        "match_rejections",
        sa.Column("reason_type", sa.String(40), nullable=False, server_default="manual"),
    )
    _add_column("match_rejections", sa.Column("metadata_json", sa.JSON()))
    _add_column(
        "match_rejections",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    _add_column("match_rejections", sa.Column("resolved_at", sa.DateTime()))
    _add_column("match_rejections", sa.Column("updated_at", sa.DateTime()))
    op.execute(sa.text("UPDATE match_rejections SET updated_at = created_at"))
    _create_index("ix_match_rejections_reason_type", "match_rejections", ["reason_type"])
    _create_index("ix_match_rejections_is_active", "match_rejections", ["is_active"])

    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "offer_observations" not in existing_tables:
        op.create_table(
            "offer_observations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("country_code", sa.String(2)),
            sa.Column("country_raw", sa.String(160)),
            sa.Column(
                "country_resolution_status",
                sa.String(20),
                nullable=False,
                server_default="unknown",
            ),
            sa.Column("country_source", sa.String(40)),
            sa.Column(
                "availability_status",
                sa.String(20),
                nullable=False,
                server_default="unknown",
            ),
            sa.Column("quantity", sa.Float()),
            sa.Column("availability_source", sa.String(40)),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        )
    for name, columns in (
        ("ix_offer_observations_tenant_id", ["tenant_id"]),
        ("ix_offer_observations_run_id", ["run_id"]),
        ("ix_offer_observations_product_id", ["product_id"]),
        ("ix_offer_observations_country_code", ["country_code"]),
        ("ix_offer_observations_availability_status", ["availability_status"]),
        ("ix_offer_observations_observed_at", ["observed_at"]),
    ):
        _create_index(name, "offer_observations", columns)

    if "match_policy_audits" not in existing_tables:
        op.create_table(
            "match_policy_audits",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("match_id", sa.Integer()),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("rolled_back_at", sa.DateTime()),
        )
    for name, columns in (
        ("ix_match_policy_audits_tenant_id", ["tenant_id"]),
        ("ix_match_policy_audits_match_id", ["match_id"]),
        ("ix_match_policy_audits_action", ["action"]),
        ("ix_match_policy_audits_created_at", ["created_at"]),
    ):
        _create_index(name, "match_policy_audits", columns)

    if "aloe_country_mappings" not in existing_tables:
        op.create_table(
            "aloe_country_mappings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("country_id", sa.String(40), nullable=False),
            sa.Column("country_code", sa.String(2), nullable=False),
            sa.Column("country_raw", sa.String(160), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=False),
            sa.Column("sample_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("verified_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id", "country_id", name="uq_aloe_country_mapping"
            ),
        )
    for name, columns in (
        ("ix_aloe_country_mappings_tenant_id", ["tenant_id"]),
        ("ix_aloe_country_mappings_country_id", ["country_id"]),
        ("ix_aloe_country_mappings_country_code", ["country_code"]),
    ):
        _create_index(name, "aloe_country_mappings", columns)


def downgrade() -> None:
    op.drop_table("aloe_country_mappings")
    op.drop_table("match_policy_audits")
    op.drop_table("offer_observations")
    op.drop_index("ix_match_rejections_is_active", table_name="match_rejections")
    op.drop_index("ix_match_rejections_reason_type", table_name="match_rejections")
    for column in ("updated_at", "resolved_at", "is_active", "metadata_json", "reason_type"):
        op.drop_column("match_rejections", column)
    for name in (
        "ix_products_availability_run_id",
        "ix_products_offer_availability_status",
        "ix_products_country_resolution_status",
        "ix_products_manufacturer_country_code",
    ):
        op.drop_index(name, table_name="products")
    for column in (
        "availability_run_id",
        "availability_observed_at",
        "availability_source",
        "offer_quantity",
        "offer_availability_status",
        "country_candidate_observed_at",
        "country_candidate_run_id",
        "country_candidate_seen_count",
        "country_candidate_code",
        "country_observed_at",
        "country_source",
        "country_resolution_status",
        "manufacturer_country_raw",
        "manufacturer_country_code",
    ):
        op.drop_column("products", column)
    op.drop_index("ix_runs_catalog_verified", table_name="runs")
    op.drop_index("ix_runs_catalog_scope", table_name="runs")
    for column in (
        "catalog_verification_reason",
        "catalog_verified",
        "full_catalog_sites",
        "catalog_scope",
    ):
        op.drop_column("runs", column)
    op.drop_column("roi_actions_cache", "trust_epoch")
    op.drop_column("roi_actions_cache", "policy_fingerprint")
