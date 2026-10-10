#!/usr/bin/env python3
"""Что `.env` рядом с кодом меняет на сервере — имена, без значений.

Настройки сервера живут в `/etc/pharmacy-monitor/env` (systemd `EnvironmentFile`).
Но рядом с кодом может лежать `.env`, и CLI его читает: до правки 2026-10-10 —
поиском вверх по каталогам и поверх окружения процесса, после неё — только из
корня своего чекаута и только для имён, которых в окружении нет. Скрипт называет
имена из такого файла и про каждое говорит, как оно соотносится с файлом секретов:

    same     значение то же
    differs  значение другое
    absent   в файле секретов такого имени нет
    unsure   сравнить нельзя: значение разные читатели поймут по-разному

Ответ `same` и `differs` даётся, только когда оба значения — печатные знаки ASCII
без обратной косой черты, `$`, `#` и кавычек внутри, а в файле секретов ещё и без
кавычек вокруг, без пробелов и CR по краям строки: systemd их снимает, а цикл,
которым файл читают workflow, — нет.
Всё остальное — `unsure`. Строка с незакрытой кавычкой или с обратной косой чертой
в конце останавливает чтение файла: дальше не понять, где имя, а где продолжение
значения, — об этом сказано отдельной строкой, и остаток файла смотрят глазами.

Ещё скрипт говорит: какому правилу следует выложенный `src/main.py`; называет ли
`.env` какой-нибудь юнит своим `EnvironmentFile`; какие из имён задаёт строка
`Environment=` юнита (файл секретов её не знает, а после выкладки правила она
сильнее `.env`).

Только чтение. Значений не печатает, в том числе при ошибке — о сбое сказано
классом исключения. Имя печатается, если оно записано ПРОПИСНЫМИ и в нём есть
подчёркивание либо такое же имя стоит в файле секретов; остальные строки только
считаются. Причина: хвост оборванного значения (`…==` на своей строке) для любого
разбора тоже выглядит как `ИМЯ=значение`. Предел, который остаётся: обрывок из
одних прописных букв, цифр и подчёркиваний от имени не отличить — незнакомое имя
в ответе может оказаться им.

Сторонних пакетов не нужно, поэтому запускается системным python3 от имени pm —
не от root и не интерпретатором из каталога, куда pm пишет. `scripts/` выкладка на
сервер не возит: текст подаётся через stdin, из корня чекаута на dev-боксе:

    ssh root@13.140.186.143 'runuser -u pm -- python3 -I -' < scripts/diag_env_sources.py

`--flag ИМЯ` после `-` говорит про переключатель из файла секретов: `unset`,
`empty`, одно из служебных слов (`1`, `0`, `true`, `false`, `yes`, `no`, `on`,
`off`, `required`) либо `set (value not shown)`. Что из этого значит «включено»,
решает код, который переключатель читает, — у разных переключателей по-разному.

Не для workflow и не для PR: журнал шага и репозиторий публичны, а перечень имён
переменных сервера там ни к чему.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = "/opt/pharmacy-monitor"
DEFAULT_SERVER_ENV = "/etc/pharmacy-monitor/env"
DEFAULT_UNITS = "/etc/systemd/system"

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UPPER_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_SWITCH_WORDS = frozenset({"1", "0", "true", "false", "yes", "no", "on", "off", "required"})
_QUOTES = "'\""
_READ_DIFFERENTLY = "\\$#`"
_UNIT_SUFFIXES = {".service", ".conf"}
_DOTENV_DIST = re.compile(r"python_dotenv-(\d+(?:\.\d+)*)\.dist-info")

# Прежний вызов в `src/main.py` — отдельной строкой с начала строки. Тот же
# шаблон стоит в RUNBOOK для ручной проверки `grep -c`.
OLD_CALL = re.compile(r"^load_dotenv\(override=True\)", re.MULTILINE)
NEW_CALL = "load_dotenv(Path(__file__).resolve().parent.parent"


def _ascii(text: object) -> str:
    """Строка отчёта: всё, кроме печатных знаков ASCII, — в виде \\xNN."""
    return "".join(
        ch if " " <= ch <= "~" else ch.encode("unicode_escape").decode("ascii") for ch in str(text)
    )


def _clean_pair(value: str) -> bool:
    """Значение в одной паре кавычек, внутри которой нет ни кавычек, ни `\\`."""
    return (
        len(value) >= 2
        and value[0] in _QUOTES
        and value[-1] == value[0]
        and not any(mark in value[1:-1] for mark in _QUOTES + "\\")
    )


def _stops_line_reading(value: str, *, dotenv: bool) -> bool:
    """После такого значения строки файла уже нельзя читать по одной.

    python-dotenv: значение, начатое кавычкой, может идти на несколько строк.
    systemd: кавычка открывается и посреди значения, а обратная косая черта в
    конце строки приклеивает следующую.
    """
    if dotenv:
        return value.startswith(tuple(_QUOTES)) and not _clean_pair(value)
    if value.endswith("\\"):
        return True
    return any(mark in value for mark in _QUOTES) and not _clean_pair(value)


def _comparable(value: str, *, quotes_are_stripped: bool) -> str | None:
    """Значение, которое все читатели файла получат одинаковым; иначе None."""
    if _clean_pair(value):
        if not quotes_are_stripped:
            return None
        value = value[1:-1]
    if any(not " " <= ch <= "~" or ch in _READ_DIFFERENTLY + _QUOTES for ch in value):
        return None
    return value


@dataclass
class EnvFile:
    values: dict[str, str | None] = field(default_factory=dict)  # None — есть, но несравнимо
    skipped: int = 0  # строки до остановки, которые именем не стали
    stopped_at: int = 0  # номер строки, на которой чтение остановилось; 0 — дочитан
    left_unread: int = 0  # непустые строки после остановки
    valid_utf8: bool = True


def read_assignments(path: Path, *, dotenv: bool) -> EnvFile:
    """Имена файла. `dotenv` — правила python-dotenv, иначе правила systemd.

    Различаются они тут в трёх местах: слово `export` перед именем (python-dotenv
    его принимает, systemd строку пропускает), комментарий с `;` (только systemd)
    и кавычки вокруг значения (см. `_comparable`).
    """
    result = EnvFile()
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        result.valid_utf8 = False
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip(" \t\r")
        if result.stopped_at:
            result.left_unread += bool(line)
            continue
        if not line or line[0] in ("#" if dotenv else "#;"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(" \t"), value.strip(" \t")
        if dotenv and name[:6] == "export" and name[6:7] in (" ", "\t"):
            name = name[6:].strip(" \t")
        named = bool(separator) and bool(_NAME.fullmatch(name))
        if separator and _stops_line_reading(value, dotenv=dotenv):
            result.stopped_at = number
            if named:
                result.values[name] = None
            continue
        if not named:
            result.skipped += 1
            continue
        if not dotenv and raw != f"{name}={value}":
            # Пробел или CR по краям systemd отбросит, а цикл workflow оставит в значении.
            result.values[name] = None
            continue
        result.values[name] = _comparable(value, quotes_are_stripped=dotenv)
    return result


def classify_switch(values: dict[str, str | None], name: str) -> str:
    if name not in values:
        return "unset"
    value = values[name]
    if value == "":
        return "empty"
    if value is not None and value.lower() in _SWITCH_WORDS:
        return value.lower()
    return "set (value not shown)"


def _owner(st: os.stat_result) -> str:
    try:
        import grp
        import pwd

        return f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name}"
    except (ImportError, KeyError):
        return f"{st.st_uid}:{st.st_gid}"


def _candidate(path: Path) -> tuple[str, bool]:
    """Что лежит по адресу и можно ли это сравнивать."""
    try:
        link = path.lstat()
    except FileNotFoundError:
        return "-", False
    except OSError as exc:
        return f"cannot look ({type(exc).__name__})", False
    try:
        target = path.stat()
    except OSError as exc:
        return f"FOUND, but cannot follow it ({type(exc).__name__})", False
    if not stat.S_ISREG(target.st_mode):
        return "FOUND, but it is not a regular file", False
    modified = time.strftime("%Y-%m-%d", time.gmtime(target.st_mtime))
    kind = "FOUND (symlink)" if stat.S_ISLNK(link.st_mode) else "FOUND"
    return (
        f"{kind}  mode={stat.S_IMODE(target.st_mode):o} owner={_owner(target)} "
        f"size={target.st_size} modified={modified}"
    ), True


def search_path(root: Path) -> list[Path]:
    """Где вызов `load_dotenv()` без пути искал `.env`, считая от выложенного кода.

    От `<root>/src` вверх — так шёл прежний `src/main.py`. `<root>/migrations` —
    оттуда начинает `migrations/env.py`, который выкладка не обновляет.
    """
    start = root / "src"
    return [
        start / ".env",
        *(parent / ".env" for parent in start.parents),
        root / "migrations/.env",
    ]


def deployed_rule(root: Path) -> str:
    try:
        source = (root / "src/main.py").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"cannot read ({type(exc).__name__})"
    if OLD_CALL.search(source):
        return "OLD rule - searches upward, the file beats the environment"
    if NEW_CALL in source:
        return "new rule - own checkout root only, the environment wins"
    return "neither call found - read it by hand"


def dotenv_version(root: Path) -> str:
    try:
        found = sorted(root.glob(".venv/lib/python*/site-packages/python_dotenv-*.dist-info"))
    except OSError as exc:
        return f"unknown ({type(exc).__name__})"
    versions = [match.group(1) for path in found if (match := _DOTENV_DIST.fullmatch(path.name))]
    return versions[-1] if versions else "not found"


@dataclass
class UnitScan:
    references: list[str] = field(default_factory=list)  # юниты, чей EnvironmentFile — такой .env
    setters: dict[str, list[str]] = field(default_factory=dict)  # имя -> юниты с Environment=
    unreadable: int = 0
    problem: str = ""


def scan_units(units: Path, env_files: set[str], names: set[str]) -> UnitScan:
    """Юниты, которые называют `.env` своим файлом окружения или сами задают его имена."""
    scan = UnitScan()
    if not units.is_dir():
        scan.problem = "not a directory"
        return scan
    try:
        candidates = sorted(path for path in units.rglob("*") if path.suffix in _UNIT_SUFFIXES)
    except OSError as exc:
        scan.problem = f"cannot list ({type(exc).__name__})"
        return scan
    seen: set[str] = set()
    for path in candidates:
        real = os.path.realpath(path)
        if real in seen or not os.path.isfile(real):
            continue
        seen.add(real)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            scan.unreadable += 1
            continue
        relative = path.relative_to(units).as_posix()
        ours = "pharmacy-monitor" in relative or relative.startswith("service.d/")
        for raw in text.replace("\\\n", " ").splitlines():
            key, separator, value = raw.strip().partition("=")
            key = key.strip()
            if not separator:
                continue
            if key == "EnvironmentFile":
                if value.strip().lstrip("-").strip(_QUOTES) in env_files:
                    scan.references.append(str(path))
            elif key == "Environment" and ours:
                for token in value.split():
                    name = token.lstrip(_QUOTES).partition("=")[0]
                    if name in names:
                        scan.setters.setdefault(name, []).append(str(path))
    return scan


def _unread_note(env: EnvFile) -> str:
    return (
        f"not read from line {env.stopped_at} on - an open quote or a line continuation "
        f"({env.left_unread} more lines): read the rest by hand"
    )


def report(root: Path, server_env: Path, units: Path, flags: list[str]) -> tuple[list[str], int]:
    lines = [
        f"python-dotenv in {root / '.venv'}: {dotenv_version(root)}",
        f"deployed {root / 'src/main.py'}: {deployed_rule(root)}",
    ]
    try:
        server = read_assignments(server_env, dotenv=False)
    except OSError as exc:
        lines.append(f"server file {server_env}: cannot read ({type(exc).__name__})")
        return lines, 2
    lines.append(
        f"server file {server_env}: {len(server.values)} names, {server.skipped} other lines"
    )
    if server.stopped_at:
        lines.append(f"  {_unread_note(server)}")
    if not server.valid_utf8:
        lines.append("  NOT valid UTF-8: systemd would refuse this file")

    lines.append("where a path-less load_dotenv() looked, from the deployed code:")
    candidates = search_path(root)
    found: list[Path] = []
    for candidate in candidates:
        description, comparable = _candidate(candidate)
        lines.append(f"  {candidate}  {description}")
        if comparable:
            found.append(candidate)
    if not found:
        lines.append("no .env next to the code or above it: nothing to compare")

    code = 0
    shown: set[str] = set()
    for path in found:
        try:
            env = read_assignments(path, dotenv=True)
        except OSError as exc:
            lines.append(f"{path}: cannot read ({type(exc).__name__})")
            code = 3
            continue
        printable = sorted(
            name
            for name in env.values
            if _UPPER_NAME.fullmatch(name) and ("_" in name or name in server.values)
        )
        lines.append(f"names in {path} ({len(printable)}), against the server file:")
        width = max((len(name) for name in printable), default=0)
        for name in printable:
            mine, theirs = env.values[name], server.values.get(name)
            if name not in server.values:
                # После остановки файл секретов не дочитан: имя может стоять ниже.
                status = "unsure" if server.stopped_at else "absent"
            elif mine is None or theirs is None:
                status = "unsure"
            else:
                status = "same" if mine == theirs else "differs"
            lines.append(f"  {name.ljust(width)}  {status}")
        lines.append(f"  other lines, not shown: {env.skipped + len(env.values) - len(printable)}")
        if env.stopped_at:
            lines.append(f"  {_unread_note(env)}")
        if not env.valid_utf8:
            lines.append("  NOT valid UTF-8: python-dotenv would fail on this file")
        shown.update(printable)

    in_tree = {str(path) for path in candidates if root in path.parents}
    scan = scan_units(units, in_tree | {str(path) for path in found}, shown)
    if scan.problem:
        lines.append(f"units under {units}: {scan.problem}")
    else:
        lines.append(f"units under {units} whose EnvironmentFile is a .env next to the code:")
        lines.extend(f"  {path}" for path in scan.references or ["none"])
        if shown:
            lines.append("names above that a pharmacy-monitor unit sets with Environment=:")
            if scan.setters:
                for name in sorted(scan.setters):
                    lines.extend(f"  {name}  {path}" for path in scan.setters[name])
            else:
                lines.append("  none")
        if scan.unreadable:
            lines.append(f"  unit files this user could not read: {scan.unreadable}")

    if flags:
        lines.append("switches in the server file:")
        lines.extend(f"  {name}  {classify_switch(server.values, name)}" for name in flags)
    return lines, code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="names in a stray .env, without values")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="deployed code directory")
    parser.add_argument("--server-env", default=DEFAULT_SERVER_ENV, help="server secrets file")
    parser.add_argument("--units", default=DEFAULT_UNITS, help="systemd unit directory")
    parser.add_argument(
        "--flag",
        action="append",
        default=[],
        metavar="NAME",
        help="a switch in the server file: say unset / empty / its on-off word",
    )
    args = parser.parse_args(argv)
    lines, code = report(Path(args.root), Path(args.server_env), Path(args.units), args.flag)
    sys.stdout.write("".join(_ascii(line) + "\n" for line in lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
