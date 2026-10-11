"""Событие Sentry не несёт учётных данных.

Что было. `init_sentry` не выключал сбор локальных переменных кадров, и при
ответе 5xx — необработанное исключение, штатный 503 «каталог не готов», 500
кнопки дайджеста — в событии дословно лежала кука сессии `pm_session`: в
переменных кадров Starlette и FastAPI. Заголовок `cookie` SDK при этом вырезал.
Кука — JWT на семь дней, по ней открывается дашборд. Тем же путём уходили
`Authorization`, `X-API-Key` и пароль из тела `/auth/login`. Тело запроса
уходило и без ошибки, в трассе успешного запроса: `new_password` и
`current_password` штатный скраббер SDK не знает. Токен из
`/auth/verify?token=…` уходил в строке запроса, в строке журнала доступа
uvicorn и — при сбое базы — в параметрах запроса, которые SQLAlchemy дописывает
в текст ошибки. Токен бота Telegram стоит в пути адреса исходящего запроса, а
адрес SDK пишет в событие и в трассу.

Как проверяется. `tests/sentry_probe.py` поднимает в отдельном процессе
настоящее приложение с настоящим `init_sentry` под настоящим uvicorn — как на
сервере — и вместо отправки запоминает всё, что ушло бы в сеть. Здесь в
запомненном ищутся значения, которые проба сама положила в куку, заголовки,
тело и строку запроса.

Режимы пробы — какой слой защиты оставлен:
  - `as_shipped` — как на сервере;
  - `sdk_options_only` — только аргументы `sentry_sdk.init`: держат куку, ключ
    и тело запроса, но не строку запроса и не токен в тексте;
  - `hooks_only` — только `before_send` / `before_send_transaction`, а SDK
    возвращён к худшему случаю: держат всё;
  - `before_fix` — ни того ни другого. Это контроль: проба обязана увидеть
    утечку, иначе её «чисто» в остальных режимах ничего не значит.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "tests" / "sentry_probe.py"
MODES = ("as_shipped", "sdk_options_only", "hooks_only", "before_fix")

# Что проба находит без защиты. Набор — нижняя граница: при необработанном
# исключении в кадрах лежит ещё и всё остальное.
_REQUEST_CREDENTIALS = {
    "session_jwt",
    "jwt_payload",
    "jwt_signature",
    "authorization",
    "x_api_key",
    "x_api_key_prefix",
}
LEAKED_BEFORE_FIX = {
    "unhandled_500": _REQUEST_CREDENTIALS,
    "http_503": _REQUEST_CREDENTIALS,
    "digest_500": _REQUEST_CREDENTIALS,
    "api_key_503": _REQUEST_CREDENTIALS,
    # Успешный запрос с ключом: заголовок SDK вырезает сам, утекать нечему, пока
    # счётчик запросов не пишет ключ в Redis (см. шапку пробы).
    "api_key_ok": set(),
    "login_500": _REQUEST_CREDENTIALS | {"login_password", "user_email"},
    "set_password_500": _REQUEST_CREDENTIALS | {"new_password", "magic_token"},
    "verify_500": _REQUEST_CREDENTIALS | {"magic_token"},
    # Ответ не 5xx, события об ошибке нет: значения уходят в трассе запроса.
    "change_password_refused": {"wrong_current_password", "new_password"},
    "magic_link_ok": {"live_magic_token"},
    "logged_error": _REQUEST_CREDENTIALS,
    "session_in_error_text": _REQUEST_CREDENTIALS,
    "telegram_then_error": {"telegram_bot_token", "telegram_bot_secret"},
}
SCENARIOS = tuple(LEAKED_BEFORE_FIX)

# Ответ 5xx: статус и тип исключения, которые обязаны дойти до Sentry.
ERRORS = {
    "unhandled_500": (500, "RuntimeError"),
    "http_503": (503, "HTTPException"),
    "digest_500": (500, "HTTPException"),
    "api_key_503": (503, "HTTPException"),
    "login_500": (500, "RuntimeError"),
    "set_password_500": (500, "OperationalError"),
    "verify_500": (500, "OperationalError"),
    "session_in_error_text": (500, "RuntimeError"),
}

# Чего аргументы sentry_sdk.init не закрывают, а хуки закрывают: строка запроса
# и адрес исходящего запроса (выключателя в SDK нет), параметры SQL и токен
# сессии в тексте исключения.
_ONLY_HOOKS_STOP_VALUES = {
    "magic_token",
    "live_magic_token",
    "telegram_bot_token",
    "telegram_bot_secret",
}
_ONLY_HOOKS_STOP_SCENARIOS = {"session_in_error_text"}


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    """Отчёт пробы по каждому режиму. Процессы идут одновременно."""
    out = tmp_path_factory.mktemp("sentry_probe")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))
    # Проба обязана сама убрать прокси из окружения: urllib повёл бы запрос к
    # заглушке Telegram через него, с токеном в адресе. Этот адрес никто не слушает.
    for name in ("http_proxy", "https_proxy", "all_proxy"):
        env[name] = env[name.upper()] = "http://127.0.0.1:1"
    running = {}
    reports = {}
    try:
        for mode in MODES:
            # Вывод — в файл: uvicorn печатает трассировку на каждый 500, а труба,
            # которую никто не читает, остановила бы пробу на полном буфере.
            with (out / f"{mode}.log").open("w", encoding="utf-8") as log:
                running[mode] = subprocess.Popen(
                    [sys.executable, str(PROBE), mode, str(out / f"{mode}.json")],
                    cwd=REPO,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
        for mode, process in running.items():
            process.wait(timeout=300)
            output = (out / f"{mode}.log").read_text(encoding="utf-8", errors="replace")
            assert process.returncode == 0, f"проба ({mode}) упала:\n{output[-4000:]}"
            reports[mode] = json.loads((out / f"{mode}.json").read_text(encoding="utf-8"))
    finally:
        for process in running.values():
            if process.poll() is None:
                process.kill()
    return reports


def _leaks(report: dict, scenario: str) -> dict[str, int]:
    """Сколько раз каждое значение встречается во всём, что ушло бы за сценарий."""
    found = {}
    for name, value in report["canaries"].items():
        count = sum(item["raw"].count(value) for item in report["scenarios"][scenario]["items"])
        if count:
            found[name] = count
    return found


def _payloads(report: dict, scenario: str, kind: str) -> list[dict]:
    return [i["payload"] for i in report["scenarios"][scenario]["items"] if i["type"] == kind]


def _sent(report: dict, scenario: str) -> Counter:
    """Что ушло за сценарий: события и трассы. Профиль зависит от длительности запроса."""
    kinds = [item["type"] for item in report["scenarios"][scenario]["items"]]
    return Counter(kind for kind in kinds if kind in ("event", "transaction"))


def _frames(event: dict) -> list[dict]:
    """Кадры события, где бы они ни лежали: исключения, потоки, отдельный стек."""
    stacks = [value.get("stacktrace") for value in (event.get("exception") or {}).get("values", [])]
    stacks += [value.get("stacktrace") for value in (event.get("threads") or {}).get("values", [])]
    stacks.append(event.get("stacktrace"))
    return [frame for stack in stacks if stack for frame in stack.get("frames", [])]


# ─── Контроль: без защиты проба утечку видит ─────────────────────────────────


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_probe_sees_the_leak_when_protection_is_off(probe, scenario):
    """Если этот тест упал, проба ослепла — и зелёные тесты ниже перестали что-либо значить.

    Так бывает, когда проба перестала слать значение или SDK сам перестал его
    собирать (после обновления). Во втором случае ожидание здесь правят осознанно.
    """
    leaked = set(_leaks(probe["before_fix"], scenario))
    assert LEAKED_BEFORE_FIX[scenario] <= leaked, (
        f"без защиты проба должна находить {sorted(LEAKED_BEFORE_FIX[scenario] - leaked)}"
    )


def test_before_fix_the_cookie_sat_in_frame_locals_while_the_header_was_filtered(probe):
    """Место утечки: заголовок вырезан, а те же байты лежат в переменных кадров."""
    [event] = _payloads(probe["before_fix"], "http_503", "event")
    jwt = probe["before_fix"]["canaries"]["session_jwt"]

    assert event["request"]["headers"]["cookie"] == "[Filtered]"
    assert any(jwt in json.dumps(frame.get("vars", {})) for frame in _frames(event))


# ─── Главное: в отправленном нет ни одного значения ──────────────────────────


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_nothing_sent_carries_a_credential(probe, scenario):
    """Как на сервере: событие, трасса, профиль, заголовки конвертов — всё, что ушло бы."""
    assert _sent(probe["as_shipped"], scenario)["transaction"] == 1, "проба ничего не отправила"
    assert _leaks(probe["as_shipped"], scenario) == {}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_hooks_alone_strip_credentials(probe, scenario):
    """SDK собирает всё, что умеет: переменные кадров, тело, куки, заголовки как есть.

    «Чисто» не должно значить «событие отброшено»: при сбое вырезания хук
    событие не отправляет, и пустой результат выглядел бы так же.
    """
    assert _leaks(probe["hooks_only"], scenario) == {}
    assert _sent(probe["hooks_only"], scenario) == _sent(probe["as_shipped"], scenario)


@pytest.mark.parametrize("scenario", sorted(set(SCENARIOS) - _ONLY_HOOKS_STOP_SCENARIOS))
def test_sdk_options_alone_keep_locals_and_bodies_out(probe, scenario):
    """Хуки сняты: куку, ключ и тело запроса не собирает сам SDK."""
    leaked = set(_leaks(probe["sdk_options_only"], scenario)) - _ONLY_HOOKS_STOP_VALUES
    assert leaked == set()


def test_sdk_options_alone_send_no_locals_and_no_body(probe):
    report = probe["sdk_options_only"]
    for scenario in SCENARIOS:
        for event in _payloads(report, scenario, "event"):
            assert [f for f in _frames(event) if "vars" in f] == [], scenario
        for kind in ("event", "transaction"):
            for payload in _payloads(report, scenario, kind):
                assert not payload["request"].get("data"), (scenario, kind)


# ─── Диагностика осталась ────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["as_shipped", "hooks_only"])
@pytest.mark.parametrize("scenario", ERRORS)
def test_error_event_keeps_traceback_request_and_request_id(probe, mode, scenario):
    """Ответ 5xx по-прежнему даёт событие: тип ошибки, строка кода, запрос, его номер."""
    status, exception_type = ERRORS[scenario]
    recorded = probe[mode]["scenarios"][scenario]
    assert recorded["status"] == status
    [event] = _payloads(probe[mode], scenario, "event")

    assert exception_type in [value["type"] for value in event["exception"]["values"]]
    ours = [f for f in _frames(event) if f["filename"].startswith("src/")]
    assert ours, "в трассировке нет ни одного кадра нашего кода"
    assert all(frame["lineno"] > 0 and frame["context_line"].strip() for frame in ours)
    assert event["tags"]["request_id"] == recorded["request_id"]
    assert event["tags"]["service"] == "api"
    assert event["request"]["method"] in ("GET", "POST")
    assert event["request"]["url"].startswith(probe[mode]["base_url"] + "/")
    assert event["request"]["headers"]["user-agent"] == "sentry-probe"


def test_query_without_a_credential_is_kept_and_with_one_is_replaced(probe):
    [search] = _payloads(probe["as_shipped"], "unhandled_500", "event")
    assert search["request"]["query_string"] == "search=kreon"

    [failed_link] = _payloads(probe["as_shipped"], "verify_500", "event")
    assert failed_link["request"]["query_string"] == "[Filtered]"


def test_access_log_breadcrumb_keeps_the_request_line_without_the_token(probe):
    """uvicorn пишет строку запроса в журнал доступа, а SDK кладёт её в событие целиком."""

    def access_log_lines(mode):
        [event] = _payloads(probe[mode], "verify_500", "event")
        crumbs = event["breadcrumbs"]["values"]
        return [c["message"] for c in crumbs if c.get("category") == "uvicorn.access"]

    token = probe["before_fix"]["canaries"]["magic_token"]
    [leaked] = access_log_lines("before_fix")
    assert f'"GET /auth/verify?token={token} HTTP/1.1" 500' in leaked

    for mode in ("as_shipped", "hooks_only"):
        [line] = access_log_lines(mode)
        assert '"GET /auth/verify?[Filtered] HTTP/1.1" 500' in line


def test_database_error_keeps_the_statement_without_its_parameters(probe):
    """Сбой базы на входе по ссылке: токен — параметр запроса, и он в тексте ошибки."""

    def error_texts(mode):
        [event] = _payloads(probe[mode], "verify_500", "event")
        return [value["value"] for value in event["exception"]["values"]]

    token = probe["before_fix"]["canaries"]["magic_token"]
    assert any(f"[parameters: ('{token}'" in text for text in error_texts("sdk_options_only"))

    for mode in ("as_shipped", "hooks_only"):
        [text] = [t for t in error_texts(mode) if "[parameters: " in t]
        assert text.endswith("[parameters: [Filtered]]")
        assert "no such table: tenant_users" in text
        assert "WHERE tenant_users.magic_token = ?" in text


def test_outgoing_request_keeps_its_address_without_the_bot_token(probe):
    """Токен бота Telegram — часть пути адреса; адрес SDK пишет в событие и в трассу."""
    token = probe["before_fix"]["canaries"]["telegram_bot_token"]

    def addresses(mode):
        [event] = _payloads(probe[mode], "telegram_then_error", "event")
        [trace] = _payloads(probe[mode], "telegram_then_error", "transaction")
        crumbs = [c["data"]["url"] for c in event["breadcrumbs"]["values"] if "url" in c["data"]]
        spans = [s["description"] for s in trace["spans"] if s.get("op") == "http.client"]
        return crumbs, spans

    crumbs, spans = addresses("before_fix")
    assert [url for url in crumbs if f"/bot{token}/sendMessage" in url]
    assert [text for text in spans if f"/bot{token}/sendMessage" in text]

    for mode in ("as_shipped", "hooks_only"):
        crumbs, spans = addresses(mode)
        assert [url for url in crumbs if url.endswith("/bot[Filtered]/sendMessage")]
        assert [text for text in spans if text.endswith("/bot[Filtered]/sendMessage")]


def test_successful_requests_still_send_a_trace_without_body_or_token(probe):
    """Трасса идёт и с запросов без ошибки — в ней запрос есть, тела и токена нет."""
    report = probe["as_shipped"]
    statuses = {name: report["scenarios"][name]["status"] for name in SCENARIOS}
    assert statuses["change_password_refused"] == 401
    assert statuses["magic_link_ok"] == 200
    assert statuses["api_key_ok"] == 200

    for scenario, path in (
        ("change_password_refused", "/api/v1/dash/me/password"),
        ("magic_link_ok", "/auth/verify"),
        ("api_key_ok", "/api/v1/products"),
    ):
        assert _payloads(report, scenario, "event") == []
        [trace] = _payloads(report, scenario, "transaction")
        assert trace["request"]["url"] == report["base_url"] + path
        assert "data" not in trace["request"]
    [link] = _payloads(report, "magic_link_ok", "transaction")
    assert link["request"]["query_string"] == "[Filtered]"


def test_logged_error_keeps_its_stack_without_locals(probe):
    """Ошибка из журнала приходит со стеком потока — он тоже без переменных."""
    [before] = _payloads(probe["before_fix"], "logged_error", "event")
    assert any("vars" in frame for frame in before["threads"]["values"][0]["stacktrace"]["frames"])

    for mode in ("as_shipped", "hooks_only"):
        [event] = _payloads(probe[mode], "logged_error", "event")
        frames = event["threads"]["values"][0]["stacktrace"]["frames"]
        assert "_logged_error" in [frame["function"] for frame in frames]
        assert [frame for frame in frames if "vars" in frame] == []
        assert event["logentry"]["message"] == "probe: ошибка в журнале без исключения"
        assert event["tags"]["request_id"] == probe[mode]["scenarios"]["logged_error"]["request_id"]


def test_session_token_quoted_in_error_text_is_masked_and_the_text_stays(probe):
    for mode in ("as_shipped", "hooks_only"):
        [event] = _payloads(probe[mode], "session_in_error_text", "event")
        texts = [value["value"] for value in event["exception"]["values"]]
        assert "probe: сессия [Filtered] не принята" in texts
