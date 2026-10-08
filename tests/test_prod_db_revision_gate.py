"""Код своей копии workflow исполняет против боевой базы только на её же ревизии схемы.

Зачем тест: 2026-10-07 пять ручных workflow меняли боевую базу кодом своего
чекаута, не сверяя ревизию схемы со своими миграциями. Любая команда CLI зовёт
`storage.init_db()`, а тот — `create_all`: коммит с таблицей, которой в базе
ещё нет, создал бы её мимо Alembic, а коммит с новой колонкой упал бы на первом
запросе. Один из пяти сам исполнял `alembic upgrade head` и следом сверял
ревизию с прошитой `0021_…` — на голове 0023 он упал бы уже после миграции.

Четыре удалены (маршруты, которыми pharmonline с августа не собирается). У
пятого — сверки личностей через Decodo — миграции больше нет: схему двигает
только `deploy.yml`, а сверка отказывает, пока база не стоит на голове миграций
её коммита. Ту же проверку с августа делает еженедельный сбор pharmonline.

Покрываем:
- список workflow, которые исполняют код чекаута против боевой базы, совпадает
  со списком `GATED`: новый такой workflow роняет тест, пока его шаги не
  исполняются в стенде ниже
- в workflow из `GATED` команда, способная менять базу, стоит только в шагах,
  которые стенд исполняет: новый шаг с такой командой роняет тест
- распознаватель видит команды из `WRITES` (ими писали удалённые workflow) и не
  принимает за запись то, что в `READS`
- стенд берёт шаги из самих файлов и исполняет их: `ssh` выполняет удалённую
  команду локально, переписав `/opt/pharmacy-monitor` и `/etc/pharmacy-monitor`
  на каталоги песочницы, `rsync` копирует туда же настоящим rsync, Alembic
  настоящий, база — SQLite со схемой из моделей
- база отстаёт от коммита, ушла вперёд или не имеет ревизии — шаг отказывает,
  не дойдя ни до бэкапа, ни до `--apply`, ни до `run`; файл базы не меняется, и
  таблицы, которой в ней не было, не появляется
- база на голове коммита — шаг доходит до первой записи, и `alembic upgrade`
  по дороге не зовётся
- у сверки: коммит с двумя головами миграций — отказ; шаг, который идёт после
  любого исхода, объясняет, что осталось на сервере

Чего тест не видит:
- запись другими командами: `psql`, python-вставка с `session.commit()`, скрипт
  из `scripts/` без `--apply` и `--commit`, CLI через переменную или
  shell-функцию (`$PM run`, `run_cli run`)
- дорогу к базе, записанную не адресом сервера и не секретом `PG_PASS`: имя
  хоста, адрес из другого секрета
- workflow без `actions/checkout`: он исполняет код живого каталога
  (`production-aptekonline-scrape.yml`, `recover-interrupted-empty-run.yml`).
  Что туда попадает только через сверку `deploy.yml` — забота не этого теста:
  2026-10-07 три workflow ещё копировали туда файлы мимо неё
- workflow, исправленный в ветке и запущенный с неё (`--ref`): CI такую ветку
  проверит, только если из неё открыт PR
- всё после первой записи: сценарий «база на голове» стенд заканчивает на
  публикации каталога — дальше сеть, полчаса ожидания и JSON-колонки, которые
  SQLite отдаёт строкой. Хвост проверен только на синтаксис (`bash -n`)
- выкладку с миграцией, начатую, пока шаг уже работает: сверка одна, до первой
  записи, а группы `concurrency` у выкладки и у этих workflow разные
- настоящие ssh, права на сервере, PostgreSQL и `pg_dump`
- модель без миграции: сверка сравнивает ревизии, а не таблицы, и `create_all`
  такую таблицу создаст на любой ревизии

Сверку стенд узнаёт по поведению, а не по тексту: как она записана, ему
неважно, важно, что при чужой ревизии до записи дело не доходит. Стенд — тот же
приём, что в `tests/test_deploy_workflow.py` (#35); когда оба в `main`, общую
часть стоит вынести в один модуль.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from src.main import cli
from src.storage import Base

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
PROD_IP = "13.140.186.143"

RECOVER = "recover-pharmonline-decodo-public-api.yml"
AUTONOMOUS = "autonomous-pharmonline-decodo-public-api.yml"

# Шаги, в которых код чекаута доходит до боевой базы, в порядке исполнения.
GATED = {
    RECOVER: ["Stage source, back up production, reconcile, and recover catalog"],
    AUTONOMOUS: [
        "Stage checksum-verified runtime source",
        "Run guarded Decodo refresh with bounded fresh-session retries",
    ],
}
# Схему двигает только он. Свою сверку он делает сам, до копирования кода.
SCHEMA_OWNER = {"deploy.yml"}

# После этих команд код чекаута может изменить базу. Любая команда CLI зовёт
# `storage.init_db()` → `create_all`, поэтому важна не подкоманда, а сам вызов;
# список подкоманд берётся из самого CLI, чтобы «pharmacy-monitor is healthy» в
# сообщении записью не считалось.
CLI_COMMANDS = "|".join(sorted(map(re.escape, cli.commands), key=len, reverse=True))
WRITER = re.compile(
    # сама программа, а не каталог `/opt/pharmacy-monitor`; дальше подкоманда,
    # переменная или параметр группы (`--log-level INFO init-db`)
    rf"(?<!/opt/)(?<!/etc/)pharmacy-monitor[\"']?[ \t]+[\"']?(?:\$|-|(?:{CLI_COMMANDS})\b)"
    r"|-m[ \t]+src\.main\b"
    # без границы слова слева: #35 зовёт миграцию через функцию `staged_alembic`
    r"|alembic\b[^\n]*?[ \t](?:upgrade|downgrade|stamp)\b"
    r"|\binit_db\(|\bcreate_all\("
    # флаги, которыми скрипты из `scripts/` включают запись
    r"|\.py\b[^\n]*?\s--(?:apply|commit|i-accept-full-rebuild)\b"
)

WRITES = [
    # так писали удалённые workflow
    "pharmacy-monitor run --site pharmonline --mode category --no-alerts",
    "pharmacy-monitor run $SITE_ARGS --mode category --no-alerts",
    "pharmacy-monitor run --site pharmonline --mode public_api --dry-run --no-alerts",
    "/opt/pharmacy-monitor/.venv/bin/pharmacy-monitor run \\\n  --site pharmonline \\\n  --dry-run",
    '/opt/pharmacy-monitor/.venv/bin/python -m alembic -c "$runtime_dir/alembic.ini" upgrade head',
    # так пишут оставшиеся
    "/opt/pharmacy-monitor/.venv/bin/python -m src.main run \\\n  --site pharmonline",
    '"$runtime_dir/scripts/reconcile_pharmonline_public_api_identities.py" --apply',
    # так ту же ошибку проще всего повторить
    ".venv/bin/pharmacy-monitor rematch",
    "uv run pharmacy-monitor init-db",
    'python -m alembic \\\n  -c "$runtime_dir/alembic.ini" upgrade head',
    'DATABASE_URL="$db_url" .venv/bin/alembic upgrade head',
    "alembic stamp head",
    "alembic downgrade -1",
    "storage.init_db()",
    "Base.metadata.create_all(engine)",
    "python scripts/cleanup_false_matches.py data/false_matches.csv --apply",
    "python scripts/recall_candidates.py --ultra --fuzz 90 --commit",
    "python scripts/full_rematch.py --i-accept-full-rebuild",
    '"$runtime_dir/.venv/bin/pharmacy-monitor" run --site aloe',
    'pharmacy-monitor "$command" --site aloe',
    "/opt/pharmacy-monitor/.venv/bin/pharmacy-monitor --log-level INFO init-db",
    # закомментированная строка с `\\` на конце следующую не прячет: bash её исполнит
    "# PHARMONLINE_USE_DDP=0 \\\n.venv/bin/pharmacy-monitor rematch",
    '/opt/pharmacy-monitor/.venv/bin/python -m src.main "$command"',
    # так миграцию зовёт deploy.yml в #35
    'if staged_alembic upgrade head 2>&1 | tee "$log"; then',
]

READS = [
    '/opt/pharmacy-monitor/.venv/bin/python -m alembic -c "$runtime_dir/alembic.ini" current',
    "python -m alembic \\\n  -c \"$runtime_dir/alembic.ini\" heads | awk 'NF {print $1}'",
    ".venv/bin/alembic heads",
    'alembic.ini "pm@13.140.186.143:$work_dir/runtime/alembic.ini"',
    "git ls-files -z -- src migrations templates alembic.ini |",
    "cd /opt/pharmacy-monitor",
    "done < /etc/pharmacy-monitor/env",
    "df -Pk /opt/pharmacy-monitor | awk 'NR==2 {print $4}'",
    "systemctl is-active --quiet pharmacy-monitor-scrape@pharmonline.service",
    "unit=pharmacy-monitor-scrape@aptekonline.service",
    "! pgrep -f 'pharmacy-monitor (run|scrape|intraday-tick|rematch)( |$)' >/dev/null 2>&1",
    "Session = storage.make_session()",
    '"$runtime_dir/scripts/reconcile_pharmonline_public_api_identities.py"',
    "# pharmacy-monitor run --site pharmonline",
    "  # alembic upgrade head is deploy.yml's job",
    "rsync -az src/ pm@13.140.186.143:/opt/pharmacy-monitor/src/",
    "python -m pip install --upgrade pip",
    "name: Pharmacy Monitor CI",
    "- name: Restart pharmacy-monitor api",
    'echo "pharmacy-monitor is healthy"',
    "find /opt/pharmacy-monitor -maxdepth 1 -name '.codex-*'",
    "ls /opt/pharmacy-monitor $work_dir",
    'grep -- "--apply" scripts/reconcile_pharmonline_public_api_identities.py',
    'gh run list --workflow ci-pipeline.yml --commit "$GITHUB_SHA" --status success',
    "scripts/preflight_pharmonline_public_api.py \\\n"
    '  "pm@13.140.186.143:$remote_work_dir/runtime/scripts/preflight_pharmonline_public_api.py"',
]


CAPTION = re.compile(r"\s*(?:#|(?:-\s+)?(?:name|description):)")


def _code(workflow_text: str) -> str:
    """Текст workflow без комментариев и подписей, команды склеены из строк с `\\`.

    Сначала убираем комментарии, потом склеиваем: `\\` в конце комментария строку
    не продолжает, и команду под ним bash исполнит.
    """
    code = "\n".join(line for line in workflow_text.splitlines() if not CAPTION.match(line))
    return re.sub(r"\\\n[ \t]*", " ", code)


def _writers(workflow_text: str) -> list[str]:
    return [line.strip() for line in _code(workflow_text).splitlines() if WRITER.search(line)]


def _runs_checkout_code_on_prod_db(workflow_text: str) -> bool:
    """Код из чекаута (а не из живого каталога) + дорога к боевой базе + запись."""
    reaches_prod = PROD_IP in workflow_text or "secrets.PG_PASS" in workflow_text
    return "actions/checkout" in workflow_text and reaches_prod and bool(_writers(workflow_text))


@pytest.mark.parametrize("command", WRITES)
def test_detector_sees_commands_that_can_change_the_db(command: str):
    assert _writers(command), command


@pytest.mark.parametrize("command", READS)
def test_detector_ignores_what_does_not_change_the_db(command: str):
    assert not _writers(command), command


def _workflow(*, checkout: bool, target: str, command: str) -> str:
    return "\n".join(
        [
            "jobs:",
            "  job:",
            "    steps:",
            *(["      - uses: actions/checkout@v4"] if checkout else []),
            "      - run: |",
            f"          {target}",
            f"          {command}",
        ]
    )


@pytest.mark.parametrize(
    ("workflow_text", "expected"),
    [
        # с раннера через туннель к базе — так ходили scrape.yml и два recover
        (
            _workflow(
                checkout=True,
                target="DATABASE_URL=postgresql+psycopg://pm:${{ secrets.PG_PASS }}@localhost:5433/x",
                command="pharmacy-monitor run --site pharmonline",
            ),
            True,
        ),
        # из чернового каталога на сервере
        (
            _workflow(
                checkout=True,
                target=f"ssh pm@{PROD_IP} 'bash -s' <<'REMOTE'",
                command='PYTHONPATH="$runtime_dir" python -m src.main run --site pharmonline',
            ),
            True,
        ),
        # код живого каталога: его кладёт deploy.yml со своей сверкой
        (
            _workflow(
                checkout=False,
                target=f"ssh pm@{PROD_IP} 'bash -s' <<'REMOTE'",
                command=".venv/bin/pharmacy-monitor run --site aptekonline --force",
            ),
            False,
        ),
        # база CI, не боевая
        (
            _workflow(
                checkout=True,
                target="DATABASE_URL=postgresql+psycopg://pm:pm@localhost:5432/test",
                command="uv run alembic upgrade head",
            ),
            False,
        ),
        # читает боевую базу кодом чекаута, но ничего не запускает
        (
            _workflow(
                checkout=True,
                target=f"ssh pm@{PROD_IP} 'bash -s' <<'REMOTE'",
                command='python "$runtime_dir/scripts/preflight_pharmonline_public_api.py"',
            ),
            False,
        ),
    ],
)
def test_detector_tells_checkout_code_on_prod_db_from_the_rest(workflow_text: str, expected: bool):
    assert _runs_checkout_code_on_prod_db(workflow_text) is expected


def test_every_workflow_running_checkout_code_on_prod_db_is_executed_in_the_sandbox():
    found = {
        path.name
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        if _runs_checkout_code_on_prod_db(path.read_text(encoding="utf-8"))
    }
    assert SCHEMA_OWNER <= found, "распознаватель перестал видеть `alembic upgrade` в deploy.yml"
    found -= SCHEMA_OWNER

    ungated = sorted(found - set(GATED))
    assert not ungated, (
        f"{ungated}: исполняет код чекаута против боевой базы. Сначала сверка — ревизия базы "
        "равна голове миграций своей копии, до первой записи, — потом шаги в GATED, чтобы стенд "
        "показал отказ. Схему двигает только deploy.yml."
    )
    stale = sorted(set(GATED) - found)
    assert not stale, f"{stale}: в GATED, но код чекаута против боевой базы больше не исполняет"


@pytest.mark.parametrize("workflow", sorted(GATED))
def test_gated_workflow_reaches_the_db_only_in_steps_the_sandbox_executes(workflow: str):
    """Список выше — по файлам, стенд — по шагам: новый шаг не должен пройти между ними."""
    outside = [
        step.get("name", "<без имени>")
        for step in _steps(workflow)
        if _writers(step.get("run", "")) and step.get("name") not in GATED[workflow]
    ]
    assert not outside, (
        f"{workflow}: шаги {outside} зовут команду, способную менять базу, а стенд их не "
        "исполняет. Внести в GATED по порядку исполнения — тогда стенд покажет, что до сверки "
        "они базу не трогают."
    )


# --- стенд -------------------------------------------------------------------

SBX_HELPER = r"""
import json
import os
import pathlib
import re

