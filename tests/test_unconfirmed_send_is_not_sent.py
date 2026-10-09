"""«Отправлено» говорится только о том, что отправитель подтвердил.

`notifier.send_telegram_message` свой сбой наружу не выпускает, а отвечает
`False` (нет токена, сеть, отказ Telegram); `notifier.send_email` отвечает
`False`, когда SMTP не настроен, а отказ сервера и сеть бросает исключением.
Вызов, после которого что-то считается разосланным, обязан читать ответ.

Здесь — места, где ответ не читался или сбой терялся по дороге к оператору:
дайджест (`notifications._send_digest`, старый `digest.send_daily_digest` и их
команды и кнопка), итог рассылки о прогоне в конце сбора
(`main._dispatch_run_alerts`), `alert evaluate --dispatch`, письмо
администраторам с тика (`notifications.mail_unstored_events_to_admins`) и
старый `alerts.dispatch_event`. Каждое место вызывается напрямую с отправителем,
который отвечает `False`, и с отправителем, который бросает исключение.
"""

from __future__ import annotations

import ast
import smtplib
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import alerts, api, digest, notifications, notifier, storage, tenants
from src import main as main_mod
from src._time import utcnow
from tests.test_run_failure_semantics import _patch_verified_aloe_pipeline

ALICE = "alice@client.example"
BOB = "bob@client.example"
CHAT_ID = "700100"
TITLE = "Naproksen подешевел"
SMTP_TEXT = f"550 5.1.1 <{ALICE}>: Recipient address rejected"

# Два способа, которыми отправитель сообщает «не ушло».
REFUSALS = ["answers_false", "raises"]


def _raises(*args, **kwargs):
    raise ConnectionRefusedError(111, SMTP_TEXT)


def _refusing(how: str):
    """Отправитель, который не отправил: ответил False или бросил исключение."""
    return {"return_value": False} if how == "answers_false" else {"side_effect": _raises}


def _refuses(how: str, *, only: str):
    """Письмо уходит всем, кроме получателя `only`."""

    def send(subject, html_body, attachments=None, to=None):
        if to == [only]:
            if how == "raises":
                _raises()
            return False
        return True

    return send


def _error_fields(how: str) -> dict:
    """Что строка сбоя несёт о причине: при отказе — ничего, отправитель написал сам."""
    return {} if how == "answers_false" else {"error_type": "ConnectionRefusedError", "errno": 111}


def _lines(logs: list[dict], *events: str) -> list[dict]:
    return [entry for entry in logs if entry["event"] in events]


def _assert_no_person_or_text(printed: object) -> None:
    """Ни адреса, ни идентификатора чата, ни текста сообщения или ошибки."""
    text = repr(printed)
    assert "@" not in text, text
    assert CHAT_ID not in text and TITLE not in text and "rejected" not in text, text


class _Journal:
    """Строки журнала одного модуля (`src.main`, `src.api`): вход в CLI заново
    настраивает structlog, и `capture_logs` событий команды не видит."""

    def __init__(self):
        self.lines: list[dict] = []

    def __getattr__(self, level):
        def record(event, **fields):
            self.lines.append({"event": event, "log_level": level, **fields})

        return record

    def about_alerts(self) -> list[dict]:
        return [line for line in self.lines if "alert" in line["event"]]


@pytest.fixture
def stand(monkeypatch, db_session):
    """База и окружение без SMTP и без токена; сеть запрещена."""
    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *a, **kw: Session)
    monkeypatch.setattr(storage, "init_db", lambda *a, **kw: None)
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", "https://example.com")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("тест не должен ходить в сеть")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(smtplib, "SMTP", no_network)
    return db_session


def _user(s, email=ALICE, **fields) -> storage.TenantUser:
    tenant = tenants.get_or_create_default(s)
    values = dict(
        tenant_id=tenant.id,
        email=email,
        role="admin",
        is_active=True,
        created_at=utcnow(),
        email_severity_min="info",
        telegram_severity_min="off",
        daily_digest=False,
        weekly_digest=True,
    )
    values.update(fields)
    user = storage.TenantUser(**values)
    s.add(user)
    s.commit()
    return user


def _events(s, user, n=1, *, severity="critical", rule_id=None) -> list[storage.AlertEvent]:
    """События не о деньгах: в дайджест идут без проверенного прогона."""
    start = s.query(storage.AlertEvent).count()
    events = [
        storage.AlertEvent(
            rule_type="site_silent",
            rule_id=rule_id,
            dedup_key=f"unconfirmed-{start + i}",
            severity=severity,
            title=TITLE,
            detail="Сайт не отвечал сутки",
            tenant_id=user.tenant_id,
            created_at=utcnow(),
        )
        for i in range(n)
    ]
    s.add_all(events)
    s.commit()
    return events


