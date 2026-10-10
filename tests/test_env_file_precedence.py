"""Настройки процесс берёт из своего окружения; `.env` — только из корня своего чекаута.

2026-10-09 на сервере рядом с кодом нашёлся `/opt/pharmacy-monitor/.env` от 6 мая.
`CLAUDE.md` называл местом настроек сервера только `/etc/pharmacy-monitor/env`, а
`src/main.py` звал `load_dotenv` без пути и с перекрытием окружения. python-dotenv
в таком вызове ищет `.env`, поднимаясь по каталогам — от файла, который его позвал,
а при запуске из stdin (`python - <<PY`) от cwd, — и найденное побеждает окружение
процесса. Поэтому:

* каждая команда CLI на сервере брала значения этого файла поверх
  `EnvironmentFile` своей службы;
* копия кода, которую еженедельный сбор pharmonline кладёт в
  `/opt/pharmacy-monitor/.codex-pharmonline-auto.XXXXXX/runtime/`, доходила до того
  же файла — и он был сильнее даже переменных, названных рядом с командой;
* `migrations/env.py` (без перекрытия) из такой копии взял бы оттуда
  `DATABASE_URL`, не окажись его в окружении, — миграции пошли бы в чужую базу;
* на dev-боксе воркитри из `.claude/worktrees/…` читал `.env` корневого чекаута:
  тесты шли с настоящим ключом из него.

Правило теперь одно на все три вызова (`src/main.py`, `src/dashboard.py`,
`migrations/env.py`): путь к `.env` явный — корень того чекаута, из которого
исполняется код; поиска вверх нет; окружение процесса главнее файла.

Тесты исполняют настоящий импорт `src.main`, начало `src/dashboard.py` и настоящий
`alembic current` на копии дерева, разложенной так же, как раскладывает его сервер.
Страж ниже держит форму вызова во всех трёх файлах и не даёт завести четвёртый.
Чего страж не видит: чтение `.env` своим кодом, без python-dotenv, и вызовы внутри
вставок Python в workflow (`.github/` он не читает).

Вторая половина файла — `scripts/diag_env_sources.py`: скрипт для сервера, который
называет имена из `.env` и их отношение к файлу секретов. Значений он не печатает
ни в каком случае — это и проверяется, в том числе на оборванных файлах.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DIAG_SCRIPT = REPO / "scripts/diag_env_sources.py"
RUNBOOK = REPO / "docs/RUNBOOK.md"
CLAUDE_MD = REPO / "CLAUDE.md"

SHARED = "PM_ENVTEST_SHARED"  # имя есть и в окружении процесса, и в файле
FILE_ONLY = "PM_ENVTEST_FILE_ONLY"  # имя есть только в файле
EMPTY = "PM_ENVTEST_EMPTY"  # в окружении пустая строка, в файле значение
STRAY = "PM_ENVTEST_STRAY"  # имя из файла, которого процесс читать не должен
PROBED = (SHARED, FILE_ONLY, EMPTY, STRAY)


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


def _clean_env(**extra: str) -> dict[str, str]:
    # Окружение собирается заново: от тестового процесса в него не должно попасть
    # ни одной настройки проекта — иначе тест проверял бы машину, а не правило.
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    if "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]
    env.update(extra)
    return env


def _run(stage: Stage, command: list[str], *, cwd: Path, stdin: str | None = None, **env: str):
    result = subprocess.run(
        command,
        input=stdin,
        cwd=cwd,
        env=_clean_env(PYTHONPATH=str(stage.checkout), **env),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return result


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
        result = _run(stage, [sys.executable, str(probe)], cwd=cwd, **process_env)
    else:
        result = _run(stage, [sys.executable, "-"], cwd=cwd, stdin=_PROBE, **process_env)
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

    assert seen == {SHARED: "from-process", FILE_ONLY: None, EMPTY: None, STRAY: None}


@pytest.mark.parametrize("how", HOW)
def test_environment_wins_and_own_file_fills_the_gaps(stage: Stage, env_file, how: str) -> None:
    """`.env` в корне чекаута даёт только имена, которых нет в окружении; cwd ни при чём."""
    env_file(
        stage.checkout,
        **{SHARED: "from-own-file", FILE_ONLY: "from-own-file", EMPTY: "from-own-file"},
    )
    env_file(stage.elsewhere, **{STRAY: "from-cwd-file"})

    seen = _import_main(stage, how, cwd=stage.elsewhere, **{SHARED: "from-process", EMPTY: ""})

    # Пустая строка в окружении — тоже «имя есть»: файл её не заполняет.
    assert seen == {SHARED: "from-process", FILE_ONLY: "from-own-file", EMPTY: "", STRAY: None}


@pytest.mark.parametrize("how", HOW)
def test_local_development_reads_the_checkout_root(stage: Stage, env_file, how: str) -> None:
    """Локальная разработка как была: `cp .env.example .env` в корне — и команды его видят."""
    env_file(stage.checkout, **{SHARED: "from-own-file", FILE_ONLY: "from-own-file"})

    seen = _import_main(stage, how, cwd=stage.checkout)

    assert seen == {SHARED: "from-own-file", FILE_ONLY: "from-own-file", EMPTY: None, STRAY: None}


# Начало `src/dashboard.py` — всё до вызова `load_dotenv` включительно. Целиком
# модуль не импортировать: дальше он рисует страницу Streamlit.
_DASHBOARD_HEAD = f"""
import ast, json, os, sys
path = sys.argv[1]
head = []
for node in ast.parse(open(path, encoding="utf-8").read()).body:
    head.append(node)
    call = getattr(node, "value", None)
    if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "load_dotenv":
        break
