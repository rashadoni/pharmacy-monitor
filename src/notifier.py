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
from src.logging_setup import mask_addresses

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


# === TELEGRAM: РАЗМЕТКА ===
# Сообщения идут в разметке Markdown — той, что Bot API называет legacy. В ней
# `_`, `*`, `` ` `` и `[` открывают сущность, и сообщение с незакрытой сущностью
# Telegram не принимает целиком: 400 «can't parse entities». Закрытую он
# принимает, но знаки из текста пропадают: «HYAL [PRE] KURSOR» приходит как
# «HYAL PRE KURSOR». Поэтому строка, которую писали не мы (название товара,
# текст проблемы health, слово, набранное человеком), попадает в сообщение
# только через `telegram_escape`.

_TELEGRAM_MARKERS = re.compile(r"[_*`\[]")
_TELEGRAM_WRAPS = ("*", "_", "`")


def telegram_escape(text: str | None, wrap: str = "") -> str:
    """Строка извне — в сообщение с разметкой Markdown, знак в знак.

    Без `wrap` строка встаёт вне сущностей: перед `_`, `*`, `` ` `` и `[`
    ставится «\\». С `wrap` («*» — жирный, «_» — курсив, «`» — моноширинный)
    строка возвращается уже обёрнутой. Внутри сущности экранирования нет — всё
    до закрывающего знака Telegram берёт как есть, — поэтому сам знак обёртки в
    строке закрывает сущность, ставится экранированным и открывает её снова
    (пример из документации Bot API: `*2*\\**2=4*`).

    Обрезать строку — до вызова: срез после него может оставить «\\» без пары.
    И не ставить знак разметки вплотную после вставки без `wrap`: строку,
    которая кончается на «\\», от него не отделить — такого экранирования в
    этой разметке нет.
    """
    text = text or ""
    if not wrap:
        return _TELEGRAM_MARKERS.sub(r"\\\g<0>", text)
    if wrap not in _TELEGRAM_WRAPS:
        raise ValueError(f"unknown Telegram Markdown entity: {wrap!r}")
    return f"\\{wrap}".join(f"{wrap}{part}{wrap}" if part else "" for part in text.split(wrap))


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
        # Пустой `parse_mode` — текст без разметки: поле в запрос не кладётся.
        **({"parse_mode": parse_mode} if parse_mode else {}),
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
        if parse_mode and isinstance(e, urllib.error.HTTPError) and e.code == 400:
            # Лучше сообщение со звёздочками, чем никакого: тот же текст уходит
            # ещё раз без разметки. Отказ Telegram приходит статусом HTTP, а 400
            # у него — и неразобранная разметка, и «нет такого чата». Чем они
            # различаются, сказано только в тексте ответа; на его формулировку
            # доставку не завязываем — повторяем на любой 400. Событие пишется,
            # когда повтор прошёл: текст без разметки принят — значит, отказали
            # из-за неё. Если не прошёл, причину запишет сам повтор.
            delivered = send_telegram_message(chat_id, text, parse_mode="")
            if delivered:
                log.warning("telegram_markup_refused", parse_mode=parse_mode)
            return delivered
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