# ─── 1. Дайджест получателям (`notifications._send_digest`) ──────────────────


@pytest.mark.parametrize("how", REFUSALS)
def test_digest_nobody_received_is_not_sent(stand, how):
    """Отправитель не отправил никому: получатели не считаются, `digest_sent` нет."""
    alice, bob = _user(stand), _user(stand, BOB)
    _events(stand, alice, 3)

    with patch("src.notifier.send_email", **_refusing(how)) as send, capture_logs() as logs:
        result = notifications.send_weekly_digest(stand, tenant_id=alice.tenant_id)

    assert send.call_count == 2  # отказ одному не отменяет письмо другому
    assert result == {"recipients": 2, "sent": 0, "failed": 2}
    assert _lines(logs, "digest_sent") == []
    assert _lines(logs, "digest_email_failed", "digest_not_sent") == [
        {
            "event": "digest_email_failed",
            "log_level": "warning",
            "user_id": alice.id,
            "kind": "weekly",
            **_error_fields(how),
        },
        {
            "event": "digest_email_failed",
            "log_level": "warning",
            "user_id": bob.id,
            "kind": "weekly",
            **_error_fields(how),
        },
        {
            "event": "digest_not_sent",
            "log_level": "warning",
            "kind": "weekly",
            "failed": 2,
            "events": 3,
        },
    ]
    _assert_no_person_or_text(logs)


@pytest.mark.parametrize("how", REFUSALS)
@pytest.mark.parametrize("refused", [ALICE, BOB])
def test_digest_counts_only_the_recipient_the_sender_confirmed(stand, how, refused):
    """Ушло одному из двух — первому или последнему: считается один, второй назван."""
    users = {ALICE: _user(stand), BOB: _user(stand, BOB)}
    _events(stand, users[ALICE], 2)

    with (
        patch("src.notifier.send_email", side_effect=_refuses(how, only=refused)) as send,
        capture_logs() as logs,
    ):
        result = notifications.send_weekly_digest(stand, tenant_id=users[ALICE].tenant_id)

    assert send.call_count == 2
    assert result == {"recipients": 2, "sent": 1, "failed": 1}
    assert [entry["user_id"] for entry in _lines(logs, "digest_email_failed")] == [
        users[refused].id
    ]
    # Ушло не всем: итог — предупреждение, и в нём сколько не ушло.
    assert _lines(logs, "digest_sent", "digest_not_sent") == [
        {
            "event": "digest_sent",
            "log_level": "warning",
            "kind": "weekly",
            "recipients": 1,
            "failed": 1,
            "events": 2,
        }
    ]
    _assert_no_person_or_text(logs)


def test_digest_confirmed_for_everyone_keeps_the_usual_line(stand):
    alice = _user(stand)
    _user(stand, BOB)
    _events(stand, alice, 2)

    with patch("src.notifier.send_email", return_value=True), capture_logs() as logs:
        result = notifications.send_weekly_digest(stand, tenant_id=alice.tenant_id)

    assert result == {"recipients": 2, "sent": 2, "failed": 0}
    assert _lines(logs, "digest_sent", "digest_not_sent", "digest_email_failed") == [
        {
            "event": "digest_sent",
            "log_level": "info",
            "kind": "weekly",
            "recipients": 2,
            "events": 2,
        }
    ]


def test_digest_without_smtp_is_not_sent_by_the_real_sender(stand):
    """Тот самый случай: `notify digest`, запущенная руками без загруженного env."""
    alice = _user(stand)
    _events(stand, alice)

    with capture_logs() as logs:
        result = notifications.send_daily_digest(stand, tenant_id=alice.tenant_id)
    assert result == {"recipients": 0, "sent": 0, "failed": 0}  # daily_digest выключен

    alice.daily_digest = True
    stand.commit()
    with capture_logs() as logs:
        result = notifications.send_daily_digest(stand, tenant_id=alice.tenant_id)

    assert result == {"recipients": 1, "sent": 0, "failed": 1}
    assert [entry["event"] for entry in logs] == [
        "email_skipped_no_smtp",
        "digest_email_failed",
        "digest_not_sent",
    ]


