"""Журнал uvicorn — без строки запроса.

На каждый ответ uvicorn пишет в журнал сервиса адрес запроса целиком:

    127.0.0.1:46808 - "GET /auth/verify?token=<токен входа> HTTP/1.1" 303 See Other

В строке запроса ездит токен входа из письма. Он одноразовый и живёт 30 минут,
но при ответе 5xx остаётся непогашенным, а журнал лежит на сервере неделями.

Здесь всё после `?` заменяется меткой — целиком, без списка «опасных»
параметров: список, в котором нет завтрашнего параметра, пропускает его молча.
Метод, путь и статус остаются: по ним строку журнала сводят с трассировкой
ошибки, которую uvicorn печатает рядом, — адреса запроса в трассировке нет.

Почему фильтр в коде, а не `--no-access-log` в systemd-юните. Юнит на сервере
ставит root руками, отдельно от выкладки, и с файлом в репозитории он уже
расходился; фильтр приезжает с кодом и не зависит от того, какими флагами
запущен uvicorn. Ставит его `src.api` при импорте: uvicorn настраивает журнал
до загрузки приложения, первый запрос приходит после.

Адрес запроса uvicorn пишет в двух местах, и закрыты оба:
  - журнал доступа, логгер `uvicorn.access` — строка на каждый ответ;
  - запрос на WebSocket, логгер `uvicorn.error`: `"WebSocket /путь?запрос" 403`.
    Маршрутов WebSocket у приложения нет, но строку uvicorn пишет и на отказ.

Чего фильтр НЕ делает:
  - путь остаётся как есть — секрету в пути маршрута он не поможет (таких
    маршрутов нет, за этим следит tests/test_access_log_carries_no_query.py);
  - журнал Caddy перед API пишет тот же адрес с тем же токеном — это настройка
    Caddyfile, не этого процесса;
  - с `--log-level trace` uvicorn печатает сообщения ASGI целиком, со строкой
    запроса внутри; на сервере уровень обычный.
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
    записи процесс говорит один раз, без её содержимого.

    `only_records_with` — для логгера, который пишет не только о запросах:
    запись без этого куска в шаблоне фильтр не трогает и не теряет.
    """

    def __init__(self, *, only_records_with: str | None = None) -> None:
        super().__init__()
        self._only_records_with = only_records_with
        self._drop_reported = False

    def filter(self, record: logging.LogRecord) -> bool:
        template = record.msg if isinstance(record.msg, str) else None
        marker = self._only_records_with
        if marker is not None and (template is None or marker not in template):
            return True
        try:
            args = record.args
            if template is not None and "?" not in template and isinstance(args, tuple):
                if args:
                    record.args = tuple(_without_query(arg) for arg in args)
                return True
            self._report_drop(record)
        except Exception:
            # Фильтр зовётся на каждом ответе, внутри uvicorn: исключение отсюда
            # оборвало бы запрос. Запись, которую не удалось разобрать, теряется.
            pass
        return False

    def _report_drop(self, record: logging.LogRecord) -> None:
        if self._drop_reported:
            return
        self._drop_reported = True
        log.warning(
            "request_log_line_dropped",
            logger=record.name,
            reason="unrecognized record shape: cannot tell where the query string ends",
            args_type=type(record.args).__name__,
        )


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
