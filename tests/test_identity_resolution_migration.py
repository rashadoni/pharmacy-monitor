"""Migration 0027: an empty decision ledger, replay-safe, never dropped with data."""

import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from src import storage

migration = importlib.import_module("migrations.versions.0027_pharmonline_identity_resolutions")


@pytest.mark.parametrize("bootstrap", [False, True])
def test_upgrade_is_replayable_and_empty_downgrade_drops_only_the_ledger(bootstrap, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    if bootstrap:
        storage.Base.metadata.create_all(engine)
    else:
        with engine.begin() as conn:
            for name in (
                "products",
                "runs",
                "pharmonline_identity_conflicts",
                "pharmonline_public_api_identity_retirements",
            ):
                conn.execute(text(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)"))
    with engine.begin() as conn:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        migration.upgrade()
        assert inspect(conn).has_table(migration.TABLE)
        migration.downgrade()
        assert not inspect(conn).has_table(migration.TABLE)
        assert inspect(conn).has_table("pharmonline_identity_conflicts")
    engine.dispose()


def test_upgrade_refuses_an_incompatible_preexisting_table(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE {migration.TABLE} (id INTEGER PRIMARY KEY)"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        with pytest.raises(RuntimeError, match="differs from migration"):
            migration.upgrade()
    engine.dispose()


def test_downgrade_refuses_to_drop_recorded_decisions(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    storage.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        columns = [c.name for c in storage.PharmonlineIdentityConflictResolution.__table__.columns]
        values = {name: "1" for name in columns}
        values["retirement_id"] = None
        values["archived_external_id"] = None
        conn.execute(
            text(
                f"INSERT INTO {migration.TABLE} ({', '.join(columns)}) "
                f"VALUES ({', '.join(':' + name for name in columns)})"
            ),
            values,
        )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        with pytest.raises(RuntimeError, match="cannot remove used identity resolution"):
            migration.downgrade()
        assert inspect(conn).has_table(migration.TABLE)
    engine.dispose()
