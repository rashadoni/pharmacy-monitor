"""«Отправлено» говорится только о том, что отправитель подтвердил.

`notifier.send_telegram_message` свой сбой наружу не выпускает, а отвечает
`False` (нет токена, сеть, отказ Telegram); `notifier.send_email` отвечает
`False`, когда SMTP не настроен, а отказ сервера и сеть бросает исключением.
Вызов, после которого что-то считается разосланным, обязан читать ответ.

Здесь — места, где ответ не читался или сбой терялся по дороге к оператору:
дайджест (`notifications._send_digest`, старый `digest.send_daily_digest`, их
команды и кнопка в дашборде), итог рассылки о прогоне
(`notifications.dispatch_events_batch`, конец команды `run`), `alert evaluate
--dispatch`, письмо администраторам с тика
(`notifications.mail_unstored_events_to_admins`), старый `alerts.dispatch_event`
и `report --send`. Каждое место вызывается напрямую с отправителем, который не
отправил: ответил `False`, ответил `None` или бросил исключение.
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
from sqlalchemy import event as sa_event
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
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

# Три способа, которыми отправитель сообщает «не ушло». `None` настоящий
# отправитель не отвечает, но так отвечает заглушка без `return` — и это тоже
# не подтверждение.
REFUSALS = ["answers_false", "answers_none", "raises"]


def _raises(*args, **kwargs):
    raise ConnectionRefusedError(111, SMTP_TEXT)


def _refusing(how: str) -> dict:
    """Отправитель, который не отправил, — аргументы для `patch`."""
    if how == "raises":
        return {"side_effect": _raises}
    return {"return_value": False if how == "answers_false" else None}


def _refuses(how: str, *, only: str):
    """Письмо уходит всем, кроме получателя `only`."""

    def send(subject, html_body, attachments=None, to=None):
        if to == [only]:
            if how == "raises":
                _raises()
            return False if how == "answers_false" else None
        return True

    return send


def _error_fields(how: str) -> dict:
    """Что строка сбоя несёт о причине: при отказе — ничего, отправитель написал сам."""
    return {"error_type": "ConnectionRefusedError", "errno": 111} if how == "raises" else {}


def _lines(logs: list[dict], *events: str) -> list[dict]:
    return [entry for entry in logs if entry["event"] in events]


def _assert_no_person_or_text(printed: object) -> None:
    """Ни адреса, ни идентификатора чата, ни текста сообщения или ошибки."""
    text = repr(printed)
    assert "@" not in text, text
    assert CHAT_ID not in text and TITLE not in text and "rejected" not in text, text


class _Journal:
    """Строки журнала одного модуля: вход в CLI заново настраивает structlog, и
    `capture_logs` событий команды не видит."""

    def __init__(self):
        self.lines: list[dict] = []

    def __getattr__(self, level):
        def record(event, **fields):
            self.lines.append({"event": event, "log_level": level, **fields})

        return record

    def named(self, *fragments: str) -> list[dict]:
        return [line for line in self.lines if any(part in line["event"] for part in fragments)]


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

_DIGEST_SUMMARIES = ("digest_sent", "digest_delivery_incomplete")


@pytest.mark.parametrize("how", REFUSALS)
def test_digest_nobody_received_is_not_sent(stand, how):
    """Отправитель не отправил никому: получатели не считаются, `digest_sent` нет."""
    alice, bob = _user(stand), _user(stand, BOB)
    _events(stand, alice, 3)

    with patch("src.notifier.send_email", **_refusing(how)) as send, capture_logs() as logs:
        result = notifications.send_weekly_digest(stand, tenant_id=alice.tenant_id)

    assert send.call_count == 2  # отказ одному не отменяет письмо другому
    assert result == {"recipients": 2, "sent": 0, "failed": 2}
    assert _lines(logs, "digest_email_failed", *_DIGEST_SUMMARIES) == [
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
            "event": "digest_delivery_incomplete",
            "log_level": "warning",
            "kind": "weekly",
            "sent": 0,
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
    # Ушло не всем: «digest_sent» не пишется, итог — предупреждение с обоими числами.
    assert _lines(logs, *_DIGEST_SUMMARIES) == [
        {
            "event": "digest_delivery_incomplete",
            "log_level": "warning",
            "kind": "weekly",
            "sent": 1,
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
    assert _lines(logs, "digest_email_failed", *_DIGEST_SUMMARIES) == [
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
    alice = _user(stand, daily_digest=True)
    _events(stand, alice)

    with capture_logs() as logs:
        result = notifications.send_daily_digest(stand, tenant_id=alice.tenant_id)

    assert result == {"recipients": 1, "sent": 0, "failed": 1}
    assert [entry["event"] for entry in logs] == [
        "email_skipped_no_smtp",
        "digest_email_failed",
        "digest_delivery_incomplete",
    ]
    _assert_no_person_or_text(_lines(logs, "digest_email_failed", "digest_delivery_incomplete"))


def test_digest_with_nobody_subscribed_composes_no_letter(stand):
    alice = _user(stand)  # ежедневный дайджест выключен
    _events(stand, alice)

    with patch("src.notifier.send_email") as send, capture_logs() as logs:
        result = notifications.send_daily_digest(stand, tenant_id=alice.tenant_id)

    assert not send.called
    assert result == {"recipients": 0, "sent": 0, "failed": 0}
    assert [entry["event"] for entry in logs] == ["digest_no_recipients"]


def test_digest_dry_run_counts_recipients_but_nothing_sent(stand):
    alice = _user(stand)
    _events(stand, alice)

    with patch("src.notifier.send_email") as send, capture_logs() as logs:
        result = notifications.send_weekly_digest(stand, tenant_id=alice.tenant_id, dry_run=True)

    assert not send.called
    assert result == {"recipients": 1, "sent": 0, "failed": 0}
    assert _lines(logs, *_DIGEST_SUMMARIES) == []
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


@pytest.mark.parametrize("args", [[], ["--only", "nobody@client.example"]])
def test_notify_digest_command_with_nothing_to_send_is_not_a_failure(stand, args):
    """Пустая неделя или адрес, которого нет среди подписанных: писем нет, но это
    не сбой — таймер не должен краснеть — и не «sent»."""
    alice = _user(stand)
    if args:
        _events(stand, alice)

    with patch("src.notifier.send_email") as send:
        result = CliRunner().invoke(main_mod.cli, ["notify", "digest", "weekly", *args])

    assert not send.called
    assert result.exit_code == 0, result.output
    assert "Nothing to send" in result.output
    assert "OK:" not in result.output and "sent to" not in result.output


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
    with patch("src.notifier.send_email", **sender) as send:
        response = dashboard.client.post("/api/v1/dash/digest/send-test?kind=weekly")
    assert response.status_code == 200, response.text
    return response.json(), dashboard.journal.named("digest"), send


@pytest.mark.parametrize("how", REFUSALS)
def test_digest_button_does_not_answer_sent_when_nothing_was(dashboard, how):
    answer, journal, _ = _press_digest_button(dashboard, **_refusing(how))

    assert answer == {"ok": False, "recipients": 2, "recipients_sent": 0, "recipients_failed": 2}
    assert journal == [
        {
            "event": "digest_not_sent_manual",
            "log_level": "error",
            "failed": 2,
            "kind": "weekly",
            "by_user_id": dashboard.admin.id,
        }
    ]
    _assert_no_person_or_text([answer, journal])


def test_digest_button_counts_only_confirmed_recipients(dashboard):
    partial, partial_journal, _ = _press_digest_button(
        dashboard, side_effect=_refuses("raises", only=ALICE)
    )
    clean, clean_journal, _ = _press_digest_button(dashboard, return_value=True)

    assert partial == {"ok": False, "recipients": 2, "recipients_sent": 1, "recipients_failed": 1}
    assert clean == {"ok": True, "recipients": 2, "recipients_sent": 2, "recipients_failed": 0}
    by = {"kind": "weekly", "by_user_id": dashboard.admin.id}
    # Ушло не всем — предупреждение, а не обычная строка.
    assert partial_journal == [
        {"event": "digest_sent_manual", "log_level": "warning", "count": 1, "failed": 1, **by}
    ]
    assert clean_journal == [
        {"event": "digest_sent_manual", "log_level": "info", "count": 2, "failed": 0, **by}
    ]


def test_digest_button_with_nothing_to_send_is_neither_sent_nor_failed(dashboard, stand):
    """Событий за окно нет: это не сбой почты и не «отправлен 0 получателям»."""
    stand.query(storage.AlertEvent).delete()
    stand.commit()

    answer, journal, send = _press_digest_button(dashboard)

    assert not send.called
    assert answer == {"ok": True, "recipients": 0, "recipients_sent": 0, "recipients_failed": 0}
    assert journal == [
        {
            "event": "digest_manual_nothing_to_send",
            "log_level": "info",
            "kind": "weekly",
            "by_user_id": dashboard.admin.id,
        }
    ]


def test_digest_button_copy_exists_for_every_outcome_in_every_language():
    """Кнопка читает исход и показывает свою фразу: все четыре — на трёх языках."""
    import json

    frontend = Path(main_mod.__file__).resolve().parent.parent / "frontend"
    component = (frontend / "src" / "components" / "quick-actions.tsx").read_text(encoding="utf-8")
    keys = {
        "digest_sent": {"count"},
        "digest_partly_sent": {"sent", "failed"},
        "digest_not_sent": {"failed"},
        "digest_nothing_to_send": set(),
    }
    assert "recipients_failed" in component
    for locale in ("ru", "az", "en"):
        copy = json.loads((frontend / "messages" / f"{locale}.json").read_text(encoding="utf-8"))
        for key, placeholders in keys.items():
            assert f't("{key}"' in component, key
            text = copy["quick_actions"][key]
            assert {name for name in placeholders if f"{{{name}}}" in text} == placeholders, (
                locale,
                key,
            )


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
    expected = {"event": "digest_email_failed", "log_level": "error"}
    expected.update(_error_fields(how) if how == "raises" else {"events": 2})
    assert _lines(logs, "digest_sent", "digest_email_failed") == [expected]
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


# ─── 2. Итог рассылки о прогоне ──────────────────────────────────────────────
#
# Итог пишет сама `dispatch_events_batch`, одной строкой, и имя строки зависит
# от исхода. Вызывающие (конец сбора, `alert evaluate --dispatch`) своей строки
# «разослано» не пишут.

_RUN_SUMMARIES = (
    "alerts_dispatched_batch",
    "alerts_dispatch_incomplete",
    "alerts_dispatch_no_recipient",
)


def _summary(event: str, level: str, **numbers) -> dict:
    return {"event": event, "log_level": level, **numbers}


@pytest.mark.parametrize("how", REFUSALS)
def test_run_letter_nobody_received_is_not_called_dispatched(stand, how):
    user = _user(stand)
    fired = _events(stand, user, 3)

    with patch("src.notifier.send_email", **_refusing(how)), capture_logs() as logs:
        result = notifications.dispatch_events_batch(stand, fired)

    assert result == {"email": 0, "telegram": 0, "failed": 1, "undelivered": 3}
    assert _lines(logs, *_RUN_SUMMARIES) == [
        _summary(
            "alerts_dispatch_incomplete",
            "warning",
            events=3,
            emails=0,
            telegram=0,
            failed=1,
            undelivered=3,
        )
    ]


def test_run_letter_one_refused_recipient_is_incomplete_with_what_went(stand):
    """Одному ушло, другому нет: не «разослано», а в строке — сколько всё-таки ушло."""
    user = _user(stand)
    _user(stand, BOB)
    fired = _events(stand, user, 2)

    with (
        patch("src.notifier.send_email", side_effect=_refuses("answers_false", only=BOB)),
        capture_logs() as logs,
    ):
        result = notifications.dispatch_events_batch(stand, fired)

    assert result == {"email": 1, "telegram": 0, "failed": 1, "undelivered": 0}
    assert _lines(logs, *_RUN_SUMMARIES) == [
        _summary(
            "alerts_dispatch_incomplete",
            "warning",
            events=2,
            emails=1,
            telegram=0,
            failed=1,
            undelivered=0,
        )
    ]


@pytest.mark.parametrize(
    ("email", "telegram"), [("info", "info"), ("off", "info"), ("info", "off")]
)
def test_run_letter_confirmed_is_dispatched_with_what_went(stand, email, telegram):
    """Подтверждено всё, что слали, — по обоим каналам или по одному из них."""
    user = _user(
        stand, email_severity_min=email, telegram_severity_min=telegram, telegram_chat_id=CHAT_ID
    )
    fired = _events(stand, user, 2)
    went = {"emails": int(email != "off"), "telegram": int(telegram != "off")}

    with (
        patch("src.notifier.send_email", return_value=True),
        patch("src.notifier.send_telegram_message", return_value=True),
        capture_logs() as logs,
    ):
        result = notifications.dispatch_events_batch(stand, fired)

    assert (result["failed"], result["undelivered"]) == (0, 0)
    assert _lines(logs, *_RUN_SUMMARIES) == [
        _summary("alerts_dispatched_batch", "info", events=2, **went, failed=0, undelivered=0)
    ]


def test_run_letter_with_nobody_to_send_to_is_not_called_dispatched(stand):
    """Сбоя нет, но и получателя нет (порог важности): это не «разослано»."""
    user = _user(stand, email_severity_min="critical")
    fired = _events(stand, user, 2, severity="info")

    with patch("src.notifier.send_email") as send, capture_logs() as logs:
        result = notifications.dispatch_events_batch(stand, fired)

    assert not send.called
    assert result == {"email": 0, "telegram": 0, "failed": 0, "undelivered": 0}
    assert _lines(logs, *_RUN_SUMMARIES) == [
        _summary(
            "alerts_dispatch_no_recipient",
            "info",
            events=2,
            emails=0,
            telegram=0,
            failed=0,
            undelivered=0,
        )
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


# ── Конец сбора: `main._dispatch_run_alerts` ──


@pytest.fixture
def journal(monkeypatch):
    """Журнал `src.main` и `src.notifications` — в том порядке, в каком писались."""
    recorder = _Journal()
    monkeypatch.setattr(main_mod, "log", recorder)
    monkeypatch.setattr(notifications, "log", recorder)
    return recorder


RUN_ID = 77


@pytest.fixture
def marks_cannot_be_written(stand):
    """База отказывает в записи меток канала — уже после того, как письмо ушло."""

    def refuse(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE ALERT_EVENTS"):
            raise OperationalError(
                statement, {}, Exception(f"server closed the connection {ALICE}")
            )

    engine = stand.get_bind()
    sa_event.listen(engine, "before_cursor_execute", refuse)
    yield
    sa_event.remove(engine, "before_cursor_execute", refuse)


@pytest.mark.parametrize("how", ["answers_false", "confirmed"])
def test_run_alerts_caller_writes_no_line_of_its_own(stand, journal, how):
    """«Разослано» от себя конец сбора не пишет ни при каком исходе: итог один,
    и он у рассылки."""
    fired = _events(stand, _user(stand), 2)
    sender = _refusing(how) if how != "confirmed" else {"return_value": True}

    with patch("src.notifier.send_email", **sender):
        main_mod._dispatch_run_alerts(stand, RUN_ID, fired)

    expected = "alerts_dispatched_batch" if how == "confirmed" else "alerts_dispatch_incomplete"
    assert [line["event"] for line in journal.named("alert")] == [expected]


def test_run_alerts_that_crashed_leave_the_session_usable(stand, journal, marks_cannot_be_written):
    """Рассылка упала на базе: строка сбоя без текста ошибки, а сессия сбора
    после этого работает — иначе сбор упал бы следующим же запросом."""
    fired = _events(stand, _user(stand), 2)

    with patch("src.notifier.send_email", return_value=True) as send:
        main_mod._dispatch_run_alerts(stand, RUN_ID, fired)

    assert send.call_count == 1
    assert journal.named("alert") == [
        {
            "event": "alert_dispatch_failed",
            "log_level": "warning",
            "run_id": RUN_ID,
            "events": 2,
            "error_type": "OperationalError",
        }
    ]
    _assert_no_person_or_text(journal.lines)
    # Сессия не в прерванной транзакции: читает и коммитит.
    assert stand.scalar(select(func.count()).select_from(storage.AlertEvent)) == 2
    stand.add(storage.Recipient(email="after@client.example", is_active=True))
    stand.commit()


def test_run_alerts_crash_with_a_dead_connection_does_not_raise_from_the_handler(
    stand, journal, marks_cannot_be_written, monkeypatch
):
    """Откат тоже не удался (соединения нет): причина — в журнале, из обработчика
    ничего не летит; сбор упадёт дальше сам, на своём запросе."""
    fired = _events(stand, _user(stand))

    def dead(*args, **kwargs):
        raise OperationalError("ROLLBACK", {}, Exception(f"connection is closed {ALICE}"))

    with patch("src.notifier.send_email", return_value=True):
        monkeypatch.setattr(stand, "rollback", dead)
        main_mod._dispatch_run_alerts(stand, RUN_ID, fired)

    assert [(line["event"], line["error_type"]) for line in journal.named("alert")] == [
        ("alert_dispatch_failed", "OperationalError"),
        ("alert_dispatch_rollback_failed", "OperationalError"),
    ]
    _assert_no_person_or_text(journal.lines)


def _run_that_fires_alerts(stand, monkeypatch, *extra_args, events=3):
    """Команда `run` целиком; подменён поход на сайт и подсчёт правил."""
    user = _user(stand)
    _patch_verified_aloe_pipeline(stand, monkeypatch)
    # У `run` своя сессия: события должны жить в ней, иначе канал им не записать.
    fired: list[storage.AlertEvent] = []

    def evaluate(session, run_id):
        fired.extend(_events(session, user, events))
        return list(fired)

    monkeypatch.setattr(alerts, "evaluate_rules", evaluate)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main_mod.cli, ["run", "--site", "aloe", "--mode", "category", "--force", *extra_args]
        )
    return result, fired


@pytest.mark.parametrize("how", REFUSALS)
def test_run_does_not_log_dispatched_when_the_letter_did_not_go(stand, monkeypatch, journal, how):
    """Конец настоящего сбора: отказ отправителя сбор не роняет и «разослано» не пишет."""
    with patch("src.notifier.send_email", **_refusing(how)):
        result, fired = _run_that_fires_alerts(stand, monkeypatch)

    assert result.exit_code == 0, result.output
    assert [line["event"] for line in journal.named("alerts_dispatch", "alert_dispatch")] == [
        "alerts_dispatch_incomplete"
    ]
    (summary,) = journal.named("alerts_dispatch_incomplete")
    assert (summary["events"], summary["failed"], summary["undelivered"]) == (3, 1, 3)
    assert not any(event.channels_sent for event in fired)
    assert stand.query(storage.Run).one().status == "ok"


def test_run_logs_dispatched_when_the_sender_confirmed(stand, monkeypatch, journal):
    with patch("src.notifier.send_email", return_value=True):
        result, fired = _run_that_fires_alerts(stand, monkeypatch)

    assert result.exit_code == 0, result.output
    assert [line["event"] for line in journal.named("alerts_dispatch", "alert_dispatch")] == [
        "alerts_dispatched_batch"
    ]
    assert [event.channels_sent for event in fired] == [["email"]] * 3


def test_run_survives_a_database_failure_inside_the_dispatch(
    stand, monkeypatch, journal, marks_cannot_be_written
):
    """Письмо ушло, запись меток сорвалась. Раньше следующий же запрос сбора падал
    на прерванной транзакции, обработчик сбоя — на том же, и прогон оставался
    `running`. Теперь сбор доходит до конца."""
    with patch("src.notifier.send_email", return_value=True) as send:
        result, _ = _run_that_fires_alerts(stand, monkeypatch)

    assert send.call_count == 1
    assert result.exit_code == 0, result.output
    run = stand.query(storage.Run).one()
    stand.refresh(run)
    assert run.status == "ok"
    (failure,) = journal.named("alert_dispatch")
    assert failure == {
        "event": "alert_dispatch_failed",
        "log_level": "warning",
        "run_id": run.id,
        "events": 3,
        "error_type": "OperationalError",
    }
    assert journal.named("alerts_dispatched") == []
    assert "run_failed" not in [line["event"] for line in journal.lines]


def test_run_dry_run_sends_nothing_and_says_so(stand, monkeypatch, journal):
    with patch("src.notifier.send_email") as send:
        result, _ = _run_that_fires_alerts(stand, monkeypatch, "--dry-run")

    assert result.exit_code == 0, result.output
    assert not send.called
    assert [line["event"] for line in journal.named("alerts_dispatch", "alert_dispatch")] == [
        "alerts_dispatch_skipped_dry_run"
    ]


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
    _assert_no_person_or_text(_lines(logs, "email_batch_failed", "telegram_batch_failed"))


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


@pytest.mark.parametrize("how", REFUSALS)
def test_legacy_dispatch_records_no_channel_the_sender_did_not_confirm(stand, how):
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

    assert result == {
        # Класс ошибки, а не её текст: в тексте ошибки отправки бывает адрес.
        "email": "error: ConnectionRefusedError"
        if how == "raises"
        else "skipped: smtp not configured",
        "telegram": "sent to 0/2",
    }
    stand.refresh(event)
    assert event.channels_sent == []
    assert logs == [
        {
            "event": "alert_email_failed",
            "log_level": "warning",
            "event_id": event.id,
            **_error_fields(how),
        },
        {
            "event": "alert_telegram_failed",
            "log_level": "warning",
            "event_id": event.id,
            "failed": 2,
            "of": 2,
        },
    ]
    _assert_no_person_or_text([logs, result])


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

    assert result == {
        "email": "sent",
        "telegram": "skipped: no telegram chat_ids"
        if telegram == "no_chat"
        else "error: ConnectionRefusedError",
    }
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


# ─── 6. `report --send` ──────────────────────────────────────────────────────


@pytest.fixture
def report_ready(stand, monkeypatch):
    """Проверенный полный прогон, по которому `report --send` готова слать письмо."""
    from src import roi

    now = utcnow()
    run = storage.Run(
        tenant_id=tenants.get_or_create_default(stand).id,
        status="ok",
        started_at=now,
        finished_at=now,
        catalog_scope="full",
        full_catalog_sites="aloe",
        catalog_verified=True,
        catalog_verification_reason="test",
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {"aloe": {"status": "ok"}},
        },
    )
    stand.add(run)
    stand.commit()
    monkeypatch.setattr(roi, "financial_inputs_are_fresh", lambda *a, **kw: True)
    monkeypatch.setattr(
        main_mod.analyzer, "analyze", lambda *a: SimpleNamespace(run_started_at=now)
    )
    monkeypatch.setattr(main_mod.reporter, "render_html", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "render_excel", lambda report: b"ok")
    monkeypatch.setattr(main_mod.reporter, "email_subject", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "excel_filename", lambda report: "ok.xlsx")
    return run


def _report_send(run):
    runner = CliRunner()
    with runner.isolated_filesystem():
        return runner.invoke(main_mod.cli, ["report", "--run-id", str(run.id), "--send"])


@pytest.mark.parametrize("how", REFUSALS)
def test_report_send_does_not_print_sent_when_the_letter_did_not_go(report_ready, how):
    with patch("src.notifier.send_email", **_refusing(how)) as send:
        result = _report_send(report_ready)

    assert send.call_count == 1
    assert result.exit_code != 0, result.output
    assert "Email sent." not in result.output
    if how != "raises":
        assert "the email was not sent" in result.output


def test_report_send_prints_sent_when_the_sender_confirmed(report_ready):
    with patch("src.notifier.send_email", return_value=True) as send:
        result = _report_send(report_ready)

    assert send.call_count == 1
    assert result.exit_code == 0, result.output
    assert "Email sent." in result.output


def test_real_senders_answer_false_without_configuration(stand):
    """На этом стоит всё выше: без SMTP и без токена отправители не бросают."""
    with capture_logs() as logs:
        assert notifier.send_email("Тема", "<p>x</p>", to=[ALICE]) is False
        assert notifier.send_telegram_message(CHAT_ID, "x") is False
    assert [entry["event"] for entry in logs] == ["email_skipped_no_smtp", "telegram_no_token"]
