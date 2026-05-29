"""`pharmacy-monitor notify test` — smoke-тест доставки алертов.

Шлёт тест-email + тест-telegram, репортит каждый канал; неконфигурированные →
'skipped' (не ошибка). Используется после configure-integrations.sh для проверки.
"""

from click.testing import CliRunner

from src import notifier
from src.main import cli


def test_skips_when_unconfigured(monkeypatch):
    for var in ("SMTP_HOST", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    r = CliRunner().invoke(cli, ["notify", "test"])
    assert r.exit_code == 0, r.output
    assert "email:    skipped" in r.output
    assert "telegram: skipped" in r.output


def test_sends_when_configured(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    captured: dict = {}
    monkeypatch.setattr(notifier, "send_email", lambda **kw: captured.setdefault("email", kw))
    monkeypatch.setattr(
        notifier,
        "send_telegram_message",
        lambda cid, text: captured.setdefault("tg", (cid, text)) or True,
    )
    r = CliRunner().invoke(cli, ["notify", "test"])
    assert r.exit_code == 0, r.output
    assert "email:    OK" in r.output
    assert "telegram: OK" in r.output
    assert captured["tg"][0] == "12345"


def test_email_failure_reported_not_raised(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    def _boom(**kw):
        raise ValueError("No recipients configured")

    monkeypatch.setattr(notifier, "send_email", _boom)
    r = CliRunner().invoke(cli, ["notify", "test"])
    assert r.exit_code == 0, r.output
    assert "email:    FAIL — No recipients configured" in r.output
    assert "telegram: skipped" in r.output


def test_chat_id_override(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    seen: dict = {}
    monkeypatch.setattr(
        notifier, "send_telegram_message", lambda cid, text: seen.setdefault("cid", cid) or True
    )
    r = CliRunner().invoke(cli, ["notify", "test", "--chat-id", "999"])
    assert r.exit_code == 0, r.output
    assert "telegram: OK" in r.output
    assert seen["cid"] == "999"