SBX = pathlib.Path(os.environ["SBX_ROOT"])
SERVER = SBX / "server"


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


def event(kind):
    with open(SBX / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(kind) + "\n")
"""

STUBS = {
    "ssh": r"""
import os
import subprocess
import sys

import _sbx

args = sys.argv[1:]
index = 0
while index < len(args) and args[index].startswith("-"):
    index += 2 if args[index] in {"-i", "-o"} else 1
destination, remote = args[index], " ".join(args[index + 1 :])
if destination != "pm@13.140.186.143":
    sys.exit(f"stub ssh: unexpected destination {destination}")
script = sys.stdin.read()
# ssh не переносит окружение: на сервер попадает только то, что передано
# аргументами или выставлено в самом удалённом скрипте.
server_env = {
    key: value
    for key, value in os.environ.items()
    if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"} or key.startswith("SBX_")
}
result = subprocess.run(
    ["bash", "-c", _sbx.to_sandbox(remote)],
    input=_sbx.to_sandbox(script),
    text=True,
    capture_output=True,
    cwd=_sbx.SERVER / "opt" / "pharmacy-monitor",  # домашний каталог pm на сервере
    env=server_env,
)
sys.stdout.write(_sbx.from_sandbox(result.stdout))
sys.stderr.write(_sbx.from_sandbox(result.stderr))
sys.exit(result.returncode)
""",
    "rsync": r"""
