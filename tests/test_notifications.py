"""Notification dispatch logic tests.

Covers:
  - severity threshold filtering (info < warning < critical < off)
  - quiet hours window
  - dispatch_event marks channels_sent
  - bind_telegram links chat_id by email
  - digest sends to opted-in users only
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import notifications, storage, tenants
from src._time import utcnow


@pytest.fixture
def setup(monkeypatch, db_session):
    engine = db_session.get_bind()
    Session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *a, **kw: Session)
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", "https://example.com")
    return db_session


@pytest.fixture
def tenant_user(setup):
    s = setup
    t = tenants.get_or_create_default(s)
    user = storage.TenantUser(
        tenant_id=t.id,
        email="alice@example.com",
        name="Alice",
        role="admin",
        is_active=True,
        created_at=utcnow(),
        email_severity_min="warning",
        telegram_severity_min="critical",
    )
    s.add(user)
    s.commit()
    s.refresh(user)
    return user


# ─── Severity threshold ─────────────────────────────────────────────────────


def test_severity_passes_default_warning():
    """Without explicit threshold, warnings pass for email by default."""
    assert notifications._severity_passes(None, "warning", "warning") is True
    assert notifications._severity_passes(None, "critical", "warning") is True
    assert notifications._severity_passes(None, "info", "warning") is False


def test_severity_passes_off_blocks_all():
    assert notifications._severity_passes("off", "critical", "warning") is False
    assert notifications._severity_passes("off", "info", "warning") is False


def test_severity_passes_critical_only():
    assert notifications._severity_passes("critical", "critical", "warning") is True
    assert notifications._severity_passes("critical", "warning", "warning") is False
    assert notifications._severity_passes("critical", "info", "warning") is False


def test_severity_passes_info_lets_everything_through():
    assert notifications._severity_passes("info", "info", "warning") is True
    assert notifications._severity_passes("info", "warning", "warning") is True
    assert notifications._severity_passes("info", "critical", "warning") is True


# ─── Quiet hours ────────────────────────────────────────────────────────────


def test_quiet_hours_none():
    assert notifications._in_quiet_hours(None) is False


def test_quiet_hours_overnight():
    """22-08 (overnight): quiet at 23:00, 03:00, 07:30. Awake at 09:00, 21:00."""
    qh = "22-08"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert notifications._in_quiet_hours(qh, base.replace(hour=23)) is True
    assert notifications._in_quiet_hours(qh, base.replace(hour=3)) is True
    assert notifications._in_quiet_hours(qh, base.replace(hour=7, minute=30)) is True
    assert notifications._in_quiet_hours(qh, base.replace(hour=9)) is False
    assert notifications._in_quiet_hours(qh, base.replace(hour=21, minute=59)) is False


def test_quiet_hours_daytime_window():
    """09-17 (daytime): quiet during 9-16, awake 18:00 + 06:00."""
    qh = "09-17"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert notifications._in_quiet_hours(qh, base.replace(hour=12)) is True
    assert notifications._in_quiet_hours(qh, base.replace(hour=18)) is False
    assert notifications._in_quiet_hours(qh, base.replace(hour=6)) is False


def test_quiet_hours_invalid_format():
    """Bad input shouldn't crash — return False."""
    assert notifications._in_quiet_hours("garbage") is False
    assert notifications._in_quiet_hours("25-30") is False  # parsing succeeds but logic still works


# ─── bind_telegram ──────────────────────────────────────────────────────────


def test_bind_telegram_existing_user(setup, tenant_user):
    s = setup
    bound = notifications.bind_telegram(s, "12345", "alice@example.com")
    assert bound is True
    s.refresh(tenant_user)
    assert tenant_user.telegram_chat_id == "12345"


def test_bind_telegram_unknown_email(setup, tenant_user):
    s = setup
    bound = notifications.bind_telegram(s, "99999", "nobody@example.com")
    assert bound is False


def test_bind_telegram_normalizes_email(setup, tenant_user):
    s = setup
    bound = notifications.bind_telegram(s, "555", "  ALICE@EXAMPLE.COM  ")
    assert bound is True


# ─── dispatch_event ─────────────────────────────────────────────────────────


def test_dispatch_event_marks_channels_sent(setup, tenant_user):
    s = setup
    event = storage.AlertEvent(
        rule_type="undercut_threshold",
        dedup_key="test-1",
        severity="critical",
        title="Test critical",
        tenant_id=tenant_user.tenant_id,
        created_at=utcnow(),
    )
    s.add(event)
    s.commit()

    with (
        patch("src.notifier.send_email") as mock_email,
        patch("src.notifier.send_telegram_message") as mock_tg,
    ):
        # No telegram_chat_id → only email attempted
        notifications.dispatch_event(s, event)
        assert mock_email.called
        assert not mock_tg.called

    s.refresh(event)
    assert event.channels_sent == ["email"]


def test_dispatch_event_skips_already_sent(setup, tenant_user):
    s = setup
    event = storage.AlertEvent(
        rule_type="undercut_threshold",
        dedup_key="test-2",
        severity="critical",
        title="Already sent",
        tenant_id=tenant_user.tenant_id,
        created_at=utcnow(),
        channels_sent=["email"],
    )
    s.add(event)
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        result = notifications.dispatch_event(s, event)
        assert not mock_email.called
        assert result == {"skipped": "already_sent"}


def test_dispatch_event_telegram_when_bound(setup, tenant_user):
    s = setup
    tenant_user.telegram_chat_id = "100"
    s.commit()

    event = storage.AlertEvent(
        rule_type="undercut_threshold",
        dedup_key="test-3",
        severity="critical",
        title="Telegram test",
        tenant_id=tenant_user.tenant_id,
        created_at=utcnow(),
    )
    s.add(event)
    s.commit()

    with (
        patch("src.notifier.send_email") as mock_email,
        patch("src.notifier.send_telegram_message") as mock_tg,
    ):
        notifications.dispatch_event(s, event)
        assert mock_tg.called
        assert mock_tg.call_args[0][0] == "100"


def test_dispatch_event_respects_quiet_hours(setup, tenant_user, monkeypatch):
    s = setup
    tenant_user.telegram_chat_id = "100"
    # Equal bounds denote the all-day quiet window.  ``00-23`` stops at
    # 23:00, so it made this test depend on the wall-clock time in CI.
    tenant_user.quiet_hours = "00-00"
    s.commit()

    event = storage.AlertEvent(
        rule_type="undercut_threshold",
        dedup_key="test-4",
        severity="critical",
        title="During quiet hours",
        tenant_id=tenant_user.tenant_id,
        created_at=utcnow(),
    )
    s.add(event)
    s.commit()

    with patch("src.notifier.send_email"), patch("src.notifier.send_telegram_message") as mock_tg:
        notifications.dispatch_event(s, event)
        assert not mock_tg.called


def test_dispatch_event_severity_below_threshold_skipped(setup, tenant_user):
    s = setup
    tenant_user.email_severity_min = "critical"  # only critical → email
    s.commit()

    event = storage.AlertEvent(
        rule_type="undercut_threshold",
        dedup_key="test-5",
        severity="warning",  # below threshold
        title="Low priority",
        tenant_id=tenant_user.tenant_id,
        created_at=utcnow(),
    )
    s.add(event)
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        notifications.dispatch_event(s, event)
        assert not mock_email.called


# ─── dispatch_events_batch (one email per run) ───────────────────────────────