def test_digest_dry_run_counts_recipients_but_nothing_sent(stand):
    alice = _user(stand)
    _events(stand, alice)

    with patch("src.notifier.send_email") as send, capture_logs() as logs:
        result = notifications.send_weekly_digest(stand, tenant_id=alice.tenant_id, dry_run=True)

    assert not send.called
    assert result == {"recipients": 1, "sent": 0, "failed": 0}
    assert _lines(logs, "digest_sent", "digest_not_sent") == []
    assert _lines(logs, "digest_dry_run_done") == [
        {
            "event": "digest_dry_run_done",
            "log_level": "info",
            "kind": "weekly",
            "recipients": 1,
            "events": 1,
        }
    ]


@pytest.mark.parametrize("how", REFUSALS)
def test_notify_digest_command_fails_when_nothing_was_sent(stand, how):
    alice = _user(stand)
    _user(stand, BOB)
    _events(stand, alice)

    with patch("src.notifier.send_email", **_refusing(how)):
        result = CliRunner().invoke(main_mod.cli, ["notify", "digest", "weekly"])

    assert result.exit_code == 1, result.output
    assert "weekly digest sent to 0 of 2 recipients, 2 not sent" in result.output
    assert "OK:" not in result.output
    _assert_no_person_or_text(result.output)


def test_notify_digest_command_fails_when_one_recipient_was_refused(stand):
    alice = _user(stand)
    _user(stand, BOB)
    _events(stand, alice)

    with patch("src.notifier.send_email", side_effect=_refuses("answers_false", only=BOB)):
        result = CliRunner().invoke(main_mod.cli, ["notify", "digest", "weekly"])

    assert result.exit_code == 1, result.output
    assert "weekly digest sent to 1 of 2 recipients, 1 not sent" in result.output
    assert "OK:" not in result.output


def test_notify_digest_command_reports_confirmed_recipients(stand):
    alice = _user(stand)
    _user(stand, BOB)
    _events(stand, alice)

    with patch("src.notifier.send_email", return_value=True):
        result = CliRunner().invoke(main_mod.cli, ["notify", "digest", "weekly"])

    assert result.exit_code == 0, result.output
    assert "OK: weekly digest sent to 2 recipients" in result.output


def test_notify_digest_command_with_nothing_to_send_is_not_a_failure(stand):
    """Пустая неделя: писем нет, но это не сбой — таймер не должен краснеть."""
    _user(stand)

    with patch("src.notifier.send_email") as send:
        result = CliRunner().invoke(main_mod.cli, ["notify", "digest", "weekly"])

    assert not send.called
    assert result.exit_code == 0, result.output
    assert "OK: weekly digest sent to 0 recipients" in result.output


@pytest.fixture
def dashboard(stand, monkeypatch):
    """Кнопка «отправить дайджест» в Quick Actions: тот же код, что у таймера."""
    admin = _user(stand)
    _user(stand, BOB)
    _events(stand, admin)
    journal = _Journal()
    monkeypatch.setattr(api, "log", journal)

    def db():
        yield stand

    api.app.dependency_overrides[api.require_user] = lambda: admin
    api.app.dependency_overrides[api.get_db] = db
    yield SimpleNamespace(client=TestClient(api.app), admin=admin, journal=journal)
    api.app.dependency_overrides.clear()


def _press_digest_button(dashboard, **sender):
    dashboard.journal.lines.clear()
    with patch("src.notifier.send_email", **sender):
        response = dashboard.client.post("/api/v1/dash/digest/send-test?kind=weekly")
    about_digest = [line for line in dashboard.journal.lines if "digest" in line["event"]]
    return response, about_digest


@pytest.mark.parametrize("how", REFUSALS)
def test_digest_button_does_not_answer_sent_when_nothing_was(dashboard, how):
    response, journal = _press_digest_button(dashboard, **_refusing(how))

    assert response.status_code == 502, response.text
    assert "recipients_sent" not in response.json()
    assert journal == [
        {
            "event": "digest_send_failed",
            "log_level": "error",
            "kind": "weekly",
            "failed": 2,
            "by_user_id": dashboard.admin.id,
        }
    ]
    _assert_no_person_or_text([response.text, journal])


def test_digest_button_counts_only_confirmed_recipients(dashboard):
    partial, partial_journal = _press_digest_button(
        dashboard, side_effect=_refuses("raises", only=ALICE)
    )
    clean, clean_journal = _press_digest_button(dashboard, return_value=True)

    assert partial.status_code == 200, partial.text
    assert partial.json() == {"ok": False, "recipients_sent": 1, "recipients_failed": 1}
    assert clean.json() == {"ok": True, "recipients_sent": 2, "recipients_failed": 0}
    by = {"kind": "weekly", "by_user_id": dashboard.admin.id}
    # Ушло не всем — предупреждение, а не обычная строка.
    assert partial_journal == [
        {"event": "digest_sent_manual", "log_level": "warning", "count": 1, "failed": 1, **by}
    ]
    assert clean_journal == [
        {"event": "digest_sent_manual", "log_level": "info", "count": 2, "failed": 0, **by}
    ]


