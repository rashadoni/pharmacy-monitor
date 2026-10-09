"""Python, который workflow запускает мимо CLI, не дотягивается до людей.

Журнал шага GitHub Actions у публичного репозитория открыт. Команду CLI в нём
держат три вещи: маска журнала, маска вывода (`src/output_mask.py`) и короткий
список команд, которые из workflow запускать можно
(`tests/test_log_carries_no_address.py`). Но чаще workflow запускает не команду,
а Python напрямую: блок `python - <<'PY'`, `python -c "…"`, файл из `scripts/`.
У такого кода нет ничего из трёх. structlog в нём не настроен и печатает поле
как есть, `print` и трассировка непойманной ошибки идут в журнал шага без
маски, а ошибка базы несёт в тексте параметры запроса.

Поставить маску внутрь блока одной общей точкой нельзя:

- большая часть блоков исполняется на сервере, и примерно половина из них —
  против выложенного кода, а не чекаута. Блок, который первой строкой берёт
  маску из `src`, упал бы там, где маска ещё не выложена, — в том числе в
  еженедельном сборе pharmonline; а блок, который берёт её «если есть», защищён
  ровно до тех пор, пока не забыли;
- маска знает только «@». Имя человека, Telegram-идентификатор и токен входа
  она пропустит — а в ошибке базы на таблице пользователей лежат именно они.

Фильтр на стороне раннера (вывод `ssh` через маску из чекаута) от выкладки не
зависел бы, но это правка каждого workflow, и знает он то же «@». Здесь он не
сделан.

Поэтому правило другое: такой код к людям не ходит вовсе. Нужно прочитать
получателей, отправить письмо или сообщение — это делает команда CLI: на ней
маска, и запускать её из workflow можно только по списку.

Что проверяется. Тест находит в тексте workflow каждый запуск Python мимо CLI,
читает запущенный код (блок или файл из репозитория) и идёт от него по именам
вглубь `src/` и `scripts/`: импортированная функция, всё, что она зовёт, и так
далее. Ни на одном шаге этого пути не должно встретиться ничего о людях: модели
с адресом (`TenantUser`, `Recipient` — выводятся из `src/storage.py`, а не
перечислены), атрибута `email` или `telegram_chat_id`, отправки (`send_email`,
`send_telegram_message`), SQL к таблицам людей, переменных окружения с адресом.
В самом запущенном коде нельзя ещё запускать процессы и импортировать по имени
(`subprocess`, `os.system`, `importlib`, `exec`): так зовут CLI мимо обеих
проверок. Списка разрешённых функций здесь нет намеренно: список закрепляет
имя, а не то, что за ним стоит, — функция, внесённая в него сегодня, завтра
начнёт слать письма, и список это пропустит; путь по именам перечитывается на
каждом прогоне CI.

Чего тест не видит — это растяжка, а не ограждение:

- путь идёт по именам, а не по значениям. Вызов через `getattr`, функцию,
  пришедшую аргументом, метод, навешенный на класс после его объявления, он не
  проследит; относительные импорты не читает (в `src/` их нет); SQL узнаёт по
  имени таблицы в строке, где есть слово запроса, — имя таблицы в переменной
  или `Table("recipients", …)` внутри `src/` не узнает;
- что модуль делает при импорте (код верхнего уровня), не читается: читаются
  только названные в нём функции, классы и переменные;
- читается `src/` чекаута, а блоки на сервере исполняют выложенный код: между
  мержем и выкладкой это разный код;
- миграции (`python -m alembic`), `pip` и `pytest` не читаются вовсе;
- Python, запущенный не словом `python`: интерпретатор в переменной
  (`"$PY" - <<'EOF'`), консольный скрипт (`alembic`, `uv run`), файл со своим
  `#!`, запуск внутри shell-скрипта, который workflow зовёт;
- текст ошибки, в котором нет человека, но есть другое: имя хоста, обрывок
  запроса, логин прокси.

Строки, в которых `python` — не запуск (`command -v python3`, `apt-get install
python3`, `ls …/python`), тест узнаёт по команде перед словом; незнакомую форму
он считает запуском и роняет CI — лучше так, чем пропустить.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

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
# Слово запроса: строка с ним и с именем таблицы людей — SQL к этой таблице.
_SQL_WORD = re.compile(r"\b(?:select|insert|update|delete|truncate|copy|from|join)\b", re.I)
# В самом запущенном коде: импорт по имени тест не прочитает, а запуск процесса —
# только если запускают Python (`pg_dump` из блока — не о людях).
_MODULES_THAT_IMPORT_BY_NAME = {"runpy", "importlib"}
_NAMES_THAT_IMPORT_BY_NAME = {"__import__", "exec", "eval"}
_MODULES_THAT_RUN_A_PROCESS = {"subprocess", "multiprocessing", "pty"}
_AN_INTERPRETER = re.compile(r"(?:^|[\s/])python[0-9.]*(?:\s|$)")
_CLI_MODULE = "src.main"

# ─── Запуски Python в тексте workflow ────────────────────────────────────────

# Интерпретатор отдельным словом: `python`, `python3`, `.venv/bin/python`,
# `"$VENV/bin/python"`, `${PYTHON:-python3}`. `setup-python@v5`,
# `python-version:` и `f"python={…}"` — не запуск.
_INTERPRETER = re.compile(
    r"""(?:(?<![\w.$/-])|(?<=:-))(?:[^\s"'`=;|&()<>]*/)?python[0-9.]*(?=["'}]*(?:[ \t<|;)&]|$))["'}]*""",
    re.M,
)
_INTERPRETER_FLAGS = re.compile(r"-(?:[BbdEIiOPqsSuvx]+|OO|bb)$")
_INTERPRETER_OPTIONS_WITH_A_VALUE = {"-X", "-W"}
_NOT_A_RUN = {"-V", "-VV", "--version", "-h", "--help"}
_HEREDOC = re.compile(r"""<<-?[ \t]*(['"]?)(\w+)\1""")
_OPENING_QUOTE = re.compile(r"""[ \t]*(\\?["'])""")
_REDIRECTION = re.compile(r"\d*[<>&]")
_END_OF_SHELL_COMMAND = re.compile(r"&&|\|\||[;|\n]")
# Где начинается команда, в которой стоит слово `python`.
_START_OF_SHELL_COMMAND = re.compile(r"&&|\|\||\$\(|[;|\n(`]")
# Команды, которым `python` — аргумент, а не программа.
_COMMANDS_THAT_ONLY_NAME_PYTHON = {
    "command",
    "which",
    "type",
    "test",
    "[",
    "[[",
    "ls",
    "ln",
    "install",
    "apt-get",
    "apt",
    "dnf",
    "yum",
    "apk",
    "brew",
    "echo",
    "printf",
}
# Шаг, тело которого — Python: `shell: python`. Тело такого шага тест не читает.
_PYTHON_SHELL = re.compile(r"""^[ \t]*(?:-[ \t]+)?shell:[ \t]*["']?python""", re.M)
# Строки, которые ничего не запускают: комментарий и имя шага.
_NOT_A_COMMAND_LINE = re.compile(r"^[ \t]*(?:#|-?[ \t]*name:).*$", re.M)
# Модули, которые запускают как есть: своего кода о людях в них нет, а
# миграции этот тест не читает (см. описание файла).
_MODULES_NOT_READ = {
    "pip",
    "py_compile",
    "compileall",
    "ensurepip",
    "venv",
    "json",
    "alembic",
    "pytest",
    "playwright",
}

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


def _repo_file(word: str) -> Path | None:
    """Файл Python в репозитории, на который указывает слово команды.

    Каталог перед путём в команде бывает любым (`"$runtime_dir/scripts/x.py"`),
    поэтому берётся самый длинный хвост пути, который есть в репозитории.
    """
    parts = [part for part in word.split("/") if part]
    if ".." in parts or not word.endswith(".py"):
        return None
    for start in range(len(parts)):
        candidate = ROOT.joinpath(*parts[start:])
        if candidate.is_file():
            return candidate
    return None


def python_runs(text: str) -> list[PythonRun]:
    """Запуски Python в тексте workflow: строка, вид и что запущено.

    Виды: `CODE` — код прочитан (блок `<<TAG`, `-c "…"`, файл из репозитория,
    модуль `-m src.x`); `CLI` — `-m src.main` и `src/main.py`, их проверяет
    `cli_commands_run`; `NOT_READ` — модуль из `_MODULES_NOT_READ`; `UNKNOWN` —
    запуск есть, а что запущено, из текста не узнать (`python "$SCRIPT"`,
    `… | python3`, шаг с `shell: python`), и это тест роняет.

    Текст читается, а не исполняется. Тело уже найденного блока вторым запуском
    не считается: слово `python` в строке Python — не команда.
    """

    def blank(found: re.Match) -> str:
        return " " * len(found.group())  # той же длины: позиции не съезжают

    def line_of(position: int) -> int:
        return text.count("\n", 0, position) + 1

    shell = re.sub(r"\\\r?\n", blank, _NOT_A_COMMAND_LINE.sub(blank, text))
    runs, already_read = [], []
    for found in _PYTHON_SHELL.finditer(shell):
        runs.append(PythonRun(line_of(found.start()), UNKNOWN, "шаг с `shell: python`"))
        end_of_line = shell.find("\n", found.end())
        already_read.append((found.start(), len(shell) if end_of_line < 0 else end_of_line))
    for interpreter in _INTERPRETER.finditer(shell):
        if any(start <= interpreter.start() < end for start, end in already_read):
            continue
        line = line_of(interpreter.start())
        end_of_line = shell.find("\n", interpreter.end())
        end_of_line = len(shell) if end_of_line < 0 else end_of_line
        rest = shell[interpreter.end() : end_of_line]

        # Команда, в которой стоит слово: что перед ним и чем она отделена.
        before = shell[: interpreter.start()]
        separators = list(_START_OF_SHELL_COMMAND.finditer(before))
        separator = separators[-1].group() if separators else ""
        command_before = before[separators[-1].end() :] if separators else before
        if {_unquoted(word) for word in command_before.split()} & _COMMANDS_THAT_ONLY_NAME_PYTHON:
            continue

        words = [
            word
            for word in _END_OF_SHELL_COMMAND.split(rest, maxsplit=1)[0].split()
            if _unquoted(word) or word.startswith("<<")
        ]
        while words:
            if words[0] in _INTERPRETER_OPTIONS_WITH_A_VALUE:
                words = words[2:]
            elif _INTERPRETER_FLAGS.match(words[0]):
                words = words[1:]
            else:
                break
        if words and words[0] in _NOT_A_RUN:
            continue
        if not words or (_REDIRECTION.match(words[0]) and not words[0].startswith("<<")):
            # Программы в команде нет. После `|` интерпретатор читает её со stdin.
            if separator == "|":
                runs.append(PythonRun(line, UNKNOWN, "код со stdin, но не блоком `<<TAG`"))
            continue
        first = words[0]
        if first == "-m":
            module = _unquoted(words[1]) if len(words) > 1 else ""
            if module == _CLI_MODULE:
                # Строка, на которой стоит `-m`: по ней запуск узнаёт проверка CLI.
                runs.append(PythonRun(line_of(shell.index("-m", interpreter.end())), CLI, module))
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
            already_read.append((quoted.end(), closing))
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
            # Только тело: на строке с `<<TAG` может стоять и второй запуск.
            already_read.append((body_start, closing.end()))
        elif (path := _repo_file(_unquoted(first))) is not None:
            origin = str(path.relative_to(ROOT))
            if origin == "src/main.py":
                runs.append(PythonRun(line_of(shell.index(first, interpreter.end())), CLI, origin))
            else:
                runs.append(PythonRun(line, CODE, path.read_text(encoding="utf-8"), origin))
        else:
            runs.append(PythonRun(line, UNKNOWN, f"`{_unquoted(first)}`"))
    return sorted(runs, key=lambda run: run.line)


# ─── Путь от кода до людей по именам ─────────────────────────────────────────


def _top_level(statements: list[ast.stmt]):
    """Команды верхнего уровня файла, включая те, что стоят под `if` и `try`."""
    for node in statements:
        yield node
        if isinstance(node, ast.If | ast.Try | ast.With | ast.For | ast.While):
            for field in ("body", "orelse", "finalbody"):
                yield from _top_level(getattr(node, field, []))
            for handler in getattr(node, "handlers", []):
                yield from _top_level(handler.body)


def _dotted(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


class _Module:
    """Имена одного файла: что в нём определено и что он взял из репозитория."""

    def __init__(self, name: str, tree: ast.Module, known: set[str]):
        roots = {module.split(".")[0] for module in known}
        self.name = name
        self.tree = tree
        self.defs: dict[str, ast.AST] = {}
        self.modules: dict[str, str] = {}  # локальное имя → модуль репозитория
        self.names: dict[str, tuple[str, str]] = {}  # локальное имя → (модуль, имя)
        self.star_imports: list[str] = []
        # Имя → функции, навешенные на него декоратором: `@cli.command()`,
        # `@app.get(…)`. Вызов `cli([...])` доходит до них без единого имени.
        self.registered: dict[str, list[str]] = {}
        for node in _top_level(tree.body):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self.defs[node.name] = node
                for decorator in node.decorator_list:
                    target = decorator.func if isinstance(decorator, ast.Call) else decorator
                    parts = _dotted(target)
                    if parts and len(parts) > 1:
                        self.registered.setdefault(parts[0], []).append(node.name)
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name):
                            self.defs[name.id] = node
        # Импорты — со всего файла: в проекте их часто пишут внутри функций.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] not in roots:
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
                    root = alias.name.split(".")[0]
                    if root not in roots:
                        continue
                    if alias.asname:
                        self.modules[alias.asname] = alias.name
                    else:  # `import src.storage` — имя `src`, дальше цепочка атрибутов
                        self.modules[root] = root


class Sources:
    """Файлы `src/` и `scripts/` — или подставленные в тесте — и путь по ним."""

    def __init__(self, sources: dict[str, str]):
        trees = {name: ast.parse(text) for name, text in sources.items()}
        self.modules = set(trees)
        self._known = self.modules | {name.split(".")[0] for name in trees}
        self._modules = {name: _Module(name, tree, self._known) for name, tree in trees.items()}
        self.people_models: set[str] = set()
        self.people_tables: set[str] = set()
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
                    self.people_tables.add(str(item.value.value))
        self._people_table = (
            re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, self.people_tables))) + r")\b")
            if self.people_tables
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
                elif (
                    self._people_table
                    and _SQL_WORD.search(sub.value)
                    and (table := self._people_table.search(sub.value))
                ):
                    marks.append(f"SQL к таблице `{table.group()}`")
        return sorted(set(marks))

    def _marks_of_the_run_itself(self, tree: ast.Module) -> list[str]:
        """Чего нельзя в самом запущенном коде, а в `src/` можно: импортировать
        по имени, запускать из кода Python (так зовут CLI мимо обеих проверок)
        и называть таблицу людей строкой."""
        marks, runs_a_process, names_an_interpreter = [], [], False
        for sub in ast.walk(tree):
            if isinstance(sub, ast.Import | ast.ImportFrom):
                modules = (
                    [alias.name for alias in sub.names]
                    if isinstance(sub, ast.Import)
                    else [sub.module or ""]
                )
                for module in modules:
                    if module.split(".")[0] in _MODULES_THAT_IMPORT_BY_NAME:
                        marks.append(f"`{module}` — импорт по имени не прочитать")
                    elif module.split(".")[0] in _MODULES_THAT_RUN_A_PROCESS:
                        runs_a_process.append(module)
            elif isinstance(sub, ast.Name) and sub.id in _NAMES_THAT_IMPORT_BY_NAME:
                marks.append(f"`{sub.id}` — импорт по имени не прочитать")
            elif isinstance(sub, ast.Attribute):
                parts = _dotted(sub)
                if parts == ["sys", "executable"]:
                    names_an_interpreter = True
                elif (
                    parts
                    and parts[0] == "os"
                    and (sub.attr in {"system", "popen"} or sub.attr.startswith(("exec", "spawn")))
                ):
                    runs_a_process.append(f"os.{sub.attr}")
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if sub.value == _CLI_MODULE:
                    marks.append(f"строка `{_CLI_MODULE}` — CLI зовут командой, а не из кода")
                elif sub.value in self.people_tables:
                    marks.append(f"таблица `{sub.value}`")
                elif _AN_INTERPRETER.search(sub.value):
                    names_an_interpreter = True
        if names_an_interpreter:
            marks += [
                f"`{how}` запускает Python — что именно, не прочитать" for how in runs_a_process
            ]
        return sorted(set(marks))

    def _resolve(self, module: str, words: list[str]) -> tuple[str, str] | None:
        """На какое имя указывает цепочка слов, начатая в модуле.

        Идёт сквозь модули: подмодуль пакета (`src.scrapers.base`) и модуль,
        который этот модуль сам взял из репозитория (`main.notifier` — тот же
        `src.notifier`). `None` — цепочка кончилась на модуле.
        """
        for word in words:
            inside = self._modules.get(module)
            if f"{module}.{word}" in self._known:
                module = f"{module}.{word}"
            elif inside is not None and word not in inside.defs and word in inside.modules:
                module = inside.modules[word]
            else:
                return (module, word)
        return None

    def _followed(self, node: ast.AST, module: _Module, *, own_defs: bool) -> list[tuple[str, str]]:
        """Имена из репозитория, которых касается этот кусок кода."""
        # Декоратор-регистрация (`@cli.command()`) — не то, что функция зовёт:
        # иначе любая команда CLI тянула бы за собой все остальные.
        registration = {
            id(inner)
            for decorator in getattr(node, "decorator_list", [])
            if len(_dotted(decorator.func if isinstance(decorator, ast.Call) else decorator) or [])
            > 1
            for inner in ast.walk(decorator)
        }
        followed = []
        for sub in ast.walk(node):
            target = None
            if id(sub) in registration:
                continue
            if isinstance(sub, ast.Name):
                if own_defs and sub.id in module.defs:
                    target = (module.name, sub.id)
                elif sub.id in module.names:
                    origin, name = module.names[sub.id]
                    target = self._resolve(origin, [name])
            elif isinstance(sub, ast.Attribute):
                parts = _dotted(sub)
                if parts and parts[0] in module.modules:
                    target = self._resolve(module.modules[parts[0]], parts[1:])
                elif parts and parts[0] in module.names:
                    origin, name = module.names[parts[0]]
                    target = self._resolve(origin, [name, *parts[1:]])
            if target is not None:
                followed.append(target)
        return followed

    def people_reached(self, code: str) -> list[str]:
        """Пути от кода до людей: «модель `TenantUser` ← notifications.f ← код».

        Пустой список — код и всё, до чего он дотягивается по именам, людей не
        касается. `SyntaxError` — код не разобран.
        """
        unit = _Module("", ast.parse(code), self._known)
        found = [f"{mark} ← код" for mark in self._marks(unit.tree)]
        found += [f"{mark} ← код" for mark in self._marks_of_the_run_itself(unit.tree)]
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
            # Функции, навешенные на это имя декоратором, достижимы из него.
            queue += [((module.name, name), key) for name in module.registered.get(key[1], [])]
            node = module.defs.get(key[1])
            if node is None:  # не определено здесь: имя, которое модуль сам взял из репозитория
                if key[1] in module.names:
                    origin, name = module.names[key[1]]
                    if (target := self._resolve(origin, [name])) is not None:
                        queue.append((target, key))
                continue
            path, step = [], key
            while step is not None:
                path.append(f"{step[0].removeprefix('src.')}.{step[1]}")
                step = came_from[step]
            found += [f"{mark} ← {' ← '.join(path)} ← код" for mark in self._marks(node)]
            queue += [(next_key, key) for next_key in self._followed(node, module, own_defs=True)]
        return sorted(set(found))

    def names_used(self, code: str) -> set[tuple[str, str]]:
        """Имена репозитория, которых код касается сам, без пути вглубь."""
        unit = _Module("", ast.parse(code), self._known)
        return set(self._followed(unit.tree, unit, own_defs=False))


