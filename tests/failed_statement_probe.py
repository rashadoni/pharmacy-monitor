"""Что уходит в Sentry и в журнал, когда запрос к базе на эндпоинте входа падает.

Запускается отдельным процессом из tests/test_statement_data_stays_out_of_errors.py:

    python tests/failed_statement_probe.py <режим> <файл отчёта>

Приложение настоящее, под настоящим uvicorn — как в
`infra/systemd/pharmacy-monitor-api.service`; база — PostgreSQL из
`DATABASE_URL`, в своей схеме; сбои запросов настоящие: чужой сеанс держит замок
на таблице пользователей, а у приложения `lock_timeout`, или запись нарушает
ограничение таблицы. Отдельным процессом — потому что интеграции Sentry правят
Starlette и SQLAlchemy на весь процесс, а stdout и stderr этого процесса и есть
журнал: на сервере их забирает journald.

Наружу ничего не уходит: DSN указывает на `.invalid`, транспорт подменён и
складывает части конвертов в список.

Проверяется один слой — движок: что стоит в тексте ошибки, когда он доходит
до Sentry. Остальное закрыто или снято аргументами `sentry_sdk.init` прямо
здесь, что бы ни передал `init_sentry`:

- переменные кадров (в кадрах SQLAlchemy лежит переменная `parameters`) и тело
  запроса (в нём пароль и токен) не собираются — иначе проба не отличила бы
  данные в тексте ошибки от тех же значений, пришедших соседним путём;
- хуки `before_send` сняты. С PR #84 они сами вырезают из события
  `[parameters: …]` и строки DETAIL / CONTEXT; с ними контрольный режим не
  увидел бы утечки, а чистый конверт ничего не говорил бы о движке. Хуки
  проверяет свой тест — `tests/test_sentry_events_carry_no_credentials.py`;
  журнал, `runs.error_message` и трассировку CLI они не закрывают, это делает
  только движок.

Режимы:
    guarded     движок создаёт `storage.create_engine_hiding_statement_data`,
                хуки Sentry сняты: чисто — значит, чист текст самой ошибки;
    unguarded   движок создаёт голый `create_engine`, как до правки, хуки сняты.
                Контроль: в этом режиме проба обязана увидеть данные запроса и в
                конверте, и в журнале — иначе её «чисто» ничего не значит;
    as_shipped  как на сервере: защищённый движок и хуки, какими их передал
                `init_sentry`. Два слоя вместе: хуки не должны ни вернуть данные,
                ни выбросить событие об ошибке.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import sys
import threading
import time
import uuid

MODE, REPORT_PATH = sys.argv[1], sys.argv[2]
assert MODE in ("guarded", "unguarded", "as_shipped"), MODE

BASE_URL = os.environ["DATABASE_URL"]
SCHEMA = f"failed_statement_{uuid.uuid4().hex[:12]}"
SEARCH_PATH = f"-csearch_path={SCHEMA}"

os.environ["SENTRY_DSN"] = "https://0123456789abcdef0123456789abcdef@sentry.invalid/1"
os.environ["SENTRY_TRACES_SAMPLE_RATE"] = "1.0"
os.environ["SENTRY_PROFILES_SAMPLE_RATE"] = "0"
os.environ["SENTRY_RELEASE"] = "probe"
os.environ["JWT_SECRET"] = "probe-only-secret-0123456789abcdef"
for name in ("PHARMACY_AUTH_DISABLED", "REDIS_URL", "ADMIN_PASSWORD_HASH"):
    os.environ.pop(name, None)

import sentry_sdk
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
                }
            )

    def flush(self, timeout, callback=None):
        pass

    def kill(self):
        pass


_real_init = sentry_sdk.init


def _init_with_recording_transport(**options):
    options.update(include_local_variables=False, max_request_body_size="never")
    if MODE != "as_shipped":
        options.update(before_send=None, before_send_transaction=None)
    return _real_init(transport=RecordingTransport, **options)


sentry_sdk.init = _init_with_recording_transport

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from src import storage

if MODE == "unguarded":
    storage.create_engine_hiding_statement_data = sqlalchemy.create_engine

# Приложению — короткий lock_timeout: ожидание чужого замка кончается настоящей
# ошибкой PostgreSQL, а не зависшим тестом.
os.environ["DATABASE_URL"] = (
    make_url(BASE_URL)
    .update_query_dict({"options": f"{SEARCH_PATH} -clock_timeout=300"})
    .render_as_string(hide_password=False)
)

import uvicorn

from src import api, rate_limit, tenants  # здесь `src.api` и зовёт init_sentry

assert isinstance(sentry_sdk.get_client().transport, RecordingTransport), "транспорт не подменён"
rate_limit._redis_client = lambda: None
rate_limit._reset_memory_for_tests()

ADDRESS = "canary.owner@client-pharmacy.example"
INVITED_ADDRESS = "canary.invited@client-pharmacy.example"
PASSWORD = "Canary-Passw0rd-7731"
NEW_PASSWORD = "Canary-New-Passw0rd-9912"

# Чужой сеанс: строит схему, держит замки, убирает за собой.
outsider = sqlalchemy.create_engine(BASE_URL, connect_args={"options": SEARCH_PATH})
with outsider.begin() as connection:
    connection.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))


def _seed() -> dict[str, str]:
    storage.Base.metadata.create_all(outsider)
    Session = sessionmaker(outsider, expire_on_commit=False, autoflush=False)
    with Session() as session:
        tenant = tenants.get_or_create_default(session)
        session.add_all(
            [
                storage.TenantUser(
                    tenant_id=tenant.id,
                    email=ADDRESS,
                    role="admin",
                    password_hash=api._hash_bcrypt(PASSWORD),
                ),
                storage.TenantUser(tenant_id=tenant.id, email=INVITED_ADDRESS, role="viewer"),
            ]
        )
        session.commit()
        # Токены выдаёт само приложение: что именно оно кладёт в базу и чем
        # потом ищет — токен или его хеш, — проба не предполагает.
        return {
            "lock": tenants.issue_magic_token(session, ADDRESS),
            "constraint": tenants.issue_magic_token(session, INVITED_ADDRESS),
        }


def _call(method: str, path: str, body: dict) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    connection.request(
        method, path, body=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def _while_users_are_locked(request: tuple) -> int:
    with outsider.connect() as holder:
        transaction = holder.begin()
        holder.execute(text("LOCK TABLE tenant_users IN ACCESS EXCLUSIVE MODE"))
        try:
            return _call(*request)
        finally:
            transaction.rollback()


CASES: dict[str, dict] = {}


def _case(name: str, status: int, secrets: dict[str, str]) -> None:
    """Запомнить, какие части конвертов породил этот запрос.

    Событие об ошибке SDK собирает, пока uvicorn отвечает, — ждём его, а не
    фиксированную паузу. Трасса запроса может прийти и позже: проверка «секрета
    нет» поэтому идёт по всему отправленному за прогон (`sent` в отчёте).
    """
    seen = sum(len(case["items"]) for case in CASES.values())
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        sentry_sdk.flush()
        if any(item["type"] == "event" for item in ITEMS[seen:]):
            break
        time.sleep(0.05)
    CASES[name] = {"status": status, "secrets": secrets, "items": ITEMS[seen:]}


try:
    tokens = _seed()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    PORT = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api.app, log_level="info", access_log=True))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn не поднялся"

    # 1. Запрос по токену ждёт чужого замка: параметр запроса — токен (или его хеш).
    token = tokens["lock"]
    status = _while_users_are_locked(
        ("POST", "/auth/set-password", {"token": token, "new_password": NEW_PASSWORD})
    )
    _case(
        "token",
        status,
        {
            "token": token,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        },
    )

    # 2. Запрос по адресу ждёт чужого замка: параметр запроса — адрес.
    status = _while_users_are_locked(
        ("POST", "/auth/login", {"login": ADDRESS, "password": PASSWORD})
    )
    _case("address", status, {"address": ADDRESS})

    # 3. Запись хеша пароля нарушает ограничение таблицы: хеш — параметр запроса,
    #    а строку пользователя целиком (адрес, хеш) приводит сам PostgreSQL в DETAIL.
    with outsider.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE tenant_users ADD CONSTRAINT probe_invited_has_no_password "
                f"CHECK (email <> '{INVITED_ADDRESS}' OR password_hash IS NULL)"
            )
        )
    status = _call(
        "POST",
        "/auth/set-password",
        {"token": tokens["constraint"], "new_password": NEW_PASSWORD},
    )
    _case("row", status, {"address": INVITED_ADDRESS})

    server.should_exit = True
    thread.join(timeout=15)
finally:
    with outsider.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{SCHEMA}" CASCADE'))
    outsider.dispose()

sentry_sdk.flush()
with open(REPORT_PATH, "w", encoding="utf-8") as report:
    # Стояли ли хуки на самом деле — спрашиваем у клиента SDK, а не у режима.
    hooks = sentry_sdk.get_client().options.get("before_send") is not None
    json.dump(
        {"mode": MODE, "hooks": hooks, "cases": CASES, "sent": ITEMS}, report, ensure_ascii=False
    )
