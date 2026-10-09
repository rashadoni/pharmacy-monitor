"""Адрес получателя не попадает в журнал.

Еженедельный сбор pharmonline идёт из GitHub Actions и рассылает письма, а
журнал шага у публичного репозитория открыт. Получатели — администратор и
сотрудник клиента: адрес в журнале — персональные данные клиента в открытом
доступе.

Слоёв три, и каждый проверяется отдельно:

1. вызов журнала не передаёт адрес. Проверяется дважды: чтением кода всего
   `src/` и прямым вызовом почтовых путей, у которых есть свой обработчик
   сбоя: о сбое в журнал идут класс ошибки и коды, не её текст, и печатью он
   тоже не выводится;
2. что бы ни сломалось при отправке, ошибка выходит из `notifier.send_email`
   уже без адреса — в тексте, в `repr`, в атрибутах, в трассировке, в причине
   и контексте. Куда её запишут дальше, уже не важно: в трассировку
   `run_failed`, в строку «Error: …» от click, в вывод команды, которая сбой не
   ловит вовсе. Стоит это на том, что к почтовому серверу ходит только
   `send_email`, — проверяется тоже;
3. вывод журнала CLI вырезает адрес из готовой строки — и у structlog, и у
   stdlib `logging`, которым пишут сторонние библиотеки.

`click.echo` и `print` идут мимо третьего слоя, а журнал шага Actions публичен.
Поэтому из workflow запускают только команды из короткого списка ниже
(`_RUN_FROM_A_WORKFLOW`), а команды, которым печатать адрес или ответ почтового
сервера разрешено, названы отдельно — и в тот список попасть не могут.

Правила «обработчик сбоя отправки не выводит текст ошибки» здесь больше нет. Оно
узнавало вывод по форме кода — журнал, печать, трассировка, новая ошибка с тем
же текстом, обёртка над отправкой — и за три захода так и не сошлось, а до
команды, которая сбой не ловит, не дотягивалось вовсе. Текст чистится там, где
он появляется (слой 2); обработчики почтовых путей проверяет прямой вызов
(слой 1).

Чего после этого не ловит ничто: новый обработчик, в `try` которого рядом с
отправкой стоит ещё что-то (запись в базу), а текст пойманной ошибки он
печатает через `click.echo` как есть. Ошибка отправки туда придёт чистой, а
ошибка базы — с параметрами запроса. Правило на это одно, и держит его
человек: мимо журнала текст чужой ошибки печатают через
`logging_setup.without_addresses`.

`structlog.testing.capture_logs()` не видит событий внутри `CliRunner`, поэтому
почтовые пути вызываются напрямую.
"""

from __future__ import annotations

import ast
import email.errors
import json
import logging
import random
import re
import smtplib
import socket
import time
import traceback
from contextlib import nullcontext
from pathlib import Path

import click
import pytest
import structlog
from click.testing import CliRunner
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import (
    alerts,
    api,
    digest,
    error_reporting,
    health,
    logging_setup,
    main,
    notifications,
    notifier,
    storage,
    tenants,
)
from src._time import utcnow

SRC = Path(__file__).resolve().parent.parent / "src"
ADDRESS = "viewer@client.example"
SMTP_REPLY = f"5.1.1 <{ADDRESS}>: Recipient address rejected"


# ─── Слой 1а: чтение кода ────────────────────────────────────────────────────

_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
# Имена и атрибуты, под которыми в проекте ходит адрес или идентификатор
# человека. Проверка видит имя, а не значение: адрес в переменной с другим
# именем, в `**fields` или в repr объекта она не поймает — на это третий слой.
# `server_reply` — ответ почтового сервера на отказ (`EmailDeliveryError`): адреса
# из него вырезаны маской, но ящик сервер может назвать и без «@» («user viewer
# unknown»), а такое маска не узнает.
_PERSON_ATTRS = {"email", "emails", "telegram_chat_id", "server_reply"}
_PERSON_NAMES = {
    "email",
    "emails",
    "recipients",
    "recipient",
    "to",
    "email_to",
    "only_email",
    "to_addrs",
    "chat_id",
}
_COUNTING = {"len", "bool"}
_PRINTING_CALLS = {
    "click.echo",
    "click.secho",
    "ctx.fail",
    "sys.exit",
    "sys.stdout.write",
    "sys.stderr.write",
}

# Команды, которые печатают адрес или ответ почтового сервера по назначению:
# оператор запускает их сам и читает вывод в своём терминале. `click.echo` и
# `print` идут мимо маски журнала, поэтому в список команд для workflow их не
# вносят — см. тест ниже. Функция команды → как её зовут в CLI; имена сверяются
# с деревом click.
_PRINTS_AN_ADDRESS_BY_DESIGN = {
    "tenant_add_user": "tenant add-user",
    "tenant_issue_token": "tenant issue-token",
    "recipient_add": "recipient add",
    "recipient_list": "recipient list",
    "recipient_remove": "recipient remove",
    "recipient_toggle": "recipient toggle",
    "recipient_update": "recipient update",
    # Проверка доставки. Об отказе SMTP печатает ответ сервера (`server_reply`)
    # — больше его не печатает никто, docs/RUNBOOK.md «Email не приходит».
    "notify_test": "notify test",
}
# Команды CLI, которые запускают из workflow. `click.echo`, `print` и строка
# «Error: …» идут мимо маски журнала прямо в публичный журнал шага, поэтому
# внести сюда команду — значит прочитать всё, что она печатает: текст пойманной
# ошибки — только через `logging_setup.without_addresses`. Кто и что прочитал,
# пишется в PR словами.
#
# `health-check` здесь нет словом владельца: он запретил запускать её из
# workflow, когда она печатала текст сбоя отправки как есть. Теперь всё, что она
# печатает из ошибок, идёт через `without_addresses`
# (`test_health_check_output_carries_no_address`), но внести её — его решение.
_RUN_FROM_A_WORKFLOW = {
    # Сбор. Письма о прогоне, трассировку `run_failed` и строку «Error: …»
    # проверяют тесты этого файла.
    "run",
}

_HOW_TO_FIX_AN_ADDRESS = (
    "В журнал и в вывод команды уходит адрес, идентификатор человека или ответ "
    "почтового сервера (`server_reply`: ящик в нём бывает назван и без «@»). "
    "Пиши `user_id=user.id`, число (`len(...)`) или "
    "`**notifier.delivery_error_fields(exc)`. Если переменная названа "
    "`to`/`email`, но адресом не является, — переименуй её. Если команда "
    "печатает адрес оператору по назначению (как `recipient list`) — это "
    "решение, а не правка теста: внеси её функцию в "
    "`_PRINTS_AN_ADDRESS_BY_DESIGN`, и запускать её из workflow станет нельзя."
)
_HOW_TO_FIX_THE_LIST_OF_PRINTERS = (
    "Список `_PRINTS_AN_ADDRESS_BY_DESIGN` разошёлся с кодом. Печатает, но не в "
    "списке: {unlisted} — новая команда выводит адрес или ответ почтового "
    "сервера через `print`/`click.echo`. Если в выводе он не нужен, печатай "
    "`user_id`, число или класс ошибки; если нужен оператору — добавь строку "
    "«функция: имя в CLI»: в список команд для workflow её после этого не внести. "
    "В списке, но не печатает: {stale} — команда перестала это выводить или её "
    "функцию переименовали: убери или поправь строку. В список идут только "
    "команды CLI: если адрес печатает вспомогательная функция, перенеси печать "
    "в саму команду."
)
_HOW_TO_FIX_A_WORKFLOW = (
    "В workflow после имени CLI стоит не то, что есть в `_RUN_FROM_A_WORKFLOW`: "
    "{found}. Журнал шага Actions публичен, а `click.echo`, `print` и строка "
    "«Error: …» идут мимо маски журнала. Прежде чем внести команду в список, "
    "прочитай всё, что она печатает: адрес — только `user_id` или числом, текст "
    "пойманной ошибки — только через `logging_setup.without_addresses`; что "
    "прочитано, напиши в PR. Команду из `_PRINTS_AN_ADDRESS_BY_DESIGN` внести "
    "нельзя: она печатает адрес или ответ почтового сервера по назначению. "
    "`health-check` — только по слову владельца. «{unknown}» значит, что после "
    "имени CLI стоят слова, но команды среди них нет: она в переменной "
    "(`pharmacy-monitor $CMD`) или это не запуск, а описание. Команду назови в "
    "тексте явно, описание перефразируй (комментарии и `name:` шага проверка "
    "пропускает сама)."
)
_HOW_TO_FIX_A_COMMAND_NAME = (
    "В списке названа команда, которой в CLI нет под этим именем или за ней "
    "стоит другая функция: {problems}. Под мёртвым именем список описывает не "
    "ту команду, что запускается на самом деле. Поправь имя в "
    "`_PRINTS_AN_ADDRESS_BY_DESIGN` / `_RUN_FROM_A_WORKFLOW`."
)
_HOW_TO_FIX_A_BYPASS = (
    "К почтовому серверу ходят мимо `notifier.send_email`: {found}. Только она "
    "выпускает ошибку отправки без адреса в тексте; ошибка smtplib и ошибка "
    "`_send_email` несут адрес получателя, и дальше их печатают трассировка "
    "`run_failed`, click и команды, которые сбой не ловят. Шли письмо через "
    "`notifier.send_email`. Если нужен второй вход (другой транспорт, "
    "асинхронная отправка) — он обязан чистить ошибку так же, и проверять его "
    "надо теми же тестами слоя 2. Если находка — не smtplib, а своё имя `SMTP` "
    "или `LMTP` (значение перечисления, константа), — переименуй его: проверка "
    "сверяет имена. В workflow имя ищется по тексту, и слово в комментарии — "
    "тоже находка."
)


