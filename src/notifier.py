"""SMTP-отправка отчёта (HTML + Excel-вложение).

Получатели берутся в таком порядке:
1. Аргумент `to=...` (если передан)
2. Активные записи из БД (таблица recipients)
3. EMAIL_TO в .env (fallback)

Запись, которая не адрес (`src/email_address.py`), в письмо не попадает: письмо
на список одно, и такая запись срывала его всем. О пропуске — событие
`email_recipients_skipped` с номером записи и кодом причины.
"""

from __future__ import annotations

import email.errors
import json
import os
import re
import smtplib
import ssl
import urllib.parse
import urllib.request
from email.message import EmailMessage

import structlog

from src import storage, watchlist
from src.email_address import InvalidEmailAddress, normalize_address
from src.logging_setup import TEXT_WITHHELD, mask_addresses, without_addresses

log = structlog.get_logger()


_SMTP_STATUS_RE = re.compile(r"[245]\.\d{1,3}\.\d{1,3}(?![\d.])")


class EmailSendError(Exception):
    """Письмо не ушло. Что бы ни сломалось при отправке, из `send_email` выходит
    эта ошибка; отказ почтового сервера — её наследником `EmailDeliveryError`.

    Текст ошибки уходит дальше, чем её ловят: в трассировку `run_failed`, в
    строку «Error: …» от click, в `runs.error_message`, в вывод команды, которая
    сбой не ловит вовсе (`report --send`). Поэтому адреса в нём нет: класс
    исходной ошибки, её текст (если в нём не было «@») и строка этого модуля,
    на которой она возникла; у отказа сервера — класс и коды ответа. Самой
    исходной ошибки при этой нет — ни причиной, ни контекстом, — и её
    трассировки тоже.

    Чего это не закрывает: трассировку с локальными переменными кадров
    (`capture_locals`, rich, Sentry) — в кадре `send_email` лежит `to`; и
    вызов из чужого `except` — контекстом этой ошибки станет та, которую
    обрабатывал вызывающий, а её текст не чистил никто.

    `error_type` — класс исходной ошибки, `errno` — её код, если она сетевая.
    """

    def __init__(self, error_type: str, text: str = "", errno: int | None = None):
        self.error_type = error_type
        self.errno = errno
        super().__init__(text or error_type)


class EmailDeliveryError(EmailSendError):
    """Почтовый сервер не принял письмо.

    Текст ошибки — класс исходной ошибки smtplib и коды ответа, без самого
    ответа сервера: smtplib кладёт в текст своих ошибок адрес получателя. Ответ
    сервера лежит в `server_reply`: адрес привычной записи из него вырезан
    маской, а если после неё остался «@», ответа нет вовсе. Знака «@» в нём не
    бывает, но начало имени ящика (`o'<address>`), имя рядом с адресом и ящик,
    названный без «@» («user viewer unknown»), остаются. Поэтому его печатает
    одна `notify test`, которую оператор запускает сам, а в журнал он не идёт.
    Чистит ответ `send_email`; кто собирает эту ошибку сам, чистит сам.

    `smtp_status` — расширенный код из начала ответа («5.1.1»): 550 сервер
    ставит на десяток разных причин, а различает их этим кодом.
    """

    def __init__(
        self,
        error_type: str,
        smtp_code: int | None = None,
        server_reply: str = "",
        smtp_status: str | None = None,
    ):
        self.smtp_code = smtp_code
        self.server_reply = server_reply
        self.smtp_status = smtp_status or _smtp_status(server_reply)
        codes = " ".join(str(code) for code in (smtp_code, self.smtp_status) if code)
        super().__init__(error_type, f"{error_type} (SMTP {codes})" if codes else error_type)


def _smtp_status(reply: str) -> str | None:
    status = _SMTP_STATUS_RE.match(reply)
    return status.group(0) if status else None


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
    reply = str(reply)
    return EmailDeliveryError(
        type(exc).__name__,
        code if isinstance(code, int) else None,
        without_addresses(mask_addresses(reply)),
        _smtp_status(reply),  # до вырезания: скрытый ответ кода уже не несёт
    )


def _text_without_addresses(exc: Exception) -> str:
    """Текст ошибки, каким его можно показать.

    Текст с «@» не показывается вовсе (`without_addresses`). Ошибки пакета
    `email` несут в тексте сам заголовок письма: `HeaderWriteError` печатает
    `To:` целиком, со всеми получателями, а нелатинская часть записана в нём
    base64, без единого «@». Их текст не показывается никогда.
    """
    if isinstance(exc, email.errors.MessageError):
        return TEXT_WITHHELD
    return without_addresses(str(exc))


def _raised_at(exc: BaseException) -> str:
    """На какой строке этого модуля возникла ошибка: `notifier.py:250 in _send_email`.

    По ней видно, на чём письмо остановилось — соединение, вход, отправка, —
    когда трассировки исходной ошибки уже нет.
    """
    at = ""
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        if code.co_filename == __file__:
            at = f"{os.path.basename(__file__)}:{tb.tb_lineno} in {code.co_name}"
        tb = tb.tb_next
    return at


