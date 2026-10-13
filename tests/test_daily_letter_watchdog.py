"""Сторож утреннего письма: запись об исходе рассылки и её проверка в health.

Письмо с разницей цен клиент ждёт каждое утро, а его команда о своём сбое
никого не оповещает. Настоящая рассылка оставляет запись в файле
(`src.daily_letter_state`), часовой `health-check` её сверяет и шлёт админу то
же письмо о проблемах, что и об остальном. Здесь проверяется:

- кто пишет запись (настоящая рассылка) и кто нет (`--dry-run`, `--only`);
- что сбой записи письмо не роняет;
- когда health поднимает тревогу и когда молчит;
- что тревога доходит до письма админу тем же путём, что соседние.

Файл состояния у каждого теста свой — фикстура в tests/conftest.py.
"""

from __future__ import annotations

import builtins
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import api as api_module
from src import (
    comparison_rows,
    daily_letter_state,
    health,
    notifications,
    notifier,
    storage,
    telegram_bot,
    tenants,
)
from src._time import utcnow
from src.main import cli
from tests.test_daily_price_letter import (
    _broken,
    _event,
    _mail_server,
    _mixed_catalog,
    _reader,
    _refusing,
    _trusted_run,
)

REPO = Path(__file__).resolve().parents[1]
SYSTEMD = REPO / "infra/systemd"
NOW = datetime(2026, 10, 14, 8, 0)
MAX_AGE = timedelta(hours=daily_letter_state.MAX_AGE_HOURS)


@pytest.fixture
def setup(monkeypatch, db_session):
    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *a, **kw: Session)
    monkeypatch.setattr(storage, "init_db", lambda *a, **kw: None)
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", "https://example.test")
    monkeypatch.delenv("COUNTRY_IDENTITY_POLICY", raising=False)
    monkeypatch.delenv("OFFER_AVAILABILITY_POLICY", raising=False)
    return db_session


@pytest.fixture
def at_eight(monkeypatch):
    """Часы health и рассылки стоят: 08:00 UTC, через три часа после таймера письма."""
    monkeypatch.setattr(health, "utcnow", lambda: NOW)
    monkeypatch.setattr(notifications, "utcnow", lambda: NOW)


def _mailed(*, ago: timedelta, recipients: int = 1, accepted: int = 1, report: str = "ok") -> None:
    """Запись о рассылке, закончившейся `ago` назад."""
    assert daily_letter_state.record(
        1, at=NOW - ago, recipients=recipients, accepted=accepted, report=report
    )


def _letter_issues(s) -> list[health.HealthIssue]:
    return health._check_daily_letter(s)


def _codes(issues) -> list[tuple[str, str]]:
    return [(issue.severity, issue.code) for issue in issues]


# ─── Кто оставляет запись ────────────────────────────────────────────────────


def test_real_mailing_leaves_a_record_without_addresses(setup, at_eight):
    s = setup
    _reader(s)
    _reader(s, email="second@example.test")
    _mixed_catalog(s, _trusted_run(s))

    with _mail_server():
        outcome = notifications.send_daily_letter(s, tenant_id=1)

    assert (outcome.recipients, outcome.accepted, outcome.report) == (2, 2, "ok")
    assert daily_letter_state.last_mailing(1) == daily_letter_state.Mailing(
        at=NOW, recipients=2, accepted=2, report="ok"
    )
    written = daily_letter_state.state_file().read_text(encoding="utf-8")
    assert "@" not in written and "example" not in written
    assert json.loads(written) == {
        "version": 1,
        "tenants": {
            "1": {"at": "2026-10-14T08:00:00", "recipients": 2, "accepted": 2, "report": "ok"}
        },
    }


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["--dry-run"], id="dry-run"),
        pytest.param(["--only", "reader@example.test"], id="only"),
        pytest.param(["--only", "reader@example.test", "--dry-run"], id="only-dry-run"),
    ],
)
def test_preview_runs_leave_no_record(setup, arguments):
    """Просмотр — не рассылка: по нему нельзя судить, получил ли клиент письмо."""
    s = setup
    _reader(s)
    _mixed_catalog(s, _trusted_run(s))

    with _mail_server():
        result = CliRunner().invoke(cli, ["notify", "digest", "daily", *arguments])

    assert result.exit_code == 0, result.output
    assert not daily_letter_state.state_file().exists()