else:
    raise SystemExit("no module-level load_dotenv call in " + path)
exec(compile(ast.Module(head, []), path, "exec"), {{"__name__": "probe", "__file__": path}})
print(json.dumps({{k: os.environ.get(k) for k in {PROBED!r}}}))
"""


def _dashboard_head(stage: Stage, **process_env: str) -> dict[str, str | None]:
    probe = stage.base / "probe_dashboard.py"
    probe.write_text(_DASHBOARD_HEAD)
    command = [sys.executable, str(probe), str(stage.checkout / "src/dashboard.py")]
    result = _run(stage, command, cwd=stage.elsewhere, **process_env)
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_dashboard_reads_only_its_own_checkout(stage: Stage, env_file) -> None:
    env_file(stage.host, **{STRAY: "from-host-file"})
    assert _dashboard_head(stage)[STRAY] is None

    env_file(stage.checkout, **{SHARED: "from-own-file", FILE_ONLY: "from-own-file"})
    seen = _dashboard_head(stage, **{SHARED: "from-process"})
    assert seen == {SHARED: "from-process", FILE_ONLY: "from-own-file", EMPTY: None, STRAY: None}


def _alembic_current(stage: Stage, **process_env: str) -> None:
    command = [sys.executable, "-m", "alembic", "-c", "alembic.ini", "current"]
    _run(stage, command, cwd=stage.checkout, **process_env)


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


# ── Страж: python-dotenv зовут три файла, и все три одинаково ──────────────────

DOTENV_CALLERS = ("src/main.py", "src/dashboard.py", "migrations/env.py")
PINNED_CALL = "load_dotenv(Path(__file__).resolve().parent.parent / '.env')"
SEARCHERS = {"find_dotenv", "dotenv_values", "DotEnv"}
SKIPPED_DIRS = {"tests", "node_modules", "__pycache__", "frontend"}


def _dotenv_problems(name: str, source: str) -> list[str]:
    """Чем файл нарушает правило; пусто — не нарушает."""
    problems: list[str] = []
    calls: list[ast.Call] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
            imported: list[ast.alias] = []
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
            imported = node.names
        else:
            modules = []
            imported = []
        if any(module == "dotenv" or module.startswith("dotenv.") for module in modules):
            if name not in DOTENV_CALLERS:
                problems.append(f"line {node.lineno}: only {DOTENV_CALLERS} may import dotenv")
            elif (
                not isinstance(node, ast.ImportFrom)
                or node.module != "dotenv"
                or [(alias.name, alias.asname) for alias in imported] != [("load_dotenv", None)]
            ):
                problems.append(f"line {node.lineno}: only `from dotenv import load_dotenv`")
        if isinstance(node, ast.Constant) and node.value == "dotenv":
            problems.append(f"line {node.lineno}: dotenv imported by name")
        if isinstance(node, ast.Name) and node.id in SEARCHERS:
            problems.append(f"line {node.lineno}: {node.id} is not the pinned call")
        if isinstance(node, ast.Attribute) and node.attr in SEARCHERS | {"load_dotenv"}:
            problems.append(f"line {node.lineno}: {node.attr} is not the pinned call")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "load_dotenv":
                calls.append(node)
    if name in DOTENV_CALLERS and len(calls) != 1:
        problems.append(f"{len(calls)} calls of load_dotenv, exactly one expected")
    for call in calls:
        if ast.unparse(call) != PINNED_CALL:
            problems.append(f"line {call.lineno}: {ast.unparse(call)} is not {PINNED_CALL}")
    return problems


def _python_sources() -> dict[str, str]:
    sources: dict[str, str] = {}
    for directory, subdirectories, files in os.walk(REPO):
        subdirectories[:] = [
            entry
            for entry in subdirectories
            if entry not in SKIPPED_DIRS and not entry.startswith(".")
        ]
        for file in files:
            if file.endswith(".py"):
                path = Path(directory) / file
                sources[path.relative_to(REPO).as_posix()] = path.read_text(encoding="utf-8")
    return sources


_IMPORT = "from pathlib import Path\nfrom dotenv import load_dotenv\n"


@pytest.mark.parametrize(
    "source",
    [
        _IMPORT + "load_dotenv()",
        _IMPORT + "load_dotenv(override=True)",
        _IMPORT + "load_dotenv(Path(__file__).resolve().parent.parent / '.env', override=True)",
        _IMPORT + "load_dotenv(Path(__file__).resolve().parent.parent / '.env', None, False, True)",
        _IMPORT + "load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / '.env')",
        _IMPORT + "load_dotenv(Path(__file__).resolve().parent / '.env')",
        _IMPORT + "load_dotenv(Path(__file__).parent.parent / '.env')",
        _IMPORT + "load_dotenv(Path.cwd() / '.env')",
        _IMPORT + "load_dotenv('.env')",
        _IMPORT + "load_dotenv(None)",
        _IMPORT + PINNED_CALL + "\n" + PINNED_CALL,
        _IMPORT,
        "from dotenv import load_dotenv as _ld\n_ld(override=True)",
        "import dotenv\ndotenv.load_dotenv()",
        "from dotenv import find_dotenv, load_dotenv\nload_dotenv(find_dotenv())",
        "from dotenv import dotenv_values\nimport os\nos.environ.update(dotenv_values('.env'))",
        "from dotenv.main import DotEnv\nDotEnv('.env', override=True).set_as_environment_variables()",
        "import importlib\nimportlib.import_module('dotenv').load_dotenv()",
    ],
)
def test_guard_refuses_anything_but_the_pinned_call(source: str) -> None:
    assert _dotenv_problems("src/main.py", source)


def test_guard_accepts_the_pinned_call_only_in_the_three_callers() -> None:
    source = _IMPORT + PINNED_CALL
    for caller in DOTENV_CALLERS:
        assert _dotenv_problems(caller, source) == []
    assert _dotenv_problems("src/api.py", source)
    assert _dotenv_problems("scripts/audit_matches.py", source)


def test_env_file_is_read_the_same_way_in_all_three_places() -> None:
    sources = _python_sources()
    assert set(DOTENV_CALLERS) <= sources.keys()  # страж видит файлы, которые держит
    found = {name: _dotenv_problems(name, text) for name, text in sources.items()}
    assert {name: problems for name, problems in found.items() if problems} == {}


# ── Проверка «выложено ли» из документации отличает прежний код от нового ──────

DEPLOY_CHECK = "^load_dotenv(override=True)"


def _grep_count(pattern: str, path: Path) -> int:
    result = subprocess.run(["grep", "-c", pattern, str(path)], capture_output=True, text=True)
    return int(result.stdout.strip())


def test_documented_deploy_check_tells_old_code_from_new(tmp_path: Path) -> None:
    current = (REPO / "src/main.py").read_text(encoding="utf-8")
    new_call = PINNED_CALL.replace("'", '"')
    assert current.count(new_call + "\n") == 1
    previous = tmp_path / "previous_main.py"
    previous.write_text(current.replace(new_call, "load_dotenv(override=True)"), encoding="utf-8")

    for document in (RUNBOOK, CLAUDE_MD):
        assert f"grep -c '{DEPLOY_CHECK}'" in document.read_text(encoding="utf-8"), document.name
    assert _grep_count(DEPLOY_CHECK, previous) == 1
    assert _grep_count(DEPLOY_CHECK, REPO / "src/main.py") == 0
    # И неточный шаблон не соврёт: слов прежнего вызова в новом файле нет вовсе.
    assert "override=True" not in current


# ── scripts/diag_env_sources.py: имена и same/differs/absent/unsure, без значений ──

SECRETS = (
    "alpha-value-1",
    "beta-file-2",
    "beta-server-9",
    "gamma-only-3",
    "delta 4",
    "eps\\ilon",
    "zeta$eta",
    "theta#iota",
    "kappa'lambda",
    "omega-other-7",
    "weird-switch-8",
    "sigmaTau",
    "QUJDREVGRw",
    "hunter2",
    "upsilon-key-9",
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
    "KEYBODY",
)

ENV_FILE_TEXT = (
    "A_SAME=alpha-value-1\n"
    'B_DIFF="beta-file-2"\n'
    "export C_ONLY=gamma-only-3\n"
    "# комментарий\n"
    'D_QUOTED="delta 4"\n'
    "E_BACKSLASH=eps\\ilon\n"
    "F_DOLLAR=zeta$eta\n"
    "G_HASH=theta#iota\n"
    "H_INNER_QUOTE=kappa'lambda\n"
    "I_SYSTEMD_EXPORT=alpha-value-1\n"
    "SCRAPE_REPORT_EMAIL=1\n"
    # Дальше — то, что именем не является: печататься не должно ничего.
    "sigmaTau=alpha-value-1\n"  # строчные буквы в имени
    "QUJDREVGRw==\n"  # хвост значения, оборванного переводом строки
    "postgresql://pm:hunter2@localhost/db?sslmode=require\n"
    "'upsilon-key-9'=x\n"
    "просто строка без знака равенства\n"
    'J_MULTILINE="-----BEGIN PRIVATE KEY-----\n'
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC=\n"
    "KEYBODY=alpha-value-1\n"  # выглядит как имя, но это середина значения
    '-----END PRIVATE KEY-----"\n'
    "K_AFTER=alpha-value-1\n"
)

SERVER_FILE_TEXT = (
    "# файл секретов\n"
    "A_SAME=alpha-value-1\r\n"
    "B_DIFF=beta-server-9\n"
    "D_QUOTED='delta 4'\n"
    "E_BACKSLASH=eps\\ilon\n"
    "F_DOLLAR=zeta$eta\n"
    "G_HASH=theta#iota\n"
    "H_INNER_QUOTE=kappa'lambda\n"
    "export I_SYSTEMD_EXPORT=alpha-value-1\n"  # systemd такую строку пропускает
    "K_AFTER=alpha-value-1\n"
    "AI_FALLBACK_ENABLED=1\n"
    "EMPTY_SWITCH=\n"
    "LOUD_SWITCH=TRUE\n"
    "ODD_SWITCH=weird-switch-8\n"
    "Z_OTHER=omega-other-7\n"
)

EXPECTED_NAMES = """\
  A_SAME               same
  B_DIFF               differs
  C_ONLY               absent
  D_QUOTED             same
  E_BACKSLASH          unsure
  F_DOLLAR             unsure
  G_HASH               unsure
  H_INNER_QUOTE        unsure
  I_SYSTEMD_EXPORT     absent
  J_MULTILINE          absent
  K_AFTER              same
  SCRAPE_REPORT_EMAIL  absent
  other lines, not shown: 8
