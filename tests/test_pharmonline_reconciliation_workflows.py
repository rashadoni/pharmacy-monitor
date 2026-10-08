"""План и запись ручной сверки pharmonline идут одним транспортом — тем, что выбран в плане.

Зачем тест: транспорт `decodo` был прошит в обоих workflow, и при недоступном
Decodo сверку нельзя было провести вовсе. Теперь его выбирают при запуске плана
(`decodo` или `direct`), а workflow записи своего параметра не имеет и берёт
транспорт из одобренного плана. Ошибиться здесь значит либо записать через
`direct` то, что требует Decodo, либо прочитать каталог не тем путём, который
одобрен.

Покрываем (шаги берутся из самих файлов и исполняются стендом из
tests/test_prod_db_revision_gate.py):
- план: параметр `transport` — выбор из двух значений, по умолчанию `decodo`;
  незнакомое значение останавливает шаг до первого обращения к серверу
- план: скрипт сверки получает ровно выбранный транспорт; настройки Decodo
  передаются только с Decodo
- план: свидетельство с другим транспортом, чем выбран, не принимается; у плана
  через `direct` в свидетельстве не должно быть ни одного перехода личности
- запись: транспорт берётся из свидетельства плана и доходит и до скрипта
  записи, и до сбора, публикующего каталог; своего параметра у workflow нет
- запись через `direct` отказывает до базы и до бэкапа, пока выложенный код не
  знает допусков сверки напрямую (иначе ночной таймер падал бы до выкладки)
- цепочка целиком: свидетельство, которое оставил шаг плана, проходит проверку
  в workflow записи и приводит к записи тем же транспортом

Чего тест не видит: сам скрипт сверки (стенд подменяет его: он только
записывает, с каким окружением вызван, и пишет свидетельство — настоящий
исполняется в tests/test_pharmonline_direct_reconciliation.py), GitHub Actions
(выбор значения в форме запуска, передачу артефакта между прогонами), настоящий
ssh и то, что код на сервере читается именно таймером.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from src import main as main_mod
from tests.test_prod_db_revision_gate import (
    RECOVER,
    ROOT,
    WORKFLOWS,
    Sandbox,
    _log,
    _write_executable,
)

PLAN = "plan-pharmonline-decodo-public-api-reconciliation.yml"
PLAN_STEP = "Stage exact source and calculate the read-only reconciliation plan"
VERIFY_STEP = "Download and verify the exact plan manifest"
APPLY_STEP = "Stage source, back up production, reconcile, and recover catalog"
COMMIT = "c0ffee" + "0" * 34
MANUAL_DIRECT = main_mod._PHARMONLINE_PUBLIC_API_MANUAL_DIRECT_ADMISSION_PROOF_VERSION
# Счётчики плана, по которым workflow судит «в плане только простые допуски».
TRANSITION_METRICS = (
    "legacy_rekeys_ready",
    "native_id_url_rebind",
    "native_id_url_rebind_ready",
    "native_id_url_rebind_redirect_ready",
    "identity_splits_ready",
)
PLAIN_METRICS = {
    **dict.fromkeys(TRANSITION_METRICS, 0),
    "reconciliation_safe": 1,
    "new_public_product_admissions_ready": 212,
    "existing_native_admissions_ready": 3,
}
DECODO_SETTINGS = {"DECODO_SITES": "pharmonline", "PHARMONLINE_DECODO_BACKCONNECT_STICKY": "1"}

# `.venv/bin/python` сервера: записывает, с каким окружением вызваны скрипт
# сверки и сбор, за скрипт плана пишет свидетельство (транспорт — тот, что ему
# передан, как у настоящего), остальное отдаёт заглушке стенда.
VENV_PYTHON = r"""
import json
import os
import pathlib
import sys

args = sys.argv[1:]
root = pathlib.Path(os.environ["SBX_ROOT"])
kind = None
if args and args[0].endswith("scripts/reconcile_pharmonline_public_api_identities.py"):
    kind = "apply" if "--apply" in args[1:] else "plan"
elif args[:3] == ["-m", "src.main", "run"]:
    kind = "run"
