"""Журнал uvicorn — без строки запроса.

На каждый ответ uvicorn пишет в журнал сервиса адрес запроса целиком:

    127.0.0.1:46808 - "GET /auth/verify?token=<токен входа> HTTP/1.1" 303 See Other

В строке запроса ездит токен входа из письма. В журнал он попадает раньше, чем
погашен, а гасит его только успешный вход: после ответа 5xx и после запроса,
который до проверки не дошёл (`HEAD` — 405), токен из журнала ещё действует,
пока не выйдут его 30 минут.

Здесь всё после `?` заменяется меткой — целиком, без списка «опасных»
параметров: список, в котором нет завтрашнего параметра, пропускает его молча.
Метод, путь и статус остаются: по ним строку журнала сводят с трассировкой
ошибки, которую uvicorn печатает рядом, — адреса запроса в трассировке нет.

Почему фильтр в коде, а не `--no-access-log` в systemd-юните. Юнит на сервере
ставит root руками, отдельно от выкладки; фильтр приезжает с кодом. Ставит его
`src.api` при импорте — в каждом рабочем процессе, до первого запроса.

При обычном уровне журнала uvicorn пишет адрес запроса в двух местах:
  - журнал доступа, логгер `uvicorn.access` — строка на каждый ответ. Запись
    незнакомого вида здесь не печатается вовсе;
  - запрос на WebSocket, логгер `uvicorn.error`: `"WebSocket /путь?запрос" 403`.
    Маршрутов WebSocket у приложения нет, но строку uvicorn пишет и на отказ.
    Этот логгер пишет не только о запросах, поэтому запись узнаётся по куску
    шаблона — и если uvicorn шаблон переименует, пройдёт как есть. Входа по
    ссылке через WebSocket нет, чужому токену взяться в такой строке неоткуда.

Чего фильтр НЕ делает:
  - путь остаётся как есть — секрету в пути маршрута он не поможет (таких
    маршрутов нет, за этим следит tests/test_access_log_carries_no_query.py);
  - уровень журнала `debug` или `trace` (флаг `--log-level`, переменная
    `UVICORN_LOG_LEVEL`): библиотека websockets и сам uvicorn печатают запрос
    целиком, мимо обеих записей выше;
  - журнал Caddy перед API пишет адрес сам — это настройка Caddyfile, не этого
    процесса;
  - событие Sentry: адрес запроса SDK берёт не из журнала, его чистит
    `src/observability.py`. Из журнала SDK берёт только «хлебную крошку» —
    строку журнала доступа, и она уходит уже с меткой;
  - текст исключения в трассировке не трогается.
"""

from __future__ import annotations

import logging

import structlog

log = structlog.get_logger()

ACCESS_LOGGER = "uvicorn.access"
ERROR_LOGGER = "uvicorn.error"
# Без пробелов: строка запроса в журнале по-прежнему делится на три слова.
QUERY_HIDDEN = "?<hidden>"
# По этому куску шаблона запись о запросе на WebSocket отличается от прочих
# записей `uvicorn.error` — о старте, остановке, ошибке приложения.
_WEBSOCKET_REQUEST = '"WebSocket '


def _without_query(value: object) -> object:
    """Аргумент записи без строки запроса: всё, начиная с первого `?`, — метка.

    Числа и None идут как есть. У остального смотрится текст, которым аргумент
    встанет в строку журнала: `?` в пути uvicorn экранирует (`%3F`), поэтому
    первый `?` — всегда начало строки запроса.
    """
    if value is None or isinstance(value, (int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    head, has_query, _ = text.partition("?")
    return head + QUERY_HIDDEN if has_query else value


class QueryHidden(logging.Filter):
    """Фильтр логгера uvicorn: запись о запросе уходит дальше без строки запроса.

    Запись uvicorn — шаблон без `?` и аргументы кортежем; адрес с запросом —
    один из аргументов. Чистятся все аргументы, а не третий по счёту: место
    адреса в кортеже — деталь uvicorn, и полагаться на неё незачем.

    Запись о запросе другого вида не пропускается вовсе: готовый текст в `msg`,
    шаблон с `?`, аргументы словарём — по ним нельзя сказать, где кончается
    запрос. Потерянная строка журнала лучше строки с токеном; о первой такой
    записи фильтр говорит один раз, без её содержимого.

    `only_records_with` — для логгера, который пишет не только о запросах:
    запись без этого куска в шаблоне фильтр не трогает и не теряет.
    """

    def __init__(self, *, only_records_with: str | None = None) -> None:
        super().__init__()
        self._only_records_with = only_records_with
        self._drop_reported = False

    def filter(self, record: logging.LogRecord) -> bool:
        # Фильтр зовётся на каждом ответе, внутри uvicorn: исключение отсюда
        # оборвало бы запрос. Запись, которую не удалось разобрать, теряется.
        try:
            template = record.msg if isinstance(record.msg, str) else None
            marker = self._only_records_with
            if marker is not None and (template is None or marker not in template):
                return True
            args = record.args
            if template is not None and "?" not in template and isinstance(args, tuple):
                if args:
                    record.args = tuple(_without_query(arg) for arg in args)
                return True
        except Exception:
            pass
        self._report_drop(record)
        return False

    def _report_drop(self, record: logging.LogRecord) -> None:
        if self._drop_reported:
            return
        self._drop_reported = True
        try:
            log.warning(
                "request_log_line_dropped",
                logger=record.name,
                reason="unrecognized record shape: cannot tell where the query string ends",
                args_type=type(record.args).__name__,
            )
        except Exception:
            pass


def _installed(logger: logging.Logger) -> bool:
    return any(isinstance(existing, QueryHidden) for existing in logger.filters)


def hide_query_strings() -> None:
    """Поставить фильтры на журнал uvicorn. Повторный вызов ничего не меняет."""
    access = logging.getLogger(ACCESS_LOGGER)
    if not _installed(access):
        access.addFilter(QueryHidden())
    errors = logging.getLogger(ERROR_LOGGER)
    if not _installed(errors):
        errors.addFilter(QueryHidden(only_records_with=_WEBSOCKET_REQUEST))
