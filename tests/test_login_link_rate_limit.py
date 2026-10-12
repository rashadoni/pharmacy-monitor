"""Вход по ссылке из письма: число попыток с одного адреса ограничено.

До этой правки у `/auth/verify` счётчика не было — ни у GET, ни у POST, которым
входит страница из письма. Шестьдесят неверных токенов подряд получали
шестьдесят ответов 401, и каждый исполнял UPDATE в `tenants.redeem_magic_token`.
Подобрать токен в 256 бит так нельзя; это записи в базу без входа и без предела.

Теперь, как у `/auth/set-password`: десять попыток в минуту с адреса до
проверки токена (одиннадцатая до базы не доходит) и пять отказов в минуту
(шестой отказ отвечает 429, а не 401). Предел записям в базу ставит первый
счётчик; второй меняет только ответ.

Что проверяется здесь:

- неверные токены перестают доходить до базы — считаются настоящие UPDATE;
- счётчик один на GET и POST;
- отказ 429 ссылку не гасит: через минуту она входит;
- счётчик свой у каждого адреса — и адрес этот приходит из `X-Forwarded-For`,
  когда uvicorn запущен с флагами из файла юнита (отдельный процесс, с
  контрольным запуском без этих флагов);
- в имени счётчика нет ни токена, ни его хеша;
- то же на настоящем Redis, где счётчики лежат на сервере (в CI он стоит рядом
  и пропуск там — ошибка; без `REDIS_URL` тест пропускается).

Что страница входа показывает на 429 — `test_login_link_in_a_browser.py`.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from src import api as api_module
from src import rate_limit, storage, tenants
from tests.test_access_log_carries_no_query import (
    REPO,
    _free_ports,
    _stop,
    _unit_uvicorn_flags,
    _wait_until_serving,
)
from tests.test_magic_link_token import ADDRESS, _add_user, _row

# Взято при импорте — до того, как фикстура из conftest.py подменит его на тест.
_redis_client_as_shipped = rate_limit._redis_client

ATTEMPTS_A_MINUTE = 10
REFUSALS_A_MINUTE = 5
BROWSER = {"Accept": "text/html,application/xhtml+xml"}


@pytest.fixture
def app_on_the_test_database(monkeypatch, db_session):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    return api_module.app


@pytest.fixture
def client(app_on_the_test_database) -> TestClient:
    return TestClient(app_on_the_test_database)


@pytest.fixture
def writes(db_session) -> list[str]:
    """Каждый UPDATE, дошедший до базы, — текстом запроса."""
    seen: list[str] = []
    engine = db_session.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.lstrip().upper().startswith("UPDATE"):
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


@pytest.fixture
def clock(monkeypatch) -> SimpleNamespace:
    """Часы счётчика: окно в минуту проходит без ожидания."""
    state = SimpleNamespace(ahead=0.0)
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(time=lambda: time.time() + state.ahead))
    return state


def _post(client: TestClient, token: str):
    return client.post("/auth/verify", json={"token": token})


def _get(client: TestClient, token: str):
    return client.get("/auth/verify", params={"token": token})


def _get_as_a_browser(client: TestClient, token: str):
    return client.get(
        "/auth/verify", params={"token": token}, headers=BROWSER, follow_redirects=False
    )


WAYS = {"POST": _post, "GET": _get, "GET as a browser": _get_as_a_browser}
# Отказ по токену: браузеру — переход на страницу входа, остальным — 401.
REFUSED = {"POST": 401, "GET": 401, "GET as a browser": 303}
SIGNED_IN = {"POST": 200, "GET": 200, "GET as a browser": 303}


def _link(db_session, address: str = ADDRESS) -> str:
    _add_user(db_session, address)
    token = tenants.issue_magic_token(db_session, address)
    assert token
    return token


# ─── До базы доходит десять попыток в минуту ─────────────────────────────────


@pytest.mark.parametrize("way", WAYS)
def test_sixty_wrong_tokens_reach_the_database_ten_times(client, writes, way):
    statuses = [WAYS[way](client, f"wrong-{n}").status_code for n in range(60)]

    assert statuses == [REFUSED[way]] * REFUSALS_A_MINUTE + [429] * (60 - REFUSALS_A_MINUTE)
    assert len(writes) == ATTEMPTS_A_MINUTE
    assert all("tenant_users" in statement for statement in writes)


def test_without_the_counters_every_wrong_token_is_a_write(client, writes, monkeypatch):
    """Контроль: так было. Без него «десять записей» выше могло бы значить, что
    тест записей не видит."""
    monkeypatch.setattr(api_module, "_check_rate_limit", lambda key, limit=None: None)

    statuses = [_post(client, f"wrong-{n}").status_code for n in range(60)]

    assert statuses == [401] * 60
    assert len(writes) == 60


def test_both_ways_in_share_one_counter(client, writes):
    """Иначе тот, кто шлёт запросы, получил бы двадцать попыток вместо десяти."""
    ways = [_post, _get]

    statuses = [ways[n % 2](client, f"wrong-{n}").status_code for n in range(30)]

    assert statuses == [401] * REFUSALS_A_MINUTE + [429] * (30 - REFUSALS_A_MINUTE)
    assert len(writes) == ATTEMPTS_A_MINUTE


def test_the_refusal_tells_when_to_come_back_and_repeats_nothing_sent(client):
    for n in range(ATTEMPTS_A_MINUTE):
        _post(client, f"wrong-{n}")

    refused = _post(client, "the-eleventh-attempt")

    assert refused.status_code == 429
    assert 1 <= int(refused.headers["retry-after"]) <= 60
    assert "the-eleventh-attempt" not in refused.text


# ─── Отказ счётчика ссылку не гасит ──────────────────────────────────────────


@pytest.mark.parametrize("way", WAYS)
def test_a_link_refused_by_the_counter_signs_in_a_minute_later(
    client, db_session, writes, clock, way
):
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    stored = _row(db_session, user)["magic_token"]
    for n in range(ATTEMPTS_A_MINUTE):
        _post(client, f"wrong-{n}")
    writes.clear()

    refused = WAYS[way](client, token)

    assert refused.status_code == 429
    assert writes == []
    assert api_module.COOKIE_NAME not in client.cookies
    assert _row(db_session, user)["magic_token"] == stored

    clock.ahead = 61
    signed_in = WAYS[way](client, token)

    assert signed_in.status_code == SIGNED_IN[way]
    assert signed_in.cookies.get(api_module.COOKIE_NAME)
    assert _row(db_session, user)["magic_token"] is None


def test_a_refused_attempt_does_not_push_the_minute_further(client, db_session, clock):
    """Тот, кто обновляет страницу, пока счётчик полон, срок себе не продлевает."""
    token = _link(db_session)
    for n in range(ATTEMPTS_A_MINUTE):
        _post(client, f"wrong-{n}")

    for seconds in (10, 20, 30, 40, 50):
        clock.ahead = seconds
        assert _post(client, token).status_code == 429

    clock.ahead = 61
    assert _post(client, token).status_code == 200


def test_five_refusals_do_not_close_the_way_for_a_good_link(client, db_session):
    """Счётчик отказов меняет ответ на отказ, а не закрывает вход с адреса."""
    token = _link(db_session)

    refusals = [_post(client, f"wrong-{n}").status_code for n in range(REFUSALS_A_MINUTE + 1)]

    assert refusals == [401] * REFUSALS_A_MINUTE + [429]
    assert _post(client, token).status_code == 200


def test_ten_people_behind_one_address_sign_in_within_a_minute(client, db_session):
    """Вход, который удался, отказом не считается: офис за одним адресом входит."""
    tokens = [_link(db_session, f"pharmacist-{n}@example.com") for n in range(11)]

    statuses = [_post(client, token).status_code for token in tokens]

    assert statuses == [200] * ATTEMPTS_A_MINUTE + [429]
    # Одиннадцатая ссылка цела — её отказал счётчик, а не проверка токена.
    assert tenants.verify_magic_token(db_session, tokens[-1]) is not None


# ─── Счётчик свой у каждого адреса ───────────────────────────────────────────


def test_one_address_does_not_spend_the_attempts_of_another(app_on_the_test_database, db_session):
    token = _link(db_session)
    noisy = TestClient(app_on_the_test_database, client=("203.0.113.5", 50000))
    quiet = TestClient(app_on_the_test_database, client=("203.0.113.6", 50000))

    spent = [_post(noisy, f"wrong-{n}").status_code for n in range(ATTEMPTS_A_MINUTE + 1)]

    assert spent[-1] == 429
    assert _post(quiet, "wrong").status_code == 401
    assert _post(quiet, token).status_code == 200


# ─── Имя счётчика ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("way", WAYS)
def test_the_counters_are_named_after_the_address_and_nothing_sent(
    client, db_session, monkeypatch, way
):
    """Имя уходит ключом в Redis, а Sentry пишет команды Redis в трассу запроса."""
    token = _link(db_session)
    wrong = "wrong-" + "x" * 40
    named: list[tuple[str, int | None]] = []
    monkeypatch.setattr(
        api_module, "_check_rate_limit", lambda key, limit=None: named.append((key, limit))
    )

    WAYS[way](client, wrong)
    WAYS[way](client, token)

    assert named == [
        ("verify:testclient", ATTEMPTS_A_MINUTE),
        ("verify_fail:testclient", REFUSALS_A_MINUTE),
        ("verify:testclient", ATTEMPTS_A_MINUTE),
    ]
    names = " ".join(name for name, _limit in named)
    for sent in (token, wrong):
        digest = hashlib.sha256(sent.encode()).hexdigest()
        assert sent[:8] not in names and digest[:8] not in names


# ─── Счётчики не переживают тест ─────────────────────────────────────────────


def test_a_test_starts_with_empty_counters_kept_in_this_process(monkeypatch):
    """Фикстура из conftest.py: с Redis рядом (так в CI) счётчик достался бы
    следующему тесту, и вход с адреса `testclient` получил бы 429."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")

    assert not rate_limit._MEMORY_BUCKETS
    assert rate_limit._redis_client() is None
    assert rate_limit.check_rate_limit("verify:testclient", limit=10, window_sec=60) == 1
    assert list(rate_limit._MEMORY_BUCKETS) == ["verify:testclient"]