"""


@dataclass(frozen=True)
class Server:
    root: Path
    env: Path
    units: Path


@pytest.fixture
def server(tmp_path: Path) -> Server:
    root = tmp_path / "opt-pharmacy-monitor"
    (root / "src").mkdir(parents=True)
    shutil.copy(REPO / "src/main.py", root / "src/main.py")
    (root / ".env").write_text(ENV_FILE_TEXT, encoding="utf-8")
    env = tmp_path / "etc-pharmacy-monitor-env"
    env.write_text(SERVER_FILE_TEXT, encoding="utf-8", newline="")
    units = tmp_path / "etc-systemd-system"
    scrape = units / "pharmacy-monitor-scrape@.service.d"
    scrape.mkdir(parents=True)
    (scrape / "zz-realtime-alerts.conf").write_text(
        "[Service]\nEnvironment=SCRAPE_REPORT_EMAIL=0 OTHER_NAME=beta-server-9\n"
    )
    (units / "pharmacy-monitor-api.service").write_text(
        f"[Service]\nEnvironmentFile={env}\nExecStart=/bin/true\n"
    )
    # Чужой юнит задаёт то же имя — к делу не относится и не называется.
    (units / "unrelated.service").write_text("[Service]\nEnvironment=A_SAME=omega-other-7\n")
    return Server(root=root, env=env, units=units)


def _diag(server: Server, *arguments: str, env: Path | None = None) -> subprocess.CompletedProcess:
    # Как в RUNBOOK: текст скрипта приходит через stdin, на машине его файла нет.
    # `-I -S` — без site-packages: скрипту хватает стандартной библиотеки.
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-",
            *("--root", str(server.root)),
            *("--server-env", str(env or server.env)),
            *("--units", str(server.units)),
            *arguments,
        ],
        input=DIAG_SCRIPT.read_text(encoding="utf-8"),
        cwd=server.root.parent,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _section(output: str, heading: str) -> str:
    """Строки с отступом, идущие сразу за строкой-заголовком."""
    lines = output.splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if line.startswith(heading)) + 1
    end = next(
        (index for index in range(start, len(lines)) if not lines[index].startswith("  ")),
        len(lines),
    )
    return "".join(lines[start:end])


def test_diag_names_every_variable_and_its_relation_to_the_server_file(server: Server) -> None:
    result = _diag(server)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert _section(result.stdout, f"names in {server.root / '.env'} (12)") == EXPECTED_NAMES
    assert f"server file {server.env}: 13 names\n" in result.stdout


def test_diag_prints_nothing_but_the_known_shapes(server: Server) -> None:
    """Весь вывод — строки известного вида: лишнему (обрывку, длине, хешу) в нём нет места."""
    result = _diag(server, "--flag", "AI_FALLBACK_ENABLED", "--flag", "ODD_SWITCH")

    assert result.returncode == 0, result.stderr
    printed = result.stdout + result.stderr
    assert printed.isascii()
    for secret in SECRETS:
        assert secret not in printed

    stat_part = r"mode=[0-7]{3} owner=\S+:\S+ size=\d+ modified=\d{4}-\d\d-\d\d"
    root, env, units = (re.escape(str(path)) for path in (server.root, server.env, server.units))
    shape = (
        rf"python-dotenv in {root}/\.venv: not found\n"
        rf"deployed {root}/src/main\.py: new rule - own checkout root only, "
        r"the environment wins\n"
        rf"server file {env}: 13 names\n"
        r"where a path-less load_dotenv\(\) looked, from the deployed code:\n"
        rf"  {root}/src/\.env  -\n"
        rf"  {root}/\.env  FOUND  {stat_part}\n"
        r"(?:  /\S*\.env  -\n)+"
        rf"  {root}/migrations/\.env  -\n"
        rf"names in {root}/\.env \(12\), against the server file:\n"
        + re.escape(EXPECTED_NAMES)
        + rf"units under {units} that name such a file as EnvironmentFile:\n"
        r"  none\n"
        r"names above that a pharmacy-monitor unit sets with Environment=:\n"
        rf"  SCRAPE_REPORT_EMAIL  {units}/pharmacy-monitor-scrape@\.service\.d/"
        r"zz-realtime-alerts\.conf\n"
        r"switches in the server file:\n"
        r"  AI_FALLBACK_ENABLED  1\n"
        r"  ODD_SWITCH  set \(value not shown\)\n"
    )
    assert re.fullmatch(shape, result.stdout), result.stdout


def test_diag_says_only_a_switch_word_about_a_switch(server: Server) -> None:
    result = _diag(
        server,
        *("--flag", "AI_FALLBACK_ENABLED"),
        *("--flag", "EMPTY_SWITCH"),
        *("--flag", "LOUD_SWITCH"),
        *("--flag", "NOT_THERE"),
        *("--flag", "ODD_SWITCH"),
        *("--flag", "E_BACKSLASH"),
    )

    assert result.returncode == 0, result.stderr
    assert _section(result.stdout, "switches in the server file:") == (
        "  AI_FALLBACK_ENABLED  1\n"
        "  EMPTY_SWITCH  empty\n"  # пусто — не «выключено»: у части переключателей это «включено»
        "  LOUD_SWITCH  true\n"
        "  NOT_THERE  unset\n"
        "  ODD_SWITCH  set (value not shown)\n"
        "  E_BACKSLASH  set (value not shown)\n"
    )


def test_diag_names_units_that_use_the_file_as_their_environment(server: Server) -> None:
    stray = server.root / ".env"
    (server.units / "legacy.service").write_text(f"[Service]\nEnvironmentFile=-{stray}\n")
    dropin = server.units / "pharmacy-monitor-run.service.d"
    dropin.mkdir()
    (dropin / "override.conf").write_text(f'[Service]\nEnvironmentFile="{stray}"\n')
    (server.units / "multi-user.target.wants").mkdir()
    (server.units / "multi-user.target.wants/legacy.service").symlink_to(
        server.units / "legacy.service"
    )

    result = _diag(server)

    assert result.returncode == 0, result.stderr
    heading = f"units under {server.units} that name such a file as EnvironmentFile:"
    assert _section(result.stdout, heading) == (
        f"  {server.units / 'legacy.service'}\n  {dropin / 'override.conf'}\n"
    )


def test_diag_looks_above_the_code_and_under_migrations(server: Server) -> None:
    (server.root / ".env").unlink()
    (server.root.parent / ".env").write_text("A_SAME=alpha-value-1\nL_ABOVE=gamma-only-3\n")
    (server.root / "migrations").mkdir()
    (server.root / "migrations/.env").write_text("B_DIFF=beta-file-2\n")

    result = _diag(server)

    assert result.returncode == 0, result.stderr
    above = _section(result.stdout, f"names in {server.root.parent / '.env'} (2)")
    assert above == "  A_SAME   same\n  L_ABOVE  absent\n  other lines, not shown: 0\n"
    under = _section(result.stdout, f"names in {server.root / 'migrations/.env'} (1)")
    assert under == "  B_DIFF  differs\n  other lines, not shown: 0\n"


def test_diag_without_env_file_says_so(server: Server) -> None:
    (server.root / ".env").unlink()

    result = _diag(server, "--flag", "AI_FALLBACK_ENABLED")

    assert result.returncode == 0, result.stderr
    assert "no .env next to the code or above it: nothing to compare\n" in result.stdout
    assert "names in" not in result.stdout
    assert "units under" not in result.stdout
    assert _section(result.stdout, "switches in the server file:") == "  AI_FALLBACK_ENABLED  1\n"


def test_diag_tells_which_rule_the_deployed_code_follows(server: Server) -> None:
    deployed = server.root / "src/main.py"
    line = f"deployed {deployed}: "
    assert line + "new rule" in _diag(server).stdout

    deployed.write_text("import os\nload_dotenv(override=True)\n")
    assert line + "OLD rule" in _diag(server).stdout

    deployed.write_text("# load_dotenv(override=True) было здесь\n", encoding="utf-8")
    assert line + "neither call found" in _diag(server).stdout

    deployed.unlink()
    assert line + "cannot read (FileNotFoundError)" in _diag(server).stdout


def test_diag_refuses_when_the_server_file_cannot_be_read(server: Server) -> None:
    result = _diag(server, env=server.root / "no-such-file")

    assert result.returncode == 2
    assert "cannot read (FileNotFoundError)" in result.stdout
    assert "names in" not in result.stdout


def test_diag_does_not_compare_what_is_not_a_readable_file(server: Server) -> None:
    stray = server.root / ".env"
    stray.unlink()
    stray.mkdir()
    result = _diag(server)
    assert result.returncode == 0, result.stderr
    assert f"  {stray}  FOUND, but it is not a regular file\n" in result.stdout
    assert "names in" not in result.stdout

    stray.rmdir()
    stray.symlink_to(server.root / "nowhere")
    result = _diag(server)
    assert result.returncode == 0, result.stderr
    assert f"  {stray}  FOUND, but cannot follow it (FileNotFoundError)\n" in result.stdout

    stray.unlink()
    stray.write_text(ENV_FILE_TEXT, encoding="utf-8")
    stray.chmod(0)
    if os.access(stray, os.R_OK):  # под root запрет на чтение не действует
        return
    result = _diag(server)
    assert result.returncode == 3
    assert f"{stray}: cannot read (PermissionError)\n" in result.stdout
    for secret in SECRETS:
        assert secret not in result.stdout + result.stderr


def test_diag_only_reads(server: Server, tmp_path: Path) -> None:
    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            str(path): (path.lstat().st_size, path.lstat().st_mtime_ns)
            for path in sorted(tmp_path.rglob("*"))
        }

    before = snapshot()
    result = _diag(server, "--flag", "AI_FALLBACK_ENABLED")

    assert result.returncode == 0, result.stderr
    assert snapshot() == before


def test_runbook_command_feeds_this_script_to_a_plain_interpreter_as_pm() -> None:
    """Команда в RUNBOOK называет существующий файл и подаёт его через stdin."""
    assert DIAG_SCRIPT.is_file()
    command = "'runuser -u pm -- python3 -' < scripts/diag_env_sources.py"
    assert command in RUNBOOK.read_text(encoding="utf-8")
    assert command in DIAG_SCRIPT.read_text(encoding="utf-8")
