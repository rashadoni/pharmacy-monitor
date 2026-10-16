"""Frozen migration upgrades old rows and refuses incompatible evidence tables."""

import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from src import storage

migration = importlib.import_module("migrations.versions.0026_pharmonline_identity_isolation")


@pytest.mark.parametrize("bootstrap", [False, True])
def test_upgrade_and_replay_preserve_original_product_and_observation(bootstrap, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    if bootstrap:
        storage.Base.metadata.create_all(engine)
    else:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.execute(text("CREATE TABLE runs (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE offer_observations (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO products VALUES (1, 'original')"))
            conn.execute(text("INSERT INTO offer_observations VALUES (1)"))
    with engine.begin() as conn:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        migration.upgrade()
        assert not inspect(conn).get_columns("products")[-1]["nullable"] if not bootstrap else True
        if not bootstrap:
            assert conn.execute(text("SELECT name, identity_conflicted FROM products")).one() == (
                "original",
                0,
            )
            assert conn.scalar(text("SELECT identity_verified FROM offer_observations")) == 0
        migration.downgrade()
        assert not inspect(conn).has_table("pharmonline_identity_conflicts")
    engine.dispose()


def test_migration_refuses_incompatible_preexisting_evidence(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE pharmonline_identity_conflicts (id INTEGER PRIMARY KEY)"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        with pytest.raises(RuntimeError, match="differs from migration"):
            migration.upgrade()
    engine.dispose()