import os
import shutil
import subprocess
import sys

import _sbx

args, translated, remote = sys.argv[1:], [], False
index = 0
while index < len(args):
    arg = args[index]
    if arg == "-e":
        index += 2
        continue
    if arg.startswith("pm@13.140.186.143:"):
        remote = True
        arg = _sbx.to_sandbox(arg.split(":", 1)[1])
    translated.append(arg)
    index += 1
if not remote:
    sys.exit("stub rsync: no remote destination")
real = os.environ["SBX_REAL_RSYNC"]
second_head = os.environ.get("SBX_SECOND_HEAD")
if second_head and translated[-1].endswith("/runtime/migrations/"):
    # коммит с двумя головами: вторая миграция приезжает вместе с остальными
    subprocess.run([real, *translated], check=True)
    shutil.copy(second_head, translated[-1] + "versions/")
    sys.exit(0)
os.execv(real, [real, *translated])
""",
    "sleep": r"""
import _sbx

_sbx.event("sleep")
""",
}

# `.venv/bin/python` сервера. Чтение (`alembic current`, `heads`, python-вставки)
# отдаёт настоящему интерпретатору; всё, что идёт после сверки, только
# записывает в журнал.
VENV_PYTHON = r"""
import os
import pathlib
import sys

sys.path.insert(0, os.environ["SBX_BIN"])
import _sbx