if kind is not None:
    passed = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(("DECODO_", "PHARMONLINE_DECODO_"))
        or key == "PHARMONLINE_PUBLIC_API_TRANSPORT"
    }
    with open(root / "calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, "env": passed}) + "\n")
if kind == "plan":
    evidence = {
        "version": 3,
        "transport": os.environ["PHARMONLINE_PUBLIC_API_TRANSPORT"],
        "product_count": 9402,
        "catalog_fingerprint_sha256": "a" * 64,
        "candidate_manifest_sha256": "b" * 64,
        "metrics": json.loads((root / "plan-metrics.json").read_text()),
    }
    evidence.update(json.loads((root / "plan-evidence-override.json").read_text()))
    pathlib.Path(os.environ["PHARMONLINE_PUBLIC_API_PLAN_EVIDENCE_PATH"]).write_text(
        json.dumps(evidence)
    )
installed = root / "installed-package"
if installed.is_dir():
    # Пакет `src`, установленный в окружение сервера (`pip install -e .`):
    # его находит любой `import src`, если в названном каталоге кода нет.
    os.environ["PYTHONPATH"] = os.pathsep.join(
        filter(None, [os.environ.get("PYTHONPATH", ""), str(installed)])
    )
gate = str(pathlib.Path(__file__).with_name("python.gate"))
os.execv(gate, [gate, *args])
"""

RUNNER_STUBS = {
    # На раннере `python` есть; на dev-боксе в PATH бывает только python3.
    "python": r"""
import os
import sys

os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
""",
    "scp": r"""
import shutil
import sys

import _sbx

source, destination = sys.argv[-2:]
prefix = "pm@13.140.186.143:"
if not source.startswith(prefix):
    sys.exit(f"stub scp: unexpected source {source}")
shutil.copy(_sbx.to_sandbox(source[len(prefix) :]), destination)
""",
    # Артефакт плана: то, что оставил шаг плана в этом же стенде.
    "gh": r"""
import os
import pathlib
import shutil
import sys

args = sys.argv[1:]
if args[:2] != ["run", "download"]:
    sys.exit(f"stub gh: unexpected command {args[:2]}")
if args[args.index("--name") + 1] != "pharmonline-reconciliation-plan-evidence":
    sys.exit("stub gh: unexpected artifact name")
artifact = pathlib.Path(os.environ["SBX_ROOT"]) / "artifact"
shutil.copytree(artifact, args[args.index("--dir") + 1], dirs_exist_ok=True)
""",
}


class ReconciliationSandbox(Sandbox):
    """Стенд сверки: база на голове коммита, выбран транспорт, выложен какой-то код."""

    def __init__(self, root: Path, *, transport: str = "decodo"):
        super().__init__(root, db_state="at head")
        self.live = self.server / "opt" / "pharmacy-monitor"
        venv_python = self.live / ".venv" / "bin" / "python"
        venv_python.rename(venv_python.with_name("python.gate"))
        _write_executable(venv_python, VENV_PYTHON)
        for name, body in RUNNER_STUBS.items():
            _write_executable(self.bin / name, body)
        self.calls_file = root / "calls.jsonl"
        self.calls_file.write_text("", encoding="utf-8")
        self.context["inputs.transport"] = transport
        self.context["github.token"] = "sandbox-token"
        # Переменные раннера; значения `PLAN_*` пишет шаг проверки плана, а не стенд.
        self.github_env.write_text(
            f"GITHUB_SHA={COMMIT}\nGITHUB_REPOSITORY=rashadoni/pharmacy-monitor\n",
            encoding="utf-8",
        )
        self.plan_metrics(PLAIN_METRICS)

    def plan_metrics(self, metrics: dict, **evidence_override: object) -> None:
        (self.root / "plan-metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
        (self.root / "plan-evidence-override.json").write_text(
            json.dumps(evidence_override), encoding="utf-8"
        )

    @property
    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def env_value(self, name: str) -> str | None:
        """Последнее значение переменной в $GITHUB_ENV — его увидит следующий шаг."""
        value = None
        for line in self.github_env.read_text(encoding="utf-8").splitlines():
            key, _, candidate = line.partition("=")
            if key == name:
                value = candidate
        return value

    def approve(self, **evidence: object) -> None:
        """Одобренный план: то, что workflow записи скачает артефактом."""
        with self.github_env.open("a", encoding="utf-8") as handle:
            handle.write(
                f"PLAN_CATALOG_FINGERPRINT_SHA256={'a' * 64}\n"
                f"PLAN_CANDIDATE_MANIFEST_SHA256={'b' * 64}\n"
                "PLAN_PRODUCT_COUNT=9402\n"
            )
            for key, value in evidence.items():
                handle.write(f"{key}={value}\n")

    def publish_plan_artifact(self) -> Path:
        """Что сделал бы `upload-artifact`: каталог свидетельства шага плана."""
        evidence_dir = Path(self.env_value("PLAN_EVIDENCE_DIR") or "")
        assert evidence_dir.is_dir(), "шаг плана не оставил каталог свидетельства"
        artifact = self.root / "artifact"
        shutil.copytree(evidence_dir, artifact, dirs_exist_ok=True)
        return artifact

    def download(self, artifact: Path, **evidence_override: object) -> Path:
        """Артефакт плана, который отдаст `gh run download`; свидетельство можно поправить."""
        mine = self.root / "artifact"
        shutil.copytree(artifact, mine, dirs_exist_ok=True)
        path = mine / "reconciliation-plan.json"
        evidence = json.loads(path.read_text())
        evidence.update(evidence_override)
        path.write_text(json.dumps(evidence), encoding="utf-8")
        return path

    def deploy(self, code: str) -> None:
        """Код в живом каталоге сервера — тот, что исполняет ночной таймер."""
        deployed = self.live / "src"
        if code == "nothing":
            return
        if code == "nothing, but this commit is importable from elsewhere":
            # В живом каталоге кода нет, а `import src` всё равно находит код,
            # знающий допуски напрямую: спросить надо именно живой каталог.
            shutil.copytree(
                ROOT / "src",
                self.root / "installed-package" / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            return
        shutil.copytree(ROOT / "src", deployed, ignore=shutil.ignore_patterns("__pycache__"))
        if code == "this commit":
            return
        deployed_main = deployed / "main.py"
        if code == "before direct admissions":
            # Автодопуск уже выложен, сверки напрямую код ещё не знает.
            patch = f"\ndel _PHARMONLINE_PUBLIC_API_ADMISSION_RULES_BY_PROOF[{MANUAL_DIRECT!r}]\n"
        elif code == "before any admission rules":
            patch = "\ndel _PHARMONLINE_PUBLIC_API_ADMISSION_RULES_BY_PROOF\n"
        else:
            raise AssertionError(code)
        deployed_main.write_text(
            deployed_main.read_text(encoding="utf-8") + patch, encoding="utf-8"
        )


@pytest.fixture
def stand(tmp_path: Path) -> ReconciliationSandbox:
    assert shutil.which("rsync"), "стенду нужен rsync — без него шаги не исполнить"
    return ReconciliationSandbox(tmp_path)


@pytest.fixture(scope="module")
def plan_artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Артефакт настоящего шага плана для каждого транспорта — один на модуль:
    проверке плана в workflow записи нужен он, а не новый прогон плана."""
    artifacts = {}
    for transport in ("decodo", "direct"):
        planner = ReconciliationSandbox(
            tmp_path_factory.mktemp(f"plan-{transport}"), transport=transport
        )
        planned = planner.run_step(PLAN, PLAN_STEP)
        assert planned.returncode == 0, _log(planned)
        artifacts[transport] = planner.publish_plan_artifact()
    return artifacts


def _workflow_text(workflow: str) -> str:
    return (WORKFLOWS / workflow).read_text(encoding="utf-8")


def _workflow(workflow: str) -> dict:
    return yaml.safe_load(_workflow_text(workflow))


# ─── Что записано в самих файлах ─────────────────────────────────────────────


def test_transport_is_chosen_at_the_plan_and_nowhere_else():
    plan_inputs = _workflow(PLAN)[True]["workflow_dispatch"]["inputs"]
    assert plan_inputs["transport"]["type"] == "choice"
    assert plan_inputs["transport"]["options"] == ["decodo", "direct"]
    # Без явного выбора — прежний путь: только он доказывает смену адреса.
    assert plan_inputs["transport"]["default"] == "decodo"
    assert plan_inputs["transport"]["required"] is True
    # У записи своего транспорта нет: иначе его можно выбрать не таким, как в плане.
    assert set(_workflow(RECOVER)[True]["workflow_dispatch"]["inputs"]) == {
        "confirmation",
        "plan_run_id",
    }
    assert "inputs.transport" not in _workflow_text(RECOVER)


@pytest.mark.parametrize("workflow", [PLAN, RECOVER])
def test_no_command_carries_a_transport_of_its_own(workflow: str):
    """Транспорт назван только в двух ветках выбора; ни одна команда не получает
    его прошитым — так он и разошёлся бы с планом."""
    text = _workflow_text(workflow)
    assignments = re.findall(r"PHARMONLINE_PUBLIC_API_TRANSPORT=(\w+)", text)
    assert sorted(assignments) == ["decodo", "direct"], assignments
    for line in text.splitlines():
        if "PHARMONLINE_PUBLIC_API_TRANSPORT=" in line or "DECODO_SITES=" in line:
            # строка массива окружения, а не команды: без продолжения `\`
            assert not line.rstrip().endswith("\\"), line
    # Каждая команда, которая ходит на сайт, получает окружение выбранного транспорта.
    site_commands = len(re.findall(r"PHARMONLINE_PUBLIC_API=required", text))
    assert site_commands == text.count('env "${transport_env[@]}"') > 0


def test_apply_workflow_still_accepts_the_plan_workflow_by_its_name():
    """Файлы и имена workflow не переименованы намеренно: запись узнаёт план по
    имени, и расхождение останавливает её на первом шаге."""
    assert f'"{_workflow(PLAN)["name"]}"' in _workflow_text(RECOVER)


def test_workflows_judge_a_direct_plan_by_counters_the_plan_really_reports(db_session):
    """Счётчик, которого план не пишет, workflow прочёл бы как «переход есть» и
    отказывал бы каждому плану через `direct`."""
    from tests.test_pharmonline_scheduled_admission import _api, _catalog

    reported = main_mod._diagnose_pharmonline_public_api_reconciliation(
        db_session, _catalog(_api(1, "brand-new")), tenant_id=1
    )
    for key in (*TRANSITION_METRICS, "reconciliation_safe"):
        assert type(reported[key]) is int, key
    listed = re.compile(r"for key in \(\n((?:\s+\"[a-z_]+\",\n)+)\s+\):\n\s+if type\(metrics")
    for workflow in (PLAN, RECOVER):
        (block,) = listed.findall(_workflow_text(workflow))
        assert tuple(re.findall(r'"([a-z_]+)"', block)) == TRANSITION_METRICS, workflow


# ─── План ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("transport", ["decodo", "direct"])
def test_plan_passes_the_chosen_transport_to_the_script(tmp_path: Path, transport: str):
    stand = ReconciliationSandbox(tmp_path, transport=transport)

    result = stand.run_step(PLAN, PLAN_STEP)

    assert result.returncode == 0, _log(result)
    (call,) = stand.calls
    assert call["kind"] == "plan"
    assert call["env"].pop("PHARMONLINE_PUBLIC_API_TRANSPORT") == transport
    # Настройки Decodo — только с Decodo: план напрямую от него не зависит.
    assert call["env"] == (DECODO_SETTINGS if transport == "decodo" else {})
    evidence = json.loads((stand.publish_plan_artifact() / "reconciliation-plan.json").read_text())
    assert evidence["transport"] == transport
    assert f"transport={transport}" in result.stdout


@pytest.mark.parametrize("transport", ["", "firecrawl", "Direct", "decodo' 'direct"])
def test_plan_refuses_an_unlisted_transport_before_reaching_the_server(
    tmp_path: Path, transport: str
):
    stand = ReconciliationSandbox(tmp_path, transport=transport)

    result = stand.run_step(PLAN, PLAN_STEP)

    assert result.returncode != 0, _log(result)
    assert "transport must be decodo or direct" in result.stderr, _log(result)
    assert stand.calls == []
    assert stand.events == []
    assert stand.env_value("REMOTE_WORK_DIR") is None


@pytest.mark.parametrize(("chosen", "reported"), [("direct", "decodo"), ("decodo", "direct")])
def test_plan_refuses_evidence_made_with_another_transport(
    tmp_path: Path, chosen: str, reported: str
):
    stand = ReconciliationSandbox(tmp_path, transport=chosen)
    stand.plan_metrics(PLAIN_METRICS, transport=reported)

    result = stand.run_step(PLAN, PLAN_STEP)

    assert result.returncode != 0, _log(result)
    assert "reconciliation plan evidence transport/version is invalid" in result.stderr


# Каждый счётчик с единицей; на одном — что отказ даёт и отсутствие счётчика, и
# не число (True в Python равен единице, "0" — не ноль).
TRANSITION_CASES = [(key, 1) for key in TRANSITION_METRICS] + [
    ("identity_splits_ready", None),
    ("legacy_rekeys_ready", True),
    ("native_id_url_rebind", "0"),
]


# Целый шаг плана на случай — дорого: здесь по одному случаю каждого рода, все
# счётчики по отдельности проверяет шаг записи (тот же список — см. тест выше).
@pytest.mark.parametrize(("key", "value"), TRANSITION_CASES[-4:])
def test_direct_plan_with_an_identity_transition_is_refused(tmp_path: Path, key: str, value):
    """Скрипт отказывает такому плану сам; здесь вторая линия — по свидетельству.
    Счётчика нет или он не число — тоже отказ."""
    metrics = dict(PLAIN_METRICS)
    if value is None:
        del metrics[key]
    else:
        metrics[key] = value
    stand = ReconciliationSandbox(tmp_path, transport="direct")
    stand.plan_metrics(metrics)

    result = stand.run_step(PLAN, PLAN_STEP)

    assert result.returncode != 0, _log(result)
    assert f"direct reconciliation plan has an identity transition: {key}" in result.stderr


def test_plan_through_decodo_may_carry_identity_transitions(tmp_path: Path):
    stand = ReconciliationSandbox(tmp_path, transport="decodo")
    stand.plan_metrics({**PLAIN_METRICS, **dict.fromkeys(TRANSITION_METRICS, 2)})

    result = stand.run_step(PLAN, PLAN_STEP)

    assert result.returncode == 0, _log(result)


# ─── Запись: транспорт из одобренного плана ──────────────────────────────────


@pytest.mark.parametrize("transport", ["", "firecrawl", None, 1])
def test_apply_refuses_a_plan_with_an_unlisted_transport(
    stand: ReconciliationSandbox, plan_artifacts: dict[str, Path], transport
):
    stand.download(plan_artifacts["decodo"], transport=transport)

    result = stand.run_step(RECOVER, VERIFY_STEP)

    assert result.returncode != 0, _log(result)
    assert "approved reconciliation plan transport is invalid" in result.stderr, _log(result)
    assert stand.env_value("PLAN_TRANSPORT") is None


@pytest.mark.parametrize(("key", "value"), TRANSITION_CASES)
def test_apply_refuses_a_direct_plan_with_an_identity_transition(
    stand: ReconciliationSandbox, plan_artifacts: dict[str, Path], key: str, value
):
    # Свидетельство поправлено уже после плана: сам план такое не оставил бы.
    path = stand.download(plan_artifacts["direct"])
    evidence = json.loads(path.read_text())
    if value is None:
        del evidence["metrics"][key]
    else:
        evidence["metrics"][key] = value
    path.write_text(json.dumps(evidence), encoding="utf-8")

    result = stand.run_step(RECOVER, VERIFY_STEP)

    assert result.returncode != 0, _log(result)
    assert f"approved direct reconciliation plan has an identity transition: {key}" in result.stderr
    assert stand.env_value("PLAN_TRANSPORT") is None


@pytest.mark.parametrize("transport", ["decodo", "direct"])
def test_apply_takes_the_transport_from_the_approved_plan(
    stand: ReconciliationSandbox, plan_artifacts: dict[str, Path], transport: str
):
    stand.download(plan_artifacts[transport])

    result = stand.run_step(RECOVER, VERIFY_STEP)

    assert result.returncode == 0, _log(result)
    assert stand.env_value("PLAN_TRANSPORT") == transport
    assert f"transport={transport}" in result.stdout


def test_apply_lets_a_decodo_plan_carry_identity_transitions(
    stand: ReconciliationSandbox, plan_artifacts: dict[str, Path]
):
    path = stand.download(plan_artifacts["decodo"])
    evidence = json.loads(path.read_text())
    evidence["metrics"].update(dict.fromkeys(TRANSITION_METRICS, 2))
    path.write_text(json.dumps(evidence), encoding="utf-8")

    result = stand.run_step(RECOVER, VERIFY_STEP)

    assert result.returncode == 0, _log(result)
    assert stand.env_value("PLAN_TRANSPORT") == "decodo"


@pytest.mark.parametrize("transport", ["", "firecrawl"])
def test_apply_step_refuses_an_unlisted_transport_before_any_production_command(
    stand: ReconciliationSandbox, transport: str
):
    """Шаг записи сверяет транспорт и сам: значение пришло из другого шага."""
    stand.approve(PLAN_TRANSPORT=transport)
    before = stand.db_fingerprint()

    result = stand.run_step(RECOVER, APPLY_STEP)

    assert result.returncode != 0, _log(result)
    assert "invalid approved plan transport" in result.stderr, _log(result)
    assert stand.calls == []
    assert stand.events == []
    assert stand.db_fingerprint() == before


@pytest.mark.parametrize("transport", ["decodo", "direct"])
def test_plan_and_apply_go_through_the_same_transport(tmp_path: Path, transport: str):
    """Цепочка целиком: шаг плана → его артефакт → проверка в workflow записи →
    запись и сбор тем же транспортом."""
    stand = ReconciliationSandbox(tmp_path, transport=transport)
    stand.deploy("this commit" if transport == "direct" else "nothing")
    planned = stand.run_step(PLAN, PLAN_STEP)
    assert planned.returncode == 0, _log(planned)
    stand.publish_plan_artifact()

    verified = stand.run_step(RECOVER, VERIFY_STEP)

    assert verified.returncode == 0, _log(verified)
    assert stand.env_value("PLAN_TRANSPORT") == transport
    assert f"transport={transport}" in verified.stdout

    applied = stand.run_step(RECOVER, APPLY_STEP)

    # Стенд заканчивает сценарий на первой публикации каталога.
    assert "sandbox: the scenario ends at the first catalog publication" in applied.stderr, _log(
        applied
    )
    order = [kind for kind in stand.events if kind != "sleep"]
    assert order == [
        "script",  # план
        "alembic current",
        "alembic heads",
        "backup",
        "script --apply",
        "cli run",
        "cli run",
        "cli run",
    ], _log(applied)
    assert [call["kind"] for call in stand.calls] == ["plan", "apply", "run", "run", "run"]
    for call in stand.calls:
        assert call["env"].pop("PHARMONLINE_PUBLIC_API_TRANSPORT") == transport, call
        assert call["env"] == (DECODO_SETTINGS if transport == "decodo" else {}), call
    # Сторож выложенного кода спрашивает только при записи напрямую.
    assert ("deployed code recognises direct admissions" in applied.stdout) == (
        transport == "direct"
    )


@pytest.mark.parametrize(
    "deployed",
    [
        "nothing",
        "nothing, but this commit is importable from elsewhere",
        "before direct admissions",
        "before any admission rules",
    ],
)
def test_direct_apply_refuses_until_the_deployed_code_knows_direct_admissions(
    stand: ReconciliationSandbox, deployed: str
):
    """Допуски сверки напрямую код до этой правки не признаёт: ночной таймер
    (он исполняет выложенный код, а не чекаут workflow) падал бы каждую ночь."""
    stand.deploy(deployed)
    stand.approve(PLAN_TRANSPORT="direct")
    before = stand.db_fingerprint()

    result = stand.run_step(RECOVER, APPLY_STEP)

    assert result.returncode != 0, _log(result)
    assert (
        "refusing direct reconciliation: the deployed code does not recognise direct admissions"
        in result.stderr
    ), _log(result)
    assert "deploy this commit with deploy.yml first" in result.stderr
    # Отказ раньше всего остального: ни сверки ревизии, ни бэкапа, ни записи.
    assert stand.events == []
    assert stand.calls == []
    assert stand.db_fingerprint() == before


def test_apply_through_decodo_does_not_ask_the_deployed_code(stand: ReconciliationSandbox):
    """Через Decodo пишется прежняя версия доказательства — её знает любой
    выложенный код, и сверка не должна зависеть от того, что лежит на сервере."""
    stand.deploy("before direct admissions")
    stand.approve(PLAN_TRANSPORT="decodo")

    result = stand.run_step(RECOVER, APPLY_STEP)

    assert "script --apply" in stand.events, _log(result)
    assert "deployed code" not in result.stdout + result.stderr
