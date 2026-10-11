"""Одноразовая ссылка одноразова и для одновременных запросов — на PostgreSQL.

До 2026-10-10 токен гасился в два шага: SELECT по хешу, потом UPDATE по
первичному ключу — без замка строки и без условия в UPDATE. Два запроса с одним
токеном, прочитавшие строку до записи друг друга, проходили оба: два входа по
одной ссылке, а у `/auth/set-password` — два пароля, из которых оставался
последний.

Теперь гашение — один UPDATE с условием. Здесь это проверяется настоящими
транзакциями: первый запрос останавливается сразу после своего первого запроса
к базе с хешем токена (у прежнего кода это SELECT, у нынешнего — сам UPDATE),
второй в это время идёт своим ходом.

- из двух одновременных входов по одной ссылке проходит один;
- из двух одновременных `/auth/set-password` пароль задаёт один, второй
  получает отказ;
- если первый после проверки токена сорвался, ссылка достаётся тому, кто ждал.

SQLite этого не покажет: там один писатель. Без PostgreSQL тесты пропускаются,
в CI пропуск — ошибка.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from src import api as api_module
from src import rate_limit, storage, tenants
from src._time import utcnow
from src.storage import Base, TenantUser

ADDRESS = "pharmacist@example.com"
FIRST_PASSWORD = "first-password-1"
SECOND_PASSWORD = "second-password-2"
# Имя приложения у соединений теста — своё на каждый запуск pytest: по нему
# видно, кто из них ждёт замка, и чужие прогоны на том же сервере не мешают.
_APPLICATION = f"magic_link_redeem_{uuid.uuid4().hex[:8]}"
# Сколько тест ждёт своего же шага. Замок база ждёт втрое дольше: на медленной
# машине тест упадёт своим «не дошёл» или «завис», а не чужим отказом по замку.
_PATIENCE_SECONDS = 10
_LOCK_TIMEOUT_SECONDS = 3 * _PATIENCE_SECONDS


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql"):
        return url
    if os.environ.get("CI"):
        pytest.fail(
            "в CI тесты гашения ссылки обязаны исполняться, а DATABASE_URL — не "
            "PostgreSQL: на SQLite один писатель, и гонку она не покажет."
        )
    pytest.skip("PostgreSQL DATABASE_URL is required")


@pytest.fixture(scope="module")
def schema() -> Iterator[tuple[str, str]]:
    """Своя схема с таблицами проекта — одна на файл."""
    url = _database_url()
    name = f"magic_link_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{name}"'))
    # lock_timeout: ожидание замка, которого быть не должно, роняет тест, а не вешает.
    options = f"-csearch_path={name} -clock_timeout={_LOCK_TIMEOUT_SECONDS * 1000}"
    builder = create_engine(url, connect_args={"options": options})
    Base.metadata.create_all(builder)
    builder.dispose()
    try:
        yield url, options
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        admin.dispose()


@pytest.fixture
def engines(schema) -> Iterator[tuple[Engine, Engine]]:
    """Движок запросов и движок постороннего наблюдателя."""
    url, options = schema
    engine = create_engine(
        url, connect_args={"options": f"{options} -capplication_name={_APPLICATION}"}
    )
    outsider = create_engine(url, connect_args={"options": options})
    try:
        yield engine, outsider
    finally:
        engine.dispose()
        with outsider.begin() as connection:
            connection.execute(text("TRUNCATE tenant_users, tenants CASCADE"))
        outsider.dispose()


def _session(bind: Engine) -> Session:
    """Сессия как в `storage.make_session`."""
    return sessionmaker(bind, expire_on_commit=False, autoflush=False)()


def _invite(engine: Engine, **fields) -> tuple[int, str]:
    """Пользователь с действующей ссылкой: его id и токен."""
    with _session(engine) as session:
        tenant = tenants.get_or_create_default(session)
        user = TenantUser(tenant_id=tenant.id, email=ADDRESS, is_active=True, **fields)
        session.add(user)
        session.commit()
        token = tenants.issue_magic_token(session, ADDRESS)
        assert token
        return user.id, token


def _row(outsider: Engine, user_id: int) -> dict:
    with outsider.connect() as connection:
        return dict(
            connection.execute(text("SELECT * FROM tenant_users WHERE id = :id"), {"id": user_id})
            .mappings()
            .one()
        )


class _FirstToTouchTheToken:
    """Останавливает первое соединение, отправившее запрос с хешем токена.

    Останавливает после запроса и до следующего: у кода «прочитал, потом
    записал» — между чтением и записью, у условного UPDATE — когда строка уже
    заперта, а транзакция не завершена.

    Расчёт на то, что первый же запрос с хешем токена строку и запирает. Код,
    который сначала читает её без замка, а запирает вторым запросом, здесь
    упадёт на «второй не ждал», хотя и верен, — тогда останавливать надо после
    запирающего запроса.
    """

    def __init__(self, engine: Engine, token: str) -> None:
        self._engine = engine
        self._token_hash = tenants._magic_token_hash(token)
        self._claim = threading.Lock()
        self._claimed = False
        self.stopped = threading.Event()
        self.go_on = threading.Event()

    def __enter__(self) -> _FirstToTouchTheToken:
        event.listen(self._engine, "after_cursor_execute", self._after)
        return self

    def __exit__(self, *exc) -> None:
        self.go_on.set()
        event.remove(self._engine, "after_cursor_execute", self._after)

    def _after(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if self._token_hash not in repr(parameters):
            return
        with self._claim:
            if self._claimed:
                return
            self._claimed = True
        self.stopped.set()
        assert self.go_on.wait(_PATIENCE_SECONDS), "первый запрос так и не отпустили"


def _waits_for_a_lock(outsider: Engine) -> bool:
    with outsider.connect() as connection:
        return bool(
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND application_name = :application AND wait_event_type = 'Lock'"
                ),
                {"application": _APPLICATION},
            )
        )


def _collide(engine: Engine, outsider: Engine, token: str, first: Callable, second: Callable):
    """Столкнуть два запроса с одним токеном.

    Первый доходит до своего первого запроса с хешем токена и встаёт. Второй
    идёт, пока не закончит или не упрётся в замок первого; после этого первого
    отпускают. Возвращает оба результата и то, ждал ли второй первого.
    """
    results: dict[str, object] = {}

    def run(name: str, request: Callable) -> None:
        try:
            results[name] = request()
        except BaseException as error:  # noqa: BLE001 — результат потока, а не обработка
            results[name] = error

    with _FirstToTouchTheToken(engine, token) as gate:
        earlier = threading.Thread(target=run, args=("first", first), daemon=True)
        earlier.start()
        assert gate.stopped.wait(_PATIENCE_SECONDS), "первый запрос не дошёл до токена"
        later = threading.Thread(target=run, args=("second", second), daemon=True)
        later.start()
        second_waited = False
        deadline = time.monotonic() + _PATIENCE_SECONDS
        while later.is_alive() and time.monotonic() < deadline:
            if _waits_for_a_lock(outsider):
                second_waited = True
                break
            time.sleep(0.02)
        gate.go_on.set()
        earlier.join(_LOCK_TIMEOUT_SECONDS + _PATIENCE_SECONDS)
        later.join(_LOCK_TIMEOUT_SECONDS + _PATIENCE_SECONDS)
    assert not earlier.is_alive() and not later.is_alive(), "запрос завис"
    return results["first"], results["second"], second_waited


@pytest.fixture
def api(monkeypatch, engines) -> Engine:
    """API, чьи запросы идут в схему теста; возвращает её движок."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    engine, _ = engines
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    # Счёт запросов — в памяти процесса и с нуля: в CI рядом стоит Redis, и
    # счётчик в нём переживал бы тест.
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: None)
    rate_limit._reset_memory_for_tests()
    return engine


