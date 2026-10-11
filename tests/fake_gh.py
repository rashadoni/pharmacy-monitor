"""Подставной `gh` для tests/test_main_protection_script.py. В сеть не ходит.

Понимает ровно то, чем пользуется `scripts/ci/configure-main-protection.sh`:

    gh api [-X PUT] <путь> [-H <заголовок>] [--input -] [--jq <фильтр>]

Всё остальное — отказ с ненулевым кодом: если скрипт начнёт звать `gh` иначе,
тест упадёт, а не пройдёт мимо.

Состояние лежит в каталоге из `FAKE_GH_DIR`:

- `state.json` — что сейчас «стоит на GitHub» (ответ на чтение защиты). Файла
  нет — ветка не защищена, чтение отвечает 404, как у GitHub.
- `calls.jsonl` — журнал: по строке на каждый вызов, до его исполнения.

`FAKE_GH_PUT` задаёт, что делает запись:

- `apply` (по умолчанию) — принять и ответить на следующее чтение так, как
  ответил бы GitHub (`as_github_returns`);
- `drop_pull_request_rule` — принять, но остаться без требования pull request;
- `reject` — отказать с 422, ничего не меняя.

`--jq` исполняет настоящий `jq`: `gh` печатает строки без кавычек, поэтому `-r`.
У `gh` внутри другая реализация (gojq) — совпадение на фильтре скрипта
проверено отдельно, чтением живой защиты (см. описание PR).

`as_github_returns` — модель, а не GitHub. К действительности она привязана
одним: для полезной нагрузки скрипта она даёт ровно тот ответ, который GitHub
вернул на чтение 2026-10-11 (`tests/fixtures/main_protection_live_2026-10-11.json`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

API = "https://api.github.com/"
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
    # Подпись коммитов задаётся отдельным запросом, запись защиты её не несёт.
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


def main(argv: list[str]) -> int:
    home = Path(os.environ["FAKE_GH_DIR"])
    state_file = home / "state.json"

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

    body = sys.stdin.read() if reads_stdin else None
    with (home / "calls.jsonl").open("a") as log:
        call = {"method": method, "endpoint": endpoint, "jq": jq_filter, "body": body}
        log.write(json.dumps(call) + "\n")

    if method == "GET":
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
        elif effect != "apply":
            return _refuse(f"unknown FAKE_GH_PUT={effect}", 97)
        state_file.write_text(json.dumps(state))
        print(json.dumps(state))
        return 0

    return _refuse(f"unexpected method {method}", 98)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