def test_preview_does_not_touch_the_record_of_the_morning_mailing(setup, at_eight):
    s = setup
    _reader(s)
    _mixed_catalog(s, _trusted_run(s))
    _mailed(ago=timedelta(hours=3))
    before = daily_letter_state.state_file().read_text(encoding="utf-8")

    with _mail_server(accepts=False):
        notifications.send_daily_letter(s, tenant_id=1, only_email="reader@example.test")
        notifications.send_daily_letter(s, tenant_id=1, dry_run=True)

    assert daily_letter_state.state_file().read_text(encoding="utf-8") == before


def test_broken_report_is_recorded_by_its_error_class(setup, at_eight, monkeypatch):
    s = setup
    _reader(s)
    run = _trusted_run(s)
    _mixed_catalog(s, run)
    _event(s, run)
    monkeypatch.setattr(comparison_rows, "comparison_rows", _broken)

    with _mail_server():
        notifications.send_daily_letter(s, tenant_id=1)

    state = daily_letter_state.last_mailing(1)
    assert (state.recipients, state.accepted, state.report) == (1, 1, "failed:KeyError")
    assert state.failed_report == "KeyError"
    # Текст ошибки в файл не идёт — только её класс.
    assert "pack_size" not in daily_letter_state.state_file().read_text(encoding="utf-8")


def test_quiet_morning_is_recorded_as_nothing_to_send(setup, at_eight):
    """Ни отчёта, ни событий: запись есть, иначе тихое утро выглядело бы пропущенным."""
    s = setup
    _reader(s)

    with _mail_server() as mock_email:
        notifications.send_daily_letter(s, tenant_id=1)

    assert not mock_email.called
    assert daily_letter_state.last_mailing(1) == daily_letter_state.Mailing(
        at=NOW, recipients=0, accepted=0, report="skipped:no_successful_run"
    )


def test_run_without_subscribers_is_recorded_too(setup, at_eight):
    """Таймер отработал, слать некому. Без записи возврат подписки через три дня
    дал бы ложное «письмо не рассылалось 72 часа» ещё до первого утра."""
    s = setup
    _reader(s, daily_digest=False)

    with _mail_server():
        notifications.send_daily_letter(s, tenant_id=1)

    assert daily_letter_state.last_mailing(1) == daily_letter_state.Mailing(
        at=NOW, recipients=0, accepted=0, report="skipped:no_recipients"
    )


