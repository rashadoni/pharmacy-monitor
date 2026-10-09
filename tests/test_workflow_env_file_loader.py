"""Разбор файла секретов прод-сервера в workflow ничего из файла не печатает.

Зачем тест: `/etc/pharmacy-monitor/env` — секреты боевого сервера, а журнал шага
GitHub Actions в этом репозитории читает любой пользователь GitHub. Workflow,
которым нужно всё окружение, подают файл удалённому скрипту сами: systemd при
входе по ssh его не загружает. До 2026-10-09 это делал цикл `while read`, и
строку, которую он не смог разобрать, он печатал целиком:

- строка без `=` (вторая половина значения, разорванного переводом строки) —
  цикл еженедельного сбора писал `invalid EnvironmentFile key: <строка>`;
- имя с дефисом проходило проверку (шаблон `[A-Za-z_][A-Za-z0-9_]*` — glob, его
  `*` пропускает любые знаки), и уже bash печатал
  ``export: `ИМЯ=значение': not a valid identifier``;
- в остальных workflow проверки имени не было совсем — там печатал только bash;
- значение под именем `LC_ALL` bash печатал в предупреждении `setlocale`.

Попутно цикл расходился с systemd, чей синтаксис брался воспроизводить: CR на
конце значения и кавычки вокруг него оставались в значении. Служба получала
`abc123`, сбор из workflow — `abc123\\r`.

Покрываем (исполняя текст разбора, вынутый из самого workflow):
- отказ называет номер строки и причину, из файла в выводе нет ничего — ни
  значения, ни имени; после отказа скрипт дальше не идёт;
- принятая строка даёт то же значение, что systemd. Таблицы ниже сняты с
  настоящего systemd 255 командой
  `systemd-run --user --wait --pipe -p EnvironmentFile=<файл> /usr/bin/env -0`:
  по ней же проверять новую строку таблицы;
- строку, которую systemd прочёл бы иначе (кавычки посреди значения, обратная
  косая черта, CR внутри строки) или пропустил бы (нет `=`, негодное имя), разбор
  не принимает вовсе. Строже systemd намеренно: так выглядит оборванное значение,
  и сбору лучше остановиться, чем уйти с половиной секрета;
- имя, которому bash значение из файла не отдаст (только для чтения, вычисляемое,
  перенастраивающее саму оболочку) или которым пользуется сам разбор, — отказ.
  Так же и имя, которое bash держит числом (`OPTIND`, `SECONDS`, `UID`):
  значение под ним bash не сохранил бы, а вычислил, то есть исполнил;
- у цикла разбора нет stderr: ни сам bash (предупреждение `setlocale`,
  «readonly variable»), ни трассировка (`set -x`) окружающего скрипта изнутри
  него ничего сказать не могут;
- файл, который не открылся или оказался каталогом, — тоже отказ, а не пустое
  окружение; отказ останавливает скрипт и без `set -e`;
- обычные строки дают то же окружение, что давал прежний цикл: на файле без
  кавычек, пробелов по краям, CR и обратной косой черты замена ничего не меняет;
- во всех workflow разбор один и тот же текст, и в нём нет одинарной кавычки
  (в `production-aptekonline-scrape.yml` скрипт лежит внутри одинарных кавычек);
- workflow не читают файл секретов способом, которого нет в списке известных;
- команда проверки файла из docs/RUNBOOK.md отвечает строкой отказа или одним
  словом;
- опасные файлы этого теста действительно дают утечку через прежний цикл —
  иначе тест мог бы зеленеть, ничего не ловя.

Чего тест не видит и что не закрыто:
- значение оборвано так, что его хвост сам похож на `ИМЯ=значение`: такая строка
  законна и для разбора, и для systemd;
- что печатают команды, запущенные после разбора: секреты у них в окружении;
- файл, который systemd не загружает вовсе (байт NUL, негодный UTF-8 — служба с
  таким файлом не стартует): разбор его читает, NUL молча теряя;
- `PWD` и `SHLVL` bash после разбора ведёт сам — это свойство оболочки, а не
  разбора. Имя, которое bash вычисляет сам, пройдёт, если значение в файле
  совпало с вычисленным (`PIPESTATUS=0`);
- значение под именем локали (`LC_ALL`, `LC_*`, `LANG`), если такой локали нет:
  разбор его принимает молча, как и systemd, но bash назовёт его в своём
  предупреждении позже — при следующей смене локали в том же скрипте или при
  старте дочернего bash;
- строка с именем переменной самого скрипта (`work_dir=…`) её перезапишет, как
  и при прежнем цикле: разбор знает только свои имена и имена bash;
- разбор защищает от испорченного файла, а не от написанного со злым умыслом:
  переменные окружения сами управляют следующими командами (`PATH`,
  `LD_PRELOAD`), а писать в файл может тот же, кто запускает скрипт;
- трассировку, уведённую в свой дескриптор (`BASH_XTRACEFD`);
- цена немого цикла: фатальная ошибка bash внутри него завершит скрипт без
  единого слова. На содержимом файла такой не нашлось, но правка цикла может её
  внести — поэтому каждая его ветка здесь исполняется;
- чтения файла из `OTHER_READERS` (их этот тест только перечисляет),
  `infra/scripts/backup.sh`, который зовёт `deploy.yml`, и путь к файлу,
  собранный из частей;
- workflow из `AWAITING_REMOVAL`.
"""

