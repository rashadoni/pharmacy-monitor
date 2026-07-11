from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]


def _alembic(db_url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": db_url},
        check=True,
        capture_output=True,
        text=True,
    )


def test_run_quality_column_upgrade_and_downgrade(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'run-quality.sqlite'}"
    _alembic(db_url, "upgrade", "0012_category_az_labels")
    engine = create_engine(db_url)
    # Historical migration 0001 imports current Base.metadata, so a brand-new
    # test DB receives future ORM columns before their Alembic revision runs.
    # Production's pre-0013 schema does not; normalize the fixture to that real
    # starting state before exercising the revision itself.
    if "run_quality" in {item["name"] for item in inspect(engine).get_columns("runs")}:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE runs DROP COLUMN run_quality"))
    assert "run_quality" not in {item["name"] for item in inspect(engine).get_columns("runs")}

    _alembic(db_url, "upgrade", "0013_run_quality")
    assert "run_quality" in {item["name"] for item in inspect(engine).get_columns("runs")}

    _alembic(db_url, "downgrade", "0012_category_az_labels")
    assert "run_quality" not in {item["name"] for item in inspect(engine).get_columns("runs")}
