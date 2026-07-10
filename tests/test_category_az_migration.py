from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[1]


def _alembic(db_url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": db_url}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_category_az_backfill_and_user_value_preservation(tmp_path: Path) -> None:
    db_path = tmp_path / "category-az-migration.sqlite"
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "0011_tracked_categories")

    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO categories
                    (key, label_ru, label_az, aptekonline_slug, is_active, created_at)
                VALUES
                    ('bakterial_aptek', 'Антибактериальные aptek', NULL, '114', 1, CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO categories
                    (key, label_ru, label_az, aloe_slug, is_active, created_at)
                VALUES
                    ('bestsellers_aloe', 'Хиты aloe.az', NULL, 'product_field=bestseller', 1, CURRENT_TIMESTAMP)
                """
            )
        )

    _alembic(db_url, "upgrade", "0012_category_az_labels")
    with engine.connect() as connection:
        labels = {
            key: label
            for key, label in connection.execute(
                text("SELECT key, label_az FROM categories")
            ).tuples()
        }
    assert labels == {
        "bakterial_aptek": "Antibakterial vasitələr",
        "bestsellers_aloe": "Aloe.az bestsellerləri",
    }

    _alembic(db_url, "downgrade", "0011_tracked_categories")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE categories SET label_az='İstifadəçi tərcüməsi' WHERE key='bakterial_aptek'"
            )
        )

    _alembic(db_url, "upgrade", "0012_category_az_labels")
    with engine.connect() as connection:
        labels = {
            key: label
            for key, label in connection.execute(
                text("SELECT key, label_az FROM categories")
            ).tuples()
        }
    assert labels["bakterial_aptek"] == "İstifadəçi tərcüməsi"
    assert labels["bestsellers_aloe"] == "Aloe.az bestsellerləri"
