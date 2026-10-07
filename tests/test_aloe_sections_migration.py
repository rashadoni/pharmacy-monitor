from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "0021_public_api_quarantines"
REVISION = "0022_aloe_cosmetics_hygiene"


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


def _aloe_rows(engine) -> dict[str, tuple]:
    with engine.connect() as connection:
        return {
            key: (label_ru, label_az, aloe_slug, pharmonline_slug, aptekonline_slug, bool(active))
            for key, label_ru, label_az, aloe_slug, pharmonline_slug, aptekonline_slug, active in (
                connection.execute(
                    text(
                        "SELECT key, label_ru, label_az, aloe_slug, pharmonline_slug, "
                        "aptekonline_slug, is_active FROM categories "
                        "WHERE aloe_slug IS NOT NULL ORDER BY id"
                    )
                ).tuples()
            )
        }


def test_adds_both_sections_next_to_existing_routes_and_reverses(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'aloe-sections.sqlite'}"
    _alembic(db_url, "upgrade", PREVIOUS)
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO categories (key, label_ru, label_az, aloe_slug, is_active, created_at) "
                "VALUES ('aloe_dermanlar', 'Лекарства', 'Dərmanlar', 'dermanlar', 1, "
                "CURRENT_TIMESTAMP)"
            )
        )

    _alembic(db_url, "upgrade", REVISION)

    assert _aloe_rows(engine) == {
        "aloe_dermanlar": ("Лекарства", "Dərmanlar", "dermanlar", None, None, True),
        "aloe_kosmetika": ("Косметика", "Kosmetika", "kosmetika", None, None, True),
        "aloe_gigiyena": ("Гигиена", "Gigiyena", "gigiyena", None, None, True),
    }
    with engine.connect() as connection:
        created = connection.execute(
            text("SELECT count(*) FROM categories WHERE created_at IS NULL")
        ).scalar_one()
    assert created == 0

    _alembic(db_url, "downgrade", PREVIOUS)
    assert set(_aloe_rows(engine)) == {"aloe_dermanlar"}


def test_keeps_a_section_the_operator_already_entered(tmp_path: Path) -> None:
    """Ни ручную правку, ни выключенный раздел миграция не перезаписывает."""
    db_url = f"sqlite:///{tmp_path / 'aloe-sections-manual.sqlite'}"
    _alembic(db_url, "upgrade", PREVIOUS)
    engine = create_engine(db_url)
    with engine.begin() as connection:
        # Тот же ключ, но раздел выключен и подписан по-своему.
        connection.execute(
            text(
                "INSERT INTO categories (key, label_ru, label_az, aloe_slug, is_active, created_at) "
                "VALUES ('aloe_kosmetika', 'Косметика (пауза)', 'Kosmetika', 'kosmetika', 0, "
                "CURRENT_TIMESTAMP)"
            )
        )
        # Тот же раздел сайта под другим ключом: вторая строка с этим slug
        # заставила бы сбор пройти раздел дважды.
        connection.execute(
            text(
                "INSERT INTO categories (key, label_ru, label_az, aloe_slug, is_active, created_at) "
                "VALUES ('hygiene', 'Гигиена и уход', NULL, 'gigiyena', 1, CURRENT_TIMESTAMP)"
            )
        )

    _alembic(db_url, "upgrade", REVISION)

    expected = {
        "aloe_kosmetika": ("Косметика (пауза)", "Kosmetika", "kosmetika", None, None, False),
        "hygiene": ("Гигиена и уход", None, "gigiyena", None, None, True),
    }
    assert _aloe_rows(engine) == expected

    # Откат убирает только строки в том виде, в каком их создала миграция.
    _alembic(db_url, "downgrade", PREVIOUS)
    assert _aloe_rows(engine) == expected


def test_does_not_become_the_whole_route_list_on_a_database_without_aloe(tmp_path: Path) -> None:
    """На базе без маршрутов aloe миграция ничего не заводит.

    Иначе новый сервер после `alembic upgrade head` собирал бы одну косметику с
    гигиеной и считал это полным проверенным каталогом — а пустая таблица
    раньше честно отказывала (`no_categories_configured`).
    """
    db_url = f"sqlite:///{tmp_path / 'aloe-sections-empty.sqlite'}"
    _alembic(db_url, "upgrade", PREVIOUS)
    engine = create_engine(db_url)
    with engine.begin() as connection:
        # Категория другого сайта не делает базу «собирающей aloe».
        connection.execute(
            text(
                "INSERT INTO categories (key, label_ru, pharmonline_slug, is_active, created_at) "
                "VALUES ('pharma_x', 'X', 'x', 1, CURRENT_TIMESTAMP)"
            )
        )

    _alembic(db_url, "upgrade", REVISION)

    assert _aloe_rows(engine) == {}
    with engine.connect() as connection:
        total = connection.execute(text("SELECT count(*) FROM categories")).scalar_one()
    assert total == 1