# ─── Там, где счётчики лежат на сервере: Redis ───────────────────────────────


@pytest.fixture
def counters_in_redis(monkeypatch) -> SimpleNamespace:
    """Настоящий Redis из `REDIS_URL` и адрес клиента, которого нет у других тестов."""
    assert _redis_client_as_shipped.__name__ == "_redis_client", "подмена взята вместо настоящего"
    monkeypatch.setattr(rate_limit, "_redis_client", _redis_client_as_shipped)
    rate_limit._reset_redis_probe_for_tests()
    redis = rate_limit._redis_client()
    if redis is None:
        rate_limit._reset_redis_probe_for_tests()
        if os.environ.get("CI"):
            pytest.fail("в CI рядом стоит Redis: счётчик входа обязан быть проверен на нём")
        pytest.skip("нужен Redis в REDIS_URL")
    address = f"2001:db8::{secrets.token_hex(2)}:{secrets.token_hex(2)}"
    keys = [f"ratelimit:verify:{address}", f"ratelimit:verify_fail:{address}"]
    try:
        yield SimpleNamespace(redis=redis, address=address, keys=keys)
    finally:
        redis.delete(*keys)
        rate_limit._reset_redis_probe_for_tests()


def test_in_redis_the_counters_stop_the_writes_and_hold_only_the_address(
    app_on_the_test_database, writes, counters_in_redis
):
    redis, address = counters_in_redis.redis, counters_in_redis.address
    client = TestClient(app_on_the_test_database, client=(address, 50000))

    statuses = [_post(client, f"wrong-{n}").status_code for n in range(60)]

    assert statuses == [401] * REFUSALS_A_MINUTE + [429] * (60 - REFUSALS_A_MINUTE)
    assert len(writes) == ATTEMPTS_A_MINUTE
    # Два ключа на адрес, и больше под этим адресом ничего нет.
    attempts, refusals = counters_in_redis.keys
    found = sorted(key.decode() for key in redis.scan_iter(match=f"*{address}*"))
    assert found == sorted([attempts, refusals])
    # Отклонённая попытка в счётчике не остаётся: срок она не продлевает.
    assert redis.zcard(attempts) == ATTEMPTS_A_MINUTE
    assert redis.zcard(refusals) == REFUSALS_A_MINUTE
    assert 0 < redis.ttl(attempts) <= 120 and 0 < redis.ttl(refusals) <= 120
    # В записях счётчика — время и случайный хвост, ничего из присланного.
    stored = [m.decode() for key in (attempts, refusals) for m in redis.zrange(key, 0, -1)]
    assert all(re.fullmatch(r"\d{13}-[0-9a-f]{8}", member) for member in stored), stored


