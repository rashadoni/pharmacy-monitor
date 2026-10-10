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
  же файла — и имя из него было бы сильнее переменной, названной рядом с командой;
* `migrations/env.py` (без перекрытия) из такой копии взял бы оттуда
  `DATABASE_URL`, не окажись его в окружении, — миграции пошли бы в чужую базу;
* на dev-боксе воркитри из `.claude/worktrees/…` читал `.env` корневого чекаута:
  тесты шли с настоящим ключом из него.

Правило теперь одно на все три вызова (`src/main.py`, `src/dashboard.py`,
`migrations/env.py`): путь к `.env` явный — корень того чекаута, из которого
исполняется код; поиска вверх нет; окружение процесса главнее файла.

Тесты исполняют настоящий импорт `src.main`, начало `src/dashboard.py` и настоящий
`alembic current` на копии дерева, разложенной так же, как раскладывает его сервер.
Страж ниже держит форму вызова во всех трёх файлах и не даёт завести четвёртый —
ни в коде, ни в тестах. Чего страж не видит: чтение `.env` своим кодом, без
python-dotenv; `env_file` не как аргумент вызова (атрибут класса настроек, ключ
словаря); `--env-file` в команде и `EnvironmentFile=…/.env` в юните; вставки
Python в workflow (`.github/` он не читает). Читает он рабочее дерево, а не
список файлов git: черновой скрипт с `load_dotenv()` уронит его и до коммита.

Вторая половина файла — `scripts/diag_env_sources.py`: скрипт для сервера, который
называет имена из `.env` и их отношение к файлу секретов. Значений он не печатает,
в том числе на оборванных файлах, — с одним пределом, записанным в его шапке:
обрывок значения из одних прописных букв, цифр и подчёркиваний от имени не
отличить. `same` он говорит, только когда все читатели файла получат одно и то же.
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
SKIPPED_DIRS = {"node_modules", "__pycache__", "frontend", "venv", "site-packages", "build", "dist"}
THIS_TEST = "tests/test_env_file_precedence.py"


def _dotenv_problems(name: str, source: str) -> list[str]:
    """Чем файл нарушает правило; пусто — не нарушает."""
    problems: list[str] = []
    mentions = 0
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
        if isinstance(node, ast.keyword) and node.arg == "env_file":
            # `uvicorn.run(..., env_file=".env")` — тот же python-dotenv, только из uvicorn.
            problems.append("env_file= hands a .env to another reader")
        if isinstance(node, ast.Name) and node.id == "load_dotenv":
            mentions += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "load_dotenv":
                calls.append(node)
    for call in calls:
        if ast.unparse(call) != PINNED_CALL:
            problems.append(f"line {call.lineno}: {ast.unparse(call)} is not {PINNED_CALL}")
    # Имя встречается один раз — в самом вызове: второе упоминание — это псевдоним
    # (`again = load_dotenv`), через который вызов уйдёт из-под стража.
    expected = 1 if name in DOTENV_CALLERS else 0
    if (len(calls), mentions) != (expected, expected):
        problems.append(
            f"load_dotenv: {len(calls)} calls, {mentions} mentions; {expected} expected"
        )
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
    # Этот файл сам полон запрещённых примеров — они тут строками.
    sources.pop(THIS_TEST)
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
        _IMPORT + PINNED_CALL + "\nagain = load_dotenv\nagain(override=True)",
        _IMPORT + PINNED_CALL + "\nimport uvicorn\nuvicorn.run('src.api:app', env_file='.env')",
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
    for other in ("src/api.py", "scripts/audit_matches.py", "tests/conftest.py"):
        assert _dotenv_problems(other, source)
    assert _dotenv_problems("src/api.py", "import uvicorn\nuvicorn.run('a:b', env_file='.env')")