args = sys.argv[1:]
kind = None
if args[:2] == ["-m", "alembic"]:
    rest = args[2:]
    if rest[:1] == ["-c"]:
        rest = rest[2:]
    kind = f"alembic {rest[0]}"
elif args[:2] == ["-m", "src.main"]:
    kind = f"cli {args[2]}"
elif args and args[0].endswith(".py"):
    kind = "script" + (" --apply" if "--apply" in args[1:] else "")
elif args == ["-"] and "BACKUP_PATH" in os.environ:
    kind = "backup"

if kind is not None:
    _sbx.event(kind)
if kind == "backup":
    pathlib.Path(os.environ["BACKUP_PATH"]).write_bytes(b"sandbox dump\n")
    sys.exit(0)
if kind in {"script", "script --apply", "alembic upgrade", "alembic downgrade", "alembic stamp"}:
    # stdin здесь — остаток удалённого скрипта (`bash -s`): читать его нельзя.
    sys.exit(0)
if kind is not None and kind.startswith("cli "):
    print("sandbox: the scenario ends at the first catalog publication", file=sys.stderr)
    sys.exit(1)
real = os.environ["SBX_REAL_PYTHON"]
os.execv(real, [real, *args])
"""

# Всё, до чего шаг не должен дойти, пока ревизия базы не равна голове коммита.
BEHIND_THE_GATE = {
    "backup",
    "script",
    "script --apply",
    "cli run",
    "alembic upgrade",
    "alembic downgrade",
    "alembic stamp",
}
EXPRESSION = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")
# Ключи, которые стенд умеет исполнять. Незнакомый (`defaults`, `env` у джобы,
# `shell`, `continue-on-error`…) меняет то, как шаг работает на раннере, поэтому
# роняет стенд: сначала научить, потом верить.
KNOWN_WORKFLOW_KEYS = {
    "name",
    True,
    "permissions",
    "concurrency",
    "jobs",
}  # `on:` YAML читает как True
KNOWN_JOB_KEYS = {"name", "runs-on", "timeout-minutes", "steps"}
KNOWN_STEP_KEYS = {"name", "id", "env", "run"}
RETAIN = "Retain recovery evidence and immutable backup"
RETAIN_CONDITION = "${{ always() && env.REMOTE_WORK_DIR != '' }}"
SECOND_HEAD = "9998_second_head"


def _migration_head_and_parent() -> tuple[str, str]:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    (head,) = script.get_heads()
    parent = script.get_revision(head).down_revision
    assert isinstance(parent, str)
    return head, parent


HEAD, PARENT = _migration_head_and_parent()
DB_STATES = {
    "at head": HEAD,
    "behind the commit": PARENT,
    "ahead of the commit": "9999_not_in_this_commit",
    "without a revision": None,
}
# Что сверка личностей говорит оператору: где база, где коммит и что делать.
RECOVER_REFUSAL = {
    "behind the commit": [
        f"refusing reconciliation: production DB is at migration {PARENT}",
        f"this commit's migrations end at {HEAD}",
        "deploy this commit with deploy.yml (apply_migrations=true) first",
    ],
    "without a revision": [
        "refusing reconciliation: production DB is at migration none",
        f"this commit's migrations end at {HEAD}",
    ],
    "ahead of the commit": [
        "refusing reconciliation: the production DB revision cannot be read",
        "dispatch from the deployed commit",
    ],
}


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + body.lstrip("\n"), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _steps(workflow: str) -> list[dict]:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    (job,) = data["jobs"].values()
    unknown = (set(data) - KNOWN_WORKFLOW_KEYS) | (set(job) - KNOWN_JOB_KEYS)
    assert not unknown, f"{workflow}: стенд не знает ключи {sorted(map(str, unknown))} — научить"
    return job["steps"]


class Sandbox:
    def __init__(self, root: Path, *, db_state: str, second_head: bool = False):
        self.root = root
        self.db_state = db_state
        db_revisions = [DB_STATES[db_state]] if DB_STATES[db_state] is not None else []
        self.second_head: Path | None = None
        if second_head:
            # Вторая голова в коммите, и база стоит на обеих: `alembic current`
            # и `alembic heads` печатают одно и то же.
            self.second_head = root / f"{SECOND_HEAD}.py"
            self.second_head.write_text(
                f'revision: str = "{SECOND_HEAD}"\n'
                f'down_revision: str | None = "{PARENT}"\n'
                "branch_labels = None\ndepends_on = None\n\n\n"
                "def upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    pass\n",
                encoding="utf-8",
            )
            db_revisions.append(SECOND_HEAD)
        self.bin = root / "bin"
        self.server = root / "server"
        self.database = self.server / "data" / "db.sqlite"
        self.events_file = root / "events.jsonl"
        self.github_env = root / "github-env"
        self.github_output = root / "github-output"
        # Что шагу оставили предыдущие: выражения `${{ … }}` и переменные из
        # $GITHUB_ENV (у сверки их пишет шаг, проверяющий план).
        self.context = {"inputs.plan_run_id": "4242"}
        self.github_env.write_text(
            f"PLAN_CATALOG_FINGERPRINT_SHA256={'a' * 64}\n"
            f"PLAN_CANDIDATE_MANIFEST_SHA256={'b' * 64}\n"
            "PLAN_PRODUCT_COUNT=9400\n"
            # Транспорт сверка берёт из одобренного плана; что он туда попадает
            # и что с ним делает шаг — tests/test_pharmonline_reconciliation_workflows.py.
            "PLAN_TRANSPORT=decodo\n",
            encoding="utf-8",
        )
        self.github_output.write_text("", encoding="utf-8")
        self.events_file.write_text("", encoding="utf-8")
        (root / "home").mkdir()
        (root / "tmp").mkdir()

        self.bin.mkdir()
        (self.bin / "_sbx.py").write_text(SBX_HELPER, encoding="utf-8")
        for name, body in STUBS.items():
            _write_executable(self.bin / name, body)
        live = self.server / "opt" / "pharmacy-monitor"
        _write_executable(live / ".venv" / "bin" / "python", VENV_PYTHON)

        (self.server / "etc" / "pharmacy-monitor").mkdir(parents=True)
        (self.server / "etc" / "pharmacy-monitor" / "env").write_text(
            f"# comment\n\nJWT_SECRET=x\nDATABASE_URL=sqlite:///{self.database}\n",
            encoding="utf-8",
        )
        self.database.parent.mkdir(parents=True)
        engine = create_engine(f"sqlite:///{self.database}")
        tables = list(Base.metadata.sorted_tables)
        # База не на голове коммита — значит, в ней нет чего-то, что есть в его
        # моделях. Убираем таблицу, на которую никто не ссылается: если код
        # чекаута доберётся до `create_all`, она появится.
        self.missing_table = None if db_state == "at head" else tables.pop().name
        Base.metadata.create_all(engine, tables=tables)
        if db_revisions:
            with engine.begin() as connection:
                connection.execute(
                    text("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)")
                )
                for revision in db_revisions:
                    connection.execute(
                        text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                        {"revision": revision},
                    )
        engine.dispose()

    @property
    def events(self) -> list[str]:
        return [json.loads(line) for line in self.events_file.read_text().splitlines()]

    def work_dir(self) -> Path:
        """Черновой каталог шага на «сервере» — куда его записал сам шаг."""
        for line in self.github_env.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key == "REMOTE_WORK_DIR":
                return self.server / value.lstrip("/")
        raise AssertionError("шаг не записал REMOTE_WORK_DIR")

    def tables(self) -> set[str]:
        engine = create_engine(f"sqlite:///{self.database}")
        try:
            return set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

    def db_fingerprint(self) -> str:
        return hashlib.sha256(self.database.read_bytes()).hexdigest()

    def _expand(self, value: object) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            assert key in self.context, f"стенд не знает выражение ${{{{ {key} }}}} — научить"
            return self.context[key]

        return EXPRESSION.sub(replace, str(value))

    def run_step(
        self, workflow: str, name: str, *, condition: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Исполняет шаг. `condition` — его `if:`, если тест сам решил, что оно истинно."""
        (step,) = [candidate for candidate in _steps(workflow) if candidate.get("name") == name]
        assert step.get("if") == condition, f"у шага «{name}» другое условие: {step.get('if')}"
        unknown = set(step) - KNOWN_STEP_KEYS - {"if"}
        assert not unknown, f"стенд не знает ключи шага {sorted(unknown)} — научить, потом верить"
        env = {
            **{key: os.environ[key] for key in ("LANG", "LC_ALL") if key in os.environ},
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "RUNNER_TEMP": str(self.root / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SBX_ROOT": str(self.root),
            "SBX_BIN": str(self.bin),
            "SBX_REAL_PYTHON": sys.executable,
            "SBX_REAL_RSYNC": shutil.which("rsync") or "",
            "GITHUB_ENV": str(self.github_env),
            "GITHUB_OUTPUT": str(self.github_output),
        }
        if self.second_head is not None:
            env["SBX_SECOND_HEAD"] = str(self.second_head)
        for line in self.github_env.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            env[key] = value
        for key, value in (step.get("env") or {}).items():
            env[key] = self._expand(value)

        script = self.root / "step.sh"
        script.write_text(self._expand(step["run"]), encoding="utf-8")
        result = subprocess.run(
            # так шаг запускает раннер GitHub
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if "id" in step:
            for line in self.github_output.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                self.context[f"steps.{step['id']}.outputs.{key}"] = value
        return result

    def run_gated(self, workflow: str) -> subprocess.CompletedProcess[str]:
        """Шаги по порядку; возвращает последний — тот, где код доходит до базы."""
        *preparation, last = GATED[workflow]
        for name in preparation:
            prepared = self.run_step(workflow, name)
            assert prepared.returncode == 0, f"{name}\n{prepared.stdout}{prepared.stderr}"
        return self.run_step(workflow, last)


@pytest.fixture
def sandbox(tmp_path: Path, request: pytest.FixtureRequest) -> Sandbox:
    assert shutil.which("rsync"), "стенду нужен rsync — без него шаги не исполнить"
    return Sandbox(tmp_path, db_state=request.param)


def _log(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit {result.returncode}\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}"


@pytest.mark.parametrize("workflow", sorted(GATED))
@pytest.mark.parametrize(
    "sandbox", [state for state in DB_STATES if state != "at head"], indirect=True
)
def test_refuses_before_any_write_when_the_db_is_not_at_the_commit_head(
    sandbox: Sandbox, workflow: str
):
    before = sandbox.db_fingerprint()

    result = sandbox.run_gated(workflow)

    assert result.returncode != 0, _log(result)
    events = sandbox.events
    # отказала именно сверка, а не что-то до неё
    assert "alembic current" in events, _log(result)
    assert not BEHIND_THE_GATE & set(events), f"{events}\n{_log(result)}"
    assert sandbox.db_fingerprint() == before
    assert sandbox.missing_table not in sandbox.tables()
    if workflow == RECOVER:
        for phrase in RECOVER_REFUSAL[sandbox.db_state]:
            assert phrase in result.stderr, _log(result)
        # Шаг `always()` после отказа: бэкапа нет, и это не ошибка самого шага.
        retained = sandbox.run_step(RECOVER, RETAIN, condition=RETAIN_CONDITION)
        assert retained.returncode == 0, _log(retained)
        assert "recovery stopped before the production backup" in retained.stderr
        # А отметка «сверено» без бэкапа — уже ошибка, и шаг её не глотает.
        (sandbox.work_dir() / "reconciliation-and-catalog-recovery-verified").touch()
        anomaly = sandbox.run_step(RECOVER, RETAIN, condition=RETAIN_CONDITION)
        assert anomaly.returncode != 0, _log(anomaly)
        assert "marked verified but its production backup is missing" in anomaly.stderr


def test_recovery_refuses_a_commit_with_two_migration_heads(tmp_path: Path):
    sandbox = Sandbox(tmp_path, db_state="at head", second_head=True)
    before = sandbox.db_fingerprint()

    result = sandbox.run_gated(RECOVER)

    assert result.returncode != 0, _log(result)
    assert sandbox.events == ["alembic current", "alembic heads"], _log(result)
    assert "this commit must have exactly one migration head" in result.stderr, _log(result)
    assert sandbox.db_fingerprint() == before


@pytest.mark.parametrize("sandbox", ["at head"], indirect=True)
def test_recovery_at_the_commit_head_backs_up_and_reconciles_without_migrating(sandbox: Sandbox):
    result = sandbox.run_gated(RECOVER)

    events = sandbox.events
    assert f"production DB is at this commit's migration head: {HEAD}" in result.stdout, _log(
        result
    )
    assert "reconciliation audit schema verified" in result.stdout, _log(result)
    order = [kind for kind in events if kind != "sleep"]
    assert order[:4] == ["alembic current", "alembic heads", "backup", "script --apply"], events
    # три попытки публикации; на них стенд сценарий и заканчивает
    assert order[4:] == ["cli run"] * 3, events
    assert result.returncode != 0
    assert "sandbox: the scenario ends at the first catalog publication" in result.stderr
    # Каталог не опубликован, бэкап снят: шаг `always()` говорит, что осталось.
    retained = sandbox.run_step(RECOVER, RETAIN, condition=RETAIN_CONDITION)
    assert retained.returncode == 0, _log(retained)
    assert "staged evidence and backup retained for audit" in retained.stderr


@pytest.mark.parametrize("sandbox", ["at head"], indirect=True)
def test_weekly_refresh_at_the_commit_head_reaches_the_catalog_run(sandbox: Sandbox):
    result = sandbox.run_gated(AUTONOMOUS)

    assert sandbox.events == ["alembic current", "alembic heads", "cli run"], _log(result)
    assert result.returncode != 0
    assert "sandbox: the scenario ends at the first catalog publication" in result.stderr


def _scripts(workflow: str) -> list[tuple[str, str]]:
    """Скрипт каждого шага и отдельно — тело каждого `<<'REMOTE'`: его разбирает сервер."""
    scripts = []
    for index, step in enumerate(_steps(workflow)):
        if "run" not in step:
            continue
        label = step.get("name", f"step {index}")
        body = EXPRESSION.sub("expression", step["run"])
        scripts.append((label, body))
        for remote in re.findall(r"<<'REMOTE'\n(.*?)\n[ \t]*REMOTE\n", body + "\n", re.S):
            scripts.append((f"{label} / REMOTE", remote))
    return scripts


@pytest.mark.parametrize("workflow", sorted(GATED))
def test_every_step_script_parses(workflow: str, tmp_path: Path):
    scripts = _scripts(workflow)
    assert any(label.endswith("/ REMOTE") for label, _ in scripts)
    for label, body in scripts:
        script = tmp_path / "script.sh"
        script.write_text(body, encoding="utf-8")
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{workflow} / {label}\n{result.stderr}"
