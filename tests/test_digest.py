"""Тесты src/digest.py — daily email digest of AlertEvents.

Coverage gap: 0% покрытия. Покрываем skip-on-empty, severity sorting,
top-n truncation, dry-run mode, subject/HTML composition.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src import digest, storage
from src._time import utcnow


@pytest.fixture
def stub_notifier(monkeypatch):
    """Подменяет notifier.send_email — список отправленных писем.

    Используем monkeypatch.setattr на реальном модуле — устойчиво к
    тому что digest.py делает `from src import notifier` локально.
    """
    import src.notifier as real_notifier

    calls = []

    def fake_send(subject, html_body):
        calls.append({"subject": subject, "html_body": html_body})
        return True  # как настоящий отправитель после принятого письма

    monkeypatch.setattr(real_notifier, "send_email", fake_send)
    return calls


def _add_alert(session, severity="warning", title="Test", detail=None, hours_ago=1):
    event = storage.AlertEvent(
        severity=severity,
        title=title,
        detail=detail,
        rule_type="test",
        dedup_key=f"k-{title}-{hours_ago}",
        created_at=utcnow() - timedelta(hours=hours_ago),
    )
    session.add(event)
    session.flush()
    return event


def test_digest_empty_window_skips(db_session, stub_notifier):
    """Нет событий → ничего не отправляем, return 0."""
    n = digest.send_daily_digest(db_session, window_hours=24)
    assert n == 0
    assert stub_notifier == []


def test_digest_filters_outside_window(db_session, stub_notifier):
    """События старше window_hours игнорируются."""
    _add_alert(db_session, hours_ago=48)  # вне 24h окна
    db_session.commit()
    n = digest.send_daily_digest(db_session, window_hours=24)
    assert n == 0


def test_digest_sends_email_on_events(db_session, stub_notifier):
    """1+ событий → один email с count."""
    _add_alert(db_session, severity="warning", title="Price drop", hours_ago=2)
    _add_alert(db_session, severity="critical", title="Site down", hours_ago=1)
    db_session.commit()
    n = digest.send_daily_digest(db_session, window_hours=24)
    assert n == 2
    assert len(stub_notifier) == 1
    subject = stub_notifier[0]["subject"]
    assert "critical" in subject
    assert "warning" in subject


def test_digest_severity_sort_critical_first(db_session, stub_notifier):
    """В HTML письма критичные события идут первыми."""
    _add_alert(db_session, severity="info", title="Info-A", hours_ago=2)
    _add_alert(db_session, severity="warning", title="Warn-B", hours_ago=3)
    _add_alert(db_session, severity="critical", title="Crit-C", hours_ago=1)
    db_session.commit()
    digest.send_daily_digest(db_session, window_hours=24)
    html = stub_notifier[0]["html_body"]
    # Crit-C должен появиться раньше Warn-B и Info-A
    idx_crit = html.find("Crit-C")
    idx_warn = html.find("Warn-B")
    idx_info = html.find("Info-A")
    assert idx_crit < idx_warn < idx_info


def test_digest_top_n_truncation(db_session, stub_notifier):
    """top_n=3 ограничивает кол-во событий в письме."""
    for i in range(5):
        _add_alert(db_session, severity="info", title=f"E-{i}", hours_ago=i + 1)
    db_session.commit()
    n = digest.send_daily_digest(db_session, window_hours=24, top_n=3)
    assert n == 3
    subject = stub_notifier[0]["subject"]
    # Subject должен показать "+2 ещё" (total=5, shown=3)
    assert "+2" in subject or "ещё" in subject


def test_digest_dry_run_no_email(db_session, stub_notifier, capsys):
    """dry_run=True → no email + console output."""
    _add_alert(db_session, severity="warning", title="Dry test")
    db_session.commit()
    n = digest.send_daily_digest(db_session, window_hours=24, dry_run=True)
    assert n == 1
    assert len(stub_notifier) == 0
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    assert "Dry test" in captured.out


def test_digest_email_failure_is_not_a_quiet_zero(db_session, monkeypatch):
    """Отправитель упал → `DigestNotSent`, а не 0: 0 значит «событий не было»."""
    import src.notifier as real_notifier

    def boom(*_a, **_kw):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr(real_notifier, "send_email", boom)

    _add_alert(db_session, severity="warning", title="Fail test")
    db_session.commit()
    with pytest.raises(digest.DigestNotSent) as raised:
        digest.send_daily_digest(db_session, window_hours=24)
    # Текст ошибки отправителя дальше не идёт — ни в сообщении, ни в причине.
    assert "SMTP down" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None


def test_make_subject_no_events_label():
    """Edge case: subject без emoji-counters → "нет событий"."""
    s = digest._make_subject(0, 0, 0, 0, 20)
    assert "нет событий" in s


def test_make_subject_includes_more_suffix():
    """Когда total > top_n → '+N ещё' добавляется."""
    s = digest._make_subject(1, 2, 3, total=30, top_n=20)
    assert "+10" in s


def test_make_html_contains_event_data(db_session):
    """HTML содержит title и detail событий."""
    e = _add_alert(db_session, severity="warning", title="UniqueTitle", detail="UniqueDetail")
    db_session.commit()
    html = digest._make_html([e], total=1, top_n=20)
    assert "UniqueTitle" in html
    assert "UniqueDetail" in html
    assert "Pharmacy Monitor" in html  # Branding