def _called_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _is_log_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOG_METHODS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "log"
    )


def _is_print_call(node: ast.AST) -> bool:
    """`print`, `click.echo`, `sys.exit("…")`, `sys.stderr.write` — вывод мимо журнала."""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id in {"print", "exit"}
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in {"echo", "secho", "exit", "write", "fail"}
        and ast.unparse(node.func) in _PRINTING_CALLS
    )


def _nodes_with_owner(node: ast.AST, owner: ast.AST | None = None):
    """Все узлы дерева и функция, внутри которой каждый стоит."""
    for child in ast.iter_child_nodes(node):
        yield child, owner
        inner = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else owner
        yield from _nodes_with_owner(child, inner)


def _calls_with_owner(node: ast.AST):
    """Все вызовы и имя функции, внутри которой каждый стоит."""
    return (
        (child, owner.name if owner else None)
        for child, owner in _nodes_with_owner(node)
        if isinstance(child, ast.Call)
    )


def _walk_outside(node: ast.AST, wrappers: set[str]):
    """Обход выражения без захода внутрь вызовов из `wrappers`."""
    stack = [node]
    while stack:
        current = stack.pop()
        if _called_name(current) in wrappers:
            continue
        yield current
        stack.extend(ast.iter_child_nodes(current))


def _values(call: ast.Call) -> list[ast.AST]:
    return [*call.args, *(keyword.value for keyword in call.keywords)]


def _names_a_person(call: ast.Call) -> bool:
    return any(
        (isinstance(node, ast.Attribute) and node.attr in _PERSON_ATTRS)
        or (isinstance(node, ast.Name) and node.id in _PERSON_NAMES)
        for value in _values(call)
        for node in _walk_outside(value, _COUNTING)
    )


def address_leaks(source: str, *, by_design: frozenset[str] | None = None) -> list[str]:
    """Вызовы журнала, `print` и `click.echo`, которым передан адрес.

    Печать внутри команд из `by_design` не считается; журнал считается всегда.
    """
    allowed = frozenset(_PRINTS_AN_ADDRESS_BY_DESIGN) if by_design is None else by_design
    found = []
    for call, owner in _calls_with_owner(ast.parse(source)):
        printed = _is_print_call(call) and owner not in allowed
        if (_is_log_call(call) or printed) and _names_a_person(call):
            found.append(f"{call.lineno}: {ast.unparse(call)}")
    return sorted(set(found))


def _src_sources() -> dict[str, str]:
    return {
        str(path.relative_to(SRC.parent)): path.read_text(encoding="utf-8")
        for path in sorted(SRC.rglob("*.py"))
    }


def _across_src(check) -> list[str]:
    return [f"{path}:{line}" for path, source in _src_sources().items() for line in check(source)]


def test_no_log_or_print_call_in_src_is_given_an_address():
    assert _across_src(address_leaks) == [], _HOW_TO_FIX_AN_ADDRESS


def test_the_list_of_commands_that_print_an_address_is_exact():
    """Новая команда, печатающая адрес, — решение, а не случайность."""
    printing = {
        owner
        for source in _src_sources().values()
        for call, owner in _calls_with_owner(ast.parse(source))
        if _is_print_call(call) and _names_a_person(call)
    }
    listed = set(_PRINTS_AN_ADDRESS_BY_DESIGN)
    assert printing == listed, _HOW_TO_FIX_THE_LIST_OF_PRINTERS.format(
        unlisted=sorted(map(str, printing - listed)) or "нет",
        stale=sorted(listed - printing) or "нет",
    )


def _cli_callback(name: str) -> str | None:
    """Функция, которая стоит за командой CLI с таким именем, — или `None`."""
    command = main.cli
    for word in name.split():
        command = command.commands.get(word) if isinstance(command, click.Group) else None
    if command is None or isinstance(command, click.Group):
        return None
    return command.callback.__name__


def test_every_listed_command_is_a_cli_command_under_that_name():
    """Переименование команды не оставляет в списках мёртвое имя."""
    problems = []
    for function, name in _PRINTS_AN_ADDRESS_BY_DESIGN.items():
        callback = _cli_callback(name)
        if callback is None:
            problems.append(f"«{name}» — такой команды нет")
        elif callback != function:
            problems.append(f"«{name}» — это {callback}, а не {function}")
    problems += [
        f"«{name}» — такой команды нет"
        for name in sorted(_RUN_FROM_A_WORKFLOW)
        if _cli_callback(name) is None
    ]
    assert problems == [], _HOW_TO_FIX_A_COMMAND_NAME.format(problems="; ".join(problems))


# Запуск CLI в тексте workflow. Консольный скрипт — отдельным словом (в
# кавычках, в `${PM:-pharmacy-monitor}`, элементом списка) или из каталога
# `bin/`; каталог `/opt/pharmacy-monitor` — не запуск. `python -m src.main` —
# тоже запуск.
_CLI_ENTRY_POINT = re.compile(
    r"""(?:(?:(?<![\w/.])|(?<=bin/))pharmacy-monitor|-m[ \t]+src\.main)["'}]*,?[ \t]"""
)
# Путь `src/main.py` стоит и там, где файл запускают, и там, где его копируют:
# запуском он считается, только если следом идёт команда CLI.
_CLI_SCRIPT_PATH = re.compile(r"""src/main\.py["'}]*,?[ \t]""")
_END_OF_SHELL_COMMAND = re.compile(r"&&|\|\||[;|\n]")
# Строки, которые ничего не запускают: комментарий и имя шага.
_NOT_A_COMMAND_LINE = re.compile(r"^[ \t]*(?:#|-?[ \t]*name:).*$", re.M)
# После имени CLI стоят слова, но команды среди них нет.
_UNKNOWN_COMMAND = "команда не узнана"


def _cli_command(words: list[str]) -> str:
    """Какую команду CLI запускают слова после точки входа: «recipient list».

    Слова сверяются с деревом click. Опция пропускается, значение опции группы
    — вместе с ней: `--log-level run recipient list` запускает `recipient
    list`, а не `run`. На первом слове, которое не опция и не команда, разбор
    останавливается: `"$CMD" run` — не `run`, а неизвестно что.
    """
    group, path, is_a_value = main.cli, [], False
    for word in words:
        if is_a_value:
            is_a_value = False
            continue
        name = re.split(r"[<>]", word.strip("'\"`()&[],"))[0]  # `health-check>out.txt`
        if name.startswith("-"):
            is_a_value = any(
                name in param.opts and not param.is_flag
                for param in group.params
                if isinstance(param, click.Option)
            )
            continue
        command = group.commands.get(name)
        if command is None:
            break
        path.append(name)
        if not isinstance(command, click.Group):
            break
        group = command
    return " ".join(path)


