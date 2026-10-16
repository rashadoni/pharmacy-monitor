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


def _definition():
    return (
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


def _verify_existing(bind):
    # 0001 creates the current model schema on an empty database. Accept that
    # bootstrap, but never stamp an unrelated/incomplete table as migrated.
    inspector = sa.inspect(bind)
    expected = sa.Table(TABLE, sa.MetaData(), *_definition())
    columns = {
        c["name"]: (str(c["type"].compile(dialect=bind.dialect)), c["nullable"])
        for c in inspector.get_columns(TABLE)
    }
    expected_columns = {
        c.name: (str(c.type.compile(dialect=bind.dialect)), c.nullable) for c in expected.columns
    }
    uniques = {
        (c["name"], tuple(c["column_names"])) for c in inspector.get_unique_constraints(TABLE)
    }
    expected_uniques = {
        (c.name, tuple(column.name for column in c.columns))
        for c in expected.constraints
        if isinstance(c, sa.UniqueConstraint)
    }
    foreign_keys = {
        (
            tuple(c["constrained_columns"]),
            c["referred_table"],
            tuple(c["referred_columns"]),
            c["options"].get("ondelete"),
        )
        for c in inspector.get_foreign_keys(TABLE)
    }
    expected_foreign_keys = {
        (
            tuple(f.parent.name for f in c.elements),
            c.elements[0].target_fullname.split(".")[0],
            tuple(f.target_fullname.split(".")[1] for f in c.elements),
            c.ondelete,
        )
        for c in expected.foreign_key_constraints
    }
    indexes = {
        (i["name"], tuple(i["column_names"]), bool(i["unique"]))
        for i in inspector.get_indexes(TABLE)
        if not i.get("duplicates_constraint")
    }
    checks = {
        (c["name"], "".join(c["sqltext"].split()).replace("(", "").replace(")", ""))
        for c in inspector.get_check_constraints(TABLE)
    }
    if (
        columns != expected_columns
        or inspector.get_pk_constraint(TABLE)["constrained_columns"] != ["id"]
        or uniques != expected_uniques
        or foreign_keys != expected_foreign_keys
        or indexes
        != {("ix_pharmonline_public_api_identity_retirements_tenant_id", ("tenant_id",), False)}
        or checks
        != {("ck_pharmonline_retirement_distinct", "legacy_product_id<>replacement_product_id")}
    ):
        raise RuntimeError("existing identity retirement table differs from migration schema")


def upgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        _verify_existing(bind)
        return
    op.create_table(TABLE, *_definition())
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