def test_env_file_is_read_the_same_way_in_all_three_places() -> None:
    sources = _python_sources()
    # Страж видит файлы, которые держит, и тесты — тоже: `load_dotenv()` в
    # conftest вернул бы воркитри чужой `.env`.
    assert {*DOTENV_CALLERS, "tests/conftest.py"} <= sources.keys()
    found = {name: _dotenv_problems(name, text) for name, text in sources.items()}
    assert {name: problems for name, problems in found.items() if problems} == {}


# ── Проверка «выложено ли» из документации отличает прежний код от нового ──────

DEPLOY_CHECK = "grep -c '^load_dotenv(override=True)' /opt/pharmacy-monitor/src/main.py"


def _grep_count(path: Path) -> int:
    pattern = DEPLOY_CHECK.split("'")[1]
    result = subprocess.run(["grep", "-c", pattern, str(path)], capture_output=True, text=True)
    return int(result.stdout.strip())


def test_documented_deploy_check_tells_old_code_from_new(tmp_path: Path) -> None:
    current = (REPO / "src/main.py").read_text(encoding="utf-8")
    new_call = PINNED_CALL.replace("'", '"')
    assert current.count(new_call + "\n") == 1
    previous = tmp_path / "previous_main.py"
    previous.write_text(current.replace(new_call, "load_dotenv(override=True)"), encoding="utf-8")

    # Команда и то, что значит её ответ, записаны в обоих документах. Переносы
    # строк в тексте документа не важны: сверяются слова.
    runbook = " ".join(RUNBOOK.read_text(encoding="utf-8").split())
    claude_md = " ".join(CLAUDE_MD.read_text(encoding="utf-8").split())
    assert f'ssh root@13.140.186.143 "{DEPLOY_CHECK}"' in runbook
    assert "`0` — выложено; `1` — выложенный код по-прежнему берёт `.env`" in runbook
    assert f"`{DEPLOY_CHECK}` (0 — выложено)" in claude_md

    assert _grep_count(previous) == 1
    assert _grep_count(REPO / "src/main.py") == 0
    # И неточный шаблон не соврёт: рядом с `load_dotenv` слова прежнего вызова нет.
    assert not [
        line for line in current.splitlines() if "load_dotenv" in line and "override" in line
    ]


# ── scripts/diag_env_sources.py: имена и same/differs/absent/unsure, без значений ──

NBSP = " "

# Значения и обрывки значений из файлов ниже: в выводе не должно быть ни одного.
SECRETS = (
    "alpha-value-1",
    "Alpha-Value-1",
    "beta-file-2",
    "beta-server-9",
    "gamma-only-3",
    "delta 4",
    "eps\\ilon",
    "zeta$eta",
    "zeta-plain",
    "theta#iota",
    "kappa'lambda",
    "kappa-plain",
    "8080",
    "4821",
    "omega-other-7",
    "weird-switch-8",
    "sigmaTau",
    "http_proxy",
    "QUJDREVGRw",
    "MFRGGZDFMZTWQ2LKCANARYSECRET",
    "TOKEN",
    "hunter2",
    "upsilon-key-9",
    "KEY_BODY",
)

# Простой файл: каждая строка — пустая, комментарий или `ИМЯ=значение`.
ENV_FILE_TEXT = (
    "A_SAME=alpha-value-1\n"
    'B_DIFF="beta-file-2"\n'
    "export C_ONLY=gamma-only-3\n"
    "# комментарий\n"
    "\n"
    'D_QUOTED="delta 4"\n'
    "E_BACKSLASH=eps\\ilon\n"
    "F_DOLLAR=zeta$eta\n"
    "G_HASH=theta#iota\n"
    "H_INNER_QUOTE=kappa'lambda\n"
    "J_SERVER_QUOTED=alpha-value-1\n"
    "K_CASE=alpha-value-1\n"
    "L_NBSP=alpha-value-1\n"
    "M_EMPTY=\n"
    "PORT=8080\n"
    "Q_CR=alpha-value-1\n"
    "R_EDGE_SPACE=alpha-value-1\n"
    "  SCRAPE_REPORT_EMAIL=1  \n"
    # Дальше — имена, которые не печатаются: только считаются.
    "sigmaTau=alpha-value-1\n"  # строчные буквы
    "http_proxy=alpha-value-1\n"  # строчные, хоть и с подчёркиванием
    "QUJDREVGRw==\n"  # хвост значения, оборванного переводом строки
    "MFRGGZDFMZTWQ2LKCANARYSECRET====\n"  # такой же хвост, но целиком прописными
    "TOKEN=alpha-value-1\n"  # без подчёркивания, и в файле секретов такого имени нет
)