def cli_commands_run(text: str) -> list[tuple[int, str]]:
    """Команды CLI, которые запускает текст workflow: номер строки и команда —
    или `_UNKNOWN_COMMAND`, если после имени CLI стоят слова, а команды среди
    них нет (`pharmacy-monitor $CMD`, `pharmacy-monitor ${{ inputs.command }}`).

    Перенос строки через `\\` — продолжение той же команды. Текст читается, а
    не исполняется, поэтому команду разбор видит и в строке `echo` с её
    описанием. Чего он не видит: имя CLI в переменной (`"$PM" recipient list`,
    массив bash), скрипт не из каталога `bin/` (`./pharmacy-monitor …`),
    `src/main.py` с командой в переменной, команду с новой строки без `\\`
    (YAML `run: >`), запуск внутри скрипта, который workflow зовёт, и вызов из
    Python (`cli([...])`).
    """

    def blank(found: re.Match) -> str:
        return " " * len(found.group())  # той же длины: номера строк не съезжают

    joined = re.sub(r"\\\r?\n", blank, _NOT_A_COMMAND_LINE.sub(blank, text))
    commands = []
    for entry_point, certainly_a_run in ((_CLI_ENTRY_POINT, True), (_CLI_SCRIPT_PATH, False)):
        for entry in entry_point.finditer(joined):
            words = _END_OF_SHELL_COMMAND.split(joined[entry.end() :], maxsplit=1)[0].split()
            command = _cli_command(words)
            if command or (words and certainly_a_run):
                line = text.count("\n", 0, entry.start()) + 1
                commands.append((line, command or _UNKNOWN_COMMAND))
    return sorted(commands)


def test_a_workflow_runs_only_the_commands_listed_for_it():
    """Журнал шага Actions публичен, а `click.echo` маска не трогает."""
    workflows = sorted((SRC.parent / ".github" / "workflows").glob("*.y*ml"))
    run = {path.name: cli_commands_run(path.read_text(encoding="utf-8")) for path in workflows}
    found = [
        f"{name}:{line}: {command}"
        for name, commands in run.items()
        for line, command in commands
        if command not in _RUN_FROM_A_WORKFLOW
    ]
    assert found == [], _HOW_TO_FIX_A_WORKFLOW.format(
        found="; ".join(found), unknown=_UNKNOWN_COMMAND
    )
    # Разбор не ослеп: в workflow, ради которого всё это, — еженедельном сборе
    # pharmonline, который шлёт письма, — он видит запуск сбора.
    weekly = "autonomous-pharmonline-decodo-public-api.yml"
    assert "run" in {command for _, command in run.get(weekly, [])}, (
        f"Проверка не видит запуск сбора в {weekly}. Если файл переименован — "
        "поправь имя здесь; если сбор теперь запускается иначе — научи "
        "`cli_commands_run` читать новую форму, иначе этот workflow никто не "
        "проверяет."
    )


def test_no_command_that_prints_an_address_is_listed_for_a_workflow():
    listed = _RUN_FROM_A_WORKFLOW & set(_PRINTS_AN_ADDRESS_BY_DESIGN.values())
    assert listed == set(), _HOW_TO_FIX_A_WORKFLOW.format(
        found=", ".join(sorted(listed)), unknown=_UNKNOWN_COMMAND
    )


@pytest.mark.parametrize(
    ("text", "commands"),
    [
        ("  .venv/bin/pharmacy-monitor recipient list", [(1, "recipient list")]),
        ("python -m src.main tenant   issue-token admin", [(1, "tenant issue-token")]),
        # Опция группы перед командой: значением через пробел и через «=».
        ("python -m src.main --log-level DEBUG recipient list", [(1, "recipient list")]),
        (
            '"$VENV/bin/pharmacy-monitor" --log-level=DEBUG recipient add a@b.example',
            [(1, "recipient add")],
        ),
        # Перенос строки через «\»: номер строки — где стоит точка входа.
        (
            "run: |\n  pharmacy-monitor \\\n    --log-level DEBUG \\\n    recipient \\\n    list\n",
            [(2, "recipient list")],
        ),
        # Команда в кавычках ssh, после другой команды.
        (
            "ssh pm@host 'cd /opt/pharmacy-monitor && .venv/bin/pharmacy-monitor notify test'",
            [(1, "notify test")],
        ),
        ("pharmacy-monitor health-check --alert-email || true", [(1, "health-check")]),
        # Подкоманда в переменной: названа только группа.
        ('pharmacy-monitor recipient "$ACTION"', [(1, "recipient")]),
        # Точка входа в подстановке по умолчанию; вывод, перенаправленный в файл.
        ("${PM:-pharmacy-monitor} recipient list", [(1, "recipient list")]),
        ("pharmacy-monitor health-check>/tmp/health.txt 2>&1", [(1, "health-check")]),
        ("pharmacy-monitor health-check& wait", [(1, "health-check")]),
        ("pharmacy-monitor run --site aloe; echo recipient list", [(1, "run")]),
        (
            "/opt/pharmacy-monitor/.venv/bin/python src/main.py recipient list",
            [(1, "recipient list")],
        ),
        # Значение опции группы, названное как команда.
        ("pharmacy-monitor --log-level run recipient list", [(1, "recipient list")]),
        # Команда в переменной: запуск есть, а что запущено — из текста не узнать.
        ("pharmacy-monitor $CMD", [(1, _UNKNOWN_COMMAND)]),
        ('pharmacy-monitor "$CMD" run', [(1, _UNKNOWN_COMMAND)]),
        # `src/main.py` за интерпретатором с опцией и за интерпретатором в переменной.
        ("python -u src/main.py health-check", [(1, "health-check")]),
        ('"$PY" src/main.py recipient list', [(1, "recipient list")]),
        # Команда списком YAML.
        ('command: ["pharmacy-monitor", "recipient", "list"]', [(1, "recipient list")]),
        ("pharmacy-monitor ${{ inputs.command }} --site aloe", [(1, _UNKNOWN_COMMAND)]),
        ("python -m src.main --help", [(1, _UNKNOWN_COMMAND)]),
        # Не запуск: каталог, копирование файла, встроенный Python.
        ("cd /opt/pharmacy-monitor && ls recipient list", []),
        ("tar -C /opt/pharmacy-monitor src", []),
        ("rsync -a src/main.py src/notifier.py pm@host:/opt/pharmacy-monitor/src/", []),
        ("from src.main import (", []),
        ("          from src.main import _verify_pharmonline_public_api_identities", []),
        # Комментарий и имя шага ничего не запускают.
        ("  # после выкладки руками: pharmacy-monitor notify test", []),
        ("      - name: Restart pharmacy-monitor health-check timer", []),
        ("#!/bin/sh\npharmacy-monitor \\\n  notify test\n", [(2, "notify test")]),
    ],
)
def test_the_workflow_check_finds_the_command_behind_options_and_line_breaks(text, commands):
    assert cli_commands_run(text) == commands


def test_health_check_is_not_listed_for_a_workflow_without_the_owners_word():
    """Растяжка, а не проверка: сама она упасть может только от правки списка —
    и говорит тому, кто правит, чьё это решение."""
    assert "health-check" not in _RUN_FROM_A_WORKFLOW, (
        "`health-check` из workflow запретил запускать владелец. Причина, по "
        "которой запрет был введён, устранена, но снять его — решение владельца, "
        "а не правка списка. Есть его слово — убери этот тест и напиши в PR, "
        "что команда печатает."
    )


@pytest.mark.parametrize(
    "call",
    [
        'log.warning("email_batch_failed", user=user.email)',
        'log.info("smtp_send", host=host, recipients=recipients)',
        'log.info("telegram_bound", user=email, chat_id=chat_id)',
        'log.info("recipient_created", id=new_user.id, email=new_user.email)',
        'log.info("x", who=f"to {user.email}")',
        'log.info("x", to=", ".join(recipients))',
        'print(f"digest sent to {user.email}")',
        'click.echo(f"OK: {r.email}")',
        # Ответ почтового сервера: ящик в нём бывает назван и без «@».
        'log.warning("email_batch_failed", reply=exc.server_reply)',
        'click.echo(f"email: FAIL — {exc}: {exc.server_reply}")',
    ],
)
def test_the_code_check_sees_an_address_in_a_log_call(call):
    assert len(address_leaks(call)) == 1


@pytest.mark.parametrize(
    "call",
    [
        'log.info("smtp_send", recipients=len(recipients))',
        'log.info("alert_dispatched", email=len(results["email"]), telegram=0)',
        'log.info("digest_sent", kind=kind, recipients=sent)',
        'log.warning("email_batch_failed", user_id=user.id, **fields)',
        'log.info("x", has_recipients=bool(recipients))',
        'log.info("pharmacy_seen", address=address, addresses=len(rows))',
        'click.echo(f"OK: {kind} digest sent to {sent} recipients")',
    ],
)
def test_the_code_check_lets_counts_and_ids_through(call):
    assert address_leaks(call) == []


