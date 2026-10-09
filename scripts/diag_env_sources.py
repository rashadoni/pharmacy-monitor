#!/usr/bin/env python3
"""Чем `.env` рядом с кодом расходится с файлом секретов сервера — имена, без значений.

Настройки сервера живут в `/etc/pharmacy-monitor/env` (systemd `EnvironmentFile`).
Но рядом с кодом может лежать `.env`, и CLI его читает: до правки 2026-10-10 —
поиском вверх по каталогам и поверх окружения процесса, после неё — только из
корня своего чекаута и только для имён, которых в окружении нет. Скрипт отвечает
на вопрос «что такой файл меняет на этой машине»: какие имена в нём стоят и как
каждое соотносится с основным файлом — `same`, `differs` или `absent`.

Только чтение. Значения не печатаются нигде, в том числе в сообщениях об ошибках:
о сбое разбора сказано классом исключения. Вывод — ASCII: локаль на сервере
бывает `C`.

Каталог `scripts/` выкладка на сервер не возит, поэтому скрипт подаётся по ssh
через stdin (с dev-бокса, под root — оба файла читают только root и pm):

    ssh root@13.140.186.143 '/opt/pharmacy-monitor/.venv/bin/python -' \
        < scripts/diag_env_sources.py

`--flag ИМЯ` дополнительно говорит про переключатель из основного файла `on`,
`off` или `unset` — без самого значения. Не для workflow: журнал шага публичен,
а имена переменных сервера в нём ни к чему.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import time
from pathlib import Path

DEFAULT_ROOT = "/opt/pharmacy-monitor"
DEFAULT_SERVER_ENV = "/etc/pharmacy-monitor/env"

_ON = {"1", "true", "yes", "on", "required"}
_OFF = {"", "0", "false", "no", "off"}


def _owner(st: os.stat_result) -> str:
    try:
        import grp
        import pwd

        return f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name}"
    except (ImportError, KeyError):
        return f"{st.st_uid}:{st.st_gid}"


def _describe(path: Path) -> str:
    st = path.stat()
    modified = time.strftime("%Y-%m-%d", time.gmtime(st.st_mtime))
    return (
        f"mode={stat.S_IMODE(st.st_mode):o} owner={_owner(st)} "
        f"size={st.st_size} modified={modified}"
    )


def search_path(root: Path) -> list[Path]:
    """Каталоги, которые обходит `load_dotenv()` без пути, вызванный из `<root>/src`."""
    start = root / "src"
    return [start, *start.parents]


def read_dotenv(path: Path) -> tuple[dict[str, str], str]:
    """Имена и значения так, как их выставил бы `load_dotenv(path)`."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        plain = read_environment_file(path)
        values = {name.removeprefix("export ").strip(): value for name, value in plain.items()}
        return values, "python-dotenv not importable: plain KEY=VALUE parse"
    values = dotenv_values(path)
    return {k: v for k, v in values.items() if v is not None}, ""


def read_environment_file(path: Path) -> dict[str, str]:
    """Имена и значения так, как их читает systemd из `EnvironmentFile`.

    Пробелы и CR по краям отброшены, одна пара кавычек вокруг значения снята.
    Обратную косую черту systemd читает по-своему; здесь она остаётся как есть.
    """
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;" or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        result[name] = value
    return result


def classify_flag(values: dict[str, str], name: str) -> str:
    if name not in values:
        return "unset"
    value = values[name].strip().lower()
    if value in _ON:
        return "on"
    if value in _OFF:
        return "off"
    return "set, not a plain on/off value"


def report(root: Path, server_env: Path, flags: list[str]) -> tuple[list[str], int]:
    lines: list[str] = []
    try:
        from importlib.metadata import version

        lines.append(f"python-dotenv: {version('python-dotenv')}")
    except Exception as exc:  # noqa: BLE001 - версия справочная, сбой не важен
        lines.append(f"python-dotenv: unknown ({type(exc).__name__})")

    try:
        server_values = read_environment_file(server_env)
    except OSError as exc:
        lines.append(f"server file {server_env}: cannot read ({type(exc).__name__})")
        return lines, 2
    lines.append(f"server file {server_env}: {len(server_values)} names")

    lines.append(f"search order of load_dotenv() without a path, called from {root / 'src'}:")
    found: list[Path] = []
    for directory in search_path(root):
        candidate = directory / ".env"
        if candidate.is_file():
            found.append(candidate)
            lines.append(f"  {candidate}  FOUND  {_describe(candidate)}")
        else:
            lines.append(f"  {candidate}  -")
    if not found:
        lines.append("no .env next to the code or above it: nothing to compare")
    elif len(found) > 1:
        lines.append("the search stops at the first FOUND file; the rest are compared too")

    for path in found:
        try:
            file_values, note = read_dotenv(path)
        except Exception as exc:  # noqa: BLE001 - текст ошибки может нести строку файла
            lines.append(f"{path}: cannot parse ({type(exc).__name__})")
            continue
        lines.append(f"names in {path} ({len(file_values)}), against the server file:")
        if note:
            lines.append(f"  note: {note}")
        width = max((len(name) for name in file_values), default=0)
        for name in sorted(file_values):
            if name not in server_values:
                status = "absent"
            elif server_values[name] == file_values[name]:
                status = "same"
            else:
                status = "differs"
            lines.append(f"  {name.ljust(width)}  {status}")

    if flags:
        lines.append("switches in the server file:")
        for name in flags:
            lines.append(f"  {name}  {classify_flag(server_values, name)}")
    return lines, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=DEFAULT_ROOT, help="каталог с выложенным кодом")
    parser.add_argument("--server-env", default=DEFAULT_SERVER_ENV, help="файл секретов сервера")
    parser.add_argument(
        "--flag",
        action="append",
        default=[],
        metavar="NAME",
        help="переключатель из файла секретов: напечатать on/off/unset",
    )
    args = parser.parse_args(argv)
    lines, code = report(Path(args.root), Path(args.server_env), args.flag)
    sys.stdout.write("\n".join(lines) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