from __future__ import annotations

import random
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
ENV_FILE = "/etc/pharmacy-monitor/env"
LOADER_NAME = "load_environment_file"
LOADER_OPENS = f"{LOADER_NAME}() {{"
LOADER_CALL = f"{LOADER_NAME} {ENV_FILE}"
LOOP_ENDS = f"done < {ENV_FILE}"
WEEKLY_REFRESH = "autonomous-pharmonline-decodo-public-api.yml"

# Цикл, стоявший в workflow до 2026-10-09. Нужен дважды: сверить, что файлы из
# AWAITING_REMOVAL с тех пор не менялись, и показать, что опасные файлы этого
# теста через него действительно текут.
PREVIOUS_LOOP = r"""while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|\#*) continue ;;
  esac
  key=${line%%=*}
  value=${line#*=}
  export "$key=$value"
done < /etc/pharmacy-monitor/env
"""
# Он же с проверкой имени: так читал файл еженедельный сбор pharmonline.
PREVIOUS_CHECKED_LOOP = r"""while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|'#'*) continue ;;
  esac
  key=${line%%=*}
  value=${line#*=}
  case "$key" in
    [A-Za-z_][A-Za-z0-9_]*) export "$key=$value" ;;
    *) echo "invalid EnvironmentFile key: $key" >&2; exit 1 ;;
  esac
done < /etc/pharmacy-monitor/env
"""

# Три workflow с прежним циклом. Открытые PR #44 и #49 удаляют эти файлы целиком:
# правка цикла сделала бы оба PR конфликтными («изменён здесь, удалён там»), а
# конфликт, разрешённый не в ту сторону, вернул бы workflow, который там удаляют
# намеренно. Запускаются они только руками; путь в них закрывает удаление.
# Список не растёт: новый файл сюда не вписывать, а переводить на общий разбор.
# Запись о файле, которого уже нет, безвредна — её убирают следующей правкой.
# Срока у списка нет: если #44 и #49 не будут смержены, эти три файла надо
# перевести на общий разбор и список убрать.
AWAITING_REMOVAL = {
    "recover-pharmonline-scraperapi-public-api.yml": "#44",
    "preflight-pharmonline-scraperapi-public-api-full.yml": "#49",
    "probe-pharmonline-scraperapi-json.yml": "#49",
}

# Другие чтения файла секретов, которые есть в workflow. Этот тест их не
# исполняет и ничего о них не доказывает — он только не даёт появиться новому
# чтению незамеченным. Новая строка сюда — сначала посмотреть, что она печатает.
# `grep | cut` читает буквально: кавычки и CR остаются в значении.
OTHER_READERS = {
    # Проверка, что файл читается; содержимого не касается.
    f"test -r {ENV_FILE}",
    # deploy.yml: одно значение в переменную оболочки, в журнал не выводится.
    f'db_url=$(grep -m1 "^DATABASE_URL=" {ENV_FILE} | cut -d= -f2-)',
    # health-pharmonline-hotfix-deploy.yml: читает Python, а не оболочка.
    f'env_path = Path("{ENV_FILE}")',
    f'for raw_line in Path("{ENV_FILE}").read_text().splitlines():',
    f'load_dotenv("{ENV_FILE}", override=True)',
}


def _readers(path: Path) -> list[str]:
    """Каждый разбор файла секретов в workflow — без отступа, как его получит bash.

    Узнаёт и общий разбор (функция и её вызов), и прежний цикл: вернувшийся цикл
    должен не просто найтись по тексту, а пройти те же проверки исполнением.
    """
    lines = path.read_text().split("\n")
    found = []
    for at, line in enumerate(lines):
        indent = line[: len(line) - len(line.lstrip())]
        if line.strip() == LOADER_CALL:
            opens = [i for i in range(at) if lines[i] == indent + LOADER_OPENS]
            assert opens and lines[at - 1] == indent + "}", (
                f"{path.name}:{at + 1}: вызов разбора без функции прямо над ним"
            )
            start = opens[-1]
        elif line.strip() == LOOP_ENDS:
            starts = [i for i in range(at) if lines[i].startswith(indent + "while ")]
            assert starts, f"{path.name}:{at + 1}: цикл без начала"
            start = starts[-1]
        else:
            continue
        block = [
            row[len(indent) :] if row.startswith(indent) else row.lstrip()
            for row in lines[start : at + 1]
        ]
        found.append("\n".join(block) + "\n")
    return found