# ─── 1б. Старый ежедневный дайджест (`digest.send_daily_digest`) ─────────────


@pytest.mark.parametrize("how", REFUSALS)
def test_legacy_digest_not_sent_is_an_error_not_a_count(stand, how):
    """Письмо было, отправитель его не подтвердил: ни числа событий, ни `digest_sent`."""
    _events(stand, _user(stand), 2)

    with (
        patch("src.notifier.send_email", **_refusing(how)) as send,
        capture_logs() as logs,
        pytest.raises(digest.DigestNotSent) as raised,
    ):
        digest.send_daily_digest(stand)

    assert send.call_count == 1
    assert _lines(logs, "digest_sent") == []
    (failure,) = _lines(logs, "digest_email_failed")
    assert failure["log_level"] == "error"
    assert ("error_type" in failure) == (how == "raises")
    assert "error" not in failure
    # Исходная ошибка с адресом в тексте не уходит ни текстом, ни причиной.
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    _assert_no_person_or_text([logs, str(raised.value)])


def test_legacy_digest_confirmed_returns_the_number_of_events(stand):
    _events(stand, _user(stand), 2)

    with patch("src.notifier.send_email", return_value=True), capture_logs() as logs:
        assert digest.send_daily_digest(stand) == 2

    assert [entry["event"] for entry in _lines(logs, "digest_sent", "digest_email_failed")] == [
        "digest_sent"
    ]


@pytest.mark.parametrize("how", REFUSALS)
def test_legacy_digest_command_fails_when_the_letter_did_not_go(stand, how):
    """Раньше: без SMTP — «OK: digest sent», при упавшей почте — «no events in window»."""
    _events(stand, _user(stand))

    with patch("src.notifier.send_email", **_refusing(how)):
        result = CliRunner().invoke(main_mod.cli, ["digest"])

    assert result.exit_code == 1, result.output
    assert "digest not sent" in result.output
    assert "OK:" not in result.output and "Skipped" not in result.output
    assert "Traceback" not in result.output
    _assert_no_person_or_text(result.output)


def test_legacy_digest_command_tells_sent_from_nothing_to_send_from_dry_run(stand):
    user = _user(stand)

    with patch("src.notifier.send_email", return_value=True) as send:
        empty = CliRunner().invoke(main_mod.cli, ["digest"])
        assert not send.called
        _events(stand, user)
        dry = CliRunner().invoke(main_mod.cli, ["digest", "--dry-run"])
        assert not send.called
        sent = CliRunner().invoke(main_mod.cli, ["digest"])
        assert send.call_count == 1

    assert (empty.exit_code, dry.exit_code, sent.exit_code) == (0, 0, 0)
    assert "Skipped: no events in window" in empty.output
    # Пробный прогон ничего не слал — «sent» в его выводе быть не может.
    assert "[dry-run] 1 events, nothing sent" in dry.output
    assert "OK:" not in dry.output
    assert "OK: digest sent, 1 events" in sent.output


# ─── 2. Итог рассылки о прогоне в конце сбора (`main._dispatch_run_alerts`) ──


@pytest.fixture
def journal(monkeypatch):
    recorder = _Journal()
    monkeypatch.setattr(main_mod, "log", recorder)
    return recorder


RUN_ID = 77


@pytest.mark.parametrize("how", REFUSALS)
def test_run_alerts_nobody_received_are_not_called_dispatched(stand, journal, how):
    user = _user(stand)
    fired = _events(stand, user, 3)

    with patch("src.notifier.send_email", **_refusing(how)):
        main_mod._dispatch_run_alerts(stand, RUN_ID, fired)

    assert journal.lines == [
        {
            "event": "alert_dispatch_failed",
            "log_level": "warning",
            "run_id": RUN_ID,
            "events": 3,
            "email": 0,
            "telegram": 0,
            "failed": 1,
            "undelivered": 3,
        }
    ]


def test_run_alerts_one_refused_recipient_is_a_failure_with_the_counts(stand, journal):
    """Одному ушло, другому нет: строка сбоя, а в ней — сколько всё-таки ушло."""
    user = _user(stand)
    _user(stand, BOB)
    fired = _events(stand, user, 2)

    with patch("src.notifier.send_email", side_effect=_refuses("answers_false", only=BOB)):
        main_mod._dispatch_run_alerts(stand, RUN_ID, fired)

    assert journal.lines == [
        {
            "event": "alert_dispatch_failed",
            "log_level": "warning",
            "run_id": RUN_ID,
            "events": 2,
            "email": 1,
            "telegram": 0,
            "failed": 1,
            "undelivered": 0,
        }
    ]


