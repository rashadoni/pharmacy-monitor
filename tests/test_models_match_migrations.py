"""Модели и миграции описывают одну схему — проверка на настоящем PostgreSQL.

Зачем. `storage.init_db()` зовёт `Base.metadata.create_all` и на PostgreSQL, а
его зовёт почти каждая команда CLI. Модель, для которой забыли миграцию, на
боевой базе создаст таблицу мимо Alembic, а забытая колонка уронит первый же
запрос к своей таблице. Сверка ревизий в workflow этого не видит: она
сравнивает номера ревизий, а не таблицы.

Почему не `alembic check` после `alembic upgrade head` на пустой базе. Первая
миграция (`0001_initial`) строит схему из моделей самого чекаута —
`Base.metadata.create_all`, — а остальные пропускают то, что уже есть. На пустой
базе модель без миграции создаётся вместе со всеми, и сравнение выходит пустым:
проверено опытом 2026-10-08, таблица и колонка без миграции прошли зелёными.

Как устроено. `tests/fixtures/schema_baseline_0023.sql` — замороженный снимок
схемы на ревизии 0023. На него накатываются миграции чекаута — те, что после
0023, исполняются по-настоящему, как на проде, — и результат сравнивает
`alembic check` с настройками из `migrations/env.py`. Тесты под чертой ломают
копию чекаута и доказывают, что проверка это ловит, а верная миграция её гасит.

Одно расхождение пропускается намеренно: серверное значение по умолчанию,
которое есть в базе, но не названо в модели. Так в проекте принято — миграция
ставит `server_default`, чтобы заполнить уже лежащие строки, а модель держит
питоновский `default`; в боевой базе таких колонок десятки, записи через ORM
это не мешает. Обратное — модель называет `server_default`, которого в базе
нет, — расхождение. Цена допуска: модель, у которой `server_default` убрали
без миграции, тоже пройдёт.

Чего проверка не видит:

- чем боевая база отличается от снимка. Снимок — модели коммита, на котором он
  снят: `0001_initial` строит схему из них, а не из истории миграций. Боевая
  строилась дольше и не только миграциями; список её отличий —
  docs/RUNBOOK.md «Модели и миграции». Миграция, которая выравнивает боевую
  базу, на снимке пройдёт вхолостую;
- данные: таблицы снимка пусты. Колонка NOT NULL без значения по умолчанию или
  заполнение строк пройдут здесь и упадут на выкладке;
- то, чего не сравнивает сам Alembic: CHECK-ограничения и смену первичного
  ключа;
- миграции до 0023 включительно: в снимке они уже «исполнены»;
- SQLite: у него свой путь, `_apply_lightweight_migrations`.

Модель вне `src/storage.py` тоже осталась бы невидимой — `migrations/env.py`
берёт метаданные только оттуда, — поэтому отдельный тест не даёт завести её в
другом модуле.

Как красный тест НЕ чинят: не пересобирают снимок и не меняют его контрольную
сумму, не расширяют `_tolerated`, не правят миграцию, которую уже выкладывали.

Без PostgreSQL в `DATABASE_URL` тесты с базой пропускаются; в CI пропуск
считается ошибкой — иначе проверка исчезла бы молча вместе с сервисом базы.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "fixtures" / "schema_baseline_0023.sql"
BASELINE_REVISION = "0023_snapshot_confirmed_run"
BASELINE_SHA256 = "bdaddcd243442bbfa902f9dacf480a8ebe88f47cdb85d21f8fc7cee7d0c5a237"

WHAT_TO_DO = (
    "Что делать: модель и миграции должны сойтись. Изменил модель — нужна миграция: "
    "новый файл в migrations/versions/, down_revision = текущая голова; на прод её "
    "ставит deploy.yml с apply_migrations=true. Расходится миграция, которую ещё не "
    "выкладывали, — править её или модель. Снимок "
    "tests/fixtures/schema_baseline_0023.sql не правят."
)


def _script_directory(root: Path = ROOT) -> ScriptDirectory:
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    return ScriptDirectory.from_config(config)


def test_baseline_is_the_frozen_file() -> None:
    digest = hashlib.sha256(BASELINE.read_bytes()).hexdigest()
    assert digest == BASELINE_SHA256, (
        "tests/fixtures/schema_baseline_0023.sql изменён. Этот файл — замороженная "
        "схема на ревизии 0023: её не правят и не пересобирают. " + WHAT_TO_DO
    )


def test_baseline_revision_is_an_ancestor_of_every_head() -> None:
    script = _script_directory()
    stamped = f"VALUES ('{BASELINE_REVISION}');"
    assert stamped in BASELINE.read_text(encoding="utf-8")
    for head in script.get_heads():
        ancestors = {revision.revision for revision in script.iterate_revisions(head, "base")}
        assert BASELINE_REVISION in ancestors, (
            f"голова {head} не стоит на {BASELINE_REVISION}: миграции со снимка до неё не доехать"
        )


def test_models_are_declared_only_in_storage() -> None:
    """`migrations/env.py` сверяет `src.storage.Base.metadata` — и только его."""
    declares_a_model = re.compile(
        r"__tablename__|\bTable\(\s*[\"'][^\"']+[\"']\s*,\s*[\w.]*metadata\b"
    )
    elsewhere = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "src").rglob("*.py"))
        if path != ROOT / "src" / "storage.py"
        and declares_a_model.search(path.read_text(encoding="utf-8"))
    ]
    assert not elsewhere, (
        f"таблица объявлена вне src/storage.py: {elsewhere}. Сверка моделей с "
        "миграциями её не увидит, а `create_all` создаст в боевой базе. Перенести "
        "модель в src/storage.py."
    )


def _server_url() -> URL:
    raw = os.environ.get("DATABASE_URL", "")
    if raw.startswith("postgresql"):
        return make_url(raw)
    if os.environ.get("CI"):
        pytest.fail(
            "в CI сверка моделей с миграциями обязана исполняться, а DATABASE_URL — "
            "не PostgreSQL. Без неё модель без миграции дойдёт до боевой базы."
        )
    pytest.skip("нужен PostgreSQL в DATABASE_URL")


def _admin(server: URL, statement: str) -> None:
    engine = create_engine(server, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(statement)
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def baseline_template() -> Iterator[tuple[URL, str]]:
    """База со снимком 0023; тесты клонируют её, а не грузят снимок заново."""
    server = _server_url()
    name = f"pm_schema_baseline_{uuid.uuid4().hex[:12]}"
    _admin(server, f'CREATE DATABASE "{name}"')
    try:
        engine = create_engine(server.set(database=name), isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql(BASELINE.read_text(encoding="utf-8"))
        finally:
            engine.dispose()
        yield server, name
    finally:
        _admin(server, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def baseline_db(baseline_template: tuple[URL, str]) -> Iterator[str]:
    server, template = baseline_template
    name = f"pm_schema_check_{uuid.uuid4().hex[:12]}"
    _admin(server, f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
    try:
        yield server.set(database=name).render_as_string(hide_password=False)
    finally:
        _admin(server, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _alembic(tree: Path, db_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=tree,
        env={**os.environ, "DATABASE_URL": db_url},
        capture_output=True,
        text=True,
        check=False,
    )


# `alembic check` изнутри: тот же `migrations/env.py`, что у команды, но
# расхождения — списком, а не строкой. Отдельный процесс: модели берутся из
# каталога, в котором он запущен, а env.py перенастраивает журналирование.
_COMPARE = r"""
import json

