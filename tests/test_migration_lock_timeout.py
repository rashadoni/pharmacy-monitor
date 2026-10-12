"""Миграция не ждёт блокировку таблицы без предела.

Зачем тест: `ALTER TABLE` берёт ACCESS EXCLUSIVE. Пока он стоит в очереди за
долгой читающей транзакцией, за ним встают все остальные запросы к этой
таблице — чтения API в том числе. На проде `lock_timeout` был 0, то есть
миграция, попавшая на идущий сбор или ночной `pg_dump`, останавливала панель до
конца чужой транзакции.

`migrations/env.py` открывает соединение с `lock_timeout`; `deploy.yml`
повторяет попытку ограниченное число раз и отказывает с понятным текстом.

Покрываем на настоящем PostgreSQL (в CI он обязателен — там пропуск считается
падением; локально нужен DATABASE_URL):
- миграция за занятой таблицей падает за секунды, а не висит
- упавшая попытка не меняет ни схему, ни ревизию, даже если до занятой таблицы
  успела пройти другая миграция, — поэтому её можно повторять
- текст ошибки содержит `LockNotAvailable`: по нему `deploy.yml` отличает
  «таблица занята» от настоящей ошибки миграции
- предел по умолчанию, свой предел и опции из самой строки подключения
- текст ошибки упавшей миграции данных не несёт данных запроса: `alembic
  upgrade` идёт из `deploy.yml`, журнал шага публичный. Движок в `env.py` —
  `storage.create_engine_hiding_statement_data` с `connect_args` предела; три
  настоящих сбоя (ожидание замка строки, нарушение уникальности, NULL в
  обязательной колонке) и контроль на `env.py` живого каталога сервера —
  выкладка его не заменяет, и с ним те же данные в тексте есть

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

revision: str = "{revision}"
down_revision: str | None = {down_revision}
branch_labels = None
depends_on = None


def upgrade() -> None:
{body}


def downgrade() -> None:
    pass
'''

ADD_COLUMN = '    op.add_column("probe", sa.Column("added", sa.Integer(), nullable=True))'
CREATE_FIRST_STEP = '    op.create_table("first_step", sa.Column("id", sa.Integer()))'
RECORD_SETTINGS = (
    "    op.execute(\n"
    '        "CREATE TABLE seen AS SELECT "\n'
    "        \"current_setting('lock_timeout') AS lock_timeout, \"\n"
    "        \"current_setting('statement_timeout') AS statement_timeout\"\n"
    "    )"
)


def _postgres_url() -> str:
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url.startswith("postgresql"):
        return database_url
    reason = "PostgreSQL DATABASE_URL is required"
    if os.environ.get("CI"):
        # В CI пропущенный тест зелёный: предел ожидания остался бы без проверки.
        pytest.fail(reason)
    pytest.skip(reason)


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


def _project(tmp_path: Path, *bodies: str, env_py: Path = ROOT / "migrations" / "env.py") -> Path:
    """Alembic-проект с настоящими alembic.ini и env.py и цепочкой пробных миграций."""
    project = tmp_path / "project"
    versions = project / "migrations" / "versions"
    versions.mkdir(parents=True)
    shutil.copy(ROOT / "alembic.ini", project / "alembic.ini")
    shutil.copy(env_py, project / "migrations" / "env.py")
    shutil.copy(ROOT / "migrations" / "script.py.mako", project / "migrations" / "script.py.mako")
    previous = None
    for number, body in enumerate(bodies, start=1):
        revision = f"{number:04d}_probe"
        (versions / f"{revision}.py").write_text(
            MIGRATION_TEMPLATE.format(
                revision=revision,
                down_revision=f'"{previous}"' if previous else "None",
                body=body,
            ),
            encoding="utf-8",
        )
        previous = revision
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
    # Первая миграция занятую таблицу не трогает и проходит, вторая упирается.
    project = _project(tmp_path, CREATE_FIRST_STEP, ADD_COLUMN)
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
        # Вся попытка — одна транзакция: откатилась и уже прошедшая первая
        # миграция, и сама таблица ревизий. На этом держится повтор в deploy.yml.
        assert observer.get_table_names() == ["probe"]
    finally:
        reader.rollback()
        reader.close()

    retried = _alembic(project, scratch_db_url, "upgrade", "head", lock_timeout_ms="400")
    assert retried.returncode == 0, retried.stderr

    observer = inspect(create_engine(scratch_db_url))
    assert [column["name"] for column in observer.get_columns("probe")] == ["id", "added"]
    assert "first_step" in observer.get_table_names()
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalars().all() == ["0002_probe"]
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


# ─── Упавшая миграция данных: в тексте ошибки нет данных запроса ─────────────

# `env.py` живого каталога сервера (прочитан 2026-10-11; deploy.yml его не
# заменяет): движок через `engine_from_config`, без предела ожидания. На нём
# идёт `alembic`, запущенный на сервере руками, и прежний deploy.yml.
ENV_PY_ON_SERVER = ROOT / "tests" / "fixtures" / "migrations_env_on_server_2026-08-23.py.txt"

ROW_EMAIL = "row-marker@example.test"
ROW_HASH = "ROW-HASH-MARKER-51c7"
PARAMETER_HASH = "PARAMETER-HASH-MARKER-9f3c"
MARKERS = (ROW_EMAIL, ROW_HASH, PARAMETER_HASH)

# Ждёт замок строки, которую держит другая транзакция.
UPDATE_A_LOCKED_ROW = f"""    op.execute(
        sa.text("UPDATE people SET password_hash = :hash WHERE email = :email").bindparams(
            hash="{PARAMETER_HASH}", email="{ROW_EMAIL}"
        )
    )"""
# Нарушает уникальность: сервер приводит значение ключа в DETAIL.
INSERT_A_DUPLICATE = f"""    op.execute(
        sa.text(
            "INSERT INTO people (id, email, password_hash) VALUES (:id, :email, :hash)"
        ).bindparams(id=2, email="{ROW_EMAIL}", hash="{PARAMETER_HASH}")
    )"""
# NULL в обязательной колонке: сервер приводит в DETAIL всю строку таблицы —
# то, чего в параметрах запроса нет.
ERASE_A_REQUIRED_VALUE = """    op.execute(
        sa.text("UPDATE people SET email = NULL WHERE id = :id").bindparams(id=1)
    )"""

# Сбой → миграция, класс ошибки и что о данных печатал прежний env.py:
# метка сервера и значения, которые он за ней приводил.
FAILURES = {
    "lock wait": (
        UPDATE_A_LOCKED_ROW,
        "LockNotAvailable",
        "CONTEXT",
        (PARAMETER_HASH, ROW_EMAIL),
    ),
    "unique": (INSERT_A_DUPLICATE, "UniqueViolation", "DETAIL", (PARAMETER_HASH, ROW_EMAIL)),
    "not null": (ERASE_A_REQUIRED_VALUE, "NotNullViolation", "DETAIL", (ROW_HASH,)),
}


def _fail_a_data_migration(
    tmp_path: Path, scratch_db_url: str, failure: str, *, env_py: Path, db_url: str | None = None
) -> str:
    """Уронить пробную миграцию данных настоящим сбоем и вернуть всё, что напечатал Alembic."""
    project = _project(tmp_path, FAILURES[failure][0], env_py=env_py)
    engine = create_engine(scratch_db_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE people "
                "(id integer PRIMARY KEY, email text NOT NULL UNIQUE, password_hash text)"
            )
        )
        connection.execute(
            text("INSERT INTO people VALUES (1, :email, :hash)"),
            {"email": ROW_EMAIL, "hash": ROW_HASH},
        )
    holder = engine.connect()
    try:
        if failure == "lock wait":
            holder.execute(text("SELECT id FROM people WHERE id = 1 FOR UPDATE")).one()
        failed = _alembic(
            project, db_url or scratch_db_url, "upgrade", "head", lock_timeout_ms="400"
        )
    finally:
        holder.rollback()
        holder.close()
        engine.dispose()
    assert failed.returncode != 0, "пробная миграция должна была упасть"
    return failed.stdout + failed.stderr


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_a_failed_data_migration_prints_no_statement_data(
    tmp_path: Path, scratch_db_url: str, failure: str
) -> None:
    error_class = FAILURES[failure][1]

    printed = _fail_a_data_migration(
        tmp_path, scratch_db_url, failure, env_py=ROOT / "migrations" / "env.py"
    )

    # Класс ошибки остаётся: по нему deploy.yml отличает «таблица занята» от поломки.
    assert f"psycopg.errors.{error_class}" in printed
    # Остаётся и сам запрос — с именами параметров, без значений.
    assert "[SQL: " in printed and "%(" in printed
    leaked = [marker for marker in MARKERS if marker in printed]
    assert not leaked, f"в тексте ошибки миграции данные запроса: {leaked}\n{printed}"
    assert "[parameters:" not in printed
    # Обе половины защиты на месте: параметры прячет движок, хвост ошибки
    # сервера (DETAIL, CONTEXT) отбрасывает обработчик фабрики.
    assert "hide_parameters=True" in printed
    assert "details hidden" in printed
    assert "DETAIL" not in printed
    assert "CONTEXT" not in printed


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_control_the_env_py_on_the_server_prints_that_data(
    tmp_path: Path, scratch_db_url: str, failure: str
) -> None:
    """Контроль пробы: на `env.py` сервера те же сбои печатают данные — проба их видит."""
    _, error_class, server_label, values = FAILURES[failure]
    # У этого env.py предела нет: без него ожидание замка не кончилось бы.
    with_limit = f"{scratch_db_url}?options=-c lock_timeout=400"

    printed = _fail_a_data_migration(
        tmp_path, scratch_db_url, failure, env_py=ENV_PY_ON_SERVER, db_url=with_limit
    )

    assert f"psycopg.errors.{error_class}" in printed
    assert "[parameters:" in printed
    assert server_label in printed, f"контроль не увидел {server_label}:\n{printed}"
    for marker in values:
        assert marker in printed, f"контроль не увидел {marker}:\n{printed}"
