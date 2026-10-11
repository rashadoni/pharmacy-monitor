"""Что API отправило бы в Sentry: настоящее приложение под настоящим uvicorn, сети нет.

Запускается отдельным процессом из tests/test_sentry_events_carry_no_credentials.py:

    python tests/sentry_probe.py <режим> <файл отчёта>

Отдельным — потому что интеграции Sentry правят Starlette, FastAPI и SQLAlchemy
на весь процесс и навсегда, а важен порядок: на сервере `src.api` зовёт
`init_observability` при импорте, раньше, чем собирает приложение. Здесь порядок
тот же, и сервер тот же — uvicorn с журналом доступа, как в
`infra/systemd/pharmacy-monitor-api.service`: строка журнала доступа попадает в
событие «хлебной крошкой», и под TestClient этого пути нет.

Наружу ничего не уходит: DSN указывает на `.invalid`, транспорт подменён и
складывает конверты в список, сервер слушает случайный порт на 127.0.0.1.

Режимы — какой слой защиты оставлен:
    as_shipped        как на сервере;
    sdk_options_only  хуки before_send сняты, остались аргументы sentry_sdk.init;
    hooks_only        аргументы возвращены к худшему случаю (переменные кадров,
                      тело запроса, PII, штатный скраббер SDK выключен), остались хуки;
    before_fix        ни того ни другого — как было до правки. Контроль: в этом
                      режиме проба обязана увидеть утечку, иначе её «чисто» ничего
                      не значит.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace

MODE, REPORT_PATH = sys.argv[1], sys.argv[2]

os.environ["SENTRY_DSN"] = "https://0123456789abcdef0123456789abcdef@sentry.invalid/1"
# Всё, что SDK умеет собирать, собирается на каждом запросе: трассы идут в
# Sentry и с успешных запросов, и проверяются они наравне с событиями об ошибках.
os.environ["SENTRY_TRACES_SAMPLE_RATE"] = "1.0"
os.environ["SENTRY_PROFILES_SAMPLE_RATE"] = "1.0"
os.environ["SENTRY_RELEASE"] = "probe"
os.environ["PHARMACY_API_KEY"] = "erp-key-CANARY-5f1c9a77d2e04b68"
os.environ["JWT_SECRET"] = "probe-only-secret-0123456789abcdef"
for name in ("PHARMACY_AUTH_DISABLED", "REDIS_URL", "ADMIN_PASSWORD_HASH"):
    os.environ.pop(name, None)

import sentry_sdk
from sentry_sdk.scrubber import EventScrubber
from sentry_sdk.transport import Transport

ITEMS: list[dict] = []


class RecordingTransport(Transport):
    """Вместо отправки запоминает каждую часть конверта так, как она ушла бы в сеть."""

    def capture_envelope(self, envelope):
        for item in envelope.items:
            ITEMS.append(
                {
                    "type": item.headers.get("type"),
                    "raw": item.get_bytes().decode("utf-8", "replace"),
                    "payload": item.payload.json,
                }
            )

    def flush(self, timeout, callback=None):
        pass

    def kill(self):
        pass


_real_init = sentry_sdk.init
SHIPPED_OPTIONS: dict = {}


def _init_with_recording_transport(**options):
    """Аргументы — те, что передал init_sentry; меняется транспорт и то, что велит режим."""
    SHIPPED_OPTIONS.update(options)
    if MODE in ("sdk_options_only", "before_fix"):
        options.update(before_send=None, before_send_transaction=None)
    if MODE == "before_fix":
        options.update(include_local_variables=True, max_request_body_size="medium")
    if MODE == "hooks_only":
        options.update(
            send_default_pii=True,
            include_local_variables=True,
            max_request_body_size="always",
            event_scrubber=EventScrubber(denylist=[], pii_denylist=[]),
        )
    return _real_init(transport=RecordingTransport, **options)


sentry_sdk.init = _init_with_recording_transport

import uvicorn
from fastapi import Depends, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import api, notifications, product_policy, storage, tenants  # здесь и зовётся init_sentry
from src._time import utcnow

assert SHIPPED_OPTIONS, "init_sentry не дошёл до sentry_sdk.init"
assert isinstance(sentry_sdk.get_client().transport, RecordingTransport), "транспорт не подменён"

engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
storage.Base.metadata.create_all(engine)
Session = sessionmaker(engine, expire_on_commit=False)
storage.make_session = lambda database_url=None: Session

EMAIL = "probe-owner@example.com"
CURRENT_PASSWORD = "Current-CANARY-pw-91ab"
with Session() as s:
    tenant = tenants.get_or_create_default(s)
    user = storage.TenantUser(
        tenant_id=tenant.id,
        email=EMAIL,
        name="Probe",
        role="admin",
        is_active=True,
        password_hash=api._hash_bcrypt(CURRENT_PASSWORD),
        created_at=utcnow(),
    )
    s.add(user)
    s.commit()
    SESSION_JWT = api._make_jwt(user.id, user.tenant_id, user.email)
    LIVE_MAGIC_TOKEN = tenants.issue_magic_token(s, EMAIL)

_jwt_header, JWT_PAYLOAD, JWT_SIGNATURE = SESSION_JWT.split(".")

# Значения, которых в отправленном быть не должно. Подпись и полезная часть JWT
# ищутся и по отдельности: обрезанный токен без подписи всё ещё называет человека.
CANARIES = {
    "session_jwt": SESSION_JWT,
    "jwt_payload": JWT_PAYLOAD,
    "jwt_signature": JWT_SIGNATURE,
    "jwt_email_claim": EMAIL,
    "authorization": "authz-CANARY-7d1e03b9c4a25f60",
    "x_api_key": os.environ["PHARMACY_API_KEY"],
    "login_password": "Login-CANARY-pw-3c7e",
    "current_password": CURRENT_PASSWORD,
    "wrong_current_password": "Wrong-CANARY-pw-58d0",
    "new_password": "New-CANARY-pw-44cd",
    "magic_token": "magic-CANARY-token-e2f0a6b1",
    "live_magic_token": LIVE_MAGIC_TOKEN,
}

# ─── Сбои по требованию ──────────────────────────────────────────────────────
# Один процесс проходит все сценарии, поэтому сбой включается на один запрос.

FAULT: dict[str, str | None] = {"policy": None, "bcrypt": None, "magic": None, "digest": None}


def _policy_rollout_eligibility(db, *, tenant_id=1):
    if FAULT["policy"] == "crash":
        raise RuntimeError("probe: сбой внутри эндпоинта")
    return SimpleNamespace(eligible=FAULT["policy"] != "not_ready", reason="catalog_not_ready")


def _failing_when(name, real):
    def wrapper(*args, **kwargs):
        if FAULT[name]:
            raise RuntimeError(f"probe: сбой {name}")
        return real(*args, **kwargs)

    return wrapper


product_policy.policy_rollout_eligibility = _policy_rollout_eligibility
api._verify_bcrypt = _failing_when("bcrypt", api._verify_bcrypt)
tenants.verify_magic_token = _failing_when("magic", tenants.verify_magic_token)
notifications.send_daily_digest = _failing_when("digest", notifications.send_daily_digest)


@api.app.get("/__probe/logged-error")
def _logged_error(request: Request, user=Depends(api.require_user)):
    """Ошибка в журнал без исключения: событие приходит со стеком потока (`threads`)."""
    logging.getLogger("probe").error("probe: ошибка в журнале без исключения")
    return {"ok": True}


@api.app.get("/__probe/session-in-error-text")
def _session_in_error_text(request: Request, user=Depends(api.require_user)):
    """Токен сессии процитирован в тексте исключения — мимо заголовков и переменных."""
    raise RuntimeError(f"probe: сессия {request.cookies[api.COOKIE_NAME]} не принята")


# ─── Сервер и клиент ─────────────────────────────────────────────────────────

listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
PORT = listener.getsockname()[1]
# Журнал доступа включён, как на сервере; lifespan выключен — прогрев индекса
# поиска пошёл бы в ту же единственную SQLite-сессию из другого потока.
server = uvicorn.Server(uvicorn.Config(api.app, log_level="info", access_log=True, lifespan="off"))
threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True).start()
_deadline = time.monotonic() + 30
while not server.started:
    assert time.monotonic() < _deadline, "uvicorn не поднялся"
    time.sleep(0.02)


def call(method: str, path: str, *, request_id: str, json_body: dict | None = None) -> int:
    """Один запрос со всеми учётными данными. Соединение своё: после 500 uvicorn его рвёт."""
    headers = {
        "Cookie": f"{api.COOKIE_NAME}={SESSION_JWT}",
        "Authorization": f"Bearer {CANARIES['authorization']}",
        "X-API-Key": CANARIES["x_api_key"],
        # Ответ на необработанное исключение собирает Starlette снаружи наших
        # middleware, заголовка X-Request-ID в нём нет — номер задаём сами.
        "X-Request-ID": request_id,
        "User-Agent": "sentry-probe",
    }
    body = None
    if json_body is not None:
        body = json.dumps(json_body)
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _comparison(**kw):
    return call("GET", "/api/v1/dash/comparison?search=kreon", **kw)


def _unhandled_500(**kw):
    FAULT["policy"] = "crash"
    return _comparison(**kw)


def _http_503(**kw):
    FAULT["policy"] = "not_ready"
    return _comparison(**kw)


def _digest_500(**kw):
    FAULT["digest"] = "on"
    return call("POST", "/api/v1/dash/digest/send-test", **kw)


def _api_key_503(**kw):
    configured = os.environ.pop("PHARMACY_API_KEY")
    try:
        return call("GET", "/api/v1/products", **kw)
    finally:
        os.environ["PHARMACY_API_KEY"] = configured


def _login_500(**kw):
    FAULT["bcrypt"] = "on"
    return call(
        "POST",
        "/auth/login",
        json_body={"login": EMAIL, "password": CANARIES["login_password"]},
        **kw,
    )


def _set_password_500(**kw):
    FAULT["magic"] = "on"
    return call(
        "POST",
        "/auth/set-password",
        json_body={"token": CANARIES["magic_token"], "new_password": CANARIES["new_password"]},
        **kw,
    )


def _verify_500(**kw):
    FAULT["magic"] = "on"
    return call("GET", f"/auth/verify?token={CANARIES['magic_token']}", **kw)


def _change_password_refused(**kw):
    return call(
        "POST",
        "/api/v1/dash/me/password",
        json_body={
            "current_password": CANARIES["wrong_current_password"],
            "new_password": CANARIES["new_password"],
        },
        **kw,
    )


def _magic_link_ok(**kw):
    return call("GET", f"/auth/verify?token={LIVE_MAGIC_TOKEN}", **kw)


def _logged_error_200(**kw):
    return call("GET", "/__probe/logged-error", **kw)


def _session_in_error_text_500(**kw):
    return call("GET", "/__probe/session-in-error-text", **kw)


SCENARIOS = {
    "unhandled_500": _unhandled_500,
    "http_503": _http_503,
    "digest_500": _digest_500,
    "api_key_503": _api_key_503,
    "login_500": _login_500,
    "set_password_500": _set_password_500,
    "verify_500": _verify_500,
    "change_password_refused": _change_password_refused,
    "magic_link_ok": _magic_link_ok,
    "logged_error": _logged_error_200,
    "session_in_error_text": _session_in_error_text_500,
}


def _wait_for_trace(first: int) -> None:
    """Трасса закрывается после ответа; событие об ошибке к этому времени уже записано."""
    deadline = time.monotonic() + 30
    while not any(item["type"] == "transaction" for item in ITEMS[first:]):
        assert time.monotonic() < deadline, "трасса запроса не пришла"
        time.sleep(0.02)


report: dict = {
    "mode": MODE,
    "sentry_sdk": sentry_sdk.VERSION,
    "base_url": f"http://127.0.0.1:{PORT}",
    "canaries": CANARIES,
    "scenarios": {},
}
for name, run in SCENARIOS.items():
    for key in FAULT:
        FAULT[key] = None
    request_id = f"probe-{name}"
    first = len(ITEMS)
    status = run(request_id=request_id)
    _wait_for_trace(first)
    sentry_sdk.flush(timeout=2)
    report["scenarios"][name] = {
        "status": status,
        "request_id": request_id,
        "items": ITEMS[first:],
    }

server.should_exit = True
with open(REPORT_PATH, "w", encoding="utf-8") as fh:
    json.dump(report, fh, ensure_ascii=False, default=str)
