"""Вход отвечает отказом на любой логин — и на тот, с которым сервер не знал, что делать.

До 2026-10-11 `POST /auth/login` отвечал 500 вместо «неверный логин или пароль»:

- на логин не латиницей («админ», `ə@example.com`): логин сверялся с ADMIN_LOGIN
  через `hmac.compare_digest` на строках, а тот берёт строки только из ASCII и
  бросает TypeError. Человек, набравший логин в другой раскладке, видел «сервер
  недоступен», а запрос с его настоящим паролем уходил с ошибкой в Sentry;
- на логин с одиночным суррогатом или нулевым байтом: в JSON обе строки
  допустимы, но суррогат не кодируется в UTF-8, а нулевой байт PostgreSQL в
  тексте не хранит — исключение бросал драйвер базы, не дойдя до запроса.

Что здесь проверяется:

- отказ на такой логин — тот же ответ, что на незнакомый латинский, и считается
  той же неудачной попыткой;
- логин, который на ADMIN_LOGIN только похож, администратором не становится;
- сверка осталась без регистра и работает при ADMIN_LOGIN не латиницей;
- пользователь с адресом не латиницей входит.

Нулевой байт SQLite в запросе принимает, отвергает его только PostgreSQL —
поэтому случаи с базой идут на обеих.

Пароль, который не берёт bcrypt (суррогат, длиннее 72 байт), — отдельная правка,
`tests/test_password_fits_bcrypt.py`.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import bcrypt
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import sessionmaker

from src import api as api_module
from src import rate_limit, storage, tenants
from src.storage import Base, TenantUser

PASSWORD = "a-long-enough-password"
UNKNOWN = "nobody@example.com"


def _cheap_hash(password: str) -> str:
    """bcrypt с наименьшей стоимостью: сверка та же, а тест не ждёт по четверти секунды."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()


PASSWORD_HASH = _cheap_hash(PASSWORD)

# Логин, на котором сервер падал: не из ASCII, с суррогатом, с нулевым байтом.
ODD_LOGINS = [
    pytest.param("админ", id="cyrillic"),
    pytest.param("АДМИН", id="cyrillic-upper"),
    pytest.param("ə@example.com", id="azeri-address"),
    pytest.param("İstanbul@example.com", id="dotted-capital-i"),
    pytest.param("почта@пример.рф", id="cyrillic-address"),
    pytest.param("a\ud800@example.com", id="lone-surrogate"),
    pytest.param("\udc00", id="lone-surrogate-alone"),
    pytest.param("a\x00@example.com", id="nul-inside"),
    pytest.param("\x00", id="nul-alone"),
]
# Похож на «admin», но не он: сверка по байтам не должна ничего отбрасывать.
ALMOST_ADMIN = [
    pytest.param("admin\ud800", id="admin-and-surrogate"),
    pytest.param("admin\x00", id="admin-and-nul"),
    pytest.param("аdmin", id="cyrillic-a"),
    pytest.param("admın", id="dotless-i"),
    pytest.param("ADMİN", id="dotted-capital-i"),
]


def _postgres_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql"):
        return url
    if os.environ.get("CI"):
        pytest.fail(
            "в CI отказ входа обязан проверяться на PostgreSQL: нулевой байт в "
            "запросе отвергает только его драйвер, SQLite такой запрос исполняет."
        )
    pytest.skip("PostgreSQL DATABASE_URL is required")


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    """Своя схема с таблицами проекта — одна на файл."""
    url = _postgres_url()
    name = f"login_refusal_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={name}"})
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        admin.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request) -> Iterator[Engine]:
    if request.param == "sqlite":
        yield request.getfixturevalue("db_session").get_bind()
        return
    engine = request.getfixturevalue("postgres_engine")
    yield engine
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM tenant_users"))
        connection.execute(text("DELETE FROM tenants"))


@pytest.fixture
def sessions(monkeypatch, engine) -> sessionmaker:
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    # Сессия запроса — как в `storage.make_session`: без autoflush.
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    monkeypatch.delenv("ADMIN_LOGIN", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    # Незнакомый логин сверяется с хешем-пустышкой; настоящая стоит как боевой пароль.
    monkeypatch.setattr(api_module, "_DUMMY_BCRYPT_HASH", _cheap_hash("not-a-real-password"))
    # Счёт запросов — в памяти процесса и с нуля: в CI рядом стоит Redis, и
    # счётчик в нём переживал бы тест.
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: None)
    rate_limit._reset_memory_for_tests()
    return factory


