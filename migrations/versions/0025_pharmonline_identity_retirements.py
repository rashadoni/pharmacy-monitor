"""Append-only recovery of previously admitted Pharmonline identities.

Revision ID: 0025_identity_retirements
Revises: 0024_aloe_product_numbers
"""

from alembic import op
import sqlalchemy as sa

revision = "0025_identity_retirements"
down_revision = "0024_aloe_product_numbers"
branch_labels = None
depends_on = None
TABLE = "pharmonline_public_api_identity_retirements"


def upgrade():
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "legacy_product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "replacement_product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "prior_admission_id",
            sa.Integer(),
            sa.ForeignKey("pharmonline_public_api_identity_admissions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("prior_admission_sha256", sa.String(64), nullable=False),
        sa.Column("retirement_kind", sa.String(40), nullable=False),
        sa.Column("legacy_external_id", sa.String(200), nullable=False),
        sa.Column("archived_external_id", sa.String(200), nullable=False),
        sa.Column("legacy_canonical_url", sa.String(500), nullable=False),
        sa.Column("public_api_external_id", sa.String(200), nullable=False),
        sa.Column("public_api_canonical_url", sa.String(500), nullable=False),
        sa.Column("proof_version", sa.String(40), nullable=False),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("catalog_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("source_transport", sa.String(40), nullable=False),
        sa.Column("preflight_run_ref", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "legacy_product_id <> replacement_product_id", name="ck_pharmonline_retirement_distinct"
        ),
        sa.UniqueConstraint("legacy_product_id", name="uq_pharmonline_retirement_legacy"),
        sa.UniqueConstraint("replacement_product_id", name="uq_pharmonline_retirement_replacement"),
        sa.UniqueConstraint("prior_admission_id", name="uq_pharmonline_retirement_admission"),
        sa.UniqueConstraint("public_api_external_id", name="uq_pharmonline_retirement_public_id"),
    )
    op.create_index(
        "ix_pharmonline_public_api_identity_retirements_tenant_id", TABLE, ["tenant_id"]
    )


def downgrade():
    # Dropping used evidence would silently orphan old history and make new
    # identities untrusted. Restore a verified backup for a data rollback.
    if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {TABLE}")):
        raise RuntimeError(
            "cannot remove used identity retirement evidence; restore the reviewed backup"
        )
    op.drop_table(TABLE)
