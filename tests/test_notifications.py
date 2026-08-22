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
