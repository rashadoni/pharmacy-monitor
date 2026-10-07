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
- распознаватель видит запись самого `deploy.yml` и каждую форму, которой это
  делали удалённые workflow или которой выкладку описывали в документах
- распознаватель не путает запись в живой каталог с чтением из него, с копией
  в черновой каталог и с текстом в `echo`

Чего тест не видит:
- каталог из переменной (`$app/src/`, `cd "$app"`)
- запись не командой оболочки (python, heredoc) и своей shell-функцией
- скрипт из репозитория, который workflow исполняет на сервере
  (`bash -s < infra/…sh`): в сам скрипт тест не заглядывает
- код чекаута, запущенный из чернового каталога против боевой базы, и
  `alembic upgrade` мимо `deploy.yml` — живой каталог при этом не меняется, а
  схема может

Это сторож от возврата известной ошибки, а не доказательство, что обходного пути
нет. Запрет срабатывает и на команду, которая только выглядит записью: если тест
ругается на безобидную строку — научить распознаватель, а не обходить его.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
ALLOWED_WRITERS = {"deploy.yml"}

APP = "/opt/pharmacy-monitor"
# Откуда исполняется прод. `data/` и черновые каталоги (`.deploy-stage.*`,
# `.codex-*`) сюда не входят: сервисы и таймеры код оттуда не берут.
CODE = r"(?:src|migrations|templates|frontend|infra|scripts|alembic\.ini|pyproject\.toml)"
# Хост перед путём: адрес, имя или переменная (`pm@13.140.186.143:`, `$USER@$HOST:`).
HOST = r"[^\s/:]+:"
# Каталог кода или корень целиком: `scp -r src host:/opt/pharmacy-monitor/`.
LIVE = rf"{APP}(?:/?$|/{CODE}(?:/|$))"
ON_HOST = re.compile(LIVE)
# Путь на сервере без каталога — от домашнего, а дом у `pm` и есть живой каталог.
OVER_NETWORK = re.compile(rf"{HOST}(?:{LIVE}|(?:~/|\./)?(?:{CODE}(?:/|$)|$)|[~.]/?$)")

COPIERS = {"rsync", "scp", "cp", "install", "mv", "tee"}
# Слова перед командой, которые её не меняют.
PREFIXES = {
    "sudo",
    "env",
    "nohup",
    "exec",
    "time",
    "nice",
    "timeout",
    "xargs",
    "do",
    "then",
    "else",
}
# За этими параметрами идёт отдельное значение, а не путь назначения.
VALUE_OPTIONS = {
    *("-e", "--rsh", "-i", "-o", "-m", "-g", "-P", "-f", "--exclude", "--include", "--filter"),
    *("--chmod", "--chown", "--files-from", "--exclude-from", "--include-from", "--backup-dir"),
    *("--link-dest", "--temp-dir", "--log-file", "--rsync-path", "--timeout", "--bwlimit"),
}
PROSE = {"echo", "printf"}
STEP = "\0step"
REDIRECT = re.compile(r"(?:^|[\s\"')])\d?>>?\s*([^\s&|<>][^\s|<>]*)")


def _commands(script: str) -> list[str]:
    """Команды shell-текста workflow: строки с `\\` склеены, цепочки разрезаны.

    Комментарии и подписи шагов выброшены — это текст. Начало шага помечено
    `STEP`: каталог, в который перешёл один шаг, следующему не достаётся.
    """
    commands = []
    for line in re.sub(r"\\\n\s*", " ", script).splitlines():
        if re.match(r"\s*-\s+(?:name|uses|id):", line):
            commands.append(STEP)
        if re.match(r"\s*#|\s*(?:-\s+)?(?:name|description):", line):
            continue
        line = re.sub(r"^\s*(?:-\s+)?run:\s*[|>]?[+-]?\s*", "", line)
        line = re.sub(r"\s#\s.*$", "", line)
        commands += [part.strip() for part in re.split(r"&&|\|\||;|\|", line) if part.strip()]
    return commands


def _at_command_position(words: list[str], names: set[str]) -> tuple[str, list[str]] | None:
    start = 0
    while start < len(words):
        word = words[start].lstrip("\"'(")
        is_prefix = word in PREFIXES or word.startswith("-") or word[:1].isdigit()
        if not is_prefix and not re.match(r"\w+=", word):
            break
        start += 2 if word == "-u" else 1
    if start < len(words):
        name = words[start].strip("\"'()").rsplit("/", 1)[-1]
        if name in names:
            return name, words[start + 1 :]
    return None


def _invocation(command: str, names: set[str]) -> tuple[str, list[str]] | None:
    """Вызов одной из `names`: в начале команды, после `sudo`/`env`/`do` или в кавычках у `ssh`."""
    words = command.split()
    if not words:
        return None
    if found := _at_command_position(words, names):
        return found
    if words[0].strip("\"'") in PROSE:
        return None
    for index in range(1, len(words)):
        if words[index][:1] in "\"'" and (found := _at_command_position(words[index:], names)):
            return found
    return None


