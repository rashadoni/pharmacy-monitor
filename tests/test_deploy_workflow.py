"""Выкладка: сначала база, потом код — и ничего на сервере не меняется до миграции.

Зачем тест: `deploy.yml` копировал код раньше, чем делал бэкап и миграцию. Около
50 секунд (замер 2026-10-07 на двух выкладках: 51 и 49,5 с) новый код лежал на
старой схеме, а таймеры запускают CLI из этого каталога весь день — вотчер
очереди раз в минуту. Процесс, стартовавший в окно, падал на первом запросе к
новой колонке. Упавшая миграция оставляла это состояние насовсем.

Workflow нельзя запустить локально, поэтому тест делает то, что делает раннер:
берёт шаги из самого `deploy.yml`, подставляет выражения `${{ … }}` и исполняет
их bash-ем по порядку. Сервер заменён песочницей:
- `ssh` исполняет удалённую команду локально, переписав `/opt/pharmacy-monitor`
  и `/etc/pharmacy-monitor` на каталоги песочницы;
- `rsync` копирует туда же настоящим rsync;
- Alembic настоящий, база — SQLite; миграции пробные;
- `sudo`, `pnpm`, `curl`, `sleep`, `systemctl` — заглушки, которые пишут журнал.

Каждая заглушка записывает, какая ревизия была в базе и какой релиз лежал в
живом каталоге в момент вызова. По этому журналу и проверяется главное: в момент
`alembic upgrade` в живом каталоге ещё прошлый релиз, а в момент первого
копирования в живой каталог база уже на голове релиза.

Чего тест не видит: настоящих ssh и прав на сервере, sudoers, длительностей,
PostgreSQL (блокировки проверяет `test_migration_lock_timeout.py` — оттуда же
слово `LockNotAvailable`, которое здесь подставляет заглушка), сборки фронтенда.
Первая выкладка после правки workflow остаётся проверкой на живом сервере.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"

PREFLIGHT_REVISION = "Preflight production DB revision against checkout"
STAGE = "Stage release runtime (nothing live changes)"
VERIFY_GRAPH = "Verify staged migration graph has one head"
MIGRATE = "Verified backup + apply migrations (before any code is replaced)"
GATE = "Refuse to replace code unless the DB is at the staged head"
RSYNC = "Rsync code -> prod (src + frontend + canonical migrations)"
RESTART = "Build frontend + restart services"
CLEANUP = "Remove staged release runtime"

REAL_RSYNC = shutil.which("rsync")

BASE = "0001_base"
HEAD = "0002_needs_new_src"

MIGRATIONS = {
    BASE: '''"""base"""

import sqlalchemy as sa
from alembic import op

revision: str = "0001_base"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("probe", sa.Column("id", sa.Integer(), primary_key=True))


def downgrade() -> None:
    op.drop_table("probe")
''',
    # Импортирует модуль, которого нет в прошлом релизе: миграция обязана
    # исполняться на src своего релиза, а не на том, что лежит в живом каталоге.
    HEAD: '''"""needs the src of its own release"""

import sqlalchemy as sa
from alembic import op

from src.release_probe import VALUE

revision: str = "0002_needs_new_src"
down_revision: str | None = "0001_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("probe", sa.Column("note", sa.String(), nullable=True))
    op.execute(sa.text("INSERT INTO probe (id, note) VALUES (1, :note)").bindparams(note=VALUE))


def downgrade() -> None:
    op.drop_column("probe", "note")
''',
    "0002b_second_head": '''"""a second head"""

revision: str = "0002b_second_head"
down_revision: str | None = "0001_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
''',
    "0009_only_on_prod": '''"""a revision the release being deployed does not know"""

revision: str = "0009_only_on_prod"
down_revision: str | None = "0001_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
''',
}

SBX_HELPER = r"""
import json
import os
import pathlib
import re
import sqlite3
import time

SBX = pathlib.Path(os.environ["SBX_ROOT"])
SERVER = SBX / "server"
LIVE = SERVER / "opt" / "pharmacy-monitor"