def test_run_alerts_that_crashed_are_not_called_dispatched(stand, journal):
    """Рассылка упала целиком (база): строка сбоя без текста ошибки, сбор не падает."""
    fired = _events(stand, _user(stand), 2)

    def crash(session, events):
        raise RuntimeError(f"UPDATE tenant_users … {ALICE}")

    with patch.object(notifications, "dispatch_events_batch", crash):
        main_mod._dispatch_run_alerts(stand, RUN_ID, fired)

    assert journal.lines == [
        {
            "event": "alert_dispatch_failed",
            "log_level": "warning",
            "run_id": RUN_ID,
            "events": 2,
            "error_type": "RuntimeError",
        }
    ]


def test_run_alerts_confirmed_are_dispatched_with_what_went(stand, journal):
    user = _user(stand, telegram_severity_min="info", telegram_chat_id=CHAT_ID)
    fired = _events(stand, user, 2)

    with (
        patch("src.notifier.send_email", return_value=True),
        patch("src.notifier.send_telegram_message", return_value=True),
    ):
        main_mod._dispatch_run_alerts(stand, RUN_ID, fired)

    assert journal.lines == [
        {
            "event": "alerts_dispatched",
            "log_level": "info",
            "run_id": RUN_ID,
            "events": 2,
            "email": 1,
            "telegram": 1,
            "failed": 0,
            "undelivered": 0,
        }
    ]


def test_run_alerts_confirmed_by_telegram_alone_are_dispatched(stand, journal):
    user = _user(
        stand, email_severity_min="off", telegram_severity_min="info", telegram_chat_id=CHAT_ID
    )
    fired = _events(stand, user)

    with patch("src.notifier.send_telegram_message", return_value=True):
        main_mod._dispatch_run_alerts(stand, RUN_ID, fired)

    (line,) = journal.lines
    assert (line["event"], line["email"], line["telegram"]) == ("alerts_dispatched", 0, 1)


def test_run_alerts_with_nobody_to_send_to_are_not_called_dispatched(stand, journal):
    """Сбоя нет, но и получателя нет (порог важности): это не «разослано»."""
    user = _user(stand, email_severity_min="critical")
    fired = _events(stand, user, 2, severity="info")

    with patch("src.notifier.send_email") as send:
        main_mod._dispatch_run_alerts(stand, RUN_ID, fired)

    assert not send.called
    assert journal.lines == [
        {
            "event": "alerts_dispatch_no_recipient",
            "log_level": "info",
            "run_id": RUN_ID,
            "events": 2,
        }
    ]


def _run_that_fires_an_alert(stand, monkeypatch):
    """Команда `run` целиком; подменён поход на сайт и подсчёт правил."""
    user = _user(stand)
    _patch_verified_aloe_pipeline(stand, monkeypatch)
    # У `run` своя сессия: событие должно жить в ней, иначе канал ему не записать.
    fired: list[storage.AlertEvent] = []

    def evaluate(session, run_id):
        event = storage.AlertEvent(
            rule_type="site_silent",
            dedup_key=f"run-{run_id}",
            severity="critical",
            title=TITLE,
            tenant_id=user.tenant_id,
            created_at=utcnow(),
        )
        session.add(event)
        session.commit()
        fired.append(event)
        return [event]

    monkeypatch.setattr(alerts, "evaluate_rules", evaluate)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main_mod.cli, ["run", "--site", "aloe", "--mode", "category", "--force"]
        )
    return result, fired


@pytest.mark.parametrize("how", REFUSALS)
def test_run_does_not_log_dispatched_when_the_letter_did_not_go(stand, monkeypatch, journal, how):
    """Конец настоящего сбора: сбой рассылки сбор не роняет и «разослано» не пишет."""
    with patch("src.notifier.send_email", **_refusing(how)):
        result, fired = _run_that_fires_an_alert(stand, monkeypatch)

    assert result.exit_code == 0, result.output
    assert [line["event"] for line in journal.about_alerts()] == ["alert_dispatch_failed"]
    (failure,) = journal.about_alerts()
    assert (failure["events"], failure["failed"], failure["undelivered"]) == (1, 1, 1)
    assert not fired[0].channels_sent
    run = stand.query(storage.Run).one()
    assert run.status == "ok" and failure["run_id"] == run.id