from alembic import command
from alembic.config import Config
from alembic.util import AutogenerateDiffsDetected


def flat(items):
    for item in items:
        if isinstance(item, list):
            yield from flat(item)
        else:
            yield item


def default_text(clause):
    arg = getattr(clause, "arg", None)
    return None if clause is None else str(getattr(arg, "text", arg))


def describe(diff):
    kind = diff[0]
    if kind in ("add_table", "remove_table"):
        return kind, diff[1].name, None, None
    if kind in ("add_column", "remove_column"):
        return kind, f"{diff[2]}.{diff[3].name}", None, None
    if kind in ("add_index", "remove_index"):
        return kind, f"{diff[1].table.name}.{diff[1].name}", None, None
    if kind == "modify_default":
        return kind, f"{diff[2]}.{diff[3]}", default_text(diff[5]), default_text(diff[6])
    if kind.startswith("modify_"):
        return kind, f"{diff[2]}.{diff[3]}", str(diff[5]), str(diff[6])
    subject = diff[1]
    table = getattr(getattr(subject, "table", None), "name", "?")
    name = getattr(subject, "name", None)
    if not name:
        name = "(" + ", ".join(column.name for column in getattr(subject, "columns", [])) + ")"
    return kind, f"{table}.{name}", None, None


try:
    command.check(Config("alembic.ini"))