def _copies() -> dict[str, str]:
    """Workflow → его разбор файла секретов. Ждущие удаления сюда не входят."""
    copies = {}
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        if path.name in AWAITING_REMOVAL:
            continue
        readers = _readers(path)
        if readers:
            assert len(readers) == 1, f"{path.name}: файл секретов разбирается дважды"
            copies[path.name] = readers[0]
    return copies


COPIES = _copies()
every_copy = pytest.mark.parametrize("workflow", sorted(COPIES))


@dataclass
class Outcome:
    code: int
    stdout: str
    stderr: str
    # None — до конца скрипт не дошёл; иначе окружение после разбора.
    exported: dict[str, str] | None


def _shell(workflow: str | None) -> str:
    """Как workflow отдаёт скрипт удалённому bash: на stdin (`bash -s`) или аргументом.

    Аргументом — там, где скрипт передан ssh строкой в одинарных кавычках, а не
    here-документом: его исполняет оболочка входа через `-c`.
    """
    if workflow is None or "<<'REMOTE'" in (WORKFLOWS / workflow).read_text():
        return "-s"
    return "-c"


NO_FILE = None
A_DIRECTORY = b"\0a directory in place of the file"


def _put(env_file: Path, content: bytes | None) -> None:
    if env_file.is_file():
        env_file.unlink()
    if content is A_DIRECTORY:
        env_file.mkdir(exist_ok=True)
    elif content is not NO_FILE:
        env_file.write_bytes(content)


def _run(
    reader: str,
    content: bytes | None,
    tmp_path: Path,
    *,
    workflow: str | None = None,
    options: str = "set -euo pipefail",
) -> Outcome:
    """Исполнить разбор так, как его исполняет workflow: тем же bash и под теми же опциями.

    Окружение после разбора уходит в файл, а не в stdout: в stdout и stderr
    остаётся только то, что напечатал сам разбор.
    """
    env_file = tmp_path / "environment"
    _put(env_file, content)
    exported = tmp_path / "exported"
    exported.unlink(missing_ok=True)
    script = (
        f"{options}\n"
        + reader.replace(ENV_FILE, shlex.quote(str(env_file)))
        # Полный путь: файл вправе задать свой PATH.
        + f"/usr/bin/env -0 > {shlex.quote(str(exported))}\n"
    )
    how = _shell(workflow)
    done = subprocess.run(
        ["bash", "-c", script] if how == "-c" else ["bash", "-s"],
        input=None if how == "-c" else script.encode(),
        capture_output=True,
        # Как на сервере при входе по ssh: LANG=C.UTF-8 (замер 2026-10-09).
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        timeout=60,
        check=False,
    )
    values = None
    if exported.exists():
        values = {}
        for item in exported.read_bytes().split(b"\0"):
            name, equals, value = item.partition(b"=")
            if equals:
                values[name.decode()] = value.decode()
    return Outcome(done.returncode, done.stdout.decode(), done.stderr.decode(), values)


SECRET = "SECRETtokenSECRETtoken"

