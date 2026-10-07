"""Миграция обязана работать с кодом прошлого релиза.

Зачем тест: `deploy.yml` сначала обновляет базу и только потом копирует код и
рестартует сервисы — иначе новый код оказывался на старой схеме, и процессы,
которые таймеры запускают весь день, падали. Цена этого порядка: между
миграцией и рестартом (около минуты) на НОВОЙ схеме работает ПРОШЛЫЙ релиз —
API из памяти и CLI с диска. Добавленную колонку или таблицу он не замечает.
Удалённую или переименованную — читает и падает; на новую обязательную колонку
без значения по умолчанию не может вставить строку.

Такое изменение делается в два релиза: сначала код, который этим больше не
пользуется, следующим релизом — миграция. Во втором релизе в файле миграции
пишется `PREVIOUS_RELEASE_COMPATIBLE = "<почему прошлый релиз этого не заметит>"`
— тест её пропустит, а разбирающий PR увидит, на что автор опирается.

Покрываем каждую миграцию в каталоге (на 2026-10-07 ни одна из 23 под запрет не
попадает, так что отсчёт с какого-то номера не нужен):
- удаление и переименование таблиц и колонок, `alter_column`
- новую колонку NOT NULL без `server_default`
- `DROP TABLE` и `ALTER TABLE … DROP / RENAME / ALTER COLUMN / SET NOT NULL`,
  записанные сырым SQL в `op.execute` / `sa.text`

Чего тест не видит: смысл. Он читает текст миграции, а не код прошлого релиза,
поэтому не знает, пользуется ли тот удаляемой колонкой, и не заметит запрет,
записанный непривычно (SQL, собранный из частей; ограничение, добавленное
отдельным `create_check_constraint`). Это сторож от привычной ошибки, а не
доказательство совместимости.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"
MARKER = "PREVIOUS_RELEASE_COMPATIBLE"
MARKER_MIN_LENGTH = 40

BREAKING_OPS = {"drop_column", "drop_table", "rename_table", "alter_column"}
SQL_CALLS = {"execute", "text"}
# Оператор целиком, а не слово где-нибудь в строке: «drop table» в данных,
# которые миграция вставляет, запретом не считается.
DROP_TABLE_SQL = re.compile(r"DROP\s+TABLE\b", re.IGNORECASE)
ALTER_TABLE_SQL = re.compile(r"ALTER\s+TABLE\b", re.IGNORECASE)
# Внутри ALTER TABLE — всё, что отнимает или меняет уже существующее.
# DROP CONSTRAINT / DROP DEFAULT / DROP NOT NULL только ослабляют схему.
ALTER_TABLE_BREAKING = re.compile(
    r"\b(DROP\s+(?!CONSTRAINT\b|DEFAULT\b|NOT\s+NULL\b)|RENAME\b|ALTER\s+COLUMN\b.*\bTYPE\b"
    r"|SET\s+NOT\s+NULL\b)",
    re.IGNORECASE | re.DOTALL,
)


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _outside_downgrade(tree: ast.Module):
    """Все узлы модуля, кроме тела `downgrade`: откат прошлому релизу не мешает."""
    pending: list[ast.AST] = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def _adds_required_column(call: ast.Call) -> bool:
    for node in ast.walk(call):
        if not (isinstance(node, ast.Call) and _call_name(node) == "Column"):
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        nullable = keywords.get("nullable")
        if (
            isinstance(nullable, ast.Constant)
            and nullable.value is False
            and "server_default" not in keywords
        ):
            return True
    return False


def _breaking_sql(sql: str) -> list[str]:
    findings = []
    for statement in sql.split(";"):
        statement = statement.strip()
        if DROP_TABLE_SQL.match(statement):
            findings.append("DROP TABLE")
        elif ALTER_TABLE_SQL.match(statement) and ALTER_TABLE_BREAKING.search(statement):
            findings.append("ALTER TABLE, который отнимает или меняет существующее")
    return findings


def breaking_changes(source: str) -> list[str]:
    """Что в миграции сломает код, написанный до неё."""
    findings: list[str] = []
    for node in _outside_downgrade(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in BREAKING_OPS:
            findings.append(f"{name} (строка {node.lineno})")
        elif name == "add_column" and _adds_required_column(node):
            findings.append(f"add_column NOT NULL без server_default (строка {node.lineno})")
        elif name in SQL_CALLS:
            for part in ast.walk(node):
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    findings.extend(
                        f"SQL: {found} (строка {part.lineno})"
                        for found in _breaking_sql(part.value)
                    )
    return sorted(set(findings))


def compatibility_note(source: str) -> str | None:
    for node in ast.parse(source).body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(target, ast.Name) and target.id == MARKER for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def _number(path: Path) -> int:
    match = re.match(r"(\d{4})_", path.name)
    assert match, f"{path.name}: имя миграции должно начинаться с четырёх цифр и «_»"
    return int(match.group(1))


def test_every_migration_is_numbered_so_none_escapes_the_guard() -> None:
    files = sorted(VERSIONS.glob("*.py"))
    assert files, "каталог миграций пуст — путь в тесте устарел?"
    numbers = [_number(path) for path in files]
    assert len(numbers) == len(set(numbers)), "два файла миграций с одним номером"


def test_new_migrations_keep_the_previous_release_working() -> None:
    problems = []
    for path in sorted(VERSIONS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        findings = breaking_changes(source)
        note = compatibility_note(source)
        if findings and (note is None or len(note.strip()) < MARKER_MIN_LENGTH):
            problems.append(f"{path.name}: {', '.join(findings)}")
    assert not problems, (
        "Миграция меняет схему так, что код прошлого релиза на ней не работает:\n  "
        + "\n  ".join(problems)
        + "\ndeploy.yml применяет миграцию ДО копирования кода и рестарта: около минуты "
        "на новой схеме живёт прошлый релиз (API из памяти, CLI таймеров с диска). "
        "Раздели на два релиза: сначала код, который этим не пользуется, следующим "
        "релизом — миграция. Если прошлый релиз этим уже не пользуется, запиши в файле "
        f'миграции {MARKER} = "<почему>" (не короче {MARKER_MIN_LENGTH} знаков).'
    )


def _kinds(source: str) -> list[str]:
    """Находки без номеров строк: тест про то, ЧТО найдено."""
    return [re.sub(r" \(строка \d+\)$", "", finding) for finding in breaking_changes(source)]


ALTERS = "SQL: ALTER TABLE, который отнимает или меняет существующее"

MIGRATION = """
import sqlalchemy as sa
from alembic import op