def test_the_code_check_excuses_printing_only_inside_the_listed_commands():
    source = """
def recipient_list():
    click.echo(f"  {r.email}")
    log.info("recipients_listed", first=r.email)

def run_cmd():
    click.echo(f"  {r.email}")
"""
    assert [line.split(":")[0] for line in address_leaks(source)] == ["4", "7"]


# ─── Слой 1б: каждый почтовый путь, вызванный напрямую ───────────────────────


# Отказ, как его пишет smtplib, и запись адреса, которую маска журнала не узнаёт.
_SMTPLIB_TEXT = f"{{'{ADDRESS}': (550, b'{SMTP_REPLY}')}}"
_PAST_THE_MASK = '"viewer name"@client.example'


def _refused(*args, **kwargs):
    """Сбой отправки с адресом в тексте. Настоящий `send_email` такую ошибку
    наружу не выпускает (слой 2); обработчик проверяется так, будто выпустил.
    Вторая запись адреса — чтобы печать через маску за чистую не сошла: мимо
    журнала текст ошибки печатают только через `without_addresses`."""
    raise RuntimeError(f"{_SMTPLIB_TEXT} {_PAST_THE_MASK}")


@pytest.fixture
def session(monkeypatch, db_session, capsys):
    engine = db_session.get_bind()
    Session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *a, **kw: Session)
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", "https://example.com")
    monkeypatch.setattr(notifier, "send_email", _refused)
    monkeypatch.setattr(notifier, "send_telegram_message", _refused)
    yield db_session
    # Журнал каждый тест смотрит сам; здесь — что обработчик сбоя не вывел текст
    # ошибки печатью, мимо журнала.
    printed = capsys.readouterr()
    assert "@" not in printed.out + printed.err, printed


@pytest.fixture
def user(session):
    tenant = tenants.get_or_create_default(session)
    row = storage.TenantUser(
        tenant_id=tenant.id,
        email=ADDRESS,
        role="admin",
        is_active=True,
        created_at=utcnow(),
        email_severity_min="info",
        telegram_severity_min="info",
        telegram_chat_id="700100",
        daily_digest=False,
        weekly_digest=True,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _event(session, user, *, rule_type="site_silent") -> storage.AlertEvent:
    event = storage.AlertEvent(
        rule_type=rule_type,
        dedup_key=f"no-address-{rule_type}",
        severity="critical",
        title="aloe молчит",
        detail="Сайт не отвечал сутки",
        tenant_id=user.tenant_id,
        created_at=utcnow(),
    )
    session.add(event)
    session.commit()
    return event


def _assert_clean(logs: list[dict], *, events: set[str], user_id: int | None) -> None:
    assert "@" not in repr(logs), logs
    failures = [entry for entry in logs if entry["event"] in events]
    assert {entry["event"] for entry in failures} == events, logs
    for entry in failures:
        # Диагностика остаётся: кому не ушло и чем ответил сервер.
        if user_id is not None:
            assert entry["user_id"] == user_id
        assert entry["error_type"] == "RuntimeError"
        # Ни текста ошибки, ни трассировки с ним.
        assert "error" not in entry and "user" not in entry
        assert not entry.get("exc_info")


def test_single_event_dispatch_failure_log_carries_no_address(session, user):
    event = _event(session, user)
    with capture_logs() as logs:
        notifications.dispatch_event(session, event)
    _assert_clean(
        logs, events={"email_dispatch_failed", "telegram_dispatch_failed"}, user_id=user.id
    )


def test_run_letter_failure_log_carries_no_address(session, user):
    event = _event(session, user)
    with capture_logs() as logs:
        counts = notifications.dispatch_events_batch(session, [event])
    assert counts == {"email": 0, "telegram": 0}
    _assert_clean(logs, events={"email_batch_failed", "telegram_batch_failed"}, user_id=user.id)


def test_admin_only_letter_failure_log_carries_no_address(session, user):
    event = _event(session, user)
    with capture_logs() as logs:
        counts = notifications.mail_unstored_events_to_admins(session, [event], note="тик")
    assert counts == {"email": 0, "telegram": 0, "failed": 2}
    _assert_clean(logs, events={"email_batch_failed", "telegram_batch_failed"}, user_id=user.id)


def test_per_user_digest_failure_log_carries_no_address(session, user):
    _event(session, user)
    with capture_logs() as logs:
        sent = notifications.send_weekly_digest(session, tenant_id=user.tenant_id)
    assert sent == 0
    _assert_clean(logs, events={"digest_email_failed"}, user_id=user.id)


def test_per_user_digest_dry_run_names_the_recipient_by_id(session, user):
    _event(session, user)
    with capture_logs() as logs:
        notifications.send_weekly_digest(session, tenant_id=user.tenant_id, dry_run=True)
    assert "@" not in repr(logs), logs
    dry_run = [entry for entry in logs if entry["event"] == "digest_dry_run"]
    assert [entry["user_id"] for entry in dry_run] == [user.id]


def test_telegram_binding_log_carries_no_address(session, user):
    with capture_logs() as logs:
        assert notifications.bind_telegram(session, "700200", ADDRESS) is True
    assert "@" not in repr(logs), logs
    assert logs == [{"event": "telegram_bound", "log_level": "info", "user_id": user.id}]


def test_legacy_daily_digest_failure_log_carries_no_address(session, user):
    _event(session, user)
    with capture_logs() as logs:
        assert digest.send_daily_digest(session) == 0
    _assert_clean(logs, events={"digest_email_failed"}, user_id=None)


def test_legacy_alert_letter_failure_log_carries_no_address(session, user):
    event = _event(session, user)
    with capture_logs() as logs:
        alerts._send_email_alert(event)
    _assert_clean(logs, events={"alert_email_failed"}, user_id=None)


def test_error_report_letter_failure_log_carries_no_address(session, monkeypatch, tmp_path):
    monkeypatch.setattr(error_reporting, "ERRORS_LOG", tmp_path / "errors.jsonl")
    monkeypatch.setattr(error_reporting, "_RECENT", {})
    with capture_logs() as logs:
        error_reporting.report_error(ValueError("сбор упал"), component="test")
    _assert_clean(logs, events={"error_email_failed"}, user_id=None)


def test_login_link_failure_log_carries_no_address(session, user):
    with capture_logs() as logs:
        assert api._send_login_link(session, ADDRESS) is True
    _assert_clean(logs, events={"login_link_email_failed"}, user_id=user.id)


@pytest.fixture
def cli_logging(monkeypatch, tmp_path):
    """`_setup_logging` пишет в `logs/` текущего каталога и меняет общий конфиг."""
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield
    for handler in root.handlers:
        handler.close()
    root.handlers[:], root.level = saved[0], saved[1]
    structlog.reset_defaults()


# ─── Слой 2: что бы ни сломалось, ошибка выходит из send_email без адреса ────


def _smtp_that_fails_with(
    error: BaseException, at: str = "send_message", closing: BaseException | None = None
):
    """Почтовый сервер, который падает с `error` на шаге `at`, а при закрытии
    соединения — ещё и с `closing`."""

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            if at == "connect":
                raise error

        def __enter__(self):
            return self

        def __exit__(self, *args):
            if closing is not None:
                raise closing
            return False

        def starttls(self):
            if at == "starttls":
                raise error

        def login(self, *args):
            if at == "login":
                raise error

        def send_message(self, message):
            if at == "send_message":
                raise error

    return FakeSMTP


def _everything_printed(failure: BaseException) -> str:
    """Всё, что из ошибки может попасть в вывод: текст, repr, аргументы,
    атрибуты и трассировка — вместе с причиной и контекстом, если они есть."""
    shown = [str(failure), repr(failure), repr(failure.args), repr(vars(failure))]
    return "".join(shown + traceback.format_exception(failure))


# Строка `notifier.py`, на которой письмо остановилось, — в конце текста ошибки.
_RAISED_AT = r" \(notifier\.py:\d+ in _send_email\)"


@pytest.fixture
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "resend")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_FROM", "Pharmacy Monitor")