# Строки, которые разбор не принимает: (содержимое файла, номер строки, причина).
# В каждой есть SECRET — он не должен появиться в выводе. Что с такой строкой
# сделал бы systemd, написано рядом: пропустил бы или прочёл иначе.
NOT_A_PAIR = "not NAME=VALUE"
BAD_NAME = "the name is not a variable name"
QUOTING = "quoting systemd reads differently"
BACKSLASH = "a backslash systemd reads differently"
CARRIAGE_RETURN = "a carriage return inside the line"
OWNED_NAME = "the name belongs to bash or to this loader"
REFUSED = {
    # Значение разорвано переводом строки; systemd вторую половину пропускает.
    "torn-value": (f"FIRST=1\nTELEGRAM_BOT_TOKEN=1234\n4321:{SECRET}\n", 3, NOT_A_PAIR),
    # Хвост начинается с буквы: прежний шаблон имени принимал его за имя.
    "torn-value-tail-of-words": (f"FIRST=1\nTOKEN=1234\n{SECRET} and more\n", 3, NOT_A_PAIR),
    "torn-value-without-final-newline": (f"FIRST=1\nTOKEN=1234\n4321:{SECRET}", 3, NOT_A_PAIR),
    # systemd строку с негодным именем пропускает (и пишет её в журнал службы).
    "name-with-a-dash": (f"FIRST=1\nSOME-KEY={SECRET}\n", 2, BAD_NAME),
    "name-with-a-space": (f"MY KEY={SECRET}\n", 1, BAD_NAME),
    "name-starting-with-a-digit": (f"1KEY={SECRET}\n", 1, BAD_NAME),
    "name-with-a-colon": (f"KEY:PART={SECRET}\n", 1, BAD_NAME),
    "name-not-in-ascii": (f"КЛЮЧ={SECRET}\n", 1, BAD_NAME),
    "empty-name": (f"={SECRET}\n", 1, BAD_NAME),
    "shell-export-prefix": (f"export TOKEN={SECRET}\n", 1, BAD_NAME),
    # Метка порядка байтов в начале файла: в редакторе её не видно.
    "byte-order-mark": (f"\ufeffTOKEN={SECRET}\n", 1, BAD_NAME),
    # Имя годное, но значение из файла под ним не окажется. systemd такую строку
    # службе отдаёт, bash — нет: имя только для чтения (и bash от такого export
    # завершается на месте, со своим сообщением)…
    "name-bash-keeps-read-only": (f"SHELLOPTS={SECRET}\n", 1, OWNED_NAME),
    # …имя, которое bash держит числом: значение под ним он вычисляет, то есть
    # исполняет. Третья строка последнего файла без отказа присвоила бы причине
    # отказа число из другой переменной — и разбор напечатал бы его сам…
    "name-bash-holds-as-a-number": (f"UID={SECRET}\n", 1, OWNED_NAME),
    "number-name-running-a-command": (
        f"OPTIND=PATH[$(echo {SECRET} >&2; echo {SECRET})0]\n",
        1,
        OWNED_NAME,
    ),
    "number-name-assigning-the-refusal-text": (
        f"TOKEN={SECRET}\nCHAT_ID=123456789\nOPTIND=env_refusal=CHAT_ID\n",
        3,
        OWNED_NAME,
    ),
    # …имя, значение которого bash вычисляет сам и присваивание молча отбрасывает…
    "name-bash-computes": (f"GROUPS={SECRET}\n", 1, OWNED_NAME),
    "name-of-the-last-argument": (f"_={SECRET}\n", 1, OWNED_NAME),
    # …имя, перенастраивающее саму оболочку: BASH_ARGV0 — это `$0`, им bash
    # начинает каждое своё сообщение об ошибке до конца скрипта…
    "name-renaming-the-shell": (f"BASH_ARGV0={SECRET}\n", 1, OWNED_NAME),
    "name-of-the-trace-descriptor": (f"BASH_XTRACEFD={SECRET}\n", 1, OWNED_NAME),
    "name-of-the-compatibility-level": (f"BASH_COMPAT={SECRET}\n", 1, OWNED_NAME),
    "name-of-the-startup-file": (f"BASH_ENV={SECRET}\n", 1, OWNED_NAME),
    # …PS4 — приставка каждой строки трассировки: bash её подставляет, то есть
    # печатает и исполняет, если окружающий скрипт идёт под `set -x`…
    "name-of-the-trace-prompt": (f"PS4={SECRET} $(echo {SECRET})\n", 1, OWNED_NAME),
    # …и имена, в которых разбор держит своё состояние: причину отказа он
    # печатает, а счётчик строк вычисляет.
    "name-of-the-refusal-text": (f"env_refusal={SECRET}\nnot a pair\n", 1, OWNED_NAME),
    "name-of-the-line-counter": (f"TOKEN={SECRET} x\nenv_number=TOKEN\nNEXT=1\n", 2, OWNED_NAME),
    "name-of-the-whitespace-set": (f"env_ws={SECRET}\nNEXT= a \n", 1, OWNED_NAME),
    # Номер считается по строкам файла — с комментариями, пустыми и CRLF.
    "counts-every-line": (f"# note\r\n\r\nFIRST=1\r\n;old\r\n  \r\n{SECRET}\r\n", 6, NOT_A_PAIR),
    # Дальше — строки, которые systemd читает, но не буквально.
    "double-quote-not-closed": (f'TOKEN="{SECRET}\nNEXT=1\n', 1, QUOTING),
    "double-quote-closed-early": (f'TOKEN="a"{SECRET}"\n', 1, QUOTING),
    "double-quote-then-more": (f'TOKEN="a"{SECRET}\n', 1, QUOTING),
    "double-quote-then-comment": (f'TOKEN="{SECRET}" # note\n', 1, QUOTING),
    "double-quote-with-escape": (f'TOKEN="{SECRET}\\"x"\n', 1, QUOTING),
    "double-quote-with-backslash": (f'TOKEN="{SECRET}\\n"\n', 1, QUOTING),
    "lone-double-quote": (f'FIRST={SECRET}\nTOKEN="\n', 2, QUOTING),
    "single-quote-not-closed": (f"TOKEN='{SECRET}\nNEXT=1\n", 1, QUOTING),
    "single-quote-closed-early": (f"TOKEN='a'{SECRET}'\n", 1, QUOTING),
    "backslash-in-value": (f"TOKEN={SECRET}\\x\n", 1, BACKSLASH),
    "backslash-before-dollar": (f"TOKEN=\\${SECRET}\n", 1, BACKSLASH),
    "backslash-joining-lines": (f"TOKEN={SECRET}\\\nNEXT=1\n", 1, BACKSLASH),
    # CR для systemd — конец строки: значение обрывается, хвост пропадает.
    "carriage-return-inside-a-value": (f"TOKEN=1234\r{SECRET}\n", 1, CARRIAGE_RETURN),
    "carriage-return-as-line-end": (f"FIRST=1\rTOKEN={SECRET}\r", 1, CARRIAGE_RETURN),
    "carriage-return-inside-a-comment": (f"# note\rTOKEN={SECRET}\n", 1, CARRIAGE_RETURN),
}