def _set_password(token: str, password: str) -> Callable[[], int]:
    def request() -> int:
        client = TestClient(api_module.app, raise_server_exceptions=False)
        try:
            return client.post(
                "/auth/set-password", json={"token": token, "new_password": password}
            ).status_code
        finally:
            client.close()

    return request


# ─── Условие гашения — на настоящем диалекте ─────────────────────────────────


def test_the_token_signs_in_once(engines):
    engine, outsider = engines
    user_id, token = _invite(engine)

    with _session(engine) as session:
        assert tenants.verify_magic_token(session, token).id == user_id
    with _session(engine) as session:
        assert tenants.verify_magic_token(session, token) is None
    row = _row(outsider, user_id)
    assert row["magic_token"] is None and row["magic_token_expires_at"] is None
    assert row["last_login_at"] is not None


@pytest.mark.parametrize(
    "spoiled",
    [
        pytest.param({"magic_token_expires_at": utcnow() - timedelta(minutes=1)}, id="expired"),
        pytest.param({"magic_token_expires_at": None}, id="no-term"),
        pytest.param({"is_active": False}, id="switched-off"),
    ],
)
def test_a_token_that_is_not_good_is_refused_and_the_row_stays(engines, spoiled):
    engine, outsider = engines
    user_id, token = _invite(engine)
    with _session(engine) as session:
        user = session.get(TenantUser, user_id)
        for field, value in spoiled.items():
            setattr(user, field, value)
        session.commit()
    before = _row(outsider, user_id)

    with _session(engine) as session:
        assert tenants.verify_magic_token(session, token) is None
        session.commit()

    assert _row(outsider, user_id) == before


