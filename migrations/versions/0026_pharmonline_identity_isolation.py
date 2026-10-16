"""Isolate pending identities without transferring historical prices.

Revision ID: 0026_identity_isolation
Revises: 0025_identity_retirements
"""

from alembic import op
import sqlalchemy as sa

revision = "0026_identity_isolation"
down_revision = "0025_identity_retirements"
branch_labels = None
depends_on = None


def _tables():
    metadata = sa.MetaData()
    for name in ("products", "runs"):
        sa.Table(name, metadata, sa.Column("id", sa.Integer(), primary_key=True))
    cases = sa.Table(
        "pharmonline_identity_conflicts",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, index=True),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "first_run_id",
            sa.Integer(),
            sa.ForeignKey("runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "last_run_id",
            sa.Integer(),
            sa.ForeignKey("runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("product_id", name="uq_pharmonline_conflict_product"),
    )
    variants = sa.Table(
        "pharmonline_identity_conflict_variants",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conflict_id",
            sa.Integer(),
            sa.ForeignKey("pharmonline_identity_conflicts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(200), nullable=False, index=True),
        sa.Column("canonical_url", sa.String(500), nullable=False, index=True),
        sa.Column(
            "first_run_id",
            sa.Integer(),
            sa.ForeignKey("runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "last_run_id",
            sa.Integer(),
            sa.ForeignKey("runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("catalog_fingerprint_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "conflict_id", "external_id", "canonical_url", name="uq_pharmonline_conflict_variant"
        ),
    )
    return cases, variants


def _verify_existing(bind, table):
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
        raise RuntimeError("existing identity isolation table differs from migration schema")


def upgrade():
    bind = op.get_bind()
    for table in _tables():
        if sa.inspect(bind).has_table(table.name):
            _verify_existing(bind, table)
        else:
            table.create(bind)
    for table, column in (
        ("products", "identity_conflicted"),
        ("offer_observations", "identity_verified"),
    ):
        existing = {c["name"]: c for c in sa.inspect(bind).get_columns(table)}.get(column)
        if existing is None:
            op.add_column(
                table, sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false())
            )
        elif (
            existing["nullable"]
            or not isinstance(existing["type"], sa.Boolean)
            or str(existing["default"]).lower().replace("::boolean", "").strip("()'")
            not in {"false", "0"}
        ):
            raise RuntimeError("existing identity trust column differs from migration schema")


def downgrade():
    bind = op.get_bind()
    if any(bind.scalar(sa.select(sa.func.count()).select_from(t)) for t in _tables()):
        raise RuntimeError(
            "cannot remove used identity isolation evidence; restore reviewed backup"
        )
    op.drop_column("offer_observations", "identity_verified")
    op.drop_column("products", "identity_conflicted")
    for table in reversed(_tables()):
        table.drop(bind)