def test_smtp_send_logs_how_many_recipients_not_who(smtp_env, monkeypatch):
    monkeypatch.setattr("smtplib.SMTP", _smtp_that_fails_with(RuntimeError(), at="never"))
    with capture_logs() as logs:
        assert notifier.send_email("Тема", "<p>x</p>", to=[ADDRESS, "second@client.example"])
    assert "@" not in repr(logs), logs
    assert [entry["event"] for entry in logs] == ["smtp_send", "smtp_sent_ok"]
    assert logs[0]["recipients"] == 2


@pytest.mark.parametrize(
    ("error", "at", "text", "fields", "reply"),
    [
        (
            smtplib.SMTPRecipientsRefused({ADDRESS: (550, SMTP_REPLY.encode())}),
            "send_message",
            "SMTPRecipientsRefused (SMTP 550 5.1.1)",
            {"error_type": "SMTPRecipientsRefused", "smtp_code": 550, "smtp_status": "5.1.1"},
            "5.1.1 <<address>>: Recipient address rejected",
        ),
        (
            smtplib.SMTPDataError(554, f"5.7.1 message to {ADDRESS} refused".encode()),
            "send_message",
            "SMTPDataError (SMTP 554 5.7.1)",
            {"error_type": "SMTPDataError", "smtp_code": 554, "smtp_status": "5.7.1"},
            "5.7.1 message to <address> refused",
        ),
        (
            smtplib.SMTPSenderRefused(550, b"domain is not verified", "digest@sender.example"),
            "send_message",
            "SMTPSenderRefused (SMTP 550)",
            {"error_type": "SMTPSenderRefused", "smtp_code": 550},
            "domain is not verified",
        ),
        (
            smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication credentials invalid"),
            "login",
            "SMTPAuthenticationError (SMTP 535 5.7.8)",
            {"error_type": "SMTPAuthenticationError", "smtp_code": 535, "smtp_status": "5.7.8"},
            "5.7.8 Authentication credentials invalid",
        ),
        (
            # Расширенный код — только в начале ответа и только из трёх чисел.
            smtplib.SMTPDataError(451, b"4.7.0.1 is not a status; policy 5.7.1 applies"),
            "send_message",
            "SMTPDataError (SMTP 451)",
            {"error_type": "SMTPDataError", "smtp_code": 451},
            "4.7.0.1 is not a status; policy 5.7.1 applies",
        ),
        (
            smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
            "starttls",
            "SMTPServerDisconnected",
            {"error_type": "SMTPServerDisconnected"},
            "Connection unexpectedly closed",
        ),
        (
            # Запись адреса, которую маска не узнала: ответа нет вовсе, а код из
            # его начала остаётся.
            smtplib.SMTPDataError(550, f"5.1.1 {_PAST_THE_MASK}: no such user".encode()),
            "send_message",
            "SMTPDataError (SMTP 550 5.1.1)",
            {"error_type": "SMTPDataError", "smtp_code": 550, "smtp_status": "5.1.1"},
            "<текст скрыт>",
        ),
    ],
)
def test_smtp_refusal_leaves_send_email_without_an_address(
    smtp_env, monkeypatch, error, at, text, fields, reply
):
    monkeypatch.setattr("smtplib.SMTP", _smtp_that_fails_with(error, at=at))

    with capture_logs() as logs, pytest.raises(notifier.EmailDeliveryError) as raised:
        notifier.send_email("Тема", "<p>x</p>", to=[ADDRESS])

    failure = raised.value
    assert isinstance(failure, notifier.EmailSendError)
    assert "@" not in _everything_printed(failure), _everything_printed(failure)
    assert failure.__cause__ is None and failure.__context__ is None
    assert "@" not in repr(logs), logs

    # Диагностика: класс исходной ошибки и коды — в журнал, ответ сервера — оператору.
    assert str(failure) == text
    assert notifier.delivery_error_fields(failure) == fields
    assert failure.server_reply == reply


@pytest.mark.parametrize(
    ("error", "at", "shown", "fields"),
    [
        # Пакет `email` кладёт в текст ошибки сам заголовок письма — со всеми
        # получателями разом (`HeaderWriteError`: «folded header contains
        # newline: b'To: …'»). Текст таких ошибок не показывается никогда —
        # и тогда, когда «@» в нём нет: нелатинская часть заголовка записана
        # base64, а разбор называет ящик без домена.
        (
            email.errors.HeaderParseError(f"cannot fold b'To: {ADDRESS},\\r\\n {ADDRESS}'"),
            "send_message",
            "HeaderParseError: <текст скрыт>",
            {"error_type": "HeaderParseError"},
        ),
        (
            email.errors.HeaderParseError("expected atom but found '=?utf-8?b?0LjQstCw0L0=?='"),
            "send_message",
            "HeaderParseError: <текст скрыт>",
            {"error_type": "HeaderParseError"},
        ),
        # Текст с «@» не показывается целиком: маска оставила бы от него начало
        # имени, имя рядом с адресом, а адрес с именем в кавычках — как есть.
        (
            RuntimeError(f"relay refused {ADDRESS}: over quota"),
            "send_message",
            "RuntimeError: <текст скрыт>",
            {"error_type": "RuntimeError"},
        ),
        (
            RuntimeError("relay refused o'brien@client.example"),
            "send_message",
            "RuntimeError: <текст скрыт>",
            {"error_type": "RuntimeError"},
        ),
        (
            ValueError(f"cannot fold Ivan Petrov <{ADDRESS}>"),
            "send_message",
            "ValueError: <текст скрыт>",
            {"error_type": "ValueError"},
        ),
        (
            ValueError('cannot fold "viewer name"@client.example'),
            "send_message",
            "ValueError: <текст скрыт>",
            {"error_type": "ValueError"},
        ),
        # Адрес не в тексте, а в аргументах ошибки — его печатает repr.
        (
            UnicodeEncodeError("ascii", "почта@пример.рф", 0, 5, "ordinal not in range(128)"),
            "login",
            "UnicodeEncodeError: 'ascii' codec can't encode characters in position 0-4: "
            "ordinal not in range(128)",
            {"error_type": "UnicodeEncodeError"},
        ),
        # Сеть: класс и errno — то, по чему случаи различает docs/RUNBOOK.md.
        (
            ConnectionRefusedError(111, "Connection refused"),
            "connect",
            "ConnectionRefusedError: [Errno 111] Connection refused",
            {"error_type": "ConnectionRefusedError", "errno": 111},
        ),
        (
            socket.gaierror(-2, "Name or service not known"),
            "connect",
            "gaierror: [Errno -2] Name or service not known",
            {"error_type": "gaierror", "errno": -2},
        ),
        (
            TimeoutError("timed out"),
            "starttls",
            "TimeoutError: timed out",
            {"error_type": "TimeoutError"},
        ),
        # Ошибка без текста.
        (RuntimeError(), "send_message", "RuntimeError", {"error_type": "RuntimeError"}),
    ],
)
def test_any_other_failure_leaves_send_email_without_an_address(
    smtp_env, monkeypatch, error, at, shown, fields
):
    """Не только отказ сервера: сборка письма, сеть, что угодно."""
    monkeypatch.setattr("smtplib.SMTP", _smtp_that_fails_with(error, at=at))

    with capture_logs() as logs, pytest.raises(notifier.EmailSendError) as raised:
        notifier.send_email("Тема", "<p>x</p>", to=[ADDRESS])

    failure = raised.value
    assert type(failure) is notifier.EmailSendError  # отказом сервера не притворяется
    assert "@" not in _everything_printed(failure), _everything_printed(failure)
    assert failure.__cause__ is None and failure.__context__ is None
    assert "@" not in repr(logs), logs

    # Диагностика: класс исходной ошибки, её текст и строка, где письмо остановилось.
    assert re.fullmatch(re.escape(shown) + _RAISED_AT, str(failure)), str(failure)
    assert notifier.delivery_error_fields(failure) == fields


def test_a_failure_while_closing_does_not_drag_the_refusal_along(smtp_env, monkeypatch):
    """Ошибка, возникшая при закрытии соединения после отказа, несёт отказ
    своим контекстом — и трассировка печатает его вместе с адресом."""
    refusal = smtplib.SMTPRecipientsRefused({ADDRESS: (550, SMTP_REPLY.encode())})
    closing = OSError(32, "Broken pipe")
    monkeypatch.setattr("smtplib.SMTP", _smtp_that_fails_with(refusal, closing=closing))

    # Тест не слеп: под `send_email` у этой ошибки адрес в трассировке есть.
    with pytest.raises(OSError) as raw:
        notifier._send_email("Тема", "<p>x</p>", None, [ADDRESS])
    assert ADDRESS in "".join(traceback.format_exception(raw.value))

    with pytest.raises(notifier.EmailSendError) as raised:
        notifier.send_email("Тема", "<p>x</p>", to=[ADDRESS])

    failure = raised.value
    assert "@" not in _everything_printed(failure), _everything_printed(failure)
    assert failure.__cause__ is None and failure.__context__ is None
    assert notifier.delivery_error_fields(failure) == {"error_type": "BrokenPipeError", "errno": 32}