def _send_failure(exc: Exception) -> EmailSendError:
    """Чем заменить пойманную при отправке ошибку, чтобы выпустить её наружу."""
    if isinstance(exc, smtplib.SMTPException):
        return _delivery_failure(exc)
    error_type = type(exc).__name__
    text, at = _text_without_addresses(exc), _raised_at(exc)
    shown = f"{error_type}: {text}" if text else error_type
    errno = exc.errno if isinstance(exc, OSError) and isinstance(exc.errno, int) else None
    return EmailSendError(error_type, f"{shown} ({at})" if at else shown, errno)


def delivery_error_fields(exc: BaseException) -> dict[str, object]:
    """Что о сбое доставки пишут в журнал: класс ошибки и её числовые коды.

    Текст ошибки (`str(exc)`) в журнал не идёт. У ошибки из `send_email` он уже
    без адреса, но в `try` обработчика стоит не только отправка, а ошибку базы
    или вёрстки письма никто не чистил. Кому не ушло, вызывающий пишет рядом как
    `user_id`.
    """
    cleaned = isinstance(exc, EmailSendError)
    fields: dict[str, object] = {"error_type": exc.error_type if cleaned else type(exc).__name__}
    smtp_code = getattr(exc, "smtp_code", None)
    if isinstance(smtp_code, int):
        fields["smtp_code"] = smtp_code
    if isinstance(exc, EmailDeliveryError) and exc.smtp_status:
        fields["smtp_status"] = exc.smtp_status
    errno = exc.errno if cleaned or isinstance(exc, OSError) else None
    if isinstance(errno, int):
        fields["errno"] = errno  # сеть: 101 — нет маршрута, 111 — отказ в соединении
    return fields


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


class NoValidRecipients(ValueError):
    """Слать некому: ни одна запись получателя не адрес."""


def _recipient_records(explicit: list[str] | None = None) -> tuple[str, list[tuple[int, str]]]:
    """Список получателей по приоритету: откуда он взят и его записи как есть,
    без проверки, — (номер, запись).

    Номер — то, чем запись называют в журнале вместо адреса: `id` строки
    `recipients`, а у `EMAIL_TO` и у аргумента `to` — место в списке, с единицы.
    """
    if explicit:
        return "to", list(enumerate(explicit, 1))
    try:
        Session = storage.make_session()
        with Session() as s:
            from_db = watchlist.active_recipients(s)
            if from_db:
                return "recipients", from_db
    except Exception as e:
        log.warning("recipients_db_lookup_failed", error=str(e))
    from_env = [s.strip() for s in os.environ.get("EMAIL_TO", "").split(",") if s.strip()]
    return "EMAIL_TO", list(enumerate(from_env, 1))


def _addresses_only(source: str, records: list[tuple[int, str]]) -> list[str]:
    """Адреса из записей списка; запись, которая не адрес, пропускается.

    Письмо на список одно, заголовок `To:` общий: запись, которая не адрес,
    ломает его сборку, и письмо не уходит никому. Поэтому она остаётся без
    письма одна, а в журнале о ней — номер и код причины (`email_address.PROBLEMS`),
    не сама запись: в ней бывает адрес с опечаткой.
    """
    addresses, skipped = [], {}
    for number, record in records:
        try:
            addresses.append(normalize_address(record))
        except InvalidEmailAddress as refusal:
            skipped[number] = refusal.problem
    if skipped:
        numbers = "recipient_ids" if source == "recipients" else "positions"
        log.warning(
            "email_recipients_skipped",
            source=source,
            skipped=len(skipped),
            sending_to=len(addresses),
            problems=sorted(set(skipped.values())),
            **{numbers: sorted(skipped)},
        )
    return addresses


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

    Whatever fails on the way, the caller gets an ``EmailSendError`` whose text
    never carries an address (``EmailDeliveryError`` when the server refused).
    Nothing else may talk to the mail server: "no address reaches the log" rests
    on this function being the only way a send failure gets out.
    """
    try:
        try:
            return _send_email(subject, html_body, attachments, to)
        except Exception as exc:
            try:
                failure = _send_failure(exc)
            except Exception:  # у ошибки не удалось взять даже текст
                failure = EmailSendError(type(exc).__name__)
    except BaseException as interrupt:
        # Прерывание (KeyboardInterrupt, SystemExit) проходит как есть, но без
        # ошибки, при обработке которой оно пришло — будь то закрытие соединения
        # после отказа или очистка строкой выше: в её тексте адрес.
        interrupt.__cause__ = interrupt.__context__ = None
        raise
    # Вне except: у новой ошибки нет ни причины, ни контекста, и трассировка не
    # покажет исходную — с адресом.
    raise failure


def _send_email(
    subject: str,
    html_body: str,
    attachments: list[tuple[str, bytes, str]] | None,
    to: list[str] | None,
) -> bool:
    """Сборка письма и разговор с почтовым сервером. Ошибки выходят отсюда как
    есть, с адресом в тексте, — звать только через `send_email`."""
    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host:
        log.info("email_skipped_no_smtp", subject=subject)
        return False
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASSWORD"]
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)

    source, records = _recipient_records(to)
    if not records:
        raise ValueError(
            "No recipients configured. Add via "
            "`pharmacy-monitor recipient add EMAIL` or set EMAIL_TO in .env."
        )
    recipients = _addresses_only(source, records)
    if not recipients:
        raise NoValidRecipients(
            f"none of the {len(records)} recipient record(s) from {source} is an email address"
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