def _positionals(arguments: list[str]) -> list[str]:
    found, skip = [], False
    for raw in arguments:
        word = raw.strip("\"'")
        if skip:
            skip = False
        elif word.startswith("-") and word != "-":
            skip = word in VALUE_OPTIONS
        elif word:
            found.append(word)
    return found


def _destinations(name: str, arguments: list[str]) -> list[str]:
    words = [raw.strip("\"'") for raw in arguments]
    if name == "rsync" and "--dry-run" in words:
        return []
    if name in {"cp", "mv", "install"}:
        for index, word in enumerate(words):
            if word in {"-t", "--target-directory"} and index + 1 < len(words):
                return [words[index + 1]]
            if word.startswith("--target-directory="):
                return [word.split("=", 1)[1]]
    positionals = _positionals(arguments)
    if name == "tee":
        return positionals
    # Куда копируют — последний аргумент; всё до него — что копируют.
    return positionals[-1:] if len(positionals) > 1 else []


def _unpack_target(arguments: list[str], cwd: str) -> str:
    words = [raw.strip("\"'") for raw in arguments]
    clusters = [word for word in words if re.fullmatch(r"-[A-Za-z]*x[A-Za-z]*", word)]
    first = bool(words) and re.fullmatch(r"[A-Za-z]*x[A-Za-z]*", words[0])
    if not (clusters or first or "--extract" in words):
        return ""
    for index, word in enumerate(words):
        if word in {"-C", "--directory"} and index + 1 < len(words):
            return words[index + 1]
        if word.startswith("--directory="):
            return word.split("=", 1)[1]
    return cwd


def _is_live(path: str, cwd: str, over_network: bool = False) -> bool:
    path = path.strip("\"'")
    if over_network and re.match(HOST, path):
        return bool(OVER_NETWORK.match(path))
    # `хост:путь` понимают только rsync и scp. Для остальных двоеточие — часть
    # имени, а в python-вставке workflow `if pages > LIMIT:` и вовсе сравнение.
    if cwd and not re.match(r"/|~|\$", path):
        path = cwd if path in {".", "./"} else f"{cwd.rstrip('/')}/{path.removeprefix('./')}"
    return bool(ON_HOST.match(path))


def _writes_live_code(script: str) -> list[str]:
    found, cwd = [], ""
    for command in _commands(script):
        if command == STEP:
            cwd = ""
            continue
        if (moved := _invocation(command, {"cd"})) and moved[1]:
            cwd = moved[1][0].strip("\"'")
            continue
        targets = REDIRECT.findall(command)
        plain = re.sub(r"\s[\d&]?[<>].*$", "", command)
        if unpacked := _invocation(plain, {"tar"}):
            targets.append(_unpack_target(unpacked[1], cwd))
        written = any(target and _is_live(target, cwd) for target in targets)
        if not written and (copied := _invocation(plain, COPIERS)):
            over_network = copied[0] in {"rsync", "scp"}
            written = any(_is_live(to, cwd, over_network) for to in _destinations(*copied))
        if written:
            found.append(command)
    return found


def test_only_deploy_workflow_writes_live_code():
    writers = {
        path.name: commands
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        if (commands := _writes_live_code(path.read_text()))
    }
    # Без этого тест проходил бы и с распознавателем, который не видит ничего.
    assert ALLOWED_WRITERS <= writers.keys(), (
        "распознаватель не видит, как deploy.yml кладёт код в живой каталог — "
        "форма записи изменилась, научить ей _writes_live_code"
    )
    extra = {
        name: commands[:3] for name, commands in writers.items() if name not in ALLOWED_WRITERS
    }
    assert not extra, (
        "workflow копирует файлы в живой каталог прода мимо deploy.yml — без сверки "
        "ревизии базы с миграциями коммита новый код ляжет на старую схему. "
        f"Выкладывать через deploy.yml: {extra}"
    )


SSH = '-e "ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=15"'