def test_a_failure_that_cannot_be_printed_leaves_send_email_clean(smtp_env, monkeypatch):
    """Ошибку не удалось даже превратить в текст — исходная наружу всё равно не идёт."""

    class Unprintable(Exception):
        def __str__(self):
            raise RuntimeError(f"no text for {ADDRESS}")

    monkeypatch.setattr("smtplib.SMTP", _smtp_that_fails_with(Unprintable()))

    with pytest.raises(notifier.EmailSendError) as raised:
        notifier.send_email("Тема", "<p>x</p>", to=[ADDRESS])

    failure = raised.value
    assert str(failure) == "Unprintable"
    assert "@" not in _everything_printed(failure), _everything_printed(failure)
    assert failure.__cause__ is None and failure.__context__ is None


def test_a_missing_setting_leaves_send_email_as_a_send_error(smtp_env, monkeypatch):
    """Сбой до разговора с сервером — та же ошибка, с тем же классом в журнале."""
    monkeypatch.delenv("SMTP_PASSWORD")

    with pytest.raises(notifier.EmailSendError) as raised:
        notifier.send_email("Тема", "<p>x</p>", to=[ADDRESS])

    assert re.fullmatch(r"KeyError: 'SMTP_PASSWORD'" + _RAISED_AT, str(raised.value))
    assert notifier.delivery_error_fields(raised.value) == {"error_type": "KeyError"}


def test_an_interrupt_passes_through_send_email_without_the_refusal(smtp_env, monkeypatch):
    """Прерывание — не сбой отправки, оно проходит как есть. Но пришедшее при
    закрытии соединения после отказа несёт отказ своим контекстом."""
    refusal = smtplib.SMTPRecipientsRefused({ADDRESS: (550, SMTP_REPLY.encode())})
    fake = _smtp_that_fails_with(refusal, closing=KeyboardInterrupt())
    monkeypatch.setattr("smtplib.SMTP", fake)

    # Тест не слеп: под `send_email` адрес в трассировке прерывания есть.
    with pytest.raises(KeyboardInterrupt) as raw:
        notifier._send_email("Тема", "<p>x</p>", None, [ADDRESS])
    assert ADDRESS in "".join(traceback.format_exception(raw.value))

    with pytest.raises(KeyboardInterrupt) as raised:
        notifier.send_email("Тема", "<p>x</p>", to=[ADDRESS])
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert "@" not in _everything_printed(raised.value)


def test_an_interrupt_while_the_failure_is_cleaned_carries_no_refusal(smtp_env, monkeypatch):
    """То же, если прерывание пришло, пока `send_email` готовила замену ошибке."""
    refusal = smtplib.SMTPRecipientsRefused({ADDRESS: (550, SMTP_REPLY.encode())})
    monkeypatch.setattr("smtplib.SMTP", _smtp_that_fails_with(refusal))

    def interrupted(exc):
        raise KeyboardInterrupt from exc

    monkeypatch.setattr(notifier, "_send_failure", interrupted)

    with pytest.raises(KeyboardInterrupt) as raised:
        notifier.send_email("Тема", "<p>x</p>", to=[ADDRESS])
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert "@" not in _everything_printed(raised.value)


class _SmtplibWithoutANetwork(smtplib.SMTP):
    """Настоящий smtplib, у которого отнята только сеть: письмо собирает и
    укладывает в байты пакет `email`, как при настоящей отправке."""

    def __init__(self, *args, **kwargs):
        super().__init__()  # без адреса сервера smtplib не соединяется

    def __exit__(self, *args):
        return False

    def starttls(self, *args, **kwargs):
        pass

    def login(self, *args, **kwargs):
        pass

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, name):
        return False

    def sendmail(self, *args, **kwargs):
        return {}


@pytest.mark.parametrize(
    "record",
    [
        # На Python 3.12.3 (он стоит на проде) эта запись даёт `HeaderWriteError`
        # с заголовком `To:` целиком — вместе с адресами двух соседей по списку.
        "[\\,группа:",
        ":",
        ".",
        "=?utf-8?q?=0A",
        "я",
    ],
)
def test_a_broken_recipient_record_never_brings_the_others_out(smtp_env, monkeypatch, record):
    """`recipient add` адрес не проверяет, а письмо на весь список одно: кривая
    запись ломает его сборку, и что об этом скажет пакет `email`, зависит от
    версии Python. Что бы ни сказал — адреса соседей наружу не выходят.

    На 3.12.3 адреса несёт только ошибка от первой записи (её отдельно держит
    тест ниже); остальные — на случай версии, где с адресами выйдет другая."""
    monkeypatch.setattr("smtplib.SMTP", _SmtplibWithoutANetwork)
    recipients = [ADDRESS, record, "second@client.example"]

    try:
        assert notifier.send_email("Тема", "<p>x</p>", to=recipients) is True
    except notifier.EmailSendError as failure:
        assert "@" not in _everything_printed(failure), _everything_printed(failure)
        assert failure.__cause__ is None and failure.__context__ is None


