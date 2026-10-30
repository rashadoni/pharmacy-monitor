"""Reviewed decisions that close pending Pharmonline identity isolation cases.

Revision ID: 0027_identity_resolutions
Revises: 0026_identity_isolation
"""

from alembic import op
import sqlalchemy as sa

revision = "0027_identity_resolutions"
down_revision = "0026_identity_isolation"
branch_labels = None
depends_on = None

TABLE = "pharmonline_identity_conflict_resolutions"

PREVIOUS_RELEASE_COMPATIBLE = (
    "Upgrade only creates one new empty table. The previous release neither reads "
    "nor writes it; existing tables, columns and rows are unchanged, and the new "
    "table stays empty until a reviewed manual reconciliation writes a decision."
)


def _table():
    metadata = sa.MetaData()
    for name in ("products", "runs", "pharmonline_identity_conflicts"):
        sa.Table(name, metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table(
        "pharmonline_public_api_identity_retirements",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )

    def fk(target):
        return sa.ForeignKey(f"{target}.id", ondelete="RESTRICT")

    return sa.Table(
        TABLE,
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, index=True),
        sa.Column(
            "conflict_id", sa.Integer(), fk("pharmonline_identity_conflicts"), nullable=False
        ),
        sa.Column("product_id", sa.Integer(), fk("products"), nullable=False),
        sa.Column("case_last_run_id", sa.Integer(), fk("runs"), nullable=False),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("current_product_id", sa.Integer(), fk("products"), nullable=False),
        sa.Column(
            "retirement_id",
            sa.Integer(),
            fk("pharmonline_public_api_identity_retirements"),
            nullable=True,
        ),
        sa.Column("legacy_external_id", sa.String(200), nullable=False),
        sa.Column("archived_external_id", sa.String(200), nullable=True),
        sa.Column("public_api_external_id", sa.String(200), nullable=False),
        sa.Column("public_api_canonical_url", sa.String(500), nullable=False),
        sa.Column("variant_set_sha256", sa.String(64), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("proof_version", sa.String(40), nullable=False),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("catalog_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("source_transport", sa.String(40), nullable=False),
        sa.Column("preflight_run_ref", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "conflict_id", "case_last_run_id", name="uq_pharmonline_conflict_resolution_state"
        ),
    )


def _verify_existing(bind, table):
    # 0001 builds the current model schema on an empty database. Accept that
    # bootstrap, but never stamp an unrelated or incomplete table as migrated.
    inspector = sa.inspect(bind)
    columns = {
        c["name"]: (str(c["type"].compile(dialect=bind.dialect)), c["nullable"])
        for c in inspector.get_columns(table.name)
    }
    expected = {
        c.name: (str(c.type.compile(dialect=bind.dialect)), c.nullable) for c in table.columns
    }
    uniques = {
        (c["name"], tuple(c["column_names"])) for c in inspector.get_unique_constraints(table.name)
    }
    wanted_uniques = {
        (c.name, tuple(col.name for col in c.columns))
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint)
    }
    foreign_keys = {
        (
            tuple(c["constrained_columns"]),
            c["referred_table"],
            tuple(c["referred_columns"]),
            c["options"].get("ondelete"),
        )
        for c in inspector.get_foreign_keys(table.name)
    }
    wanted_keys = {
        (
            tuple(f.parent.name for f in c.elements),
            c.elements[0].target_fullname.split(".")[0],
            tuple(f.target_fullname.split(".")[1] for f in c.elements),
            c.ondelete,
        )
        for c in table.foreign_key_constraints
    }
    indexes = {
        (i["name"], tuple(i["column_names"]), bool(i["unique"]))
        for i in inspector.get_indexes(table.name)
        if not i.get("duplicates_constraint")
    }
    wanted_indexes = {
        (i.name, tuple(c.name for c in i.columns), bool(i.unique)) for i in table.indexes
    }
    if (
        columns != expected
        or uniques != wanted_uniques
        or foreign_keys != wanted_keys
        or indexes != wanted_indexes
        or inspector.get_pk_constraint(table.name)["constrained_columns"] != ["id"]
    ):
        raise RuntimeError("existing identity resolution table differs from migration schema")


def upgrade():
    bind = op.get_bind()
    table = _table()
    if sa.inspect(bind).has_table(TABLE):
        _verify_existing(bind, table)
    else:
        table.create(bind)


def downgrade():
    bind = op.get_bind()
    table = _table()
    if bind.scalar(sa.select(sa.func.count()).select_from(table)):
        raise RuntimeError(
            "cannot remove used identity resolution evidence; restore reviewed backup"
        )
    table.drop(bind)
