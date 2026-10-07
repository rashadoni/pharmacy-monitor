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
- распознаватель видит запись самого `deploy.yml` и формы из `WRITES`: те,
  которыми писали удалённые workflow, и те, которыми ту же ошибку проще всего
  повторить
- распознаватель не принимает за запись то, что в `READS`: чтение живого
  каталога, копию в черновой каталог, текст сообщений

Чего тест не видит:
- каталог из переменной (`$app/src/`, `cd "$app"`) и домашний каталог без пути
  (`cd ~`, голый `cd`, `tar xzf -` во второй строке многострочной команды ssh)
- запись другими командами: `sed -i`, `patch`, `unzip -d`, `curl -o`,
  `git pull` или `git reset` в каталоге, python, своя shell-функция
- скрипт из репозитория, который workflow исполняет на сервере
  (`bash -s < infra/…sh`): в сам скрипт тест не заглядывает
- код чекаута, запущенный из чернового каталога против боевой базы, и
  `alembic upgrade` мимо `deploy.yml` — живой каталог при этом не меняется, а
  схема может

Это сторож от возврата известной ошибки, а не доказательство, что обходного пути
нет. Если тест ругается на безобидную строку — научить распознаватель и добавить
её в `READS`, а не обходить его.
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

COPIERS = {"rsync", "scp", "cp", "install", "mv", "tee", "ln"}
# Параметры, за которыми идёт отдельное значение, а не путь. У каждой команды
# свои: у `cp` и `mv` `-f` значения не берёт, и общий список съедал источник.
VALUE_OPTIONS = {
    "rsync": {
        *("-e", "--rsh", "-f", "--filter", "--exclude", "--include", "--chmod", "--chown"),
        *("--files-from", "--exclude-from", "--include-from", "--backup-dir", "--link-dest"),
        *("--temp-dir", "-T", "--log-file", "--rsync-path", "--timeout", "--bwlimit"),
    },
    "scp": {"-i", "-o", "-P", "-F", "-c", "-l", "-S", "-J"},
    "install": {"-m", "-o", "-g"},
}
# Слова перед командой, которые её не меняют.
PREFIXES = {
    *("sudo", "env", "nohup", "exec", "time", "nice", "timeout", "xargs"),
    *("if", "!", "then", "else", "elif", "do", "until", "while", "{"),
}
# Команды, которые исполняют свой аргумент: `ssh host 'cp …'`, `bash -c "…"`.
WRAPPERS = {"ssh", "bash", "sh", "su", "flock", "retry"}
REDIRECT = re.compile(r"(?:^|[\s\"')])\d?>>?\s*([^\s&|<>][^\s|<>]*)")
STEP_START = re.compile(r"\s*(?:-\s+)?(?:run|uses):|\s*-\s+(?:name|id):")
CAPTION = re.compile(r"\s*#|\s*(?:-\s+)?(?:name|description):")
HEREDOC = re.compile(r"<<-?\s*[\"']?([A-Za-z_]\w*)[\"']?")


def _commands(line: str) -> list[str]:
    """Команды одной строки: цепочки `&&`, `;`, `|` и подстановки `$(…)` разрезаны."""
    line = re.sub(r"^\s*(?:-\s+)?run:\s*[|>]?[+-]?\s*", "", line)
    line = re.sub(r"\s#\s.*$", "", line)
    return [part.strip() for part in re.split(r"&&|\|\||;|\||\$\(|`", line) if part.strip()]


def _name(word: str) -> str:
    return word.strip("\"'()").rsplit("/", 1)[-1]


def _command_position(words: list[str]) -> int:
    index = 0
    while index < len(words):
        word = words[index].lstrip("\"'(")
        if word == "-u":
            index += 2
            continue
        is_prefix = word in PREFIXES or word.startswith("-") or word[:1].isdigit()
        # `ИМЯ=значение` перед командой и метка ветки `case`: `deploy) cp …`.
        if not (is_prefix or re.match(r"\w+=", word) or word.endswith(")")):
            break
        index += 1
    return index