ORDINARY_NAMES = (
    "DATABASE_URL",
    "EMAIL_TO",
    "ENV",
    "ENVIRONMENT",
    "HOME",
    "HOSTNAME",
    "LANGUAGE",
    "NO_PROXY",
    "PWD",
    "PYTHONPATH",
    "SHLVL",
    "TERM",
    "TZ",
    "USER",
    "BASHFUL",
    "PS1",
    "ENV_FILE",
    "K8S_NS",
    "a",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "MAIL",
    "IGNOREEOF",
    "TMOUT",
    "FUNCNEST",
    "GLOBIGNORE",
    "OPTERR",
    "COLUMNS",
)

# Строки, которые разбор принимает: (содержимое файла, что обязано оказаться в
# окружении; None — переменной быть не должно). Значения сняты с systemd 255.
ACCEPTED = {
    "plain": ("V=abc123\n", {"V": "abc123"}),
    "crlf": ("V=abc123\r\n", {"V": "abc123"}),
    "spaces-and-tab-after-value": ("V=abc123  \t\n", {"V": "abc123"}),
    "spaces-before-value": ("V=  abc123\n", {"V": "abc123"}),
    "spaces-around-equals": ("V = abc123\n", {"V": "abc123"}),
    "indented-line": ("   V=abc123\n", {"V": "abc123"}),
    "space-inside-value": ("V=abc 123\n", {"V": "abc 123"}),
    "tab-inside-value": ("V=a\tb\n", {"V": "a\tb"}),
    # Вертикальная табуляция и перевод страницы для systemd — не пробелы.
    "vertical-tab-after-value": ("V=abc\x0b\n", {"V": "abc\x0b"}),
    "form-feed-after-value": ("V=abc\x0c\n", {"V": "abc\x0c"}),
    "double-quoted": ('V="abc 123"\n', {"V": "abc 123"}),
    "double-quoted-crlf": ('V="abc 123"\r\n', {"V": "abc 123"}),
    "double-quoted-keeps-inner-spaces": ('V="  abc  "\n', {"V": "  abc  "}),
    "double-quoted-empty": ('V=""\n', {"V": ""}),
    "double-quoted-with-apostrophe": ('V="it\'s"\n', {"V": "it's"}),
    "double-quoted-with-dollar": ('V="a$b`c"\n', {"V": "a$b`c"}),
    "single-quoted": ("V='abc 123'\n", {"V": "abc 123"}),
    "single-quoted-empty": ("V=''\n", {"V": ""}),
    "single-quoted-keeps-backslash": ("V='a\\b'\n", {"V": "a\\b"}),
    "single-quoted-with-double-quote": ("V='a\"b'\n", {"V": 'a"b'}),
    # Кавычка не в начале значения для systemd — обычный знак.
    "double-quote-in-the-middle": ('V=ab"cd\n', {"V": 'ab"cd'}),
    "apostrophe-in-the-middle": ("V=O'Brien\n", {"V": "O'Brien"}),
    "double-quote-at-the-end": ('V=abc"\n', {"V": 'abc"'}),
    # bcrypt-хеш: `$2` не подставляется, как было бы при `source`.
    "dollar-signs": ("V=$2b$12$abc\n", {"V": "$2b$12$abc"}),
    "hash-inside-value": ("V=abc #def\n", {"V": "abc #def"}),
    "equals-inside-value": ("V=a=b=c\n", {"V": "a=b=c"}),
    "glob-characters": ("V=*?[!a]{b,c}~\n", {"V": "*?[!a]{b,c}~"}),
    "empty-value": ("V=\n", {"V": ""}),
    "value-of-spaces": ("V=   \n", {"V": ""}),
    "hash-comment": ("#V=no\nW=yes\n", {"V": None, "W": "yes"}),
    "semicolon-comment": (";V=no\nW=yes\n", {"V": None, "W": "yes"}),
    "indented-comment": ("   # V=no\nW=yes\n", {"V": None, "W": "yes"}),
    # С systemd 254 обратная косая черта в конце комментария его не продолжает.
    "comment-ending-with-backslash": ("# note \\\nV=abc\n", {"V": "abc"}),
    # Прежняя проверка имени требовала двух знаков и однобуквенное имя отвергала.
    "one-letter-name": ("V=1\n", {"V": "1"}),
    "underscore-name": ("_V1=abc\n", {"_V1": "abc"}),
    "lower-case-name": ("http_proxy=abc\n", {"http_proxy": "abc"}),
    "mixed-case-name": ("Mixed_Case9=abc\n", {"Mixed_Case9": "abc"}),
    # Имена, которые оболочка читает сама, но значение из файла держит: дальше
    # по файлу разбор идёт так же. Про негодную локаль bash пишет предупреждение
    # со значением — в вывод оно попасть не должно.
    "name-of-a-locale-setting": (
        "LC_ALL=xx_NO.SUCH-LOCALE\nV= a \n",
        {"LC_ALL": "xx_NO.SUCH-LOCALE", "V": "a"},
    ),
    "names-of-locale-settings": (
        "LANG=C\nLC_CTYPE=C.UTF-8\n",
        {"LANG": "C", "LC_CTYPE": "C.UTF-8"},
    ),
    "name-of-the-field-separator": ("IFS=:=\nV= a:b = c \n", {"IFS": ":=", "V": "a:b = c"}),
    "name-of-the-search-path": ("PATH=/nonexistent\nV=abc\n", {"PATH": "/nonexistent", "V": "abc"}),
    # Переменная-переключатель: с ней bash переходит в режим POSIX, а при выходе
    # из режима сам её удаляет — разбор режимы оболочки не трогает.
    "name-of-the-posix-switch": ("POSIXLY_CORRECT=y\nV= a \n", {"POSIXLY_CORRECT": "y", "V": "a"}),
    # Обычные имена, в том числе те, что bash заводит себе сам, но отдаёт.
    "ordinary-names": (
        "".join(f"{name}=value of {name}\n" for name in ORDINARY_NAMES),
        {name: f"value of {name}" for name in ORDINARY_NAMES},
    ),
    "no-final-newline": ("V=abc", {"V": "abc"}),
    "later-line-wins": ("V=first\nV=second\n", {"V": "second"}),
    "not-ascii-value": ("V=значение\n", {"V": "значение"}),
    "blank-lines-around": ("\n\n  \nV=abc\n\n", {"V": "abc"}),
    "empty-file": ("", {"V": None}),
}