def test_a_real_assembly_error_keeps_the_neighbours_addresses_inside(smtp_env, monkeypatch):
    """Не выдуманная ошибка: на Python 3.12.3 (он стоит на проде) запись
    `[\\,группа:` даёт `HeaderWriteError` с заголовком `To:` целиком. Тест выше
    на остальных записях проходит и без очистки — различает случаи этот."""
    monkeypatch.setattr("smtplib.SMTP", _SmtplibWithoutANetwork)
    recipients = [ADDRESS, "[\\,группа:", "second@client.example"]

    try:
        notifier._send_email("Тема", "<p>x</p>", None, recipients)
        raw = ""
    except Exception as error:
        raw = str(error)
    if "second@client.example" not in raw:
        pytest.skip("этот Python на такую запись адресами соседей не отвечает: пример устарел")

    with pytest.raises(notifier.EmailSendError) as raised:
        notifier.send_email("Тема", "<p>x</p>", to=recipients)
    assert "<текст скрыт>" in str(raised.value)
    assert "@" not in _everything_printed(raised.value), _everything_printed(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None


_NOTIFIER = "src/notifier.py"


# Чем smtplib открывает разговор с почтовым сервером.
_MAIL_SERVER_CLASSES = {"SMTP", "SMTP_SSL", "LMTP"}


def mail_server_bypasses(sources: dict[str, str]) -> list[str]:
    """Кто ходит к почтовому серверу мимо `send_email`.

    Сверка по именам: smtplib импортирует только `notifier`; класс сервера
    (`SMTP`, `SMTP_SSL`, `LMTP`) назван только в его функции `_send_email`; сама
    `_send_email` названа только в его функции `send_email`. Имя строкой
    (`getattr(notifier, "_send_email")`, `import_module("smtplib")`) — тоже имя.

    Чего проверка не видит: имя, собранное из частей (`"_send" + "_email"`),
    класс сервера под другим именем (`from smtplib import SMTP_SSL as Server`
    в `notifier`) и другой транспорт — HTTP API почтового сервиса, сторонний
    SMTP-клиент. Своё имя `SMTP` (значение перечисления) она примет за класс
    сервера.
    """
    found = []
    for path, source in sources.items():
        tree = ast.parse(source)
        # Функции самого модуля `notifier`: метод с тем же именем — уже не они.
        entry = (
            {node: node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
            if path == _NOTIFIER
            else {}
        )
        for node, owner in _nodes_with_owner(tree):
            inside = entry.get(owner)
            if isinstance(node, ast.Import | ast.ImportFrom):
                named = {alias.name for alias in node.names}
                if isinstance(node, ast.ImportFrom):
                    named.add(node.module or "")
                bypass = "_send_email" in named or (
                    path != _NOTIFIER and any(name.split(".")[0] == "smtplib" for name in named)
                )
            elif isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                bypass = (name == "_send_email" and inside != "send_email") or (
                    name in _MAIL_SERVER_CLASSES and inside != "_send_email"
                )
            elif isinstance(node, ast.Constant):
                bypass = (node.value == "_send_email" and inside != "send_email") or (
                    node.value == "smtplib"
                )
            else:
                continue
            if bypass:
                found.append(f"{path}:{node.lineno}: {ast.unparse(node)}")
    return found


def _sources_that_could_send_mail() -> dict[str, str]:
    """`src/` и `scripts/`: скрипты зовут из workflow наравне с командами CLI."""
    scripts = SRC.parent / "scripts"
    return _src_sources() | {
        str(path.relative_to(SRC.parent)): path.read_text(encoding="utf-8")
        for path in sorted(scripts.rglob("*.py"))
    }


def test_only_send_email_talks_to_the_mail_server():
    """На этом стоит весь слой 2: ошибка, взятая мимо `send_email`, не очищена."""
    sources = _sources_that_could_send_mail()
    found = mail_server_bypasses(sources)
    # Python, встроенный в workflow, как код не разобрать — те же имена ищутся
    # в его тексте.
    found += [
        f".github/workflows/{path.name}: {name}"
        for path in sorted((SRC.parent / ".github" / "workflows").glob("*.y*ml"))
        for name in sorted(set(re.findall(r"\b(?:smtplib|_send_email)\b", path.read_text("utf-8"))))
    ]
    assert found == [], _HOW_TO_FIX_A_BYPASS.format(found="; ".join(found))
    # Проверка не слепа: скрипты она читает, а тот же `notifier.py` под чужим
    # именем — уже обход.
    assert any(path.startswith("scripts/") for path in sources), sorted(sources)[:5]
    elsewhere = mail_server_bypasses({"src/elsewhere.py": sources[_NOTIFIER]})
    assert any("import smtplib" in line for line in elsewhere), elsewhere
    assert any("smtplib.SMTP" in line for line in elsewhere), elsewhere


@pytest.mark.parametrize(
    ("path", "source", "bypasses"),
    [
        ("src/digest.py", "import smtplib", 1),
        ("src/digest.py", "import ssl, smtplib as mail", 1),
        ("src/digest.py", "from smtplib import SMTP", 1),
        ("src/digest.py", "notifier._send_email(subject, html, None, None)", 1),
        ("src/digest.py", "from src.notifier import _send_email", 1),
        ("src/digest.py", "send = notifier._send_email", 1),
        ("src/digest.py", "send = getattr(notifier, '_send_email')", 1),
        # smtplib, взятый у `notifier`, и smtplib, импортированный по строке.
        ("src/digest.py", "from src.notifier import smtplib", 1),
        ("src/digest.py", "notifier.smtplib.SMTP(host, 587)", 1),
        ("src/digest.py", "mail = importlib.import_module('smtplib')", 1),
        # В самом `notifier` — тоже: второй отправитель рядом с первым.
        (_NOTIFIER, "def resend():\n    return _send_email('x', 'y', None, None)", 1),
        (_NOTIFIER, "def send_invite():\n    with smtplib.SMTP_SSL(host) as smtp: ...", 1),
        # Метод с именем входа — не сам вход.
        (
            _NOTIFIER,
            "class Mailer:\n    def send_email(self):\n        return _send_email('x', 'y')",
            1,
        ),
        ("scripts/mail_probe.py", "import smtplib", 1),
        (_NOTIFIER, "def _send_email():\n    with smtplib.SMTP(host) as smtp: ...", 0),
        (
            _NOTIFIER,
            "import smtplib\n\ndef send_email():\n    return _send_email()\n\n"
            "def _send_email(): ...",
            0,
        ),
        ("src/digest.py", "notifier.send_email(subject='x', html_body='y')", 0),
        # Чужая функция с похожим именем.
        ("src/alerts.py", "def _send_email_alert(event): ...\n\n_send_email_alert(event)", 0),
        ("src/digest.py", "import smtplib_stub", 0),
    ],
)
def test_the_code_check_sees_a_way_past_send_email(path, source, bypasses):
    assert len(mail_server_bypasses({path: source})) == bypasses


def test_delivery_error_fields_never_carry_the_error_text():
    raw = smtplib.SMTPRecipientsRefused({ADDRESS: (550, SMTP_REPLY.encode())})
    assert notifier.delivery_error_fields(raw) == {"error_type": "SMTPRecipientsRefused"}
    assert notifier.delivery_error_fields(smtplib.SMTPDataError(554, SMTP_REPLY.encode())) == {
        "error_type": "SMTPDataError",
        "smtp_code": 554,
    }


def test_delivery_error_fields_keep_what_tells_network_failures_apart():
    assert notifier.delivery_error_fields(TimeoutError("timed out")) == {
        "error_type": "TimeoutError"
    }
    assert notifier.delivery_error_fields(OSError(101, "Network is unreachable")) == {
        "error_type": "OSError",
        "errno": 101,
    }
    assert notifier.delivery_error_fields(ConnectionRefusedError(111, "Connection refused")) == {
        "error_type": "ConnectionRefusedError",
        "errno": 111,
    }
    assert notifier.delivery_error_fields(KeyError("SMTP_USER")) == {"error_type": "KeyError"}


def test_run_failure_output_carries_no_address(cli_logging, db_session, monkeypatch):
    """Сбой сбора печатают двое: журнал (`run_failed` с трассировкой) и сам click."""
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)

    async def refused(*args, **kwargs):
        # Только запись адреса, которую маска знает: трассировку печатает
        # журнал, а на журнале маска. Запись, которую она не узнаёт, прошла бы
        # в трассировке как есть — это известный предел слоя 3, не этого теста.
        raise RuntimeError(_SMTPLIB_TEXT)

    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: factory)
    monkeypatch.setattr(main, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main.watchlist, "categories_for_site", lambda session, site, only_category_id=None: ["cat"]
    )
    monkeypatch.setattr(main, "baselines_for_sites", lambda *args: {"aloe": None})
    monkeypatch.setattr(main, "scrape_all", refused)

    result = CliRunner().invoke(
        main.cli, ["run", "--site", "aloe", "--mode", "category", "--no-alerts", "--force"]
    )

    assert result.exit_code != 0
    assert "run_failed" in result.output and "Traceback" in result.output
    assert "Error: <текст скрыт>" in result.output
    assert "@" not in result.output, result.output


def _refusal() -> Exception:
    return smtplib.SMTPRecipientsRefused({ADDRESS: (550, SMTP_REPLY.encode())})


def _bad_header() -> Exception:
    return email.errors.HeaderParseError(f"cannot fold b'To: {ADDRESS}'")


def _fail_to_record(*args, **kwargs):
    raise RuntimeError(f"cannot record the alert sent to {ADDRESS}")


@pytest.mark.parametrize(
    ("smtp_error", "write_state", "reason"),
    [
        # Отказ сервера и сбой сборки письма: чисты уже на выходе из `send_email`.
        (_refusal, None, "SMTPRecipientsRefused (SMTP 550 5.1.1)"),
        (_bad_header, None, "HeaderParseError: <текст скрыт> (notifier.py:"),
        # Письмо ушло, а упало то, что стоит в том же `try` после отправки: эту
        # ошибку `send_email` не видела, её чистит сама команда.
        (None, _fail_to_record, "<текст скрыт>"),
    ],
)
def test_health_check_output_carries_no_address(
    cli_logging, smtp_env, monkeypatch, tmp_path, smtp_error, write_state, reason
):
    """Команда печатает через `click.echo`, мимо маски журнала, — и причину, по
    которой письмо о тревоге не ушло, и текст ошибки прогона, как он записан в
    базу (`runs.error_message`)."""
    stored = f"Последний прогон #7 завершился со статусом failed: IntegrityError: {ADDRESS}"
    report = health.HealthReport(
        status="critical", issues=[health.HealthIssue("critical", "last_run_failed", stored)]
    )
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: lambda: nullcontext(object()))
    monkeypatch.setattr(health, "check_health", lambda *args, **kwargs: report)
    monkeypatch.setattr(notifier, "resolve_recipients", lambda explicit=None: [ADDRESS])
    fake = (
        _smtp_that_fails_with(smtp_error())
        if smtp_error
        else _smtp_that_fails_with(RuntimeError(), at="never")
    )
    monkeypatch.setattr("smtplib.SMTP", fake)
    if write_state is not None:
        monkeypatch.setattr(main, "_write_health_alert_state", write_state)

    result = CliRunner().invoke(
        main.cli,
        ["health-check", "--alert-email", "--alert-state-file", str(tmp_path / "health.json")],
    )

    assert result.exit_code == 2, result.output
    assert "[critical] last_run_failed: <текст скрыт>" in result.output, result.output
    assert f"Не удалось отправить health email: {reason}" in result.output, result.output
    assert "@" not in result.output, result.output