@cache
def _src() -> Sources:
    sources = {}
    for package in ("src", "scripts"):
        for path in (ROOT / package).rglob("*.py"):
            parts = path.relative_to(ROOT).with_suffix("").parts
            sources[".".join(parts).removesuffix(".__init__")] = path.read_text(encoding="utf-8")
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
    "(`python -m src.main …`); шаг с `shell: python` и код, пришедший по `|`, "
    "перепиши блоком. Блок, который не разбирается как Python, — это либо "
    "подстановка shell, ломающая синтаксис (открой кавычками `<<'PY'` и передай "
    "значения через окружение), либо синтаксис новее Python 3.11, на котором "
    "идёт CI. Если `python` здесь не запускают — это аргумент другой команды "
    "или слово в описании, — тест узнаёт такое по команде перед словом "
    "(`_COMMANDS_THAT_ONLY_NAME_PYTHON`) и по модулю после `-m` "
    "(`_MODULES_NOT_READ`): допиши туда свою, если она ничего не исполняет, "
    "или перефразируй (комментарии и `name:` шага тест пропускает сам)."
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
    "tests/test_log_carries_no_address.py; звать CLI из кода (`cli([...])`, "
    "`subprocess`) нельзя по той же причине. Если нужен только номер тенанта или "
    "роль — назови колонку явно (`storage.TenantUser.tenant_id`): колонки из "
    "`_COLUMNS_THAT_NAME_NOBODY` человека не называют. Если до людей дотянулась "
    "функция из `src/`, которую блок звал и раньше, — значит, она изменилась: "
    "блоку нужна другая функция или команда CLI. Путь идёт по именам: если "
    "находка — совпадение имён (локальная переменная названа как функция модуля, "
    "атрибут `.email` у объекта не о человеке), переименуй."
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
        for name in _src().names_used(run.what)
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
    assert {"tenant_users", "recipients"} <= _src().people_tables
    # Читаются оба каталога, из которых workflow запускает код.
    assert {"src.storage", "scripts.preflight_pharmonline_public_api"} <= _src().modules
    # На настоящем коде путь до людей находится там, где он есть: рассылка о
    # прогоне читает получателей и шлёт письма; CLI — вся — тоже: команды
    # навешены на `cli` декоратором, имени у этого пути нет.
    assert _src().people_reached(
        "from src import notifications\nnotifications.dispatch_events_batch"
    )
    assert _src().people_reached("from src import main\nmain.notifier.send_email")
    assert _src().people_reached("from src.main import cli\ncli(['recipient', 'list'])")


