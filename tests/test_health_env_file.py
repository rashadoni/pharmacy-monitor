"""Health замечает `.env`, вернувшийся на сервер рядом с кодом.

Настройки сервера лежат в `/etc/pharmacy-monitor/env`. `.env` в корне
выложенного кода — второй источник: команды CLI добирают из него имена, которых
нет в окружении службы, API его не читает. 2026-10-09 такой файл нашёлся на
сервере — лежал с 6 мая. Выкладка его не возит, но и следить, не появился ли он
снова, было некому (docs/RUNBOOK.md «Откуда процесс берёт настройки»).

Проверка — `health._check_env_file_next_to_code`, идёт в ежечасном
`pharmacy-monitor health-check`. Тесты держат четыре вещи:

* сервер отличается от машины разработчика тем, что на нём есть файл настроек
  сервера; `.env` у разработчика — штатное место настроек и проблемой не считается;
* в отчёт попадает только путь: ни один из двух файлов не открывается;
* это предупреждение, а не авария, и пока файл лежит, письмо о нём уходит раз в
  сутки, а не каждый час;
* смотрит проверка на тот самый файл, который читает CLI, и на тот самый файл
  настроек, который юниты из репозитория называют своим `EnvironmentFile`.
"""

from __future__ import annotations

import ast
import builtins
import io
import os
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


def test_the_file_coming_back_is_noticed_on_the_next_check(server: Machine) -> None:
    assert health._check_env_file_next_to_code() == []
    server.put_env_file()
    assert [issue.code for issue in health._check_env_file_next_to_code()] == [CODE]
    server.beside_code.unlink()
    assert health._check_env_file_next_to_code() == []


@pytest.mark.parametrize("kind", ["dangling symlink", "symlink to a file", "directory"])
def test_anything_named_env_file_counts(server: Machine, tmp_path: Path, kind: str) -> None:
    # Ссылка в никуда сегодня не читается, а завтра по ней появится файл.
    if kind == "directory":
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
def no_reading(server: Machine, monkeypatch: pytest.MonkeyPatch) -> Machine:
    """Сервер с `.env` рядом с кодом; открыть любой из двух файлов — уронить тест."""
    server.put_env_file()
    guarded = {str(server.beside_code), str(server.settings_file)}

    def refuse(original):
        def opener(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                assert os.path.abspath(os.fsdecode(file)) not in guarded, f"opened {file}"
            return original(file, *args, **kwargs)

        return opener

    monkeypatch.setattr(builtins, "open", refuse(builtins.open))
    monkeypatch.setattr(io, "open", refuse(io.open))
    monkeypatch.setattr(os, "open", refuse(os.open))
    return server


def test_the_guard_against_reading_would_catch_a_read(no_reading: Machine) -> None:
    for read in (
        lambda: no_reading.beside_code.read_text(),
        lambda: open(no_reading.beside_code),
        lambda: os.open(no_reading.beside_code, os.O_RDONLY),
    ):
        with pytest.raises(AssertionError, match="opened"):
            read()


def test_report_carries_the_path_and_nothing_from_either_file(
    no_reading: Machine, db_session
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
    assert str(no_reading.beside_code) in issue.message
    assert str(no_reading.beside_code) in health.render_alert_html(report)
    for secret in FILE_CONTENT:
        assert secret not in told


def test_health_check_command_prints_and_mails_the_path_only(
    no_reading: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    assert f"[warning] {CODE}: " in result.output
    assert str(no_reading.beside_code) in result.output
    assert [mail["subject"] for mail in mailed] == ["Pharmacy Monitor — WARNING"]
    assert str(no_reading.beside_code) in mailed[0]["html_body"]
    for secret in FILE_CONTENT:
        assert secret not in result.output
        assert secret not in mailed[0]["html_body"]
        assert secret not in (tmp_path / "state.json").read_text()


# ── Предупреждение, а не авария; письмо раз в сутки ────────────────────────────


def test_it_is_a_warning_whatever_the_state_of_the_data(server: Machine, db_session) -> None:
    server.put_env_file()

    # Прогонов ещё нет: проверка от базы не зависит и раннему выходу не мешает.
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


def test_it_watches_the_file_the_cli_reads() -> None:
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

    assert health._CHECKOUT_ENV_FILE == loaded == REPO / ".env"


def test_server_is_recognised_by_the_file_the_units_are_fed() -> None:
    named = {
        line.split("=", 1)[1].strip().lstrip("-")
        for unit in (REPO / "infra" / "systemd").rglob("*")
        if unit.is_file()
        for line in unit.read_text(encoding="utf-8").splitlines()
        if line.startswith("EnvironmentFile=")
    }

    assert named == {str(health._SERVER_ENV_FILE)} == {"/etc/pharmacy-monitor/env"}