def _mk_event(s, tenant_id, key, severity, title):
    e = storage.AlertEvent(
        rule_type="undercut_threshold",
        dedup_key=key,
        severity=severity,
        title=title,
        tenant_id=tenant_id,
        created_at=utcnow(),
    )
    s.add(e)
    return e


def test_dispatch_events_batch_one_email_for_many(setup, tenant_user):
    """3 события прогона → ОДНО письмо, все 3 внутри, каждое помечено channels_sent."""
    s = setup
    evs = [
        _mk_event(s, tenant_user.tenant_id, f"batch-{i}", "critical", f"Noreva drop {i}")
        for i in range(3)
    ]
    s.commit()

    with (
        patch("src.notifier.send_email") as mock_email,
        patch("src.notifier.send_telegram_message") as mock_tg,
    ):
        result = notifications.dispatch_events_batch(s, evs)
        assert mock_email.call_count == 1  # не 3 письма, а одно
        assert not mock_tg.called
        body = mock_email.call_args.kwargs["html_body"]
        for i in range(3):
            assert f"Noreva drop {i}" in body
        assert "3" in mock_email.call_args.kwargs["subject"]

    assert result["email"] == 1
    for e in evs:
        s.refresh(e)
        assert e.channels_sent == ["email"]


def test_dispatch_events_batch_single_event_keeps_old_subject(setup, tenant_user):
    """Одиночное событие — тема в старом single-style (без регресса)."""
    s = setup
    e = _mk_event(s, tenant_user.tenant_id, "batch-solo", "critical", "Solo alert")
    s.commit()
    with patch("src.notifier.send_email") as mock_email:
        notifications.dispatch_events_batch(s, [e])
        assert mock_email.call_count == 1
        subj = mock_email.call_args.kwargs["subject"]
        assert subj.startswith("[CRITICAL]")
        assert "Solo alert" in subj


def test_dispatch_events_batch_daily_digest_user_skipped(setup, tenant_user):
    """Юзер на daily_digest не получает real-time — событие уйдёт в дайджест."""
    s = setup
    tenant_user.daily_digest = True
    s.commit()
    e = _mk_event(s, tenant_user.tenant_id, "batch-dd", "critical", "x")
    s.commit()
    with patch("src.notifier.send_email") as mock_email:
        result = notifications.dispatch_events_batch(s, [e])
        assert not mock_email.called
        assert result["email"] == 0


def test_dispatch_events_batch_severity_filter_per_event(setup, tenant_user):
    """Порог warning: info-событие не входит в письмо и не метится channels_sent."""
    s = setup  # tenant_user.email_severity_min == "warning"
    crit = _mk_event(s, tenant_user.tenant_id, "batch-crit", "critical", "CRITTITLE")
    info = _mk_event(s, tenant_user.tenant_id, "batch-info", "info", "INFOTITLE")
    s.commit()
    with patch("src.notifier.send_email") as mock_email:
        notifications.dispatch_events_batch(s, [crit, info])
        assert mock_email.call_count == 1
        body = mock_email.call_args.kwargs["html_body"]
        assert "CRITTITLE" in body
        assert "INFOTITLE" not in body  # ниже порога → не в сводке
    s.refresh(crit)
    s.refresh(info)
    assert crit.channels_sent == ["email"]
    assert not info.channels_sent  # никому не ушло → метка не ставится


def test_dispatch_events_batch_skips_already_sent_and_empty(setup, tenant_user):
    s = setup
    already = _mk_event(s, tenant_user.tenant_id, "batch-sent", "critical", "done")
    already.channels_sent = ["email"]
    s.commit()
    with patch("src.notifier.send_email") as mock_email:
        assert notifications.dispatch_events_batch(s, [already]) == {"email": 0, "telegram": 0}
        assert notifications.dispatch_events_batch(s, []) == {"email": 0, "telegram": 0}
        assert not mock_email.called


def test_dispatch_events_batch_two_users_different_thresholds(setup, tenant_user):
    """Два получателя, разные пороги → два письма с разным содержимым."""
    s = setup  # tenant_user (alice): email_severity_min="warning"
    t = tenant_user.tenant_id
    bob = storage.TenantUser(
        tenant_id=t,
        email="bob@example.com",
        name="Bob",
        role="viewer",
        is_active=True,
        created_at=utcnow(),
        email_severity_min="critical",  # только critical
        telegram_severity_min="critical",
    )
    s.add(bob)
    crit = _mk_event(s, t, "two-crit", "critical", "CRITONLY")
    warn = _mk_event(s, t, "two-warn", "warning", "WARNONLY")
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        notifications.dispatch_events_batch(s, [crit, warn])
        assert mock_email.call_count == 2  # alice + bob — по одному письму
        bodies = {c.kwargs["to"][0]: c.kwargs["html_body"] for c in mock_email.call_args_list}
        # alice (warning) видит оба
        assert "CRITONLY" in bodies["alice@example.com"]
        assert "WARNONLY" in bodies["alice@example.com"]
        # bob (critical-only) — только critical
        assert "CRITONLY" in bodies["bob@example.com"]
        assert "WARNONLY" not in bodies["bob@example.com"]

    s.refresh(crit)
    s.refresh(warn)
    assert crit.channels_sent == ["email"]  # ушёл обоим
    assert warn.channels_sent == ["email"]  # ушёл alice → помечен


# ─── dispatch_event: отправитель ответил отказом ─────────────────────────────
#
# `send_telegram_message` свой сбой ловит сама и отвечает False (нет токена,
# сеть, отказ Telegram). Исключения при этом нет — обработчик `except` не
# срабатывает.

_CHAT_ID = "700100"
_ALERT_TITLE = "Naproksen подешевел"


def _telegram_only(user, s) -> None:
    """Почта выключена: Telegram — единственный канал получателя."""
    user.email_severity_min = "off"
    user.telegram_severity_min = "info"
    user.telegram_chat_id = _CHAT_ID
    s.commit()


def _failures(logs: list[dict], event: str) -> list[dict]:
    return [entry for entry in logs if entry["event"] == event]


def test_dispatch_event_telegram_refusal_is_not_a_delivery(setup, tenant_user):
    """Отказ отправителя: канал не пишется, счёт нулевой, в журнале сбой с user_id."""
    s = setup
    _telegram_only(tenant_user, s)
    event = _mk_event(s, tenant_user.tenant_id, "single-refused", "critical", _ALERT_TITLE)
    s.commit()

    with (
        patch("src.notifier.send_telegram_message", return_value=False) as mock_tg,
        capture_logs() as logs,
    ):
        result = notifications.dispatch_event(s, event)

    assert mock_tg.call_count == 1
    assert result == {"email": "sent_0", "telegram": "sent_0"}
    s.refresh(event)
    assert not event.channels_sent
    assert _failures(logs, "telegram_dispatch_failed") == [
        {"event": "telegram_dispatch_failed", "log_level": "warning", "user_id": tenant_user.id}
    ]
    assert _failures(logs, "alert_dispatched")[0]["telegram"] == 0
    # Ни идентификатора чата, ни текста сообщения в журнале.
    assert _CHAT_ID not in repr(logs) and _ALERT_TITLE not in repr(logs)


