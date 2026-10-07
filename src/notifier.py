"""SMTP-отправка отчёта (HTML + Excel-вложение).

Получатели берутся в таком порядке:
1. Аргумент `to=...` (если передан)
2. Активные записи из БД (таблица recipients)
3. EMAIL_TO в .env (fallback)
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.parse
import urllib.request
from email.message import EmailMessage

import structlog

from src import storage, watchlist

log = structlog.get_logger()


# === TELEGRAM ===
# Используем raw HTTP (через stdlib) — никаких внешних библиотек.
# Bot создаётся через @BotFather, токен кладётся в .env как TELEGRAM_BOT_TOKEN.

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def send_telegram_message(chat_id: str | int, text: str, parse_mode: str = "Markdown") -> bool:
    """Отправить Telegram-сообщение. Возвращает True если ok=true."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("telegram_no_token", hint="set TELEGRAM_BOT_TOKEN in .env")
        return False
    url = TELEGRAM_API_BASE.format(token=token, method="sendMessage")
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                log.warning("telegram_api_error", body=body)
                return False
            return True
    except Exception as e:
        log.warning("telegram_send_failed", error=str(e))
        return False


def telegram_get_updates(offset: int | None = None, timeout: int = 0) -> list[dict]:
    """Получить новые сообщения боту через long-polling.

    Используется командой `pharmacy-monitor telegram poll` чтобы получить chat_id
    нового пользователя.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    url = TELEGRAM_API_BASE.format(token=token, method="getUpdates")
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(full_url, timeout=timeout + 5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                return []
            return body.get("result", [])
    except Exception as e:
        log.warning("telegram_poll_failed", error=str(e))
        return []


def resolve_recipients(explicit: list[str] | None = None) -> list[str]:
    """Собрать список получателей по приоритету."""
    if explicit:
        return explicit
    try:
        Session = storage.make_session()
        with Session() as s:
            from_db = watchlist.active_recipient_emails(s)
            if from_db:
                return from_db
    except Exception as e:
        log.warning("recipients_db_lookup_failed", error=str(e))
    return [s.strip() for s in os.environ.get("EMAIL_TO", "").split(",") if s.strip()]


# Без таймаута smtplib ждёт зависший сервер бесконечно, а письмо шлют и
# короткие таймерные задачи (intraday-тик с 20-минутным лимитом systemd).
# Таймаут — на каждую сетевую операцию (отправка тела письма — одна операция),
# поэтому с запасом на письма с вложениями.
SMTP_TIMEOUT_SEC = 60


def send_email(
    subject: str,
    html_body: str,
    attachments: list[tuple[str, bytes, str]] | None = None,
    to: list[str] | None = None,
) -> bool:
    """Send an email, returning whether SMTP delivery was attempted successfully.

    ``False`` means SMTP is not configured. Existing fire-and-forget callers may
    ignore the return value; stateful callers must require ``True`` before they
    mark a notification as delivered.
    """
    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host:
        log.info("email_skipped_no_smtp", subject=subject)
        return False
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASSWORD"]
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)

    recipients = resolve_recipients(to)
    if not recipients:
        raise ValueError(
            "No recipients configured. Add via "
            "`pharmacy-monitor recipient add EMAIL` or set EMAIL_TO in .env."
        )

    msg = EmailMessage()
    msg["From"] = smtp_from
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content("Откройте письмо в HTML-совместимом клиенте.")
    msg.add_alternative(html_body, subtype="html")

    for fname, content, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=fname)

    log.info("smtp_send", host=smtp_host, port=smtp_port, recipients=recipients)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=SMTP_TIMEOUT_SEC) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    log.info("smtp_sent_ok")
    return True