except AutogenerateDiffsDetected as exc:
    found = [describe(diff) for diff in flat(exc.diffs)]
else:
    found = []
print("DIFFS " + json.dumps(found))
"""

Difference = tuple[str, str, str | None, str | None]  # что, где, в базе, в модели


def _tolerated(difference: Difference) -> bool:
    kind, _where, _in_db, in_model = difference
    return kind == "modify_default" and in_model is None


def _differences(tree: Path, db_url: str) -> list[Difference]:
    """Миграции чекаута поверх базы, затем сравнение с его моделями."""
    upgrade = _alembic(tree, db_url, "upgrade", "head")
    assert upgrade.returncode == 0, (
        "миграции не накатились на схему ревизии 0023:\n" + upgrade.stdout + upgrade.stderr
    )
    compare = subprocess.run(
        [sys.executable, "-c", _COMPARE],
        cwd=tree,
        env={**os.environ, "DATABASE_URL": db_url},
        capture_output=True,
        text=True,
        check=False,
    )
    marker = [line for line in compare.stdout.splitlines() if line.startswith("DIFFS ")]
    assert compare.returncode == 0 and len(marker) == 1, (
        "сравнение моделей с базой не состоялось:\n" + compare.stdout + compare.stderr
    )
    found = [tuple(item) for item in json.loads(marker[0].removeprefix("DIFFS "))]
    return [difference for difference in found if not _tolerated(difference)]


def _render(difference: Difference) -> str:
    kind, where, in_db, in_model = difference
    if in_db is None and in_model is None:
        return f"{kind}: {where}"
    return f"{kind}: {where} (в базе: {in_db}; в модели: {in_model})"


def test_migrations_bring_the_baseline_to_the_models(baseline_db: str) -> None:
    found = _differences(ROOT, baseline_db)
    assert not found, (
        "Модели (src/storage.py) и миграции расходятся. База, собранная миграциями "
        "из схемы ревизии 0023, отличается от моделей (add_* — есть в модели, нет в "
        "базе; remove_* — есть в базе, нет в модели):\n"
        + "\n".join(f"  - {_render(difference)}" for difference in found)
        + "\n"
        + WHAT_TO_DO
    )


# --- Проверка ловит то, ради чего стоит -------------------------------------

_FORGOTTEN_TABLE = '''

class ForgottenMigrationProbe(Base):
    """Опыт: модель без миграции."""

    __tablename__ = "forgotten_migration_probe"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note: Mapped[str] = mapped_column(String(50){note_options})
'''

_PRODUCT_ANCHOR = (
    '    snapshots: Mapped[list[PriceSnapshot]] = relationship(back_populates="product")'
)
_FORGOTTEN_COLUMN = (
    "    forgotten_probe: Mapped[str | None] = mapped_column(String(10), nullable=True)"
)
_DROPPED_COLUMN = (
    "    barcode: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)"
)

_PROBE_MIGRATION = '''"""probe: the table the forgotten model needs"""

import sqlalchemy as sa
from alembic import op

revision = "9999_probe"
down_revision = "{head}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forgotten_migration_probe",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("note", sa.String(length=50), nullable=False{note_options}),
    )


def downgrade() -> None:
    op.drop_table("forgotten_migration_probe")
'''

_SERVER_DEFAULT = ', server_default="none"'


def _checkout_copy(tmp_path: Path) -> Path:
    tree = tmp_path / "checkout"
    tree.mkdir()
    shutil.copy(ROOT / "alembic.ini", tree / "alembic.ini")
    for name in ("src", "migrations"):
        shutil.copytree(ROOT / name, tree / name, ignore=shutil.ignore_patterns("__pycache__"))
    return tree


def _edit_models(tree: Path, old: str, new: str) -> None:
    path = tree / "src" / "storage.py"
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1, (
        f"опыт привязан к строке src/storage.py, а её там не ровно одна: {old!r}. "
        "Если строку изменили законно — поправить якорь в этом тесте "
        "(_PRODUCT_ANCHOR, _DROPPED_COLUMN), проверку это не ослабляет."
    )
    path.write_text(source.replace(old, new), encoding="utf-8")


def _add_probe_model(tree: Path, *, note_options: str = "") -> None:
    path = tree / "src" / "storage.py"
    model = _FORGOTTEN_TABLE.format(note_options=note_options)
    path.write_text(path.read_text(encoding="utf-8") + model, encoding="utf-8")


def _add_probe_migration(tree: Path, *, note_options: str = "") -> None:
    (head,) = _script_directory(tree).get_heads()
    migration = tree / "migrations" / "versions" / "9999_probe.py"
    migration.write_text(
        _PROBE_MIGRATION.format(head=head, note_options=note_options), encoding="utf-8"
    )


def _kinds(found: list[Difference]) -> set[tuple[str, str]]:
    return {(kind, where) for kind, where, _in_db, _in_model in found}


def test_a_model_table_without_a_migration_is_caught(tmp_path: Path, baseline_db: str) -> None:
    tree = _checkout_copy(tmp_path)
    _add_probe_model(tree)

    found = _differences(tree, baseline_db)

    assert ("add_table", "forgotten_migration_probe") in _kinds(found), found


def test_a_model_column_without_a_migration_is_caught(tmp_path: Path, baseline_db: str) -> None:
    tree = _checkout_copy(tmp_path)
    _edit_models(tree, _PRODUCT_ANCHOR, _FORGOTTEN_COLUMN + "\n" + _PRODUCT_ANCHOR)

    found = _differences(tree, baseline_db)

    assert ("add_column", "products.forgotten_probe") in _kinds(found), found


def test_a_column_dropped_from_the_model_without_a_migration_is_caught(
    tmp_path: Path, baseline_db: str
) -> None:
    tree = _checkout_copy(tmp_path)
    _edit_models(tree, _DROPPED_COLUMN + "\n", "")

    found = _differences(tree, baseline_db)

    assert ("remove_column", "products.barcode") in _kinds(found), found


def test_a_matching_migration_makes_the_same_change_pass(tmp_path: Path, baseline_db: str) -> None:
    tree = _checkout_copy(tmp_path)
    _add_probe_model(tree)
    _add_probe_migration(tree)

    assert _differences(tree, baseline_db) == []


def test_a_server_default_only_the_migration_sets_is_let_through(
    tmp_path: Path, baseline_db: str
) -> None:
    tree = _checkout_copy(tmp_path)
    _add_probe_model(tree)
    _add_probe_migration(tree, note_options=_SERVER_DEFAULT)

    assert _differences(tree, baseline_db) == []


def test_a_server_default_only_the_model_names_is_caught(tmp_path: Path, baseline_db: str) -> None:
    tree = _checkout_copy(tmp_path)
    _add_probe_model(tree, note_options=_SERVER_DEFAULT)
    _add_probe_migration(tree)

    found = _differences(tree, baseline_db)

    assert found == [("modify_default", "forgotten_migration_probe.note", None, "none")], found
