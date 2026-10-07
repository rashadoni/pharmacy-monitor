"""Код в живой каталог прода кладёт только `deploy.yml`.

Зачем тест: 2026-10-07 в `.github/workflows/` нашлись три workflow, которые
копировали файлы в `/opt/pharmacy-monitor` мимо `deploy.yml`: две «срочные
выкладки» прошлых инцидентов (25 июля — `api.py` и `analytics.py`; 9 августа —
пять файлов, среди них `storage.py` с моделями базы) и разовая активация
23 августа, менявшая `src/`, `migrations/` и `templates/` целиком. Ни один не
сверял ревизию базы с миграциями выкладываемого коммита. Запусти такой workflow
с коммита, где модели ушли вперёд базы, — и новый код лежит на старой схеме:
каждый запрос к изменённой таблице падает, и у API, и у сборщиков.

Все три удалены. `deploy.yml` перед копированием сверяет ревизию базы с
миграциями коммита и занимает две минуты — отдельная «быстрая» выкладка пары
файлов ничего не выигрывает. Срочную правку выкладывать им же.

Покрываем:
- ни один workflow, кроме `deploy.yml`, не копирует файлы в каталоги кода прода
- распознаватель видит каждую форму, которой это делали удалённые workflow, и
  не путает запись в живой каталог с чтением из него и с копией в черновой

Чего тест не видит: путь, собранный из переменной (`$app/src/`), запись не
командой копирования (python, heredoc, своя shell-функция) и запуск кода
чекаута из чернового каталога против боевой базы. Это сторож от возврата
известной ошибки, а не доказательство, что обходного пути нет.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
ALLOWED_WRITERS = {"deploy.yml"}

# Откуда исполняется прод. `data/` и черновые каталоги (`.deploy-stage.*`,
# `.codex-*`) сюда не входят: сервисы и таймеры код оттуда не берут.
LIVE_CODE = re.compile(
    r"^(?:[\w.-]+@[\w.-]+:)?/opt/pharmacy-monitor/"
    r"(?:src|migrations|templates|frontend|infra|scripts|alembic\.ini|pyproject\.toml)(?:/|$)"
)
COPY_COMMANDS = re.compile(r"(?:^|[\s\"'(])(?:rsync|scp|cp|install|mv|tee)\s")
REDIRECT_INTO_LIVE_CODE = re.compile(
    r">>?\s*[\"']?/opt/pharmacy-monitor/"
    r"(?:src|migrations|templates|frontend|infra|scripts|alembic\.ini|pyproject\.toml)\b"
)
UNPACK_INTO_LIVE_TREE = re.compile(
    r"\btar\s+-?[A-Za-z]*x[A-Za-z]*\b.*\s(?:-C|--directory)[\s=]+[\"']?/opt/pharmacy-monitor"
    r"(?:/(?:src|migrations|templates|frontend|infra|scripts))?/?[\"']?(?:\s|$)"
)


def _commands(script: str) -> list[str]:
    """Команды shell-текста: строки с `\\` склеены, цепочки `&&`, `;`, `|` разрезаны."""
    joined = re.sub(r"\\\n\s*", " ", script)
    return [
        part.strip()
        for line in joined.splitlines()
        for part in re.split(r"&&|\|\||;|\|", line)
        if part.strip()
    ]


def _writes_live_code(script: str) -> list[str]:
    found = []
    for command in _commands(script):
        if REDIRECT_INTO_LIVE_CODE.search(command) or UNPACK_INTO_LIVE_TREE.search(command):
            found.append(command)
            continue
        if not COPY_COMMANDS.search(command):
            continue
        # Куда копируют — последний аргумент; всё до него — что копируют.
        arguments = re.sub(r"\s\d*>.*$", "", command).split()
        destination = arguments[-1].strip("\"'")
        if LIVE_CODE.match(destination):
            found.append(command)
    return found


def test_only_deploy_workflow_writes_live_code():
    workflows = sorted(WORKFLOWS.glob("*.y*ml"))
    assert workflows, "в .github/workflows нет ни одного workflow — путь в тесте устарел?"

    writers = {
        path.name: commands
        for path in workflows
        if (commands := _writes_live_code(path.read_text()))
    }
    extra = {
        name: commands[:3] for name, commands in writers.items() if name not in ALLOWED_WRITERS
    }
    assert not extra, (
        "workflow копирует файлы в живой каталог прода мимо deploy.yml — без сверки "
        "ревизии базы с миграциями коммита новый код ляжет на старую схему. "
        f"Выкладывать через deploy.yml: {extra}"
    )


# Формы, которыми писали в живой каталог удалённые workflow.
REMOTE_RSYNC_OF_ONE_FILE = """
for file in api.py analytics.py
do
  rsync -z --checksum --inplace --no-perms --no-owner --no-group \\
    -e "ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes" \\
    "src/$file" "pm@13.140.186.143:/opt/pharmacy-monitor/src/$file"
