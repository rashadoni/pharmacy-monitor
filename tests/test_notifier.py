"""Тесты src/notifier.py — SMTP + Telegram dispatch.

Coverage gap: 18% → больше. Покрываем:
- Telegram send_message: missing token, success, API error, network error
- telegram_get_updates: missing token, success, error
- resolve_recipients: explicit, from DB, from env, all-empty
- send_email: no SMTP env → skip, with SMTP → SMTP smtplib mocked
"""

from __future__ import annotations

import json

import pytest

from src import notifier


# ─── send_telegram_message ──────────────────────────────────────────────────


def test_telegram_no_token_returns_false(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert notifier.send_telegram_message("123", "hi") is False


def test_telegram_success(monkeypatch):
    """200 + ok=true → True."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    fake_resp = json.dumps({"ok": True, "result": {"message_id": 1}}).encode()

    class FakeResp:
        def __init__(self):
            self._body = fake_resp

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: FakeResp())
    assert notifier.send_telegram_message("12345", "Test message") is True


def test_telegram_api_error_response(monkeypatch):
    """ok=false → возвращаем False (не падаем)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    fake = json.dumps({"ok": False, "description": "chat not found"}).encode()

    class FakeResp:
        def read(self):
            return fake

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: FakeResp())
    assert notifier.send_telegram_message("0", "x") is False


def test_telegram_network_error_returns_false(monkeypatch):
    """OSError/timeout → False, не падает."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")

    def boom(*_a, **_kw):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert notifier.send_telegram_message("0", "x") is False


# ─── telegram_get_updates ───────────────────────────────────────────────────


def test_telegram_get_updates_no_token_returns_empty(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert notifier.telegram_get_updates() == []


def test_telegram_get_updates_returns_result_list(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    payload = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 100}, "text": "/start"}},
            {"update_id": 2, "message": {"chat": {"id": 200}, "text": "hi"}},
        ],
    }

    class FakeResp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: FakeResp())
    updates = notifier.telegram_get_updates(offset=10)
    assert len(updates) == 2
    assert updates[0]["update_id"] == 1
    assert updates[1]["message"]["chat"]["id"] == 200


def test_telegram_get_updates_handles_error(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")

    def boom(*_a, **_kw):
        raise TimeoutError("polling timeout")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert notifier.telegram_get_updates() == []


# ─── resolve_recipients ────────────────────────────────────────────────────


def test_resolve_recipients_explicit_wins():
    """Explicit аргумент имеет приоритет — без обращения к DB / env."""
    assert notifier.resolve_recipients(["alice@x"]) == ["alice@x"]


def test_resolve_recipients_falls_back_to_env(monkeypatch):
    """Если DB пуста (или не найдена) — берём EMAIL_TO."""
    # Make DB lookup return empty
    import src.watchlist as wl

    monkeypatch.setattr(wl, "active_recipient_emails", lambda _s: [])
    monkeypatch.setenv("EMAIL_TO", "  env1@x.com,  env2@y.com  ")
    out = notifier.resolve_recipients()
    assert out == ["env1@x.com", "env2@y.com"]


def test_resolve_recipients_from_db_when_available(monkeypatch):
    """Если БД даёт recipients — используем их (env игнорируется)."""
    import src.watchlist as wl

    monkeypatch.setattr(wl, "active_recipient_emails", lambda _s: ["db@x"])
    monkeypatch.setenv("EMAIL_TO", "env-only@x")
    assert notifier.resolve_recipients() == ["db@x"]


def test_resolve_recipients_db_failure_falls_back_to_env(monkeypatch):
    """Если DB lookup кидает exception — fallback на EMAIL_TO."""
    import src.watchlist as wl

    def boom(_s):
        raise RuntimeError("DB connection refused")

    monkeypatch.setattr(wl, "active_recipient_emails", boom)
    monkeypatch.setenv("EMAIL_TO", "fallback@x")
    assert notifier.resolve_recipients() == ["fallback@x"]


def test_resolve_recipients_all_empty(monkeypatch):
    """Нет explicit / DB / env → пустой список."""
    import src.watchlist as wl

    monkeypatch.setattr(wl, "active_recipient_emails", lambda _s: [])
    monkeypatch.delenv("EMAIL_TO", raising=False)
    assert notifier.resolve_recipients() == []


# ─── send_email ────────────────────────────────────────────────────────────


def test_send_email_skip_without_smtp_host(monkeypatch, caplog):
    """SMTP_HOST не задан → silent skip (не raise)."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    # Должно не упасть и не вызвать smtplib
    notifier.send_email("Subject", "<p>html</p>")