# ─── Чей адрес считается за прокси: uvicorn с флагами из юнита ───────────────
#
# На сервере uvicorn слушает 127.0.0.1, запросы ему передаёт Caddy с той же
# машины. Без `--proxy-headers` адресом клиента был бы 127.0.0.1 у всех — один
# счётчик на весь интернет: десять запросов в минуту от кого угодно закрывали бы
# вход по ссылке каждому. С флагами адрес берётся из `X-Forwarded-For`, который
# ставит Caddy.

NOISY = "203.0.113.5"
QUIET = "203.0.113.6"


def _launch_command(port: int, *, as_in_the_unit: bool) -> list[str]:
    flags = _unit_uvicorn_flags()
    assert flags[flags.index("--host") + 1] == "127.0.0.1", "проба слушает только 127.0.0.1"
    flags[flags.index("--port") + 1] = str(port)
    # Один рабочий процесс: без Redis каждый считает сам, и ответ зависел бы от
    # того, которому достался запрос. На сервере счётчик общий — он в Redis.
    flags[flags.index("--workers") + 1] = "1"
    assert "--proxy-headers" in flags, "юнит больше не читает адрес клиента у прокси"
    assert flags[flags.index("--forwarded-allow-ips") + 1] == "127.0.0.1", (
        "юнит верит заголовку прокси не только от своей машины: адрес клиента "
        "смог бы назвать сам клиент"
    )
    if not as_in_the_unit:
        flags[flags.index("--proxy-headers")] = "--no-proxy-headers"
    return [sys.executable, "-m", "uvicorn", "src.api:app", *flags]


