"""Админ задаёт пользователю пароль на странице «Пользователи».

Просьба владельца 2026-10-26: письмо со ссылкой до пользователя не дошло, и
нужен способ дать ему вход без почты — задать пароль самому и сообщить его.

- после записи пользователь входит по email и новому паролю, прежний не входит;
- правило пароля то же, что везде, и отказ — до записи;
- только админ, только в своём арендаторе, не себе (свой — в настройках, где
  спрашивают текущий) и не деактивированному;
- пароля нет ни в ответе, ни в журнале; адреса в журнале нет.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import api as api_module
from src import rate_limit, storage
from src._time import utcnow
from src.storage import TenantUser
from tests.test_magic_link_token import ADDRESS, PASSWORD, _add_user, _sign_in_as_admin

NEW_PASSWORD = "chosen-by-the-admin-42"


@pytest.fixture
def client(monkeypatch, db_session) -> TestClient:
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: None)
    rate_limit._reset_memory_for_tests()
    return TestClient(api_module.app)


def _set(client: TestClient, user_id: int, password: str = NEW_PASSWORD):
    return client.post(
        f"/api/v1/dash/recipients/{user_id}/password", json={"new_password": password}
    )


def _signs_in(password: str) -> bool:
    # Свой клиент: cookie админа не мешает и не подменяется.
    fresh = TestClient(api_module.app)
    return fresh.post("/auth/login", json={"login": ADDRESS, "password": password}).is_success


@pytest.mark.parametrize("had_password", [True, False])
def test_the_user_signs_in_with_the_password_the_admin_chose(client, db_session, had_password):
    _sign_in_as_admin(client, db_session)
    fields = {"password_hash": api_module._hash_bcrypt(PASSWORD)} if had_password else {}
    user = _add_user(db_session, **fields)

    with capture_logs() as logs:
        r = _set(client, user.id)

    assert r.status_code == 200, r.text
    assert NEW_PASSWORD not in r.text
    assert _signs_in(NEW_PASSWORD)
    assert not _signs_in(PASSWORD)
    # В журнале — кто и кому, без пароля и адреса.
    assert NEW_PASSWORD not in repr(logs) and "@" not in repr(logs)
    (entry,) = [e for e in logs if e["event"] == "recipient_password_set"]
    assert entry["id"] == user.id


def test_the_password_rule_refuses_before_anything_is_written(client, db_session):
    _sign_in_as_admin(client, db_session)
    user = _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))

    r = _set(client, user.id, "short")

    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "password_too_short"
    assert "short" not in r.json()["detail"][0]["msg"].split()
    assert _signs_in(PASSWORD)


def test_surrounding_spaces_are_dropped_as_sign_in_drops_them(client, db_session):
    _sign_in_as_admin(client, db_session)
    user = _add_user(db_session)

    assert _set(client, user.id, f"  {NEW_PASSWORD} ").status_code == 200

    assert _signs_in(NEW_PASSWORD)


def test_a_viewer_cannot_set_passwords(client, db_session):
    viewer = _add_user(db_session, "viewer@example.com", role="viewer")
    target = _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))
    client.cookies.set(
        api_module.COOKIE_NAME, api_module._make_jwt(viewer.id, viewer.tenant_id, viewer.email)
    )

    assert _set(client, target.id).status_code == 403
    assert _signs_in(PASSWORD)


def test_not_for_yourself_the_settings_ask_for_the_current_one(client, db_session):
    admin = _sign_in_as_admin(client, db_session)

    r = _set(client, admin.id)

    assert r.status_code == 400
    db_session.refresh(admin)
    assert admin.password_hash is None


def test_another_admin_too_by_design(client, db_session):
    """Другому админу — тоже: решение владельца, а не недосмотр.

    Запрет здесь ничего бы не защитил: админ и так может понизить другого до
    viewer, задать пароль и вернуть роль. Кто задал — в журнале
    (`recipient_password_set by_user_id`).
    """
    _sign_in_as_admin(client, db_session)
    other_admin = _add_user(db_session, role="admin")

    assert _set(client, other_admin.id).status_code == 200
    assert _signs_in(NEW_PASSWORD)


def test_not_for_a_deactivated_user(client, db_session):
    _sign_in_as_admin(client, db_session)
    user = _add_user(db_session, is_active=False)

    assert _set(client, user.id).status_code == 400
    db_session.refresh(user)
    assert user.password_hash is None


def test_not_for_a_user_of_another_tenant(client, db_session):
    _sign_in_as_admin(client, db_session)
    other = storage.Tenant(name="Other", slug="other", created_at=utcnow())
    db_session.add(other)
    db_session.commit()
    stranger = TenantUser(
        tenant_id=other.id, email=ADDRESS, role="viewer", is_active=True, created_at=utcnow()
    )
    db_session.add(stranger)
    db_session.commit()

    assert _set(client, stranger.id).status_code == 404
    db_session.refresh(stranger)
    assert stranger.password_hash is None
