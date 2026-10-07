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


# ─── Digest: сводка и ограниченные списки ───────────────────────────────────

# Письмо больше 102 КБ Gmail сворачивает («Message clipped»).
GMAIL_CLIP_BYTES = 102 * 1024
NBSP = "\u00a0"


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


def _event_rows(html: str) -> int:
    """Сколько событий письмо показывает строками (у каждой — цветная полоса слева)."""
    return html.count("border-left:3px solid")


def _weekly_bodies(s, tenant_id) -> tuple[dict[str, str], str]:
    with patch("src.notifier.send_email") as mock_email:
        notifications.send_weekly_digest(s, tenant_id=tenant_id)
    bodies = {c.kwargs["to"][0]: c.kwargs["html_body"] for c in mock_email.call_args_list}
    subject = mock_email.call_args.kwargs["subject"] if mock_email.called else ""
    return bodies, subject


def test_weekly_digest_big_week_is_summary_plus_capped_lists(setup, tenant_user):
    """Неделя на 2 039 событий (как после добавления разделов aloe) — письмо со
    сводкой и списком до потолка, а не строка на каждое событие."""
    s = setup  # порог alice — warning
    tenant_user.weekly_digest = True
    run = _eligible_run(s, tenant_user.tenant_id)
    for i in range(1900):
        _digest_event(s, run, "new_product", "info", f"Новый товар на aloe: N{i}", site="aloe")
    for i in range(9):
        _digest_event(s, run, "new_product", "info", f"Новый товар на ph: N{i}", site="pharmonline")
    for i in range(85):
        _digest_event(
            s,
            run,
            "price_drop_pct",
            "critical",
            f"DROPCRIT{i:02d}",
            site="aptekonline",
            drop_pct=20 + i,
        )
    for i in range(22):
        _digest_event(
            s,
            run,
            "price_drop_pct",
            "warning",
            f"DROPWARN{i:02d}",
            site="aptekonline",
            drop_pct=10 + i / 10,
        )
    for i in range(5):
        _digest_event(
            s,
            run,
            "undercut_threshold",
            "critical",
            f"UNDERCUT{i}",
            site="aloe",
            diff_pct=10 + i,
        )
    for i in range(18):
        _digest_event(s, run, "price_raise_opportunity", "info", f"RAISE{i:02d}", gap_pct=7 + i)
    s.commit()

    bodies, subject = _weekly_bodies(s, tenant_user.tenant_id)
    body = bodies["alice@example.com"]

    assert len(bodies) == 1
    assert len(body.encode()) < GMAIL_CLIP_BYTES
    assert _event_rows(body) == 5 + notifications._DIGEST_TYPE_CAP
    assert _event_rows(body) <= notifications._DIGEST_ROW_BUDGET

    # Сводка: каждый тип одной строкой, новые товары — числом с разбивкой по сайтам.
    assert "Новые товары" in body
    assert f"aloe 1{NBSP}900, pharmonline 9" in body
    assert "85 критичных, 22 предупреждения · aptekonline 107" in body
    assert "Можно поднять цену" in body
    # …и ни один из 1 909 новых товаров строкой.
    assert "Новый товар на" not in body
    assert "RAISE" not in body  # info ниже порога alice — только числом

    # Малочисленное, но главное («конкурент дешевле») не вытеснено сотней падений цены.
    for i in range(5):
        assert f"UNDERCUT{i}" in body
    # Из падений цены — самые крупные; остальное за ссылкой с фильтром по типу.
    assert "DROPCRIT84" in body
    assert "DROPCRIT55" in body
    assert "DROPCRIT54" not in body
    assert "DROPWARN" not in body
    assert "30 из 107" in body
    assert "Ещё 77 — в дашборде" in body
    assert "https://example.com/alerts?hours=168&amp;type=price_drop_pct" in body

    assert subject == (
        f"Pharmacy Monitor — дайджест за неделю: 2{NBSP}039 событий, из них 90 критичных"
    )


def test_weekly_digest_small_week_lists_every_event(setup, tenant_user):
    """Мало событий — как раньше: каждое строкой, ничего не спрятано за ссылку."""
    s = setup
    tenant_user.weekly_digest = True
    tenant_user.email_severity_min = "info"
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
    assert "ниже порога важности" not in body
    # Критичные идут раньше информационных.
    assert body.index("UNDERCUT0") < body.index("DROPWARN0") < body.index("NEWPRODUCT0")
    assert subject == "Pharmacy Monitor — дайджест за неделю: 14 событий, из них 3 критичных"


def test_weekly_digest_respects_recipient_threshold(setup, tenant_user):
    """События ниже порога получателя — числом в сводке, не строками."""
    s = setup  # alice: warning
    tenant_user.weekly_digest = True
    s.add(
        storage.TenantUser(
            tenant_id=tenant_user.tenant_id,
            email="bob@example.com",
            name="Bob",
            role="viewer",
            is_active=True,
            created_at=utcnow(),
            email_severity_min="info",
            weekly_digest=True,
        )
    )
    run = _eligible_run(s, tenant_user.tenant_id)
    _digest_event(s, run, "undercut_threshold", "critical", "CRITTITLE", site="aloe")
    _digest_event(s, run, "new_product", "info", "INFOTITLE", site="aloe")
    s.commit()

    bodies, _ = _weekly_bodies(s, tenant_user.tenant_id)
    alice, bob = bodies["alice@example.com"], bodies["bob@example.com"]

    assert "CRITTITLE" in alice and "CRITTITLE" in bob
    assert "INFOTITLE" in bob
    assert "INFOTITLE" not in alice
    # У alice новый товар не пропал — он посчитан в сводке, и письмо говорит почему.
    assert "Новые товары" in alice
    assert "1 из 2, самые важные" in alice
    assert "ниже порога важности" in alice
    assert "ниже порога важности" not in bob


def test_digest_size_is_bounded_on_longest_texts(setup):
    """Потолок размера держится и на предельно длинных названиях и описаниях."""
    events = [
        storage.AlertEvent(
            rule_type=rule_type,
            dedup_key=f"long-{rule_type}-{i}",
            severity="critical",
            title="Ж" * 500,
            detail="Щ" * 5000,
            tenant_id=1,
            created_at=utcnow(),
            payload={"site": "aloe", "drop_pct": i},
        )
        for rule_type in [*notifications._RULE_TYPE_LABELS, "some_future_rule"]
        for i in range(100)
    ]
    html = notifications._render_digest_email(events, kind="weekly", since=utcnow())

    assert _event_rows(html) == notifications._DIGEST_ROW_BUDGET
    assert len(html.encode()) < GMAIL_CLIP_BYTES
    assert "some_future_rule" in html  # незнакомый тип виден в сводке под своим именем


def test_digest_escapes_scraped_text(setup):
    """Имя товара приходит с чужого сайта — в письме это текст, а не разметка."""
    event = storage.AlertEvent(
        rule_type="new_product",
        dedup_key="esc",
        severity="info",
        title='Новый товар: <a href="https://evil.example">Johnson & Johnson</a>',
        detail="<b>жирный</b>",
        tenant_id=1,
        created_at=utcnow(),
        payload={"site": "aloe"},
    )
    html = notifications._render_digest_email([event], kind="weekly", since=utcnow())

    assert '<a href="https://evil.example">' not in html
    assert '&lt;a href="https://evil.example"&gt;Johnson &amp; Johnson&lt;/a&gt;' in html
    assert "&lt;b&gt;жирный&lt;/b&gt;" in html


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
    assert "https://example.com/alerts?hours=24" in mock_email.call_args.kwargs["html_body"]


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