def test_every_workflow_naming_the_loader_is_checked():
    # Пустой список — и проверки ниже не запустятся ни разу, оставаясь зелёными.
    assert WEEKLY_REFRESH in COPIES, "разбор файла секретов в еженедельном сборе не найден"
    naming = {path.name for path in WORKFLOWS.glob("*.y*ml") if LOADER_NAME in path.read_text()}
    assert naming == set(COPIES), "workflow упоминает разбор, но этот тест его не исполняет"


def test_every_workflow_carries_the_same_loader():
    texts = {}
    for name, text in COPIES.items():
        texts.setdefault(text, []).append(name)
    assert len(texts) == 1, "разбор файла секретов разошёлся между workflow: " + " / ".join(
        ", ".join(names) for names in texts.values()
    )


def test_the_loader_fits_inside_a_single_quoted_ssh_argument():
    # production-aptekonline-scrape.yml передаёт скрипт аргументом ssh в одинарных
    # кавычках: первая же одинарная кавычка в разборе (хоть в комментарии) закроет
    # аргумент. Копии одинаковы, поэтому правило общее.
    with_quote = [name for name, text in COPIES.items() if "'" in text]
    assert not with_quote, f"одинарная кавычка в разборе: {with_quote}"
    assert {_shell(name) for name in COPIES} == {"-s", "-c"}, (
        "ни один workflow больше не передаёт скрипт аргументом — правило и `_shell` пересмотреть"
    )


@every_copy
@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_refusal_names_the_line_and_prints_nothing_from_the_file(workflow, case, tmp_path):
    content, line, reason = REFUSED[case]
    assert SECRET in content

    got = _run(COPIES[workflow], content.encode(), tmp_path, workflow=workflow)

    assert SECRET not in got.stdout + got.stderr, "значение из файла секретов попало в вывод"
    # Не только значение: в выводе нет ничего, кроме номера строки и причины.
    assert got.stderr == f"EnvironmentFile line {line}: {reason}\n"
    assert got.stdout == ""
    assert got.code == 1
    assert got.exported is None, "после отказа скрипт пошёл дальше"


@every_copy
@pytest.mark.parametrize("case", sorted(ACCEPTED))
def test_an_accepted_line_means_what_it_means_to_systemd(workflow, case, tmp_path):
    content, expected = ACCEPTED[case]

    got = _run(COPIES[workflow], content.encode(), tmp_path, workflow=workflow)

    assert (got.code, got.stdout, got.stderr) == (0, "", "")
    assert got.exported is not None
    assert {name: got.exported.get(name) for name in expected} == expected


@every_copy
@pytest.mark.parametrize(
    "content",
    [f"TOKEN={SECRET}\nNEXT=1\n", f"TOKEN={SECRET}\n4321:{SECRET}\n"],
    ids=["accepted", "refused"],
)
def test_tracing_in_the_surrounding_script_shows_nothing_from_the_file(workflow, content, tmp_path):
    # Сегодня `set -x` нет ни в одном workflow. Появись он рядом с разбором,
    # трассировка напечатала бы каждое присваивание — вместе со значением. У
    # цикла разбора stderr нет, и трассировке изнутри него писать некуда.
    got = _run(
        COPIES[workflow],
        content.encode(),
        tmp_path,
        workflow=workflow,
        options="set -euxo pipefail",
    )

    assert f"+ {LOADER_NAME} " in got.stderr, "трассировка в этом прогоне не была включена"
    assert SECRET not in got.stdout + got.stderr
    if got.code == 0:
        # Опций оболочки разбор не трогает: за ним трассировка идёт дальше.
        assert "+ /usr/bin/env -0" in got.stderr


