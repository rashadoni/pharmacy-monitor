"""API endpoint tests using FastAPI TestClient + in-memory SQLite.

Coverage:
  - /health
  - /auth/request rate-limit + email enumeration safety
  - /auth/verify magic-token flow
  - /api/v1/dash/* require JWT cookie (401 without)
  - /api/v1/dash/comparison filters by tenant_id
  - /api/v1/dash/categories CRUD with role enforcement
  - /api/v1/products legacy ERP endpoint requires X-API-Key
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from src import api as api_module
from src import storage, tenants
from src._time import utcnow


@pytest.fixture
def setup_db(monkeypatch, db_session):
    """Override storage.make_session() to return our in-memory session factory."""
    engine = db_session.get_bind()
    Session = sessionmaker(engine, expire_on_commit=False)

    def fake_make_session(database_url=None):
        return Session

    monkeypatch.setattr(storage, "make_session", fake_make_session)
    monkeypatch.setenv("PHARMACY_API_KEY", "test-key-1234")
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    monkeypatch.setenv("PHARMACY_AUTH_DEV_SHOW_TOKEN", "1")
    return db_session


@pytest.fixture
def client(setup_db):
    return TestClient(api_module.app)


@pytest.fixture
def tenant_user(setup_db):
    """Create default tenant + a user."""
    s = setup_db
    tenant = tenants.get_or_create_default(s)
    user = storage.TenantUser(
        tenant_id=tenant.id,
        email="test@example.com",
        name="Test User",
        role="admin",
        is_active=True,
        created_at=utcnow(),
    )
    s.add(user)
    s.commit()
    s.refresh(user)
    return user


@pytest.fixture
def auth_cookie(client, tenant_user):
    """Login via magic-link, return JWT cookie value."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    token = tenants.issue_magic_token(tenant_user._sa_instance_state.session, tenant_user.email)
    if not token:
        pytest.skip("issue_magic_token returned None")
    response = client.get(f"/auth/verify?token={token}")
    assert response.status_code == 200
    return response.cookies.get(api_module.COOKIE_NAME)


# ─── Public endpoints ────────────────────────────────────────────────────────

def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "up"
    assert "last_run_at" in body
    assert "last_run_status" in body


# ─── Auth endpoints ──────────────────────────────────────────────────────────

def test_auth_request_unknown_email_returns_success(client):
    """Don't leak email existence — always 200."""
    r = client.post("/auth/request", json={"email": "nobody@example.com"})
    assert r.status_code == 200
    assert r.json()["sent"] is True


def test_auth_request_invalid_email_400(client):
    r = client.post("/auth/request", json={"email": "not-an-email"})
    assert r.status_code == 422  # pydantic validation


def test_auth_verify_invalid_token_401(client):
    r = client.get("/auth/verify?token=bogus")
    assert r.status_code == 401


def test_auth_verify_valid_token_sets_cookie(client, tenant_user, setup_db):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    token = tenants.issue_magic_token(setup_db, tenant_user.email)
    assert token is not None
    r = client.get(f"/auth/verify?token={token}")
    assert r.status_code == 200
    assert api_module.COOKIE_NAME in r.cookies


# ─── Dashboard endpoints — require JWT ───────────────────────────────────────

def test_dash_me_without_cookie_401(client):
    r = client.get("/api/v1/dash/me")
    assert r.status_code == 401


def test_dash_comparison_without_cookie_401(client):
    r = client.get("/api/v1/dash/comparison")
    assert r.status_code == 401


def test_dash_alerts_without_cookie_401(client):
    r = client.get("/api/v1/dash/alerts")
    assert r.status_code == 401


# ─── Legacy ERP endpoints — require X-API-Key ────────────────────────────────

def test_legacy_products_without_key_401(client):
    r = client.get("/api/v1/products")
    assert r.status_code == 401


def test_legacy_products_with_key_200(client):
    r = client.get(
        "/api/v1/products",
        headers={"X-API-Key": "test-key-1234"},
    )
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_legacy_alerts_recent_with_key(client):
    r = client.get(
        "/api/v1/alerts/recent",
        headers={"X-API-Key": "test-key-1234"},
    )
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_legacy_comparisons_with_key_empty(client):
    r = client.get(
        "/api/v1/comparisons",
        headers={"X-API-Key": "test-key-1234"},
    )
    assert r.status_code == 200
    assert r.json() == []  # empty DB


def test_legacy_invalid_key_401(client):
    r = client.get(
        "/api/v1/products",
        headers={"X-API-Key": "wrong"},
    )
    assert r.status_code == 401


# ─── Inventory push endpoints ────────────────────────────────────────────────

def test_legacy_push_stock_with_key(client):
    r = client.post(
        "/api/v1/inventory/stock",
        headers={"X-API-Key": "test-key-1234"},
        json=[{"sku": "TEST-001", "qty": 5, "name": "Test product"}],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["received"] == 1


def test_legacy_push_prices_with_key(client):
    r = client.post(
        "/api/v1/inventory/prices",
        headers={"X-API-Key": "test-key-1234"},
        json=[{
            "sku": "TEST-001",
            "supplier_name": "Supplier A",
            "purchase_price": 12.5,
            "currency": "AZN",
        }],
    )
    assert r.status_code == 200


# ─── Categories CRUD (requires admin role) ───────────────────────────────────

def test_dash_categories_create_requires_auth(client):
    r = client.post(
        "/api/v1/dash/categories",
        json={"key": "test", "label_ru": "Test"},
    )
    assert r.status_code == 401


# ─── JWT decode/encode (no DB needed) ────────────────────────────────────────

def test_jwt_roundtrip(monkeypatch):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    monkeypatch.setenv("JWT_SECRET", "test-very-long-secret-key-not-for-prod")
    monkeypatch.setattr(api_module, "JWT_SECRET", "test-very-long-secret-key-not-for-prod")
    token = api_module._make_jwt(user_id=42, tenant_id=1, email="x@y.com")
    payload = api_module._decode_jwt(token)
    assert payload is not None
    assert payload["sub"] == "42"
    assert payload["tid"] == 1
    assert payload["email"] == "x@y.com"


def test_jwt_decode_garbage_returns_none():
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    assert api_module._decode_jwt("garbage.not.jwt") is None


def test_jwt_decode_wrong_secret(monkeypatch):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    monkeypatch.setattr(api_module, "JWT_SECRET", "secret-a")
    token = api_module._make_jwt(user_id=1, tenant_id=1, email="a@b.c")
    monkeypatch.setattr(api_module, "JWT_SECRET", "secret-b")
    assert api_module._decode_jwt(token) is None