def to_sandbox(text):
    # Не трогаем путь, который уже внутри песочницы: перед ним стоит символ пути.
    return re.sub(
        r"(?<![\w./-])/(opt|etc)/pharmacy-monitor",
        lambda match: f"{SERVER}/{match.group(1)}/pharmacy-monitor",
        text,
    )


def from_sandbox(text):
    return text.replace(f"{SERVER}/opt/pharmacy-monitor", "/opt/pharmacy-monitor").replace(
        f"{SERVER}/etc/pharmacy-monitor", "/etc/pharmacy-monitor"
    )


def db_revision():
    connection = sqlite3.connect(SERVER / "data" / "db.sqlite")
    try:
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    return rows[0][0] if rows else None


def event(kind, **fields):
    record = {
        "kind": kind,
        "db_revision": db_revision(),
        "live_release": (LIVE / "src" / "RELEASE").read_text().strip(),
        "at": time.monotonic_ns(),
        **fields,
    }
    with open(SBX / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
"""

STUBS = {
    "ssh": r"""
import subprocess
import sys

import _sbx

args = sys.argv[1:]
index = 0
while index < len(args) and args[index].startswith("-"):
    index += 2 if args[index] in {"-i", "-o", "-p", "-l", "-F"} else 1
destination, remote = args[index], " ".join(args[index + 1 :])
if destination != "pm@13.140.186.143":
    sys.exit(f"stub ssh: unexpected destination {destination}")
script = sys.stdin.read()
_sbx.event("ssh", remote=remote, script=script)
result = subprocess.run(
    ["bash", "-c", _sbx.to_sandbox(remote)],
    input=_sbx.to_sandbox(script),
    text=True,
    capture_output=True,
    cwd=_sbx.LIVE,  # домашний каталог pm на сервере
)
sys.stdout.write(_sbx.from_sandbox(result.stdout))
sys.stderr.write(_sbx.from_sandbox(result.stderr))
sys.exit(result.returncode)
""",
    "rsync": r"""
import os
import sys

import _sbx

args, translated, destination = sys.argv[1:], [], None
index = 0
while index < len(args):
    arg = args[index]
    if arg == "-e":
        index += 2
        continue
    if arg.startswith("pm@13.140.186.143:"):
        destination = arg.split(":", 1)[1]
        arg = _sbx.to_sandbox(destination)
    translated.append(arg)
    index += 1
if destination is None:
    sys.exit("stub rsync: no remote destination")
_sbx.event("rsync", destination=destination, live=".deploy-stage." not in destination)
real = os.environ["SBX_REAL_RSYNC"]
os.execv(real, [real, *translated])
""",
    "sudo": r"""
import sys

import _sbx

_sbx.event("sudo", argv=sys.argv[1:])
""",
    "pnpm": r"""
import sys

import _sbx

_sbx.event("pnpm", argv=sys.argv[1:])
""",
    "curl": r"""
import sys

import _sbx

_sbx.event("curl", argv=sys.argv[1:])
print('<html lang="ru">')
""",
    "sleep": r"""
import sys

import _sbx

_sbx.event("sleep", seconds=sys.argv[1])
""",
    "systemctl": r"""
import sys

import _sbx

args = sys.argv[1:]
if args[:2] == ["show", "--property"]:
    answers = {
        "User": "pm",
        "FragmentPath": str(_sbx.SERVER / "units" / args[-1]),
        "DropInPaths": "",
    }
    print(answers[args[2]])
""",
    "stat": r"""
import os
import sys

if sys.argv[1:3] == ["-c", "%U"]:
    print("root")  # юниты на сервере принадлежат root; в песочнице root-а нет
    sys.exit(0)
os.execv("/usr/bin/stat", ["stat", *sys.argv[1:]])
""",
}

# `.venv/bin/python` сервера: пишет в журнал каждый вызов Alembic и умеет
# подставить отказ вместо `upgrade`, остальное отдаёт настоящему интерпретатору.
VENV_PYTHON = r"""
import json
import os
import sys

sys.path.insert(0, os.environ["SBX_BIN"])
import _sbx

args = sys.argv[1:]
if args[:2] == ["-m", "alembic"]:
    rest = args[2:]
    if rest[:1] == ["-c"]:
        rest = rest[2:]
    command = rest[0]
    _sbx.event(
        "alembic",
        command=command,
        cwd=_sbx.from_sandbox(os.getcwd()),
        pythonpath=_sbx.from_sandbox(os.environ.get("PYTHONPATH", "")),
        lock_timeout_ms=os.environ.get("MIGRATION_LOCK_TIMEOUT_MS"),
    )
    plan_path = _sbx.SBX / "upgrade-failures.json"
    if command == "upgrade" and plan_path.exists():
        plan = json.loads(plan_path.read_text())
        if plan:
            failure = plan.pop(0)
            plan_path.write_text(json.dumps(plan))
            messages = {
                "lock": "sqlalchemy.exc.OperationalError: (psycopg.errors.LockNotAvailable) "
                "canceling statement due to lock timeout",
                "error": 'sqlalchemy.exc.ProgrammingError: (psycopg.errors.UndefinedTable) '
                'relation "missing" does not exist',
            }
            print(messages[failure], file=sys.stderr)
            sys.exit(1)
real = os.environ["SBX_REAL_PYTHON"]
os.execv(real, [real, *args])
"""

BACKUP_STUB = r"""#!/usr/bin/env bash
set -eo pipefail
"$SBX_REAL_PYTHON" - <<'PY'
import os
import sys

sys.path.insert(0, os.environ["SBX_BIN"])
import _sbx

_sbx.event("backup")
PY
if [[ -e "$SBX_ROOT/backup-must-fail" ]]; then
    echo "pg_dump: error: connection to server failed" >&2
    exit 1
fi
echo "==> Done."
"""


def _write_executable(path: Path, body: str, *, python: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (f"#!{sys.executable}\n" if python else "") + body.lstrip("\n"), encoding="utf-8"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_release(target: Path, *, release: str, revisions: list[str]) -> None:
    """Дерево релиза в том виде, в каком его видит workflow: чекаут или сервер."""
    (target / "src").mkdir(parents=True)
    (target / "src" / "__init__.py").write_text("", encoding="utf-8")
    (target / "src" / "storage.py").write_text(
        "from sqlalchemy.orm import DeclarativeBase\n\n\nclass Base(DeclarativeBase):\n    pass\n",
        encoding="utf-8",
    )
    (target / "src" / "RELEASE").write_text(release + "\n", encoding="utf-8")
    if release == "new":
        (target / "src" / "release_probe.py").write_text(
            'VALUE = "from the staged release"\n', encoding="utf-8"
        )

    versions = target / "migrations" / "versions"
    versions.mkdir(parents=True)
    shutil.copy(ROOT / "alembic.ini", target / "alembic.ini")
    for name in ("env.py", "script.py.mako", "README"):
        shutil.copy(ROOT / "migrations" / name, target / "migrations" / name)
    if release == "old":
        # На сервере env.py прошлого релиза: правка обязана доехать выкладкой.
        (target / "migrations" / "env.py").write_text(
            (target / "migrations" / "env.py").read_text(encoding="utf-8")
            + "\n# previous release\n",
            encoding="utf-8",
        )
    for revision in revisions:
        (versions / f"{revision}.py").write_text(MIGRATIONS[revision], encoding="utf-8")

    _write_executable(target / "infra" / "scripts" / "backup.sh", BACKUP_STUB, python=False)
    (target / "infra" / "systemd").mkdir(parents=True)
    (target / "infra" / "systemd" / "example.service").write_text(release, encoding="utf-8")
    (target / "frontend" / "src").mkdir(parents=True)
    (target / "frontend" / "package.json").write_text("{}\n", encoding="utf-8")
    (target / "frontend" / "src" / "page.tsx").write_text(f"// {release}\n", encoding="utf-8")


@dataclass
class StepResult:
    name: str
    returncode: int
    stdout: str
    stderr: str


@dataclass
class WorkflowResult:
    steps: list[StepResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def failed_step(self) -> StepResult | None:
        return next((step for step in self.steps if step.returncode != 0), None)

    def step(self, name: str) -> StepResult:
        return next(step for step in self.steps if step.name == name)

    def ran(self, name: str) -> bool:
        return any(step.name == name for step in self.steps)

    def describe(self) -> str:
        return "\n".join(
            f"--- {step.name} (exit {step.returncode})\n{step.stdout}{step.stderr}"
            for step in self.steps
        )


class Sandbox:
    def __init__(self, root: Path, *, release_revisions: list[str], live_revisions: list[str]):
        self.root = root
        self.checkout = root / "checkout"
        self.server = root / "server"
        self.live = self.server / "opt" / "pharmacy-monitor"
        self.bin = root / "bin"
        self.database = self.server / "data" / "db.sqlite"

        _write_release(self.checkout, release="new", revisions=release_revisions)
        _write_release(self.live, release="old", revisions=live_revisions)
        # Прошлый релиз выложен давно. Без этого rsync, который сверяет размер и
        # время, счёл бы файл одного размера, записанный в ту же секунду, прежним.
        yesterday = time.time() - 24 * 3600
        for path in self.live.rglob("*"):
            os.utime(path, (yesterday, yesterday))
        subprocess.run(["git", "init", "-q"], cwd=self.checkout, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.checkout, check=True)

        (self.server / "etc" / "pharmacy-monitor").mkdir(parents=True)
        (self.server / "etc" / "pharmacy-monitor" / "env").write_text(
            f"JWT_SECRET=x\nDATABASE_URL=sqlite:///{self.database}\n", encoding="utf-8"
        )
        self.database.parent.mkdir(parents=True)
        (root / "home").mkdir()
        (root / "events.jsonl").write_text("", encoding="utf-8")

        (self.bin / "_sbx.py").parent.mkdir(parents=True)
        (self.bin / "_sbx.py").write_text(SBX_HELPER, encoding="utf-8")
        for name, body in STUBS.items():
            _write_executable(self.bin / name, body)
        _write_executable(self.live / ".venv" / "bin" / "python", VENV_PYTHON)
        _write_executable(
            self.live / ".venv" / "bin" / "alembic",
            "import os\nimport sys\n\n"
            "python = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'python')\n"
            "os.execv(python, [python, '-m', 'alembic', *sys.argv[1:]])\n",
        )
        for service in ("pharmacy-monitor-api.service", "pharmacy-monitor-frontend.service"):
            unit = self.server / "units" / service
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("[Service]\n", encoding="utf-8")
            unit.chmod(0o444)

    @property
    def env(self) -> dict[str, str]:
        kept = {key: os.environ[key] for key in ("LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        return {
            **kept,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SBX_ROOT": str(self.root),
            "SBX_BIN": str(self.bin),
            "SBX_REAL_PYTHON": sys.executable,
            "SBX_REAL_RSYNC": REAL_RSYNC or "",
            "GITHUB_OUTPUT": str(self.root / "github-output"),
            "GITHUB_ENV": str(self.root / "github-env"),
        }

    def migrate_production_to(self, revision: str) -> None:
        """Состояние прода до выкладки: база на ревизии прошлого релиза."""
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision],
            cwd=self.live,
            env={**self.env, "DATABASE_URL": f"sqlite:///{self.database}"},
            check=True,
            capture_output=True,
            text=True,
        )

    def db_revision(self) -> str | None:
        with sqlite3.connect(self.database) as connection:
            return connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]

    def probe_rows(self) -> list[tuple]:
        with sqlite3.connect(self.database) as connection:
            return connection.execute("SELECT * FROM probe").fetchall()

    def events(self, kind: str | None = None, **match: object) -> list[dict]:
        lines = (self.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines]
        return [
            event
            for event in events
            if (kind is None or event["kind"] == kind)
            and all(event.get(key) == value for key, value in match.items())
        ]

    def live_digest(self) -> dict[str, str]:
        """Всё, что лежит в живом каталоге, кроме окружения и кэша байткода."""
        digest = {}
        for path in sorted(self.live.rglob("*")):
            relative = path.relative_to(self.live)
            if relative.parts[0] == ".venv" or "__pycache__" in relative.parts:
                continue
            digest[str(relative)] = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
            )
        return digest

    def staging_dirs(self) -> list[str]:
        return sorted(path.name for path in self.live.glob(".deploy-stage.*"))


def _value(reference: str, context: dict) -> object:
    reference = reference.strip()
    if reference == "''":
        return ""
    if match := re.fullmatch(r"(inputs|secrets)\.(\w+)", reference):
        return context[match.group(1)][match.group(2)]
    if match := re.fullmatch(r"steps\.(\w+)\.outputs\.(\w+)", reference):
        return context["steps"].get(match.group(1), {}).get(match.group(2), "")
    raise AssertionError(f"стенд не умеет выражение workflow: {reference!r}")


def _evaluate(expression: str, context: dict) -> bool:
    """Ровно те формы `if:`, что есть в deploy.yml; на незнакомой стенд падает."""
    results = []
    for term in expression.split("&&"):
        term = term.strip()
        if term == "always()":
            results.append(True)
        elif "!=" in term:
            left, right = term.split("!=")
            results.append(_value(left, context) != _value(right, context))
        else:
            results.append(bool(_value(term, context)))
    return all(results)


def _render(text: str, context: dict) -> str:
    def replace(match: re.Match) -> str:
        value = _value(match.group(1), context)
        return str(value).lower() if isinstance(value, bool) else str(value)

    return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", replace, text)


def run_workflow(
    sandbox: Sandbox,
    *,
    apply_migrations: bool,
    without: tuple[str, ...] = (),
    before: dict | None = None,
) -> WorkflowResult:
    """Исполнить шаги deploy.yml так, как их исполняет раннер GitHub.

    ``without`` — шаги, которых как будто нет в workflow: так проверяется, что
    следующая проверка держит и одна. ``before`` — что сделать перед шагом.
    """
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["deploy"]["steps"]
    context = {
        "inputs": {"apply_migrations": apply_migrations},
        "secrets": {"SSH_PRIVATE_KEY": "not-a-real-key"},
        "steps": {},
    }
    result = WorkflowResult()
    output_file = Path(sandbox.env["GITHUB_OUTPUT"])
    job_failed = False

    for number, step in enumerate(steps):
        if "uses" in step:
            assert step["uses"].startswith("actions/checkout@")
            continue
        name = step["name"]
        if name in without:
            continue

        condition = step.get("if")
        if condition is None:
            should_run = not job_failed
        else:
            expression = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", condition).group(1)
            should_run = _evaluate(expression, context)
            if "always()" not in expression:
                should_run = should_run and not job_failed
        if not should_run:
            result.skipped.append(name)
            continue

        if before and name in before:
            before[name]()

        script = sandbox.root / f"step-{number}.sh"
        script.write_text(_render(step["run"], context), encoding="utf-8")
        output_file.write_text("", encoding="utf-8")
        completed = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
            cwd=sandbox.checkout,
            env={
                **sandbox.env,
                **{key: _render(str(value), context) for key, value in step.get("env", {}).items()},
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=180,
        )
        result.steps.append(
            StepResult(name, completed.returncode, completed.stdout, completed.stderr)
        )
        if "id" in step:
            context["steps"][step["id"]] = dict(
                line.split("=", 1) for line in output_file.read_text().splitlines() if "=" in line
            )
        job_failed = job_failed or completed.returncode != 0
    return result


pytestmark = [
    pytest.mark.skipif(
        not all(shutil.which(tool) for tool in ("rsync", "git", "ssh-keygen", "sha256sum")),
        reason="стенду выкладки нужны rsync, git, ssh-keygen и sha256sum",
    ),
    pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="под root не проверить, что юнит недоступен для записи",
    ),
]


@pytest.fixture
def pending_migration(tmp_path: Path) -> Sandbox:
    """Прод на 0001, релиз приносит 0002 — как выкладка PR с новой колонкой."""
    sandbox = Sandbox(tmp_path, release_revisions=[BASE, HEAD], live_revisions=[BASE])
    sandbox.migrate_production_to(BASE)
    return sandbox


def _restarts(sandbox: Sandbox) -> list[dict]:
    """Рестарты сервисов; `sudo -l` из preflight только спрашивает о праве."""
    return [
        event
        for event in sandbox.events("sudo")
        if "restart" in event["argv"] and "-l" not in event["argv"]
    ]


def _assert_production_untouched(sandbox: Sandbox, before: dict[str, str], revision: str) -> None:
    assert sandbox.live_digest() == before, "живой каталог изменился при отказе выкладки"
    assert sandbox.db_revision() == revision
    assert sandbox.events("rsync", live=True) == []
    assert _restarts(sandbox) == []
    assert sandbox.staging_dirs() == [], "каталог .deploy-stage.* остался на сервере"


def test_migration_runs_while_the_previous_release_is_still_live(
    pending_migration: Sandbox,
) -> None:
    sandbox = pending_migration

    result = run_workflow(sandbox, apply_migrations=True)

    assert result.failed_step is None, result.describe()
    assert result.skipped == []

    upgrades = sandbox.events("alembic", command="upgrade")
    assert len(upgrades) == 1
    upgrade = upgrades[0]
    # Окно закрыто: миграция идёт, пока в живом каталоге прошлый релиз.
    assert upgrade["live_release"] == "old"
    assert upgrade["db_revision"] == BASE
    # …и идёт она из отдельного каталога, на src и env.py своего релиза.
    assert re.fullmatch(r"/opt/pharmacy-monitor/\.deploy-stage\.\w{6}/runtime", upgrade["cwd"])
    assert upgrade["pythonpath"] == upgrade["cwd"]
    assert upgrade["lock_timeout_ms"] == "5000"
    assert sandbox.probe_rows() == [(1, "from the staged release")]

    backups = sandbox.events("backup")
    assert len(backups) == 1
    assert backups[0]["at"] < upgrade["at"], "бэкап должен быть снят до миграции"
    assert backups[0]["db_revision"] == BASE

    live_copies = sandbox.events("rsync", live=True)
    assert live_copies, "код так и не скопирован в живой каталог"
    # Код ложится только на схему, которой он ждёт.
    assert {event["db_revision"] for event in live_copies} == {HEAD}
    assert min(event["at"] for event in live_copies) > upgrade["at"]
    staged_copies = sandbox.events("rsync", live=False)
    assert max(event["at"] for event in staged_copies) < upgrade["at"]

    assert (sandbox.live / "src" / "RELEASE").read_text().strip() == "new"
    assert (sandbox.live / "migrations" / "versions" / f"{HEAD}.py").exists()
    # env.py несёт lock_timeout: ручной `alembic upgrade` на сервере его тоже получит.
    assert (sandbox.live / "migrations" / "env.py").read_bytes() == (
        ROOT / "migrations" / "env.py"
    ).read_bytes()

    restarts = _restarts(sandbox)
    assert [event["argv"][-1] for event in restarts] == [
        "pharmacy-monitor-api.service",
        "pharmacy-monitor-frontend.service",
    ]
    assert restarts[0]["at"] > max(event["at"] for event in live_copies)
    assert sandbox.staging_dirs() == []


def test_release_without_a_migration_takes_no_backup_and_runs_no_upgrade(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path, release_revisions=[BASE], live_revisions=[BASE])
    sandbox.migrate_production_to(BASE)

    result = run_workflow(sandbox, apply_migrations=False)

    assert result.failed_step is None, result.describe()
    assert result.skipped == [MIGRATE]
    assert sandbox.events("backup") == []
    assert sandbox.events("alembic", command="upgrade") == []
    assert (sandbox.live / "src" / "RELEASE").read_text().strip() == "new"
    assert sandbox.staging_dirs() == []


def test_pending_migration_without_the_flag_is_refused_before_anything_is_uploaded(
    pending_migration: Sandbox,
) -> None:
    sandbox = pending_migration
    before = sandbox.live_digest()

    result = run_workflow(sandbox, apply_migrations=False)

    assert result.failed_step.name == PREFLIGHT_REVISION, result.describe()
    assert "re-run with apply_migrations=true" in result.failed_step.stdout
    assert sandbox.events("rsync") == []
    assert not result.ran(CLEANUP), "чистить нечего: каталог не создавался"
    _assert_production_untouched(sandbox, before, BASE)


def test_gate_alone_keeps_new_code_off_the_old_schema(pending_migration: Sandbox) -> None:
    """Проверка на сервере держит и без ранней проверки по чекауту."""
    sandbox = pending_migration
    before = sandbox.live_digest()

    result = run_workflow(sandbox, apply_migrations=False, without=(PREFLIGHT_REVISION,))

    assert result.failed_step.name == GATE, result.describe()
    assert f"production DB is at {BASE}, this release needs {HEAD}" in result.failed_step.stdout
    assert RSYNC in result.skipped
    assert RESTART in result.skipped
    assert result.step(CLEANUP).returncode == 0
    _assert_production_untouched(sandbox, before, BASE)


def test_failed_backup_stops_before_the_migration_and_the_code(pending_migration: Sandbox) -> None:
    sandbox = pending_migration
    before = sandbox.live_digest()
    (sandbox.root / "backup-must-fail").touch()

    result = run_workflow(sandbox, apply_migrations=True)

    assert result.failed_step.name == MIGRATE, result.describe()
    assert sandbox.events("alembic", command="upgrade") == []
    assert GATE in result.skipped
    assert result.step(CLEANUP).returncode == 0
    _assert_production_untouched(sandbox, before, BASE)


def test_lock_timeout_is_retried_and_the_deploy_goes_on(pending_migration: Sandbox) -> None:
    sandbox = pending_migration
    (sandbox.root / "upgrade-failures.json").write_text(json.dumps(["lock", "lock"]))

    result = run_workflow(sandbox, apply_migrations=True)

    assert result.failed_step is None, result.describe()
    upgrades = sandbox.events("alembic", command="upgrade")
    assert len(upgrades) == 3
    # Все попытки — пока живой каталог не тронут.
    assert {event["live_release"] for event in upgrades} == {"old"}
    assert [event["seconds"] for event in sandbox.events("sleep")][:2] == ["10", "10"]
    assert len(sandbox.events("backup")) == 1, "бэкап не повторяется с каждой попыткой"
    output = result.step(MIGRATE).stdout
    assert "::warning::migration lock timeout, attempt 1 of 3" in output
    assert "::warning::migration lock timeout, attempt 2 of 3" in output
    assert sandbox.db_revision() == HEAD
    assert (sandbox.live / "src" / "RELEASE").read_text().strip() == "new"


def test_lock_timeout_on_every_attempt_refuses_and_says_why(pending_migration: Sandbox) -> None:
    sandbox = pending_migration
    before = sandbox.live_digest()
    (sandbox.root / "upgrade-failures.json").write_text(json.dumps(["lock"] * 3))

    result = run_workflow(sandbox, apply_migrations=True)

    failed = result.failed_step
    assert failed.name == MIGRATE, result.describe()
    assert failed.returncode == 75
    assert "::error::the migration could not take its table lock: 3 attempts of 5000 ms" in (
        failed.stdout
    )
    assert "Nothing was changed" in failed.stdout
    assert len(sandbox.events("alembic", command="upgrade")) == 3
    assert result.step(CLEANUP).returncode == 0
    _assert_production_untouched(sandbox, before, BASE)


def test_a_broken_migration_is_not_retried(pending_migration: Sandbox) -> None:
    sandbox = pending_migration
    before = sandbox.live_digest()
    (sandbox.root / "upgrade-failures.json").write_text(json.dumps(["error"]))

    result = run_workflow(sandbox, apply_migrations=True)

    failed = result.failed_step
    assert failed.name == MIGRATE, result.describe()
    assert failed.returncode == 1
    assert "::error::alembic upgrade failed" in failed.stdout
    assert len(sandbox.events("alembic", command="upgrade")) == 1
    assert sandbox.events("sleep") == []
    _assert_production_untouched(sandbox, before, BASE)


@pytest.mark.parametrize(
    ("without", "refusing_step"),
    [((), PREFLIGHT_REVISION), ((PREFLIGHT_REVISION,), VERIFY_GRAPH)],
    ids=["checkout-side check", "server-side check alone"],
)
def test_two_heads_stop_the_deploy_before_the_backup(
    tmp_path: Path, without: tuple[str, ...], refusing_step: str
) -> None:
    sandbox = Sandbox(
        tmp_path, release_revisions=[BASE, HEAD, "0002b_second_head"], live_revisions=[BASE]
    )
    sandbox.migrate_production_to(BASE)
    before = sandbox.live_digest()

    result = run_workflow(sandbox, apply_migrations=True, without=without)

    assert result.failed_step.name == refusing_step, result.describe()
    assert sandbox.events("backup") == []
    assert sandbox.events("alembic", command="upgrade") == []
    _assert_production_untouched(sandbox, before, BASE)


def test_production_revision_unknown_to_the_release_is_refused(tmp_path: Path) -> None:
    """Выкладка коммита старше прода: `rsync --delete` стёр бы ревизию базы."""
    sandbox = Sandbox(
        tmp_path, release_revisions=[BASE, HEAD], live_revisions=[BASE, "0009_only_on_prod"]
    )
    sandbox.migrate_production_to("0009_only_on_prod")
    before = sandbox.live_digest()

    result = run_workflow(sandbox, apply_migrations=True)

    assert result.failed_step.name == PREFLIGHT_REVISION, result.describe()
    assert "is absent from checkout" in result.failed_step.stdout
    assert sandbox.events("rsync") == []
    _assert_production_untouched(sandbox, before, "0009_only_on_prod")


def test_release_altered_after_upload_is_not_executed(pending_migration: Sandbox) -> None:
    """Миграция исполняется только на том, что сошлось с контрольными суммами коммита."""
    sandbox = pending_migration
    before = sandbox.live_digest()

    def tamper() -> None:
        (staged,) = sandbox.live.glob(".deploy-stage.*")
        (staged / "runtime" / "migrations" / "versions" / f"{HEAD}.py").write_text(
            MIGRATIONS[HEAD].replace("note=VALUE", "note='tampered'"), encoding="utf-8"
        )

    result = run_workflow(sandbox, apply_migrations=True, before={MIGRATE: tamper})

    assert result.failed_step.name == MIGRATE, result.describe()
    assert sandbox.events("backup") == []
    assert sandbox.events("alembic", command="upgrade") == []
    _assert_production_untouched(sandbox, before, BASE)


def test_only_stale_staging_directories_of_the_deploy_itself_are_pruned(
    pending_migration: Sandbox,
) -> None:
    sandbox = pending_migration
    two_days_ago = time.time() - 2 * 24 * 3600
    stale = sandbox.live / ".deploy-stage.STALE1"
    recent = sandbox.live / ".deploy-stage.RECENT"
    foreign = sandbox.live / ".codex-decodo-preflight.3OVXSQ"
    for directory in (stale, recent, foreign):
        directory.mkdir()
        (directory / "file").write_text("x", encoding="utf-8")
    for directory in (stale, foreign):
        os.utime(directory, (two_days_ago, two_days_ago))

    result = run_workflow(sandbox, apply_migrations=True)

    assert result.failed_step is None, result.describe()
    assert not stale.exists()
    assert recent.exists(), "свежий каталог может принадлежать идущей выкладке"
    assert foreign.exists(), "чужие каталоги на сервере выкладка не трогает"
    assert sandbox.staging_dirs() == [".deploy-stage.RECENT"]


def test_failed_health_check_still_removes_the_staging_directory(
    pending_migration: Sandbox,
) -> None:
    sandbox = pending_migration
    _write_executable(sandbox.bin / "curl", "import sys\n\nsys.exit(7)\n")

    result = run_workflow(sandbox, apply_migrations=True)

    assert result.failed_step.name == "Health check", result.describe()
    assert result.step(CLEANUP).returncode == 0
    assert sandbox.staging_dirs() == []