def _attempt(port: int, forwarded_for: str | None, token: str = "wrong") -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if forwarded_for is not None:
            headers["X-Forwarded-For"] = forwarded_for
        connection.request(
            "POST", "/auth/verify", body=json.dumps({"token": token}), headers=headers
        )
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


@pytest.fixture(scope="module")
def behind_a_proxy(tmp_path_factory) -> dict[str, dict[str, object]]:
    """Те же запросы серверу с флагами юнита и серверу без `--proxy-headers`."""
    folder: Path = tmp_path_factory.mktemp("login-link-rate-limit")
    answers: dict[str, dict[str, object]] = {}
    processes: list[subprocess.Popen] = []
    try:
        for mode, port in zip(("as in the unit", "no proxy headers"), _free_ports(2), strict=True):
            database = f"sqlite:///{folder / (mode.replace(' ', '-') + '.db')}"
            storage.init_db(database)
            env = {n: os.environ[n] for n in ("PATH", "HOME", "TMPDIR") if n in os.environ}
            env.update(
                PYTHONPATH=os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH")])),
                DATABASE_URL=database,
                JWT_SECRET="probe-only-secret-0123456789abcdef",
                PYTHONUTF8="1",
            )
            log_path = folder / f"{mode.replace(' ', '-')}.log"
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    _launch_command(port, as_in_the_unit=mode == "as in the unit"),
                    cwd=REPO,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            processes.append(process)
            _wait_until_serving(port, process, log_path)
            answers[mode] = {
                "noisy": [_attempt(port, NOISY, f"wrong-{n}") for n in range(11)],
                "quiet": _attempt(port, QUIET),
                # Клиент прислал свой X-Forwarded-For, прокси дописал настоящий адрес.
                "noisy naming another address": _attempt(port, f"198.51.100.77, {NOISY}"),
                "no proxy header": _attempt(port, None),
            }
    finally:
        for process in processes:
            _stop(process)
    return answers


def test_behind_the_proxy_each_address_has_its_own_counter(behind_a_proxy):
    answers = behind_a_proxy["as in the unit"]

    assert answers["noisy"] == [401] * REFUSALS_A_MINUTE + [429] * 6
    assert answers["quiet"] == 401
    # Счётчик не сменить, назвав в заголовке чужой адрес: считается тот, что
    # дописал прокси.
    assert answers["noisy naming another address"] == 429
    assert answers["no proxy header"] == 401


def test_without_proxy_headers_one_counter_would_serve_everyone(behind_a_proxy):
    """Контроль: на этом запуске видно, что проба различает адреса не случайно."""
    answers = behind_a_proxy["no proxy headers"]

    assert answers["noisy"] == [401] * REFUSALS_A_MINUTE + [429] * 6
    assert answers["quiet"] == 429
    assert answers["no proxy header"] == 429