def test_a_cli_run_is_left_to_the_cli_check():
    """`python -m src.main …` здесь не читается: его проверяет список команд."""
    # Импорт здесь, а не вверху файла: тот файл правят несколько открытых PR, и
    # потерянное при слиянии имя должно ронять этот тест, а не весь файл.
    from tests.test_log_carries_no_address import cli_commands_run

    seen = 0
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        seen_by_the_cli_check = {line for line, _ in cli_commands_run(text)}
        for run in python_runs(text):
            if run.kind == CLI:
                seen += 1
                assert run.line in seen_by_the_cli_check, (
                    f"{path.name}:{run.line}: запуск `python -m src.main` не видит "
                    "`cli_commands_run` из tests/test_log_carries_no_address.py — "
                    "этот запуск не проверяет никто."
                )
    assert seen, "Ни одного запуска CLI через `python` в workflow не найдено: разбор ослеп."


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
        (f"sudo -u pm env X=1 python - <<'PY'\n{_BLOCK}PY\n", [(1, CODE)]),
        (f"python <<'EOF'\n{_BLOCK}EOF\n", [(1, CODE)]),
        (f"python3<<'PY'\n{_BLOCK}PY\n", [(1, CODE)]),
        (f"${{PYTHON:-python3}} - <<'PY'\n{_BLOCK}PY\n", [(1, CODE)]),
        # Перенос строки через «\»: номер строки — где стоит интерпретатор.
        (f"python \\\n  - <<'PY'\n{_BLOCK}PY\n", [(1, CODE)]),
        # Слово `python` в теле блока — не второй запуск.
        ("python - <<'PY'\nprint('python - is here')\nPY\n", [(1, CODE)]),
        # Два блока подряд.
        (
            f"python - <<'PY'\n{_BLOCK}PY\necho ok\npython - <<'PY'\n{_BLOCK}PY\n",
            [(1, CODE), (6, CODE)],
        ),
        # Второй запуск на строке, где открыт блок.
        (f"python - <<'PY' && python \"$SCRIPT\"\n{_BLOCK}PY\n", [(1, CODE), (1, UNKNOWN)]),
        (f"python - <<'PY' | python -c 'import sys'\n{_BLOCK}PY\n", [(1, CODE), (1, CODE)]),
        ('DATABASE_URL="$u" .venv/bin/python -c "\nfrom src import storage\n"\n', [(1, CODE)]),
        ("python -c 'import sys; print(sys.version)'", [(1, CODE)]),
        ("python -m src.main run --site aloe", [(1, CLI)]),
        ("python \\\n  -m src.main run --site aloe", [(2, CLI)]),
        ("/opt/app/.venv/bin/python src/main.py run --site aloe", [(1, CLI)]),
        ("/opt/app/.venv/bin/python -m alembic upgrade head", [(1, NOT_READ)]),
        ("python -m pip install --upgrade pip", [(1, NOT_READ)]),
        (".venv/bin/python -m py_compile src/api.py", [(1, NOT_READ)]),
        ("curl -s https://x.example | python -m json.tool", [(1, NOT_READ)]),
        ("python -m playwright install chromium", [(1, NOT_READ)]),
        ("python -m src.digest", [(1, CODE)]),
        ("python migrations/env.py", [(1, CODE)]),
        (
            '/opt/app/.venv/bin/python \\\n  "$dir/scripts/preflight_pharmonline_public_api.py" --x\n',
            [(1, CODE)],
        ),
        # Запуск есть, а что запущено — из текста не узнать.
        ('python "$SCRIPT"', [(1, UNKNOWN)]),
        ("python ${{ inputs.script }}", [(1, UNKNOWN)]),
        ("python scripts/no_such_file.py", [(1, UNKNOWN)]),
        ("python ../outside.py", [(1, UNKNOWN)]),
        ("python tests/../migrations/env.py", [(1, UNKNOWN)]),
        ("python -m some.module", [(1, UNKNOWN)]),
        ("cat job.py | python -", [(1, UNKNOWN)]),
        ("python - < job.py", [(1, UNKNOWN)]),
        ("python -c $CODE", [(1, UNKNOWN)]),
        # Код пришёл по `|`: интерпретатор без единого слова после себя.
        ("cat <<'CODE' | python3\nprint(1)\nCODE\n", [(1, UNKNOWN)]),
        ("out=$(render | python3)\n", [(1, UNKNOWN)]),
        ("render | python3 >/dev/null\n", [(1, UNKNOWN)]),
        # Шаг, тело которого — Python.
        ("- run: |\n    print(1)\n  shell: python\n", [(3, UNKNOWN)]),
        ("defaults:\n  run:\n    shell: 'python3 {0}'\n", [(3, UNKNOWN)]),
        ("steps:\n  - shell: python\n    run: |\n      print(1)\n", [(2, UNKNOWN)]),
        # Не запуск: `python` — аргумент другой команды.
        ("- uses: actions/setup-python@v5\n  with:\n    python-version: '3.12'\n", []),
        ("test -x /opt/app/.venv/bin/python\n", []),
        ("test -x /opt/app/.venv/bin/python && echo ok\n", []),
        ("command -v python3 >/dev/null || exit 1\n", []),
        ("sudo apt-get install -y python3 python3-venv\n", []),
        ("ln -sf /usr/bin/python3 /usr/local/bin/python\n", []),
        ("ls -l .venv/bin/python >&2\n", []),
        ("echo 'python is here'\n", []),
        ("python --version", []),
        ("python -VV", []),
        ('PYTHONPATH="$dir" pharmacy-monitor run', []),
        ("  # руками: python - <<'PY'\n", []),
        ("      - name: Run python - check\n", []),
        # …а после неё в той же строке — уже запуск.
        (f"command -v python3 && python3 - <<'PY'\n{_BLOCK}PY\n", [(1, CODE)]),
        ('if [ -x .venv/bin/python ]; then .venv/bin/python "$SCRIPT"; fi', [(1, UNKNOWN)]),
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
    # Каталог перед путём бывает любым; чужой каталог с тем же хвостом — тоже.
    assert python_runs('python "$dir/scripts/preflight_pharmonline_public_api.py"')[0].origin == (
        "scripts/preflight_pharmonline_public_api.py"
    )
    second = python_runs("python - <<'PY' | python -c 'import this'\nimport os\nPY\n")
    assert [run.what for run in second] == ["import os\n", "import this"]


