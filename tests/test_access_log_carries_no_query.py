"""Журнал процесса API не несёт строки запроса.

Что было. uvicorn пишет в журнал сервиса адрес каждого запроса целиком, и токен
входа из письма ложился туда дословно — на сервере это journald:

    127.0.0.1:46808 - "GET /auth/verify?token=<токен> HTTP/1.1" 303 See Other

Как проверяется. Настоящее приложение поднимается под настоящим uvicorn,
отдельным процессом, с флагами из самого файла юнита
(`infra/systemd/pharmacy-monitor-api.service`). От запуска на сервере отличаются
порт, обёртка приложения (tests/access_log_probe.py — она нужна ради ответа 500
и контроля), `python -m uvicorn` вместо скрипта из venv и окружение: оно
собрано заново, из переменных сервера в нём нет ни одной. По настоящей ссылке
на вход приходит запрос, и токен ищется во всём, что процесс напечатал: stdout
и stderr вместе, родительского процесса и обоих рабочих.

Два запуска:
  - `shipped` — как на сервере;
  - `unfiltered` — фильтры `src.access_log` сняты. Это контроль: проба обязана
    увидеть токен, иначе её «чисто» в первом запуске ничего не значит.

Чего эти тесты НЕ проверяют: журнал Caddy (он пишет адрес сам, и это не этот
процесс); события Sentry и параметры SQL в тексте ошибки базы — у них свои
тесты (test_sentry_events_carry_no_credentials.py,
test_statement_data_stays_out_of_errors.py); уровень журнала `debug` и `trace`;
запуск с настроенным Sentry (здесь `SENTRY_DSN` не задан); версии uvicorn,
кроме той, что стоит у запускающего. Какой из двух рабочих процессов ответил на
запрос, проба не выбирает.
"""

from __future__ import annotations

import http.client
import logging
import logging.config
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from src import access_log, storage, tenants
from src._time import utcnow

REPO = Path(__file__).resolve().parents[1]
API_UNIT = REPO / "infra/systemd/pharmacy-monitor-api.service"
PROBE_APP = "tests.access_log_probe:app"
MODES = ("shipped", "unfiltered")

FAULT_EMAIL = "fault@access-log-probe.example"
# Этим пользователям выпускается по настоящей ссылке на вход.
LINK_OWNERS = ("browser", "json", "fault", "head")

BOGUS_TOKEN = "bogus-CANARY-token-e2f0a6b1"
WEBSOCKET_TOKEN = "websocket-CANARY-token-7d1e03b9"
SECOND_PLACE_TOKEN = "second-CANARY-token-44cd58d0"
SEARCH_TERM = "kreon-CANARY-search"
POSTED_TOKEN = "posted-CANARY-token-91ab3c7e"
ODD_NAME_VALUE = "oddname-CANARY-value-5f1c9a77"
BARE_VALUE = "bare-CANARY-value-d2e04b68"
LONG_QUERY_VALUE = "long-CANARY-value-0a1b2c3d"


# ─── Фильтр: что он делает с записью ─────────────────────────────────────────

# Шаблоны uvicorn 0.46–0.52: журнал доступа и три записи о запросе на WebSocket.
_ACCESS_TEMPLATE = '%s - "%s %s HTTP/%s" %d'
_WEBSOCKET_TEMPLATES = (
    '%s - "WebSocket %s" [accepted]',
    '%s - "WebSocket %s" 403',
    '%s - "WebSocket %s" %d',
)
_CANARY = "magic-CANARY-token-0a1b2c3d"


def _record(msg: object, args: object, name: str = access_log.ACCESS_LOGGER) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 0, msg, args, None)


def _query_filters(name: str) -> list[access_log.QueryHidden]:
    return [f for f in logging.getLogger(name).filters if isinstance(f, access_log.QueryHidden)]


def _shipped_filter(name: str) -> access_log.QueryHidden:
    """Фильтр логгера, как его ставит `hide_query_strings`, — а не собранный в тесте."""
    logging.getLogger(name).filters.clear()
    access_log.hide_query_strings()
    (shipped,) = _query_filters(name)
    return shipped


