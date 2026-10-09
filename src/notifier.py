"""SMTP-отправка отчёта (HTML + Excel-вложение).

Получатели берутся в таком порядке:
1. Аргумент `to=...` (если передан)
2. Активные записи из БД (таблица recipients)
3. EMAIL_TO в .env (fallback)
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

import structlog

from src import storage, watchlist
from src.logging_setup import error_fields, mask_addresses

log = structlog.get_logger()


_SMTP_STATUS_RE = re.compile(r"[245]\.\d{1,3}\.\d{1,3}(?![\d.])")


class EmailDeliveryError(smtplib.SMTPException):
    """Почтовый сервер не принял письмо.

    Текст ошибки — класс исходной ошибки smtplib и коды ответа, без самого
    ответа сервера. smtplib кладёт в текст своих ошибок адрес получателя, а
    текст ошибки уходит дальше, чем её ловят: в поле журнала, в трассировку
    `run_failed`. Ответ сервера с вырезанными адресами лежит в `server_reply` —
    его печатает только `notify test`, которую оператор запускает сам.

    `smtp_status` — расширенный код из начала ответа («5.1.1»): 550 сервер
    ставит на десяток разных причин, а различает их этим кодом.
    """

    def __init__(self, error_type: str, smtp_code: int | None = None, server_reply: str = ""):
        self.error_type = error_type
        self.smtp_code = smtp_code
        self.server_reply = server_reply
        status = _SMTP_STATUS_RE.match(server_reply)
        self.smtp_status = status.group(0) if status else None
        codes = " ".join(str(code) for code in (smtp_code, self.smtp_status) if code)
        super().__init__(f"{error_type} (SMTP {codes})" if codes else error_type)


def _delivery_failure(exc: smtplib.SMTPException) -> EmailDeliveryError:
    code: object = getattr(exc, "smtp_code", None)
    reply: object = getattr(exc, "smtp_error", None)
    if isinstance(exc, smtplib.SMTPRecipientsRefused) and exc.recipients:
        # {адрес: (код, ответ сервера)} — нужен ответ, адрес-ключ не нужен.
        code, reply = next(iter(exc.recipients.values()))
    if reply is None:
        reply = str(exc)
    if isinstance(reply, bytes):
        reply = reply.decode("utf-8", "replace")
    return EmailDeliveryError(
        type(exc).__name__,
        code if isinstance(code, int) else None,
        mask_addresses(str(reply)),
    )


def delivery_error_fields(exc: BaseException) -> dict[str, object]:
    """Что о сбое доставки пишут в журнал: класс ошибки и её числовые коды.

    Текст ошибки (`str(exc)`) в журнал не идёт: в нём бывает адрес получателя.
    Кому не ушло, вызывающий пишет рядом как `user_id`.
    """
    refused = isinstance(exc, EmailDeliveryError)
    fields: dict[str, object] = {"error_type": exc.error_type if refused else type(exc).__name__}
    smtp_code = getattr(exc, "smtp_code", None)
    if isinstance(smtp_code, int):
        fields["smtp_code"] = smtp_code
    if refused and exc.smtp_status:
        fields["smtp_status"] = exc.smtp_status
    elif isinstance(exc, OSError) and isinstance(exc.errno, int):
        fields["errno"] = exc.errno  # сеть: 101 — нет маршрута, 111 — отказ в соединении
    return fields


# === TELEGRAM ===
# Используем raw HTTP (через stdlib) — никаких внешних библиотек.
# Bot создаётся через @BotFather, токен кладётся в .env как TELEGRAM_BOT_TOKEN.

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _telegram_error_fields(exc: BaseException) -> dict[str, object]:
    """Что о сбое запроса к Telegram пишут в журнал вместо текста ошибки.

    Токен бота стоит в адресе запроса, а текст ошибки адрес несёт: токен с
    управляющим символом (строка env с CRLF) даёт `http.client.InvalidURL`, и в
    её тексте путь запроса лежит целиком. Маска журнала вырезает только почтовые
    адреса. Пишутся класс ошибки и строка этого модуля (`error_fields`), а чем
    случаи различаются — числами и именами классов:

    - `http_status` — код ответа: 401 — токен не подошёл, 400 — запрос не
      принят (нет такого чата, не разобрана разметка), 403 — человек заблокировал
      бота, 409 — тем же токеном опрашивает второй бот, 429 — слишком часто;
    - `reason_type` и `errno` — что стоит за `URLError`: нет имени в DNS
      (`gaierror`), отказ в соединении (`ConnectionRefusedError`, 111), сертификат.
    """
    fields = error_fields(exc)
    if isinstance(exc, urllib.error.HTTPError):
        fields["http_status"] = exc.code
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        fields["reason_type"] = type(reason).__name__
        if isinstance(reason, OSError) and isinstance(reason.errno, int):
            fields["errno"] = reason.errno
    return fields


def _telegram_refusal_fields(body: dict) -> dict[str, object]:
    """Отказ Telegram в ответе с `ok: false`: из ответа берётся только `error_code`.

    Сам ответ в журнал не идёт: в `parameters.migrate_to_chat_id` стоит
    идентификатор чата, а `description` — текст, который пишет сервер.
    """
    code = body.get("error_code")
    return {"error_code": code} if type(code) is int else {}


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
                log.warning(
                    "telegram_api_error", method="sendMessage", **_telegram_refusal_fields(body)
                )
                return False
            return True
    except Exception as e:
        log.warning("telegram_send_failed", **_telegram_error_fields(e))
        return False


def telegram_get_updates(offset: int | None = None, timeout: int = 0) -> list[dict] | None:
    """Получить новые сообщения боту через long-polling.

    `None` — опрос не состоялся: нет токена, сеть, отказ Telegram; причина уже в
    журнале. Пустой список — опрос состоялся, сообщений нет. Это разные ответы:
    бот после несостоявшегося опроса ждёт, а `telegram poll` не говорит
    оператору «сообщений нет», когда не подошёл токен.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("telegram_no_token", hint="set TELEGRAM_BOT_TOKEN in .env")
        return None
    url = TELEGRAM_API_BASE.format(token=token, method="getUpdates")
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(full_url, timeout=timeout + 5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                log.warning(
                    "telegram_api_error", method="getUpdates", **_telegram_refusal_fields(body)
                )
                return None
            return body.get("result") or []
    except Exception as e:
        log.warning("telegram_poll_failed", **_telegram_error_fields(e))
        return None


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

    An SMTP refusal is raised as ``EmailDeliveryError``, whose text never
    carries an address; network errors propagate as they are.
    """
    try:
        return _send_email(subject, html_body, attachments, to)
    except smtplib.SMTPException as exc:
        failure = _delivery_failure(exc)
    # Вне except: у новой ошибки нет ни причины, ни контекста, и трассировка не
    # покажет исходную — с адресом.
    raise failure


def _send_email(
    subject: str,
    html_body: str,
    attachments: list[tuple[str, bytes, str]] | None,
    to: list[str] | None,
) -> bool:
    """Сам разговор с почтовым сервером. Ошибки smtplib выходят отсюда как есть,
    с адресом в тексте, — звать только через `send_email`."""
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

    # Число, а не адреса: журнал еженедельного сбора pharmonline — это журнал
    # шага GitHub Actions публичного репозитория.
    log.info("smtp_send", host=smtp_host, port=smtp_port, recipients=len(recipients))
    with smtplib.SMTP(smtp_host, smtp_port, timeout=SMTP_TIMEOUT_SEC) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    log.info("smtp_sent_ok")
    return True
