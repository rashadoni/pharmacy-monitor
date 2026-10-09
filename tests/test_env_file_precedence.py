"""Настройки процесс берёт из своего окружения; `.env` — только из корня своего чекаута.

2026-10-09 на сервере рядом с кодом нашёлся `/opt/pharmacy-monitor/.env` от 6 мая,
о котором документация молчала: местом настроек она называла только
`/etc/pharmacy-monitor/env`. При этом `src/main.py` звал `load_dotenv(override=True)`
без пути. python-dotenv в таком вызове ищет `.env`, поднимаясь по каталогам — от
файла, который его позвал, а при запуске из stdin (`python - <<PY`) от cwd, — и с
`override=True` найденное побеждает окружение процесса. Поэтому:

* каждая команда CLI на сервере брала значения майского файла поверх
  `EnvironmentFile` своей службы;
* копия кода, которую еженедельный сбор pharmonline кладёт в
  `/opt/pharmacy-monitor/.codex-pharmonline-auto.XXXXXX/runtime/`, доходила до того
  же файла — и он побеждал даже то, что workflow подал явно, рядом с командой;
* `migrations/env.py` (без override) из такой копии брал оттуда `DATABASE_URL`,
  если в окружении его не оказалось, — миграции пошли бы в чужую базу;
* на dev-боксе воркитри в `.claude/worktrees/…` читал `.env` корневого чекаута:
  тесты шли с настоящим ключом из него.

Правило теперь одно на все три вызова (`src/main.py`, `src/dashboard.py`,
`migrations/env.py`): путь к `.env` явный — корень того чекаута, из которого
исполняется код; поиска вверх нет; окружение процесса главнее файла.

Тесты исполняют настоящий импорт `src.main` и настоящий `alembic current` на копии
дерева, разложенной так же, как раскладывает его сервер. Чего они не видят:
`src/dashboard.py` проверен только по тексту вызова (страж ниже) — импорт требует
Streamlit; вызовы `load_dotenv` внутри heredoc-ов workflow страж не читает (там
путь явный — файл секретов сервера, и override задуман).

Вторая половина файла — `scripts/diag_env_sources.py`: скрипт для сервера, который
называет имена из `.env` и их отношение к основному файлу. Значений он не печатает
ни в каком случае — это и проверяется.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DIAG_SCRIPT = REPO / "scripts/diag_env_sources.py"
RUNBOOK = REPO / "docs/RUNBOOK.md"

SHARED = "PM_ENVTEST_SHARED"  # имя есть и в окружении процесса, и в файле
FILE_ONLY = "PM_ENVTEST_FILE_ONLY"  # имя есть только в файле
STRAY = "PM_ENVTEST_STRAY"  # имя из файла, которого процесс читать не должен
PROBED = (SHARED, FILE_ONLY, STRAY)


@dataclass(frozen=True)
class Stage:
    base: Path
    host: Path  # стоит на месте /opt/pharmacy-monitor
    checkout: Path  # копия кода двумя уровнями ниже — как у еженедельного сбора
    elsewhere: Path  # посторонний каталог: cwd, не связанный с кодом


@pytest.fixture(scope="module")
def stage(tmp_path_factory: pytest.TempPathFactory) -> Stage:
    base = tmp_path_factory.mktemp("env-file-precedence")
    host = base / "opt-pharmacy-monitor"
    checkout = host / ".codex-pharmonline-auto.AbC123" / "runtime"
    elsewhere = base / "elsewhere"
    skip = shutil.ignore_patterns("__pycache__")
    shutil.copytree(REPO / "src", checkout / "src", ignore=skip)
    shutil.copytree(REPO / "migrations", checkout / "migrations", ignore=skip)
    shutil.copy(REPO / "alembic.ini", checkout / "alembic.ini")
    (checkout / "data").mkdir()
    elsewhere.mkdir()
    return Stage(base=base, host=host, checkout=checkout, elsewhere=elsewhere)


@pytest.fixture
def env_file(stage: Stage):
    """Кладёт `.env` в каталог; после теста убирает всё, что положил."""
    written: list[Path] = []

    def write(directory: Path, **values: str) -> Path:
        path = directory / ".env"
        path.write_text("".join(f"{name}={value}\n" for name, value in values.items()))
        written.append(path)
        return path

    yield write
    for path in written:
        path.unlink(missing_ok=True)
    for leftover in stage.base.rglob("*.sqlite"):
        leftover.unlink()


def _process_env(stage: Stage, **extra: str) -> dict[str, str]:
    # Окружение собирается заново: от тестового процесса в него не должно попасть
    # ни одной настройки проекта — иначе тест проверял бы машину, а не правило.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(stage.checkout),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]
    env.update(extra)
    return env


_PROBE = f"""
import json, os
import src.main
print(json.dumps({{"module": src.main.__file__, **{{k: os.environ.get(k) for k in {PROBED!r}}}}}))
"""

# Два способа запуска — два разных поиска в python-dotenv. «script» — файл на диске:
# так идут `pharmacy-monitor …`, `python -m src.main …` и `scripts/*.py`, поиск
# начинался от каталога позвавшего файла. «stdin» — `python - <<PY`, как во
# вставках workflow: поиск начинался от cwd.
HOW = ("script", "stdin")


def _import_main(stage: Stage, how: str, *, cwd: Path, **process_env: str) -> dict[str, str | None]:
    if how == "script":
        probe = stage.base / "probe_import.py"
        probe.write_text(_PROBE)
        command, stdin = [sys.executable, str(probe)], None
    else:
        command, stdin = [sys.executable, "-"], _PROBE
    result = subprocess.run(
        command,
        input=stdin,
        cwd=cwd,
        env=_process_env(stage, **process_env),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    # Импортирована именно копия: иначе тест молча проверил бы чужой код.
    assert Path(seen.pop("module")).is_relative_to(stage.checkout)
    return seen


@pytest.mark.parametrize("how", HOW)
def test_env_file_above_the_checkout_is_not_read(stage: Stage, env_file, how: str) -> None:
    """Раскладка сервера: `.env` лежит выше копии кода — и уровнем, и двумя."""
    env_file(stage.host, **{SHARED: "from-host-file", FILE_ONLY: "from-host-file"})
    env_file(stage.checkout.parent, **{STRAY: "from-parent-file"})

    seen = _import_main(stage, how, cwd=stage.checkout, **{SHARED: "from-process"})

    assert seen == {SHARED: "from-process", FILE_ONLY: None, STRAY: None}


@pytest.mark.parametrize("how", HOW)
def test_environment_wins_and_own_file_fills_the_gaps(stage: Stage, env_file, how: str) -> None:
    """`.env` в корне чекаута даёт только имена, которых нет в окружении; cwd ни при чём."""
    env_file(stage.checkout, **{SHARED: "from-own-file", FILE_ONLY: "from-own-file"})
    env_file(stage.elsewhere, **{STRAY: "from-cwd-file"})

    seen = _import_main(stage, how, cwd=stage.elsewhere, **{SHARED: "from-process"})

    assert seen == {SHARED: "from-process", FILE_ONLY: "from-own-file", STRAY: None}


@pytest.mark.parametrize("how", HOW)
def test_local_development_reads_the_checkout_root(stage: Stage, env_file, how: str) -> None:
    """Локальная разработка как была: `cp .env.example .env` в корне — и команды его видят."""
    env_file(stage.checkout, **{SHARED: "from-own-file", FILE_ONLY: "from-own-file"})

    seen = _import_main(stage, how, cwd=stage.checkout)

    assert seen == {SHARED: "from-own-file", FILE_ONLY: "from-own-file", STRAY: None}


def _alembic_current(stage: Stage, **process_env: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "current"],
        cwd=stage.checkout,
        env=_process_env(stage, **process_env),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]


def test_alembic_does_not_take_database_url_from_a_file_above(stage: Stage, env_file) -> None:
    """Миграции из копии кода не уходят в базу, названную в чужом `.env`."""
    foreign = stage.host / "from-above.sqlite"
    env_file(stage.host, DATABASE_URL=f"sqlite:///{foreign}")

    _alembic_current(stage)

    assert not foreign.exists()
    # Без DATABASE_URL — база по умолчанию из migrations/env.py, относительно cwd.
    assert (stage.checkout / "data/db.sqlite").exists()


def test_alembic_reads_own_file_unless_the_environment_names_a_database(
    stage: Stage, env_file
) -> None:
    own = stage.checkout / "own.sqlite"
    named = stage.elsewhere / "named-by-process.sqlite"
    env_file(stage.checkout, DATABASE_URL=f"sqlite:///{own}")

    _alembic_current(stage)
    assert own.exists()

    own.unlink()
    _alembic_current(stage, DATABASE_URL=f"sqlite:///{named}")
    assert named.exists()
    assert not own.exists()


# ── Страж: вызов без пути и override=True не возвращаются ──────────────────────

GUARDED_DIRS = ("src", "migrations", "scripts", "infra")
READERS = {"load_dotenv", "dotenv_values"}


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _dotenv_violations(source: str) -> list[str]:
    """Чем вызовы python-dotenv в тексте нарушают правило; пусто — не нарушают."""
    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id == "find_dotenv":
            violations.append(f"line {node.lineno}: find_dotenv searches up the directory tree")
        elif isinstance(node, ast.Attribute) and node.attr == "find_dotenv":
            violations.append(f"line {node.lineno}: find_dotenv searches up the directory tree")
        elif isinstance(node, ast.alias) and node.name == "find_dotenv":
            violations.append("import of find_dotenv: it searches up the directory tree")
        elif isinstance(node, ast.Call) and _called_name(node) in READERS:
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            if None in keywords:
                violations.append(f"line {node.lineno}: **kwargs hide the path and override")
            if not node.args and not ({"dotenv_path", "stream"} & keywords.keys()):
                violations.append(
                    f"line {node.lineno}: no path — python-dotenv would search for one"
                )
            override = keywords.get("override")
            if override is not None and not (
                isinstance(override, ast.Constant) and override.value is False
            ):
                violations.append(
                    f"line {node.lineno}: override lets the file beat the environment"
                )
            if "usecwd" in keywords:
                violations.append(f"line {node.lineno}: usecwd is a search, not a path")
    return violations


def _guarded_sources() -> dict[str, str]:
    sources: dict[str, str] = {}
    for directory in GUARDED_DIRS:
        for path in sorted((REPO / directory).rglob("*.py")):
            if "__pycache__" not in path.parts:
                sources[path.relative_to(REPO).as_posix()] = path.read_text(encoding="utf-8")
    return sources


@pytest.mark.parametrize(
    "call",
    [
        "load_dotenv()",
        "load_dotenv(override=True)",
        "load_dotenv(path, override=True)",
        "load_dotenv(path, override=flag)",
        "dotenv.load_dotenv()",
        "load_dotenv(find_dotenv())",
        "load_dotenv(dotenv.find_dotenv(usecwd=True))",
        "load_dotenv(usecwd=True)",
        "load_dotenv(**options)",
        "dotenv_values()",
        "from dotenv import find_dotenv",
    ],
)
def test_guard_flags_search_and_override(call: str) -> None:
    assert _dotenv_violations(call)


@pytest.mark.parametrize(
    "call",
    [
        "load_dotenv(path)",
        "load_dotenv(dotenv_path=path, override=False)",
        "load_dotenv(PROJECT_ROOT / '.env')",
        "dotenv_values(path)",
    ],
)
def test_guard_accepts_an_explicit_path(call: str) -> None:
    assert _dotenv_violations(call) == []


def test_no_env_file_search_or_override_in_the_code() -> None:
    sources = _guarded_sources()
    # Страж не слеп: три известных вызова он видит. Переименование или перенос
    # модуля не должны оставить проверку зелёной на пустом месте.
    for known in ("src/main.py", "src/dashboard.py", "migrations/env.py"):
        calls = [
            node
            for node in ast.walk(ast.parse(sources[known]))
            if isinstance(node, ast.Call) and _called_name(node) == "load_dotenv"
        ]
        assert len(calls) == 1, known

    found = {name: _dotenv_violations(text) for name, text in sources.items()}
    assert {name: problems for name, problems in found.items() if problems} == {}


# ── scripts/diag_env_sources.py: имена и same/differs/absent, без значений ─────

SECRET_VALUES = (
    "alpha-value-1",
    "beta-file-2",
    "beta-server-9",
    "gamma-only-3",
    "delta 4",
    "omega-other-7",
    "weird-switch-8",
)


@dataclass(frozen=True)
class Server:
    root: Path
    env: Path


@pytest.fixture
def server(tmp_path: Path) -> Server:
    root = tmp_path / "opt-pharmacy-monitor"
    (root / "src").mkdir(parents=True)
    (root / ".env").write_text(
        "A_SAME=alpha-value-1\n"
        'B_DIFF="beta-file-2"\n'
        "export C_ONLY=gamma-only-3\n"
        "# комментарий\n"
        'D_QUOTED="delta 4"\n',
        encoding="utf-8",
    )
    env = tmp_path / "etc-pharmacy-monitor-env"
    env.write_text(
        "# файл секретов\n"
        "A_SAME=alpha-value-1\r\n"
        "B_DIFF=beta-server-9\n"
        "D_QUOTED='delta 4'\n"
        "AI_FALLBACK_ENABLED=1\n"
        "SCRAPE_REPORT_EMAIL=0\n"
        "ODD_SWITCH=weird-switch-8\n"
        "Z_OTHER=omega-other-7\n",
        encoding="utf-8",
    )
    return Server(root=root, env=env)


def _diag(server: Server, *arguments: str, env: Path | None = None) -> subprocess.CompletedProcess:
    # Как в RUNBOOK: текст скрипта приходит через stdin, на машине его файла нет.
    return subprocess.run(
        [
            sys.executable,
            "-",
            "--root",
            str(server.root),
            "--server-env",
            str(env or server.env),
            *arguments,
        ],
        input=DIAG_SCRIPT.read_text(encoding="utf-8"),
        cwd=server.root.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _statuses(output: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] in {"same", "differs", "absent"}:
            statuses[parts[0]] = parts[1]
    return statuses


def test_diag_names_every_variable_and_its_relation_to_the_server_file(server: Server) -> None:
    result = _diag(server)

    assert result.returncode == 0, result.stderr
    assert _statuses(result.stdout) == {
        "A_SAME": "same",  # CR в конце строки основного файла отброшен, как у systemd
        "B_DIFF": "differs",
        "C_ONLY": "absent",
        "D_QUOTED": "same",  # кавычки разные, значение одно
    }
    assert f"{server.root / '.env'}  FOUND" in result.stdout
    assert "Z_OTHER" not in result.stdout  # имена, которых в `.env` нет, не перечисляются


def test_diag_never_prints_a_value(server: Server) -> None:
    result = _diag(server, "--flag", "AI_FALLBACK_ENABLED", "--flag", "ODD_SWITCH")

    assert result.returncode == 0, result.stderr
    printed = result.stdout + result.stderr
    assert printed.isascii()
    for value in SECRET_VALUES:
        assert value not in printed


def test_diag_says_only_on_off_or_unset_about_a_switch(server: Server) -> None:
    result = _diag(
        server,
        *("--flag", "AI_FALLBACK_ENABLED"),
        *("--flag", "SCRAPE_REPORT_EMAIL"),
        *("--flag", "NOT_THERE"),
        *("--flag", "ODD_SWITCH"),
    )

    assert result.returncode == 0, result.stderr
    switches = result.stdout.split("switches in the server file:")[1]
    assert "AI_FALLBACK_ENABLED  on" in switches
    assert "SCRAPE_REPORT_EMAIL  off" in switches
    assert "NOT_THERE  unset" in switches
    assert "ODD_SWITCH  set, not a plain on/off value" in switches


def test_diag_reports_a_file_above_the_code_too(server: Server) -> None:
    (server.root / ".env").unlink()
    (server.root.parent / ".env").write_text("A_SAME=alpha-value-1\nE_ABOVE=gamma-only-3\n")

    result = _diag(server)

    assert result.returncode == 0, result.stderr
    assert _statuses(result.stdout) == {"A_SAME": "same", "E_ABOVE": "absent"}


def test_diag_without_env_file_says_so(server: Server) -> None:
    (server.root / ".env").unlink()

    result = _diag(server)

    assert result.returncode == 0, result.stderr
    assert "nothing to compare" in result.stdout
    assert _statuses(result.stdout) == {}


def test_diag_refuses_when_the_server_file_cannot_be_read(server: Server) -> None:
    result = _diag(server, env=server.root / "no-such-file")

    assert result.returncode == 2
    assert "cannot read (FileNotFoundError)" in result.stdout
    assert _statuses(result.stdout) == {}


def test_runbook_command_feeds_this_script_over_stdin() -> None:
    """Команда в RUNBOOK называет существующий файл и подаёт его через stdin."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert DIAG_SCRIPT.is_file()
    assert "/opt/pharmacy-monitor/.venv/bin/python -" in text
    assert "< scripts/diag_env_sources.py" in text
