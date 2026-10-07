"""Миграция обязана работать с кодом прошлого релиза.

Зачем тест: `deploy.yml` сначала обновляет базу и только потом копирует код и
рестартует сервисы — иначе новый код оказывался на старой схеме, и процессы,
которые таймеры запускают весь день, падали. Цена этого порядка: после
миграции на НОВОЙ схеме какое-то время работает ПРОШЛЫЙ релиз — API из памяти
до рестарта, CLI с диска, сбор, начатый до выкладки, до своего конца. Новую
необязательную колонку или таблицу он не замечает. Удалённую или
переименованную — читает и падает; на новую обязательную колонку или новое
ограничение натыкается при записи.

Тест устроен как разрешительный список, а не как список запретов: известно
безопасное — новая таблица, новый неуникальный индекс, новая колонка, которую
можно не заполнять, и чтение (`SELECT`). Всё остальное, что миграция делает со
схемой или данными, требует письменного объяснения в самом файле:

    PREVIOUS_RELEASE_COMPATIBLE = "почему прошлый релиз этого не заметит"

Так незнакомое — красное. Список запретов пропускал бы всё, чего в нём нет:
ограничение уникальности, обязательную колонку сырым SQL, SQL из переменной.

Удаление и переименование делается в два релиза: сначала код, который этим
больше не пользуется, следующим релизом — миграция с объяснением.

Покрываем миграции с 0024: всё до неё применено на проде при прежнем порядке
выкладки. Смотрим только то, что достижимо из `upgrade`: откат прошлому релизу
не мешает.

Чего тест не видит: правду. Он читает текст миграции, а не код прошлого релиза,
и не знает, верно ли объяснение, — его читает тот, кто разбирает PR. Не видит и
обход, записанный так, что вызов не похож на вызов (`getattr(op, name)(…)`).
Это сторож, который заставляет автора остановиться и написать, на что он
опирается, а не доказательство совместимости.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"
FIRST_GUARDED = 24
MARKER = "PREVIOUS_RELEASE_COMPATIBLE"
NOTE_MIN_WORDS = 6

# Вызовы `op.…`, которые прошлый релиз не замечает. `add_column` и
# `create_index` — с оговорками, см. `_op_finding`.
ALWAYS_SAFE_OPS = {"create_table", "get_bind", "get_context", "f"}
# Через эти методы в базу уходит SQL — у `op`, соединения или сессии.
SQL_METHODS = {"execute", "exec_driver_sql", "executemany", "scalar", "scalars"}
SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def _is_op_call(node: ast.Call) -> bool:
    function = node.func
    return (
        isinstance(function, ast.Attribute)
        and isinstance(function.value, ast.Name)
        and function.value.id == "op"
    )


def _called_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _keywords(node: ast.Call) -> dict[str | None, ast.expr]:
    return {keyword.arg: keyword.value for keyword in node.keywords}


def _is_constant(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _adds_required_column(call: ast.Call) -> bool:
    for node in ast.walk(call):
        if not (isinstance(node, ast.Call) and _called_name(node) == "Column"):
            continue
        keywords = _keywords(node)
        nullable = keywords.get("nullable")
        # nullable=True или не задан — колонку можно не заполнять. Всё прочее
        # (False, выражение) без значения по умолчанию ломает вставку.
        optional = nullable is None or _is_constant(nullable, True)
        if not optional and "server_default" not in keywords:
            return True
    return False


def _sql_finding(node: ast.Call) -> str | None:
    """SQL безопасен, только если он записан прямо здесь и это чтение."""
    if not node.args and not node.keywords:
        return None  # `.scalar()` у готового результата — SQL сюда не передан
    literals = [
        part.value
        for part in ast.walk(node)
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    ]
    statements = [
        statement.strip()
        for literal in literals
        for statement in SQL_COMMENT.sub(" ", literal).split(";")
        if statement.strip()
    ]
    if not statements:
        return "SQL не записан литералом — что он делает, отсюда не видно"
    for statement in statements:
        verb = statement.split()[0].upper()
        if verb != "SELECT":
            return f"SQL: {verb}"
    return None


def _op_finding(node: ast.Call) -> str | None:
    name = _called_name(node)
    if name in ALWAYS_SAFE_OPS:
        return None
    if name == "add_column":
        return "add_column NOT NULL без server_default" if _adds_required_column(node) else None
    if name == "create_index":
        unique = _keywords(node).get("unique")
        return None if unique is None or _is_constant(unique, False) else "create_index unique"
    if name in SQL_METHODS:
        return _sql_finding(node)
    return f"op.{name}"


def _reachable_from_upgrade(tree: ast.Module) -> list[ast.AST]:
    """Тело `upgrade`, функции модуля, на которые оно ссылается, и код вне функций."""
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    reached: list[str] = []
    queue = ["upgrade"]
    while queue:
        name = queue.pop()
        if name in reached or name not in functions:
            continue
        reached.append(name)
        queue.extend(
            node.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Name) and node.id in functions
        )
    module_level = [
        node for node in tree.body if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    return [functions[name] for name in reached] + module_level


def unexplained_changes(source: str) -> list[str]:
    """Что миграция делает сверх известного безопасного."""
    findings: set[str] = set()
    for root in _reachable_from_upgrade(ast.parse(source)):
        for node in ast.walk(root):
            if not isinstance(node, ast.Call):
                continue
            if _is_op_call(node):
                finding = _op_finding(node)
            elif _called_name(node) in SQL_METHODS and isinstance(node.func, ast.Attribute):
                finding = _sql_finding(node)
            else:
                finding = None
            if finding:
                findings.add(f"{finding} (строка {node.lineno})")
    return sorted(findings)


def compatibility_note(source: str) -> str | None:
    """Объяснение автора: строка на уровне модуля, не короче нескольких слов."""
    for node in ast.parse(source).body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(target, ast.Name) and target.id == MARKER for target in targets):
            continue
        value = node.value
        if (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and len(value.value.split()) >= NOTE_MIN_WORDS
        ):
            return value.value
    return None


def _number(path: Path) -> int:
    match = re.match(r"(\d{4})_", path.name)
    assert match, f"{path.name}: имя миграции должно начинаться с четырёх цифр и «_»"
    return int(match.group(1))


def test_every_migration_is_numbered_so_none_escapes_the_guard() -> None:
    """Сторож отбирает миграции по номеру: файл без номера прошёл бы мимо него."""
    files = sorted(VERSIONS.glob("*.py"))
    assert files, "каталог миграций пуст — путь в тесте устарел?"
    numbers = [_number(path) for path in files]
    assert len(numbers) == len(set(numbers)), "два файла миграций с одним номером"
    assert max(numbers) >= FIRST_GUARDED - 1, "отсчёт сторожа опередил каталог миграций"


def test_new_migrations_keep_the_previous_release_working() -> None:
    problems = []
    for path in sorted(VERSIONS.glob("*.py")):
        if _number(path) < FIRST_GUARDED:
            continue
        source = path.read_text(encoding="utf-8")
        findings = unexplained_changes(source)
        if findings and compatibility_note(source) is None:
            problems.append(f"{path.name}: {', '.join(findings)}")
    assert not problems, (
        "Миграция делает то, о чём неизвестно, переживёт ли это код прошлого релиза:\n  "
        + "\n  ".join(problems)
        + "\ndeploy.yml применяет миграцию ДО копирования кода и рестарта: на новой схеме "
        "какое-то время живёт прошлый релиз (API из памяти, CLI таймеров с диска, сбор, "
        "начатый до выкладки). Удаление и переименование — в два релиза: сначала код, "
        "который этим не пользуется, следующим релизом миграция. Если прошлый релиз "
        f'изменения не заметит, напиши в файле миграции {MARKER} = "<почему>" '
        f"(не короче {NOTE_MIN_WORDS} слов) — это прочтёт тот, кто разбирает PR."
    )


def test_the_additive_migration_that_prompted_the_rule_needs_no_note() -> None:
    """0023 добавила необязательную колонку — ровно то, что порядок разрешает."""
    (path,) = VERSIONS.glob("0023_*.py")
    assert unexplained_changes(path.read_text(encoding="utf-8")) == []


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


def _kinds(source: str) -> list[str]:
    """Находки без номеров строк: тест про то, ЧТО найдено."""
    return [re.sub(r" \(строка \d+\)$", "", finding) for finding in unexplained_changes(source)]


NOT_LITERAL = "SQL не записан литералом — что он делает, отсюда не видно"


@pytest.mark.parametrize(
    ("upgrade", "expected"),
    [
        ('    op.drop_column("t", "c")', ["op.drop_column"]),
        ('    op.drop_table("t")', ["op.drop_table"]),
        ('    op.rename_table("t", "u")', ["op.rename_table"]),
        ('    op.alter_column("t", "c", new_column_name="d")', ["op.alter_column"]),
        (
            '    with op.batch_alter_table("t") as batch:\n        batch.drop_column("c")',
            ["op.batch_alter_table"],
        ),
        (
            '    op.add_column("t", sa.Column("c", sa.Integer(), nullable=False))',
            ["add_column NOT NULL без server_default"],
        ),
        # Ограничение прошлый релиз встретит при записи.
        ('    op.create_unique_constraint("uq", "t", ["c"])', ["op.create_unique_constraint"]),
        ('    op.create_check_constraint("ck", "t", "c > 0")', ["op.create_check_constraint"]),
        ('    op.create_index("ix", "t", ["c"], unique=True)', ["create_index unique"]),
        ('    op.execute("DROP TABLE t")', ["SQL: DROP"]),
        ('    op.execute("ALTER TABLE t ADD COLUMN c int NOT NULL")', ["SQL: ALTER"]),
        ('    op.execute(sa.text("alter table t rename to u"))', ["SQL: ALTER"]),
        ('    op.execute("-- чистка\\nDROP TABLE t")', ["SQL: DROP"]),
        ('    op.execute("SELECT 1; DELETE FROM t")', ["SQL: DELETE"]),
        ('    op.execute("UPDATE t SET c = 1")', ["SQL: UPDATE"]),
        ('    op.get_bind().exec_driver_sql("TRUNCATE t")', ["SQL: TRUNCATE"]),
        ('    op.get_bind().execute(sa.text("INSERT INTO t (c) VALUES (1)"))', ["SQL: INSERT"]),
        # Что уйдёт в базу, отсюда не видно — значит, объяснять.
        ("    op.execute(SQL)", [NOT_LITERAL]),
        ("    op.get_bind().execute(sa.insert(table).values(c=1))", [NOT_LITERAL]),
        ('    op.bulk_insert(table, [{"c": 1}])', ["op.bulk_insert"]),
    ],
)
def test_anything_beyond_the_safe_list_is_reported(upgrade: str, expected: list[str]) -> None:
    assert _kinds(_migration(upgrade, header='SQL = "DROP TABLE t"')) == expected


@pytest.mark.parametrize(
    "upgrade",
    [
        '    op.add_column("t", sa.Column("c", sa.Integer(), nullable=True))',
        '    op.add_column("t", sa.Column("c", sa.Integer()))',
        '    op.add_column("t", sa.Column("c", sa.Integer(), nullable=False, server_default="0"))',
        '    op.create_table("t", sa.Column("id", sa.Integer(), primary_key=True))',
        '    op.create_index("ix_t_c", "t", ["c"])',
        '    op.create_index(op.f("ix_t_c"), "t", ["c"], unique=False)',
        '    columns = sa.inspect(op.get_bind()).get_columns("t")',
        '    rows = op.get_bind().execute(sa.text("SELECT id FROM t")).scalars().all()',
        '    count = op.get_bind().execute(sa.text("SELECT count(*) FROM t")).scalar()',
        '    """drop column — только слова в описании"""\n    op.create_index("ix", "t", ["c"])',
    ],
)
def test_known_safe_changes_need_no_note(upgrade: str) -> None:
    assert _kinds(_migration(upgrade)) == []


def test_downgrade_and_its_helpers_are_not_examined() -> None:
    source = _migration(
        '    op.add_column("t", sa.Column("c", sa.Integer(), nullable=True))',
        downgrade="    _undo()",
        header='def _undo() -> None:\n    op.drop_column("t", "c")\n',
    )
    assert _kinds(source) == []


def test_a_helper_reached_from_upgrade_is_examined() -> None:
    called = _migration(
        "    _cleanup()", header='def _cleanup() -> None:\n    op.drop_table("t")\n'
    )
    assert _kinds(called) == ["op.drop_table"]
    passed_around = _migration(
        "    for step in (_cleanup,):\n        step()",
        header='def _cleanup() -> None:\n    op.drop_table("t")\n',
    )
    assert _kinds(passed_around) == ["op.drop_table"]


def test_a_function_named_downgrade_inside_upgrade_hides_nothing() -> None:
    source = _migration(
        '    def downgrade() -> None:\n        op.drop_table("t")\n\n    downgrade()'
    )
    assert _kinds(source) == ["op.drop_table"]


def test_the_note_must_be_a_module_level_sentence() -> None:
    reason = "колонку перестал читать релиз от 2026-10-01, здесь она только удаляется"
    assert compatibility_note(_migration("    pass", header=f'{MARKER} = "{reason}"')) == reason
    assert (
        compatibility_note(_migration("    pass", header=f'{MARKER}: str = "{reason}"')) == reason
    )
    assert compatibility_note(_migration("    pass", header=f"{MARKER} = True")) is None
    assert compatibility_note(_migration(f'    {MARKER} = "{reason}"')) is None
    # Отписка вместо объяснения не считается.
    assert compatibility_note(_migration("    pass", header=f'{MARKER} = "{"x" * 60}"')) is None
    assert compatibility_note(_migration("    pass", header=f'{MARKER} = "всё хорошо"')) is None
