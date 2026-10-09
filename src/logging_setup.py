"""Structured logging setup — JSON to stdout (journald) + optional file with rotation.

Production setup uses systemd journal (StandardOutput=journal in unit files), so
JSON logs go to journald which can be shipped to Better Stack / Loki / ELK.

Optionally write to a rotating file too (LOG_FILE env). Useful for local dev or
fallback if journald isn't available.

Usage (call once at process start):
    from src.logging_setup import configure_logging
    configure_logging(service="api")
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

import structlog

# Почтовый адрес в готовой строке журнала. Строка бывает двух видов: как есть
# (ConsoleRenderer) и JSON, где всё не-ASCII записано `\uXXXX`, а перевод строки
# — `\n`. Отсюда оговорки в шаблоне:
#   - буква считается буквой в обеих записях, иначе адрес с нелатинскими
#     буквами прошёл бы мимо. `\uXXXX` — запись буквы, только когда `\` перед
#     `u` сам не экранирован: чётное число `\` — это текст «A» как он есть;
#   - буква сразу после `\` в адрес не входит: это хвост `\n` или `\t`, и вырезать
#     его значило бы оставить в JSON одинокую `\`.
# Точка в домене обязательна (иначе под шаблон попал бы `admin@local`), длина
# имени ограничена, чтобы длинная строка без пробелов не разбиралась квадратично.
#
# Шаблон не полон и не должен считаться защитой секретов: имя в кавычках
# (`"имя фамилия"@…`) он не видит, у имени с апострофом или длиннее 64 знаков
# остаётся начало. Домен при этом скрыт всегда, кроме первого случая.
_ESCAPED = r"(?<!\\)(?:\\\\)*\\u[0-9a-fA-F]{4}"
_CHAR = rf"(?:(?<!\\)\w|{_ESCAPED})"
_LETTER = rf"(?:(?<!\\)[^\W\d_]|{_ESCAPED})"
_ADDRESS_RE = re.compile(
    rf"(?:{_CHAR}|[.%+\-]){{1,64}}@(?:{_CHAR}|-)+(?:\.(?:{_CHAR}|-)+)*\.{_LETTER}{{2,}}"
)
ADDRESS_MASK = "<address>"


def mask_addresses(text: str) -> str:
    """Заменить в тексте всё, что похоже на почтовый адрес.

    Журнал команды, которую запускает GitHub Actions, — журнал шага публичного
    репозитория. Адрес получателя в нём — персональные данные клиента в
    открытом доступе.
    """
    if "@" not in text:  # почти каждая строка журнала: регулярку не запускаем
        return text
    return _ADDRESS_RE.sub(ADDRESS_MASK, text)


def masking(renderer):
    """Обернуть последний процессор structlog: адреса вырезаются из готовой строки.

    Последняя линия, а не замена аккуратности в вызовах: поле с адресом, repr
    объекта, трассировка `log.exception` — что бы ни дошло до строки, адрес из
    неё уходит. Вызовы журнала проверяет `tests/test_log_carries_no_address.py`.
    """

    def render(logger, name, event_dict):
        rendered = renderer(logger, name, event_dict)
        return mask_addresses(rendered) if isinstance(rendered, str) else rendered

    return render


class MaskingFormatter(logging.Formatter):
    """Формат корневых обработчиков stdlib `logging`: адреса вырезаются из строки.

    `masking` стоит на structlog, а в тот же вывод пишут ещё и сторонние
    библиотеки — через корневые обработчики, мимо него. Источника адреса среди
    них не найдено; формат стоит, чтобы появление такого источника ничего не
    изменило. Вырезается всё, что формат отдаёт обработчику: сообщение,
    трассировка, `stack_info`. Мимо остаётся только то, что `logging` печатает
    в stderr сам, когда строку не принял поток или запись не удалось даже
    показать через `repr`.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            text = super().format(record)
        except Exception:
            # Сообщение не сошлось со своими аргументами. `logging` в ответ
            # печатает запись в stderr сам — мимо формата и вместе с аргументами.
            text = (
                f"unformattable log record from {record.name} "
                f"({record.pathname}:{record.lineno}): {record.msg!r} % {record.args!r}"
            )
        return mask_addresses(text)


_SRC_PREFIX = os.path.dirname(os.path.abspath(__file__)) + os.sep
_SQLSTATE_RE = re.compile(r"[0-9A-Z]{5}")


def error_fields(exc: BaseException) -> dict[str, object]:
    """Что о сбое пишут в журнал вместо текста ошибки и трассировки.

    Текст ошибки несёт данные, с которыми код работал: SQLAlchemy кладёт в него
    параметры запроса, а запрос бывает «найти пользователя по адресу» или
    «записать ему chat_id». Адрес из готовой строки вырезала бы маска, число —
    нет. Остаётся то, что данных не несёт:

    - `error_type` — класс ошибки;
    - `sqlstate` — код ошибки PostgreSQL, если это ошибка базы: `OperationalError`
      у SQLAlchemy — и остановка сервера (`57P01`), и сбой соединения (`08006`);
    - `error_at` — последняя строка `src/`, через которую ошибка прошла:
      `notifications.py:405 in bind_telegram`.

    Зовут из `except`, поэтому сама не бросает: чего не удалось узнать, того в
    ответе нет.
    """
    fields: dict[str, object] = {"error_type": type(exc).__name__}
    try:
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if isinstance(sqlstate, str) and _SQLSTATE_RE.fullmatch(sqlstate):
            fields["sqlstate"] = sqlstate
        tb = exc.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            # Только полный путь внутри `src/`: у кода, собранного из строки
            # (`<string>` — так SQLAlchemy строит свои методы), пути нет.
            if code.co_filename.startswith(_SRC_PREFIX):
                # Глубже по стеку — ближе к сбою: остаётся последняя строка.
                where = code.co_filename[len(_SRC_PREFIX) :]
                fields["error_at"] = f"{where}:{tb.tb_lineno} in {code.co_name}"
            tb = tb.tb_next
    except Exception:  # обработчик сбоя не должен упасть сам
        pass
    return fields


def configure_logging(service: str = "app") -> None:
    """Configure structlog → either JSON (production) or pretty (dev).

    Format is controlled by LOG_FORMAT env: "json" (default) or "console".
    Optional file rotation: LOG_FILE=/path/to/file.jsonl + LOG_FILE_MAX_MB=50.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_format = os.environ.get("LOG_FORMAT", "json").lower()
    log_file = os.environ.get("LOG_FILE")  # e.g. /var/log/pharmacy-monitor/app.jsonl

    # Note (2026-05-28): убран `structlog.stdlib.add_logger_name` —
    # он требует Logger.name attribute, а мы используем PrintLoggerFactory
    # (не stdlib). Без этого fix первый же log emit падал с AttributeError.
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Always include service tag for filtering across CLI / API / scraper
        _add_service_tag(service),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_format == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [masking(renderer)],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Stdlib logging → forward to structlog so 3rd-party libs get same format
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(MaskingFormatter("%(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Optional rotating file handler
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = int(os.environ.get("LOG_FILE_MAX_MB", "50")) * 1024 * 1024
        backups = int(os.environ.get("LOG_FILE_BACKUPS", "5"))
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(MaskingFormatter("%(message)s"))
        root.addHandler(file_handler)


def _add_service_tag(service: str):
    """Processor that injects service=... into every log event."""

    def add(_, __, event_dict):
        event_dict.setdefault("service", service)
        return event_dict

    return add
