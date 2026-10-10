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
    unsure   сравнить нельзя: в одном из значений есть знак, который python-dotenv
             и systemd читают по-разному (обратная косая черта, `$`, `#`, кавычка
             внутри значения, значение на несколько строк)

Ещё он говорит: какому правилу следует выложенный `src/main.py`; называет ли
`.env` какой-нибудь юнит своим `EnvironmentFile`; какие из имён задаёт строка
`Environment=` юнита (файл секретов её не знает, а после выкладки правила она
сильнее `.env`).

Только чтение. Значений не печатает нигде, в том числе при ошибке — о сбое сказано
классом исключения. Печатаются только имена, записанные ПРОПИСНЫМИ: хвост
оборванного значения (`…==` на своей строке) для любого разбора тоже выглядит как
`ИМЯ=значение`, и строчные буквы его выдают; такие строки только считаются.
Предел: обрывок из одних прописных букв и цифр от имени не отличить.

Сторонних пакетов не нужно, поэтому запускается системным python3 от имени pm —
не от root и не интерпретатором из каталога, куда pm пишет. `scripts/` выкладка на
сервер не возит: текст подаётся через stdin, из корня чекаута на dev-боксе:

    ssh root@13.140.186.143 'runuser -u pm -- python3 -' < scripts/diag_env_sources.py

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
from pathlib import Path

DEFAULT_ROOT = "/opt/pharmacy-monitor"
DEFAULT_SERVER_ENV = "/etc/pharmacy-monitor/env"
DEFAULT_UNITS = "/etc/systemd/system"

_SHOWN_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_SWITCH_WORDS = frozenset({"1", "0", "true", "false", "yes", "no", "on", "off", "required"})
_READ_DIFFERENTLY = "\\$#'\"`"
_UNIT_SUFFIXES = {".service", ".conf"}

# Прежний вызов в `src/main.py` — отдельной строкой с начала строки. Тот же
# шаблон стоит в RUNBOOK для ручной проверки `grep -c`.
OLD_CALL = re.compile(r"^load_dotenv\(override=True\)", re.MULTILINE)
NEW_CALL = "load_dotenv(Path(__file__).resolve().parent.parent"


def _ascii(text: object) -> str:
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _plain(raw: str) -> str | None:
    """Значение, которое python-dotenv и systemd прочтут одинаково; иначе None."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    if any(mark in value for mark in _READ_DIFFERENTLY):
        return None
    return value


def read_assignments(path: Path, *, export_prefix: bool) -> tuple[dict[str, str | None], int]:
    """Имена файла и число строк, которые именем не стали.

    Значение `None` — «есть, но сравнивать нельзя». `export_prefix` — python-dotenv
    слово `export` перед именем принимает, systemd такую строку пропускает.
    """
    values: dict[str, str | None] = {}
    hidden = 0
    open_quote = ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if open_quote:
            # Продолжение значения в кавычках, открытых строкой выше.
            hidden += 1
            if open_quote in line:
                open_quote = ""
            continue
        if not line or line[0] in "#;":
            continue
        name, separator, value = line.partition("=")
        name = name.strip()
        if export_prefix and name.startswith("export "):
            name = name[len("export ") :].strip()
        if not separator or not _SHOWN_NAME.fullmatch(name):
            hidden += 1
            continue
        value = value.strip()
        if value[:1] in ("'", '"') and value.count(value[0]) == 1:
            # Кавычка открыта и на этой строке не закрыта: значение идёт дальше.
            open_quote = value[0]
            values[name] = None
            continue
        values[name] = _plain(value)
    return values, hidden


def classify_switch(values: dict[str, str | None], name: str) -> str:
    if name not in values:
        return "unset"
    value = values[name]
    if value == "":
        return "empty"
    if value is not None and value.strip().lower() in _SWITCH_WORDS:
        return value.strip().lower()
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
    if not found:
        return "not found"
    return found[-1].name[len("python_dotenv-") : -len(".dist-info")]


def scan_units(
    units: Path, env_files: list[Path], names: set[str]
) -> tuple[list[str], dict[str, list[str]], str]:
    """Юниты, которые называют `.env` своим файлом окружения или сами задают его имена."""
    references: list[str] = []
    setters: dict[str, list[str]] = {}
    wanted = {str(path) for path in env_files}
    seen: set[str] = set()
    if not units.is_dir():
        return references, setters, "not a directory"
    try:
        candidates = sorted(path for path in units.rglob("*") if path.suffix in _UNIT_SUFFIXES)
    except OSError as exc:
        return references, setters, f"cannot list ({type(exc).__name__})"
    for path in candidates:
        try:
            real = os.path.realpath(path)
            if real in seen or not os.path.isfile(real):
                continue
            seen.add(real)
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            key, separator, value = raw.strip().partition("=")
            key = key.strip()
            if not separator:
                continue
            if key == "EnvironmentFile":
                if value.strip().lstrip("-").strip("'\"") in wanted:
                    references.append(str(path))
            elif key == "Environment" and "pharmacy-monitor" in path.relative_to(units).as_posix():
                for token in value.split():
                    name = token.lstrip("'\"").partition("=")[0]
                    if name in names:
                        setters.setdefault(name, []).append(str(path))
    return references, setters, ""


def report(root: Path, server_env: Path, units: Path, flags: list[str]) -> tuple[list[str], int]:
    lines = [
        f"python-dotenv in {root / '.venv'}: {dotenv_version(root)}",
        f"deployed {root / 'src/main.py'}: {deployed_rule(root)}",
    ]
    try:
        server_values, _ = read_assignments(server_env, export_prefix=False)
    except OSError as exc:
        lines.append(f"server file {server_env}: cannot read ({type(exc).__name__})")
        return lines, 2
    lines.append(f"server file {server_env}: {len(server_values)} names")

    lines.append("where a path-less load_dotenv() looked, from the deployed code:")
    found: list[Path] = []
    for candidate in search_path(root):
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
            file_values, hidden = read_assignments(path, export_prefix=True)
        except OSError as exc:
            lines.append(f"{path}: cannot read ({type(exc).__name__})")
            code = 3
            continue
        lines.append(f"names in {path} ({len(file_values)}), against the server file:")
        width = max((len(name) for name in file_values), default=0)
        for name in sorted(file_values):
            if name not in server_values:
                status = "absent"
            elif file_values[name] is None or server_values[name] is None:
                status = "unsure"
            elif file_values[name] == server_values[name]:
                status = "same"
            else:
                status = "differs"
            lines.append(f"  {name.ljust(width)}  {status}")
        lines.append(f"  other lines, not shown: {hidden}")
        shown.update(file_values)

    if found:
        references, setters, problem = scan_units(units, found, shown)
        if problem:
            lines.append(f"units under {units}: {problem}")
        else:
            lines.append(f"units under {units} that name such a file as EnvironmentFile:")
            lines.extend(f"  {path}" for path in references or ["none"])
            lines.append("names above that a pharmacy-monitor unit sets with Environment=:")
            if setters:
                for name in sorted(setters):
                    lines.extend(f"  {name}  {path}" for path in setters[name])
            else:
                lines.append("  none")

    if flags:
        lines.append("switches in the server file:")
        lines.extend(f"  {name}  {classify_switch(server_values, name)}" for name in flags)
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