def test_dispatch_event_telegram_refusal_leaves_the_event_pending(setup, tenant_user):
    """Событие с единственным каналом Telegram после отказа не считается
    разосланным: повторный вызов шлёт его снова."""
    s = setup
    _telegram_only(tenant_user, s)
    event = _mk_event(s, tenant_user.tenant_id, "single-retry", "critical", "x")
    s.commit()

    with patch("src.notifier.send_telegram_message", side_effect=[False, True]) as mock_tg:
        notifications.dispatch_event(s, event)
        again = notifications.dispatch_event(s, event)

    assert mock_tg.call_count == 2
    assert again == {"email": "sent_0", "telegram": "sent_1"}
    s.refresh(event)
    assert event.channels_sent == ["telegram"]


def test_dispatch_event_telegram_refusal_keeps_the_email_mark(setup, tenant_user):
    s = setup
    tenant_user.telegram_chat_id = _CHAT_ID
    s.commit()
    event = _mk_event(s, tenant_user.tenant_id, "single-mixed", "critical", "x")
    s.commit()

    with (
        patch("src.notifier.send_email", return_value=True),
        patch("src.notifier.send_telegram_message", return_value=False),
    ):
        result = notifications.dispatch_event(s, event)

    assert result == {"email": "sent_1", "telegram": "sent_0"}
    s.refresh(event)
    assert event.channels_sent == ["email"]


def test_dispatch_event_counts_telegram_per_recipient(setup, tenant_user):
    """Одному ушло, другому отказано: счёт — один, сбой — у того, кому не ушло."""
    s = setup
    _telegram_only(tenant_user, s)
    bob = _add_user(
        s,
        tenant_user.tenant_id,
        "bob@example.com",
        "viewer",
        telegram_chat_id="222",
        telegram_severity_min="info",
    )
    bob.email_severity_min = "off"
    event = _mk_event(s, tenant_user.tenant_id, "single-two", "critical", "x")
    s.commit()

    with (
        patch("src.notifier.send_telegram_message", side_effect=lambda chat, _text: chat == "222"),
        capture_logs() as logs,
    ):
        result = notifications.dispatch_event(s, event)

    assert result == {"email": "sent_0", "telegram": "sent_1"}
    assert [e["user_id"] for e in _failures(logs, "telegram_dispatch_failed")] == [tenant_user.id]
    s.refresh(event)
    assert event.channels_sent == ["telegram"]


# ─── dispatch_events_batch: отправитель ответил отказом ──────────────────────


def _batch_summary(logs: list[dict]) -> dict:
    (summary,) = _failures(logs, "alerts_dispatched_batch")
    return summary


def test_dispatch_events_batch_telegram_refusal_is_not_a_delivery(setup, tenant_user):
    """Отказ отправителя: счётчик не растёт, канал не пишется ни одному событию,
    в журнале сбой с user_id и числом событий, не ушедших никому."""
    s = setup
    _telegram_only(tenant_user, s)
    evs = [
        _mk_event(s, tenant_user.tenant_id, f"batch-refused-{i}", "critical", _ALERT_TITLE)
        for i in range(3)
    ]
    s.commit()

    with (
        patch("src.notifier.send_telegram_message", return_value=False) as mock_tg,
        capture_logs() as logs,
    ):
        result = notifications.dispatch_events_batch(s, evs)

    assert mock_tg.call_count == 1
    assert result == {"email": 0, "telegram": 0}
    for e in evs:
        s.refresh(e)
        assert not e.channels_sent
    assert _failures(logs, "telegram_batch_failed") == [
        {
            "event": "telegram_batch_failed",
            "log_level": "warning",
            "user_id": tenant_user.id,
            "events": 3,
        }
    ]
    summary = _batch_summary(logs)
    assert (summary["telegram"], summary["failed"], summary["undelivered"]) == (0, 1, 3)
    assert _CHAT_ID not in repr(logs) and _ALERT_TITLE not in repr(logs)


def test_dispatch_events_batch_telegram_refusal_leaves_events_pending(setup, tenant_user):
    """Telegram был единственным каналом и отказал: событие остаётся
    неразосланным, и повторный вызов на тех же событиях шлёт его снова."""
    s = setup
    _telegram_only(tenant_user, s)
    e = _mk_event(s, tenant_user.tenant_id, "batch-retry", "critical", "x")
    s.commit()

    with patch("src.notifier.send_telegram_message", side_effect=[False, True]) as mock_tg:
        first = notifications.dispatch_events_batch(s, [e])
        again = notifications.dispatch_events_batch(s, [e])

    assert mock_tg.call_count == 2
    assert first == {"email": 0, "telegram": 0}
    assert again == {"email": 0, "telegram": 1}
    s.refresh(e)
    assert e.channels_sent == ["telegram"]


def test_dispatch_events_batch_telegram_refusal_keeps_the_email_mark(setup, tenant_user):
    """Письмо ушло, Telegram отказал: у события только «email»."""
    s = setup
    tenant_user.telegram_chat_id = _CHAT_ID
    s.commit()
    e = _mk_event(s, tenant_user.tenant_id, "batch-mixed", "critical", "x")
    s.commit()

    with (
        patch("src.notifier.send_email", return_value=True),
        patch("src.notifier.send_telegram_message", return_value=False),
        capture_logs() as logs,
    ):
        result = notifications.dispatch_events_batch(s, [e])

    assert result == {"email": 1, "telegram": 0}
    s.refresh(e)
    assert e.channels_sent == ["email"]
    summary = _batch_summary(logs)
    # Сбой один, но событие дошло письмом — «не ушедших никому» нет.
    assert (summary["failed"], summary["undelivered"]) == (1, 0)


def test_dispatch_events_batch_marks_only_what_telegram_took(setup, tenant_user):
    """Метка ставится по событию: что входило только в отказанное сообщение,
    остаётся без канала, даже если другому получателю Telegram доставил."""
    s = setup
    t = tenant_user.tenant_id
    tenant_user.email_severity_min = "off"
    tenant_user.telegram_chat_id = _CHAT_ID  # порог critical, ему доставят
    bob = _add_user(
        s, t, "bob@example.com", "viewer", telegram_chat_id="222", telegram_severity_min="warning"
    )
    bob.email_severity_min = "off"
    crit = _mk_event(s, t, "batch-only-crit", "critical", "CRITONLY")
    warn = _mk_event(s, t, "batch-only-warn", "warning", "WARNONLY")
    info = _mk_event(s, t, "batch-only-info", "info", "INFOONLY")  # ниже порогов обоих
    s.commit()

    with (
        patch(
            "src.notifier.send_telegram_message", side_effect=lambda chat, _text: chat == _CHAT_ID
        ) as mock_tg,
        capture_logs() as logs,
    ):
        result = notifications.dispatch_events_batch(s, [crit, warn, info])

    assert mock_tg.call_count == 2
    assert result == {"email": 0, "telegram": 1}
    # В сбое — сколько событий было в сообщении этого получателя, а не в прогоне.
    assert [(f["user_id"], f["events"]) for f in _failures(logs, "telegram_batch_failed")] == [
        (bob.id, 2)
    ]
    for e in (crit, warn, info):
        s.refresh(e)
    assert crit.channels_sent == ["telegram"]
    assert not warn.channels_sent  # предупреждение ждал только bob
    assert not info.channels_sent
    summary = _batch_summary(logs)
    assert (summary["telegram"], summary["failed"], summary["undelivered"]) == (1, 1, 1)


