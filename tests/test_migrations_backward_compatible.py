"""Миграция обязана работать с кодом прошлого релиза.

Зачем тест: `deploy.yml` сначала обновляет базу и только потом копирует код и
рестартует сервисы — иначе новый код оказывался на старой схеме, и процессы,
которые таймеры запускают весь день, падали. Цена этого порядка: после
миграции на НОВОЙ схеме какое-то время работает ПРОШЛЫЙ релиз — API из памяти
до рестарта, CLI с диска, сбор, начатый до выкладки, до своего конца. Новую
необязательную колонку или таблицу он не замечает. Удалённую или
переименованную — читает и падает; на новую обязательную колонку или новое
ограничение натыкается при записи.

Тест устроен как разрешительный список, а не как список запретов. Молча
проходит только известное безопасное:
- `op.create_table`, `op.create_index` без `unique`, `op.add_column` с колонкой,
  которую можно не заполнять (то же внутри `with op.batch_alter_table(…) as b`);
- чтение: `SELECT`, записанный литералом, и `sa.select(…)`;
- `sa.inspect(bind)`.

Всё остальное, что миграция делает через `op`, соединение или SQL, требует
письменного объяснения в самом файле:

    PREVIOUS_RELEASE_COMPATIBLE = "почему прошлый релиз этого не заметит"

Объяснения требуют и те случаи, где отсюда не видно, что произойдёт: SQL не
литералом, колонка, описанная не в самом вызове `add_column`, соединение,
переданное в чужую функцию (`metadata.drop_all(bind)`, `Session(bind=bind)`),
`op` под другим именем, импорт из `src`.

Удаление и переименование делается в два релиза: сначала код, который этим
больше не пользуется, следующим релизом — миграция с объяснением.

Покрываем миграции с 0024: всё до неё применено на проде при прежнем порядке
выкладки. Смотрим то, что достижимо из `upgrade`: откат прошлому релизу не
мешает.

Чего тест не видит: правду. Он читает текст миграции, а не код прошлого релиза,
и не знает, верно ли объяснение, — его читает тот, кто разбирает PR. Не видит
он и работу с базой, которая не проходит ни через `op`, ни через полученное от
него соединение, ни через методы `execute`/`scalar` (например, свой
`create_engine` внутри миграции). Это сторож, который заставляет автора
остановиться и написать, на что он опирается, а не доказательство
совместимости.
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

ALWAYS_SAFE_OPS = {"create_table", "get_bind", "get_context", "f"}
# Через эти методы в базу уходит SQL — у `op`, соединения или сессии.
SQL_METHODS = {"execute", "exec_driver_sql", "executemany", "scalar", "scalars"}
# Кому можно отдать соединение, не объясняясь: они только читают схему.
BIND_READERS = {"inspect"}
SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
SQL_WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)
SQL_SIDE_EFFECT = re.compile(r"\b(setval|nextval)\s*\(", re.IGNORECASE)

NOT_LITERAL = "SQL не записан литералом — что он делает, отсюда не видно"
COLUMN_ELSEWHERE = "add_column: колонка описана не в самом вызове"
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _called_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _is_constant(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _is_get_bind(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_bind"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    )


def _values(node: ast.Call) -> list[ast.expr]:
    return [*node.args, *(keyword.value for keyword in node.keywords)]


def _has_unpacking(node: ast.Call) -> bool:
    return any(isinstance(arg, ast.Starred) for arg in node.args) or any(
        keyword.arg is None for keyword in node.keywords
    )


def _column_finding(call: ast.Call) -> str | None:
    """`add_column` безопасен, только если колонка описана тут же и её можно не заполнять."""
    columns = [
        value
        for value in _values(call)
        if isinstance(value, ast.Call) and _called_name(value) == "Column"
    ]
    if len(columns) != 1 or _has_unpacking(call) or _has_unpacking(columns[0]):
        return COLUMN_ELSEWHERE
    keywords = {keyword.arg: keyword.value for keyword in columns[0].keywords}
    nullable = keywords.get("nullable")
    optional = nullable is None or _is_constant(nullable, True)
    default = keywords.get("server_default")
    has_default = default is not None and not _is_constant(default, None)
    if optional or has_default:
        return None
    return "add_column NOT NULL без server_default"


def _index_finding(call: ast.Call) -> str | None:
    unique = {keyword.arg: keyword.value for keyword in call.keywords}.get("unique")
    plain = unique is None or _is_constant(unique, False)
    # Четвёртый и пятый позиционные аргументы — schema и unique.
    if plain and len(call.args) <= 3 and not _has_unpacking(call):
        return None
    return "create_index unique или с аргументами, которых отсюда не видно"


def _sql_finding(call: ast.Call) -> str | None:
    """SQL безопасен, только если он записан прямо здесь и это чтение."""
    if not call.args and not call.keywords:
        return None  # `.scalar()` у готового результата — SQL сюда не передан
    if not call.args:
        return NOT_LITERAL
    statement = call.args[0]
    if isinstance(statement, ast.Call) and _called_name(statement) == "text" and statement.args:
        statement = statement.args[0]
    if isinstance(statement, ast.Call) and _called_name(statement) == "select":
        return None
    if not (isinstance(statement, ast.Constant) and isinstance(statement.value, str)):
        return NOT_LITERAL
    for sql in SQL_COMMENT.sub(" ", statement.value).split(";"):
        if not sql.strip():
            continue
        verb = sql.split()[0].upper()
        if verb not in {"SELECT", "WITH"}:
            return f"SQL: {verb}"
        if SQL_WRITE.search(sql) or SQL_SIDE_EFFECT.search(sql):
            return f"SQL: {verb}, который меняет данные"
    return None


class _Migration:
    def __init__(self, source: str) -> None:
        self.tree = ast.parse(source)
        self.functions = {
            node.name: node for node in self.tree.body if isinstance(node, FunctionNode)
        }
        self.module_level = [node for node in self.tree.body if not isinstance(node, FunctionNode)]
        nodes = list(ast.walk(self.tree))
        # `with op.batch_alter_table("t") as batch:` — batch принимает те же вызовы, что op.
        self.batch_blocks: set[int] = set()
        self.op_names = {"op"}
        for node in nodes:
            if not isinstance(node, ast.With | ast.AsyncWith):
                continue
            for item in node.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and self._receiver(call) == "op"
                    and _called_name(call) == "batch_alter_table"
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    self.batch_blocks.add(id(call))
                    self.op_names.add(item.optional_vars.id)
        self.attribute_bases = {id(node.value) for node in nodes if isinstance(node, ast.Attribute)}
        self.binds = self._bind_names()

    @staticmethod
    def _receiver(call: ast.Call) -> str | None:
        function = call.func
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            return function.value.id
        return None

    def _is_bind(self, value: ast.expr, scope: str | None) -> bool:
        if _is_get_bind(value):
            return True
        return isinstance(value, ast.Name) and value.id in self.binds[scope] | self.binds[None]

    def _bind_names(self) -> dict[str | None, set[str]]:
        """Под какими именами в каждой функции модуля живёт соединение с базой.

        Привычная запись здесь — `_has_column(op.get_bind(), "t", "c")`: проверка
        «уже есть?» вынесена в функцию того же файла. Её тело разбирается по тем
        же правилам, поэтому соединение прослеживается в неё, а не считается
        отданным в чужие руки. ``None`` — код вне функций.
        """
        scopes: dict[str | None, list[ast.AST]] = {None: list(self.module_level)}
        scopes.update({name: [node] for name, node in self.functions.items()})
        self.binds = {
            scope: {
                target.id
                for root in roots
                for node in ast.walk(root)
                if isinstance(node, ast.Assign) and _is_get_bind(node.value)
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            for scope, roots in scopes.items()
        }
        changed = True
        while changed:
            changed = False
            for scope, roots in scopes.items():
                for call in (node for root in roots for node in ast.walk(root)):
                    if not (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id in self.functions
                    ):
                        continue
                    arguments = self.functions[call.func.id].args
                    names = [argument.arg for argument in (*arguments.posonlyargs, *arguments.args)]
                    received = {
                        names[index]
                        for index, value in enumerate(call.args)
                        if index < len(names) and self._is_bind(value, scope)
                    } | {
                        keyword.arg
                        for keyword in call.keywords
                        if keyword.arg and self._is_bind(keyword.value, scope)
                    }
                    if not received <= self.binds[call.func.id]:
                        self.binds[call.func.id] |= received
                        changed = True
        return self.binds

    def _reachable(self) -> list[tuple[str | None, ast.AST]]:
        """Код вне функций, `upgrade` и функции модуля, на которые они ссылаются."""

        def referenced(root: ast.AST) -> list[str]:
            return [
                node.id
                for node in ast.walk(root)
                if isinstance(node, ast.Name) and node.id in self.functions
            ]

        reached: list[str] = []
        queue = ["upgrade", *(name for node in self.module_level for name in referenced(node))]
        while queue:
            name = queue.pop()
            if name in reached or name not in self.functions:
                continue
            reached.append(name)
            queue.extend(referenced(self.functions[name]))
        return [
            *((None, node) for node in self.module_level),
            *((name, self.functions[name]) for name in reached),
        ]

    def _op_finding(self, call: ast.Call) -> str | None:
        name = _called_name(call)
        if name in ALWAYS_SAFE_OPS:
            return None
        if name == "batch_alter_table":
            # Без `as имя` вызовы внутри блока не видны.
            return None if id(call) in self.batch_blocks else "op.batch_alter_table без `as`"
        if name == "add_column":
            return _column_finding(call)
        if name == "create_index":
            return _index_finding(call)
        if name in SQL_METHODS:
            return _sql_finding(call)
        return f"op.{name}"

    def _hands_the_bind_away(self, call: ast.Call, scope: str | None) -> bool:
        if _called_name(call) in BIND_READERS:
            return False
        if isinstance(call.func, ast.Name) and call.func.id in self.functions:
            return False  # функция этого же файла: её тело разобрано, соединение прослежено
        return any(self._is_bind(value, scope) for value in _values(call))

    def findings(self) -> list[str]:
        found: set[str] = set()

        def report(what: str | None, node: ast.AST) -> None:
            if what:
                found.add(f"{what} (строка {node.lineno})")  # type: ignore[attr-defined]

        for node in ast.walk(self.tree):
            # Что сделает код приложения, из текста миграции не видно.
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "src":
                report("импорт из src", node)
            elif isinstance(node, ast.Import) and any(
                alias.name.split(".")[0] == "src" for alias in node.names
            ):
                report("импорт из src", node)

        for scope, root in self._reachable():
            for node in ast.walk(root):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in self.op_names
                    and id(node) not in self.attribute_bases
                ):
                    report(f"{node.id} передан дальше или переименован", node)
                if not isinstance(node, ast.Call):
                    continue
                name = _called_name(node)
                if self._receiver(node) in self.op_names:
                    report(self._op_finding(node), node)
                elif isinstance(node.func, ast.Attribute) and name in SQL_METHODS:
                    report(_sql_finding(node), node)
                elif self._hands_the_bind_away(node, scope):
                    report(f"соединение с базой передано в {name or 'вызов'}(…)", node)
        return sorted(found)


def unexplained_changes(source: str) -> list[str]:
    """Что миграция делает сверх известного безопасного."""
    return _Migration(source).findings()


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


BATCH = '    with op.batch_alter_table("t") as batch:\n        '
INDEX_UNCLEAR = "create_index unique или с аргументами, которых отсюда не видно"


@pytest.mark.parametrize(
    ("upgrade", "expected"),
    [
        ('    op.drop_column("t", "c")', ["op.drop_column"]),
        ('    op.drop_table("t")', ["op.drop_table"]),
        ('    op.rename_table("t", "u")', ["op.rename_table"]),
        ('    op.alter_column("t", "c", new_column_name="d")', ["op.alter_column"]),
        (BATCH + 'batch.drop_column("c")', ["op.drop_column"]),
        (BATCH + 'batch.create_unique_constraint("uq", ["c"])', ["op.create_unique_constraint"]),
        # Без `as` вызовы внутри блока не видны.
        (
            '    block = op.batch_alter_table("t")\n    with block as batch:\n        batch.drop_column("c")',
            ["op.batch_alter_table без `as`"],
        ),
        (
            '    op.add_column("t", sa.Column("c", sa.Integer(), nullable=False))',
            ["add_column NOT NULL без server_default"],
        ),
        (
            '    op.add_column("t", sa.Column("c", sa.Integer(), nullable=False, server_default=None))',
            ["add_column NOT NULL без server_default"],
        ),
        (
            BATCH + 'batch.add_column(sa.Column("c", sa.Integer(), nullable=False))',
            ["add_column NOT NULL без server_default"],
        ),
        # Колонка описана в другом месте — обязательна она или нет, отсюда не видно.
        ('    op.add_column("t", COLUMN)', [COLUMN_ELSEWHERE]),
        ('    op.add_column("t", _column("c"))', [COLUMN_ELSEWHERE]),
        ('    op.add_column("t", sa.Column("c", sa.Integer(), **OPTIONS))', [COLUMN_ELSEWHERE]),
        # Ограничение прошлый релиз встретит при записи.
        ('    op.create_unique_constraint("uq", "t", ["c"])', ["op.create_unique_constraint"]),
        ('    op.create_check_constraint("ck", "t", "c > 0")', ["op.create_check_constraint"]),
        ('    op.create_index("ix", "t", ["c"], unique=True)', [INDEX_UNCLEAR]),
        ('    op.create_index("ix", "t", ["c"], None, True)', [INDEX_UNCLEAR]),
        ('    op.execute("DROP TABLE t")', ["SQL: DROP"]),
        ('    op.execute("ALTER TABLE t ADD COLUMN c int NOT NULL")', ["SQL: ALTER"]),
        ('    op.execute(sa.text("alter table t rename to u"))', ["SQL: ALTER"]),
        ('    op.execute("-- чистка\\nDROP TABLE t")', ["SQL: DROP"]),
        ('    op.execute("SELECT 1; DELETE FROM t")', ["SQL: DELETE"]),
        ('    op.execute("UPDATE t SET c = 1")', ["SQL: UPDATE"]),
        ('    op.get_bind().exec_driver_sql("TRUNCATE t")', ["SQL: TRUNCATE"]),
        ('    op.get_bind().execute(sa.text("INSERT INTO t (c) VALUES (1)"))', ["SQL: INSERT"]),
        (
            '    op.execute("WITH gone AS (DELETE FROM t RETURNING id) SELECT count(*) FROM gone")',
            ["SQL: WITH, который меняет данные"],
        ),
        (
            "    op.execute(\"SELECT setval('t_id_seq', 1)\")",
            ["SQL: SELECT, который меняет данные"],
        ),
        # Что уйдёт в базу, отсюда не видно — значит, объяснять.
        ("    op.execute(SQL)", [NOT_LITERAL]),
        ('    op.execute(f"SELECT 1; {SQL}")', [NOT_LITERAL]),
        ("    op.get_bind().execute(sa.insert(table).values(c=1))", [NOT_LITERAL]),
        ('    op.bulk_insert(table, [{"c": 1}])', ["op.bulk_insert"]),
        # Соединение, отданное в чужие руки.
        ("    metadata.drop_all(op.get_bind())", ["соединение с базой передано в drop_all(…)"]),
        (
            "    bind = op.get_bind()\n    table.drop(bind)",
            ["соединение с базой передано в drop(…)"],
        ),
        (
            "    session = Session(bind=op.get_bind())\n    session.add(row)",
            ["соединение с базой передано в Session(…)"],
        ),
        # `op` под другим именем или в чужих руках.
        ('    o = op\n    o.drop_table("t")', ["op передан дальше или переименован"]),
        ('    getattr(op, "drop_table")("t")', ["op передан дальше или переименован"]),
        ("    _helper(op)", ["op передан дальше или переименован"]),
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
        '    op.add_column("t", sa.Column("c", sa.Integer(), sa.ForeignKey("r.id"), nullable=True))',
        # То, что автогенерация пишет для SQLite.
        BATCH + 'batch.add_column(sa.Column("c", sa.Integer(), nullable=True))',
        BATCH + 'batch.create_index("ix_t_c", ["c"], unique=False)',
        '    op.create_table("t", sa.Column("id", sa.Integer(), primary_key=True))',
        '    op.create_index("ix_t_c", "t", ["c"])',
        '    op.create_index(op.f("ix_t_c"), "t", ["c"], unique=False)',
        '    columns = sa.inspect(op.get_bind()).get_columns("t")',
        '    bind = op.get_bind()\n    if bind.dialect.name == "sqlite":\n        return',
        '    rows = op.get_bind().execute(sa.text("SELECT id FROM t")).scalars().all()',
        '    count = op.get_bind().execute(sa.text("SELECT count(*) FROM t")).scalar()',
        # Значения параметров — не SQL.
        '    op.get_bind().execute(sa.text("SELECT id FROM t WHERE k = :k"), {"k": "DROP TABLE t"})',
        '    op.get_bind().execute(sa.text("WITH x AS (SELECT 1) SELECT * FROM x"))',
        "    op.get_bind().execute(sa.select(table.c.id))",
        '    """drop column — только слова в описании"""\n    op.create_index("ix", "t", ["c"])',
    ],
)
def test_known_safe_changes_need_no_note(upgrade: str) -> None:
    assert _kinds(_migration(upgrade)) == []


HAS_COLUMN = (
    "def _has_column(bind, table: str, column: str) -> bool:\n"
    "    return column in {item['name'] for item in sa.inspect(bind).get_columns(table)}\n"
)


def test_an_existence_check_in_a_local_helper_needs_no_note() -> None:
    """Запись, которой здесь написана половина миграций: «если ещё нет — добавить»."""
    source = _migration(
        '    if not _has_column(op.get_bind(), "t", "c"):\n'
        '        op.add_column("t", sa.Column("c", sa.Integer(), nullable=True))',
        header=HAS_COLUMN,
    )
    assert _kinds(source) == []


def test_the_bind_is_followed_into_a_local_helper() -> None:
    by_position = _migration(
        "    _wipe(op.get_bind())",
        header="def _wipe(connection) -> None:\n    metadata.drop_all(connection)\n",
    )
    assert _kinds(by_position) == ["соединение с базой передано в drop_all(…)"]
    through_two_helpers = _migration(
        "    bind = op.get_bind()\n    _outer(target=bind)",
        header=(
            "def _outer(*, target) -> None:\n    _inner(target)\n\n\n"
            'def _inner(connection) -> None:\n    connection.execute(sa.text("DELETE FROM t"))\n'
        ),
    )
    assert _kinds(through_two_helpers) == ["SQL: DELETE"]


def test_importing_application_code_needs_a_note() -> None:
    """Что сделает функция из `src`, из текста миграции не видно."""
    assert _kinds(_migration("    run()", header="from src.backfill import run")) == [
        "импорт из src"
    ]
    assert _kinds(_migration("    pass", header="import src.storage")) == ["импорт из src"]


def test_downgrade_and_its_helpers_are_not_examined() -> None:
    source = _migration(
        '    op.add_column("t", sa.Column("c", sa.Integer(), nullable=True))',
        downgrade="    _undo()",
        header='def _undo() -> None:\n    op.drop_column("t", "c")\n',
    )
    assert _kinds(source) == []


def test_a_helper_reached_from_upgrade_is_examined() -> None:
    helper = 'def _cleanup() -> None:\n    op.drop_table("t")\n'
    assert _kinds(_migration("    _cleanup()", header=helper)) == ["op.drop_table"]
    passed_around = _migration("    for step in (_cleanup,):\n        step()", header=helper)
    assert _kinds(passed_around) == ["op.drop_table"]
    listed_at_module_level = _migration(
        "    for step in STEPS:\n        step()", header=helper + "\nSTEPS = (_cleanup,)\n"
    )
    assert _kinds(listed_at_module_level) == ["op.drop_table"]


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