def _invocation(command: str, names: set[str]) -> tuple[str, list[str], str] | None:
    """Вызов одной из `names`: команда, её аргументы и через что она вызвана.

    Третье — `ssh`, `bash`, `$ssh_base`, если команда стоит в их аргументе, и
    пустая строка, если она сама в начале. Слово в аргументе другой команды
    (`echo "cp …"`, `log "mv …"`) вызовом не считается.
    """
    words = command.split()
    start = _command_position(words)
    if start >= len(words):
        return None
    first = _name(words[start])
    if first in names:
        return first, words[start + 1 :], ""
    if first in WRAPPERS or first.startswith("$"):
        for index in range(start + 1, len(words)):
            if _name(words[index]) in names:
                return _name(words[index]), words[index + 1 :], first
    return None


def _arguments(name: str, raw_arguments: list[str]) -> tuple[list[str], list[str]]:
    """Параметры и пути команды. Значение параметра (`-e "ssh -i ключ"`) — ни то ни другое."""
    takes_value = VALUE_OPTIONS.get(name, set())
    options, paths, skip, quote = [], [], False, ""
    for raw in raw_arguments:
        word = raw.strip("\"'()")
        if quote:
            quote = "" if raw.endswith(quote) else quote
        elif skip:
            skip = False
            if raw[:1] in "\"'" and not (len(raw) > 1 and raw.endswith(raw[0])):
                quote = raw[0]
        elif word.startswith("-") and word != "-":
            options.append(word)
            skip = word in takes_value
        elif word:
            paths.append(word)
    return options, paths


def _destinations(name: str, raw_arguments: list[str]) -> list[str]:
    options, paths = _arguments(name, raw_arguments)
    is_dry_run = re.compile(r"--dry-run|-[A-Za-z]*n[A-Za-z]*").fullmatch
    if name == "rsync" and any(is_dry_run(option) for option in options):
        return []
    if name in {"cp", "mv", "install", "ln"}:
        words = [raw.strip("\"'()") for raw in raw_arguments]
        for index, word in enumerate(words[:-1]):
            if word in {"-t", "--target-directory"}:
                return [words[index + 1]]
        for word in words:
            if word.startswith("--target-directory="):
                return [word.split("=", 1)[1]]
    if name == "tee":
        return paths
    # Куда копируют — последний путь; всё до него — что копируют.
    return paths[-1:] if len(paths) > 1 else []


def _unpack_target(raw_arguments: list[str], default: str) -> str:
    words = [raw.strip("\"'()") for raw in raw_arguments]
    clusters = [word for word in words if re.fullmatch(r"-[A-Za-z]*x[A-Za-z]*", word)]
    first = bool(words) and re.fullmatch(r"[A-Za-z]*x[A-Za-z]*", words[0])
    if not (clusters or first or "--extract" in words):
        return ""
    for index, word in enumerate(words[:-1]):
        if word in {"-C", "--directory"}:
            return words[index + 1]
    for word in words:
        if word.startswith("--directory="):
            return word.split("=", 1)[1]
    return default


def _is_live(path: str, cwd: str, over_network: bool = False) -> bool:
    path = path.strip("\"'()")
    if over_network and re.match(HOST, path):
        return bool(OVER_NETWORK.match(path))
    # `хост:путь` понимают только rsync и scp. Для остальных двоеточие — часть
    # имени, а в python-вставке workflow `if pages > LIMIT:` и вовсе сравнение.
    if cwd and not re.match(r"/|~|\$", path):
        path = cwd if path in {".", "./"} else f"{cwd.rstrip('/')}/{path.removeprefix('./')}"
    return bool(ON_HOST.match(path))


def _writes(command: str, cwd: str) -> bool:
    if any(_is_live(target, cwd) for target in REDIRECT.findall(command)):
        return True
    plain = re.sub(r"\s[\d&]?[<>].*$", "", command)
    for names in ({"tar"}, COPIERS):
        if not (called := _invocation(plain, names)):
            continue
        name, raw_arguments, via = called
        # У `ssh host 'tar xzf -'` каталог не назван: это дом `pm` — живой каталог.
        base = cwd or (APP if via == "ssh" or via.startswith("$") else "")
        if name == "tar":
            targets = [_unpack_target(raw_arguments, base)]
        else:
            targets = _destinations(name, raw_arguments)
        if any(to and _is_live(to, base, name in {"rsync", "scp"}) for to in targets):
            return True
    return False


def _odd(line: str, quote: str) -> bool:
    return len(re.findall(rf"(?<!\\){quote}", line)) % 2 == 1


