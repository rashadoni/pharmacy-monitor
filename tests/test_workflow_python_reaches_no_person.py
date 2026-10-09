"""Python, который workflow запускает мимо CLI, не дотягивается до людей.

Журнал шага GitHub Actions у публичного репозитория открыт. Команду CLI в нём
держат три вещи: маска журнала, маска вывода (`src/output_mask.py`) и короткий
список команд, которые из workflow запускать можно
(`tests/test_log_carries_no_address.py`). Но чаще workflow запускает не команду,
а Python напрямую: блок `python - <<'PY'`, `python -c "…"`, файл из `scripts/`.
У такого кода нет ничего из трёх. structlog в нём не настроен и печатает поле
как есть, `print` и трассировка непойманной ошибки идут в журнал шага без
маски, а ошибка базы несёт в тексте параметры запроса.

Поставить туда маску одной общей точкой нельзя, и вот почему:

- половина блоков исполняется на сервере против выложенного кода, а не против
  чекаута. Блок, который первой строкой берёт маску из `src`, упал бы там, где
  маска ещё не выложена, — в том числе в еженедельном сборе pharmonline; а
  блок, который берёт её «если есть», защищён ровно до тех пор, пока не забыли;
- маска знает только «@». Имя человека, Telegram-идентификатор и токен входа
  она пропустит — а в ошибке базы на таблице пользователей лежат именно они.

Поэтому правило другое: такой код к людям не ходит вовсе. Нужно прочитать
получателей, отправить письмо или сообщение — это делает команда CLI: на ней
маска, и запускать её из workflow можно только по списку.

Что проверяется. Тест находит в тексте workflow каждый запуск Python мимо CLI,
читает запущенный код (блок или файл из репозитория) и идёт от него по именам
вглубь `src/`: импортированная функция, всё, что она зовёт, и так далее. Ни на
одном шаге этого пути не должно встретиться ничего о людях: модели с адресом
(`TenantUser`, `Recipient` — выводятся из `src/storage.py`, а не перечислены),
атрибута `email` или `telegram_chat_id`, отправки (`send_email`,
`send_telegram_message`), сырого SQL к таблицам людей, переменных окружения с
адресом. Списка разрешённых имён здесь нет намеренно: список закрепляет имя, а
не то, что за ним стоит, — функция, внесённая в него сегодня, завтра начнёт
слать письма, и список это пропустит; путь по именам перечитывается на каждом
прогоне CI.

Чего тест не видит — это растяжка, а не ограждение:

- путь идёт по именам, а не по значениям. Вызов через `getattr`, функцию,
  пришедшую аргументом, метод объекта, чей класс нигде не назван, он не
  проследит; сырой SQL узнаёт только по словам `from/join/update/into` перед
  именем таблицы;
- что модуль делает при импорте (код верхнего уровня), не читается: читаются
  только названные в нём функции, классы и переменные;
- миграции (`python -m alembic`), `pip` и `pytest` не читаются вовсе;
- Python, запущенный не словом `python`: консольный скрипт (`alembic`,
  `pytest`), файл со своим `#!`, запуск внутри shell-скрипта, который workflow
  зовёт;
- текст ошибки, в котором нет человека, но есть другое: имя хоста, обрывок
  запроса, логин прокси. Блок, читающий `runs.error_message`, печатает то, что в
  поле записано.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from tests.test_log_carries_no_address import cli_commands_run

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

# ─── Что значит «о людях» ────────────────────────────────────────────────────

# Колонка, по которой модель считается моделью людей, и она же — атрибут,
# которого коду из workflow касаться нельзя, у какого бы объекта он ни был.
_PERSONAL_COLUMNS = {"email", "telegram_chat_id"}
_PERSON_ATTRS = _PERSONAL_COLUMNS | {"emails"}
_SENDERS = {"send_email", "send_telegram_message"}
# Переменные окружения, в которых лежит адрес или идентификатор получателя.
_PEOPLE_ENV = {"EMAIL_TO", "TELEGRAM_CHAT_ID"}
# Модель людей можно назвать только вместе с колонкой, в которой человека нет:
# `select(storage.TenantUser.tenant_id)` читает номера тенантов, и ни в запросе,
# ни в его ошибке адресу взяться неоткуда. Сама модель (`select(TenantUser)`,
# `TenantUser(...)`, `session.get(TenantUser, …)`) — уже строка о человеке.
_COLUMNS_THAT_NAME_NOBODY = {"id", "tenant_id", "role", "is_active"}

# ─── Запуски Python в тексте workflow ────────────────────────────────────────

# Интерпретатор отдельным словом: `python`, `python3`, `.venv/bin/python`,
# `"$VENV/bin/python"`. `setup-python@v5`, `python-version:` и `f"python={…}"`
# — не запуск.
_INTERPRETER = re.compile(
    r"""(?<![\w.$/-])(?:[^\s"'`=;|&()<>]*/)?python[0-9.]*(?=["'}]*[ \t])["'}]*"""
)
_INTERPRETER_FLAGS = re.compile(r"-(?:[BbdEIiOPqsSuvx]+|OO|bb)$")
_INTERPRETER_OPTIONS_WITH_A_VALUE = {"-X", "-W"}
_NOT_A_RUN = {"-V", "--version", "-h", "--help"}
_HEREDOC = re.compile(r"""<<-?[ \t]*(['"]?)(\w+)\1""")
_END_OF_SHELL_COMMAND = re.compile(r"&&|\|\||[;|\n]")
# Строки, которые ничего не запускают: комментарий и имя шага.
_NOT_A_COMMAND_LINE = re.compile(r"^[ \t]*(?:#|-?[ \t]*name:).*$", re.M)
# Файл Python, названный в команде: путь от корня репозитория.
_REPO_PYTHON_FILE = re.compile(r"(?:^|/)((?:scripts|src|infra|migrations|tests)/[\w./-]+\.py)$")
# Модули, которые запускают как есть: своего кода о людях в них нет, а
# миграции этот тест не читает (см. описание файла).
_MODULES_NOT_READ = {"pip", "py_compile", "ensurepip", "venv", "alembic", "pytest"}
_CLI_MODULE = "src.main"

CODE, CLI, NOT_READ, UNKNOWN = "код", "CLI", "не читается", "не узнан"


@dataclass(frozen=True)
class PythonRun:
    line: int
    kind: str
    # Для `CODE` — исходный текст, для остальных — что запущено или почему не узнано.
    what: str
    # Откуда код: «блок» или путь файла в репозитории.
    origin: str = "блок"


def _unquoted(word: str) -> str:
    return word.strip("'\"`(){}[],")


_OPENING_QUOTE = re.compile(r"""[ \t]*(\\?["'])""")


def _closing_quote(text: str, start: int, quote: str) -> int:
    """Где кончается строка, открытая кавычкой `quote` (`"`, `'` или `\\"`)."""
    if len(quote) > 1 or quote == "'":  # экранированная кавычка и `'…'`: до такой же
        return text.find(quote, start)
    position = start
    while position < len(text):
        if text[position] == "\\":
            position += 2
            continue
        if text[position] == quote:
            return position
        position += 1
    return -1


def _repo_file(module_or_path: str) -> Path | None:
    found = _REPO_PYTHON_FILE.search(module_or_path)
    path = ROOT / found.group(1) if found else None
    return path if path is not None and path.is_file() else None


def python_runs(text: str) -> list[PythonRun]:
    """Запуски Python в тексте workflow: строка, вид и что запущено.

    Виды: `CODE` — код прочитан (блок `<<TAG`, `-c "…"`, файл из репозитория,
    модуль `-m src.x`); `CLI` — `-m src.main`, его проверяет
    `cli_commands_run`; `NOT_READ` — модуль из `_MODULES_NOT_READ`; `UNKNOWN` —
    запуск есть, а что запущено, из текста не узнать (`python "$SCRIPT"`,
    `python - < file`), и это тест роняет.

    Текст читается, а не исполняется. Тело уже найденного блока вторым запуском
    не считается: слово `python` в строке Python — не команда.
    """

    def blank(found: re.Match) -> str:
        return " " * len(found.group())  # той же длины: позиции не съезжают

    shell = re.sub(r"\\\r?\n", blank, _NOT_A_COMMAND_LINE.sub(blank, text))
    runs: list[PythonRun] = []
    read_until = 0
    for interpreter in _INTERPRETER.finditer(shell):
        if interpreter.start() < read_until:
            continue
        line = text.count("\n", 0, interpreter.start()) + 1
        end_of_line = shell.find("\n", interpreter.end())
        end_of_line = len(shell) if end_of_line < 0 else end_of_line
        rest = shell[interpreter.end() : end_of_line]
        words = _END_OF_SHELL_COMMAND.split(rest, maxsplit=1)[0].split()
        while words:
            if words[0] in _INTERPRETER_OPTIONS_WITH_A_VALUE:
                words = words[2:]
            elif _INTERPRETER_FLAGS.match(words[0]):
                words = words[1:]
            else:
                break
        if not words or words[0] in _NOT_A_RUN:
            continue
        first = words[0]
        if first == "-m":
            module = _unquoted(words[1]) if len(words) > 1 else ""
            if module == _CLI_MODULE:
                # Строка, на которой стоит `-m`: по ней запуск узнаёт проверка CLI.
                at = shell.index("-m", interpreter.end())
                runs.append(PythonRun(text.count("\n", 0, at) + 1, CLI, module))
            elif module.split(".")[0] in _MODULES_NOT_READ:
                runs.append(PythonRun(line, NOT_READ, module))
            elif (path := _repo_file(module.replace(".", "/") + ".py")) is not None:
                origin = str(path.relative_to(ROOT))
                runs.append(PythonRun(line, CODE, path.read_text(encoding="utf-8"), origin))
            else:
                runs.append(PythonRun(line, UNKNOWN, f"модуль `{module or '?'}`"))
        elif first.startswith("-c"):
            after_option = shell.index("-c", interpreter.end()) + 2
            # Кавычка бывает и экранированной: `ssh host "python -c \"…\""`.
            quoted = _OPENING_QUOTE.match(shell, after_option)
            closing = _closing_quote(shell, quoted.end(), quoted.group(1)) if quoted else -1
            if closing < 0:
                runs.append(PythonRun(line, UNKNOWN, "код после `-c` не в кавычках"))
                continue
            code = text[quoted.end() : closing]
            if quoted.group(1) == '"':  # в двойных кавычках shell снимает `\` сам
                code = re.sub(r'\\(["\\$`])', r"\1", code)
            runs.append(PythonRun(line, CODE, textwrap.dedent(code)))
            read_until = closing
        elif first == "-" or first.startswith("<<"):
            heredoc = _HEREDOC.search(rest)
            body_start = end_of_line + 1
            closing = (
                re.compile(rf"^[ \t]*{heredoc.group(2)}[ \t]*$", re.M).search(text, body_start)
                if heredoc
                else None
            )
            if closing is None:
                runs.append(PythonRun(line, UNKNOWN, "код со stdin, но не блоком `<<TAG`"))
                continue
            runs.append(PythonRun(line, CODE, textwrap.dedent(text[body_start : closing.start()])))
            read_until = closing.end()
        elif (path := _repo_file(_unquoted(first))) is not None:
            origin = str(path.relative_to(ROOT))
            runs.append(PythonRun(line, CODE, path.read_text(encoding="utf-8"), origin))
        else:
            runs.append(PythonRun(line, UNKNOWN, f"`{_unquoted(first)}`"))
    return runs


# ─── Путь от кода до людей по именам ─────────────────────────────────────────


class _Module:
    """Имена одного файла: что в нём определено и что он взял из `src`."""

    def __init__(self, name: str, tree: ast.Module, known: set[str]):
        self.name = name
        self.tree = tree
        self.defs: dict[str, ast.AST] = {}
        self.modules: dict[str, str] = {}  # локальное имя → модуль src
        self.names: dict[str, tuple[str, str]] = {}  # локальное имя → (модуль, имя)
        self.star_imports: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self.defs[node.name] = node
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.defs[target.id] = node
        # Импорты — со всего файла: в проекте их часто пишут внутри функций.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] != "src":
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        self.star_imports.append(node.module)
                        continue
                    full = f"{node.module}.{alias.name}"
                    if full in known:
                        self.modules[alias.asname or alias.name] = full
                    else:
                        self.names[alias.asname or alias.name] = (node.module, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] != "src":
                        continue
                    if alias.asname:
                        self.modules[alias.asname] = alias.name
                    else:  # `import src.storage` — имя `src`, дальше цепочка атрибутов
                        self.modules["src"] = "src"


def _dotted(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


class Sources:
    """Все файлы `src/` — или подставленные в тесте — и путь по ним."""

    def __init__(self, sources: dict[str, str]):
        trees = {name: ast.parse(text) for name, text in sources.items()}
        self._modules = {name: _Module(name, tree, set(trees)) for name, tree in trees.items()}
        self._known = set(trees)
        self.people_models: set[str] = set()
        people_tables: set[str] = set()
        storage = trees.get("src.storage")
        for node in storage.body if storage else []:
            if not isinstance(node, ast.ClassDef):
                continue
            columns = {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
            if not columns & _PERSONAL_COLUMNS:
                continue
            self.people_models.add(node.name)
            for item in node.body:
                if (
                    isinstance(item, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "__tablename__" for t in item.targets
                    )
                    and isinstance(item.value, ast.Constant)
                ):
                    people_tables.add(str(item.value.value))
        self._sql_on_people = (
            re.compile(
                r"\b(?:from|join|update|into)\s+[\"`]?(?:"
                + "|".join(sorted(map(re.escape, people_tables)))
                + r")\b",
                re.I,
            )
            if people_tables
            else None
        )

    def _names_a_people_model(self, node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in self.people_models) or (
            isinstance(node, ast.Attribute) and node.attr in self.people_models
        )

    def _marks(self, node: ast.AST) -> list[str]:
        """Что в этом куске кода названо о людях."""
        only_a_column = {
            id(sub.value)
            for sub in ast.walk(node)
            if isinstance(sub, ast.Attribute)
            and sub.attr in _COLUMNS_THAT_NAME_NOBODY
            and self._names_a_people_model(sub.value)
        }
        marks = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name | ast.Attribute):
                name = sub.id if isinstance(sub, ast.Name) else sub.attr
                if name in self.people_models and id(sub) not in only_a_column:
                    marks.append(f"модель `{name}`")
                elif name in _SENDERS:
                    marks.append(f"отправка `{name}`")
                elif isinstance(sub, ast.Attribute) and name in _PERSON_ATTRS:
                    marks.append(f"атрибут `.{name}`")
            elif isinstance(sub, ast.keyword) and sub.arg in _PERSON_ATTRS:
                marks.append(f"аргумент `{sub.arg}=`")
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if sub.value in _PEOPLE_ENV:
                    marks.append(f"переменная окружения `{sub.value}`")
                elif self._sql_on_people and (found := self._sql_on_people.search(sub.value)):
                    marks.append(f"SQL `{' '.join(found.group().split())}`")
        return sorted(set(marks))

    def _followed(self, node: ast.AST, module: _Module, *, own_defs: bool) -> list[tuple[str, str]]:
        """Имена из `src`, которых касается этот кусок кода."""
        followed = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                if own_defs and sub.id in module.defs:
                    followed.append((module.name, sub.id))
                elif sub.id in module.names:
                    followed.append(module.names[sub.id])
            elif isinstance(sub, ast.Attribute):
                parts = _dotted(sub)
                if not parts or parts[0] not in module.modules:
                    continue
                dotted = [*module.modules[parts[0]].split("."), *parts[1:]]
                # Самый длинный префикс, который есть модуль; имя — следующее слово.
                for cut in range(len(dotted) - 1, 0, -1):
                    if ".".join(dotted[:cut]) in self._known:
                        followed.append((".".join(dotted[:cut]), dotted[cut]))
                        break
        return followed

    def people_reached(self, code: str) -> list[str]:
        """Пути от кода до людей: «модель `TenantUser` ← notifications.f ← код».

        Пустой список — код и всё, до чего он дотягивается по именам, людей не
        касается. `SyntaxError` — код не разобран.
        """
        unit = _Module("", ast.parse(code), self._known)
        found = [f"{mark} ← код" for mark in self._marks(unit.tree)]
        found += [
            f"`from {module} import *` — что взято, не прочитать" for module in unit.star_imports
        ]
        came_from: dict[tuple[str, str], tuple[str, str] | None] = {}
        queue = [(key, None) for key in self._followed(unit.tree, unit, own_defs=False)]
        while queue:
            key, parent = queue.pop()
            if key in came_from:
                continue
            came_from[key] = parent
            module = self._modules.get(key[0])
            if module is None:
                continue
            node = module.defs.get(key[1])
            if node is None:  # не определено здесь: имя, которое модуль сам взял из src
                if key[1] in module.names:
                    queue.append((module.names[key[1]], key))
                continue
            path, step = [], key
            while step is not None:
                path.append(f"{step[0].removeprefix('src.')}.{step[1]}")
                step = came_from[step]
            found += [f"{mark} ← {' ← '.join(path)} ← код" for mark in self._marks(node)]
            queue += [(next_key, key) for next_key in self._followed(node, module, own_defs=True)]
        return sorted(set(found))

    def src_names_used(self, code: str) -> set[tuple[str, str]]:
        """Имена `src`, которых код касается сам, без пути вглубь."""
        unit = _Module("", ast.parse(code), self._known)
        return set(self._followed(unit.tree, unit, own_defs=False))


@cache
def _src() -> Sources:
    sources = {}
    for path in (ROOT / "src").rglob("*.py"):
        name = ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")
        sources[name] = path.read_text(encoding="utf-8")
    return Sources(sources)


@cache
def _runs_by_workflow() -> dict[str, list[PythonRun]]:
    return {
        path.name: python_runs(path.read_text(encoding="utf-8"))
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
    }


_HOW_TO_FIX_A_RUN = (
    "В workflow запускают Python, но что именно запущено, из текста не узнать: "
    "{found}. Код мимо CLI идёт в публичный журнал шага без маски, поэтому тест "
    "обязан его прочитать. Запускай блоком (`python - <<'PY'`), `python -c "
    '"…"`, файлом из репозитория (`python scripts/имя.py`) или командой CLI '
    "(`python -m src.main …`). Блок, который не разбирается как Python "
    "(подстановка shell ломает синтаксис), — открой кавычками `<<'PY'` и "
    "передай значения через окружение. Если слово `python` стоит в описании, а "
    "не в команде, — перефразируй (комментарии и `name:` шага тест пропускает сам)."
)
_HOW_TO_FIX_A_REACH = (
    "Python, который workflow запускает мимо CLI, дотягивается до людей: {found}. "
    "У такого кода нет ни маски журнала, ни маски вывода: поле журнала, `print` "
    "и трассировка ошибки базы с параметрами запроса идут в публичный журнал "
    "шага как есть — а там имя получателя, адрес и Telegram-идентификатор. Путь "
    "читается справа налево: «код» — блок или файл из workflow, слева — что "
    "нашлось и где. Что делать: получателей, письма, сообщения и всё, что читает "
    "`tenant_users` или `recipients`, вынеси в команду CLI — на ней маска, а "
    "запускать её из workflow можно только по списку `_RUN_FROM_A_WORKFLOW` в "
    "tests/test_log_carries_no_address.py. Если нужен только номер тенанта или "
    "роль — назови колонку явно (`storage.TenantUser.tenant_id`): колонки из "
    "`_COLUMNS_THAT_NAME_NOBODY` человека не называют. Если до людей дотянулась "
    "функция из `src/`, которую блок звал и раньше, — значит, она изменилась: "
    "блоку нужна другая функция или команда CLI. Путь идёт по именам: если "
    "находка — совпадение имён (локальная переменная названа как функция модуля), "
    "переименуй её."
)


def runs_not_read(runs_by_workflow: dict[str, list[PythonRun]]) -> list[str]:
    """Запуски, код которых тест прочитать не смог: не узнан или не разобран."""
    found = []
    for name, runs in runs_by_workflow.items():
        for run in runs:
            if run.kind == UNKNOWN:
                found.append(f"{name}:{run.line}: {run.what}")
            elif run.kind == CODE:
                try:
                    ast.parse(run.what)
                except SyntaxError as error:
                    found.append(f"{name}:{run.line}: {run.origin} не разобран ({error.msg})")
    return found


def people_reached_from(runs_by_workflow: dict[str, list[PythonRun]], src: Sources) -> list[str]:
    """Где запущенный из workflow код дотягивается до людей и каким путём."""
    found = []
    for name, runs in runs_by_workflow.items():
        for run in runs:
            if run.kind != CODE:
                continue
            try:
                reached = src.people_reached(run.what)
            except SyntaxError:
                continue  # об этом — `runs_not_read`
            found += [f"{name}:{run.line} ({run.origin}): {path}" for path in reached]
    return found


def test_every_python_run_in_a_workflow_is_read():
    found = runs_not_read(_runs_by_workflow())
    assert found == [], _HOW_TO_FIX_A_RUN.format(found="; ".join(found))


def test_python_run_from_a_workflow_reaches_no_person():
    found = people_reached_from(_runs_by_workflow(), _src())
    assert found == [], _HOW_TO_FIX_A_REACH.format(found="; ".join(found))


def test_the_check_is_not_blind():
    """Проверка, которая ничего не находит, потому что ничего не видит, зелёная."""
    # В workflow, ради которого всё это, — еженедельном сборе pharmonline, — она
    # видит блок, который берёт сессию базы из `src`.
    weekly = "autonomous-pharmonline-decodo-public-api.yml"
    used = {
        name
        for run in _runs_by_workflow().get(weekly, [])
        if run.kind == CODE
        for name in _src().src_names_used(run.what)
    }
    assert ("src.storage", "make_session") in used, (
        f"Проверка не видит в {weekly} блок, который берёт сессию базы. Если файл "
        "переименован — поправь имя здесь; если блок теперь запускают иначе — "
        "научи `python_runs` читать новую форму, иначе этот workflow никто не "
        "проверяет."
    )
    # Модели людей выведены из `src/storage.py`, а не перечислены.
    assert {"TenantUser", "Recipient"} <= _src().people_models, (
        "Из `src/storage.py` не выводятся модели людей. Если колонку с адресом "
        "переименовали — поправь `_PERSONAL_COLUMNS`."
    )
    # На настоящем коде путь до людей находится там, где он есть: рассылка о
    # прогоне читает получателей и шлёт письма, проверка здоровья — нет.
    assert _src().people_reached(
        "from src import notifications\nnotifications.dispatch_events_batch"
    )
    assert _src().people_reached("from src.main import health_check_cmd\nhealth_check_cmd()")
    assert _src().people_reached("from src.health import check_health\ncheck_health") == []


def test_a_cli_run_is_left_to_the_cli_check():
    """`python -m src.main …` здесь не читается: его проверяет список команд."""
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        seen_by_the_cli_check = {line for line, _ in cli_commands_run(text)}
        for run in python_runs(text):
            if run.kind == CLI:
                assert run.line in seen_by_the_cli_check, (
                    f"{path.name}:{run.line}: запуск `python -m src.main` не видит "
                    "`cli_commands_run` из tests/test_log_carries_no_address.py — "
                    "этот запуск не проверяет никто."
                )


# ─── Сам разбор: формы запуска ───────────────────────────────────────────────

_BLOCK = "from src import storage\nprint(storage.make_session)\n"


def _kinds(text: str) -> list[tuple[int, str]]:
    return [(run.line, run.kind) for run in python_runs(text)]


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        (f"python - <<'PY'\n{_BLOCK}PY\n", [(1, CODE)]),
        (f"  /opt/app/.venv/bin/python - <<PY\n{_BLOCK}PY\n", [(1, CODE)]),
        (f'  "$VENV/bin/python" - <<"PY"\n{_BLOCK}PY\n', [(1, CODE)]),
        (f"head=$(python3 - <<'PY'\n{_BLOCK}PY\n)\n", [(1, CODE)]),
        (f"python3 - <<'PY' >> \"$GITHUB_OUTPUT\"\n{_BLOCK}PY\n", [(1, CODE)]),
        (f"RUN_ID=\"$id\" timeout 85s .venv/bin/python -u - <<'PY'\n{_BLOCK}PY\n", [(1, CODE)]),
        (f"python <<'EOF'\n{_BLOCK}EOF\n", [(1, CODE)]),
        # Перенос строки через «\»: номер строки — где стоит интерпретатор.
        (f"python \\\n  - <<'PY'\n{_BLOCK}PY\n", [(1, CODE)]),
        # Слово `python` в теле блока — не второй запуск.
        ("python - <<'PY'\nprint('python - is here')\nPY\n", [(1, CODE)]),
        # Два блока подряд.
        (
            f"python - <<'PY'\n{_BLOCK}PY\necho ok\npython - <<'PY'\n{_BLOCK}PY\n",
            [(1, CODE), (6, CODE)],
        ),
        ('DATABASE_URL="$u" .venv/bin/python -c "\nfrom src import storage\n"\n', [(1, CODE)]),
        ("python -c 'import sys; print(sys.version)'", [(1, CODE)]),
        ("python -m src.main run --site aloe", [(1, CLI)]),
        ("/opt/app/.venv/bin/python -m alembic upgrade head", [(1, NOT_READ)]),
        ("python -m pip install --upgrade pip", [(1, NOT_READ)]),
        (".venv/bin/python -m py_compile src/api.py", [(1, NOT_READ)]),
        ("python -m src.digest", [(1, CODE)]),
        (
            '/opt/app/.venv/bin/python \\\n  "$dir/scripts/preflight_pharmonline_public_api.py" --x\n',
            [(1, CODE)],
        ),
        # Запуск есть, а что запущено — из текста не узнать.
        ('python "$SCRIPT"', [(1, UNKNOWN)]),
        ("python ${{ inputs.script }}", [(1, UNKNOWN)]),
        ("python scripts/no_such_file.py", [(1, UNKNOWN)]),
        ("python -m some.module", [(1, UNKNOWN)]),
        ("cat job.py | python -", [(1, UNKNOWN)]),
        ("python - < job.py", [(1, UNKNOWN)]),
        ("python -c $CODE", [(1, UNKNOWN)]),
        # Не запуск.
        ("- uses: actions/setup-python@v5\n  with:\n    python-version: '3.12'\n", []),
        ("test -x /opt/app/.venv/bin/python\n", []),
        ("python --version", []),
        ('PYTHONPATH="$dir" pharmacy-monitor run', []),
        ("  # руками: python - <<'PY'\n", []),
        ("      - name: Run python - check\n", []),
    ],
)
def test_the_check_finds_python_behind_every_form_of_run(text, kinds):
    assert _kinds(text) == kinds


def test_the_block_is_read_as_it_was_written():
    quoted = python_runs("    python - <<'PY'\n    import os\n    print(os.name)\n    PY\n")
    assert quoted[0].what == "import os\nprint(os.name)\n"
    inside_ssh = python_runs('ssh host "python -c \\"print(1)\\""')
    assert [(run.kind, run.what) for run in inside_ssh] == [(CODE, "print(1)")]
    escaped = python_runs('python -c "\nprint(\\"pharmonline\\")\n"')
    assert escaped[0].what == '\nprint("pharmonline")\n'
    script = python_runs("python scripts/preflight_pharmonline_public_api.py")
    assert script[0].origin == "scripts/preflight_pharmonline_public_api.py"
    assert "def " in script[0].what


# ─── Сам разбор: путь до людей ───────────────────────────────────────────────

_FAKE_SRC = {
    "src": "",
    "src.storage": textwrap.dedent(
        """
        class Run:
            __tablename__ = "runs"
            id: int
            requested_by: int = ForeignKey("tenant_users.id")

        class TenantUser:
            __tablename__ = "tenant_users"
            id: int
            tenant_id: int
            email: str

        class Recipient:
            __tablename__ = "recipients"
            telegram_chat_id: str

        def make_session():
            return Run
        """
    ),
    "src.notifier": "def send_email(subject, html):\n    pass\n",
    "src.health": textwrap.dedent(
        """
        from src import storage

        def check_health(session):
            return _latest(session)

        def _latest(session):
            return session.query(storage.Run)

        def alert(session):
            from src.notifier import send_email
            send_email("x", "y")

        def late(session, who):
            from src.tenants import find
            return find(session, who)

        HANDLERS = {"alert": alert}

        class Report:
            def send(self):
                alert(None)
        """
    ),
    "src.tenants": textwrap.dedent(
        """
        from src.storage import TenantUser

        def count_tenants(session):
            return session.query(TenantUser.tenant_id).distinct().count()

        def find(session, who):
            return session.query(TenantUser).filter_by(email=who).one()

        def raw(session):
            return session.execute(text("SELECT id FROM  tenant_users"))
        """
    ),
    "src.reexport": "from src.health import alert\nfrom src.tenants import count_tenants\n",
    "src.scrapers.base": "import os\n\ndef proxy():\n    return os.environ['PROXY']\n",
}


@pytest.mark.parametrize(
    ("code", "reached"),
    [
        # Не о людях.
        ("from src import storage\nstorage.make_session()", []),
        ("from src.health import check_health\ncheck_health(None)", []),
        ("import src.scrapers.base\nsrc.scrapers.base.proxy()", []),
        ("from src import storage\nselect(storage.Run.id)", []),
        ("import os\nprint(os.environ['HOME'])", []),
        # Модель людей — только колонкой, в которой человека нет.
        ("from src import storage\nselect(storage.TenantUser.tenant_id).distinct()", []),
        ("from src.tenants import count_tenants\ncount_tenants(s)", []),
        ("from src.reexport import count_tenants\ncount_tenants(s)", []),
        # В самом блоке.
        ("from src import storage\nselect(storage.TenantUser)", ["модель `TenantUser` ← код"]),
        ("from src.storage import Recipient\nRecipient()", ["модель `Recipient` ← код"]),
        (
            "from src import storage\nselect(storage.TenantUser.email)",
            ["атрибут `.email` ← код", "модель `TenantUser` ← код"],
        ),
        ("print(user.telegram_chat_id)", ["атрибут `.telegram_chat_id` ← код"]),
        ("add(email='x')", ["аргумент `email=` ← код"]),
        ("import os\nos.environ['EMAIL_TO']", ["переменная окружения `EMAIL_TO` ← код"]),
        ("text('select 1 from\\n tenant_users')", ["SQL `from tenant_users` ← код"]),
        ("text('UPDATE recipients SET is_active = false')", ["SQL `UPDATE recipients` ← код"]),
        ("from src.notifier import *", ["`from src.notifier import *` — что взято, не прочитать"]),
        # Вглубь `src`: функция, переменная со ссылкой на неё, класс, реэкспорт.
        (
            "from src.health import alert\nalert(s)",
            ["отправка `send_email` ← health.alert ← код"],
        ),
        (
            "from src import health\nhealth.HANDLERS['alert'](s)",
            ["отправка `send_email` ← health.alert ← health.HANDLERS ← код"],
        ),
        (
            "from src.health import Report\nReport().send()",
            ["отправка `send_email` ← health.alert ← health.Report ← код"],
        ),
        (
            "from src.reexport import alert\nalert(s)",
            ["отправка `send_email` ← health.alert ← reexport.alert ← код"],
        ),
        (
            "import src.health as h\nh.alert(s)",
            ["отправка `send_email` ← health.alert ← код"],
        ),
        (
            "import src.tenants\nsrc.tenants.find(s, w)",
            ["аргумент `email=` ← tenants.find ← код", "модель `TenantUser` ← tenants.find ← код"],
        ),
        (
            "from src.tenants import raw\nraw(s)",
            ["SQL `FROM tenant_users` ← tenants.raw ← код"],
        ),
        # Импорт внутри функции: в проекте так пишут часто.
        (
            "from src.health import late\nlate(s, w)",
            [
                "аргумент `email=` ← tenants.find ← health.late ← код",
                "модель `TenantUser` ← tenants.find ← health.late ← код",
            ],
        ),
        # Внешний ключ на таблицу людей — не запрос к ней.
        ("from src.storage import Run\nRun.requested_by", []),
    ],
)
def test_the_check_follows_names_down_to_people(code, reached):
    assert Sources(_FAKE_SRC).people_reached(code) == reached


def test_a_run_that_cannot_be_read_is_reported_with_its_place():
    text = "python \"$SCRIPT\"\npython - <<'PY'\ndef (\nPY\npython - <<'PY'\nprint(1)\nPY\n"
    found = runs_not_read({"a.yml": python_runs(text)})
    assert [line.split(" (")[0] for line in found] == [
        "a.yml:1: `$SCRIPT`",
        "a.yml:2: блок не разобран",
    ]


def test_what_a_run_reaches_is_reported_with_its_place():
    text = (
        "echo start\n"
        "python - <<'PY'\nfrom src.health import alert\nalert(1)\nPY\n"
        "python - <<'PY'\nfrom src.health import check_health\ncheck_health(1)\nPY\n"
        "python - <<'PY'\ndef (\nPY\n"
        "python -m alembic upgrade head\n"
    )
    found = people_reached_from({"a.yml": python_runs(text)}, Sources(_FAKE_SRC))
    assert found == ["a.yml:2 (блок): отправка `send_email` ← health.alert ← код"]
