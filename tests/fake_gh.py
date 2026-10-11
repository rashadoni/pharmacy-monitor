"""Подставной `gh` для tests/test_main_protection_script.py. В сеть не ходит.

Понимает ровно то, чем пользуется `scripts/ci/configure-main-protection.sh`:

    gh api [-X PUT] <путь> [-H <заголовок>] [--input -] [--jq <фильтр>]

Всё остальное — отказ с ненулевым кодом. Скрипт может такой отказ проглотить
(`|| true`), поэтому в журнал пишется каждый запуск, ещё до разбора аргументов.

Состояние лежит в каталоге из `FAKE_GH_DIR`:

- `state.json` — что сейчас «стоит на GitHub» (ответ на чтение защиты). Файла
  нет — ветка не защищена, чтение отвечает 404, как у GitHub.
- `argv.jsonl` — аргументы каждого запуска, как пришли.
- `calls.jsonl` — запуски, которые удалось разобрать: метод, путь, фильтр, тело.

`FAKE_GH_GET` — когда чтение не удаётся: `ok` (никогда, по умолчанию), `fail`
(всегда), `fail_first` (только первое), `fail_after_put` (те, что идут после
записи). `FAKE_GH_GET_ERROR` — чем именно оно не удаётся (`GET_ERRORS`): отказом
с телом ответа, как у настоящего `gh`, или «успехом» без вердикта.

`FAKE_GH_PUT` — что делает запись:

- `apply` (по умолчанию) — принять и отвечать на чтение так, как ответил бы
  GitHub (`as_github_returns`);
- `drop_pull_request_rule` — принять, но остаться без требования pull request;
- `keep_admins_exempt` — принять, но правила на администратора не действуют;
- `unpin_checks` — принять, но проверки засчитываются из любого источника;
- `reject` — отказать с 422, ничего не меняя;
- `error_after_apply` — применить и после этого ответить ошибкой (обрыв связи
  на ответе).

`--jq` исполняет настоящий `jq`: `gh` печатает строки без кавычек, поэтому `-r`.
У `gh` внутри другая реализация (gojq). На фильтре скрипта они сверены вручную
2026-10-11, gh 2.45.0, чтением живой защиты: и совпадение, и расхождение дают тот
же вердикт и те же ключи в строках DIFFERS; отличается только порядок ключей
внутри значения в этих строках. Тест этого не повторяет — повторяет каждый
настоящий `--check`.

Как ведёт себя настоящий `gh` на ошибке, сверено там же: тело ответа уходит в
stdout без фильтра, `gh: … (HTTP 404)` — в stderr, код 1.

`as_github_returns` — модель, а не GitHub: принимает любую запись и не знает,
примет ли GitHub эту. К действительности она привязана одним: для полезной
нагрузки скрипта даёт ровно тот ответ, который GitHub вернул на чтение
2026-10-11 (`tests/fixtures/main_protection_live_2026-10-11.json`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

API = "https://api.github.com/"
# Чем кончается неудавшееся чтение: (код выхода, stdout, stderr). Настоящий `gh`
# на отказе печатает тело ответа в stdout — скрипт по нему отличает незащищённую
# ветку от всего остального, поэтому тела здесь разные и похожие на правду.
GET_ERRORS = {
    "502": (1, "<html><body><h1>502 Bad Gateway</h1></body></html>", "gh: HTTP 502"),
    "403": (
        1,
        json.dumps({"message": "API rate limit exceeded for user ID 1.", "status": "403"}),
        "gh: API rate limit exceeded for user ID 1. (HTTP 403)",
    ),
    "404_not_found": (
        1,
        json.dumps({"message": "Not Found", "status": "404"}),
        "gh: Not Found (HTTP 404)",
    ),
    "404_branch_not_found": (
        1,
        json.dumps({"message": "Branch not found", "status": "404"}),
        "gh: Branch not found (HTTP 404)",
    ),
    # Страница ошибки, которая цитирует те самые слова, но ответом GitHub не является.
    "503_quoting": (
        1,
        '<html><body>upstream said: {"message": "Branch not protected"}</body></html>',
        "gh: HTTP 503",
    ),
    # Код 0, а сверки нет.
    "empty": (0, "", ""),
    "partial": (0, "pull request required  : true\nrequired approvals     : 0", ""),
    "lookalike": (0, "RESULT: match?", ""),
}

SWITCHES = (
    "required_linear_history",
    "allow_force_pushes",
    "allow_deletions",
    "block_creations",
    "required_conversation_resolution",
    "lock_branch",
    "allow_fork_syncing",
)


def as_github_returns(payload: dict, endpoint: str) -> dict:
    """Ответ GitHub на чтение защиты после записи `payload` в `endpoint`."""
    url = API + endpoint
    state: dict = {"url": url}
    checks = payload["required_status_checks"]
    if checks is not None:
        state["required_status_checks"] = {
            "url": f"{url}/required_status_checks",
            "strict": checks["strict"],
            "contexts": [check["context"] for check in checks["checks"]],
            "contexts_url": f"{url}/required_status_checks/contexts",
            "checks": [
                {"context": check["context"], "app_id": check["app_id"]}
                for check in checks["checks"]
            ],
        }
    reviews = payload["required_pull_request_reviews"]
    if reviews is not None:
        state["required_pull_request_reviews"] = {
            "url": f"{url}/required_pull_request_reviews",
            **reviews,
        }
    if payload["restrictions"] is not None:
        state["restrictions"] = {"url": f"{url}/restrictions", **payload["restrictions"]}
    # Подпись коммитов задаётся отдельным запросом. Здесь она всегда выключена:
    # так на GitHub сейчас, и скрипт её не читает.
    state["required_signatures"] = {"url": f"{url}/required_signatures", "enabled": False}
    state["enforce_admins"] = {
        "url": f"{url}/enforce_admins",
        "enabled": payload["enforce_admins"],
    }
    for name in SWITCHES:
        state[name] = {"enabled": payload[name]}
    return state


def _refuse(message: str, code: int) -> int:
    print(f"fake gh: {message}", file=sys.stderr)
    return code


def _fail(kind: str) -> int:
    code, out, err = GET_ERRORS[kind]
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    return code


def main(argv: list[str]) -> int:
    home = Path(os.environ["FAKE_GH_DIR"])
    state_file = home / "state.json"
    journal = home / "calls.jsonl"
    with (home / "argv.jsonl").open("a") as raw:
        raw.write(json.dumps(argv) + "\n")

    if not argv or argv[0] != "api":
        return _refuse(f"only `gh api` is supported, got {argv!r}", 97)

    method, endpoint, jq_filter, reads_stdin = "GET", None, None, False
    args = iter(argv[1:])
    for arg in args:
        if arg in ("-X", "--method"):
            method = next(args)
        elif arg in ("-H", "--header"):
            next(args)
        elif arg == "--input":
            if next(args) != "-":
                return _refuse("--input is supported from stdin only", 97)
            reads_stdin = True
        elif arg == "--jq":
            jq_filter = next(args)
        elif arg.startswith("-"):
            return _refuse(f"unsupported flag {arg}", 97)
        elif endpoint is None:
            endpoint = arg
        else:
            return _refuse(f"unexpected argument {arg!r}", 97)
    if endpoint is None:
        return _refuse("no endpoint", 97)

    earlier = (
        [json.loads(line)["method"] for line in journal.read_text().splitlines()]
        if journal.exists()
        else []
    )
    body = sys.stdin.read() if reads_stdin else None
    with journal.open("a") as log:
        call = {"method": method, "endpoint": endpoint, "jq": jq_filter, "body": body}
        log.write(json.dumps(call) + "\n")

    if method == "GET":
        reading = os.environ.get("FAKE_GH_GET", "ok")
        if reading not in ("ok", "fail", "fail_first", "fail_after_put"):
            return _refuse(f"unknown FAKE_GH_GET={reading}", 97)
        if (
            reading == "fail"
            or (reading == "fail_first" and "GET" not in earlier)
            or (reading == "fail_after_put" and "PUT" in earlier)
        ):
            return _fail(os.environ.get("FAKE_GH_GET_ERROR", "502"))
        if not state_file.exists():
            print(json.dumps({"message": "Branch not protected", "status": "404"}))
            print("gh: Branch not protected (HTTP 404)", file=sys.stderr)
            return 1
        if jq_filter is None:
            print(state_file.read_text())
            return 0
        done = subprocess.run(["jq", "-r", jq_filter, str(state_file)], check=False)
        return 1 if done.returncode else 0

    if method == "PUT":
        effect = os.environ.get("FAKE_GH_PUT", "apply")
        if effect == "reject":
            print(json.dumps({"message": "Invalid request.", "status": "422"}))
            print("gh: Invalid request. (HTTP 422)", file=sys.stderr)
            return 1
        state = as_github_returns(json.loads(body or ""), endpoint)
        if effect == "drop_pull_request_rule":
            state.pop("required_pull_request_reviews", None)
        elif effect == "keep_admins_exempt":
            state["enforce_admins"]["enabled"] = False
        elif effect == "unpin_checks":
            for check in state["required_status_checks"]["checks"]:
                check["app_id"] = None
        elif effect not in ("apply", "error_after_apply"):
            return _refuse(f"unknown FAKE_GH_PUT={effect}", 97)
        state_file.write_text(json.dumps(state))
        if effect == "error_after_apply":
            return _fail("502")
        print(json.dumps(state))
        return 0

    return _refuse(f"unexpected method {method}", 98)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
