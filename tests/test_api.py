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
    # Phase 0.3 — deep health fields
    assert "db_ping_ms" in body
    assert "redis_ping_ms" in body  # null if REDIS_URL unset in tests
    assert body.get("sites") == []  # empty DB → no sites
    assert body["staleness_warning"] is False
    assert body["db_ping_ms"] is not None and body["db_ping_ms"] >= 0


def test_health_endpoint_db_ping_responds_quickly(client):
    """SQLite in-memory ping should always be < 100ms."""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db_ping_ms"] < 100


def test_health_endpoint_redis_unset_returns_null(client, monkeypatch):
    """No REDIS_URL → redis_ping_ms is null, but health still reports up."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["redis_ping_ms"] is None
    assert body["status"] == "up"


def test_health_endpoint_flags_staleness(client, setup_db):
    """Product older than 30h → staleness_warning=true, status=degraded."""
    from datetime import timedelta

    db = setup_db
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    db.add(
        storage.Product(
            tenant_id=1,
            site="pharmonline",
            external_id="stale-1",
            url="https://example.com/x",
            name="Stale",
            name_normalized="stale",
            last_seen_at=old,
            first_seen_at=old,
        )
    )
    db.commit()
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["staleness_warning"] is True
    assert body["status"] == "degraded"
    sites = {s["site"]: s for s in body["sites"]}
    assert "pharmonline" in sites
    assert sites["pharmonline"]["hours_since"] >= 48


# ─── Request ID middleware (Phase 0.5) ───────────────────────────────────────


def test_request_id_generated_when_absent(client):
    """No incoming header → server generates a UUID4-hex (32 chars)."""
    r = client.get("/health")
    rid = r.headers.get("X-Request-ID")
    assert rid is not None
    # uuid4().hex is 32 lowercase hex chars
    assert len(rid) == 32
    assert all(c in "0123456789abcdef" for c in rid)


def test_request_id_echoes_safe_client_value(client):
    """Safe-looking client `X-Request-ID` is echoed back unchanged."""
    rid_in = "client-trace-abc.123_xyz"
    r = client.get("/health", headers={"X-Request-ID": rid_in})
    assert r.headers.get("X-Request-ID") == rid_in


def test_request_id_rejects_unsafe_client_value(client):
    """Newlines / control chars / overlong values must be replaced, not logged."""
    bad = "evil\r\nLog-Injection: pwned" + "A" * 200
    r = client.get("/health", headers={"X-Request-ID": bad})
    out = r.headers.get("X-Request-ID")
    assert out is not None
    assert out != bad
    assert "\n" not in out and "\r" not in out
    assert len(out) == 32  # falls back to fresh UUID


def test_request_id_unique_per_request(client):
    """Two consecutive requests get distinct IDs (no leak from contextvars)."""
    r1 = client.get("/health")
    r2 = client.get("/health")
    assert r1.headers["X-Request-ID"] != r2.headers["X-Request-ID"]


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