done
"""
REMOTE_RSYNC_OF_MODELS = """
rsync -az --delay-updates -e "ssh -i ~/.ssh/id_ed25519" \\
  src/storage.py pm@13.140.186.143:/opt/pharmacy-monitor/src/storage.py
"""
ON_HOST_COPY_FROM_STAGING = """
rsync -a --checksum --delay-updates --exclude='__pycache__/' \\
  "$runtime_dir/src/" /opt/pharmacy-monitor/src/
rsync -a --checksum --delay-updates --exclude='__pycache__/' \\
  "$runtime_dir/migrations/" /opt/pharmacy-monitor/migrations/
cp -p "$runtime_dir/alembic.ini" /opt/pharmacy-monitor/alembic.ini
"""
RESTORE_INSIDE_SSH_STRING = """
ssh pm@13.140.186.143 \\
  "set -euo pipefail
   cp -- '$BACKUP_DIR/api.py' /opt/pharmacy-monitor/src/api.py
   sudo -n /usr/bin/systemctl restart pharmacy-monitor-api.service"
"""
CHAINED_AFTER_CD = "cd /tmp/release && cp main.py /opt/pharmacy-monitor/src/main.py && echo copied"


def test_detector_sees_every_form_the_removed_workflows_used():
    assert len(_writes_live_code(REMOTE_RSYNC_OF_ONE_FILE)) == 1
    assert len(_writes_live_code(REMOTE_RSYNC_OF_MODELS)) == 1
    assert len(_writes_live_code(ON_HOST_COPY_FROM_STAGING)) == 3
    assert len(_writes_live_code(RESTORE_INSIDE_SSH_STRING)) == 1
    assert len(_writes_live_code(CHAINED_AFTER_CD)) == 1


def test_detector_sees_writes_that_are_not_a_copy_command():
    assert _writes_live_code("printf '%s' \"$patch\" > /opt/pharmacy-monitor/src/health.py")
    assert _writes_live_code("cat fix.py | ssh pm@host 'tee /opt/pharmacy-monitor/src/health.py'")
    assert _writes_live_code("tar -xzf release.tgz -C /opt/pharmacy-monitor")
    assert _writes_live_code("tar xzf - --directory=/opt/pharmacy-monitor/src/")


# То, что запись в живой каталог не есть: чтение из него и копии в черновые.
BACKUP_OF_LIVE_FILES = """
cp -- /opt/pharmacy-monitor/src/api.py '$backup_dir/api.py'
rsync -a --delete --exclude='__pycache__/' /opt/pharmacy-monitor/src/ "$backup_dir/src/"
cp -p /opt/pharmacy-monitor/alembic.ini "$backup_dir/alembic.ini"
"""
COPY_INTO_STAGING = """
rsync -az --checksum --delete --no-perms --no-owner --no-group \\
  -e "ssh -i ~/.ssh/id_ed25519" \\
  src/ "pm@13.140.186.143:$work_dir/runtime/src/"
rsync -az -e "ssh -i ~/.ssh/id_ed25519" \\
  src/ pm@13.140.186.143:/opt/pharmacy-monitor/.deploy-stage.abc123/src/
"""
READS_AND_DATA_WRITES = """
sha256sum /opt/pharmacy-monitor/src/main.py
.venv/bin/python -m py_compile /opt/pharmacy-monitor/src/api.py
printf '%s\\n' "$marker" > /opt/pharmacy-monitor/data/pharmonline-public-api-autonomous-v1
tar -czf /tmp/src.tgz -C /opt/pharmacy-monitor src
tar -xzf dump.tgz -C /opt/pharmacy-monitor/data/restore
"""


def test_detector_ignores_reads_backups_and_staging_copies():
    assert _writes_live_code(BACKUP_OF_LIVE_FILES) == []
    assert _writes_live_code(COPY_INTO_STAGING) == []
    assert _writes_live_code(READS_AND_DATA_WRITES) == []
