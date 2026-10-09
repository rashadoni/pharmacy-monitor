"""Тесты src/notifier.py — SMTP + Telegram dispatch.

Coverage gap: 18% → больше. Покрываем:
- Telegram send_message: missing token, success, API error, network error
- telegram_get_updates: missing token, success, error
- resolve_recipients: explicit, from DB, from env, all-empty
- send_email: no SMTP env → skip, with SMTP → SMTP smtplib mocked
"""

from __future__ import annotations

import ast
import errno
import http.client
import io
import json
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from pathlib import Path

import pytest
from structlog.testing import capture_logs

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


def test_telegram_get_updates_without_a_token_is_a_poll_that_did_not_happen(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with capture_logs() as logs:
        assert notifier.telegram_get_updates() is None
    assert [entry["event"] for entry in logs] == ["telegram_no_token"]


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
    """Сбой — `None`, а не пустой список: «сообщений нет» значит другое."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")

    def boom(*_a, **_kw):
        raise TimeoutError("polling timeout")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert notifier.telegram_get_updates() is None


def test_telegram_get_updates_tells_no_messages_from_a_failed_poll(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr("urllib.request.urlopen", _answers_with({"ok": True, "result": []}))
    with capture_logs() as logs:
        assert notifier.telegram_get_updates() == []
    assert logs == []


@pytest.mark.parametrize(
    "reply",
    [{"ok": True}]
    + [
        {"ok": True, "result": result}
        for result in (None, False, 0, {}, True, 7, "text", {"update_id": 1}, [1, 2], [None])
    ],
)
def test_telegram_get_updates_answers_with_messages_or_with_none(monkeypatch, reply):
    """Оба вызывающих разбирают ответ без проверок: не список сообщений — это
    опрос, который не состоялся, а не «сообщений нет» и не то, обо что бот упадёт."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr("urllib.request.urlopen", _answers_with(reply))
    with capture_logs() as logs:
        assert notifier.telegram_get_updates() is None
    assert _the_failure(logs, "telegram_get_updates") == {
        "event": "telegram_poll_failed",
        "log_level": "warning",
        "error_type": "TypeError",
    }


# ─── Журнал отправителя Telegram: ни токена, ни ответа сервера ───────────────
#
# Токен бота стоит в адресе запроса, а текст ошибки `urlopen` адрес несёт. Маска
# журнала вырезает только почтовые адреса. `send_telegram_message` зовёт `run`
# (рассылка алертов), а журнал еженедельного сбора pharmonline — журнал шага
# GitHub Actions публичного репозитория.

TOKEN = "000111:FAKE-token-for-tests"
CHAT_ID = "700200300400"

_TELEGRAM_CALLS = [
    pytest.param(
        lambda: notifier.send_telegram_message(CHAT_ID, "готово"),
        False,
        "telegram_send_failed",
        "send_telegram_message",
        id="send",
    ),
    pytest.param(
        lambda: notifier.telegram_get_updates(offset=41),
        None,
        "telegram_poll_failed",
        "telegram_get_updates",
        id="poll",
    ),
]


def _answers_with(body: dict):
    """`urlopen`, которому Telegram ответил 200 с таким телом."""

    class Reply:
        def read(self):
            return json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return lambda *args, **kwargs: Reply()


def _refuses_with(status: int, reason: str, body: dict):
    """`urlopen`, которому Telegram ответил отказом. Ошибка собрана так же, как
    её собирает urllib: с адресом запроса (в нём токен) и телом ответа."""

    def urlopen(request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        assert TOKEN in url
        raise urllib.error.HTTPError(
            url, status, reason, Message(), io.BytesIO(json.dumps(body).encode())
        )

    return urlopen


@pytest.fixture
def unreachable_telegram(monkeypatch):
    """Запросы идут не в сеть, а на закрытый порт этой же машины."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(
        notifier, "TELEGRAM_API_BASE", f"https://127.0.0.1:{port}/bot{{token}}/{{method}}"
    )
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "*")


def _called(call, capsys, caplog) -> tuple[object, list[dict], str]:
    """Ответ функции, её журнал и всё, что она вывела мимо него."""
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    capsys.readouterr()
    with capture_logs() as logs:
        result = call()
    printed = capsys.readouterr()
    return result, logs, repr(logs) + printed.out + printed.err + caplog.text


def _the_failure(logs: list[dict], function: str) -> dict:
    """Единственная запись журнала без `error_at`: номер строки плавает."""
    [failure] = [dict(entry) for entry in logs]
    assert re.fullmatch(rf"notifier\.py:\d+ in {function}", failure.pop("error_at")), logs
    return failure


@pytest.mark.parametrize(("call", "failed", "event", "function"), _TELEGRAM_CALLS)
def test_telegram_failure_log_carries_no_token(
    unreachable_telegram, monkeypatch, capsys, caplog, call, failed, event, function
):
    """Токен с управляющим символом на конце — строка env с CRLF."""
    token = TOKEN + "\r"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    # Посылка правила. Перестанет urllib класть путь запроса в текст ошибки —
    # тест скажет об этом, а не пройдёт вхолостую.
    with pytest.raises(http.client.InvalidURL) as raised:
        urllib.request.urlopen(
            notifier.TELEGRAM_API_BASE.format(token=token, method="getMe"), timeout=5
        )
    assert TOKEN in str(raised.value)

    result, logs, written = _called(call, capsys, caplog)

    assert result is failed
    assert TOKEN not in written, written
    assert _the_failure(logs, function) == {
        "event": event,
        "log_level": "warning",
        "error_type": "InvalidURL",
    }


@pytest.mark.parametrize(("call", "failed", "event", "function"), _TELEGRAM_CALLS)
@pytest.mark.parametrize(
    ("status", "reason", "description"),
    [
        (401, "Unauthorized", "Unauthorized"),
        (400, "Bad Request", "Bad Request: chat not found"),
        (429, "Too Many Requests", "Too Many Requests: retry after 35"),
    ],
)
def test_telegram_refusal_log_keeps_the_http_status(
    monkeypatch, capsys, caplog, call, failed, event, function, status, reason, description
):
    """Неверный токен, непринятый запрос и «слишком часто» по-прежнему различаются."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    body = {"ok": False, "error_code": status, "description": description}
    monkeypatch.setattr("urllib.request.urlopen", _refuses_with(status, reason, body))

    result, logs, written = _called(call, capsys, caplog)

    assert result is failed
    for piece in (TOKEN, CHAT_ID, description):
        assert piece not in written, written
    assert _the_failure(logs, function) == {
        "event": event,
        "log_level": "warning",
        "error_type": "HTTPError",
        "http_status": status,
    }


@pytest.mark.parametrize(("call", "failed", "event", "function"), _TELEGRAM_CALLS)
def test_telegram_network_failure_log_says_what_is_behind_the_urlerror(
    unreachable_telegram, monkeypatch, capsys, caplog, call, failed, event, function
):
    """`URLError` — и нет имени в DNS, и отказ в соединении, и сертификат."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)

    result, logs, written = _called(call, capsys, caplog)

    assert result is failed
    assert TOKEN not in written, written
    assert _the_failure(logs, function) == {
        "event": event,
        "log_level": "warning",
        "error_type": "URLError",
        "reason_type": "ConnectionRefusedError",
        "errno": errno.ECONNREFUSED,
    }


@pytest.mark.parametrize(
    ("call", "failed", "method"),
    [
        pytest.param(_TELEGRAM_CALLS[0].values[0], False, "sendMessage", id="send"),
        pytest.param(_TELEGRAM_CALLS[1].values[0], None, "getUpdates", id="poll"),
    ],
)
@pytest.mark.parametrize(
    ("error_code", "logged"),
    [
        (400, {"error_code": 400}),
        # Пишется код ответа, и только пока он на код похож: ни строка, ни
        # число размером с идентификатор чата в журнал не идут.
        ("400 chat -1001234567890", {}),
        (-1001234567890, {}),
        (True, {}),
        (None, {}),
    ],
)
def test_telegram_api_error_log_keeps_the_error_code_and_not_the_reply(
    monkeypatch, capsys, caplog, call, failed, method, error_code, logged
):
    """Ответ с `ok: false`: в нём идентификатор чата и текст, который пишет сервер."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    reply = {
        "ok": False,
        "error_code": error_code,
        "description": "Bad Request: group chat was upgraded to a supergroup chat",
        "parameters": {"migrate_to_chat_id": -1001234567890},
    }
    monkeypatch.setattr("urllib.request.urlopen", _answers_with(reply))

    result, logs, written = _called(call, capsys, caplog)

    assert result is failed
    for piece in (TOKEN, CHAT_ID, "1001234567890", "supergroup"):
        assert piece not in written, written
    assert logs == [
        {"event": "telegram_api_error", "log_level": "warning", "method": method, **logged}
    ]


def test_telegram_message_that_cannot_be_encoded_is_a_failed_send(monkeypatch, capsys, caplog):
    """Сбой до сети — тот же сбой отправки: наружу не выходит, в журнале — класс."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    text = "цена \udc80 упала"
    # Посылка: такой текст не кодируется, а сама ошибка держит его целиком.
    with pytest.raises(UnicodeEncodeError) as raised:
        urllib.parse.urlencode({"text": text})
    assert "упала" in raised.value.object

    result, logs, written = _called(
        lambda: notifier.send_telegram_message(CHAT_ID, text), capsys, caplog
    )

    assert result is False
    for piece in (TOKEN, CHAT_ID, "упала"):
        assert piece not in written, written
    assert _the_failure(logs, "send_telegram_message") == {
        "event": "telegram_send_failed",
        "log_level": "warning",
        "error_type": "UnicodeEncodeError",
    }


# Где в том, что исполняется, назван адрес Telegram, и сколько раз. Счёт, а не
# одно имя файла: второе упоминание в уже названном файле — тоже новое место.
_NAMES_THE_TELEGRAM_ADDRESS = {
    # Один раз — в `TELEGRAM_API_BASE`; кто по ней собирает запрос, названо в тесте.
    "src/notifier.py": 1,
    # Подсказка оператору, где взять chat_id: на месте токена заглушка, запроса нет.
    "scripts/configure-integrations.sh": 1,
}


def test_only_the_two_sender_functions_build_a_telegram_request():
    """Токен закрыт там, где собирается адрес запроса, — в двух функциях, которые
    тесты выше зовут с настоящей ошибкой. Третье место — решение, а не случайность."""
    root = Path(__file__).resolve().parent.parent
    named_in = {}
    for folder in ("src", "scripts", "infra", ".github"):
        for path in (root / folder).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "api.telegram.org" in content or "TELEGRAM_API_BASE" in content:
                named_in[path.relative_to(root).as_posix()] = content.count("api.telegram.org")
    builders = {
        node.name
        for node in ast.parse((root / "src/notifier.py").read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef)
        and any(getattr(inner, "id", None) == "TELEGRAM_API_BASE" for inner in ast.walk(node))
    }
    assert (named_in, builders) == (
        _NAMES_THE_TELEGRAM_ADDRESS,
        {"send_telegram_message", "telegram_get_updates"},
    ), (
        f"Адрес Telegram назван в {named_in} (файл: сколько раз), запрос собирают "
        f"{sorted(builders)}. "
        "Токен бота стоит в адресе запроса, и текст ошибки запроса его несёт. В новом "
        "месте о сбое пиши `**_telegram_error_fields(exc)`, а не текст ошибки и не ответ "
        "сервера; проверь прямым вызовом с токеном, у которого на конце `\\r`, — как "
        "`test_telegram_failure_log_carries_no_token`; затем впиши место сюда. Упоминание, "
        "за которым нет запроса, — в `_NAMES_THE_TELEGRAM_ADDRESS`, с причиной."
    )


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
    assert notifier.send_email("Subject", "<p>html</p>") is False


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
        def __init__(self, host, port, timeout=None):
            sent["host"] = host
            sent["port"] = port
            sent["timeout"] = timeout

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
    assert notifier.send_email("Test subject", "<p>Body</p>", to=["explicit@x"]) is True
    assert sent["host"] == "smtp.example.com"
    assert sent["port"] == 587
    # Без таймаута зависший SMTP держал бы таймерную задачу до лимита systemd.
    assert sent["timeout"] == notifier.SMTP_TIMEOUT_SEC
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
        def __init__(self, *a, **kw):
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
    assert (
        notifier.send_email(
            "S",
            "<p>x</p>",
            attachments=[("report.xlsx", b"fake-xlsx", "application/vnd.ms-excel")],
            to=["r@x"],
        )
        is True
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
        def __init__(self, *a, **kw):
            raise ConnectionRefusedError("nope")

        def __enter__(self):
            raise ConnectionRefusedError("nope")

        def __exit__(self, *a):
            pass

    monkeypatch.setattr("smtplib.SMTP", BoomSMTP)
    with pytest.raises(ConnectionRefusedError):
        notifier.send_email("S", "<p></p>")