{header}

def upgrade() -> None:
{upgrade}


def downgrade() -> None:
{downgrade}
"""


def _migration(upgrade: str, *, downgrade: str = "    pass", header: str = "") -> str:
    return MIGRATION.format(upgrade=upgrade, downgrade=downgrade, header=header)


@pytest.mark.parametrize(
    ("upgrade", "expected"),
    [
        ('    op.drop_column("t", "c")', ["drop_column"]),
        ('    op.drop_table("t")', ["drop_table"]),
        ('    op.rename_table("t", "u")', ["rename_table"]),
        ('    op.alter_column("t", "c", new_column_name="d")', ["alter_column"]),
        (
            '    with op.batch_alter_table("t") as batch:\n        batch.drop_column("c")',
            ["drop_column"],
        ),
        (
            '    op.add_column("t", sa.Column("c", sa.Integer(), nullable=False))',
            ["add_column NOT NULL без server_default"],
        ),
        ('    op.execute("DROP TABLE t")', ["SQL: DROP TABLE"]),
        ('    op.execute("ALTER TABLE t DROP COLUMN c")', [ALTERS]),
        # В PostgreSQL слово COLUMN необязательно.
        ('    op.execute("ALTER TABLE t DROP c")', [ALTERS]),
        ('    op.execute(sa.text("alter table t  rename  to u"))', [ALTERS]),
        ('    op.execute("ALTER TABLE t ALTER COLUMN c SET NOT NULL")', [ALTERS]),
        ('    op.execute("ALTER TABLE t ALTER COLUMN c TYPE text")', [ALTERS]),
        ('    op.execute("UPDATE t SET c = 1; DROP TABLE old_t")', ["SQL: DROP TABLE"]),
    ],
)
def test_breaking_changes_are_found(upgrade: str, expected: list[str]) -> None:
    assert _kinds(_migration(upgrade)) == expected


@pytest.mark.parametrize(
    "upgrade",
    [
        '    op.add_column("t", sa.Column("c", sa.Integer(), nullable=True))',
        '    op.add_column("t", sa.Column("c", sa.Integer()))',
        '    op.add_column("t", sa.Column("c", sa.Integer(), nullable=False, server_default="0"))',
        '    op.create_table("t", sa.Column("id", sa.Integer(), primary_key=True))',
        '    op.create_index("ix_t_c", "t", ["c"])',
        "    op.execute(\"INSERT INTO t (c) VALUES ('drop table t')\")",
        '    op.execute("ALTER TABLE t ADD COLUMN c integer")',
        '    op.execute("ALTER TABLE t DROP CONSTRAINT t_c_check")',
        '    op.execute("ALTER TABLE t ALTER COLUMN c DROP NOT NULL")',
        '    op.execute("ALTER TABLE t ALTER COLUMN c SET DEFAULT 0")',
        '    """drop column — только слова в описании"""\n    op.create_index("ix", "t", ["c"])',
    ],
)
def test_additive_changes_pass(upgrade: str) -> None:
    assert breaking_changes(_migration(upgrade)) == []


def test_downgrade_may_remove_what_upgrade_added() -> None:
    source = _migration(
        '    op.add_column("t", sa.Column("c", sa.Integer(), nullable=True))',
        downgrade='    op.drop_column("t", "c")',
    )
    assert breaking_changes(source) == []


def test_a_helper_called_from_upgrade_does_not_hide_the_change() -> None:
    source = _migration(
        "    _cleanup()", header='def _cleanup() -> None:\n    op.drop_table("t")\n'
    )
    assert _kinds(source) == ["drop_table"]


def test_the_note_is_read_only_as_a_module_level_string() -> None:
    reason = "колонку перестал читать релиз от 2026-10-01; здесь она только удаляется"
    assert compatibility_note(_migration("    pass", header=f'{MARKER} = "{reason}"')) == reason
    assert (
        compatibility_note(_migration("    pass", header=f'{MARKER}: str = "{reason}"')) == reason
    )
    assert compatibility_note(_migration("    pass", header=f"{MARKER} = True")) is None
    assert compatibility_note(_migration(f'    {MARKER} = "{reason}"')) is None