def test_dispatch_events_batch_without_a_token_delivers_nothing(setup, tenant_user, monkeypatch):
    """Настоящий отправитель без токена — как на проде, где Telegram не настроен."""
    s = setup
    _telegram_only(tenant_user, s)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")

    def no_network(*args, **kwargs):
        raise AssertionError("без токена запроса к Telegram быть не должно")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    e = _mk_event(s, tenant_user.tenant_id, "batch-no-token", "critical", "x")
    s.commit()

    with capture_logs() as logs:
        result = notifications.dispatch_events_batch(s, [e])

    assert result == {"email": 0, "telegram": 0}
    s.refresh(e)
    assert not e.channels_sent
    # Причину называет отправитель, получателя — рассылка.
    names = [entry["event"] for entry in logs]
    assert names.index("telegram_no_token") < names.index("telegram_batch_failed")
    assert _failures(logs, "telegram_batch_failed")[0]["user_id"] == tenant_user.id


def test_dispatch_events_batch_clean_run_reports_no_failures(setup, tenant_user):
    s = setup
    tenant_user.telegram_chat_id = _CHAT_ID
    s.commit()
    crit = _mk_event(s, tenant_user.tenant_id, "batch-clean", "critical", "x")
    info = _mk_event(s, tenant_user.tenant_id, "batch-clean-info", "info", "y")
    s.commit()

    with (
        patch("src.notifier.send_email", return_value=True),
        patch("src.notifier.send_telegram_message", return_value=True),
        capture_logs() as logs,
    ):
        result = notifications.dispatch_events_batch(s, [crit, info])

    assert result == {"email": 1, "telegram": 1}
    assert _failures(logs, "telegram_batch_failed") == []
    summary = _batch_summary(logs)
    # info ниже порогов получателя: его никому и не слали — это не «не ушло».
    assert (summary["failed"], summary["undelivered"]) == (0, 0)


# ─── mail_unstored_events_to_admins (события частичного сбора) ───────────────


def _add_user(s, tenant_id, email, role, **fields):
    user = storage.TenantUser(
        tenant_id=tenant_id,
        email=email,
        role=role,
        is_active=True,
        created_at=utcnow(),
        email_severity_min="warning",
        **fields,
    )
    s.add(user)
    return user


def test_unstored_events_reach_admins_and_never_viewers(setup, tenant_user):
    """Письмо и Telegram — только admin/owner; сотрудник клиента не получает ничего."""
    s = setup  # tenant_user (alice) — admin, Telegram не привязан
    t = tenant_user.tenant_id
    # У сотрудника клиента привязан Telegram и самый низкий порог: если бы круг
    # получателей не резался по роли, сообщение ушло бы и ему.
    _add_user(
        s, t, "bob@example.com", "viewer", telegram_chat_id="111", telegram_severity_min="info"
    )
    _add_user(
        s, t, "carol@example.com", "owner", telegram_chat_id="222", telegram_severity_min="info"
    )
    _add_user(s, t, "gone@example.com", "admin").is_active = False
    s.commit()
    # Событие вне базы — так приходят изменения цены с частичного сбора.
    event = _loose_event("price_drop_pct", "critical", "Цена упала на 20.0%: X")

    with (
        patch("src.notifier.send_email") as mock_email,
        patch("src.notifier.send_telegram_message") as mock_tg,
    ):
        result = notifications.mail_unstored_events_to_admins(
            s, [event], note="Частичный сбор aloe <b>, только администраторам"
        )

    assert sorted(c.kwargs["to"][0] for c in mock_email.call_args_list) == [
        "alice@example.com",
        "carol@example.com",
    ]
    assert [c.args[0] for c in mock_tg.call_args_list] == ["222"]
    assert "Только администраторам" in mock_tg.call_args.args[1]
    assert result == {"email": 2, "telegram": 1, "failed": 0}
    body = mock_email.call_args.kwargs["html_body"]
    assert "Частичный сбор aloe &lt;b&gt;, только администраторам" in body
    # В журнал событие не попало, и рассылка ничего о нём не записала.
    assert s.query(storage.AlertEvent).count() == 0
    assert event.channels_sent is None


def test_unstored_events_ignore_the_daily_digest_opt_out(setup, tenant_user):
    """Админ «на дайджесте» письмо всё равно получает: в дайджест такое событие
    не попадёт — его нет в базе, а обычная рассылка его бы молча пропустила."""
    s = setup
    tenant_user.daily_digest = True
    s.commit()
    event = _loose_event("price_drop_pct", "critical", "Цена упала")

    with patch("src.notifier.send_email") as mock_email:
        assert notifications.dispatch_events_batch(s, [event])["email"] == 0
        event.channels_sent = None
        result = notifications.mail_unstored_events_to_admins(s, [event], note="n")

    assert mock_email.call_count == 1
    assert result["email"] == 1


def test_unstored_events_respect_the_admin_severity_threshold(setup, tenant_user):
    s = setup
    tenant_user.email_severity_min = "critical"
    s.commit()
    warning = _loose_event("price_drop_pct", "warning", "WARNONLY")
    critical = _loose_event("price_drop_pct", "critical", "CRITONLY")

    with patch("src.notifier.send_email") as mock_email:
        result = notifications.mail_unstored_events_to_admins(s, [warning, critical], note="n")
        body = mock_email.call_args.kwargs["html_body"]
        assert "CRITONLY" in body and "WARNONLY" not in body

        tenant_user.email_severity_min = "off"
        s.commit()
        silent = notifications.mail_unstored_events_to_admins(s, [warning, critical], note="n")

    assert result == {"email": 1, "telegram": 0, "failed": 0}
    assert silent == {"email": 0, "telegram": 0, "failed": 0}
    assert mock_email.call_count == 1


def test_unstored_events_keep_telegram_quiet_hours(setup, tenant_user, monkeypatch):
    """Тихие часы админа действуют: Telegram молчит, письмо уходит."""
    s = setup
    tenant_user.telegram_chat_id = "777"
    tenant_user.telegram_severity_min = "info"
    tenant_user.quiet_hours = "22-08"
    s.commit()
    event = _loose_event("price_drop_pct", "critical", "Цена упала")
    monkeypatch.setattr(
        notifications, "_in_quiet_hours", lambda quiet_hours: quiet_hours == "22-08"
    )

    with (
        patch("src.notifier.send_email") as mock_email,
        patch("src.notifier.send_telegram_message") as mock_tg,
    ):
        result = notifications.mail_unstored_events_to_admins(s, [event], note="n")

    assert mock_email.call_count == 1
    assert not mock_tg.called
    assert result == {"email": 1, "telegram": 0, "failed": 0}


def test_unstored_events_report_a_letter_that_did_not_go(setup, tenant_user):
    """Сбой почты не бросает исключение, но и «отправлено» не считается."""
    s = setup
    event = _loose_event("price_drop_pct", "critical", "Цена упала")

    with patch("src.notifier.send_email", side_effect=ConnectionRefusedError("smtp down")):
        refused = notifications.mail_unstored_events_to_admins(s, [event], note="n")
    # SMTP не настроен: send_email возвращает False, письма не было.
    with patch("src.notifier.send_email", return_value=False):
        unconfigured = notifications.mail_unstored_events_to_admins(s, [event], note="n")

    assert refused == {"email": 0, "telegram": 0, "failed": 1}
    assert unconfigured == {"email": 0, "telegram": 0, "failed": 1}


def test_dispatch_events_batch_reaches_every_role(setup, tenant_user):
    """Обычная рассылка о прогоне по-прежнему идёт всем и без пояснения."""
    s = setup
    _add_user(s, tenant_user.tenant_id, "bob@example.com", "viewer")
    event = _mk_event(s, tenant_user.tenant_id, "everyone", "critical", "For all")
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        notifications.dispatch_events_batch(s, [event])

    assert sorted(c.kwargs["to"][0] for c in mock_email.call_args_list) == [
        "alice@example.com",
        "bob@example.com",
    ]
    assert "#fffbeb" not in mock_email.call_args.kwargs["html_body"]


