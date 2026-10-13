"""Сборка строк сравнения живёт в src/comparison_rows.py, а не в src/api.py.

Вынесена она ради CLI: утреннее письмо о разнице цен собирает те же строки, что
страница сравнения, а `src/api.py` при импорте поднимает Sentry, ставит фильтр
журнала доступа и собирает приложение FastAPI. Сами строки проверяют тесты
страницы (tests/test_api.py); здесь — только то, что появилось с переносом.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from src import api as api_module
from src import comparison_rows

REPO = Path(__file__).resolve().parents[1]

# Имена, под которыми сборку строк знает src/api.py и его тесты.
MOVED = (
    "_COMPARISON_PRODUCT_COLUMNS",
    "_COMPARISON_STALE_DAYS",
    "_SITE_ORDER",
    "_comparison_rows",
    "_comparison_spread",
    "_has_ok_run",
    "_price_age_days",
    "_trusted_snapshot_lineage_available",
)


def _modules_loaded_by(statement: str) -> set[str]:
    """Какие из модулей веб-приложения оказались загружены после `statement`."""
    probe = (
        f"{statement}\n"
        "import sys\n"
        "print(' '.join(sorted({'src.api', 'fastapi', 'src.observability'} & set(sys.modules))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO)},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return set(result.stdout.split())


def test_row_builder_does_not_import_the_web_app():
    assert _modules_loaded_by("import src.comparison_rows") == set()


def test_digest_command_does_not_import_the_web_app():
    """Письмо шлёт `notify digest`: его модули приложение за собой не тянут."""
    assert _modules_loaded_by("import src.main, src.notifications") == set()


def test_the_probe_sees_the_web_app_when_it_is_imported():
    assert "src.api" in _modules_loaded_by("import src.api")


def test_page_and_letter_share_one_row_builder():
    """Второе определение в src/api.py развело бы страницу и письмо молча."""
    for name in MOVED:
        assert getattr(api_module, name) is getattr(comparison_rows, name), name
    assert comparison_rows.comparison_rows is comparison_rows._comparison_rows
    assert comparison_rows.has_ok_run is comparison_rows._has_ok_run