@pytest.mark.parametrize(("seconds_past_the_term", "signs_in"), [(0, True), (1, False)])
def test_the_term_ends_to_the_second(engines, monkeypatch, seconds_past_the_term, signs_in):
    """Срок сверяет база, и хранит она его с той же точностью, что и Python."""
    engine, _ = engines
    issued_at = utcnow()
    monkeypatch.setattr(tenants, "utcnow", lambda: issued_at)
    _, token = _invite(engine)

    now = issued_at + timedelta(minutes=30, seconds=seconds_past_the_term)
    monkeypatch.setattr(tenants, "utcnow", lambda: now)

    with _session(engine) as session:
        assert (tenants.verify_magic_token(session, token) is not None) is signs_in


def test_redeeming_is_not_final_until_the_caller_commits(engines):
    """Пока вызывающий не записал, посторонний видит ссылку целой, а откат её возвращает."""
    engine, outsider = engines
    user_id, token = _invite(engine)
    before = _row(outsider, user_id)

    with _session(engine) as session:
        assert tenants.redeem_magic_token(session, token).id == user_id
        assert _row(outsider, user_id) == before
        session.rollback()

    assert _row(outsider, user_id) == before
    with _session(engine) as session:
        assert tenants.verify_magic_token(session, token).id == user_id


# ─── Два одновременных запроса с одним токеном ───────────────────────────────


def test_of_two_simultaneous_sign_ins_by_one_link_one_passes(engines):
    engine, outsider = engines
    user_id, token = _invite(engine)

    def sign_in() -> int | None:
        with _session(engine) as session:
            user = tenants.verify_magic_token(session, token)
            return user.id if user else None

    first, second, second_waited = _collide(engine, outsider, token, sign_in, sign_in)

    assert (first, second) == (user_id, None)
    # Второй получил отказ не потому, что пришёл позже: он ждал транзакцию
    # первого и условия после неё не нашёл.
    assert second_waited


def test_of_two_simultaneous_passwords_by_one_invitation_one_is_set(api, engines):
    engine, outsider = engines
    user_id, token = _invite(engine)

    first, second, second_waited = _collide(
        engine,
        outsider,
        token,
        _set_password(token, FIRST_PASSWORD),
        _set_password(token, SECOND_PASSWORD),
    )

    assert (first, second) == (200, 401)
    assert second_waited
    stored = _row(outsider, user_id)["password_hash"]
    assert api_module._verify_bcrypt(FIRST_PASSWORD, stored)
    assert not api_module._verify_bcrypt(SECOND_PASSWORD, stored)


def test_a_request_that_fails_after_the_check_leaves_the_invitation_to_the_one_waiting(
    api, engines, monkeypatch
):
    """Откат первого возвращает ссылку: второй, ждавший его, задаёт пароль."""
    engine, outsider = engines
    user_id, token = _invite(engine)
    hash_password = api_module._hash_bcrypt

    def fails_for_the_first(password: str) -> str:
        if password == FIRST_PASSWORD:
            raise RuntimeError("сломано нарочно")
        return hash_password(password)

    monkeypatch.setattr(api_module, "_hash_bcrypt", fails_for_the_first)

    first, second, second_waited = _collide(
        engine,
        outsider,
        token,
        _set_password(token, FIRST_PASSWORD),
        _set_password(token, SECOND_PASSWORD),
    )

    assert (first, second) == (500, 200)
    assert second_waited
    row = _row(outsider, user_id)
    assert api_module._verify_bcrypt(SECOND_PASSWORD, row["password_hash"])
    assert row["magic_token"] is None
