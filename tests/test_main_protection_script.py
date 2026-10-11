"""Скрипт защиты `main` описывает то, что стоит на GitHub, и не врёт о результате.

2026-10-11 при разборе PR #83 нашлось: `scripts/ci/configure-main-protection.sh`
слал `"required_pull_request_reviews": null`, а на GitHub требование pull request
включено (ноль одобрений). Запись заменяет защиту целиком, null по описанию API
GitHub требование снимает — повторный запуск открыл бы прямой пуш в `main`
коммиту с зелёными проверками. Итоговая печать этого не показала бы: поле
`pull_request_only` считалось по списку проверок и было бы `true`.

Запустить скрипт против настоящего репозитория для проверки нельзя: это и есть
смена защиты. Поэтому здесь:

- полезная нагрузка достаётся из текста скрипта и сверяется с ожидаемой целиком;
- она же сверяется с ответом GitHub на чтение защиты, записанным 2026-10-11
  (`tests/fixtures/main_protection_live_2026-10-11.json`), — двумя независимыми
  способами: сравнением самого скрипта (jq) и пересчётом на Python здесь;
- скрипт исполняется с подставным `gh` (`tests/fake_gh.py`), который в сеть не
  ходит: видно, какие запросы он шлёт, и что он падает, когда после записи
  требования pull request на GitHub нет.

Чего тест НЕ доказывает: что GitHub примет эту запись и поведёт себя так, как
описано в его API. Подставной `gh` — модель. И снимок — это 2026-10-11: о
сегодняшнем состоянии GitHub говорит только `--check` самого скрипта.

Настоящий `gh` из этих тестов недостижим трижды: подставной стоит первым в PATH,
окружение собрано без токена и с пустым каталогом настроек, а запись идёт только
в `test-owner/test-repo`.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests import fake_gh

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/ci/configure-main-protection.sh"
WORKFLOW = REPO / ".github/workflows/ci-pipeline.yml"
LIVE_2026_10_11 = REPO / "tests/fixtures/main_protection_live_2026-10-11.json"

GITHUB_ACTIONS = 15368
REQUIRED_CHECKS = ["Lint (ruff)", "Test (pytest)", "Frontend (Next.js)"]
EXPECTED = {
    "required_status_checks": {
        "strict": False,
        "checks": [{"context": name, "app_id": GITHUB_ACTIONS} for name in REQUIRED_CHECKS],
    },
    "enforce_admins": True,
    "required_pull_request_reviews": {
        "dismiss_stale_reviews": False,
        "require_code_owner_reviews": False,
        "require_last_push_approval": False,
        "required_approving_review_count": 0,
    },
    "restrictions": None,
    "required_linear_history": False,
    "allow_force_pushes": False,
    "allow_deletions": False,
    "block_creations": False,
    "required_conversation_resolution": False,
    "lock_branch": False,
    "allow_fork_syncing": False,
}

OWN_ENDPOINT = "repos/rashadoni/pharmacy-monitor/branches/main/protection"
TEST_REPO = "test-owner/test-repo"
TEST_ENDPOINT = f"repos/{TEST_REPO}/branches/main/protection"


def _payload() -> dict:
    """JSON, который скрипт шлёт в PUT, — из текста скрипта, без запуска."""
    blocks = re.findall(r"<<'JSON'\n(.*?)\nJSON\n", SCRIPT.read_text(), flags=re.DOTALL)
    assert len(blocks) == 1, "в скрипте должна быть ровно одна полезная нагрузка"
    return json.loads(blocks[0])


def _live() -> dict:
    return json.loads(LIVE_2026_10_11.read_text())


def _as_payload(state: dict) -> dict:
    """Ответ GitHub на чтение защиты — в виде, в котором её задают. Без jq скрипта."""
    checks = state.get("required_status_checks")
    reviews = state.get("required_pull_request_reviews")
    return {
        "required_status_checks": checks
        and {
            "strict": checks["strict"],
            "checks": [{"context": c["context"], "app_id": c["app_id"]} for c in checks["checks"]],
        },
        "enforce_admins": state["enforce_admins"]["enabled"],
        "required_pull_request_reviews": reviews
        and {key: value for key, value in reviews.items() if key != "url"},
        "restrictions": state.get("restrictions"),
        **{name: state[name]["enabled"] for name in fake_gh.SWITCHES},
    }


class _Run:
    def __init__(self, done: subprocess.CompletedProcess, home: Path):
        self.code = done.returncode
        self.out = done.stdout
        self.err = done.stderr
        log = home / "calls.jsonl"
        self.calls = (
            [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        )
        state = home / "state.json"
        self.state = json.loads(state.read_text()) if state.exists() else None

    @property
    def methods(self) -> list[str]:
        return [call["method"] for call in self.calls]

    @property
    def verdict(self) -> str:
        """Последняя строка сверки — та, по которой скрипт решает, совпало ли."""
        lines = [line for line in self.out.splitlines() if line.startswith("RESULT: ")]
        return lines[-1] if lines else ""


@pytest.fixture
def run(tmp_path):
    """Запустить скрипт с подставным `gh`. `state=None` — ветка не защищена."""
    for tool in ("bash", "jq"):
        if shutil.which(tool) is None:
            pytest.fail(f"тесту нужен {tool}: без него скрипт защиты не исполнить")

    home = tmp_path / "github"
    bin_dir = tmp_path / "bin"
    empty = tmp_path / "empty"
    for directory in (home, bin_dir, empty):
        directory.mkdir()
    stand_in = bin_dir / "gh"
    stand_in.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(REPO / "tests/fake_gh.py"))} "$@"\n'
    )
    stand_in.chmod(0o755)

    def _run(*args: str, state: dict | None, put: str = "apply") -> _Run:
        if state is not None:
            (home / "state.json").write_text(json.dumps(state))
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(empty),
            "GH_CONFIG_DIR": str(empty),
            "FAKE_GH_DIR": str(home),
            "FAKE_GH_PUT": put,
        }
        done = subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return _Run(done, home)

    return _run


# --- Полезная нагрузка: чтением текста скрипта, без запуска ------------------


def test_payload_is_the_agreed_protection():
    assert _payload() == EXPECTED


def test_payload_requires_a_pull_request_and_no_approvals():
    reviews = _payload()["required_pull_request_reviews"]
    assert reviews is not None, (
        "null в required_pull_request_reviews снимает требование pull request: "
        "коммит с зелёными проверками можно будет запушить прямо в main"
    )
    assert reviews["required_approving_review_count"] == 0, (
        "владелец работает один: одобрение, которое некому дать, останавливает доставку"
    )
    assert "bypass_pull_request_allowances" not in reviews


def test_payload_applies_to_admins():
    assert _payload()["enforce_admins"] is True, (
        "решение владельца (2026-10-11): администратор обходить проверки не может"
    )


def test_required_checks_are_the_ci_jobs_reported_by_github_actions():
    checks = _payload()["required_status_checks"]
    assert "contexts" not in checks, "старое поле: с ним источник проверки не задаётся"
    names = [check["context"] for check in checks["checks"]]
    assert "agent-review" not in names
    assert {check["app_id"] for check in checks["checks"]} == {GITHUB_ACTIONS}

    workflow = yaml.safe_load(WORKFLOW.read_text())
    jobs = [job["name"] for job in workflow["jobs"].values()]
    assert sorted(names) == sorted(jobs), (
        "обязательная проверка, которой нет среди джоб ci-pipeline.yml, не придёт никогда — "
        "и ни один PR не смержится"
    )
    # PyYAML читает ключ `on` как True.
    trigger = workflow[True]["pull_request"]
    assert trigger == {"branches": ["main"]}, (
        "обязательная проверка должна стартовать на каждом PR в main: фильтр по путям "
        "оставит остальные PR ждать её вечно"
    )


# --- Снимок живой защиты от 2026-10-11 ---------------------------------------


def test_recorded_live_protection_is_what_the_payload_sets():
    assert _as_payload(_live()) == EXPECTED


def test_stand_in_answers_a_write_the_way_github_answered_a_read():
    assert fake_gh.as_github_returns(EXPECTED, OWN_ENDPOINT) == _live()


# --- Режим --check: только чтение --------------------------------------------


def test_check_finds_the_recorded_live_protection_in_sync_and_sends_nothing(run):
    result = run("--check", state=_live())

    assert result.code == 0, result.out + result.err
    assert result.verdict == "RESULT: match"
    assert "pull request required  : true" in result.out
    assert "required approvals     : 0" in result.out
    assert "applies to admins      : true" in result.out
    assert [(call["method"], call["endpoint"]) for call in result.calls] == [("GET", OWN_ENDPOINT)]


def _without_pull_request_rule(state):
    del state["required_pull_request_reviews"]


def _one_approval(state):
    state["required_pull_request_reviews"]["required_approving_review_count"] = 1


def _bypass_allowed(state):
    state["required_pull_request_reviews"]["bypass_pull_request_allowances"] = {
        "users": [{"login": "someone"}],
        "teams": [],
        "apps": [],
    }


def _admins_exempt(state):
    state["enforce_admins"]["enabled"] = False


def _without_status_checks(state):
    del state["required_status_checks"]


def _one_check_gone(state):
    checks = state["required_status_checks"]
    checks["contexts"].remove("Test (pytest)")
    checks["checks"] = [c for c in checks["checks"] if c["context"] != "Test (pytest)"]


def _agent_review_back(state):
    checks = state["required_status_checks"]
    checks["contexts"].append("agent-review")
    checks["checks"].append({"context": "agent-review", "app_id": None})


def _check_from_any_source(state):
    state["required_status_checks"]["checks"][1]["app_id"] = None


def _strict(state):
    state["required_status_checks"]["strict"] = True


def _force_push(state):
    state["allow_force_pushes"]["enabled"] = True


def _deletion(state):
    state["allow_deletions"]["enabled"] = True


def _push_restricted(state):
    state["restrictions"] = {"users": [], "teams": [], "apps": []}


def _setting_not_returned(state):
    del state["lock_branch"]


DRIFT = [
    (_without_pull_request_rule, "required_pull_request_reviews"),
    (_one_approval, "required_pull_request_reviews"),
    (_bypass_allowed, "required_pull_request_reviews"),
    (_admins_exempt, "enforce_admins"),
    (_without_status_checks, "required_status_checks"),
    (_one_check_gone, "required_status_checks"),
    (_agent_review_back, "required_status_checks"),
    (_check_from_any_source, "required_status_checks"),
    (_strict, "required_status_checks"),
    (_force_push, "allow_force_pushes"),
    (_deletion, "allow_deletions"),
    (_push_restricted, "restrictions"),
    (_setting_not_returned, "lock_branch"),
]


@pytest.mark.parametrize(("drift", "setting"), DRIFT, ids=[d.__name__.strip("_") for d, _ in DRIFT])
def test_check_names_the_setting_that_drifted_and_sends_nothing(run, drift, setting):
    state = copy.deepcopy(_live())
    drift(state)

    result = run("--check", TEST_REPO, state=state)

    assert result.code == 1, result.out + result.err
    assert result.verdict == "RESULT: MISMATCH"
    differs = [line.split()[1] for line in result.out.splitlines() if line.startswith("DIFFERS ")]
    assert differs == [f"{setting}:"]
    assert result.methods == ["GET"]


def test_check_says_out_loud_that_pull_request_is_not_required(run):
    state = copy.deepcopy(_live())
    _without_pull_request_rule(state)

    result = run("--check", TEST_REPO, state=state)

    assert "pull request required  : false" in result.out
    assert result.code == 1


def test_check_fails_when_protection_cannot_be_read(run):
    result = run("--check", TEST_REPO, state=None)

    assert result.code == 1
    assert "RESULT: match" not in result.out
    assert result.methods == ["GET"]


# --- Запись ------------------------------------------------------------------


def test_run_sends_nothing_when_github_already_matches(run):
    result = run(TEST_REPO, state=_live())

    assert result.code == 0, result.out + result.err
    assert "Nothing sent." in result.out
    assert result.methods == ["GET"]


def test_run_writes_the_payload_when_pull_request_rule_is_missing(run):
    state = copy.deepcopy(_live())
    _without_pull_request_rule(state)

    result = run(TEST_REPO, state=state)

    assert result.code == 0, result.out + result.err
    assert [(call["method"], call["endpoint"]) for call in result.calls] == [
        ("GET", TEST_ENDPOINT),
        ("PUT", TEST_ENDPOINT),
        ("GET", TEST_ENDPOINT),
    ]
    assert json.loads(result.calls[1]["body"]) == EXPECTED
    assert result.verdict == "RESULT: match"
    assert result.state == fake_gh.as_github_returns(EXPECTED, TEST_ENDPOINT)


def test_run_protects_an_unprotected_branch(run):
    result = run(TEST_REPO, state=None)

    assert result.code == 0, result.out + result.err
    assert result.methods == ["GET", "PUT", "GET"]
    assert json.loads(result.calls[1]["body"]) == EXPECTED
    assert result.verdict == "RESULT: match"


def test_run_fails_when_github_ends_up_without_the_pull_request_rule(run):
    """Прежняя печать сказала бы здесь `pull_request_only: true` и вышла с нулём."""
    state = copy.deepcopy(_live())
    _admins_exempt(state)

    result = run(TEST_REPO, state=state, put="drop_pull_request_rule")

    assert result.methods == ["GET", "PUT", "GET"]
    assert result.code != 0
    assert result.verdict == "RESULT: MISMATCH"
    assert "pull request required  : false" in result.out
    assert "NOT in the state" in result.err


def test_run_fails_when_github_refuses_the_write(run):
    state = copy.deepcopy(_live())
    _without_pull_request_rule(state)

    result = run(TEST_REPO, state=state, put="reject")

    assert result.methods == ["GET", "PUT"]
    assert result.code != 0
    assert "Read back" not in result.out
    assert result.state == state


@pytest.mark.parametrize(
    "args",
    [
        ["--chek"],
        ["--apply"],
        ["-h"],
        [TEST_REPO, "--check"],
        ["--check", "--check"],
        ["--check", TEST_REPO, "extra"],
        [TEST_REPO, "extra"],
    ],
    ids=" ".join,
)
def test_unrecognised_arguments_stop_before_the_first_request(run, args):
    result = run(*args, state=_live())

    assert result.code == 2
    assert result.calls == []