# ─── Сам разбор: путь до людей ───────────────────────────────────────────────

_FAKE_SRC = {
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

        def init_db():
            for table, column in [("recipients", "telegram_chat_id")]:
                add_column(table, column)
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

        try:
            import fast
        except ImportError:
            def fallback(session):
                alert(session)

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
    "src.main": textwrap.dedent(
        """
        import click
        from src import health, notifier, storage

        @click.group()
        def cli():
            pass

        @cli.command("run")
        @click.option("--site")
        def run_cmd(site):
            storage.make_session()

        @cli.group("recipient")
        def recipient_group():
            pass

        @recipient_group.command("list")
        def recipient_list():
            print(storage.Recipient)

        @retry
        def plain():
            storage.make_session()

        def retry(function):
            return function

        @retry
        def mailer():
            notifier.send_email("x", "y")
        """
    ),
    "src.scrapers.base": "import os\n\ndef proxy():\n    return os.environ['PROXY']\n",
    "scripts.helpers": "from src.tenants import find\n\ndef who(session):\n    return find(session, 1)\n",
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
        ("from src.main import run_cmd, plain\nrun_cmd('aloe'); plain()", []),
        # Модель людей — только колонкой, в которой человека нет.
        ("from src import storage\nselect(storage.TenantUser.tenant_id).distinct()", []),
        ("from src.tenants import count_tenants\ncount_tenants(s)", []),
        ("from src.reexport import count_tenants\ncount_tenants(s)", []),
        # Имя таблицы людей без запроса к ней: список колонок, внешний ключ.
        ("from src import storage\nstorage.init_db()", []),
        ("from src.storage import Run\nRun.requested_by", []),
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
        ("text('select 1 from\\n tenant_users')", ["SQL к таблице `tenant_users` ← код"]),
        ("text('UPDATE recipients SET is_active = false')", ["SQL к таблице `recipients` ← код"]),
        ("text('select 1 from public.tenant_users u')", ["SQL к таблице `tenant_users` ← код"]),
        ("text('select 1 from runs r, recipients x')", ["SQL к таблице `recipients` ← код"]),
        ("text('TRUNCATE recipients')", ["SQL к таблице `recipients` ← код"]),
        ("Base.metadata.tables['recipients']", ["таблица `recipients` ← код"]),
        ("from src.notifier import *", ["`from src.notifier import *` — что взято, не прочитать"]),
        # Импорт по имени и запуск Python из кода: так зовут CLI мимо обеих проверок.
        (
            "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'src.main', 'recipient'])",
            [
                "`subprocess` запускает Python — что именно, не прочитать ← код",
                "строка `src.main` — CLI зовут командой, а не из кода ← код",
            ],
        ),
        (
            "import subprocess\nsubprocess.run(['/opt/app/.venv/bin/python3', path])",
            ["`subprocess` запускает Python — что именно, не прочитать ← код"],
        ),
        (
            "import os\nos.system('python ' + path)\nos.execv('python', args)",
            [
                "`os.execv` запускает Python — что именно, не прочитать ← код",
                "`os.system` запускает Python — что именно, не прочитать ← код",
            ],
        ),
        # Не Python: бэкап перед восстановлением снимает `pg_dump`.
        ("import subprocess\nsubprocess.run(['pg_dump', '--file=x', name], check=True)", []),
        ("import os\nos.system(command)", []),
        (
            "import os\nos.system('cd /opt/app && .venv/bin/python job.py')",
            ["`os.system` запускает Python — что именно, не прочитать ← код"],
        ),
        (
            "from importlib import import_module\nimport_module(name)",
            ["`importlib` — импорт по имени не прочитать ← код"],
        ),
        ("exec(code)", ["`exec` — импорт по имени не прочитать ← код"]),
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
            ["SQL к таблице `tenant_users` ← tenants.raw ← код"],
        ),
        # Импорт внутри функции: в проекте так пишут часто.
        (
            "from src.health import late\nlate(s, w)",
            [
                "аргумент `email=` ← tenants.find ← health.late ← код",
                "модель `TenantUser` ← tenants.find ← health.late ← код",
            ],
        ),
        # Функция, объявленная под `try`.
        (
            "from src.health import fallback\nfallback(s)",
            ["отправка `send_email` ← health.alert ← health.fallback ← код"],
        ),
        # Модуль, взятый через другой модуль: `main.notifier` — тот же `src.notifier`.
        (
            "from src import main\nmain.notifier.send_email('x', 'y')",
            ["отправка `send_email` ← код"],
        ),
        (
            "from src import main\nmain.health.alert(s)",
            ["отправка `send_email` ← health.alert ← код"],
        ),
        (
            "from src.main import health as h\nh.alert(s)",
            ["отправка `send_email` ← health.alert ← код"],
        ),
        # CLI из кода: команды навешены на группу декоратором.
        (
            "from src.main import cli\ncli(['recipient', 'list'])",
            ["модель `Recipient` ← main.recipient_list ← main.recipient_group ← main.cli ← код"],
        ),
        (
            "from src import main\nCliRunner().invoke(main.recipient_group, ['list'])",
            ["модель `Recipient` ← main.recipient_list ← main.recipient_group ← код"],
        ),
        # Простой декоратор — не регистрация: `plain` не тянет за собой `mailer`.
        (
            "from src.main import mailer\nmailer()",
            ["отправка `send_email` ← main.mailer ← код"],
        ),
        # Файл из `scripts/`, который берёт код из соседнего файла.
        (
            "from scripts.helpers import who\nwho(s)",
            [
                "аргумент `email=` ← tenants.find ← scripts.helpers.who ← код",
                "модель `TenantUser` ← tenants.find ← scripts.helpers.who ← код",
            ],
        ),
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
