"""Скрипт защиты `main` описывает то, что стоит на GitHub, и не врёт о результате.

2026-10-11 при разборе PR #83 нашлось: `scripts/ci/configure-main-protection.sh`
слал `"required_pull_request_reviews": null`, а на GitHub требование pull request
включено (ноль одобрений). Запись заменяет защиту целиком, null по описанию API
GitHub требование снимает — запуск открыл бы прямой пуш в `main` коммиту с
зелёными проверками. Итоговая печать этого не показала бы: поле
`pull_request_only` считалось по списку проверок и было бы `true`.

Запустить скрипт против настоящего репозитория для проверки нельзя: это и есть
смена защиты. Поэтому здесь:

- полезная нагрузка достаётся из текста скрипта и сверяется с ожидаемой целиком;
- она же сверяется с ответом GitHub на чтение защиты, записанным 2026-10-11
  (`tests/fixtures/main_protection_live_2026-10-11.json`), — двумя независимыми
  способами: сравнением самого скрипта (jq) и пересчётом на Python здесь;
- скрипт исполняется с подставным `gh` (`tests/fake_gh.py`): видно, какие
  запросы он шлёт, что без `--apply` он не пишет, что пишет только после сверки,
  кончившейся расхождением, или по ответу «ветка не защищена», и что падает,
  когда после записи на GitHub не то.

Чего тест НЕ доказывает: что GitHub примет эту запись и поведёт себя так, как
описано в его API. Подставной `gh` — модель и любую запись принимает. И снимок —
это 2026-10-11: о сегодняшнем состоянии GitHub говорит только `--check` самого
скрипта.

Настоящего `gh` в PATH этих тестов нет: там подставной и каталог со ссылкой на
`jq`. Не исполнится подставной — тест упадёт, а до настоящего не дойдёт. Запись
в тестах идёт только в `test-owner/test-repo`.

Если защиту меняют намеренно (по слову владельца): в PR правятся скрипт и
`EXPECTED`, а три теста со снимком (раздел «Снимок живой защиты») снимаются — он
перестаёт быть записью живого состояния. Остальные тесты снимка не читают: их
исходное состояние строится из `EXPECTED`; поправить придётся только случай
расхождения для самой изменённой настройки. Новый снимок записывают чтением после
`--apply` и возвращают те три теста следующим PR. Править снимок руками нельзя:
это единственное здесь, что пришло от GitHub.
"""

from __future__ import annotations