def test_send_email_no_recipients_raises(monkeypatch):
    """SMTP задан но нет получателей → ValueError."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "u@x")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.delenv("EMAIL_TO", raising=False)
    import src.watchlist as wl

    monkeypatch.setattr(wl, "active_recipient_emails", lambda _s: [])
    with pytest.raises(ValueError, match="No recipients"):
        notifier.send_email("S", "<p></p>")


def test_send_email_sends_via_smtplib(monkeypatch):
    """С SMTP env + получателями — вызываем smtplib.SMTP + login + send_message."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "u@x")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("EMAIL_TO", "recipient@x")
    import src.watchlist as wl

    monkeypatch.setattr(wl, "active_recipient_emails", lambda _s: [])

    sent: dict = {}

    class FakeSMTP:
        def __init__(self, host, port):
            sent["host"] = host
            sent["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self):
            sent["starttls"] = True

        def login(self, user, password):
            sent["user"] = user
            sent["password"] = password

        def send_message(self, msg):
            sent["from"] = msg["From"]
            sent["to"] = msg["To"]
            sent["subject"] = msg["Subject"]

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    notifier.send_email("Test subject", "<p>Body</p>", to=["explicit@x"])
    assert sent["host"] == "smtp.example.com"
    assert sent["port"] == 587
    assert sent["user"] == "u@x"
    assert sent["password"] == "secret"
    assert sent["starttls"] is True
    assert sent["to"] == "explicit@x"
    assert sent["subject"] == "Test subject"


def test_send_email_with_attachment(monkeypatch):
    """Attachment добавляется в EmailMessage с правильным mimetype."""
    monkeypatch.setenv("SMTP_HOST", "smtp.x")
    monkeypatch.setenv("SMTP_USER", "u@x")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    import src.watchlist as wl

    monkeypatch.setattr(wl, "active_recipient_emails", lambda _s: [])

    captured_messages = []

    class FakeSMTP:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg):
            captured_messages.append(msg)

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    notifier.send_email(
        "S",
        "<p>x</p>",
        attachments=[("report.xlsx", b"fake-xlsx", "application/vnd.ms-excel")],
        to=["r@x"],
    )
    msg = captured_messages[0]
    # Walk через части — должна быть attachment с filename
    has_attachment = False
    for part in msg.walk():
        if part.get_filename() == "report.xlsx":
            has_attachment = True
            assert part.get_content_type() == "application/vnd.ms-excel"
    assert has_attachment


def test_send_email_smtp_failure_propagates(monkeypatch):
    """SMTP exception НЕ ловится внутри send_email — propagate up.

    Это намеренно: caller (alerts dispatcher) сам решает что делать
    с failed delivery (retry / drop).
    """
    monkeypatch.setenv("SMTP_HOST", "x")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("EMAIL_TO", "r@x")
    import src.watchlist as wl

    monkeypatch.setattr(wl, "active_recipient_emails", lambda _s: [])

    class BoomSMTP:
        def __init__(self, *a):
            raise ConnectionRefusedError("nope")

        def __enter__(self):
            raise ConnectionRefusedError("nope")

        def __exit__(self, *a):
            pass

    monkeypatch.setattr("smtplib.SMTP", BoomSMTP)
    with pytest.raises(ConnectionRefusedError):
        notifier.send_email("S", "<p></p>")