def test_dashboard_button_mailing_is_recorded(setup, at_eight, monkeypatch):
    """Кнопка «отправить дайджест» — тоже настоящая рассылка."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    s = setup
    _reader(s)
    _mixed_catalog(s, _trusted_run(s))
    admin = _reader(s, email="admin@example.test", role="admin", daily_digest=False)
    client = TestClient(api_module.app)
    client.get(f"/auth/verify?token={tenants.issue_magic_token(s, admin.email)}")

    with _mail_server():
        response = client.post("/api/v1/dash/digest/send-test?kind=daily")

    assert response.status_code == 200, response.text
    assert daily_letter_state.last_mailing(1).accepted == 1


def test_weekly_digest_leaves_no_record(setup):
    s = setup
    _reader(s, daily_digest=False, weekly_digest=True)
    _event(s, _trusted_run(s))

    with _mail_server():
        assert notifications.send_weekly_digest(s, tenant_id=1) == 1

    assert not daily_letter_state.state_file().exists()


# ─── Сама запись ─────────────────────────────────────────────────────────────


def test_record_goes_through_a_temporary_file_beside_the_state(monkeypatch):
    """Health читает файл каждый час и половину записи увидеть не должен."""
    path = daily_letter_state.state_file()
    real_replace = daily_letter_state.os.replace
    replaced = []

    def tracked(source, destination):
        replaced.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(daily_letter_state.os, "replace", tracked)
    _mailed(ago=timedelta(hours=1))

    ((source, destination),) = replaced
    assert destination == path and source.parent == path.parent
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_failed_replace_keeps_the_previous_record_and_leaves_no_litter(monkeypatch):
    path = daily_letter_state.state_file()
    _mailed(ago=timedelta(hours=30))
    before = path.read_text(encoding="utf-8")

    def refuse(source, destination):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(daily_letter_state.os, "replace", refuse)
    with capture_logs() as logs:
        written = daily_letter_state.record(1, at=NOW, recipients=1, accepted=1, report="ok")

    assert written is False
    assert path.read_text(encoding="utf-8") == before
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
    (failure,) = logs
    assert failure["event"] == "daily_letter_state_write_failed"
    assert (failure["error_type"], failure["errno"]) == ("PermissionError", 13)


def test_another_tenants_mailing_does_not_replace_ours():
    _mailed(ago=timedelta(hours=3), report="failed:KeyError")
    daily_letter_state.record(2, at=NOW, recipients=5, accepted=5, report="ok")

    assert daily_letter_state.last_mailing(1).report == "failed:KeyError"
    assert daily_letter_state.last_mailing(2).recipients == 5
    assert daily_letter_state.last_mailing(3) is None


def test_record_starts_over_from_a_damaged_file():
    daily_letter_state.state_file().write_text("{не json", encoding="utf-8")

    _mailed(ago=timedelta(hours=1))

    assert daily_letter_state.last_mailing(1).report == "ok"


def test_state_file_can_be_moved_with_the_environment(monkeypatch, tmp_path):
    elsewhere = tmp_path / "state" / "letter.json"
    monkeypatch.setenv(daily_letter_state.STATE_FILE_ENV, str(elsewhere))
    _mailed(ago=timedelta(hours=1))
    assert elsewhere.exists()

    monkeypatch.delenv(daily_letter_state.STATE_FILE_ENV)
    assert daily_letter_state.state_file() == Path("data/daily_letter_state.json")


def test_failed_state_write_does_not_stop_the_letter(setup, monkeypatch, tmp_path):
    """Письма к моменту записи уже ушли: сбой записи — строка в журнале, не ошибка команды."""
    s = setup
    _reader(s)
    _mixed_catalog(s, _trusted_run(s))
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(daily_letter_state.STATE_FILE_ENV, str(blocker / "state.json"))

    with _mail_server() as mock_email, capture_logs() as logs:
        outcome = notifications.send_daily_letter(s, tenant_id=1)

    assert mock_email.call_count == 1 and outcome.accepted == 1 and outcome.problems == []
    (failure,) = [entry for entry in logs if entry["event"] == "daily_letter_state_write_failed"]
    assert failure["log_level"] == "warning" and failure["path"].endswith("state.json")
    assert issubclass(getattr(builtins, failure["error_type"]), OSError)

    with _mail_server():
        result = CliRunner().invoke(cli, ["notify", "digest", "daily"])
    assert result.exit_code == 0, result.output


# ─── Проверка в health ───────────────────────────────────────────────────────


def test_no_record_means_no_check(setup, at_eight):
    """Сторож начинает работать с первой настоящей рассылки: включение подписки
    не поднимает тревогу до первого утра."""
    _reader(setup)

    assert not daily_letter_state.state_file().exists()
    assert _letter_issues(setup) == []


@pytest.mark.parametrize(
    "subscriber",
    [
        pytest.param(None, id="nobody"),
        pytest.param({"daily_digest": False, "weekly_digest": True}, id="weekly-only"),
        pytest.param({"is_active": False}, id="inactive"),
    ],
)
def test_no_subscribers_means_no_check(setup, at_eight, subscriber):
    """Письмо выключили — старая запись о рассылке не превращается в тревогу."""
    tenants.get_or_create_default(setup)
    if subscriber is not None:
        _reader(setup, **subscriber)
    _mailed(ago=timedelta(days=5), recipients=1, accepted=0, report="failed:KeyError")

    assert _letter_issues(setup) == []


def test_fresh_good_mailing_is_healthy(setup, at_eight):
    _reader(setup)
    _mailed(ago=timedelta(hours=3))

    assert _letter_issues(setup) == []


def test_mailing_is_overdue_only_past_the_threshold(setup, at_eight):
    _reader(setup)

    _mailed(ago=MAX_AGE)
    assert _letter_issues(setup) == []

    _mailed(ago=MAX_AGE + timedelta(minutes=1))
    (issue,) = _letter_issues(setup)
    assert (issue.severity, issue.code) == ("critical", "daily_letter_overdue")
    # Что случилось, с какого времени и куда смотреть.
    assert "не рассылалось 26ч (порог 26ч; последняя рассылка — 13.10 05:59 UTC)" in issue.message
    assert "journalctl -u pharmacy-monitor-digest-daily" in issue.message
    assert "systemctl status pharmacy-monitor-digest-daily.timer" in issue.message


def test_missed_morning_is_noticed_within_three_hours_and_not_before(setup, monkeypatch):
    """Вчера письмо ушло в 05:02. Сегодня в 05:00 оно не ушло: в 06:00 и 07:00
    ещё рано (вчера могло уйти позже обычного), в 08:00 сторож это видит."""
    _reader(setup)
    daily_letter_state.record(
        1, at=datetime(2026, 10, 13, 5, 2), recipients=1, accepted=1, report="ok"
    )

    def codes_at(hour: int) -> list[tuple[str, str]]:
        monkeypatch.setattr(health, "utcnow", lambda: datetime(2026, 10, 14, hour, 0))
        return _codes(_letter_issues(setup))

    assert codes_at(6) == [] and codes_at(7) == []
    assert codes_at(8) == [("critical", "daily_letter_overdue")]


def test_letter_nobody_received_is_critical(setup, at_eight):
    _reader(setup)
    _mailed(ago=timedelta(hours=3), recipients=2, accepted=0)

    (issue,) = _letter_issues(setup)
    assert (issue.severity, issue.code) == ("critical", "daily_letter_not_delivered")
    assert "14.10 05:00 UTC почтовый сервер не принял ни у одного из 2 получателей" in issue.message
    assert "digest_email_failed" in issue.message and "email_skipped_no_smtp" in issue.message


def test_letter_one_of_two_received_is_not_an_incident(setup, at_eight):
    _reader(setup)
    _mailed(ago=timedelta(hours=3), recipients=2, accepted=1)

    assert _letter_issues(setup) == []


@pytest.mark.parametrize(
    ("recipients", "accepted", "severity", "says"),
    [
        (2, 2, "warning", "ушло без отчёта о разнице цен и без файла (2 из 2 получателей)"),
        (2, 0, "critical", "осталось без отчёта о разнице цен, и почтовый сервер его не принял"),
        (0, 0, "critical", "не отправлено: отчёта о разнице цен нет, а событий за сутки не было"),
    ],
    ids=["letter-went-without-report", "server-refused-it", "nothing-to-send"],
)
def test_broken_report_in_the_last_mailing(setup, at_eight, recipients, accepted, severity, says):
    """Письмо без отчёта — ущербное утро; если оно при этом никому не ушло — несостоявшееся."""
    _reader(setup)
    _mailed(
        ago=timedelta(hours=3),
        recipients=recipients,
        accepted=accepted,
        report="failed:OperationalError",
    )

    (issue,) = _letter_issues(setup)
    assert (issue.severity, issue.code) == (severity, "daily_letter_report_failed")
    assert f"Утреннее письмо 14.10 05:00 UTC {says}" in issue.message
    assert "Сборка отчёта упала (OperationalError)" in issue.message
    assert "digest_price_report_failed" in issue.message


@pytest.mark.parametrize(
    "report", ["skipped:no_successful_run", "skipped:no_comparison_rows", "skipped:no_recipients"]
)
def test_morning_with_nothing_to_send_is_not_an_incident(setup, at_eight, report):
    _reader(setup)
    _mailed(ago=timedelta(hours=3), recipients=0, accepted=0, report=report)

    assert _letter_issues(setup) == []


def test_overdue_outranks_what_the_old_mailing_looked_like(setup, at_eight):
    """Одна проблема на письмо: давно не рассылалось — прошлый исход уже не новость."""
    _reader(setup)
    _mailed(ago=timedelta(days=3), recipients=2, accepted=0, report="failed:KeyError")

    assert _codes(_letter_issues(setup)) == [("critical", "daily_letter_overdue")]


def test_record_of_another_tenant_is_not_ours(setup, at_eight):
    _reader(setup)
    daily_letter_state.record(2, at=NOW - timedelta(days=9), recipients=1, accepted=0, report="ok")

    assert _letter_issues(setup) == []


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("{не json", id="not-json"),
        pytest.param("[]", id="not-an-object"),
        pytest.param('{"version": 1}', id="no-tenants"),
        pytest.param('{"tenants": {"1": {"at": "вчера"}}}', id="bad-time"),
        pytest.param(
            '{"tenants": {"1": {"at": "2026-10-14T05:00:00", "recipients": 1, "accepted": 2,'
            ' "report": "ok"}}}',
            id="more-accepted-than-sent",
        ),
        pytest.param(
            '{"tenants": {"1": {"at": "2026-10-14T05:00:00", "recipients": 1, "accepted": 1,'
            ' "report": "failed:<b>x</b>"}}}',
            id="report-is-not-ours",
        ),
    ],
)
def test_unreadable_record_is_reported_not_ignored(setup, at_eight, content):
    """Сторож, который молча выключается на испорченном файле, — не сторож. И в
    письмо админу из файла не попадает ничего, кроме того, что писали мы сами."""
    _reader(setup)
    daily_letter_state.state_file().write_text(content, encoding="utf-8")

    (issue,) = _letter_issues(setup)
    assert (issue.severity, issue.code) == ("warning", "daily_letter_state_unreadable")
    assert "сторож письма не работает" in issue.message
    assert "<b>" not in issue.message


# ─── Тревога доходит до админа тем же путём, что остальные ───────────────────


def _healthy_catalog(s) -> None:
    """Свежий проверенный сбор с товарами: без письма health зелёный, и письму есть что слать."""
    run = _trusted_run(s)
    _mixed_catalog(s, run)
    run.products_scraped = 20
    s.commit()


def test_health_report_carries_the_letter_problem(setup):
    s = setup
    _healthy_catalog(s)
    _reader(s)
    assert health.check_health(s).status == "ok"

    daily_letter_state.record(
        1, at=utcnow() - MAX_AGE - timedelta(hours=1), recipients=1, accepted=1, report="ok"
    )
    report = health.check_health(s)

    assert report.status == "critical"
    assert [issue.code for issue in report.issues] == ["daily_letter_overdue"]
    # Письмо админу и /status в Telegram собираются из того же отчёта.
    assert "Утреннее письмо с разницей цен не рассылалось" in health.render_alert_html(report)
    assert "daily_letter_overdue" in telegram_bot.cmd_status(s, "chat", "")


def test_health_check_command_mails_the_admin_once_and_reports_recovery(setup, tmp_path):
    """Часовой `health-check --alert-email`: тревога, тишина при том же инциденте,
    письмо о восстановлении после удавшейся рассылки."""
    s = setup
    _healthy_catalog(s)
    _reader(s)
    arguments = [
        "health-check",
        "--alert-email",
        "--quiet-on-ok",
        "--alert-state-file",
        str(tmp_path / "health_alert_state.json"),
    ]

    # Утро: почтовый сервер отказал всем.
    with patch("src.notifier.send_email", side_effect=_refusing):
        assert CliRunner().invoke(cli, ["notify", "digest", "daily"]).exit_code == 1

    mailed = []
    with patch.object(notifier, "send_email", lambda **kwargs: mailed.append(kwargs) or True):
        first = CliRunner().invoke(cli, arguments)
        second = CliRunner().invoke(cli, arguments)
    assert first.exit_code == 2, first.output
    assert "[critical] daily_letter_not_delivered" in first.output
    assert "Email-алерт отправлен" in first.output and "Email подавлен" in second.output
    (alert,) = mailed
    assert alert["subject"] == "Pharmacy Monitor — CRITICAL"
    assert "почтовый сервер не принял ни у одного из 1 получателей" in alert["html_body"]
    assert "@" not in alert["html_body"]

    # Следующая рассылка удалась — health сообщает о восстановлении.
    with _mail_server():
        assert CliRunner().invoke(cli, ["notify", "digest", "daily"]).exit_code == 0
    with patch.object(notifier, "send_email", lambda **kwargs: mailed.append(kwargs) or True):
        recovered = CliRunner().invoke(cli, arguments)
    assert recovered.exit_code == 0, recovered.output
    assert [letter["subject"] for letter in mailed][1:] == ["Pharmacy Monitor — RECOVERED"]


def test_same_letter_incident_is_one_incident_and_a_different_one_is_new(setup, at_eight):
    """Письмо админу — раз на инцидент: час спустя подпись та же, другая беда — новая."""
    _reader(setup)

    def signature() -> str:
        issues = _letter_issues(setup)
        return health.alert_signature(health.HealthReport(status="critical", issues=issues))

    _mailed(ago=MAX_AGE + timedelta(hours=1))
    overdue = signature()
    _mailed(ago=MAX_AGE + timedelta(hours=2))
    assert signature() == overdue

    _mailed(ago=timedelta(hours=3), recipients=1, accepted=0)
    not_delivered = signature()
    _mailed(ago=timedelta(hours=3), recipients=1, accepted=1, report="failed:KeyError")
    report_failed = signature()
    _mailed(ago=timedelta(hours=3), recipients=1, accepted=0, report="failed:KeyError")
    assert len({overdue, not_delivered, report_failed, signature()}) == 4


# ─── Растяжки на юниты в infra/systemd ───────────────────────────────────────


def _settings(unit_file: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in unit_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            found.setdefault(key, []).append(value.strip())
    return found


def test_threshold_fits_a_letter_that_goes_out_once_a_day():
    """Порог сторожа выведен из суточного таймера. Таймер перестал быть суточным
    (или юнит стал работать дольше) — этот тест скажет поменять `MAX_AGE_HOURS`."""
    timer = _settings(SYSTEMD / "pharmacy-monitor-digest-daily.timer")
    unit = _settings(SYSTEMD / "pharmacy-monitor-digest-daily.service")
    health_timer = _settings(SYSTEMD / "pharmacy-monitor-health.timer")

    (schedule,) = timer["OnCalendar"]
    assert re.fullmatch(r"\*-\*-\* \d{2}:\d{2}:\d{2} UTC", schedule), schedule
    assert health_timer["OnCalendar"] == ["hourly"]
    # Худшее исправное утро: вчера письмо ушло сразу, сегодня — с полной случайной
    # задержкой и за всё отведённое юниту время.
    latest_healthy_gap = timedelta(
        hours=24,
        seconds=int(timer["RandomizedDelaySec"][0]) + int(unit["TimeoutStartSec"][0]),
    )
    assert latest_healthy_gap < MAX_AGE
    # И не настолько больше, чтобы о пропущенном утре узнавали к вечеру.
    assert MAX_AGE <= timedelta(hours=30)


@pytest.mark.parametrize(
    "unit_name",
    [
        "pharmacy-monitor-digest-daily.service",  # пишет запись
        "pharmacy-monitor-api.service",  # пишет запись: кнопка в дашборде
        "pharmacy-monitor-health.service",  # читает запись
    ],
)
def test_writer_and_reader_units_see_the_same_state_file(unit_name):
    """Путь по умолчанию относительный: один каталог, один пользователь и право
    писать в `data/` — иначе health читал бы не тот файл, что пишет письмо."""
    unit = _settings(SYSTEMD / unit_name)

    assert unit["User"] == ["pm"]
    assert unit["WorkingDirectory"] == ["/opt/pharmacy-monitor"]
    assert unit["EnvironmentFile"] == ["/etc/pharmacy-monitor/env"]
    assert "/opt/pharmacy-monitor/data" in unit["ReadWritePaths"][0].split()
    assert not Path(daily_letter_state.DEFAULT_STATE_FILE).is_absolute()
    assert Path(daily_letter_state.DEFAULT_STATE_FILE).parts[0] == "data"