def test_dispatch_events_batch_all_below_threshold_redispatchable(setup, tenant_user):
    """Все события ниже порога → 0 писем и channels_sent НЕ ставится (re-dispatch)."""
    s = setup
    tenant_user.email_severity_min = "critical"  # только critical
    s.commit()
    warn = _mk_event(s, tenant_user.tenant_id, "below-warn", "warning", "x")
    info = _mk_event(s, tenant_user.tenant_id, "below-info", "info", "y")
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        result = notifications.dispatch_events_batch(s, [warn, info])
        assert not mock_email.called
        assert result == {"email": 0, "telegram": 0}

    s.refresh(warn)
    s.refresh(info)
    assert not warn.channels_sent  # никому не ушло → можно отправить позже/в дайджест
    assert not info.channels_sent


# ─── Digest ─────────────────────────────────────────────────────────────────


def test_send_daily_digest_no_recipients(setup, tenant_user):
    """User without daily_digest=True → no email."""
    s = setup
    with patch("src.notifier.send_email") as mock_email:
        sent = notifications.send_daily_digest(s, tenant_id=tenant_user.tenant_id)
        assert sent == 0
        assert not mock_email.called


def test_send_daily_digest_with_events(setup, tenant_user):
    s = setup
    tenant_user.daily_digest = True
    run = storage.Run(
        tenant_id=tenant_user.tenant_id,
        status="ok",
        started_at=utcnow(),
        finished_at=utcnow(),
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
    s.add(run)
    s.flush()
    s.add(
        storage.AlertEvent(
            rule_type="undercut_threshold",
            dedup_key="d-1",
            severity="warning",
            title="Today event",
            tenant_id=tenant_user.tenant_id,
            created_at=utcnow(),
            payload={"source_run_id": run.id},
        )
    )
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        sent = notifications.send_daily_digest(s, tenant_id=tenant_user.tenant_id)
        assert sent == 1
        assert mock_email.called


def test_send_digest_skips_legacy_financial_event_without_verified_run(setup, tenant_user):
    s = setup
    tenant_user.daily_digest = True
    s.add(
        storage.AlertEvent(
            rule_type="undercut_threshold",
            dedup_key="legacy-unverified",
            severity="critical",
            title="Legacy event",
            tenant_id=tenant_user.tenant_id,
            created_at=utcnow(),
        )
    )
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        assert notifications.send_daily_digest(s, tenant_id=tenant_user.tenant_id) == 0
        assert not mock_email.called


def test_send_daily_digest_skips_old_events(setup, tenant_user):
    """Events older than 24h not included — if all old, no email."""
    s = setup
    tenant_user.daily_digest = True
    s.add(
        storage.AlertEvent(
            rule_type="undercut_threshold",
            dedup_key="d-old",
            severity="warning",
            title="Old event",
            tenant_id=tenant_user.tenant_id,
            created_at=utcnow() - timedelta(days=3),
        )
    )
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        sent = notifications.send_daily_digest(s, tenant_id=tenant_user.tenant_id)
        assert sent == 0
        assert not mock_email.called


# ─── Digest: сводка и ограниченные списки ───────────────────────────────────

# Письмо больше 102 КБ Gmail сворачивает («Message clipped»).
GMAIL_CLIP_BYTES = 102 * 1024
NBSP = " "


def _eligible_run(s, tenant_id):
    """Полный проверенный прогон — без него финансовые события в дайджест не идут."""
    run = storage.Run(
        tenant_id=tenant_id,
        status="ok",
        started_at=utcnow(),
        finished_at=utcnow(),
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
    s.add(run)
    s.flush()
    return run


def _digest_event(s, run, rule_type, severity, title, **payload):
    e = storage.AlertEvent(
        rule_type=rule_type,
        dedup_key=f"digest|{title}",
        severity=severity,
        title=title,
        detail=f"detail of {title}",
        tenant_id=run.tenant_id,
        created_at=utcnow(),
        payload={**payload, "source_run_id": run.id},
    )
    s.add(e)
    return e


def _loose_event(rule_type, severity, title, payload=None, detail=None):
    """Событие вне базы — для проверок самого рендера."""
    return storage.AlertEvent(
        rule_type=rule_type,
        dedup_key=f"loose|{title}",
        severity=severity,
        title=title,
        detail=detail,
        tenant_id=1,
        created_at=utcnow(),
        payload=payload,
    )


def _event_rows(html: str) -> int:
    """Сколько событий письмо показывает строками."""
    return html.count(notifications._EVENT_ROW_MARK)


def _weekly_bodies(s, tenant_id) -> tuple[dict[str, str], str]:
    with patch("src.notifier.send_email") as mock_email:
        notifications.send_weekly_digest(s, tenant_id=tenant_id)
    bodies = {c.kwargs["to"][0]: c.kwargs["html_body"] for c in mock_email.call_args_list}
    subject = mock_email.call_args.kwargs["subject"] if mock_email.called else ""
    return bodies, subject


def test_weekly_digest_big_week_is_summary_plus_capped_lists(setup, tenant_user):
    """Неделя на 2 039 событий (как после добавления разделов aloe) — письмо со
    сводкой и списком до потолка, а не строка на каждое событие."""
    s = setup
    tenant_user.weekly_digest = True
    run = _eligible_run(s, tenant_user.tenant_id)
    for i in range(1900):
        _digest_event(s, run, "new_product", "info", f"Новый товар на aloe: N{i}", site="aloe")
    for i in range(9):
        _digest_event(s, run, "new_product", "info", f"Новый товар на ph: N{i}", site="pharmonline")
    for i in range(85):
        # Проценты идут вперемешку с порядком создания: «самые крупные» нельзя
        # получить, просто взяв первые или последние события.
        pct = 20 + (i * 37) % 85
        _digest_event(
            s,
            run,
            "price_drop_pct",
            "critical",
            f"DROPCRIT{pct:03d}",
            site="aptekonline",
            drop_pct=pct,
        )
    for i in range(22):
        # Предупреждения «крупнее» критичных — и всё равно идут после них.
        _digest_event(
            s,
            run,
            "price_drop_pct",
            "warning",
            f"DROPWARN{i:02d}",
            site="aptekonline",
            drop_pct=200 + i,
        )
    for i in range(5):
        _digest_event(
            s, run, "undercut_threshold", "critical", f"UNDERCUT{i}", site="aloe", diff_pct=10 + i
        )
    for i in range(18):
        _digest_event(s, run, "price_raise_opportunity", "info", f"RAISE{i:02d}", gap_pct=7 + i)
    s.commit()

    bodies, subject = _weekly_bodies(s, tenant_user.tenant_id)
    body = bodies["alice@example.com"]

    assert len(bodies) == 1
    assert len(body.encode()) < GMAIL_CLIP_BYTES
    assert _event_rows(body) == 5 + notifications._DIGEST_TYPE_CAP + 18

    # Шапка и сводка: каждый тип одной строкой, новые товары — числом по сайтам.
    assert f"2{NBSP}039 событий: 90 критичных, 22 предупреждения, 1{NBSP}927 информационных" in body
    assert f"aloe 1{NBSP}900, pharmonline 9" in body
    assert f">1{NBSP}909</td>" in body
    assert "85 критичных, 22 предупреждения · aptekonline 107" in body
    assert ">107</td>" in body
    assert "Можно поднять цену" in body
    # …и ни один из 1 909 новых товаров строкой.
    assert "Новый товар на" not in body
    # «Можно поднять цену» помещается целиком — идёт строками, крупные сверху.
    assert body.count("detail of RAISE") == 18
    assert body.index("RAISE17") < body.index("RAISE00")

    # Малочисленное, но главное («конкурент дешевле») не вытеснено сотней падений цены.
    for i in range(5):
        assert f"UNDERCUT{i}" in body
    # Из падений цены — 30 самых крупных критичных, по убыванию.
    for pct in range(75, 105):
        assert f"DROPCRIT{pct:03d}" in body
    assert "DROPCRIT074" not in body
    assert body.index("DROPCRIT104") < body.index("DROPCRIT103") < body.index("DROPCRIT075")
    assert "DROPWARN" not in body
    # Остальное — за ссылкой с тем же окном и фильтром по типу.
    assert "30 из 107" in body
    assert "Ещё 77 — в дашборде" in body
    # Ссылка с фильтром по типу стоит и в сводке, и под обрезанным списком.
    assert body.count('href="https://example.com/alerts?hours=168&amp;type=price_drop_pct"') == 2
    # При равной важности типы идут в порядке подписей.
    summary = [
        body.index(f">{label}</a>")
        for label in ("Конкурент дешевле", "Цена упала", "Можно поднять цену", "Новые товары")
    ]
    assert summary == sorted(summary)
    # У типа без строк нет и блока — только строка сводки.
    assert "Новые товары <span" not in body
    assert "Цена упала <span" in body
    assert 'href="https://example.com/alerts?hours=168"' in body
    assert f"53 из 2{NBSP}039, самые важные" in body

    assert subject == (
        f"Pharmacy Monitor — дайджест за неделю: 2{NBSP}039 событий, из них 90 критичных"
    )


def test_weekly_digest_small_week_lists_every_event(setup, tenant_user):
    """Мало событий — как раньше: каждое строкой, ничего не спрятано за ссылку."""
    s = setup  # порог alice — warning; на дайджест он не действует
    tenant_user.weekly_digest = True
    run = _eligible_run(s, tenant_user.tenant_id)
    titles = []
    for i in range(3):
        titles.append(f"UNDERCUT{i}")
        _digest_event(s, run, "undercut_threshold", "critical", titles[-1], site="aloe")
    for i in range(4):
        titles.append(f"DROPWARN{i}")
        _digest_event(s, run, "price_drop_pct", "warning", titles[-1], site="aptekonline")
    for i in range(5):
        titles.append(f"NEWPRODUCT{i}")
        _digest_event(s, run, "new_product", "info", titles[-1], site="aloe")
    for i in range(2):
        titles.append(f"RAISE{i}")
        _digest_event(s, run, "price_raise_opportunity", "info", titles[-1])
    s.commit()

    bodies, subject = _weekly_bodies(s, tenant_user.tenant_id)
    body = bodies["alice@example.com"]

    assert _event_rows(body) == len(titles) == 14
    for title in titles:
        assert title in body
        assert f"detail of {title}" in body
    assert "Ещё " not in body
    assert "самые важные" not in body
    assert subject == "Pharmacy Monitor — дайджест за неделю: 14 событий, из них 3 критичных"


def test_weekly_digest_ignores_recipient_threshold(setup, tenant_user):
    """«Порог email-уведомлений» получателя на дайджест не действует (решение
    владельца 2026-10-07): письмо у всех одно, информационные события в нём есть."""
    s = setup  # alice: warning
    tenant_user.weekly_digest = True
    for email, threshold in (
        ("bob@example.com", "critical"),
        ("carol@example.com", "off"),
        ("dave@example.com", None),
    ):
        s.add(
            storage.TenantUser(
                tenant_id=tenant_user.tenant_id,
                email=email,
                role="viewer",
                is_active=True,
                created_at=utcnow(),
                email_severity_min=threshold,
                weekly_digest=True,
            )
        )
    run = _eligible_run(s, tenant_user.tenant_id)
    _digest_event(s, run, "undercut_threshold", "critical", "CRITTITLE", site="aloe")
    _digest_event(s, run, "new_product", "info", "INFOTITLE", site="aloe")
    s.commit()

    bodies, _ = _weekly_bodies(s, tenant_user.tenant_id)

    assert len(bodies) == 4
    assert len(set(bodies.values())) == 1
    assert "CRITTITLE" in bodies["bob@example.com"]
    assert "INFOTITLE" in bodies["bob@example.com"]


def test_digest_rows_go_to_critical_of_every_type_first():
    """Предупреждения одного типа не вытесняют критичные другого, а большой
    тип не оставляет маленький без строк."""
    events = (
        [_loose_event("undercut_threshold", "critical", f"UC_CRIT{i:02d}") for i in range(12)]
        + [_loose_event("undercut_threshold", "warning", f"UC_WARN{i:02d}") for i in range(40)]
        + [_loose_event("price_drop_pct", "critical", f"PD_CRIT{i:02d}") for i in range(20)]
        + [_loose_event("price_drop_pct", "warning", f"PD_WARN{i:02d}") for i in range(30)]
        + [_loose_event("price_change_pct", "critical", f"PC_CRIT{i:02d}") for i in range(25)]
        + [_loose_event("site_drop_smoke", "warning", "SITEDROP")]
    )
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert _event_rows(html) == notifications._DIGEST_ROW_BUDGET == 60
    assert html.count("UC_CRIT") == 12
    assert html.count("PD_CRIT") == 20
    assert html.count("PC_CRIT") == 25
    # Три оставшиеся строки — по одной каждому типу с предупреждениями.
    assert html.count("UC_WARN") == html.count("PD_WARN") == html.count("SITEDROP") == 1


def test_digest_rows_are_shared_between_types_of_same_severity():
    """Критичных больше, чем строк: каждому типу достаётся доля, малому — всё."""
    events = (
        [_loose_event("undercut_threshold", "critical", f"UC{i:02d}") for i in range(40)]
        + [_loose_event("price_drop_pct", "critical", f"PD{i:02d}") for i in range(40)]
        + [_loose_event("price_change_pct", "critical", f"PC{i:02d}") for i in range(5)]
    )
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert html.count("PC") == 5
    assert sorted([html.count("UC"), html.count("PD")]) == [27, 28]
    assert _event_rows(html) == 60


def test_digest_info_type_is_listed_whole_or_counted():
    """Информационный тип без процента: помещается — целиком, нет — числом."""
    fits = [
        _loose_event("price_raise_opportunity", "info", f"RAISE{i:02d}")
        for i in range(notifications._DIGEST_TYPE_CAP)
    ]
    overflows = [
        _loose_event("new_product", "info", f"NEWPRODUCT{i:02d}", {"site": "aloe"})
        for i in range(notifications._DIGEST_TYPE_CAP + 1)
    ]
    html = notifications._render_digest_email(fits + overflows, kind="weekly", since=utcnow())

    assert html.count("RAISE") == 30
    assert "NEWPRODUCT" not in html
    assert "aloe 31" in html  # не поместившийся тип остался числом в сводке
    assert "30 из 61, самые важные" in html


def test_digest_info_type_is_not_cut_to_fit_remaining_rows():
    """Строк осталось меньше, чем событий в информационном типе, — он идёт числом."""
    events = (
        [_loose_event("undercut_threshold", "critical", f"UC{i:02d}") for i in range(28)]
        + [_loose_event("price_drop_pct", "critical", f"PD{i:02d}") for i in range(28)]
        + [_loose_event("new_product", "info", f"NEWPRODUCT{i:02d}") for i in range(10)]
    )
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert _event_rows(html) == 56
    assert "NEWPRODUCT" not in html
    assert ">10</td>" in html


@pytest.mark.parametrize("new_products", [7, 20, 26, 31])
def test_digest_info_type_that_does_not_fit_takes_no_rows_from_the_next(new_products):
    """После 35 критичных осталось 25 строк. 18 «можно поднять цену» идут
    строками при любом числе новых товаров; новые товары — только если влезают
    следом целиком (7), иначе числом (20, 26, 31)."""
    events = (
        [_loose_event("undercut_threshold", "critical", f"UC{i:02d}") for i in range(5)]
        + [_loose_event("price_drop_pct", "critical", f"PD{i:02d}") for i in range(85)]
        + [_loose_event("price_raise_opportunity", "info", f"RAISE{i:02d}") for i in range(18)]
        + [_loose_event("new_product", "info", f"NEWPRODUCT{i:02d}") for i in range(new_products)]
    )
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert html.count("RAISE") == 18
    assert html.count("NEWPRODUCT") == (7 if new_products == 7 else 0)


def test_digest_ranked_info_type_shows_largest_when_letter_is_short_of_rows():
    """Живой случай 2026-10-07: после 37 критичных осталось 23 строки, а «можно
    поднять цену» — 28. Тип меньше потолка и ранжируется по проценту, поэтому
    идут 23 самых крупных, а не одно число."""
    events = (
        [_loose_event("undercut_threshold", "critical", f"UC{i:02d}") for i in range(7)]
        + [_loose_event("price_drop_pct", "critical", f"PD{i:03d}") for i in range(136)]
        + [
            _loose_event("price_raise_opportunity", "info", f"RAISE{i:02d}", {"gap_pct": 7 + i})
            for i in range(28)
        ]
        + [_loose_event("new_product", "info", f"NEWPRODUCT{i:04d}") for i in range(3043)]
    )
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert _event_rows(html) == 60
    assert html.count("RAISE") == 23
    assert "RAISE27" in html and "RAISE05" in html
    assert "RAISE04" not in html
    assert "Можно поднять цену <span" in html and "23 из 28" in html
    assert "Ещё 5 — в дашборде" in html
    assert "NEWPRODUCT" not in html


def test_digest_ranked_info_type_above_cap_shows_largest():
    """«Можно поднять цену» больше потолка — 30 самых крупных, а не одно число."""
    events = [
        _loose_event("price_raise_opportunity", "info", f"RAISE{i:02d}", {"gap_pct": 7 + i})
        for i in range(45)
    ]
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert html.count("RAISE") == notifications._DIGEST_TYPE_CAP == 30
    assert "RAISE44" in html and "RAISE15" in html and "RAISE14" not in html
    assert "30 из 45" in html and "Ещё 15 — в дашборде" in html


def test_digest_type_cap_counts_rows_of_every_severity():
    """Потолок 30 — на тип целиком: критичные и информационные одного типа вместе."""
    events = [_loose_event("price_change_pct", "critical", f"CRIT{i:02d}") for i in range(20)] + [
        _loose_event("price_change_pct", "info", f"INFO{i:02d}", {"change_pct": i + 1})
        for i in range(15)
    ]
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert html.count("CRIT") == 20
    assert html.count("INFO") == 10
    assert "INFO14" in html and "INFO05" in html and "INFO04" not in html


def test_digest_info_type_is_ranked_only_by_percent():
    """Осталось 10 строк. «Ранжируемый» — тот, у событий которого есть процент:
    хватает и части событий, а payload без процента (как у новых товаров) типом
    с размером тип не делает. Не влезший тип не закрывает дорогу следующему."""
    critical = [_loose_event("price_drop_pct", "critical", f"PD{i:02d}") for i in range(25)] + [
        _loose_event("undercut_threshold", "critical", f"UC{i:02d}") for i in range(25)
    ]
    partly_ranked = [
        _loose_event("price_raise_opportunity", "info", f"RAISE{i:02d}", {"gap_pct": i})
        for i in range(1, 15)
    ] + [_loose_event("price_raise_opportunity", "info", "RAISE_NO_PCT", {"match_id": 1})]
    unranked = [
        _loose_event("new_product", "info", f"NEWPRODUCT{i:02d}", {"site": "aloe", "product_id": i})
        for i in range(12)
    ]
    few = [
        _loose_event("some_future_rule", "info", f"FUTURE{i}", {"site": "aloe"}) for i in range(3)
    ]

    html = notifications._render_digest_email(
        critical + partly_ranked + few, kind="weekly", since=utcnow()
    )
    assert html.count("RAISE") == 10
    assert "RAISE14" in html and "RAISE05" in html and "RAISE04" not in html
    assert "FUTURE" not in html  # строки кончились

    html = notifications._render_digest_email(
        critical + unranked + few, kind="weekly", since=utcnow()
    )
    assert "NEWPRODUCT" not in html
    assert html.count("FUTURE") == 3  # следующий тип помещается и идёт строками


def test_digest_last_rows_go_to_smallest_type_first():
    """Остаток меньше числа типов — единственное предупреждение о сбое сбора
    не теряется за десятками предупреждений о ценах."""
    events = (
        [_loose_event("price_change_pct", "critical", f"PC{i:02d}") for i in range(29)]
        + [_loose_event("price_drop_pct", "critical", f"PD{i:02d}") for i in range(29)]
        + [_loose_event("undercut_threshold", "warning", f"UC{i:02d}") for i in range(40)]
        + [_loose_event("promo_started", "warning", f"PROMO{i:02d}") for i in range(30)]
        + [_loose_event("site_drop_smoke", "warning", "SITEDROP")]
    )
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert _event_rows(html) == 60
    assert "SITEDROP" in html
    assert html.count("UC") + html.count("PROMO") == 1


def test_digest_ranks_by_size_of_change_in_either_direction():
    events = [
        _loose_event("price_change_pct", "critical", f"CHANGE{i:02d}", {"change_pct": 20 + i})
        for i in range(30)
    ] + [_loose_event("price_change_pct", "critical", "BIGFALL", {"change_pct": -70})]
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert "BIGFALL" in html
    assert "CHANGE00" not in html
    assert html.index("BIGFALL") < html.index("CHANGE29")


def test_digest_with_nothing_to_list_says_so():
    events = [_loose_event("new_product", "info", f"NEWPRODUCT{i:02d}") for i in range(31)]
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert _event_rows(html) == 0
    assert "В этом письме только сводка" in html
    assert "самые важные" not in html


def test_digest_orders_types_by_worst_severity():
    """Тип с критичным событием идёт выше, даже если в списке подписей он ниже."""
    events = [
        _loose_event("undercut_threshold", "warning", "UNDERCUT"),
        _loose_event("new_product", "info", "NEWPRODUCT"),
        _loose_event("site_drop_smoke", "critical", "SITEDROP"),
    ]
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    labels = ["Сайт собран не полностью", "Конкурент дешевле", "Новые товары"]
    positions = [html.index(f">{label}</a>") for label in labels]
    assert positions == sorted(positions)
    assert html.index("SITEDROP") < html.index("UNDERCUT") < html.index("NEWPRODUCT")


def test_digest_header_shows_period(monkeypatch):
    monkeypatch.setattr(notifications, "utcnow", lambda: datetime(2026, 10, 12, 6, 0))
    html = notifications._render_digest_email(
        [_loose_event("new_product", "info", "X")], kind="weekly", since=datetime(2026, 10, 5, 6, 0)
    )
    assert "05.10 – 12.10 · 1 событие: 1 информационное" in html


def test_digest_size_is_bounded_by_row_budget():
    """Сколько бы событий и типов ни пришло, строк не больше бюджета, и на
    длинных кириллических названиях письмо остаётся под порогом Gmail."""
    events = [
        _loose_event(rule_type, "critical", "Ж" * 500, {"site": "aloe", "drop_pct": i}, "Щ" * 5000)
        for rule_type in [*notifications._RULE_TYPE_LABELS, "some_future_rule"]
        for i in range(100)
    ]
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert _event_rows(html) == notifications._DIGEST_ROW_BUDGET
    assert len(html.encode()) < GMAIL_CLIP_BYTES
    assert "Ж" * 199 + "…" in html and "Ж" * 200 not in html
    assert ">some_future_rule</a>" in html  # незнакомый тип виден под своим именем


def test_digest_escapes_scraped_text():
    """Имя товара, сайт и тип приходят извне — в письме это текст, а не разметка."""
    event = _loose_event(
        "a&b <x>",
        "info",
        'Новый товар: <a href="https://evil.example">Johnson & Johnson</a>',
        {"site": "<i>site</i>"},
        "<b>жирный</b>",
    )
    html = notifications._render_digest_email([event], kind="weekly", since=utcnow())

    assert '<a href="https://evil.example">' not in html
    assert '&lt;a href="https://evil.example"&gt;Johnson &amp; Johnson&lt;/a&gt;' in html
    assert "&lt;b&gt;жирный&lt;/b&gt;" in html
    assert "<i>site</i>" not in html
    assert "&lt;i&gt;site&lt;/i&gt; 1" in html
    assert ">a&amp;b &lt;x&gt;</a>" in html
    assert "a&amp;b &lt;x&gt; <span" in html
    assert '/alerts?hours=168&amp;type=a%26b+%3Cx%3E"' in html


def test_instant_emails_escape_scraped_text():
    """Мгновенные письма собирают строки той же функцией."""
    event = _loose_event("new_product", "critical", "<b>T</b> & co", detail="<i>D</i>")

    for html in (
        notifications._render_batch_email([event, event]),
        notifications._render_single_event_email(event),
    ):
        assert "<b>T</b>" not in html and "<i>D</i>" not in html
        assert "&lt;b&gt;T&lt;/b&gt; &amp; co" in html


@pytest.mark.parametrize("payload", [None, [1], "x", {"site": 5, "drop_pct": "many"}])
def test_digest_survives_odd_payload(payload):
    """payload — колонка JSON; странное значение не должно оставить всех без письма."""
    html = notifications._render_digest_email(
        [_loose_event("site_drop_smoke", "warning", "ODD", payload)], kind="weekly", since=utcnow()
    )
    assert "ODD" in html


def test_digest_skips_financial_event_with_odd_payload(setup, tenant_user):
    s = setup
    tenant_user.weekly_digest = True
    s.add(_loose_event("new_product", "info", "ODD", [1]))
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        assert notifications.send_weekly_digest(s, tenant_id=tenant_user.tenant_id) == 0
        assert not mock_email.called


def test_daily_digest_links_to_day_window(setup, tenant_user):
    s = setup
    tenant_user.daily_digest = True
    run = _eligible_run(s, tenant_user.tenant_id)
    _digest_event(s, run, "undercut_threshold", "critical", "Today event", site="aloe")
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        notifications.send_daily_digest(s, tenant_id=tenant_user.tenant_id)
    assert mock_email.call_args.kwargs["subject"] == (
        "Pharmacy Monitor — дайджест за сутки: 1 событие, из них 1 критичное"
    )
    body = mock_email.call_args.kwargs["html_body"]
    assert 'href="https://example.com/alerts?hours=24"' in body
    assert 'href="https://example.com/alerts?hours=24&amp;type=undercut_threshold"' in body
    assert "hours=168" not in body


def test_digest_only_sends_to_one_opted_in_recipient(setup, tenant_user):
    """`--only` — посмотреть письмо на живых данных, не трогая остальных."""
    s = setup
    tenant_user.weekly_digest = True
    s.add(
        storage.TenantUser(
            tenant_id=tenant_user.tenant_id,
            email="Bob@Example.com",
            role="viewer",
            is_active=True,
            created_at=utcnow(),
            weekly_digest=True,
        )
    )
    run = _eligible_run(s, tenant_user.tenant_id)
    _digest_event(s, run, "undercut_threshold", "critical", "X", site="aloe")
    s.commit()

    def send(only_email):
        with patch("src.notifier.send_email") as mock_email:
            sent = notifications.send_weekly_digest(
                s, tenant_id=tenant_user.tenant_id, only_email=only_email
            )
        assert sent == mock_email.call_count
        return [c.kwargs["to"][0] for c in mock_email.call_args_list]

    assert send(" Alice@Example.com ") == ["alice@example.com"]
    assert send("bob@example.com") == ["Bob@Example.com"]
    # Не включал дайджест, часть адреса, пустая строка — никому, а не всем.
    assert send("stranger@example.com") == []
    assert send("alice@example.co") == []
    assert send("example.com") == []
    assert send("") == []


def test_digest_dry_run_sends_nothing(setup, tenant_user):
    s = setup
    tenant_user.weekly_digest = True
    run = _eligible_run(s, tenant_user.tenant_id)
    _digest_event(s, run, "undercut_threshold", "critical", "X", site="aloe")
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        assert (
            notifications.send_weekly_digest(s, tenant_id=tenant_user.tenant_id, dry_run=True) == 1
        )
        assert not mock_email.called


def test_notify_digest_cli_dry_run_and_only(setup, tenant_user, monkeypatch):
    from click.testing import CliRunner

    from src.main import cli

    monkeypatch.setattr(storage, "init_db", lambda *a, **kw: None)
    s = setup
    tenant_user.weekly_digest = True
    run = _eligible_run(s, tenant_user.tenant_id)
    _digest_event(s, run, "undercut_threshold", "critical", "X", site="aloe")
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        r = CliRunner().invoke(cli, ["notify", "digest", "weekly", "--dry-run"])
        assert r.exit_code == 0, r.output
        assert "1 recipients, nothing sent" in r.output
        assert not mock_email.called

        r = CliRunner().invoke(cli, ["notify", "digest", "weekly", "--only", "nobody@example.com"])
        assert r.exit_code == 0, r.output
        assert "sent to 0 recipients" in r.output
        assert not mock_email.called

        r = CliRunner().invoke(cli, ["notify", "digest", "weekly", "--only", "alice@example.com"])
        assert r.exit_code == 0, r.output
        assert "sent to 1 recipients" in r.output
        assert mock_email.call_count == 1


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, "событие"),
        (2, "события"),
        (5, "событий"),
        (11, "событий"),
        (21, "событие"),
        (112, "событий"),
        (1094, "события"),
    ],
)
def test_plural(n, expected):
    assert notifications._plural(n, "событие", "события", "событий") == expected