def test_run_logs_dispatched_when_the_sender_confirmed(stand, monkeypatch, journal):
    with patch("src.notifier.send_email", return_value=True):
        result, fired = _run_that_fires_an_alert(stand, monkeypatch)

    assert result.exit_code == 0, result.output
    (line,) = journal.about_alerts()
    assert (line["event"], line["email"], line["failed"]) == ("alerts_dispatched", 1, 0)
    assert fired[0].channels_sent == ["email"]


# ─── 3. `alert evaluate --dispatch` ──────────────────────────────────────────


@pytest.fixture
def three_alerts_fire(stand, monkeypatch):
    user = _user(stand)

    def evaluate(session, rule_ids=None):
        return _events(session, user, 3)

    monkeypatch.setattr(alerts, "evaluate_rules", evaluate)
    return user


@pytest.mark.parametrize("how", REFUSALS)
def test_alert_evaluate_dispatch_fails_when_nothing_was_sent(three_alerts_fire, how):
    with patch("src.notifier.send_email", **_refusing(how)):
        result = CliRunner().invoke(main_mod.cli, ["alert", "evaluate", "--dispatch"])

    assert result.exit_code == 1, result.output
    assert "Сработало: 3" in result.output
    assert "не отправлено ничего — писем 0, сообщений Telegram 0" in result.output
    assert "отправок не состоялось: 1" in result.output
    assert "не дошедших ни по одному каналу: 3" in result.output
    assert "→ отправлено" not in result.output
    _assert_no_person_or_text(result.output.replace(TITLE, ""))  # заголовки события печатаются


def test_alert_evaluate_dispatch_fails_when_one_recipient_was_refused(stand, three_alerts_fire):
    _user(stand, BOB)

    with patch("src.notifier.send_email", side_effect=_refuses("raises", only=BOB)):
        result = CliRunner().invoke(main_mod.cli, ["alert", "evaluate", "--dispatch"])

    assert result.exit_code == 1, result.output
    assert "отправлено не всё — писем 1, сообщений Telegram 0" in result.output
    assert "отправок не состоялось: 1" in result.output
    assert "не дошедших ни по одному каналу: 0" in result.output


def test_alert_evaluate_dispatch_reports_what_the_sender_confirmed(three_alerts_fire):
    with patch("src.notifier.send_email", return_value=True):
        result = CliRunner().invoke(main_mod.cli, ["alert", "evaluate", "--dispatch"])

    assert result.exit_code == 0, result.output
    assert "→ отправлено: писем 1, сообщений Telegram 0" in result.output


def test_alert_evaluate_dispatch_reports_a_telegram_only_delivery(stand, three_alerts_fire):
    three_alerts_fire.email_severity_min = "off"
    three_alerts_fire.telegram_severity_min = "info"
    three_alerts_fire.telegram_chat_id = CHAT_ID
    stand.commit()

    with patch("src.notifier.send_telegram_message", return_value=True):
        result = CliRunner().invoke(main_mod.cli, ["alert", "evaluate", "--dispatch"])

    assert result.exit_code == 0, result.output
    assert "→ отправлено: писем 0, сообщений Telegram 1" in result.output


def test_alert_evaluate_dispatch_with_nobody_to_send_to_says_so(stand, three_alerts_fire):
    three_alerts_fire.email_severity_min = "off"
    stand.commit()

    with patch("src.notifier.send_email") as send:
        result = CliRunner().invoke(main_mod.cli, ["alert", "evaluate", "--dispatch"])

    assert not send.called
    assert result.exit_code == 0, result.output
    assert "→ никому не отправлено" in result.output
    assert "→ отправлено" not in result.output


def test_alert_evaluate_without_dispatch_sends_and_claims_nothing(three_alerts_fire):
    with patch("src.notifier.send_email") as send:
        result = CliRunner().invoke(main_mod.cli, ["alert", "evaluate"])

    assert not send.called
    assert result.exit_code == 0, result.output
    assert "→" not in result.output


# ─── 4. Письмо администраторам с тика ────────────────────────────────────────


def _admin_with_telegram(s, email=ALICE, chat_id=CHAT_ID):
    return _user(s, email, telegram_severity_min="info", telegram_chat_id=chat_id)


