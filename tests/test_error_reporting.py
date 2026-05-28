"""Тесты src/error_reporting.py — light-weight error capture.

Coverage gap: 0% покрытия. Покрываем fingerprint, dedup, file logging,
notification suppression, и декоратор @report_on_error.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src import error_reporting


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    """Свежее состояние между тестами + temp errors.jsonl."""
    error_reporting._RECENT.clear()
    monkeypatch.setattr(error_reporting, "ERRORS_LOG", tmp_path / "errors.jsonl")
    yield
    error_reporting._RECENT.clear()


def _stub_notifier(monkeypatch):
    """Подменяем notifier.send_email чтобы не отправлять реально + считать вызовы.

    Используем monkeypatch.setattr на реальном модуле (не setitem в sys.modules) —
    это устойчиво к тому что другие тесты уже сделали `from src import notifier`
    и забиндили имя локально.
    """
    import src.notifier as real_notifier

    calls = []

    def fake_send(subject, html_body):
        calls.append({"subject": subject, "html_body": html_body})

    monkeypatch.setattr(real_notifier, "send_email", fake_send)
    return calls


def test_fingerprint_stable_for_same_error():
    """Один тип + сообщение → одинаковый fingerprint."""
    e1 = ValueError("boom")
    e2 = ValueError("boom")
    assert error_reporting._fingerprint(e1) == error_reporting._fingerprint(e2)


def test_fingerprint_changes_with_message():
    """Разные сообщения → разные fingerprints."""
    fp1 = error_reporting._fingerprint(ValueError("boom-a"))
    fp2 = error_reporting._fingerprint(ValueError("boom-b"))
    assert fp1 != fp2


def test_fingerprint_changes_with_type():
    """Разные типы → разные fingerprints."""
    fp1 = error_reporting._fingerprint(ValueError("x"))
    fp2 = error_reporting._fingerprint(TypeError("x"))
    assert fp1 != fp2


def test_fingerprint_includes_context():
    """Context влияет на fingerprint."""
    e = ValueError("x")
    fp1 = error_reporting._fingerprint(e, {"a": 1})
    fp2 = error_reporting._fingerprint(e, {"a": 2})
    assert fp1 != fp2


def test_report_error_writes_jsonl(monkeypatch):
    """Файл errors.jsonl содержит запись после report_error()."""
    _stub_notifier(monkeypatch)
    try:
        raise RuntimeError("test-error-jsonl")
    except RuntimeError as e:
        error_reporting.report_error(e, component="scraper.test", notify=False)

    log_file: Path = error_reporting.ERRORS_LOG
    assert log_file.exists()
    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["component"] == "scraper.test"
    assert payload["type"] == "RuntimeError"
    assert payload["message"] == "test-error-jsonl"
    assert "fingerprint" in payload
    assert "ts" in payload


def test_report_error_notify_calls_email(monkeypatch):
    """notify=True → send_email вызывается."""
    calls = _stub_notifier(monkeypatch)
    try:
        raise ValueError("notify-test")
    except ValueError as e:
        error_reporting.report_error(e, component="api", notify=True)
    assert len(calls) == 1
    assert "ValueError" in calls[0]["subject"]


def test_report_error_dedup_suppresses_same_fingerprint(monkeypatch):
    """Подряд два одинаковых error'а → email отправляется только раз."""
    calls = _stub_notifier(monkeypatch)
    for _ in range(3):
        try:
            raise ValueError("dedup-test")
        except ValueError as e:
            error_reporting.report_error(e, component="x", notify=True)
    assert len(calls) == 1, f"Expected 1 email (dedup), got {len(calls)}"


def test_report_error_dedup_unblocks_after_window(monkeypatch):
    """Через _DEDUP_WINDOW секунд тот же error снова шлёт email."""
    calls = _stub_notifier(monkeypatch)
    try:
        raise ValueError("window-test")
    except ValueError as e:
        error_reporting.report_error(e, component="x", notify=True)
    # Симулируем что прошло > _DEDUP_WINDOW
    assert error_reporting._RECENT
    fp = next(iter(error_reporting._RECENT))
    error_reporting._RECENT[fp] = time.time() - (error_reporting._DEDUP_WINDOW + 10)
    # Снова бахаем — должно пройти
    try:
        raise ValueError("window-test")
    except ValueError as e:
        error_reporting.report_error(e, component="x", notify=True)
    assert len(calls) == 2


def test_report_on_error_decorator_catches_and_reraises(monkeypatch):
    """Декоратор репортит и продолжает propagate exception (повторно raise)."""
    calls = _stub_notifier(monkeypatch)

    @error_reporting.report_on_error(component="decorated", notify=True)
    def failing():
        raise KeyError("decorator-test")

    with pytest.raises(KeyError):
        failing()
    assert len(calls) == 1
    assert "KeyError" in calls[0]["subject"]


def test_report_on_error_decorator_passes_through_success(monkeypatch):
    """Если функция возвращает значение — декоратор отдаёт его без обёртки."""
    _stub_notifier(monkeypatch)

    @error_reporting.report_on_error(component="ok")
    def succeeding(a, b):
        return a + b

    assert succeeding(2, 3) == 5


def test_email_failure_does_not_break_reporter(monkeypatch):
    """Если notifier.send_email кидает exception — report_error не падает."""
    import src.notifier as real_notifier

    def boom(*_a, **_kw):
        raise RuntimeError("smtp dead")

    monkeypatch.setattr(real_notifier, "send_email", boom)

    try:
        raise OSError("disk-full")
    except OSError as e:
        # Это не должно падать — internal try/except глотает SMTP-failure
        error_reporting.report_error(e, component="x", notify=True)
    # JSONL запись всё равно появилась
    assert error_reporting.ERRORS_LOG.read_text().strip()