@every_copy
@pytest.mark.parametrize("options", ["set -euo pipefail", "set -uo pipefail"], ids=["-e", "no -e"])
def test_a_refusal_stops_the_script_whatever_its_options(workflow, options, tmp_path):
    got = _run(
        COPIES[workflow],
        f"FIRST=1\n4321:{SECRET}\n".encode(),
        tmp_path,
        workflow=workflow,
        options=options,
    )

    assert (got.code, got.stdout, got.stderr) == (1, "", f"EnvironmentFile line 2: {NOT_A_PAIR}\n")
    assert got.exported is None


@every_copy
@pytest.mark.parametrize("options", ["set -euo pipefail", "set -eo pipefail", ":"])
@pytest.mark.parametrize("target", [NO_FILE, A_DIRECTORY], ids=["no file", "directory"])
def test_a_file_that_cannot_be_read_is_a_refusal_not_an_empty_environment(
    workflow, options, target, tmp_path
):
    # Каталог открывается как файл, а читается как пустой: без проверки разбор
    # принял бы его за файл без строк — или, под `set -u`, умер бы молча.
    got = _run(COPIES[workflow], target, tmp_path, workflow=workflow, options=options)

    assert (got.code, got.stdout) == (1, "")
    assert got.stderr == "EnvironmentFile line 0: the file cannot be read\n"
    assert got.exported is None


@pytest.mark.parametrize("name", ["OPTIND", "RANDOM", "SECONDS", "HISTCMD", "UID"])
def test_a_value_is_stored_or_refused_but_never_computed(name, tmp_path):
    # Под именем, которое bash держит числом, значение — арифметическое
    # выражение: индекс массива в нём bash подставляет, то есть исполняет команду.
    marker = tmp_path / "executed"
    content = f"{name}=PATH[$(touch {shlex.quote(str(marker))})0]\n"

    got = _run(COPIES[WEEKLY_REFRESH], content.encode(), tmp_path)

    assert (got.code, got.stderr) == (1, f"EnvironmentFile line 1: {OWNED_NAME}\n")
    assert not marker.exists(), "значение из файла секретов было исполнено"


def test_every_variable_of_the_loader_is_a_name_it_refuses(tmp_path):
    # Имя из файла, совпавшее с переменной самой функции, она присвоила бы себе:
    # причину отказа она печатает, счётчик строк вычисляет. Поэтому все её
    # переменные носят приставку, а имена с этой приставкой она не принимает.
    loader = COPIES[WEEKLY_REFRESH]
    own = [
        word.split("=")[0]
        for line in loader.split("\n")
        if line.strip().startswith("local ")
        for word in line.split()[1:]
    ]
    assert len(own) >= 9, f"переменные разбора не найдены: {own}"

    for name in own:
        assert name.startswith("env_"), f"переменная разбора без приставки env_: {name}"
        got = _run(loader, f"{name}={SECRET}\nnot a pair\n".encode(), tmp_path)
        assert got.stderr == f"EnvironmentFile line 1: {OWNED_NAME}\n", name
        assert got.code == 1


RUNBOOK = ROOT / "docs" / "RUNBOOK.md"
RUNBOOK_CHECK_ENDS = "| ssh root@13.140.186.143 'runuser -u pm -- bash -s'"


@pytest.mark.parametrize(
    ("content", "code", "stdout", "stderr"),
    [
        (b'FIRST=1\nLABEL="Monday 10:00"\nTOKEN=abc\r\n', 0, "accepted\n", ""),
        (b"", 0, "accepted\n", ""),
        (f"FIRST=1\n4321:{SECRET}\n".encode(), 1, "", f"EnvironmentFile line 2: {NOT_A_PAIR}\n"),
        (NO_FILE, 1, "", "EnvironmentFile line 0: the file cannot be read\n"),
        (A_DIRECTORY, 1, "", "EnvironmentFile line 0: the file cannot be read\n"),
    ],
    ids=["good file", "empty file", "torn value", "no file", "directory"],
)
def test_the_runbook_check_answers_with_a_refusal_line_or_one_word(
    content, code, stdout, stderr, tmp_path
):
    # Команда из RUNBOOK берёт функцию из workflow и исполняет её на сервере.
    # Здесь — она же, только вместо ssh локальный bash и файл из теста.
    commands = [
        line for line in RUNBOOK.read_text().split("\n") if line.endswith(RUNBOOK_CHECK_ENDS)
    ]
    assert len(commands) == 1, "команда проверки файла секретов в RUNBOOK не найдена"
    env_file = tmp_path / "environment"
    _put(env_file, content)
    rewrite = shlex.quote(f"s|{ENV_FILE}|{env_file}|")
    local = commands[0].replace(RUNBOOK_CHECK_ENDS, f"| sed {rewrite} | bash -s")

    done = subprocess.run(
        ["bash", "-c", local],
        cwd=ROOT,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        timeout=60,
        check=False,
    )

    assert (done.returncode, done.stdout.decode(), done.stderr.decode()) == (code, stdout, stderr)