@pytest.fixture(autouse=True)
def _uvicorn_loggers_as_found():
    """Логгеры uvicorn живут на весь процесс — после теста они такие же, как до него."""
    names = ("uvicorn", access_log.ACCESS_LOGGER, access_log.ERROR_LOGGER)
    before = [
        (logger, list(logger.filters), list(logger.handlers), logger.level, logger.propagate)
        for logger in map(logging.getLogger, names)
    ]
    yield
    for logger, filters, handlers, level, propagate in before:
        logger.filters[:] = filters
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


class _Recorder:
    """Вместо `access_log.log`: что процесс сказал бы о потерянной записи."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def warning(self, event: str, **fields) -> None:
        self.events.append((event, fields))


@pytest.mark.parametrize(
    "target",
    [
        f"/auth/verify?token={_CANARY}",
        f"/auth/verify?utm_source=mail&token={_CANARY}",
        # Второй `?` — уже часть запроса: резать по последнему значило бы
        # оставить в журнале начало.
        f"/auth/verify?token={_CANARY}?again={_CANARY}",
        f"/api/v1/dash/comparison?search={_CANARY}&min_spread=5",
    ],
)
def test_access_record_keeps_method_path_and_status_and_loses_the_query(target):
    from uvicorn.logging import AccessFormatter

    record = _record(_ACCESS_TEMPLATE, ("203.0.113.7:51324", "GET", target, "1.1", 303))

    assert _shipped_filter(access_log.ACCESS_LOGGER).filter(record) is True

    path = target.partition("?")[0]
    assert record.getMessage() == f'203.0.113.7:51324 - "GET {path}?<hidden> HTTP/1.1" 303'
    # Строку печатает форматтер uvicorn — и он берёт аргументы записи, а не текст.
    printed = AccessFormatter(
        fmt='%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        use_colors=False,
    ).format(record)
    assert _CANARY not in printed
    assert f'"GET {path}?<hidden> HTTP/1.1" 303' in printed


def test_access_record_without_a_query_is_left_as_it_is():
    args = ("203.0.113.7:51324", "GET", "/health", "1.1", 200)
    record = _record(_ACCESS_TEMPLATE, args)

    assert _shipped_filter(access_log.ACCESS_LOGGER).filter(record) is True

    assert record.args == args
    assert record.getMessage() == '203.0.113.7:51324 - "GET /health HTTP/1.1" 200'


def test_the_address_is_found_wherever_it_stands_in_the_arguments():
    """Место адреса в кортеже — деталь uvicorn; фильтр на неё не опирается."""

    class Target:
        def __str__(self) -> str:
            return f"/auth/verify?token={_CANARY}"

    record = _record(
        '%s "%s" %s %s - %d (%s)',
        (f"/a?token={_CANARY}", Target(), "GET", None, 200, 0.25),
    )

    assert _shipped_filter(access_log.ACCESS_LOGGER).filter(record) is True

    assert record.getMessage() == '/a?<hidden> "/auth/verify?<hidden>" GET None - 200 (0.25)'


@pytest.mark.parametrize(
    ("msg", "args"),
    [
        # Готовый текст вместо шаблона с аргументами.
        (f'203.0.113.7:51324 - "GET /auth/verify?token={_CANARY} HTTP/1.1" 303', ()),
        (f'203.0.113.7:51324 - "GET /auth/verify?token={_CANARY} HTTP/1.1" %d', (303,)),
        # Аргументы словарём.
        ('%(client)s - "%(target)s"', ({"client": "c", "target": f"/a?token={_CANARY}"},)),
        # Не строка.
        (RuntimeError(f"/auth/verify?token={_CANARY}"), ()),
        (_ACCESS_TEMPLATE, None),
    ],
)
def test_a_record_of_unknown_shape_is_dropped_not_passed_on(msg, args, monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(access_log, "log", recorder)
    record = _record(msg, args)
    hidden = _shipped_filter(access_log.ACCESS_LOGGER)

    assert hidden.filter(record) is False
    assert hidden.filter(_record(msg, args)) is False

    # Сказано один раз, и в сказанном нет ничего из записи — только её вид.
    assert recorder.events == [
        (
            "request_log_line_dropped",
            {
                "logger": access_log.ACCESS_LOGGER,
                "reason": "unrecognized record shape: cannot tell where the query string ends",
                "args_type": type(record.args).__name__,
            },
        )
    ]


def test_a_record_that_cannot_be_read_is_dropped_reported_and_nothing_is_raised(monkeypatch):
    """Фильтр зовётся внутри uvicorn на каждом ответе: исключение оборвало бы запрос."""

    class Unprintable:
        def __str__(self) -> str:
            raise ValueError(f"/auth/verify?token={_CANARY}")

    class UnreadableTemplate(str):
        def __contains__(self, item) -> bool:
            raise RuntimeError(f"/auth/verify?token={_CANARY}")

    recorder = _Recorder()
    monkeypatch.setattr(access_log, "log", recorder)
    unprintable = _record(_ACCESS_TEMPLATE, ("c", "GET", Unprintable(), "1.1", 200))

    assert _shipped_filter(access_log.ACCESS_LOGGER).filter(unprintable) is False
    assert [event for event, _ in recorder.events] == ["request_log_line_dropped"]
    assert _CANARY not in repr(recorder.events)
    for name in (access_log.ACCESS_LOGGER, access_log.ERROR_LOGGER):
        unreadable = _record(UnreadableTemplate(_ACCESS_TEMPLATE), ("c",), name=name)
        assert _shipped_filter(name).filter(unreadable) is False

    class BrokenLog:
        def warning(self, *args, **kwargs) -> None:
            raise OSError("stdout закрыт")

    monkeypatch.setattr(access_log, "log", BrokenLog())
    hidden = _shipped_filter(access_log.ACCESS_LOGGER)
    assert hidden.filter(_record(f"/a?token={_CANARY}", ())) is False
    assert hidden.filter(unprintable) is False


@pytest.mark.parametrize("template", _WEBSOCKET_TEMPLATES)
def test_websocket_request_record_loses_the_query(template):
    args = ("203.0.113.7:51324", f"/auth/verify?token={_CANARY}", 403)[: template.count("%")]
    record = _record(template, args, name=access_log.ERROR_LOGGER)
    hidden = _shipped_filter(access_log.ERROR_LOGGER)

    assert hidden.filter(record) is True

    assert '"WebSocket /auth/verify?<hidden>"' in record.getMessage()
    assert _CANARY not in record.getMessage()


def test_websocket_filter_leaves_every_other_record_of_that_logger_alone(monkeypatch):
    """`uvicorn.error` пишет о старте, остановке и ошибках приложения — их терять нельзя."""
    recorder = _Recorder()
    monkeypatch.setattr(access_log, "log", recorder)
    hidden = _shipped_filter(access_log.ERROR_LOGGER)
    untouched = [
        ("Started server process [%d]", (4242,)),
        ("Exception in ASGI application\n", ()),
        ("Is the application ready? %s", ("why?",)),
        ("No supported WebSocket library detected. Please use …", ()),
        # Слово «WebSocket» без кавычки перед ним — не запись о запросе.
        ("WebSocket library missing? %s", ("why?",)),
        ("%(what)s failed?", ({"what": "x?y"},)),
        (RuntimeError("a?b"), ()),
    ]
    for msg, args in untouched:
        record = _record(msg, args, name=access_log.ERROR_LOGGER)
        kept_args = record.args

        assert hidden.filter(record) is True
        assert record.msg is msg and record.args is kept_args

    # А запись о запросе незнакомого вида теряется и здесь.
    preformatted = f'203.0.113.7:51324 - "WebSocket /auth/verify?token={_CANARY}" 403'
    assert hidden.filter(_record(preformatted, (), name=access_log.ERROR_LOGGER)) is False
    assert len(recorder.events) == 1


def test_filters_are_installed_once_and_outlive_uvicorns_own_logging_setup(monkeypatch):
    from uvicorn.config import LOGGING_CONFIG

    monkeypatch.setattr(access_log, "log", _Recorder())
    for name in (access_log.ACCESS_LOGGER, access_log.ERROR_LOGGER):
        logging.getLogger(name).filters.clear()

    access_log.hide_query_strings()
    access_log.hide_query_strings()
    # uvicorn настраивает журнал раньше, чем грузит приложение, но при запуске
    # из кода (`uvicorn.Server` после импорта) порядок обратный.
    logging.config.dictConfig(LOGGING_CONFIG)

    assert len(_query_filters(access_log.ACCESS_LOGGER)) == 1
    assert len(_query_filters(access_log.ERROR_LOGGER)) == 1
    # Строгий фильтр — только на журнале доступа: на `uvicorn.error` он терял бы
    # записи об ошибках.
    stray = _record("Is the application ready? %(answer)s", ({"answer": "no"},))
    assert _query_filters(access_log.ERROR_LOGGER)[0].filter(stray) is True
    assert _query_filters(access_log.ACCESS_LOGGER)[0].filter(stray) is False


# Имена, по которым src/observability.py узнаёт учётные данные, и три здешних.
# Просто `key` или `code` секретом не считаются: `{barcode}`, `{category_key}`.
_SECRET_LIKE_NAME = re.compile(
    r"auth|cookie|token|secret|session|passw|pwd|signature|api[-_]?key|otp|jwt|magic", re.I
)


def test_no_route_carries_a_secret_in_its_path():
    """Путь в журнале остаётся — секрет, переданный частью пути, лёг бы туда целиком."""
    from src import api

    offenders = [
        (route.path, name)
        for route in api.app.routes
        for name in re.findall(r"{(\w+)", getattr(route, "path", ""))
        if _SECRET_LIKE_NAME.search(name)
    ]
    assert not offenders, (
        f"параметр пути похож на секрет: {offenders}. Журнал доступа пишет путь целиком — "
        "секрет передают телом запроса или заголовком."
    )


# ─── Приложение под настоящим uvicorn ────────────────────────────────────────


def _unit_uvicorn_flags() -> list[str]:
    """Флаги, с которыми юнит запускает uvicorn, — из самого файла юнита."""
    text = API_UNIT.read_text(encoding="utf-8").replace("\\\n", " ")
    lines = [line.strip() for line in text.splitlines()]
    commands = [line.removeprefix("ExecStart=").strip() for line in lines if "ExecStart=" in line]
    assert len(commands) == 1, f"ожидался один ExecStart, найдено {commands}"
    argv = shlex.split(commands[0])
    assert argv[0].endswith("/uvicorn"), argv[0]
    assert argv[1] == "src.api:app", f"юнит запускает {argv[1]}, а проба оборачивает src.api:app"
    # uvicorn читает свои флаги и из окружения (`UVICORN_LOG_LEVEL=debug` печатает
    # запрос целиком). Проба окружение юнита не повторяет — пусть его там и не будет.
    in_unit = [line for line in lines if line.startswith("Environment=") and "UVICORN_" in line]
    assert not in_unit, f"юнит задаёт флаги uvicorn окружением, проба их не видит: {in_unit}"
    return argv[2:]


def _launch_command(port: int) -> list[str]:
    flags = _unit_uvicorn_flags()
    assert flags[flags.index("--host") + 1] == "127.0.0.1", "проба слушает только 127.0.0.1"
    flags[flags.index("--port") + 1] = str(port)
    return [sys.executable, "-m", "uvicorn", PROBE_APP, *flags]


def _worker_count() -> int:
    flags = _unit_uvicorn_flags()
    return int(flags[flags.index("--workers") + 1]) if "--workers" in flags else 1


def _server_environment(mode: str, database_url: str) -> dict[str, str]:
    """Окружение сервера — собранное заново: переменные запускающего (`UVICORN_*`,
    `LOG_FORMAT`, `SENTRY_DSN`, прокси) в пробу не попадают."""
    env = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR") if name in os.environ}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH")]))
    env.update(
        ACCESS_LOG_PROBE=mode,
        ACCESS_LOG_PROBE_FAULT_EMAIL=FAULT_EMAIL,
        DATABASE_URL=database_url,
        JWT_SECRET="probe-only-secret-0123456789abcdef",
        PYTHONUTF8="1",
    )
    return env


def _free_ports(count: int) -> list[int]:
    listeners = [socket.socket(socket.AF_INET, socket.SOCK_STREAM) for _ in range(count)]
    try:
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in listeners]
    finally:
        for listener in listeners:
            listener.close()


def _database_with_login_links(path: Path) -> tuple[str, dict[str, str]]:
    """База приложения и по настоящей ссылке на вход каждому из LINK_OWNERS."""
    url = f"sqlite:///{path}"
    storage.init_db(url)
    tokens: dict[str, str] = {}
    with storage.make_session(url)() as session:
        tenant = tenants.get_or_create_default(session)
        emails = {owner: f"{owner}@access-log-probe.example" for owner in LINK_OWNERS}
        assert emails["fault"] == FAULT_EMAIL
        for owner, email in emails.items():
            session.add(
                storage.TenantUser(
                    tenant_id=tenant.id,
                    email=email,
                    name=owner,
                    role="viewer",
                    is_active=True,
                    created_at=utcnow(),
                )
            )
        session.commit()
        for owner, email in emails.items():
            token = tenants.issue_magic_token(session, email)
            assert token, f"ссылка на вход для {email} не выпущена"
            tokens[owner] = token
    return url, tokens


@dataclass(frozen=True)
class Call:
    name: str
    target: str  # адрес, как его шлёт клиент
    status: int
    logged: str  # каким адрес обязан лечь в журнал
    method: str = "GET"  # "WebSocket" — запрос на WebSocket
    accept: str | None = None


def _calls(tokens: dict[str, str]) -> list[Call]:
    link = "/auth/verify?<hidden>"
    comparison = "/api/v1/dash/comparison"
    return [
        # Ссылку открыл браузер: вход и переход на дашборд.
        Call(
            "link_browser", f"/auth/verify?token={tokens['browser']}", 303, link, accept="text/html"
        ),
        Call("link_json", f"/auth/verify?token={tokens['json']}", 200, link),
        # Ответ 5xx: uvicorn печатает ещё и трассировку, а ссылка остаётся действующей.
        Call("link_500", f"/auth/verify?token={tokens['fault']}", 500, link),
        # До проверки токена запрос не дошёл — ссылка действует и после него.
        Call("link_head", f"/auth/verify?token={tokens['head']}", 405, link, method="HEAD"),
        Call("link_post", f"/auth/verify?token={POSTED_TOKEN}", 405, link, method="POST"),
        Call("link_refused", f"/auth/verify?token={BOGUS_TOKEN}", 401, link),
        Call("token_second", f"/auth/verify?utm=mail&token={SECOND_PLACE_TOKEN}", 401, link),
        Call("no_query", "/auth/verify", 422, "/auth/verify"),
        # Не токен: строка запроса скрыта целиком, а не по списку имён, —
        # и с именем, которого ни в каком списке нет, и вовсе без имени, и длинная.
        Call(
            "search",
            f"{comparison}?search={SEARCH_TERM}&min_spread=5",
            401,
            f"{comparison}?<hidden>",
        ),
        Call("odd_name", f"/health?zq={ODD_NAME_VALUE}", 200, "/health?<hidden>"),
        Call("bare_value", f"/auth/verify?{BARE_VALUE}", 422, link),
        Call(
            "long",
            f"{comparison}?pad={'x' * 3000}&q={LONG_QUERY_VALUE}",
            401,
            f"{comparison}?<hidden>",
        ),
        # Запрос на WebSocket uvicorn пишет другим логгером и другой строкой.
        Call("websocket", f"/auth/verify?token={WEBSOCKET_TOKEN}", 403, link, method="WebSocket"),
    ]


def _canaries(tokens: dict[str, str]) -> dict[str, str]:
    """Значения, которых в журнале быть не должно, — и их половины: обрезок тоже утечка."""
    whole = {
        **{f"{owner}_link_token": token for owner, token in tokens.items()},
        "posted_token": POSTED_TOKEN,
        "bogus_token": BOGUS_TOKEN,
        "second_place_token": SECOND_PLACE_TOKEN,
        "websocket_token": WEBSOCKET_TOKEN,
        "search_term": SEARCH_TERM,
        "odd_name_value": ODD_NAME_VALUE,
        "bare_value": BARE_VALUE,
        "long_query_value": LONG_QUERY_VALUE,
    }
    halves = {}
    for name, value in whole.items():
        halves[f"{name}_head"] = value[: len(value) // 2]
        halves[f"{name}_tail"] = value[len(value) // 2 :]
    return {**whole, **halves}


def _send(port: int, call: Call) -> int:
    if call.method == "WebSocket":
        handshake = (
            f"GET {call.target} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        return _raw_status(port, handshake.encode("ascii"))
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        connection.request(call.method, call.target, headers={"Accept": call.accept or "*/*"})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _raw_status(port: int, request: bytes) -> int:
    """Байты как есть — статус первой строки ответа."""
    with socket.create_connection(("127.0.0.1", port), timeout=60) as connection:
        connection.sendall(request)
        received = b""
        while b"\r\n" not in received:
            chunk = connection.recv(4096)
            if not chunk:
                break
            received += chunk
    return int(received.split(b" ", 2)[1])


def _wait_until_serving(port: int, process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + 120
    while True:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/health")
                connection.getresponse().read()
                return
            finally:
                connection.close()
        except OSError:
            output = log_path.read_text(encoding="utf-8", errors="replace")
            assert process.poll() is None, f"uvicorn не поднялся:\n{output[-4000:]}"
            # Рабочий процесс, упавший на импорте приложения, uvicorn поднимает
            # снова и снова: без этой строки тест ждал бы весь срок.
            assert "Traceback (most recent call last)" not in output, (
                f"приложение не загрузилось:\n{output[-4000:]}"
            )
            assert time.monotonic() < deadline, f"uvicorn не начал отвечать:\n{output[-4000:]}"
            time.sleep(0.1)


def _wait_until_every_worker_started(process: subprocess.Popen, log_path: Path) -> None:
    """Первый ответ даёт тот рабочий процесс, что поднялся раньше; ждём и остальных —
    иначе на медленной машине остановка застала бы второй на старте."""
    deadline = time.monotonic() + 120
    while True:
        output = log_path.read_text(encoding="utf-8", errors="replace")
        if output.count("Application startup complete.") >= _worker_count():
            return
        assert process.poll() is None, f"uvicorn остановился:\n{output[-4000:]}"
        assert time.monotonic() < deadline, f"не все рабочие процессы поднялись:\n{output[-4000:]}"
        time.sleep(0.1)


def _stop(process: subprocess.Popen) -> None:
    """Остановить uvicorn вместе с рабочими процессами — у них своя группа."""
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=30)


@pytest.fixture(scope="module")
def probe(tmp_path_factory) -> dict[str, dict]:
    """Что напечатал процесс API в каждом режиме. Оба сервера идут одновременно."""
    out = tmp_path_factory.mktemp("access_log_probe")
    ports = dict(zip(MODES, _free_ports(len(MODES))))
    runs: dict[str, dict] = {}
    processes: dict[str, subprocess.Popen] = {}
    try:
        for mode in MODES:
            url, tokens = _database_with_login_links(out / f"{mode}.sqlite")
            log_path = out / f"{mode}.log"
            # Журнал — в файл, как у journald: stdout и stderr вместе. Дописыванием:
            # в один файл пишут родительский процесс и оба рабочих.
            with log_path.open("a", encoding="utf-8") as log:
                processes[mode] = subprocess.Popen(
                    _launch_command(ports[mode]),
                    cwd=REPO,
                    env=_server_environment(mode, url),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            runs[mode] = {"tokens": tokens, "log_path": log_path, "calls": _calls(tokens)}
        for mode in MODES:
            _wait_until_serving(ports[mode], processes[mode], runs[mode]["log_path"])
            _wait_until_every_worker_started(processes[mode], runs[mode]["log_path"])
        for mode in MODES:
            statuses = {call.name: _send(ports[mode], call) for call in runs[mode]["calls"]}
            # Не HTTP вовсе: об этом uvicorn пишет предупреждение, без строки запроса.
            statuses["not_http"] = _raw_status(ports[mode], b"NOT-HTTP\r\n\r\n")
            runs[mode]["statuses"] = statuses
    finally:
        for process in processes.values():
            _stop(process)
    for run in runs.values():
        run["output"] = run["log_path"].read_text(encoding="utf-8", errors="replace")
    return runs


_HTTP_LINE = re.compile(r'- "(?P<method>[A-Z]+) (?P<target>\S+) HTTP/[\d.]+" (?P<status>\d{3})')
_WEBSOCKET_LINE = re.compile(r'- "WebSocket (?P<target>\S+)" (?P<status>\d{3})')


def _logged_requests(output: str) -> Counter:
    """Запросы, как их записал uvicorn: (метод, адрес, статус). Опрос готовности не в счёт."""
    logged: Counter = Counter()
    for line in output.splitlines():
        if found := _HTTP_LINE.search(line):
            if found["target"] != "/health":
                logged[(found["method"], found["target"], int(found["status"]))] += 1
        elif found := _WEBSOCKET_LINE.search(line):
            logged[("WebSocket", found["target"], int(found["status"]))] += 1
    return logged


def _expected(calls: list[Call], *, with_query: bool) -> Counter:
    return Counter(
        (call.method, call.target if with_query else call.logged, call.status) for call in calls
    )


def _other_lines(output: str) -> dict[str, int]:
    """Записи uvicorn не о запросах — сколько раз каждая встретилась."""
    return {
        phrase: output.count(phrase)
        for phrase in (
            "Started server process",
            "Application startup complete.",
            "Exception in ASGI application",
            "Invalid HTTP request received.",
        )
    }


@pytest.mark.parametrize("mode", MODES)
def test_probe_requests_got_the_answers_they_were_sent_for(probe, mode):
    """Ссылки настоящие: вход состоялся (303, 200), сбой дал 500, чужой токен — отказ."""
    run = probe[mode]
    assert run["statuses"] == {**{call.name: call.status for call in run["calls"]}, "not_http": 400}


def test_without_the_filters_every_value_is_in_the_journal(probe):
    """Контроль: так было до правки, и проба это видит — на каждом запросе."""
    run = probe["unfiltered"]

    assert _logged_requests(run["output"]) == _expected(run["calls"], with_query=True)
    missing = [
        name for name, value in _canaries(run["tokens"]).items() if value not in run["output"]
    ]
    assert not missing, f"проба не видит в журнале без фильтров: {missing}"


def test_every_request_is_logged_and_none_with_its_query(probe):
    """Строка на каждый запрос осталась — метод, путь и статус; вместо запроса метка."""
    run = probe["shipped"]

    assert _logged_requests(run["output"]) == _expected(run["calls"], with_query=False), (
        "журнал запросов не тот, что ждали. Если в юните появился --no-access-log или "
        "--log-config, строк нет вовсе — тогда и этот тест, и src/access_log.py пересмотреть."
    )
    # Фильтр ничего не потерял и не уронил форматтер uvicorn.
    assert "request_log_line_dropped" not in run["output"]
    assert "--- Logging error ---" not in run["output"]


def test_no_value_from_a_query_anywhere_in_what_the_process_printed(probe):
    run = probe["shipped"]
    output = run["output"]

    # Трассировка ответа 500 в журнале есть — значит, искали и в ней.
    assert "RuntimeError: probe: session issue failed" in output
    leaked = [name for name, value in _canaries(run["tokens"]).items() if value in output]
    assert not leaked, f"в журнале процесса: {leaked}"


def test_startup_error_and_shutdown_records_are_still_there(probe):
    """Фильтр на `uvicorn.error` стоит рядом с записями о старте, остановке и ошибках.

    Сверяются четыре записи и две об остановке, а не все: строки остановки у
    родительского процесса идут в разном порядке и числе от запуска к запуску.
    """
    workers = _worker_count()
    shipped = _other_lines(probe["shipped"]["output"])

    assert shipped == {
        "Started server process": workers,
        "Application startup complete.": workers,
        "Exception in ASGI application": 1,
        "Invalid HTTP request received.": 1,
    }
    assert shipped == _other_lines(probe["unfiltered"]["output"])
    # Остановка: рабочие процессы гасятся одним сигналом, порядок строк не важен.
    for phrase in ("Shutting down", "Finished server process"):
        assert phrase in probe["shipped"]["output"], phrase