WRITES = {
    # Формы удалённых workflow.
    "hotfix: один файл по rsync": f"""
        for file in api.py analytics.py
        do
          rsync -z --checksum --inplace --no-perms --no-owner --no-group \\
            {SSH} \\
            "src/$file" "pm@13.140.186.143:/opt/pharmacy-monitor/src/$file"
        done
    """,
    "hotfix: файл с моделями базы": """
        rsync -az --delay-updates -e "ssh -i ~/.ssh/id_ed25519" \\
          src/storage.py pm@13.140.186.143:/opt/pharmacy-monitor/src/storage.py
    """,
    "активация: каталог из чернового на сервере": """
        rsync -a --checksum --delay-updates --exclude='__pycache__/' \\
          "$runtime_dir/src/" /opt/pharmacy-monitor/src/
    """,
    "активация: файл настроек alembic": """
        cp -p "$runtime_dir/alembic.ini" /opt/pharmacy-monitor/alembic.ini
    """,
    "откат внутри строки ssh": """
        ssh pm@13.140.186.143 \\
          "set -euo pipefail
           cp -- '$BACKUP_DIR/api.py' /opt/pharmacy-monitor/src/api.py
           sudo -n /usr/bin/systemctl restart pharmacy-monitor-api.service"
    """,
    # Формы, которыми выкладку описывали в документах.
    "всё дерево в корень каталога": f"""
        rsync -az --delete {SSH} ./ "pm@13.140.186.143:/opt/pharmacy-monitor/"
    """,
    "каталог в корень по scp": "scp -r src pm@13.140.186.143:/opt/pharmacy-monitor/",
    "tar через ssh после cd": """
        tar czf - src | ssh pm@13.140.186.143 'cd /opt/pharmacy-monitor && tar xzf -'
    """,
    "путь от домашнего каталога pm": "scp src/api.py pm@13.140.186.143:src/api.py",
    # То же, записанное иначе.
    "хост из переменной": 'rsync -az src/ "$PROD_USER@$PROD_HOST:/opt/pharmacy-monitor/src/"',
    "параметр после пути назначения": """
        rsync -az src/ pm@13.140.186.143:/opt/pharmacy-monitor/src/ --delete --exclude '*.pyc'
    """,
    "вывод команды перенаправлен": "cp main.py /opt/pharmacy-monitor/src/main.py 2>&1",
    "после другой команды": "cd /tmp/release && cp main.py /opt/pharmacy-monitor/src/main.py",
    "от имени другого пользователя": """
        sudo -n /usr/bin/install -m 644 health.py /opt/pharmacy-monitor/src/health.py
    """,
    "относительный путь после cd": """
        cd /opt/pharmacy-monitor
        cp "$fix/api.py" src/api.py
    """,
    "каталог назначения параметром": "cp -t /opt/pharmacy-monitor/src api.py analytics.py",
    "одной строкой в run": "run: scp src/api.py pm@13.140.186.143:/opt/pharmacy-monitor/src/",
    "перенаправление в файл": "printf '%s' \"$patch\" > /opt/pharmacy-monitor/src/health.py",
    "tee на сервере": "cat fix.py | ssh pm@host 'tee /opt/pharmacy-monitor/src/health.py'",
    "распаковка в каталог": "tar -xzf release.tgz -C /opt/pharmacy-monitor",
    "распаковка, каталог раньше ключа": "tar -C /opt/pharmacy-monitor/src -xzf fix.tgz",
}


@pytest.mark.parametrize("script", WRITES.values(), ids=WRITES.keys())
def test_detector_sees_a_write_into_live_code(script: str):
    assert len(_writes_live_code(script)) == 1


READS = {
    "копия живого файла в сторону": "cp -- /opt/pharmacy-monitor/src/api.py '$backup_dir/api.py'",
    "копия живого каталога в сторону": """
        rsync -a --delete --exclude='__pycache__/' /opt/pharmacy-monitor/src/ "$backup_dir/src/"
    """,
    "чекаут в черновой каталог": f"""
        rsync -az --checksum --delete {SSH} src/ "pm@13.140.186.143:$work_dir/runtime/src/"
    """,
    "чекаут в каталог релиза": """
        rsync -az src/ pm@13.140.186.143:/opt/pharmacy-monitor/.deploy-stage.abc123/src/
    """,
    "сверка и компиляция": """
        sha256sum /opt/pharmacy-monitor/src/main.py
        .venv/bin/python -m py_compile /opt/pharmacy-monitor/src/api.py
    """,
    "запись в data": """
        printf '%s\\n' "$marker" > /opt/pharmacy-monitor/data/pharmonline-public-api-autonomous-v1
    """,
    "архив живого каталога": "tar -czf /tmp/src.tgz -C /opt/pharmacy-monitor src",
    "распаковка в data": "tar -xzf dump.tgz -C /opt/pharmacy-monitor/data/restore",
    "пробный rsync": "rsync -az --dry-run src/ pm@13.140.186.143:/opt/pharmacy-monitor/src/",
    "установка зависимостей": """
        cd /opt/pharmacy-monitor/frontend
        NODE_OPTIONS=--max-old-space-size=4096 pnpm install --frozen-lockfile
    """,
    "команда в тексте сообщения": """
        echo "::error::refusing to rsync src/ pm@host:/opt/pharmacy-monitor/src/"
    """,
    "команда в комментарии": "# rsync src/ pm@13.140.186.143:/opt/pharmacy-monitor/src/",
    "команда в подписи шага": "- name: cp fix.py /opt/pharmacy-monitor/src/fix.py",
    "стрелка в тексте": 'echo "release -> /opt/pharmacy-monitor/src"',
    "сравнение в python-вставке": """
        cd /opt/pharmacy-monitor
        .venv/bin/python - <<'PY'
        if expected_pages > MAX_PAGES:
            raise SystemExit(1)
        if total > src:
            raise SystemExit(1)
        PY
    """,
    "файл с двоеточием в имени": "cp report.txt notes:",
    "cd из прошлого шага": """
        - name: Look at production
          run: |
            ssh pm@13.140.186.143 'cd /opt/pharmacy-monitor && ls src'
        - name: Keep a copy of the report on the runner
          run: |
            cp report.txt templates/report.txt
    """,
}


@pytest.mark.parametrize("script", READS.values(), ids=READS.keys())
def test_detector_ignores_what_is_not_a_write_into_live_code(script: str):
    assert _writes_live_code(script) == []