def _plain_lines() -> list[tuple[str, str]]:
    """Строки вида, который цикл и systemd читают одинаково: без кавычки в начале
    значения, без обратной косой черты, без CR и без пробелов по краям."""
    rng = random.Random(20261009)
    alphabet = "abcXYZ019 $=#;:/@%+-.,!*?[]{}()<>|&~^`\"'я"
    pairs = []
    for number in range(60):
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))).strip()
        if value[:1] in ('"', "'"):
            value = "x" + value
        pairs.append((f"PLAIN_{number}", value))
    return pairs


@every_copy
@pytest.mark.parametrize(
    "previous", [PREVIOUS_LOOP, PREVIOUS_CHECKED_LOOP], ids=["plain", "checked"]
)
def test_plain_lines_give_the_environment_the_previous_loop_gave(workflow, previous, tmp_path):
    pairs = _plain_lines()
    content = "# comment\n\n" + "".join(f"{name}={value}\n" for name, value in pairs)

    before = _run(previous, content.encode(), tmp_path)
    after = _run(COPIES[workflow], content.encode(), tmp_path, workflow=workflow)

    assert before.code == 0 and after.code == 0, (before.stderr, after.stderr)
    assert after.exported == before.exported
    assert {name: after.exported[name] for name, _ in pairs} == dict(pairs)


@pytest.mark.parametrize(
    ("previous", "case"),
    [
        (PREVIOUS_CHECKED_LOOP, "torn-value"),
        (PREVIOUS_CHECKED_LOOP, "torn-value-tail-of-words"),
        (PREVIOUS_CHECKED_LOOP, "name-with-a-dash"),
        (PREVIOUS_LOOP, "torn-value"),
        (PREVIOUS_LOOP, "name-with-a-dash"),
    ],
)
def test_the_dangerous_files_do_leak_through_the_previous_loop(previous, case, tmp_path):
    # Проверка самих файлов: если прежний цикл на них не течёт, тесты выше ничего
    # не доказывают.
    content, _, _ = REFUSED[case]

    got = _run(previous, content.encode(), tmp_path)

    assert got.code != 0
    assert SECRET in got.stderr


@pytest.mark.parametrize(
    "previous", [PREVIOUS_LOOP, PREVIOUS_CHECKED_LOOP], ids=["plain", "checked"]
)
def test_the_previous_loop_kept_what_systemd_drops(previous, tmp_path):
    # То же про расхождение с systemd: прежний цикл отдавал CR и кавычки дальше.
    got = _run(previous, b'TOKEN=abc123\r\nLABEL="Monday 10:00"\n', tmp_path)

    assert got.exported is not None
    assert got.exported["TOKEN"] == "abc123\r"
    assert got.exported["LABEL"] == '"Monday 10:00"'


def test_workflows_read_the_secrets_file_only_in_known_ways():
    unknown = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        allowed = OTHER_READERS | {LOADER_CALL}
        if path.name in AWAITING_REMOVAL:
            allowed = allowed | {LOOP_ENDS}
        for number, line in enumerate(path.read_text().split("\n"), start=1):
            text = line.strip()
            if ENV_FILE in text and not text.startswith("#") and text not in allowed:
                unknown.append(f"{path.name}:{number}")
    assert not unknown, (
        "файл секретов читается способом, которого нет в списке известных: "
        f"{unknown}. Перевести на {LOADER_NAME} или разобрать, что строка "
        "печатает в публичный журнал, и внести в OTHER_READERS"
    )


@pytest.mark.parametrize("name", sorted(AWAITING_REMOVAL))
def test_a_workflow_awaiting_removal_is_unchanged_and_runs_by_hand_only(name):
    path = WORKFLOWS / name
    if not path.exists():
        return  # удалён, как и собирались
    text = path.read_text()
    assert _readers(path) == [PREVIOUS_LOOP], (
        f"{name} изменён — значит, его не удаляют: перевести на {LOADER_NAME} "
        "и убрать из AWAITING_REMOVAL"
    )
    triggers = []
    inside = False
    for line in text.split("\n"):
        if line.startswith("on:"):
            inside = True
        elif inside and line[:1] not in ("", " ", "#"):
            break
        elif inside and line.startswith("  ") and not line.startswith("   "):
            triggers.append(line.strip().rstrip(":"))
    assert triggers == ["workflow_dispatch"], f"{name} запускается не только руками: {triggers}"