def _writes_live_code(script: str) -> list[str]:
    """Команды workflow, которые пишут в каталоги кода прода.

    Каталог после `cd` действует, пока исполняется то, где он набран: до конца
    строки-аргумента `ssh`, до конца heredoc, до конца шага. Иначе `cd` на
    сервере превращал бы в запись копию отчёта на раннере строкой ниже.
    """
    found: list[str] = []
    cwd = ""
    heredocs: list[tuple[str, bool]] = []  # слово-окончание и «внутри shell, не python»
    open_quote = ""  # аргумент ssh в кавычках, не закрытый на своей строке
    for line in re.sub(r"\\\n\s*", " ", script).splitlines():
        if STEP_START.match(line):
            cwd, open_quote, heredocs = "", "", []
        inside = bool(heredocs)
        if inside:
            if line.strip() == heredocs[-1][0]:
                heredocs.pop()
                cwd = cwd if heredocs else ""
                continue
            if not heredocs[-1][1]:
                continue
        if CAPTION.match(line):
            continue
        in_one_line_argument = False
        for command in _commands(line):
            if (moved := _invocation(command, {"cd"})) and moved[1]:
                cwd = moved[1][0].strip("\"'()")
                in_one_line_argument = bool(moved[2])
            elif _writes(command, cwd):
                found.append(command)
        if not inside:
            if open_quote:
                if _odd(line, open_quote):
                    cwd = open_quote = ""
            elif unclosed := next((quote for quote in "'\"" if _odd(line, quote)), ""):
                open_quote = unclosed
            elif in_one_line_argument:
                cwd = ""
        if opened := HEREDOC.search(line):
            heredocs.append((opened.group(1), "python" not in line))
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
    "hotfix: откат внутри строки ssh": """
        $ssh_base pm@13.140.186.143 \\
          "set -euo pipefail
           cp -- '$BACKUP_DIR/api.py' /opt/pharmacy-monitor/src/api.py
           sudo -n /usr/bin/systemctl restart pharmacy-monitor-api.service"
    """,
    "hotfix: откат своей функцией": """
        restore_file() {
          if ! cp -p "$1" "$2.rollback" || ! mv -f "$2.rollback" /opt/pharmacy-monitor/src/main.py; then
            return 1
          fi
        }
    """,
    "активация: каталог из чернового на сервере": """
        rsync -a --checksum --delay-updates --exclude='__pycache__/' \\
          "$runtime_dir/src/" /opt/pharmacy-monitor/src/
    """,
    "активация: файл настроек alembic": """
        cp -p "$runtime_dir/alembic.ini" /opt/pharmacy-monitor/alembic.ini
    """,
    # Формы, которыми выкладку описывали в документах.
    "всё дерево в корень каталога": f"""
        rsync -az --delete {SSH} ./ "pm@13.140.186.143:/opt/pharmacy-monitor/"
    """,
    "tar через ssh": "tar czf - src | ssh pm@13.140.186.143 'tar xzf -'",
    "tar через ssh после cd": """
        tar czf - src | ssh pm@13.140.186.143 'cd /opt/pharmacy-monitor && tar xzf -'
    """,
    # То же, записанное иначе.
    "каталог в корень по scp": "scp -r src pm@13.140.186.143:/opt/pharmacy-monitor/",
    "путь от домашнего каталога pm": "scp src/api.py pm@13.140.186.143:src/api.py",
    "хост из переменной": 'rsync -az src/ "$PROD_USER@$PROD_HOST:/opt/pharmacy-monitor/src/"',
    "параметр после пути назначения": """
        rsync -az src/ pm@13.140.186.143:/opt/pharmacy-monitor/src/ --delete --exclude '*.pyc'
    """,
    "rsync с ходом копирования": "rsync -a -P src/ pm@13.140.186.143:/opt/pharmacy-monitor/src/",
    "вывод команды перенаправлен": "cp main.py /opt/pharmacy-monitor/src/main.py 2>&1",
    "после другой команды": "cd /tmp/release && cp main.py /opt/pharmacy-monitor/src/main.py",
    "замена без вопросов": 'cp -f "$fix/main.py" /opt/pharmacy-monitor/src/main.py',
    "перенос файла": 'mv -f "$tmp" /opt/pharmacy-monitor/src/main.py',
    "ссылка на каталог релиза": "ln -sfn /opt/releases/42 /opt/pharmacy-monitor/src",
    "от имени другого пользователя": """
        sudo -n /usr/bin/install -m 644 health.py /opt/pharmacy-monitor/src/health.py
    """,
    "повтор до успеха": """
        until rsync -az src/ pm@13.140.186.143:/opt/pharmacy-monitor/src/; do sleep 5; done
    """,
    "под замком": "flock /tmp/deploy.lock cp main.py /opt/pharmacy-monitor/src/main.py",
    "результат в переменную": 'out=$(cp -v "$fix" /opt/pharmacy-monitor/src/main.py)',
    "ветка case": "deploy) cp main.py /opt/pharmacy-monitor/src/main.py ;;",
    "команда ssh без кавычек": """
        ssh pm@13.140.186.143 cp /tmp/main.py /opt/pharmacy-monitor/src/main.py
    """,
    "относительный путь у команды ssh": "ssh pm@13.140.186.143 'cp /tmp/main.py src/main.py'",
    "относительный путь после cd в heredoc": """
        ssh pm@13.140.186.143 'bash -s' <<'REMOTE'
        set -euo pipefail
        cd /opt/pharmacy-monitor
        .venv/bin/python - <<'PY'
        print("ok")
        PY
        cp "$fix/api.py" src/api.py
        REMOTE
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
    "листинг живого каталога": f"rsync {SSH} pm@13.140.186.143:/opt/pharmacy-monitor/src/",
    "сверка и компиляция": """
        sha256sum /opt/pharmacy-monitor/src/main.py
        .venv/bin/python -m py_compile /opt/pharmacy-monitor/src/api.py
    """,
    "запись в data": """
        printf '%s\\n' "$marker" > /opt/pharmacy-monitor/data/pharmonline-public-api-autonomous-v1
    """,
    "перенос файла в data": 'mv -f "$marker_tmp" "$marker"',
    "архив живого каталога": "tar -czf /tmp/src.tgz -C /opt/pharmacy-monitor src",
    "распаковка в data": "tar -xzf dump.tgz -C /opt/pharmacy-monitor/data/restore",
    "распаковка на раннере": "tar xzf artifact.tgz",
    "пробный rsync": "rsync -az --dry-run src/ pm@13.140.186.143:/opt/pharmacy-monitor/src/",
    "пробный rsync коротко": "rsync -azn src/ pm@13.140.186.143:/opt/pharmacy-monitor/src/",
    "установка зависимостей": """
        cd /opt/pharmacy-monitor/frontend
        NODE_OPTIONS=--max-old-space-size=4096 pnpm install --frozen-lockfile
    """,
    "установка зависимостей на сервере": """
        ssh pm@13.140.186.143 'cd /opt/pharmacy-monitor/frontend && pnpm install --frozen-lockfile'
    """,
    "команда в тексте сообщения": """
        echo "::error::refusing to rsync src/ pm@host:/opt/pharmacy-monitor/src/"
    """,
    "команда в сообщении своей функции": 'log "mv main.py /opt/pharmacy-monitor/src/main.py"',
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
    "скачать бэкап после cd на сервере": """
        ssh pm@13.140.186.143 'cd /opt/pharmacy-monitor && ls data/backups'
        scp pm@13.140.186.143:/opt/pharmacy-monitor/data/backups/latest.gpg .
    """,
    "копия на раннере после heredoc": """
        ssh pm@13.140.186.143 'bash -s' <<'REMOTE'
        cd /opt/pharmacy-monitor
        ls src
        REMOTE
        cp report.txt templates/report.txt
    """,
    "копия на раннере после многострочной команды ssh": """
        ssh pm@13.140.186.143 'set -euo pipefail
          cd /opt/pharmacy-monitor
          ls src'
        cp report.txt templates/report.txt
    """,
    "cd из прошлого шага": """
        - name: Look at production
          run: |
            ssh pm@13.140.186.143 'bash -s' <<'REMOTE'
            cd /opt/pharmacy-monitor
        - name: Keep a copy of the report on the runner
          run: |
            cp report.txt templates/report.txt
    """,
}


@pytest.mark.parametrize("script", READS.values(), ids=READS.keys())
def test_detector_ignores_what_is_not_a_write_into_live_code(script: str):
    assert _writes_live_code(script) == []