@pytest.mark.parametrize("how", REFUSALS)
def test_admin_letter_refusal_names_the_recipient(stand, how):
    """Отказ считался в `failed`, но кому не ушло, журнал не называл.

    `events` — сколько событий было в сообщении этого получателя, а не в тике:
    из четырёх письмо с порогом `warning` несёт три, Telegram с `critical` — два.
    """
    admin = _admin_with_telegram(stand)
    admin.email_severity_min = "warning"
    admin.telegram_severity_min = "critical"
    events = [
        *_events(stand, admin, 2),
        *_events(stand, admin, severity="warning"),
        *_events(stand, admin, severity="info"),
    ]

    with (
        patch("src.notifier.send_email", **_refusing(how)),
        patch("src.notifier.send_telegram_message", **_refusing(how)),
        capture_logs() as logs,
    ):
        counts = notifications.mail_unstored_events_to_admins(stand, events, note="тик")

    assert counts == {"email": 0, "telegram": 0, "failed": 2}
    assert logs == [
        {
            "event": "email_batch_failed",
            "log_level": "warning",
            "user_id": admin.id,
            "events": 3,
            **_error_fields(how),
        },
        {
            "event": "telegram_batch_failed",
            "log_level": "warning",
            "user_id": admin.id,
            "events": 2,
            **_error_fields(how),
        },
    ]
    _assert_no_person_or_text(logs)


@pytest.mark.parametrize("refused", [ALICE, BOB])
def test_admin_letter_names_only_the_admin_it_did_not_reach(stand, refused):
    admins = {
        ALICE: _admin_with_telegram(stand, ALICE, "700100"),
        BOB: _admin_with_telegram(stand, BOB, "700200"),
    }
    events = _events(stand, admins[ALICE])

    def telegram(chat_id, text, parse_mode="Markdown"):
        return chat_id != admins[refused].telegram_chat_id

    with (
        patch("src.notifier.send_email", side_effect=_refuses("answers_false", only=refused)),
        patch("src.notifier.send_telegram_message", side_effect=telegram),
        capture_logs() as logs,
    ):
        counts = notifications.mail_unstored_events_to_admins(stand, events, note="тик")

    assert counts == {"email": 1, "telegram": 1, "failed": 2}
    assert [(entry["event"], entry["user_id"]) for entry in logs] == [
        ("email_batch_failed", admins[refused].id),
        ("telegram_batch_failed", admins[refused].id),
    ]


def test_admin_letter_confirmed_leaves_no_failure_line(stand):
    admin = _admin_with_telegram(stand)
    events = _events(stand, admin)

    with (
        patch("src.notifier.send_email", return_value=True),
        patch("src.notifier.send_telegram_message", return_value=True),
        capture_logs() as logs,
    ):
        counts = notifications.mail_unstored_events_to_admins(stand, events, note="тик")

    assert counts == {"email": 1, "telegram": 1, "failed": 0}
    assert logs == []


def test_admin_letter_without_smtp_and_token_names_the_recipient(stand):
    """Настоящие отправители: причину пишут они, получателя — рассылка."""
    admin = _admin_with_telegram(stand)
    events = _events(stand, admin)

    with capture_logs() as logs:
        counts = notifications.mail_unstored_events_to_admins(stand, events, note="тик")

    assert counts == {"email": 0, "telegram": 0, "failed": 2}
    assert [(entry["event"], entry.get("user_id")) for entry in logs] == [
        ("email_skipped_no_smtp", None),
        ("email_batch_failed", admin.id),
        ("telegram_no_token", None),
        ("telegram_batch_failed", admin.id),
    ]


@pytest.mark.parametrize(
    ("channel", "in_message", "undelivered"), [("email", 2, 1), ("telegram", 1, 0)]
)
def test_run_letter_failure_line_carries_the_event_count_when_the_sender_raised(
    stand, channel, in_message, undelivered
):
    """RUNBOOK обещает `events=N` в строке сбоя — и когда отправитель упал.

    N — сколько событий было в сообщении этого получателя, а не в прогоне:
    из трёх событий письмо (порог `warning`) несёт два, Telegram (`critical`) — одно.
    """
    user = _user(
        stand,
        email_severity_min="warning",
        telegram_severity_min="critical",
        telegram_chat_id=CHAT_ID,
    )
    events = [
        *_events(stand, user, severity="critical"),
        *_events(stand, user, severity="warning"),
        *_events(stand, user, severity="info"),
    ]
    senders = {"email": "send_email", "telegram": "send_telegram_message"}
    other = senders["telegram" if channel == "email" else "email"]

    with (
        patch(f"src.notifier.{senders[channel]}", side_effect=_raises),
        patch(f"src.notifier.{other}", return_value=True),
        capture_logs() as logs,
    ):
        result = notifications.dispatch_events_batch(stand, events)

    assert (result["failed"], result["undelivered"]) == (1, undelivered)
    assert _lines(logs, "email_batch_failed", "telegram_batch_failed") == [
        {
            "event": f"{channel}_batch_failed",
            "log_level": "warning",
            "user_id": user.id,
            "events": in_message,
            **_error_fields("raises"),
        }
    ]


