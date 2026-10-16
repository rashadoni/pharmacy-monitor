"""Exercise migration and the actual workflow evidence validators offline."""

import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect
import yaml

from src import storage
from tests.test_pharmonline_identity_retirement import apply, seed

ROOT = Path(__file__).resolve().parents[1]


def migration(connection, monkeypatch):
    path = ROOT / "migrations/versions/0025_pharmonline_identity_retirements.py"
    spec = importlib.util.spec_from_file_location("retirement_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(connection)))
    return module


def test_migration_creates_evidence_only_and_empty_downgrade_is_reversible(db_session, monkeypatch):
    table = storage.PharmonlinePublicAPIIdentityRetirement.__table__
    connection = db_session.connection()
    inspector = inspect(connection)
    expected_columns = [
        (c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table.name)
    ]
    expected_uniques = inspector.get_unique_constraints(table.name)
    expected_foreign = inspector.get_foreign_keys(table.name)
    table.drop(connection)
    module = migration(connection, monkeypatch)
    module.upgrade()
    inspector = inspect(connection)
    assert [
        (c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table.name)
    ] == expected_columns
    assert inspector.get_unique_constraints(table.name) == expected_uniques
    assert inspector.get_foreign_keys(table.name) == expected_foreign
    assert db_session.query(storage.Product).count() == 0
    assert db_session.query(storage.PharmonlinePublicAPIIdentityRetirement).count() == 0
    module.downgrade()
    assert table.name not in inspect(connection).get_table_names()
    module.upgrade()
    assert table.name in inspect(connection).get_table_names()


def test_migration_cannot_drop_used_history_evidence(db_session, monkeypatch):
    _, results, proofs = seed(db_session)
    apply(db_session, results, proofs)
    module = migration(db_session.connection(), monkeypatch)
    with pytest.raises(RuntimeError, match="cannot remove used"):
        module.downgrade()
    assert db_session.query(storage.PharmonlinePublicAPIIdentityRetirement).count() == 3
    assert db_session.query(storage.PharmonlinePublicAPIIdentityAdmission).count() == 3


@pytest.mark.parametrize(
    "workflow,step_name",
    [
        (
            "plan-pharmonline-decodo-public-api-reconciliation.yml",
            "Stage exact source and calculate the read-only reconciliation plan",
        ),
        (
            "recover-pharmonline-decodo-public-api.yml",
            "Download and verify the exact plan manifest",
        ),
    ],
)
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("damage", [None, "mode", "version", "string_boolean", "unsafe"])
def test_workflow_executes_matching_v4_evidence_only(
    tmp_path, monkeypatch, workflow, step_name, enabled, damage
):
    evidence = tmp_path / "plan.json"
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_PLAN_EVIDENCE_PATH", str(evidence))
    script = runpy.run_path(str(ROOT / "scripts/reconcile_pharmonline_public_api_identities.py"))
    script["write_plan_evidence"](
        transport="decodo",
        product_count=9431,
        catalog_fingerprint_sha256="a" * 64,
        candidate_manifest_sha256="b" * 64,
        metrics={"reconciliation_safe": 1},
        allow_identity_retirement=enabled,
    )
    data = json.loads(evidence.read_text())
    if damage == "mode":
        data["allow_identity_retirement"] = not enabled
    elif damage == "version":
        data["version"] = 3
    elif damage == "string_boolean":
        data["allow_identity_retirement"] = str(enabled).lower()
    elif damage == "unsafe":
        data["metrics"]["reconciliation_safe"] = 0
    evidence.write_text(json.dumps(data))
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    steps = next(iter(document["jobs"].values()))["steps"]
    step = next(s for s in steps if s.get("name") == step_name)
    assert step["env"]["RETIRE_IDENTITIES"] == "${{ inputs.retire_conflicting_identities }}"
    python = step["run"].split("python - <<'PY'\n")[-1].split("\nPY")[0]
    result = subprocess.run(
        [sys.executable, "-c", python],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PLAN_EVIDENCE_FILE": str(evidence),
            "RETIRE_IDENTITIES": str(enabled).lower(),
            "GITHUB_ENV": str(tmp_path / "env"),
        },
    )
    assert (result.returncode == 0) == (damage is None), result.stderr
    if damage is not None:
        assert not (tmp_path / "env").exists()


def test_workflow_shell_blocks_parse_with_explicit_retirement_argument(tmp_path):
    for name in (
        "plan-pharmonline-decodo-public-api-reconciliation.yml",
        "recover-pharmonline-decodo-public-api.yml",
    ):
        document = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        for job in document["jobs"].values():
            for step in job["steps"]:
                if "run" not in step:
                    continue
                # Check only shell syntax. Never execute the SSH/deployment steps.
                result = subprocess.run(
                    ["bash", "-n"], input=step["run"], text=True, capture_output=True
                )
                assert result.returncode == 0, (step.get("name"), result.stderr)
        content = (ROOT / ".github/workflows" / name).read_text()
        assert '"${retirement_args[@]}"' in content
        assert "--retire-conflicting-identities" in content