SERVER_FILE_TEXT = (
    "# файл секретов\n"
    "\n"
    "A_SAME=alpha-value-1\n"
    "B_DIFF=beta-server-9\n"
    "D_QUOTED=delta 4\n"
    "E_BACKSLASH=eps\\ilon\n"
    "F_DOLLAR=zeta-plain\n"
    "G_HASH=theta#iota\n"
    "H_INNER_QUOTE=kappa-plain\n"
    'J_SERVER_QUOTED="alpha-value-1"\n'  # службы кавычки снимут, цикл workflow — нет
    "K_CASE=Alpha-Value-1\n"
    f"L_NBSP=alpha-value-1{NBSP}\n"  # хвост, оставленный вставкой из чата
    "M_EMPTY=\n"
    "PORT=8080\n"
    # Края значения systemd обрежет, а цикл workflow оставит как есть.
    "Q_CR=alpha-value-1\r\n"
    "R_EDGE_SPACE=alpha-value-1 \n"
    "AI_FALLBACK_ENABLED=1\n"
    "EMPTY_SWITCH=\n"
    "LOUD_SWITCH=TRUE\n"
    "ODD_SWITCH=weird-switch-8\n"
    "PIN_SWITCH=4821\n"
    "lower_switch=on\n"
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
  J_SERVER_QUOTED      unsure
  K_CASE               differs
  L_NBSP               unsure
  M_EMPTY              same
  PORT                 same
  Q_CR                 unsure
  R_EDGE_SPACE         unsure
  SCRAPE_REPORT_EMAIL  absent
  names not shown: 5
"""
SERVER_NAMES = 21
NOT_PLAIN = (
    "  NOT a plain NAME=value file from line {line} on (a quote without its pair, a line "
    "continuation, a control or non-ASCII character, or a line that is not NAME=value)\n"
)


@dataclass(frozen=True)
class Server:
    root: Path
    env: Path
    units: Path

    @property
    def stray(self) -> Path:
        return self.root / ".env"


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


def _names(server: Server, count: int, path: Path | None = None) -> str:
    return f"names in {path or server.stray} ({count}), against the server file:"


def _server_line(server: Server, count: int, *, stopped_at: int = 0) -> str:
    tail = f" above line {stopped_at}, the rest not read" if stopped_at else ""
    return f"server file {server.env}: {count} names{tail}"


def _found_line(path: Path, kind: str = "FOUND") -> str:
    import grp
    import pwd
    import time

    st = path.stat()
    owner = f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name}"
    modified = time.strftime("%Y-%m-%d", time.gmtime(st.st_mtime))
    return (
        f"  {path}  {kind}  mode={st.st_mode & 0o7777:o} owner={owner} "
        f"size={st.st_size} modified={modified}\n"
    )


def test_diag_names_every_variable_and_its_relation_to_the_server_file(server: Server) -> None:
    result = _diag(server)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert _section(result.stdout, _names(server, 16)) == EXPECTED_NAMES
    assert _section(result.stdout, _server_line(server, SERVER_NAMES)) == ""


def test_diag_prints_exactly_this_and_nothing_else(server: Server) -> None:
    """Вывод сверяется целиком: обрывку значения, его длине или хешу в нём нет места."""
    above = [parent / ".env" for parent in server.root.parents]
    if any(path.exists() for path in above):
        pytest.skip("a .env above the temporary directory would add lines of its own")
    result = _diag(server, "--flag", "AI_FALLBACK_ENABLED", "--flag", "ODD_SWITCH")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    for secret in SECRETS:
        assert secret not in result.stdout
    scrape = server.units / "pharmacy-monitor-scrape@.service.d/zz-realtime-alerts.conf"
    assert result.stdout == (
        f"python-dotenv in {server.root}/.venv: not found\n"
        f"deployed {server.root}/src/main.py: new rule - own checkout root only, "
        "the environment wins\n" + _server_line(server, SERVER_NAMES) + "\n"
        "where a path-less load_dotenv() looked, from the deployed code:\n"
        f"  {server.root}/src/.env  -\n"
        + _found_line(server.stray)
        + "".join(f"  {path}  -\n" for path in above)
        + f"  {server.root}/migrations/.env  -\n"
        + _names(server, 16)
        + "\n"
        + EXPECTED_NAMES
        + f"units under {server.units} whose EnvironmentFile is a .env next to the code:\n"
        "  none\n"
        "names above that a pharmacy-monitor unit sets with Environment=:\n"
        f"  SCRAPE_REPORT_EMAIL  {scrape}\n"
        "switches in the server file:\n"
        "  AI_FALLBACK_ENABLED  1\n"
        "  ODD_SWITCH  set (value not shown)\n"
    )


# Строки `.env`, после которых файлу нельзя верить построчно. В каждой — причина,
# по которой настоящий python-dotenv прочёл бы файл иначе, чем «по строке на имя».
NOT_PLAIN_ENV_LINES = [
    "postgresql://pm:hunter2@localhost/db?sslmode=require",  # не имя
    "просто строка без знака равенства",
    "'upsilon-key-9'=x",  # имя в кавычках python-dotenv принимает
    "'A_SAME'=beta-file-2",  # …и так переопределяет имя, названное выше
    "'note",  # незакрытое имя в кавычках тянется на следующие строки
    'N_MULTILINE="-----BEGIN PRIVATE KEY-----',  # значение на несколько строк
    'NOTE_TEXT="first \\" still open',  # кавычка под обратной косой чертой не закрывает
    "NOTE_TEXT='one",
    'NOTE_TEXT="a" "b',  # закрыта и открыта снова
    'NOTE_TEXT="closed" # а дальше комментарий',  # читается, но не нами
    "W_A=1\rA_SAME=beta-file-2",  # CR посреди строки для python-dotenv — перевод строки
    f"{NBSP}A_SAME=beta-file-2",  # пробелом он считает и это, и срезает перед именем
    "\x0cA_SAME=beta-file-2",
    " A_SAME=beta-file-2",
    "export\x0bA_SAME=beta-file-2",
    f"A_SAME{NBSP}=beta-file-2",
    "A_SAME = beta-file-2",
    f'W_A={NBSP}"abc',
    "W_A=a\tb",
    "W_A=é",
]


@pytest.mark.parametrize("line", NOT_PLAIN_ENV_LINES)
def test_diag_answers_unsure_for_an_env_file_that_is_not_plain(server: Server, line: str) -> None:
    """Ниже странной строки может быть и продолжение значения, и второе определение имени."""
    text = f"A_SAME=alpha-value-1\n# комментарий\n{line}\nKEY_BODY=alpha-value-1\n"
    server.stray.write_text(text, encoding="utf-8", newline="")

    result = _diag(server)

    assert result.returncode == 0, result.stderr
    assert result.stdout.isascii()
    assert _section(result.stdout, _names(server, 1)) == (
        "  A_SAME  unsure\n"
        "  names not shown: 0\n"
        + NOT_PLAIN.format(line=3)
        + "  the file was not read past that line: every answer above is unsure\n"
    )
    for secret in SECRETS:
        assert secret not in result.stdout


# То же для файла секретов: его читают systemd и цикл в workflow, и оба — по-своему.
NOT_PLAIN_SERVER_LINES = [
    "W_JOINED=abc\\",  # systemd приклеит следующую строку
    'W_JOINED="a" "b',  # начато кавычкой и это не одна пара: такое не разбираем
    "W_JOINED='open",
    "W_A=1\rA_SAME=beta-file-2",
    " A_SAME=beta-file-2",  # systemd пробел срежет и имя переопределит; цикл остановится
    "A_SAME+=tail",  # цикл допишет к значению, systemd строку пропустит
    "A_SAME",  # цикл выставит имя самому себе
    "export A_SAME=beta-file-2",  # systemd пропустит, цикл остановится
    "; комментарий по-systemd",  # для цикла это не комментарий
    " # комментарий с отступом",
    "   ",
]


@pytest.mark.parametrize("line", NOT_PLAIN_SERVER_LINES)
def test_diag_compares_nothing_against_a_server_file_that_is_not_plain(
    server: Server, line: str
) -> None:
    server.stray.write_text("A_SAME=alpha-value-1\nB_DIFF=beta-file-2\nC_ONLY=gamma-only-3\n")
    text = f"A_SAME=alpha-value-1\n{line}\nB_DIFF=beta-file-2\n"
    server.env.write_text(text, encoding="utf-8", newline="")

    result = _diag(server, "--flag", "A_SAME", "--flag", "B_DIFF", "--flag", "NOT_THERE")

    assert result.returncode == 0, result.stderr
    assert _section(result.stdout, _server_line(server, 1, stopped_at=2)) == (
        NOT_PLAIN.format(line=2)
        + "  nothing can be compared against it: every answer below is unsure\n"
    )
    # И про переключатель тоже: он может стоять или меняться ниже этой строки.
    assert _section(result.stdout, "switches in the server file:") == (
        "  A_SAME  unsure\n  B_DIFF  unsure\n  NOT_THERE  unsure\n"
    )
    # Ни `same`, ни `absent`: имя может стоять ниже строки, на которой чтение кончилось.
    assert _section(result.stdout, _names(server, 3)) == (
        "  A_SAME  unsure\n  B_DIFF  unsure\n  C_ONLY  unsure\n  names not shown: 0\n"
    )


@pytest.mark.parametrize(
    ("in_env", "in_server"),
    [
        ("X_ONE=abc", f"X_ONE=abc{NBSP}"),  # python-dotenv срезал бы, systemd оставит
        ("X_ONE=abc", f"X_ONE={NBSP}abc"),
        ("X_ONE=abc", "X_ONE=abc "),
        ("X_ONE=abc", "X_ONE=abc\x1f"),
        ("X_ONE=ab", "X_ONE=a\tb"),
        ("X_ONE=abc", "X_ONE=é"),
        ("X_ONE=abc", "X_ONE=abc "),  # края systemd срежет, цикл workflow оставит
        ("X_ONE=abc", "X_ONE= abc"),
        ("X_ONE=abc", "X_ONE=abc\r"),
        ("X_ONE='abc'", "X_ONE='abc'"),  # кавычки тоже: systemd снимет, цикл нет
        ("X_ONE=abc", 'X_ONE="abc"'),
        ("X_ONE=a\\b", "X_ONE=a\\b"),
        ("X_ONE=a$b", "X_ONE=a$b"),
        ("X_ONE=a#b", "X_ONE=a#b"),
        ("X_ONE=a`b", "X_ONE=a`b"),
        ('X_ONE="a$b"', "X_ONE=a$b"),
        ("X_ONE=a'b", "X_ONE=ab"),
        ("X_ONE=its", "X_ONE=it's"),  # кавычка посреди значения в файле секретов
        ("X_ONE=abc", "X_ONE=abc\nX_ONE=abc"),  # дважды: `grep -m1` возьмёт первое
        ("X_ONE=xyz", "X_ONE=abc\nX_ONE=xyz"),
    ],
)
def test_diag_is_unsure_about_values_readers_would_not_agree_on(
    server: Server, in_env: str, in_server: str
) -> None:
    server.stray.write_text(in_env + "\n", encoding="utf-8", newline="")
    server.env.write_text(in_server + "\n", encoding="utf-8", newline="")

    result = _diag(server)

    assert result.returncode == 0, result.stderr
    assert result.stdout.isascii()
    assert f"{_server_line(server, 1)}\nwhere" in result.stdout  # файл секретов простой
    assert _section(result.stdout, _names(server, 1)) == "  X_ONE  unsure\n  names not shown: 0\n"


@pytest.mark.parametrize(
    ("in_env", "in_server", "answer"),
    [
        ("X_ONE=abc", "X_ONE=abc", "same"),
        ('X_ONE="a b"', "X_ONE=a b", "same"),  # кавычки в `.env` python-dotenv снимает
        ("X_ONE='a b'", "X_ONE=a b", "same"),
        ("  export X_ONE= abc  ", "X_ONE=abc", "same"),
        ("X_ONE=abc\r", "X_ONE=abc", "same"),  # CRLF в `.env`
        ("X_ONE=abc\nX_ONE=xyz", "X_ONE=xyz", "same"),  # действует последнее определение
        ("X_ONE=abc", "# заметка с чертой в конце\\\nX_ONE=abc", "same"),
        ("X_ONE=abc", "X_ONE=ABC", "differs"),
        ("X_ONE=a b", "X_ONE=a  b", "differs"),
        ("X_ONE=", "X_ONE=0", "differs"),
        ("X_ONE=abc", "Y_TWO=abc", "absent"),
        ("X_ONE=abc", "x_one=abc", "absent"),
    ],
)
def test_diag_compares_plain_values_exactly(
    server: Server, in_env: str, in_server: str, answer: str
) -> None:
    server.stray.write_text(in_env + "\n", encoding="utf-8", newline="")
    server.env.write_text(in_server + "\n", encoding="utf-8", newline="")

    result = _diag(server)

    assert result.returncode == 0, result.stderr
    assert _section(result.stdout, _names(server, 1)) == (
        f"  X_ONE  {answer}\n  names not shown: 0\n"
    )


def test_diag_says_when_a_file_is_not_valid_utf8(server: Server) -> None:
    server.stray.write_bytes(b"X_ONE=ab\xffc\n")
    server.env.write_bytes(b"X_ONE=ab\xfec\n")

    result = _diag(server)

    assert result.returncode == 0, result.stderr
    assert _section(result.stdout, _server_line(server, 1)) == (
        "  NOT valid UTF-8: systemd would refuse this file\n"
    )
    assert _section(result.stdout, _names(server, 0)) == (
        "  names not shown: 0\n"
        + NOT_PLAIN.format(line=1)
        + "  the file was not read past that line: every answer above is unsure\n"
        "  NOT valid UTF-8: python-dotenv would fail on this file\n"
    )


def test_diag_says_only_a_switch_word_about_a_switch(server: Server) -> None:
    result = _diag(
        server,
        *("--flag", "AI_FALLBACK_ENABLED"),
        *("--flag", "EMPTY_SWITCH"),
        *("--flag", "LOUD_SWITCH"),
        *("--flag", "lower_switch"),
        *("--flag", "NOT_THERE"),
        *("--flag", "ODD_SWITCH"),
        *("--flag", "PIN_SWITCH"),
        *("--flag", "E_BACKSLASH"),
        *("--flag", "J_SERVER_QUOTED"),
    )

    assert result.returncode == 0, result.stderr
    assert _section(result.stdout, "switches in the server file:") == (
        "  AI_FALLBACK_ENABLED  1\n"
        "  EMPTY_SWITCH  empty\n"  # пусто — не «выключено»: у части переключателей это «включено»
        "  LOUD_SWITCH  true\n"
        "  lower_switch  on\n"
        "  NOT_THERE  unset\n"
        "  ODD_SWITCH  set (value not shown)\n"
        "  PIN_SWITCH  set (value not shown)\n"  # короткое, но не служебное слово
        "  E_BACKSLASH  set (value not shown)\n"
        "  J_SERVER_QUOTED  set (value not shown)\n"
    )


def test_diag_names_units_that_use_the_file_as_their_environment(server: Server) -> None:
    (server.units / "legacy.service").write_text(f"[Service]\nEnvironmentFile=-{server.stray}\n")
    dropin = server.units / "pharmacy-monitor-run.service.d"
    dropin.mkdir()
    (dropin / "override.conf").write_text(f'[Service]\nEnvironmentFile="{server.stray}"\n')
    (server.units / "multi-user.target.wants").mkdir()
    (server.units / "multi-user.target.wants/legacy.service").symlink_to(
        server.units / "legacy.service"
    )
    heading = f"units under {server.units} whose EnvironmentFile is a .env next to the code:"
    expected = f"  {server.units / 'legacy.service'}\n  {dropin / 'override.conf'}\n"

    result = _diag(server)
    assert result.returncode == 0, result.stderr
    assert _section(result.stdout, heading) == expected

    # Файл убрали, а юнит по-прежнему на него ссылается — это надо увидеть и тогда.
    server.stray.unlink()
    result = _diag(server)
    assert result.returncode == 0, result.stderr
    assert _section(result.stdout, heading) == expected
    assert "names above that a pharmacy-monitor unit sets" not in result.stdout


def test_diag_names_units_that_set_the_same_names_themselves(server: Server) -> None:
    everyone = server.units / "service.d"
    everyone.mkdir()
    (everyone / "all.conf").write_text('[Service]\nEnvironment="A_SAME=omega-other-7"\n')
    continued = server.units / "pharmacy-monitor-health.service"
    continued.write_text("[Service]\nEnvironment=OTHER_NAME=1 \\\n  B_DIFF=beta-server-9\n")
    hidden = server.units / "pharmacy-monitor-secret.service"
    hidden.write_text("[Service]\nEnvironment=C_ONLY=gamma-only-3\n")
    hidden.chmod(0)

    result = _diag(server)

    assert result.returncode == 0, result.stderr
    scrape = server.units / "pharmacy-monitor-scrape@.service.d/zz-realtime-alerts.conf"
    expected = (
        f"  A_SAME  {everyone / 'all.conf'}\n"
        f"  B_DIFF  {continued}\n"
        f"  SCRAPE_REPORT_EMAIL  {scrape}\n"
    )
    if not os.access(hidden, os.R_OK):  # под root запрет на чтение не действует
        expected += "  unit files this user could not read: 1\n"
    else:
        expected = expected.replace("  SCRAPE", f"  C_ONLY  {hidden}\n  SCRAPE")
    heading = "names above that a pharmacy-monitor unit sets with Environment=:"
    assert _section(result.stdout, heading) == expected
    for secret in SECRETS:
        assert secret not in result.stdout


def test_diag_looks_above_the_code_and_under_migrations(server: Server) -> None:
    server.stray.unlink()
    above = server.root.parent / ".env"
    above.write_text("A_SAME=alpha-value-1\nL_ABOVE=gamma-only-3\n")
    (server.root / "migrations").mkdir()
    under = server.root / "migrations/.env"
    under.write_text("B_DIFF=beta-file-2\n")

    result = _diag(server)

    assert result.returncode == 0, result.stderr
    assert _section(result.stdout, _names(server, 2, above)) == (
        "  A_SAME   same\n  L_ABOVE  absent\n  names not shown: 0\n"
    )
    assert _section(result.stdout, _names(server, 1, under)) == (
        "  B_DIFF  differs\n  names not shown: 0\n"
    )


def test_diag_without_env_file_says_so(server: Server) -> None:
    server.stray.unlink()

    result = _diag(server, "--flag", "AI_FALLBACK_ENABLED")

    assert result.returncode == 0, result.stderr
    assert "no .env next to the code or above it: nothing to compare\n" in result.stdout
    assert "names in" not in result.stdout
    assert _section(result.stdout, "switches in the server file:") == "  AI_FALLBACK_ENABLED  1\n"


def test_diag_tells_which_rule_the_deployed_code_follows(server: Server) -> None:
    deployed = server.root / "src/main.py"
    line = f"deployed {deployed}: "
    assert line + "new rule" in _diag(server).stdout

    deployed.write_text("import os\nload_dotenv(override=True)\n" + deployed.read_text("utf-8"))
    assert line + "OLD rule" in _diag(server).stdout

    deployed.write_text("# load_dotenv(override=True) было здесь\n", encoding="utf-8")
    assert line + "neither call found" in _diag(server).stdout

    deployed.unlink()
    assert line + "cannot read (FileNotFoundError)" in _diag(server).stdout


def test_diag_reads_the_dotenv_version_from_the_directory_name_only(server: Server) -> None:
    packages = server.root / ".venv/lib/python3.12/site-packages"
    packages.mkdir(parents=True)
    line = f"python-dotenv in {server.root}/.venv: "
    forged = "no .env next to the code or above it: nothing to compare"
    (packages / f"python_dotenv-1.2.2\n{forged}\nX.dist-info").mkdir()
    result = _diag(server)
    assert line + "not found\n" in result.stdout
    assert forged not in result.stdout

    (packages / "python_dotenv-1.2.2.dist-info").mkdir()
    assert line + "1.2.2\n" in _diag(server).stdout


def test_diag_escapes_what_a_file_name_could_forge(server: Server) -> None:
    forged = "  DATABASE_URL  same"
    unit = server.units / f"zz\n{forged}\n.conf"
    unit.write_text(f"[Service]\nEnvironmentFile={server.stray}\n")

    result = _diag(server)

    assert result.returncode == 0, result.stderr
    assert forged not in result.stdout.splitlines()
    assert f"  {server.units}/zz\\n{forged}\\n.conf\n" in result.stdout


def test_diag_refuses_when_the_server_file_cannot_be_read(server: Server) -> None:
    result = _diag(server, env=server.root / "no-such-file")

    assert result.returncode == 2
    assert result.stdout.endswith(
        f"server file {server.root / 'no-such-file'}: cannot read (FileNotFoundError)\n"
    )


def test_diag_does_not_compare_what_is_not_a_readable_file(server: Server) -> None:
    stray = server.stray
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

    (server.root / "nowhere").write_text("A_SAME=alpha-value-1\n")
    result = _diag(server)
    assert result.returncode == 0, result.stderr
    assert _found_line(stray, "FOUND (symlink)") in result.stdout
    assert _section(result.stdout, _names(server, 1)) == "  A_SAME  same\n  names not shown: 0\n"

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
    home = tmp_path / "home"
    home.mkdir()

    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            str(path): (path.lstat().st_size, path.lstat().st_mtime_ns)
            for path in sorted(tmp_path.rglob("*"))
        }

    before = snapshot()
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-", "--root", str(server.root), "--server-env"]
        + [str(server.env), "--units", str(server.units), "--flag", "AI_FALLBACK_ENABLED"],
        input=DIAG_SCRIPT.read_text(encoding="utf-8"),
        cwd=home,
        env=_clean_env(HOME=str(home), TMPDIR=str(home)),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert snapshot() == before


def test_runbook_command_feeds_this_script_to_a_plain_interpreter_as_pm() -> None:
    """Команда в RUNBOOK называет существующий файл и подаёт его через stdin."""
    assert DIAG_SCRIPT.is_file()
    command = (
        "ssh root@13.140.186.143 'runuser -u pm -- python3 -I -' < scripts/diag_env_sources.py"
    )
    assert command + "\n" in RUNBOOK.read_text(encoding="utf-8")
    assert command + "\n" in DIAG_SCRIPT.read_text(encoding="utf-8")
