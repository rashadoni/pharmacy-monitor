"""Health замечает `.env`, вернувшийся на сервер рядом с кодом.

Настройки сервера лежат в `/etc/pharmacy-monitor/env`. `.env` в корне
выложенного кода — второй источник: команды CLI добирают из него имена, которых
нет в окружении службы, API его не читает. 2026-10-09 такой файл нашёлся на
сервере — лежал с 6 мая. Выкладка его не возит, но и следить, не появился ли он
снова, было некому (docs/RUNBOOK.md «Откуда процесс берёт настройки»).

Проверка — `health._check_env_file_next_to_code`, идёт в ежечасном
`pharmacy-monitor health-check`. Тесты держат четыре вещи:

* сервер отличается от машины разработчика тем, что на нём есть файл настроек
  сервера; `.env` у разработчика — штатное место настроек и проблемой не считается.
  Как запущен процесс (таймер, руками), роли не играет;
* в отчёт попадает только путь: ни один из двух файлов не открывается, и ни
  размера, ни времени, ни владельца файла в сообщении нет;
* это предупреждение, а не авария, и пока файл лежит, письмо о нём уходит раз в
  сутки, а не каждый час;
* смотрит проверка на тот самый файл, который читает CLI, и на тот самый файл
  настроек, который юниты из репозитория называют своим `EnvironmentFile`.
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from src import health
from src import main as main_mod
from src._time import utcnow
from src.health import HealthReport, alert_signature, check_health
from src.storage import PriceSnapshot, Product, Run

REPO = Path(__file__).resolve().parents[1]
CODE = "env_file_next_to_code"

# Содержимое обоих файлов: если хоть что-то из этого окажется в отчёте, письме
# или выводе команды — проверка читает файл.
ENV_FILE_NAME = "PM_ENVTEST_STRAY_NAME"
ENV_FILE_VALUE = "pm-envtest-stray-value"
SERVER_NAME = "PM_ENVTEST_SERVER_NAME"
SERVER_VALUE = "pm-envtest-server-value"
FILE_CONTENT = (ENV_FILE_NAME, ENV_FILE_VALUE, SERVER_NAME, SERVER_VALUE)

# Что systemd кладёт в окружение процесса службы.
SYSTEMD_MARKERS = ("INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "NOTIFY_SOCKET")


@dataclass(frozen=True)
class Machine:
    """Машина, на которой исполняется код: каталог кода и место файла настроек."""

    beside_code: Path  # стоит на месте /opt/pharmacy-monitor/.env
    settings_file: Path  # стоит на месте /etc/pharmacy-monitor/env

    def make_it_a_server(self) -> None:
        self.settings_file.parent.mkdir(parents=True)
        self.settings_file.write_text(f"{SERVER_NAME}={SERVER_VALUE}\n")

    def put_env_file(self) -> None:
        self.beside_code.write_text(f"{ENV_FILE_NAME}={ENV_FILE_VALUE}\n")


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    root = tmp_path / "opt" / "pharmacy-monitor"
    root.mkdir(parents=True)
    made = Machine(
        beside_code=root / ".env",
        settings_file=tmp_path / "etc" / "pharmacy-monitor" / "env",
    )
    # Признак сервера для всех тестов уже снят в conftest (`_not_a_server`);
    # здесь оба пути переезжают во временный каталог.
    monkeypatch.setattr(health, "_CHECKOUT_ENV_FILE", made.beside_code)
    monkeypatch.setattr(health, "_SERVER_ENV_FILE", made.settings_file)
    return made


@pytest.fixture
def server(machine: Machine) -> Machine:
    machine.make_it_a_server()
    return machine


def _healthy_run(session) -> None:
    """Свежий подтверждённый полный сбор: без `.env` отчёт по такой базе — ok."""
    started = utcnow() - timedelta(hours=1)
    run = Run(
        started_at=started,
        finished_at=started + timedelta(minutes=1),
        status="ok",
        products_scraped=10,
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {
                "pharmonline": {"status": "ok"},
                "aptekonline": {"status": "ok"},
                "aloe": {"status": "ok"},
            },
        },
    )
    session.add(run)
    session.flush()
    for number in range(10):
        product = Product(
            site="pharmonline",
            external_id=f"p-{number}",
            url=f"http://x/{number}",
            name=f"P {number}",
            name_normalized=f"p {number}",
            last_seen_at=started,
        )
        session.add(product)
        session.flush()
        session.add(PriceSnapshot(run_id=run.id, product_id=product.id, price=10.0))
    session.commit()


# ── Что считается проблемой ────────────────────────────────────────────────────


def test_env_file_next_to_code_on_a_server_is_reported_with_its_path(server: Machine) -> None:
    server.put_env_file()

    issues = health._check_env_file_next_to_code()

    assert [(issue.severity, issue.code) for issue in issues] == [("warning", CODE)]
    assert str(server.beside_code) in issues[0].message
    assert issues[0].context == {"path": str(server.beside_code)}


def test_server_without_the_file_reports_nothing(server: Machine) -> None:
    assert health._check_env_file_next_to_code() == []


def test_env_file_in_a_developer_checkout_is_not_a_problem(machine: Machine) -> None:
    # Файла настроек сервера на машине нет — это не сервер, и `.env` в корне
    # чекаута здесь единственное и штатное место настроек.
    machine.put_env_file()

    assert health._check_env_file_next_to_code() == []


def test_settings_directory_without_the_file_is_not_a_server(machine: Machine) -> None:
    # Признак — сам файл настроек, а не каталог, в котором он должен лежать.
    machine.settings_file.parent.mkdir(parents=True)
    machine.put_env_file()

    assert health._check_env_file_next_to_code() == []


@pytest.mark.parametrize("under_systemd", [False, True])
def test_how_the_process_was_started_changes_nothing(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, under_systemd: bool
) -> None:
    # Сервер узнаётся по диску, а не по окружению процесса: ручной запуск на
    # сервере видит то же, что таймер, а служба пользователя на машине
    # разработчика сервером её не делает.
    for name in SYSTEMD_MARKERS:
        if under_systemd:
            monkeypatch.setenv(name, "8c1f0c0c5d3b4a6e9f1d2b3c4d5e6f70")
        else:
            monkeypatch.delenv(name, raising=False)
    machine.put_env_file()

    assert health._check_env_file_next_to_code() == []
    machine.make_it_a_server()
    found = health._check_env_file_next_to_code()
    assert [(issue.severity, issue.code) for issue in found] == [("warning", CODE)]


def test_the_file_coming_back_is_noticed_on_the_next_check(server: Machine) -> None:
    assert health._check_env_file_next_to_code() == []
    server.put_env_file()
    assert [issue.code for issue in health._check_env_file_next_to_code()] == [CODE]
    server.beside_code.unlink()
    assert health._check_env_file_next_to_code() == []


@pytest.mark.parametrize(
    "kind", ["empty file", "dangling symlink", "symlink to a file", "directory"]
)
def test_anything_named_env_file_counts(server: Machine, tmp_path: Path, kind: str) -> None:
    # Пустой файл завтра наполнят; ссылка в никуда сегодня не читается, а завтра
    # по ней появится файл.
    if kind == "empty file":
        server.beside_code.write_text("")
    elif kind == "directory":
        server.beside_code.mkdir()
    else:
        target = tmp_path / "elsewhere"
        if kind == "symlink to a file":
            target.write_text(f"{ENV_FILE_NAME}={ENV_FILE_VALUE}\n")
        server.beside_code.symlink_to(target)

    assert [issue.code for issue in health._check_env_file_next_to_code()] == [CODE]


class _Unreachable:
    """Путь, про который система не говорит «нет», а отказывает."""

    def __init__(self, error: OSError) -> None:
        self._error = error

    def lstat(self) -> os.stat_result:
        raise self._error

    def __str__(self) -> str:
        return "/unreachable"


def test_refusal_is_not_taken_for_absence(tmp_path: Path) -> None:
    # Проверка, которая на собственном сбое отвечает «файла нет», ничего не сторожит.
    assert health._is_there(_Unreachable(PermissionError(13, "Permission denied"))) is True
    assert health._is_there(_Unreachable(OSError(5, "Input/output error"))) is True
    assert health._is_there(tmp_path / "missing") is False
    (tmp_path / "file").write_text("")
    assert health._is_there(tmp_path / "file" / "below-a-file") is False


def test_server_whose_settings_directory_is_closed_is_still_a_server(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.put_env_file()
    closed = _Unreachable(PermissionError(13, "Permission denied"))
    monkeypatch.setattr(health, "_SERVER_ENV_FILE", closed)

    assert [issue.code for issue in health._check_env_file_next_to_code()] == [CODE]


# ── В отчёте только путь ───────────────────────────────────────────────────────


@pytest.fixture
def opened(server: Machine, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Сервер с `.env` рядом с кодом; сюда пишется каждое открытие любого из двух файлов.

    Открытие не запрещается, а записывается: запрет через исключение проглотил бы
    `except Exception` вокруг чтения, и тест остался бы зелёным, а на сервере
    чтение удалось бы.
    """
    server.put_env_file()
    watched = {str(server.beside_code), str(server.settings_file)}
    seen: list[str] = []

    def recording(original):
        def opener(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                path = os.path.abspath(os.fsdecode(file))
                if path in watched:
                    seen.append(path)
            return original(file, *args, **kwargs)

        return opener

    monkeypatch.setattr(builtins, "open", recording(builtins.open))
    monkeypatch.setattr(io, "open", recording(io.open))
    monkeypatch.setattr(os, "open", recording(os.open))
    return seen


def test_recorder_sees_every_way_of_opening(server: Machine, opened: list[str]) -> None:
    server.beside_code.read_text()
    with open(server.settings_file):
        pass
    os.close(os.open(server.beside_code, os.O_RDONLY))

    assert opened == [
        str(server.beside_code),
        str(server.settings_file),
        str(server.beside_code),
    ]


def test_message_says_where_the_file_is_and_nothing_else_about_it(server: Machine) -> None:
    server.put_env_file()

    told = health._check_env_file_next_to_code()[0]

    # Текст целиком: два пути и постоянные слова. Ни содержимого, ни размера, ни
    # времени, ни владельца, ни прав — ничему из этого в нём нет места.
    assert told.message == (
        f"Рядом с кодом лежит {server.beside_code} — на сервере его быть не должно: "
        f"настройки сервера живут в {server.settings_file}, а из .env команды CLI "
        "добирают имена, которых нет в окружении службы (API его не читает). "
        "Как проверить и убрать — docs/RUNBOOK.md «Откуда процесс берёт настройки»."
    )
    assert told.context == {"path": str(server.beside_code)}
    # И о других файлах на тех же местах сказано слово в слово то же.
    server.beside_code.write_text("")
    os.utime(server.beside_code, (0, 0))
    server.settings_file.write_text("OTHER_NAME=other-value\n" * 50)
    assert health._check_env_file_next_to_code() == [told]


def test_report_carries_the_path_and_nothing_from_either_file(
    server: Machine, opened: list[str], db_session
) -> None:
    report = check_health(db_session)

    issue = next(issue for issue in report.issues if issue.code == CODE)
    told = "\n".join(
        (
            issue.message,
            repr(issue.context),
            health.render_alert_html(report),
            alert_signature(report),
        )
    )
    assert opened == []
    assert str(server.beside_code) in issue.message
    assert str(server.beside_code) in health.render_alert_html(report)
    for secret in FILE_CONTENT:
        assert secret not in told


def test_health_check_command_prints_and_mails_the_path_only(
    server: Machine, opened: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'health.sqlite'}")
    mailed: list[dict] = []
    monkeypatch.setattr(
        main_mod.notifier, "send_email", lambda **kwargs: mailed.append(kwargs) or True
    )

    result = CliRunner().invoke(
        main_mod.cli,
        [
            "health-check",
            "--quiet-on-ok",
            "--alert-email",
            "--alert-state-file",
            str(tmp_path / "state.json"),
        ],
    )

    # 1 — warning: юнит health считает такой выход штатным (SuccessExitStatus=1 2).
    assert result.exit_code == 1, result.output
    assert opened == []
    assert f"[warning] {CODE}: " in result.output
    assert str(server.beside_code) in result.output
    assert [mail["subject"] for mail in mailed] == ["Pharmacy Monitor — WARNING"]
    assert str(server.beside_code) in mailed[0]["html_body"]
    for secret in FILE_CONTENT:
        assert secret not in result.output
        assert secret not in mailed[0]["html_body"]
        assert secret not in (tmp_path / "state.json").read_text()


# ── Предупреждение, а не авария; письмо раз в сутки ────────────────────────────


def test_it_is_a_warning_whatever_the_state_of_the_data(server: Machine, db_session) -> None:
    server.put_env_file()

    # Прогонов ещё нет: проверке они не нужны, и ранний выход `no_runs` её не глушит.
    empty = check_health(db_session)
    assert empty.status == "warning"
    assert [issue.code for issue in empty.issues] == [CODE, "no_runs"]

    # Данные в порядке: статус поднимает только сам файл — до warning, не выше.
    _healthy_run(db_session)
    report = check_health(db_session)
    assert [(issue.severity, issue.code) for issue in report.issues] == [("warning", CODE)]
    assert report.status == "warning"

    # Файл убрали — отчёт снова чистый.
    server.beside_code.unlink()
    assert check_health(db_session).status == "ok"


def test_healthy_developer_machine_stays_ok(machine: Machine, db_session) -> None:
    machine.put_env_file()
    _healthy_run(db_session)

    report = check_health(db_session)

    assert report.status == "ok"
    assert report.issues == []


def test_file_that_stays_is_mailed_once_a_day_not_every_hour(server: Machine, db_session) -> None:
    server.put_env_file()
    _healthy_run(db_session)
    found_at = datetime(2026, 10, 10, 12, 0)

    def decide(state: dict | None, now: datetime) -> health.HealthAlertDecision:
        return health.health_alert_decision(
            check_health(db_session), state, now=now, reminder_hours=24
        )

    first = decide(None, found_at)
    assert first.action == "incident"
    state = health.health_alert_state_after(first, None, now=found_at)

    for hours in (1, 2, 23):
        assert decide(state, found_at + timedelta(hours=hours)).action is None
    assert decide(state, found_at + timedelta(hours=24)).action == "reminder"

    # Файл убрали: одно письмо о восстановлении.
    server.beside_code.unlink()
    assert decide(state, found_at + timedelta(hours=25)).action == "recovery"


def test_path_is_not_part_of_the_incident_identity(server: Machine) -> None:
    # Подпись инцидента — код и важность. Путь в ней сделал бы «новым инцидентом»
    # тот же файл в каталоге с другим именем.
    server.put_env_file()
    report = HealthReport(status="warning", issues=health._check_env_file_next_to_code())

    assert alert_signature(report) == (
        '[{"code":"env_file_next_to_code","context":{},"severity":"warning"}]'
    )


# ── Проверка смотрит на те же файлы, что код и юниты ───────────────────────────

_PATHS_PROBE = """
import json
from src import health
print(json.dumps({"checkout": str(health._CHECKOUT_ENV_FILE), "server": str(health._SERVER_ENV_FILE)}))
"""


@pytest.fixture(scope="module")
def real_paths(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Оба пути, как их видит свежий процесс, запущенный из постороннего каталога.

    В самом тестовом процессе их смотреть нельзя: признак сервера снят в conftest,
    а cwd pytest — корень репозитория, и путь, собранный от cwd, совпал бы с
    верным случайно. Ручной запуск на сервере идёт из любого каталога.
    """
    result = subprocess.run(
        [sys.executable, "-c", _PATHS_PROBE],
        cwd=tmp_path_factory.mktemp("elsewhere"),
        env={**os.environ, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_it_watches_the_file_the_cli_reads(real_paths: dict[str, str]) -> None:
    # Путь, который `src/main.py` отдаёт загрузчику `.env`, вычисляется из его
    # же текста — и должен совпасть с тем, за которым следит health.
    main_path = REPO / "src" / "main.py"
    loader_calls = [
        node
        for node in ast.walk(ast.parse(main_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "load_dotenv"
    ]
    assert len(loader_calls) == 1
    argument = compile(ast.Expression(loader_calls[0].args[0]), str(main_path), "eval")
    loaded = eval(argument, {"Path": Path, "__file__": str(main_path)})

    assert real_paths["checkout"] == str(loaded) == str(REPO / ".env")


def test_server_is_recognised_by_the_file_the_units_are_fed(real_paths: dict[str, str]) -> None:
    named = {
        line.split("=", 1)[1].strip().lstrip("-")
        for unit in (REPO / "infra" / "systemd").rglob("*")
        if unit.is_file()
        for line in unit.read_text(encoding="utf-8").splitlines()
        if line.startswith("EnvironmentFile=")
    }

    assert named == {real_paths["server"]} == {"/etc/pharmacy-monitor/env"}


def test_rest_of_the_suite_runs_as_on_a_developer_machine(real_paths: dict[str, str]) -> None:
    # Тесты проверяют правило, а не машину: в тестовом процессе признак сервера
    # снят (conftest), иначе на машине с файлом настроек сервера и `.env` в корне
    # чекаута краснел бы любой тест, ждущий чистый отчёт. Сверка с настоящим
    # путём нужна, чтобы пропажа фикстуры была видна и там, где файла нет.
    assert str(health._SERVER_ENV_FILE) != real_paths["server"]
    assert not health._is_there(health._SERVER_ENV_FILE)
    assert health._check_env_file_next_to_code() == []