# ─── 5. Старый путь `alerts.dispatch_event` ──────────────────────────────────


def _rule(s, channels) -> storage.AlertRule:
    rule = storage.AlertRule(name="r", rule_type="site_silent", params={}, channels=channels)
    s.add(rule)
    s.commit()
    return rule


def _legacy_telegram_recipients(s, *chat_ids) -> None:
    s.add_all(
        storage.Recipient(email=f"r{i}@client.example", is_active=True, telegram_chat_id=chat_id)
        for i, chat_id in enumerate(chat_ids)
    )
    s.commit()


@pytest.mark.parametrize(
    ("how", "status"), [("answers_false", "skipped: smtp not configured"), ("raises", "error: ")]
)
def test_legacy_dispatch_records_no_channel_the_sender_did_not_confirm(stand, how, status):
    """Писались каналы правила, а не состоявшиеся отправки."""
    _legacy_telegram_recipients(stand, "700100", "700200")
    rule = _rule(stand, ["email", "telegram"])
    (event,) = _events(stand, _user(stand), rule_id=rule.id)

    with (
        patch("src.notifier.send_email", **_refusing(how)),
        patch("src.notifier.send_telegram_message", return_value=False),
        capture_logs() as logs,
    ):
        result = alerts.dispatch_event(stand, event)

    assert result["email"].startswith(status)
    assert result["telegram"] == "sent to 0/2"
    stand.refresh(event)
    assert event.channels_sent == []
    assert [entry["event"] for entry in logs] == ["alert_email_failed", "alert_telegram_failed"]
    assert logs[1] == {
        "event": "alert_telegram_failed",
        "log_level": "warning",
        "event_id": event.id,
        "failed": 2,
        "of": 2,
    }
    _assert_no_person_or_text(logs)


def test_legacy_dispatch_records_the_channels_the_sender_confirmed(stand):
    _legacy_telegram_recipients(stand, "700100", "700200")
    rule = _rule(stand, ["email", "telegram"])
    (event,) = _events(stand, _user(stand), rule_id=rule.id)

    def telegram(chat_id, text, parse_mode="Markdown"):
        return chat_id == "700200"  # одному из двух — канал состоялся

    with (
        patch("src.notifier.send_email", return_value=True),
        patch("src.notifier.send_telegram_message", side_effect=telegram),
        capture_logs() as logs,
    ):
        result = alerts.dispatch_event(stand, event)

    assert result == {"email": "sent", "telegram": "sent to 1/2"}
    stand.refresh(event)
    assert event.channels_sent == ["email", "telegram"]
    assert [(entry["event"], entry["failed"], entry["of"]) for entry in logs] == [
        ("alert_telegram_failed", 1, 2)
    ]


@pytest.mark.parametrize("telegram", ["no_chat", "raises"])
def test_legacy_dispatch_skipped_or_crashed_telegram_is_not_a_channel(stand, telegram):
    """Письмо ушло, Telegram — нет: в `channels_sent` только письмо."""
    if telegram == "raises":
        _legacy_telegram_recipients(stand, "700100")
    rule = _rule(stand, ["email", "telegram"])
    (event,) = _events(stand, _user(stand), rule_id=rule.id)

    with (
        patch("src.notifier.send_email", return_value=True),
        patch("src.notifier.send_telegram_message", side_effect=_raises),
    ):
        result = alerts.dispatch_event(stand, event)

    assert result["email"] == "sent"
    assert result["telegram"].startswith("skipped: " if telegram == "no_chat" else "error: ")
    stand.refresh(event)
    assert event.channels_sent == ["email"]


def test_single_event_dispatch_is_not_called_from_src():
    """`alerts.dispatch_event` и `notifications.dispatch_event` остались под
    тестами; рассылку ведёт `dispatch_events_batch`. Новый вызов — решение, а не
    случайность: у старого пути свои получатели (таблица `recipients`)."""
    src = Path(main_mod.__file__).resolve().parent
    callers = [
        f"{path.name}:{node.lineno}"
        for path in sorted(src.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", None)) == "dispatch_event"
    ]
    assert callers == []


def test_real_senders_answer_false_without_configuration(stand):
    """На этом стоит всё выше: без SMTP и без токена отправители не бросают."""
    with capture_logs() as logs:
        assert notifier.send_email("Тема", "<p>x</p>", to=[ALICE]) is False
        assert notifier.send_telegram_message(CHAT_ID, "x") is False
    assert [entry["event"] for entry in logs] == ["email_skipped_no_smtp", "telegram_no_token"]