import copy
import json
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
PULL_REQUEST_RULE = {
    "dismiss_stale_reviews": False,
    "require_code_owner_reviews": False,
    "require_last_push_approval": False,
    "required_approving_review_count": 0,
}
EXPECTED = {
    "required_status_checks": {
        "strict": False,
        "checks": [{"context": name, "app_id": GITHUB_ACTIONS} for name in REQUIRED_CHECKS],
    },
    "enforce_admins": True,
    "required_pull_request_reviews": PULL_REQUEST_RULE,
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

GET, GET_PUT_GET = ["GET"], ["GET", "PUT", "GET"]


def _on(endpoint: str, methods: list[str]) -> list[tuple[str, str]]:
    return [(method, endpoint) for method in methods]


def _payload() -> dict:
    """JSON, который скрипт шлёт в PUT, — из текста скрипта, без запуска."""
    blocks = re.findall(r"<<'JSON'[^\n]*\n(.*?)\nJSON\n", SCRIPT.read_text(), flags=re.DOTALL)
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


def _in_sync(endpoint: str = TEST_ENDPOINT) -> dict:
    """Защита, какой её описывает скрипт, — в виде ответа GitHub на чтение."""
    return fake_gh.as_github_returns(copy.deepcopy(EXPECTED), endpoint)


def _drifted(drift) -> dict:
    state = _in_sync()
    drift(state)
    return state


def _without_pull_request_rule(state):
    del state["required_pull_request_reviews"]


def _admins_exempt(state):
    state["enforce_admins"]["enabled"] = False


def _lines(path: Path) -> list:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class _Run:
    def __init__(self, done: subprocess.CompletedProcess, home: Path):
        self.code = done.returncode
        self.out = done.stdout
        self.err = done.stderr
        self.argv = _lines(home / "argv.jsonl")
        self.calls = _lines(home / "calls.jsonl")
        state = home / "state.json"
        self.state = json.loads(state.read_text()) if state.exists() else None

    @property
    def requests(self) -> list[tuple[str, str]]:
        return [(call["method"], call["endpoint"]) for call in self.calls]

    @property
    def verdict(self) -> str:
        """Последняя строка сверки в stdout."""
        lines = [line for line in self.out.splitlines() if line.startswith("RESULT: ")]
        return lines[-1] if lines else ""

    @property
    def differs(self) -> list[str]:
        return [
            line.split()[1].rstrip(":")
            for line in self.out.splitlines()
            if line.startswith("DIFFERS ")
        ]


@pytest.fixture
def run(tmp_path):
    """Запустить скрипт с подставным `gh`. `state=None` — ветка не защищена."""
    tools = {}
    for tool in ("bash", "jq"):
        tools[tool] = shutil.which(tool)
        if tools[tool] is None:
            pytest.fail(f"тесту нужен {tool}: без него скрипт защиты не исполнить")

    home = tmp_path / "github"
    bin_dir = tmp_path / "bin"
    toolbox = tmp_path / "toolbox"
    empty = tmp_path / "empty"
    for directory in (home, bin_dir, toolbox, empty):
        directory.mkdir()
    (toolbox / "jq").symlink_to(tools["jq"])
    stand_in = bin_dir / "gh"
    stand_in.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(REPO / "tests/fake_gh.py"))} "$@"\n'
    )
    stand_in.chmod(0o755)
    path = f"{bin_dir}:{toolbox}"
    assert shutil.which("gh", path=path) == str(stand_in), "в PATH должен быть только подставной gh"

    def _run(
        *args: str,
        state: dict | None,
        put: str = "apply",
        get: str = "ok",
        error: str = "502",
        env: dict[str, str] | None = None,
        with_gh: bool = True,
    ) -> _Run:
        if state is not None:
            (home / "state.json").write_text(json.dumps(state))
        done = subprocess.run(
            [tools["bash"], str(SCRIPT), *args],
            env={
                "PATH": path if with_gh else str(toolbox),
                "HOME": str(empty),
                "GH_CONFIG_DIR": str(empty),
                "FAKE_GH_DIR": str(home),
                "FAKE_GH_PUT": put,
                "FAKE_GH_GET": get,
                "FAKE_GH_GET_ERROR": error,
                **(env or {}),
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        result = _Run(done, home)
        assert len(result.argv) == len(result.calls), (
            f"скрипт позвал gh так, как он им пользоваться не должен: {result.argv}"
        )
        return result

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


def test_required_checks_are_ci_jobs_that_run_on_every_pull_request():
    checks = _payload()["required_status_checks"]
    assert "contexts" not in checks, "старое поле: с ним источник проверки не задаётся"
    names = [check["context"] for check in checks["checks"]]
    assert "agent-review" not in names
    assert {check["app_id"] for check in checks["checks"]} == {GITHUB_ACTIONS}

    workflow = yaml.safe_load(WORKFLOW.read_text())
    jobs = {job["name"]: job for job in workflow["jobs"].values()}
    assert set(names) <= set(jobs), (
        "обязательной проверки нет среди джоб ci-pipeline.yml: она не придёт никогда, "
        "и ни один PR не смержится"
    )
    for name in names:
        assert not {"if", "needs"} & set(jobs[name]), (
            f"джоба «{name}» может быть пропущена, а пропущенную джобу GitHub считает зелёной"
        )
    # PyYAML читает ключ `on` как True.
    triggers = workflow[True]
    assert isinstance(triggers, dict) and "pull_request" in triggers
    trigger = triggers["pull_request"] or {}
    assert not {"paths", "paths-ignore", "branches-ignore", "types"} & set(trigger), (
        "обязательная проверка должна стартовать на каждом PR в main и на каждом его "
        "коммите: PR мимо фильтра будет ждать её вечно"
    )
    assert "main" in trigger.get("branches", ["main"])


def test_script_reaches_the_network_only_through_gh_and_writes_once():
    code = [line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in code if re.search(r"\b(curl|wget|nc)\b", line)]
    writes = [line for line in code if re.search(r"(-X|--method)\b", line)]
    assert len(writes) == 1 and "-X PUT" in writes[0]
    # Диапазоны в шаблоне имени репозитория — ASCII при любой локали вызывающего.
    assert code.index("export LC_ALL=C") < next(
        number for number, line in enumerate(code) if "REPO_SHAPE" in line
    )


# --- Снимок живой защиты от 2026-10-11 ---------------------------------------


def test_recorded_live_protection_is_what_the_payload_sets():
    assert _as_payload(_live()) == EXPECTED


def test_stand_in_answers_a_write_the_way_github_answered_a_read():
    assert fake_gh.as_github_returns(EXPECTED, OWN_ENDPOINT) == _live()


@pytest.mark.parametrize("args", [["--check"], []], ids=["--check", "no flag"])
def test_script_finds_the_recorded_live_protection_in_sync_and_sends_nothing(run, args):
    result = run(*args, state=_live())

    assert result.code == 0, result.out + result.err
    assert result.verdict == "RESULT: match"
    for line in (
        "pull request required  : true",
        "required approvals     : 0",
        "required checks        : Frontend (Next.js), Lint (ruff), Test (pytest)",
        "applies to admins      : true",
        "force pushes allowed   : false",
        "branch deletion allowed: false",
    ):
        assert line in result.out.splitlines()
    assert result.requests == _on(OWN_ENDPOINT, GET)


# --- Без --apply: только чтение ----------------------------------------------


@pytest.mark.parametrize("args", [["--check"], [], [TEST_REPO]], ids=["--check", "no flag", "repo"])
def test_nothing_is_written_without_apply_whatever_github_has(run, args):
    result = run(*args, state=_drifted(_without_pull_request_rule))

    assert result.code == 1
    assert "Nothing was sent." in result.out
    assert [method for method, _ in result.requests] == GET


def test_order_of_checks_does_not_matter(run):
    def reorder(state):
        state["required_status_checks"]["checks"].reverse()
        state["required_status_checks"]["contexts"].reverse()

    result = run("--check", TEST_REPO, state=_drifted(reorder))

    assert result.code == 0, result.out + result.err
    assert result.verdict == "RESULT: match"


def _drift_cases() -> list:
    cases = []

    def case(name, setting, drift, line=None):
        cases.append(pytest.param(drift, setting, line, id=name))

    def checks(state):
        return state["required_status_checks"]

    def reviews(state):
        return state["required_pull_request_reviews"]

    shown = {
        "allow_force_pushes": "force pushes allowed   : {}",
        "allow_deletions": "branch deletion allowed: {}",
    }
    for switch in fake_gh.SWITCHES:
        line = shown.get(switch)
        case(
            f"{switch}_on",
            switch,
            lambda s, k=switch: s[k].update(enabled=True),
            line and line.format("true"),
        )
        case(
            f"{switch}_not_returned",
            switch,
            lambda s, k=switch: s.pop(k),
            line and line.format("null"),
        )

    case("admins_exempt", "enforce_admins", _admins_exempt, "applies to admins      : false")
    case(
        "admins_not_returned",
        "enforce_admins",
        lambda s: s.pop("enforce_admins"),
        "applies to admins      : null",
    )

    rule = "required_pull_request_reviews"
    case("no_pull_request_rule", rule, _without_pull_request_rule, "pull request required  : false")
    case(
        "one_approval",
        rule,
        lambda s: reviews(s).update(required_approving_review_count=1),
        "required approvals     : 1",
    )
    case(
        "approvals_not_returned",
        rule,
        lambda s: reviews(s).pop("required_approving_review_count"),
        "required approvals     : null",
    )
    for flag in (
        "dismiss_stale_reviews",
        "require_code_owner_reviews",
        "require_last_push_approval",
    ):
        case(f"{flag}_on", rule, lambda s, k=flag: reviews(s).update({k: True}))
    allowed = {"users": [{"login": "someone"}], "teams": [], "apps": []}
    case(
        "bypass_allowed", rule, lambda s: reviews(s).update(bypass_pull_request_allowances=allowed)
    )
    case(
        "bypass_object_empty", rule, lambda s: reviews(s).update(bypass_pull_request_allowances={})
    )
    case("dismissal_restricted", rule, lambda s: reviews(s).update(dismissal_restrictions=allowed))

    status = "required_status_checks"

    def one_check_gone(state):
        checks(state)["contexts"].remove("Test (pytest)")
        checks(state)["checks"] = [
            check for check in checks(state)["checks"] if check["context"] != "Test (pytest)"
        ]

    def agent_review_back(state):
        checks(state)["contexts"].append("agent-review")
        checks(state)["checks"].append({"context": "agent-review", "app_id": None})

    def check_listed_twice(state):
        checks(state)["checks"].append(dict(checks(state)["checks"][0]))

    def check_named_like_a_verdict(state):
        checks(state)["checks"].append({"context": "x\nRESULT: match", "app_id": GITHUB_ACTIONS})

    case("no_status_checks", status, lambda s: s.pop(status), "required checks        : ")
    case(
        "one_check_gone",
        status,
        one_check_gone,
        "required checks        : Frontend (Next.js), Lint (ruff)",
    )
    case("agent_review_back", status, agent_review_back)
    case("check_listed_twice", status, check_listed_twice)
    case("check_named_like_a_verdict", status, check_named_like_a_verdict)
    case("check_from_any_source", status, lambda s: checks(s)["checks"][1].update(app_id=None))
    case("check_source_not_returned", status, lambda s: checks(s)["checks"][1].pop("app_id"))
    case("checks_not_returned", status, lambda s: checks(s).pop("checks"))
    case("strict", status, lambda s: checks(s).update(strict=True))
    case("strict_not_returned", status, lambda s: checks(s).pop("strict"))

    case("push_restricted", "restrictions", lambda s: s.update(restrictions=allowed))
    empty = {"users": [], "teams": [], "apps": []}
    case("push_restricted_to_nobody", "restrictions", lambda s: s.update(restrictions=empty))
    return cases


@pytest.mark.parametrize(("drift", "setting", "line"), _drift_cases())
def test_check_names_the_setting_that_drifted_and_sends_nothing(run, drift, setting, line):
    result = run("--check", TEST_REPO, state=_drifted(drift))

    assert result.code == 1, result.out + result.err
    assert result.out.splitlines()[-2] == "RESULT: MISMATCH"
    assert result.differs == [setting]
    if line is not None:
        assert line in result.out.splitlines()
    assert result.requests == _on(TEST_ENDPOINT, GET)


def test_check_says_which_side_has_what(run):
    result = run("--check", TEST_REPO, state=_drifted(_without_pull_request_rule))

    line = next(line for line in result.out.splitlines() if line.startswith("DIFFERS "))
    script_side, github_side = line.removeprefix(
        "DIFFERS required_pull_request_reviews: this script sets "
    ).split(", GitHub has ")
    assert json.loads(script_side) == PULL_REQUEST_RULE
    assert github_side == "null"


def test_check_reports_an_unprotected_branch(run):
    result = run("--check", TEST_REPO, state=None)

    assert result.code == 1
    assert "no branch protection at all" in result.out
    assert result.verdict == ""
    assert result.requests == _on(TEST_ENDPOINT, GET)


# Чтение не удалось или кончилось без вердикта: это не «не защищена» и не «расходится».
UNREADABLE = sorted(fake_gh.GET_ERRORS)


@pytest.mark.parametrize("error", UNREADABLE)
@pytest.mark.parametrize("flag", ["--check", "--apply"])
@pytest.mark.parametrize("drift", [lambda state: None, _admins_exempt], ids=["in sync", "drifted"])
def test_nothing_is_written_over_protection_that_could_not_be_read(run, flag, error, drift):
    state = _drifted(drift)

    result = run(flag, TEST_REPO, state=state, get="fail_first", error=error)

    assert result.code == 3, result.out + result.err
    assert "Nothing was sent." in result.err
    assert "no branch protection" not in result.out
    assert result.requests == _on(TEST_ENDPOINT, GET)
    assert result.state == state


def test_gh_is_required(run):
    result = run("--apply", TEST_REPO, state=_drifted(_admins_exempt), with_gh=False)

    assert result.code == 3
    assert "gh is required" in result.err
    assert result.argv == []


# --- Запись: только --apply --------------------------------------------------


def test_apply_sends_nothing_when_github_already_matches(run):
    result = run("--apply", TEST_REPO, state=_in_sync())

    assert result.code == 0, result.out + result.err
    assert "nothing sent" in result.out
    assert result.requests == _on(TEST_ENDPOINT, GET)


def test_apply_writes_the_payload_when_pull_request_rule_is_missing(run):
    result = run("--apply", TEST_REPO, state=_drifted(_without_pull_request_rule))

    assert result.code == 0, result.out + result.err
    assert result.requests == _on(TEST_ENDPOINT, GET_PUT_GET)
    assert json.loads(result.calls[1]["body"]) == EXPECTED
    assert result.verdict == "RESULT: match"
    assert result.state == _in_sync()


def test_apply_protects_an_unprotected_branch(run):
    result = run("--apply", TEST_REPO, state=None)

    assert result.code == 0, result.out + result.err
    assert result.requests == _on(TEST_ENDPOINT, GET_PUT_GET)
    assert json.loads(result.calls[1]["body"]) == EXPECTED
    assert result.verdict == "RESULT: match"


@pytest.mark.parametrize(
    ("put", "setting", "line"),
    [
        (
            "drop_pull_request_rule",
            "required_pull_request_reviews",
            "pull request required  : false",
        ),
        ("keep_admins_exempt", "enforce_admins", "applies to admins      : false"),
        ("unpin_checks", "required_status_checks", None),
    ],
)
def test_apply_fails_when_github_ends_up_in_another_state(run, put, setting, line):
    """Прежняя печать сказала бы здесь `pull_request_only: true` и вышла с нулём."""
    result = run("--apply", TEST_REPO, state=None, put=put)

    assert result.requests == _on(TEST_ENDPOINT, GET_PUT_GET)
    assert result.code == 1
    assert result.verdict == "RESULT: MISMATCH"
    assert result.differs == [setting]
    if line is not None:
        assert line in result.out.splitlines()
    assert "NOT in the state" in result.err


def test_apply_fails_when_the_branch_is_unprotected_after_an_accepted_write(run):
    result = run("--apply", TEST_REPO, state=None, put="accept_and_drop")

    assert result.requests == _on(TEST_ENDPOINT, GET_PUT_GET)
    assert result.code == 1
    assert result.out.count("no branch protection at all") == 2  # до записи и после
    assert "NOT in the state" in result.err


def test_apply_fails_when_github_refuses_the_write(run):
    state = _drifted(_without_pull_request_rule)

    result = run("--apply", TEST_REPO, state=state, put="reject")

    assert result.code == 3
    assert "Sent." not in result.out
    assert "ended in an error" in result.err
    # Что стоит на GitHub после несостоявшейся записи, скрипт читает, а не утверждает.
    assert result.requests == _on(TEST_ENDPOINT, GET_PUT_GET)
    assert "RESULT: MISMATCH" in result.err
    assert result.state == state


def test_apply_says_what_is_set_after_a_refused_write_to_an_unprotected_branch(run):
    result = run("--apply", TEST_REPO, state=None, put="reject")

    assert result.code == 3
    assert result.requests == _on(TEST_ENDPOINT, GET_PUT_GET)
    assert "no branch protection at all" in result.err
    assert "Could not read" not in result.err


def test_apply_does_not_claim_a_refusal_when_the_write_went_through(run):
    result = run(
        "--apply", TEST_REPO, state=_drifted(_without_pull_request_rule), put="error_after_apply"
    )

    assert result.code == 3
    assert "Nothing" not in result.err and "did not" not in result.err
    assert result.requests == _on(TEST_ENDPOINT, GET_PUT_GET)
    assert "RESULT: match" in result.err


def test_apply_admits_it_when_neither_the_write_nor_the_reading_after_it_worked(run):
    result = run(
        "--apply",
        TEST_REPO,
        state=_drifted(_without_pull_request_rule),
        put="reject",
        get="fail_after_put",
    )

    assert result.code == 3
    assert result.requests == _on(TEST_ENDPOINT, GET_PUT_GET)
    assert "Could not read" in result.err


@pytest.mark.parametrize("error", UNREADABLE)
def test_apply_fails_when_the_result_cannot_be_read_back(run, error):
    result = run(
        "--apply",
        TEST_REPO,
        state=_drifted(_without_pull_request_rule),
        get="fail_after_put",
        error=error,
    )

    assert result.requests == _on(TEST_ENDPOINT, GET_PUT_GET)
    assert result.code == 3
    assert "could not be read back" in result.err
    assert result.verdict == "RESULT: MISMATCH"  # сверка до записи; после неё сверки нет


FOREIGN = {
    "BRANCH": "release",
    "REPO": "other-owner/other-repo",
    "ENDPOINT": "repos/other-owner/other-repo/branches/release/protection",
    "MODE": "apply",
    "DESIRED": "{}",
    "COMPARE": "| empty",
    "GH": "false",
}


@pytest.mark.parametrize("flag", ["--check", "--apply"])
def test_nothing_about_the_target_comes_from_the_environment(run, flag):
    result = run(flag, TEST_REPO, state=_drifted(_without_pull_request_rule), env=FOREIGN)

    expected = GET_PUT_GET if flag == "--apply" else GET
    assert result.requests == _on(TEST_ENDPOINT, expected)
    if flag == "--apply":
        assert json.loads(result.calls[1]["body"]) == EXPECTED


def test_default_repository_and_mode_do_not_come_from_the_environment(run):
    result = run(state=_drifted(_without_pull_request_rule), env=FOREIGN)

    assert result.code == 1
    assert result.requests == _on(OWN_ENDPOINT, GET)


@pytest.mark.parametrize(
    "args",
    [
        ["--chek"],
        ["--checks"],
        ["--apply-now"],
        ["-h"],
        ["check"],
        ["apply"],
        ["—check"],
        [" --apply"],
        [""],
        ["owner"],
        ["/repo"],
        ["owner/"],
        ["owner/."],
        ["owner/.."],
        ["owner/re po"],
        ["{owner}/repo"],
        ["owner/repo/extra"],
        ["--apply", ""],
        ["--apply", "apply"],
        ["--apply", "--check"],
        ["--check", "--apply"],
        ["--apply", "--apply"],
        [TEST_REPO, "--apply"],
        [TEST_REPO, "--check"],
        ["--apply", TEST_REPO, "extra"],
    ],
    ids=lambda args: repr(" ".join(args)),
)
def test_unrecognised_arguments_stop_before_the_first_request(run, args):
    result = run(*args, state=_drifted(_without_pull_request_rule))

    assert result.code == 2
    assert result.argv == []
