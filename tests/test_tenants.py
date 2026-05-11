"""Тесты multi-tenant: создание, default, magic-link auth."""

from datetime import datetime, timedelta
from src._time import utcnow

import pytest

from src import tenants as t_mod


def test_get_or_create_default_idempotent(db_session):
    t1 = t_mod.get_or_create_default(db_session)
    t2 = t_mod.get_or_create_default(db_session)
    assert t1.id == t2.id
    assert t1.slug == "default"


def test_create_tenant_unique_slug(db_session):
    t_mod.create_tenant(db_session, "client-a", "Client A", plan="basic")
    with pytest.raises(ValueError):
        t_mod.create_tenant(db_session, "Client-A", "Other name")  # case-insensitive dedup


def test_list_tenants(db_session):
    t_mod.get_or_create_default(db_session)
    t_mod.create_tenant(db_session, "client-b", "B")
    rows = t_mod.list_tenants(db_session)
    slugs = {t.slug for t in rows}
    assert "default" in slugs and "client-b" in slugs


def test_add_user_idempotent(db_session):
    t = t_mod.get_or_create_default(db_session)
    u1 = t_mod.add_user(db_session, t.id, "x@y.com", name="X")
    u2 = t_mod.add_user(db_session, t.id, "x@y.com", name="X-renamed")
    assert u1.id == u2.id
    assert u2.name == "X-renamed"


def test_magic_token_roundtrip(db_session):
    t = t_mod.get_or_create_default(db_session)
    t_mod.add_user(db_session, t.id, "user@example.com")
    token = t_mod.issue_magic_token(db_session, "user@example.com")
    assert token is not None and len(token) > 20

    user = t_mod.verify_magic_token(db_session, token)
    assert user is not None
    assert user.email == "user@example.com"
    assert user.last_login_at is not None
    # После использования — токен инвалидирован
    assert t_mod.verify_magic_token(db_session, token) is None


def test_magic_token_unknown_user(db_session):
    assert t_mod.issue_magic_token(db_session, "ghost@x.com") is None


def test_magic_token_expired(db_session):
    t = t_mod.get_or_create_default(db_session)
    u = t_mod.add_user(db_session, t.id, "exp@x.com")
    token = t_mod.issue_magic_token(db_session, "exp@x.com", ttl_minutes=5)
    # "Состарим" токен искусственно
    u.magic_token_expires_at = utcnow() - timedelta(minutes=1)
    db_session.commit()
    assert t_mod.verify_magic_token(db_session, token) is None


def test_get_tenant_returns_none_for_unknown(db_session):
    assert t_mod.get_tenant(db_session, "no-such-tenant") is None
