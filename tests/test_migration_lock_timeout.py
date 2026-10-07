"""Миграция не ждёт блокировку таблицы без предела.

Зачем тест: `ALTER TABLE` берёт ACCESS EXCLUSIVE. Пока он стоит в очереди за
долгой читающей транзакцией, за ним встают все остальные запросы к этой
таблице — чтения API в том числе. На проде `lock_timeout` был 0, то есть
миграция, попавшая на идущий сбор или ночной `pg_dump`, останавливала панель до
конца чужой транзакции.

`migrations/env.py` открывает соединение с `lock_timeout`; `deploy.yml`
повторяет попытку ограниченное число раз и отказывает с понятным текстом.

Покрываем на настоящем PostgreSQL (в CI он есть, локально нужен DATABASE_URL):
- миграция за занятой таблицей падает за секунды, а не висит
- упавшая попытка не меняет ни схему, ни ревизию — повтор проходит
- текст ошибки содержит `LockNotAvailable`: по нему `deploy.yml` отличает
  «таблица занята» от настоящей ошибки миграции
- предел по умолчанию, свой предел и опции из самой строки подключения

Тест исполняет настоящий `migrations/env.py` репозитория на одноразовой базе с
одной пробной миграцией, чтобы не зависеть от содержимого боевых.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent

MIGRATION_TEMPLATE = '''"""probe"""

import sqlalchemy as sa
from alembic import op

revision: str = "0001_probe"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
{body}


def downgrade() -> None:
    pass
'''

ADD_COLUMN = '    op.add_column("probe", sa.Column("added", sa.Integer(), nullable=True))'
RECORD_SETTINGS = (
    "    op.execute(\n"
    '        "CREATE TABLE seen AS SELECT "\n'
    "        \"current_setting('lock_timeout') AS lock_timeout, \"\n"
    "        \"current_setting('statement_timeout') AS statement_timeout\"\n"
    "    )"
)


def _postgres_url() -> str:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL is required")
    return database_url


@pytest.fixture
def scratch_db_url():
    """Пустая база на том же сервере: тест берёт блокировки и ломает миграции."""
    admin_url = make_url(_postgres_url())
    name = f"migration_lock_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield admin_url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _project(tmp_path: Path, body: str) -> Path:
    """Alembic-проект с настоящими alembic.ini и env.py и одной пробной миграцией."""
    project = tmp_path / "project"
    versions = project / "migrations" / "versions"
    versions.mkdir(parents=True)
    shutil.copy(ROOT / "alembic.ini", project / "alembic.ini")
    shutil.copy(ROOT / "migrations" / "env.py", project / "migrations" / "env.py")
    shutil.copy(ROOT / "migrations" / "script.py.mako", project / "migrations" / "script.py.mako")
    (versions / "0001_probe.py").write_text(MIGRATION_TEMPLATE.format(body=body), encoding="utf-8")
    return project


def _alembic(
    project: Path, db_url: str, *args: str, lock_timeout_ms: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "MIGRATION_LOCK_TIMEOUT_MS"}
    env["DATABASE_URL"] = db_url
    # env.py импортирует src.storage; cwd — пробный проект, src берём из репозитория.
    env["PYTHONPATH"] = str(ROOT)
    if lock_timeout_ms is not None:
        env["MIGRATION_LOCK_TIMEOUT_MS"] = lock_timeout_ms
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_migration_behind_a_long_reader_fails_fast_and_changes_nothing(
    tmp_path: Path, scratch_db_url: str
) -> None:
    project = _project(tmp_path, ADD_COLUMN)
    engine = create_engine(scratch_db_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE probe (id integer)"))

    # Долгая читающая транзакция: SELECT выполнен, транзакция не закрыта —
    # ACCESS SHARE на таблице держится до её конца.
    reader = engine.connect()
    try:
        reader.execute(text("SELECT count(*) FROM probe")).scalar_one()

        started = time.monotonic()
        blocked = _alembic(project, scratch_db_url, "upgrade", "head", lock_timeout_ms="400")
        elapsed = time.monotonic() - started

        assert blocked.returncode != 0
        assert "LockNotAvailable" in blocked.stdout + blocked.stderr
        assert elapsed < 30, f"миграция ждала блокировку {elapsed:.0f} с вместо отказа"

        observer = inspect(create_engine(scratch_db_url))
        assert [column["name"] for column in observer.get_columns("probe")] == ["id"]
        # Вся попытка — одна транзакция: даже таблица ревизий не появилась.
        assert "alembic_version" not in observer.get_table_names()
    finally:
        reader.rollback()
        reader.close()

    retried = _alembic(project, scratch_db_url, "upgrade", "head", lock_timeout_ms="400")
    assert retried.returncode == 0, retried.stderr

    observer = inspect(create_engine(scratch_db_url))
    assert [column["name"] for column in observer.get_columns("probe")] == ["id", "added"]
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalars().all() == ["0001_probe"]
    engine.dispose()


@pytest.mark.parametrize(
    ("lock_timeout_ms", "url_options", "expected"),
    [
        # Без настройки — предел по умолчанию, а не ожидание без конца.
        (None, None, ("5s", "0")),
        ("1500", None, ("1500ms", "0")),
        # 0 снимает предел: осознанный выбор оператора, а не умолчание.
        ("0", None, ("0", "0")),
        # Опции из строки подключения не теряются.
        (None, "-c statement_timeout=7777", ("5s", "7777ms")),
    ],
)
def test_lock_timeout_reaches_the_connection_that_runs_the_migration(
    tmp_path: Path,
    scratch_db_url: str,
    lock_timeout_ms: str | None,
    url_options: str | None,
    expected: tuple[str, str],
) -> None:
    project = _project(tmp_path, RECORD_SETTINGS)
    db_url = scratch_db_url
    if url_options is not None:
        # Без percent-кодирования: env.py кладёт адрес в ConfigParser, а тот
        # не принимает «%» (так было и до этой правки).
        db_url = f"{scratch_db_url}?options={url_options}"

    result = _alembic(project, db_url, "upgrade", "head", lock_timeout_ms=lock_timeout_ms)
    assert result.returncode == 0, result.stderr

    engine = create_engine(scratch_db_url)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT lock_timeout, statement_timeout FROM seen")).one()
    engine.dispose()
    assert tuple(row) == expected


def test_a_malformed_timeout_stops_the_migration_before_it_connects(tmp_path: Path) -> None:
    """«5s» вместо числа не должно тихо превратиться в «без предела»."""
    project = _project(tmp_path, ADD_COLUMN)
    # Адрес, на котором никто не слушает: до соединения дойти не должно.
    unreachable = "postgresql+psycopg://nobody@127.0.0.1:9/nowhere"

    result = _alembic(project, unreachable, "upgrade", "head", lock_timeout_ms="5s")

    assert result.returncode != 0
    assert "MIGRATION_LOCK_TIMEOUT_MS must be a whole number" in result.stderr