@pytest.fixture
def client(sessions) -> TestClient:
    # Исключение обработчика — это ответ 500, а не падение теста: сверяем ответы.
    return TestClient(api_module.app, raise_server_exceptions=False)


def _add_user(sessions: sessionmaker, email: str, *, role: str = "viewer") -> None:
    with sessions() as session:
        tenant = tenants.get_or_create_default(session)
        session.add(
            TenantUser(
                tenant_id=tenant.id,
                email=email,
                role=role,
                is_active=True,
                password_hash=PASSWORD_HASH,
            )
        )
        session.commit()


def _login(client: TestClient, login: str, password: str = PASSWORD) -> Response:
    """Тело — как его шлёт браузер: одиночный суррогат уходит записью `\\udXXX`."""
    return client.post(
        "/auth/login",
        content=json.dumps({"login": login, "password": password}).encode("ascii"),
        headers={"content-type": "application/json"},
    )


def _refusals_before_the_limit(client: TestClient, login: str) -> int:
    rate_limit._reset_memory_for_tests()
    for attempt in range(20):
        if _login(client, login).status_code == 429:
            return attempt
    raise AssertionError("счётчик неудачных попыток не сработал за 20 запросов")


@pytest.mark.parametrize("login", ODD_LOGINS)
def test_an_odd_login_is_refused_like_an_unknown_one(client, sessions, login):
    # Пароль у администратора задан: отказ — про логин, а не про пустую базу.
    _add_user(sessions, "owner@example.com", role="admin")
    unknown = _login(client, UNKNOWN)
    assert unknown.status_code == 401, unknown.text

    refused = _login(client, login)

    assert (refused.status_code, refused.json()) == (401, unknown.json())
    assert api_module.COOKIE_NAME not in refused.cookies


@pytest.mark.parametrize("login", ODD_LOGINS)
def test_an_odd_login_counts_as_a_failed_attempt(client, login):
    assert _refusals_before_the_limit(client, login) == _refusals_before_the_limit(client, UNKNOWN)


@pytest.mark.parametrize("login", ["admin", "Admin", "ADMIN", "  admin  "])
def test_the_admin_login_is_matched_whatever_its_case(client, sessions, login):
    _add_user(sessions, "owner@example.com", role="admin")

    signed_in = _login(client, login)

    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["email"] == "owner@example.com"
    assert api_module.COOKIE_NAME in signed_in.cookies


@pytest.mark.parametrize("login", ALMOST_ADMIN)
def test_a_login_that_only_resembles_the_admin_login_is_not_the_admin(client, sessions, login):
    _add_user(sessions, "owner@example.com", role="admin")

    refused = _login(client, login)

    assert refused.status_code == 401, refused.text
    assert api_module.COOKIE_NAME not in refused.cookies


@pytest.mark.parametrize("login", ["админ", "Админ", "АДМИН"])
def test_an_admin_login_in_another_script_is_matched(client, sessions, monkeypatch, login):
    monkeypatch.setenv("ADMIN_LOGIN", "Админ")
    _add_user(sessions, "owner@example.com", role="admin")

    signed_in = _login(client, login)

    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["email"] == "owner@example.com"
    # Прежний логин при этом — обычный адрес, которого в базе нет.
    assert _login(client, "admin").status_code == 401


@pytest.mark.parametrize(
    ("stored", "typed"),
    [
        pytest.param("ə@example.com", "ə@example.com", id="azeri"),
        pytest.param("ə@example.com", "Ə@Example.com", id="azeri-upper"),
        pytest.param("почта@пример.рф", "Почта@Пример.РФ", id="cyrillic-upper"),
    ],
)
def test_a_user_with_an_address_in_another_script_signs_in(client, sessions, stored, typed):
    _add_user(sessions, stored)

    signed_in = _login(client, typed)

    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["email"] == stored
    assert _login(client, typed, "not-the-password").status_code == 401