@pytest.mark.parametrize(
    ("smtp_error", "line"),
    [
        (
            _refusal,
            "email:    FAIL — SMTPRecipientsRefused (SMTP 550 5.1.1): "
            "5.1.1 <<address>>: Recipient address rejected",
        ),
        (_bad_header, "email:    FAIL — HeaderParseError: <текст скрыт> (notifier.py:"),
    ],
)
def test_notify_test_output_carries_no_address(
    cli_logging, smtp_env, monkeypatch, smtp_error, line
):
    """То же у проверки доставки — с настоящим `send_email`, а не подменённым."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr("smtplib.SMTP", _smtp_that_fails_with(smtp_error()))

    result = CliRunner().invoke(main.cli, ["notify", "test", "--email", ADDRESS])

    assert result.exit_code == 0, result.output
    assert line in result.output, result.output
    assert "@" not in result.output, result.output


# ─── Слой 3: вывод журнала CLI ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        (f"user={ADDRESS}", "user=<address>"),
        (f"recipients=['{ADDRESS}', 'b@c.example']", "recipients=['<address>', '<address>']"),
        (
            f"{{'{ADDRESS}': (550, b'{SMTP_REPLY}')}}",
            "{'<address>': (550, b'5.1.1 <<address>>: Recipient address rejected')}",
        ),
        ("first.last+tag@mail.client-name.example", "<address>"),
        ("почта@пример.рф", "<address>"),
        # Так адрес выглядит в JSON-выводе: не-ASCII записано \uXXXX, перевод
        # строки — \n. Буква «n» от него в адрес не входит.
        (json.dumps({"user": "почта@пример.рф"}), '{"user": "<address>"}'),
        (json.dumps(f"отказ\n{ADDRESS}"), json.dumps("отказ\n<address>")),
    ],
)
def test_mask_replaces_every_address(text, masked):
    assert logging_setup.mask_addresses(text) == masked


@pytest.mark.parametrize(
    "text",
    [
        "run_finished products=9424 run_id=980 status=ok",
        "scrape_using_proxy proxy=http://***@gate.example.com:7000",
        "ssh pm@13.140.186.143",
        '{"@type": "Product", "@context": "https://schema.org"}',
        "admin@local",
        "",
    ],
)
def test_mask_leaves_what_is_not_an_address(text):
    assert logging_setup.mask_addresses(text) == text


def test_mask_stays_linear_on_a_long_line_without_spaces():
    # Маска стоит на пути каждой строки журнала сбора. Без ограничения длины
    # имени такая строка разбирается квадратично — десятки секунд вместо сотых.
    line = "payload=" + "a" * 20_000 + f" user={ADDRESS}"
    started = time.perf_counter()
    masked = logging_setup.mask_addresses(line)
    assert time.perf_counter() - started < 5
    assert masked.endswith("user=<address>")


def test_mask_keeps_a_json_line_valid():
    """В JSON-режиме маска правит готовую строку: одинокая `\\` её бы сломала."""
    pieces = ["\\", "u", "n", '"', "'", "a", "0", "f", "@", ".", "-", " ", "я", "\n", "x@y.zz"]
    pieces += ["\\u0041", "client.example"]
    rng = random.Random(7)
    for _ in range(20_000):
        raw = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 14)))
        masked = logging_setup.mask_addresses(json.dumps({"value": raw}))
        assert isinstance(json.loads(masked)["value"], str), raw


def test_masking_passes_through_what_a_renderer_did_not_turn_into_text():
    event = {"event": "x", "user": ADDRESS}
    assert logging_setup.masking(lambda *_: event)(None, "info", event) is event


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("timed out", "timed out"),
        ("", ""),
        (f"relay refused {ADDRESS}", "<текст скрыт>"),
        # То, от чего маска оставила бы начало имени, имя рядом с адресом или всё.
        ("relay refused o'brien@client.example", "<текст скрыт>"),
        (f"Иван Петров <{ADDRESS}>", "<текст скрыт>"),
        ('"viewer name"@client.example', "<текст скрыт>"),
        ("proxy http://user:secret@gate.example.com:7000 refused", "<текст скрыт>"),
    ],
)
def test_text_printed_past_the_log_mask_is_withheld_if_it_has_an_at_sign(text, shown):
    """Строку журнала выбросить нельзя — на ней маска. Текст, который печатают
    мимо журнала (`click.echo`, «Error: …», ошибка отправки), можно не показать."""
    assert logging_setup.without_addresses(text) == shown


def _configure_cli(monkeypatch, as_json: bool) -> None:
    if as_json:
        monkeypatch.setenv("PHARMACY_LOG_JSON", "1")
    else:
        monkeypatch.delenv("PHARMACY_LOG_JSON", raising=False)
    main._setup_logging("INFO")


def _configure_shared(monkeypatch, as_json: bool) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json" if as_json else "console")
    monkeypatch.delenv("LOG_FILE", raising=False)
    logging_setup.configure_logging(service="test")


@pytest.mark.parametrize("configure", [_configure_cli, _configure_shared])
@pytest.mark.parametrize("as_json", [False, True])
def test_log_output_never_prints_an_address(cli_logging, monkeypatch, capsys, configure, as_json):
    configure(monkeypatch, as_json)
    log = structlog.get_logger()

    # То, что слои 1 и 2 должны были не пропустить: поле с адресом, список,
    # объект с адресом в repr и трассировка с текстом ошибки smtplib.
    log.info("smtp_send", host="smtp.example.test", recipients=[ADDRESS])
    log.warning("email_batch_failed", user=ADDRESS, row={"email": ADDRESS})
    log.warning("digest_email_failed", user="почта@пример.рф", note=f"отказ\n{ADDRESS}")
    try:
        raise smtplib.SMTPRecipientsRefused({ADDRESS: (550, SMTP_REPLY.encode())})
    except smtplib.SMTPException:
        log.exception("run_failed", run_id=980)

    output = capsys.readouterr().out
    lines = output.strip().splitlines()
    assert "@" not in output, output
    assert output.count(logging_setup.ADDRESS_MASK) >= 7
    assert "smtp_send" in lines[0] and "email_batch_failed" in output and "run_failed" in output
    assert "SMTPRecipientsRefused" in output
    if as_json:
        assert [json.loads(line)["event"] for line in lines] == [
            "smtp_send",
            "email_batch_failed",
            "digest_email_failed",
            "run_failed",
        ]


def _stdlib_logging_of_the_cli(monkeypatch, tmp_path) -> Path:
    main._setup_logging("INFO")
    return tmp_path / "logs" / "app.jsonl"


def _stdlib_logging_shared(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("LOG_FORMAT", "console")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "shared.jsonl"))
    logging_setup.configure_logging(service="test")
    return tmp_path / "shared.jsonl"


@pytest.mark.parametrize("configure", [_stdlib_logging_of_the_cli, _stdlib_logging_shared])
def test_third_party_logging_never_prints_an_address(
    cli_logging, monkeypatch, capsys, tmp_path, configure
):
    """Сторонние библиотеки пишут через stdlib `logging` — мимо structlog и его маски."""
    log_file = configure(monkeypatch, tmp_path)
    library = logging.getLogger("some.library")

    library.warning("550 <%s>: Recipient address rejected", ADDRESS)
    try:
        raise smtplib.SMTPRecipientsRefused({ADDRESS: (550, SMTP_REPLY.encode())})
    except smtplib.SMTPException:
        library.exception("delivery to %s failed", "почта@пример.рф")
    # Сообщение не сошлось с аргументами: `logging` такую запись печатает сам,
    # мимо формата и вместе с аргументами.
    library.warning("sent to %s and %s", ADDRESS)

    for handler in logging.getLogger().handlers:
        handler.flush()
    captured = capsys.readouterr()
    # Оба корневых обработчика: поток и файл.
    for written in (captured.out + captured.err, log_file.read_text(encoding="utf-8")):
        assert "@" not in written, written
        assert "550 <<address>>: Recipient address rejected" in written
        assert "delivery to <address> failed" in written
        assert "SMTPRecipientsRefused" in written and "Traceback" in written
        assert "unformattable log record from some.library" in written
        assert "'sent to %s and %s' % ('<address>',)" in written
        assert "Logging error" not in written
