"""Одноразовый токен входа лежит в базе хешем, а сам существует только в письме.

Токен из ссылки — полноценный вход: `/auth/verify` ставит по нему сессию,
`/auth/set-password` задаёт пароль. До 2026-10-10 он лежал в
`tenant_users.magic_token` открытым текстом, и вошёл бы любой, кто прочитал базу
или дамп, снятый, пока токен жив.

Что здесь проверяется:

- в строке пользователя и в запросах к базе самого токена нет;
- токен из письма входит один раз, после срока и без срока — не входит;
- содержимое колонки токеном не служит — ни хеш, ни токен, записанный открытым
  текстом прежним кодом;
- так на каждом пути выдачи: запрос ссылки, приглашение, повторная отправка
  админом, команда `tenant issue-token`.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker

from src import api as api_module
from src import main, notifier, rate_limit, storage, tenants
from src._time import utcnow
from src.storage import TenantUser

ADDRESS = "pharmacist@example.com"
PASSWORD = "a-long-enough-password"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _add_user(session: Session, address: str = ADDRESS, **fields) -> TenantUser:
    tenant = tenants.get_or_create_default(session)
    user = TenantUser(tenant_id=tenant.id, email=address, is_active=True, **fields)
    session.add(user)
    session.commit()
    return user


def _row(session: Session, user: TenantUser) -> dict:
    """Строка пользователя, как она лежит в базе, — мимо модели."""
    return dict(
        session.execute(text("SELECT * FROM tenant_users WHERE id = :id"), {"id": user.id})
        .mappings()
        .one()
    )


def _assert_only_the_hash_is_stored(session: Session, user: TenantUser, token: str) -> None:
    row = _row(session, user)
    assert row["magic_token"] == _sha256(token)
    assert token not in repr(row)


# ─── Выдача и проверка ───────────────────────────────────────────────────────


def test_the_row_holds_the_hash_of_the_token_not_the_token(db_session):
    user = _add_user(db_session)

    token = tenants.issue_magic_token(db_session, ADDRESS)

    assert token
    _assert_only_the_hash_is_stored(db_session, user, token)


def test_the_token_is_in_no_query_sent_to_the_database(db_session):
    """Текст ошибки базы несёт параметры запроса — токена среди них быть не должно."""
    _add_user(db_session)
    sent: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        sent.append(f"{statement} {parameters!r}")

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        token = tenants.issue_magic_token(db_session, ADDRESS)
        assert tenants.verify_magic_token(db_session, token) is not None
        assert tenants.verify_magic_token(db_session, token) is None
    finally:
        event.remove(engine, "before_cursor_execute", record)

    # Запись и оба поиска на месте — иначе проверка ниже ничего бы не значила.
    assert sum(_sha256(token) in query for query in sent) >= 3, sent
    assert not [query for query in sent if token in query]


def test_the_token_signs_in_once(db_session):
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)

    assert tenants.verify_magic_token(db_session, token).id == user.id
    assert tenants.verify_magic_token(db_session, token) is None
    assert _row(db_session, user)["magic_token"] is None


def test_the_token_does_not_sign_in_after_its_term(db_session):
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    user.magic_token_expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()

    assert tenants.verify_magic_token(db_session, token) is None


def test_a_token_without_a_term_does_not_sign_in(db_session):
    """Срок ставит выдача; строка без срока — ручная правка базы, а не вечный вход."""
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    user.magic_token_expires_at = None
    db_session.commit()

    assert tenants.verify_magic_token(db_session, token) is None


def test_what_the_column_holds_does_not_sign_in(db_session):
    """Кто прочитал базу, получил хеш. Хеш — не вход."""
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    stored = _row(db_session, user)["magic_token"]

    assert tenants.verify_magic_token(db_session, stored) is None
    # Отказ — из-за того, что прислали, а не из-за сломанного поиска: настоящий
    # токен после него входит.
    assert tenants.verify_magic_token(db_session, token).id == user.id


def test_a_token_stored_in_the_clear_by_the_old_code_does_not_sign_in(db_session):
    """Цена того, что колонка осталась прежней: выданное до выкладки не действует.

    Такая строка на проде есть (2026-10-10 — одна, просрочена): прежний код
    не стирал токен, по которому не вошли.
    """
    in_the_clear = "kJ3vQ1x0Zt8mB5nR7cW2yH4dL6fS9aG0pT1uE3iO5qA"
    user = _add_user(db_session)
    user.magic_token_hash = in_the_clear
    user.magic_token_expires_at = utcnow() + timedelta(minutes=30)
    db_session.commit()

    assert tenants.verify_magic_token(db_session, in_the_clear) is None


# ─── Пути выдачи: токен уходит письмом, в базе — хеш ─────────────────────────


@pytest.fixture
def client(monkeypatch, db_session) -> TestClient:
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    # Счёт запросов — в памяти процесса и с нуля: в CI рядом стоит Redis, и
    # счётчик в нём переживал бы тест.
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: None)
    rate_limit._reset_memory_for_tests()
    return TestClient(api_module.app)


@pytest.fixture
def letters(monkeypatch) -> list[str]:
    """Тела писем, которые ушли бы получателям."""
    sent: list[str] = []
    monkeypatch.setattr(
        notifier, "send_email", lambda *, subject, html_body, to: sent.append(html_body)
    )
    return sent


def _link_in(letter: str) -> tuple[str, str]:
    """Куда ведёт ссылка из письма и какой токен она несёт."""
    links = set(re.findall(r"(/[a-z/-]+)\?token=([A-Za-z0-9_-]+)", letter))
    assert len(links) == 1, letter
    return links.pop()


def _sign_in_as_admin(client: TestClient, session: Session) -> TenantUser:
    admin = _add_user(session, "owner@example.com", role="admin")
    client.cookies.set(
        api_module.COOKIE_NAME, api_module._make_jwt(admin.id, admin.tenant_id, admin.email)
    )
    return admin


def test_a_requested_login_link_signs_in_once(client, letters, db_session):
    user = _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))

    assert client.post("/auth/request", json={"email": ADDRESS}).status_code == 200

    path, token = _link_in(letters[0])
    assert path == "/auth/verify"
    _assert_only_the_hash_is_stored(db_session, user, token)
    first = client.get(path, params={"token": token})
    assert first.status_code == 200 and first.json()["user_id"] == user.id
    assert api_module.COOKIE_NAME in first.cookies
    assert client.get(path, params={"token": token}).status_code == 401


def test_an_invitation_sets_a_password_once(client, letters, db_session):
    _sign_in_as_admin(client, db_session)

    created = client.post("/api/v1/dash/recipients", json={"email": ADDRESS, "role": "viewer"})

    assert created.status_code == 200, created.text
    user = db_session.get(TenantUser, created.json()["id"])
    path, token = _link_in(letters[0])
    assert path == "/set-password"
    _assert_only_the_hash_is_stored(db_session, user, token)
    client.cookies.clear()
    first = client.post("/auth/set-password", json={"token": token, "new_password": PASSWORD})
    assert first.status_code == 200 and first.json()["user_id"] == user.id
    again = client.post("/auth/set-password", json={"token": token, "new_password": "another-1"})
    assert again.status_code == 401
    db_session.refresh(user)
    assert api_module._verify_bcrypt(PASSWORD, user.password_hash)


def test_a_link_resent_by_an_admin_signs_in_once(client, letters, db_session):
    _sign_in_as_admin(client, db_session)
    user = _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))

    resent = client.post(f"/api/v1/dash/recipients/{user.id}/send-login-link")

    assert resent.status_code == 200, resent.text
    path, token = _link_in(letters[0])
    _assert_only_the_hash_is_stored(db_session, user, token)
    client.cookies.clear()
    assert client.get(path, params={"token": token}).status_code == 200
    assert client.get(path, params={"token": token}).status_code == 401


def test_what_the_column_holds_opens_neither_door(client, db_session):
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    stored = _row(db_session, user)["magic_token"]

    assert client.get("/auth/verify", params={"token": stored}).status_code == 401
    refused = client.post("/auth/set-password", json={"token": stored, "new_password": PASSWORD})
    assert refused.status_code == 401
    db_session.refresh(user)
    assert user.password_hash is None
    assert client.get("/auth/verify", params={"token": token}).status_code == 200


def test_a_token_that_is_not_text_is_refused_not_a_server_error(client, db_session):
    """Одиночный суррогат в JSON — строка, которую UTF-8 не кодирует."""
    _add_user(db_session)
    tenants.issue_magic_token(db_session, ADDRESS)

    refused = client.post(
        "/auth/set-password",
        content=b'{"token": "\\ud800", "new_password": "%s"}' % PASSWORD.encode(),
        headers={"content-type": "application/json"},
    )

    assert refused.status_code == 401


def test_the_token_printed_by_the_cli_signs_in_and_is_not_stored(db_session, monkeypatch):
    user = _add_user(db_session)
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: factory)

    result = CliRunner().invoke(main.cli, ["tenant", "issue-token", ADDRESS])

    assert result.exit_code == 0, result.output
    token = re.search(r"^Token: (\S+)$", result.output, re.MULTILINE).group(1)
    _assert_only_the_hash_is_stored(db_session, user, token)
    assert tenants.verify_magic_token(db_session, token).id == user.id
