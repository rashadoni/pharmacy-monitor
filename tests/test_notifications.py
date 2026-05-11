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

    with patch("src.notifier.send_email") as mock_email, patch(
        "src.notifier.send_telegram_message"
    ) as mock_tg:
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

    with patch("src.notifier.send_email") as mock_email, patch(
        "src.notifier.send_telegram_message"
    ) as mock_tg:
        notifications.dispatch_event(s, event)
        assert mock_tg.called
        assert mock_tg.call_args[0][0] == "100"


def test_dispatch_event_respects_quiet_hours(setup, tenant_user, monkeypatch):
    s = setup
    tenant_user.telegram_chat_id = "100"
    tenant_user.quiet_hours = "00-23"  # always quiet — telegram should be skipped
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

    with patch("src.notifier.send_email"), patch(
        "src.notifier.send_telegram_message"
    ) as mock_tg:
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
    s.add(storage.AlertEvent(
        rule_type="undercut_threshold",
        dedup_key="d-1",
        severity="warning",
        title="Today event",
        tenant_id=tenant_user.tenant_id,
        created_at=utcnow(),
    ))
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        sent = notifications.send_daily_digest(s, tenant_id=tenant_user.tenant_id)
        assert sent == 1
        assert mock_email.called


def test_send_daily_digest_skips_old_events(setup, tenant_user):
    """Events older than 24h not included — if all old, no email."""
    s = setup
    tenant_user.daily_digest = True
    s.add(storage.AlertEvent(
        rule_type="undercut_threshold",
        dedup_key="d-old",
        severity="warning",
        title="Old event",
        tenant_id=tenant_user.tenant_id,
        created_at=utcnow() - timedelta(days=3),
    ))
    s.commit()

    with patch("src.notifier.send_email") as mock_email:
        sent = notifications.send_daily_digest(s, tenant_id=tenant_user.tenant_id)
        assert sent == 0
        assert not mock_email.called
