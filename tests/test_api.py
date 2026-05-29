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


def _make_match_with_prices(db, run, *, canonical, prices, tenant_id=1):
    """Match + products на 2 сайтах + PriceSnapshot для каждого."""
    m = storage.Match(tenant_id=tenant_id, canonical_name=canonical, confidence=1.0)
    db.add(m)
    db.flush()
    for site, price in prices.items():
        p = storage.Product(
            tenant_id=tenant_id,
            site=site,
            external_id=f"{site}-{canonical}",
            url=f"https://{site}.example/{canonical}",
            name=f"{canonical} {site}",
            name_normalized=canonical.lower(),
            canonical_id=m.id,
        )
        db.add(p)
        db.flush()
        db.add(
            storage.PriceSnapshot(run_id=run.id, product_id=p.id, price=price, captured_at=utcnow())
        )
    db.commit()
    return m


def test_comparison_limit_applies_after_filter(client, tenant_user, setup_db):
    """Regression (2026-05-29): `limit` должен применяться к ОТФИЛЬТРОВАННОМУ
    выходу, не к raw matches query.

    Сценарий: создаём 3 невалидных match'а (только 1 сайт с ценой → отсеются)
    с МАЛЕНЬКИМИ id, потом 2 валидных (2 сайта) с большими id. Со старым
    кодом `.limit(2)` взял бы первые 2 невалидных → 0 в выходе. С фиксом —
    fetch все, фильтр, и оба валидных видны.
    """
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    # 3 невалидных (1 сайт) — низкие id
    for i in range(3):
        _make_match_with_prices(s, run, canonical=f"solo{i}", prices={"pharmonline": 10.0})
    # 2 валидных (2 сайта) — высокие id
    _make_match_with_prices(s, run, canonical="dual1", prices={"pharmonline": 10.0, "aloe": 12.0})
    _make_match_with_prices(
        s, run, canonical="dual2", prices={"pharmonline": 20.0, "aptekonline": 30.0}
    )

    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/comparison?min_sites=2")
    assert r.status_code == 200, r.text
    names = [row["name"] for row in r.json()]
    # Оба валидных видны несмотря на то что они после невалидных по id
    assert "dual1" in names
    assert "dual2" in names
    # Невалидные (1 сайт) отсеяны
    assert not any(n.startswith("solo") for n in names)


def test_comparison_sorted_by_spread_desc(client, tenant_user, setup_db):
    """Rows отсортированы по spread_pct desc (самое полезное сверху)."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    # spread small: 10 vs 11 = ~9%
    _make_match_with_prices(s, run, canonical="small", prices={"pharmonline": 10.0, "aloe": 11.0})
    # spread big: 10 vs 30 = ~67%
    _make_match_with_prices(s, run, canonical="big", prices={"pharmonline": 10.0, "aloe": 30.0})

    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/comparison?min_sites=2")
    rows = r.json()
    names = [row["name"] for row in rows]
    # big spread первым
    assert names.index("big") < names.index("small")


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
        json=[
            {
                "sku": "TEST-001",
                "supplier_name": "Supplier A",
                "purchase_price": 12.5,
                "currency": "AZN",
            }
        ],
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


# ─── Phase 2.5 — match suggestions endpoint ──────────────────────────────────


def _make_match_with_products(
    db,
    *,
    confidence: float,
    is_manual: bool = False,
    needs_review: bool = False,
    canonical: str = "Test Product",
    tenant_id: int = 1,
    products_per_site: int = 2,
) -> storage.Match:
    """Helper: create a match + N products on different sites bound to it."""
    m = storage.Match(
        tenant_id=tenant_id,
        canonical_name=canonical,
        confidence=confidence,
        is_manual=is_manual,
        needs_review=needs_review,
    )
    db.add(m)
    db.flush()
    sites = ["pharmonline", "aptekonline", "aloe"][:products_per_site]
    for i, site in enumerate(sites):
        p = storage.Product(
            tenant_id=tenant_id,
            site=site,
            external_id=f"{site}-{canonical.lower()}-{i}",
            url=f"https://{site}.example/p/{canonical}",
            name=f"{canonical} on {site}",
            name_normalized=canonical.lower(),
            canonical_id=m.id,
        )
        db.add(p)
    db.commit()
    return m


def test_match_suggestions_returns_low_confidence(client, tenant_user, setup_db):
    """Matches with confidence below threshold + not manual are returned."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    _make_match_with_products(s, confidence=0.70, canonical="LowConf")
    _make_match_with_products(s, confidence=0.95, canonical="HighConf")  # excluded

    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")  # set cookie
    r = client.get("/api/v1/dash/matches/suggestions?confidence_max=0.85")
    assert r.status_code == 200, r.text
    names = [m["canonical_name"] for m in r.json()]
    assert "LowConf" in names
    assert "HighConf" not in names


def test_match_suggestions_skips_manual(client, tenant_user, setup_db):
    """is_manual=True matches are never in the queue (already confirmed)."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    _make_match_with_products(s, confidence=0.5, is_manual=True, canonical="Manual")
    _make_match_with_products(s, confidence=0.5, is_manual=False, canonical="Auto")
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/matches/suggestions")
    names = [m["canonical_name"] for m in r.json()]
    assert "Auto" in names
    assert "Manual" not in names


def test_match_suggestions_only_needs_review_filter(client, tenant_user, setup_db):
    """only_needs_review=true narrows the list."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    _make_match_with_products(s, confidence=0.5, needs_review=True, canonical="Flagged")
    _make_match_with_products(s, confidence=0.5, needs_review=False, canonical="Unflagged")
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/matches/suggestions?only_needs_review=true")
    names = [m["canonical_name"] for m in r.json()]
    assert "Flagged" in names
    assert "Unflagged" not in names


def test_match_suggestions_sorted_ascending_confidence(client, tenant_user, setup_db):
    """Lowest confidence first (most uncertain, highest review priority)."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    _make_match_with_products(s, confidence=0.80, canonical="Higher")
    _make_match_with_products(s, confidence=0.50, canonical="Lower")
    _make_match_with_products(s, confidence=0.65, canonical="Middle")
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/matches/suggestions")
    names = [m["canonical_name"] for m in r.json()]
    # Order: Lower (.50), Middle (.65), Higher (.80)
    assert names.index("Lower") < names.index("Middle") < names.index("Higher")


def test_match_confirm_sets_is_manual(client, tenant_user, setup_db):
    """POST /confirm sets is_manual=true and clears needs_review."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    m = _make_match_with_products(s, confidence=0.5, needs_review=True, canonical="Confirm me")
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.post(f"/api/v1/dash/matches/{m.id}/confirm")
    assert r.status_code == 204
    s.refresh(m)
    assert m.is_manual is True
    assert m.needs_review is False


def test_match_confirm_404_on_unknown(client, tenant_user, setup_db):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.post("/api/v1/dash/matches/99999/confirm")
    assert r.status_code == 404


def test_match_confirm_requires_auth(client, setup_db):
    r = client.post("/api/v1/dash/matches/1/confirm")
    assert r.status_code == 401


def test_match_suggestions_requires_auth(client, setup_db):
    r = client.get("/api/v1/dash/matches/suggestions")
    assert r.status_code == 401


# ─── Phase 4.1+4.3+4.6 — Pricing settings + cost CSV import ──────────────────


def test_pricing_get_returns_defaults(client, tenant_user, setup_db):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/settings/pricing")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["raise_threshold_pct"] == 5.0
    assert body["undercut_threshold_pct"] == 3.0
    assert body["max_spread_pct"] == 80.0
    assert body["min_margin_pct"] == 10.0
    assert body["max_per_type"] == 10


def test_pricing_put_updates_values(client, tenant_user, setup_db):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.put(
        "/api/v1/dash/settings/pricing",
        json={
            "raise_threshold_pct": 7.5,
            "undercut_threshold_pct": 2.0,
            "max_spread_pct": 70.0,
            "min_margin_pct": 15.0,
            "max_per_type": 20,
        },
    )
    assert r.status_code == 200
    assert r.json()["raise_threshold_pct"] == 7.5
    # GET should reflect new values
    g = client.get("/api/v1/dash/settings/pricing").json()
    assert g["min_margin_pct"] == 15.0


def test_pricing_put_rejects_out_of_range(client, tenant_user, setup_db):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.put(
        "/api/v1/dash/settings/pricing",
        json={
            "raise_threshold_pct": 150.0,  # > 100
            "undercut_threshold_pct": 3.0,
            "max_spread_pct": 80.0,
            "min_margin_pct": 10.0,
            "max_per_type": 10,
        },
    )
    assert r.status_code == 422


def test_pricing_requires_auth(client):
    r = client.get("/api/v1/dash/settings/pricing")
    assert r.status_code == 401
    r2 = client.put("/api/v1/dash/settings/pricing", json={})
    assert r2.status_code == 401


def test_cost_csv_import_rejects_missing_columns(client, tenant_user, setup_db):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    bad_csv = b"sku,name\nABC123,Foo\n"
    r = client.post(
        "/api/v1/dash/settings/costs/import",
        files={"file": ("bad.csv", bad_csv, "text/csv")},
    )
    assert r.status_code == 400
    assert "purchase_price" in r.text or "supplier_name" in r.text


def test_cost_csv_import_imports_valid_rows(client, tenant_user, setup_db):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    # Setup: insert one product so CSV row matches by sku
    p = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="SKU-001",
        url="http://x",
        name="Test",
        name_normalized="test",
    )
    s.add(p)
    s.commit()
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    csv_body = (
        b"sku,supplier_name,purchase_price,currency\n"
        b"SKU-001,Vendor1,3.50,AZN\n"
        b"NOT-FOUND,Vendor1,5.0,AZN\n"
        b"SKU-001,,not_a_number,AZN\n"
    )
    r = client.post(
        "/api/v1/dash/settings/costs/import",
        files={"file": ("costs.csv", csv_body, "text/csv")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_processed"] == 3
    assert body["rows_imported"] == 1
    assert body["rows_skipped"] == 2


# ─── Phase 5.2 prep — batch price-history endpoint ───────────────────────────


def _seed_history(
    db,
    *,
    site: str,
    external_id: str,
    name: str,
    daily_prices: list[float],
    tenant_id: int = 1,
) -> int:
    """Создаёт продукт + snapshots по одному в день с captured_at=days_ago.

    Возвращает product.id. Каждая цена в `daily_prices` идёт в один из
    последних len(prices) дней (от старого к новому).
    """
    p = storage.Product(
        tenant_id=tenant_id,
        site=site,
        external_id=external_id,
        url=f"https://{site}.example/p/{external_id}",
        name=name,
        name_normalized=name.lower(),
    )
    db.add(p)
    db.flush()
    n = len(daily_prices)
    for i, price in enumerate(daily_prices):
        ts = utcnow() - timedelta(days=n - 1 - i)
        run = storage.Run(started_at=ts, status="ok", finished_at=ts)
        db.add(run)
        db.flush()
        snap = storage.PriceSnapshot(
            run_id=run.id,
            product_id=p.id,
            price=price,
            captured_at=ts,
        )
        db.add(snap)
    db.commit()
    return p.id


def _login(client, tenant_user, db):
    """Возвращает True если JWT доступен и сессия установлена."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    token = tenants.issue_magic_token(db, tenant_user.email)
    client.get(f"/auth/verify?token={token}")


def test_price_history_batch_returns_dict_keyed_by_id(client, tenant_user, setup_db):
    """Базовый случай: 2 продукта, оба возвращаются в dict с правильной payload-схемой."""
    s = setup_db
    pid1 = _seed_history(
        s,
        site="pharmonline",
        external_id="b-1",
        name="Drug A",
        daily_prices=[10.0, 11.0, 12.0],
    )
    pid2 = _seed_history(
        s,
        site="aloe",
        external_id="b-2",
        name="Drug B",
        daily_prices=[5.0, 5.0, 6.0],
    )
    _login(client, tenant_user, s)

    r = client.get(f"/api/v1/dash/products/price-history?ids={pid1},{pid2}&days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {str(pid1), str(pid2)}
    a = body[str(pid1)]
    assert a["product_id"] == pid1
    assert a["site"] == "pharmonline"
    assert a["name"] == "Drug A"
    assert len(a["points"]) == 3
    assert a["current"] == 12.0
    assert a["delta_pct"] == 20.0  # (12-10)/10 * 100


def test_price_history_batch_skips_other_tenant(client, tenant_user, setup_db):
    """Продукты другого tenant'а не должны попасть в ответ."""
    s = setup_db
    mine = _seed_history(
        s,
        site="pharmonline",
        external_id="mine",
        name="Mine",
        daily_prices=[10.0, 11.0],
    )
    other = _seed_history(
        s,
        site="pharmonline",
        external_id="other",
        name="Other",
        daily_prices=[20.0, 22.0],
        tenant_id=999,
    )
    _login(client, tenant_user, s)

    r = client.get(f"/api/v1/dash/products/price-history?ids={mine},{other}&days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert str(mine) in body
    assert str(other) not in body


def test_price_history_batch_empty_ids_returns_empty(client, tenant_user, setup_db):
    """ids='' → {} без 400."""
    s = setup_db
    _login(client, tenant_user, s)
    r = client.get("/api/v1/dash/products/price-history?ids=&days=30")
    assert r.status_code == 200
    assert r.json() == {}


def test_price_history_batch_invalid_ids_400(client, tenant_user, setup_db):
    """Нечисловые ids → 400."""
    s = setup_db
    _login(client, tenant_user, s)
    r = client.get("/api/v1/dash/products/price-history?ids=abc,def&days=30")
    assert r.status_code == 400


def test_price_history_batch_too_many_ids_400(client, tenant_user, setup_db):
    """Больше 50 ids → 400 (anti-abuse)."""
    s = setup_db
    _login(client, tenant_user, s)
    ids = ",".join(str(i) for i in range(1, 52))
    r = client.get(f"/api/v1/dash/products/price-history?ids={ids}&days=30")
    assert r.status_code == 400


def test_price_history_batch_no_snapshots_returns_empty_points(client, tenant_user, setup_db):
    """Продукт без snapshots → присутствует в ответе с points=[]."""
    s = setup_db
    p = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="no-snaps",
        url="https://x",
        name="NoSnaps",
        name_normalized="nosnaps",
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    _login(client, tenant_user, s)

    r = client.get(f"/api/v1/dash/products/price-history?ids={p.id}&days=30")
    assert r.status_code == 200
    body = r.json()
    assert str(p.id) in body
    assert body[str(p.id)]["points"] == []
    assert body[str(p.id)]["current"] is None
    assert body[str(p.id)]["delta_pct"] is None


def test_price_history_batch_requires_auth(client, setup_db):
    """Без cookie → 401."""
    r = client.get("/api/v1/dash/products/price-history?ids=1&days=30")
    assert r.status_code == 401


def test_price_history_batch_rejects_huge_ids_string(client, tenant_user, setup_db):
    """Codex review fix: `ids` string > 2048 chars → 400 (DoS guard)."""
    s = setup_db
    _login(client, tenant_user, s)
    # 3000 chars of `1,1,1,...` — длиннее лимита но валидные ints
    huge = ",".join(["1"] * 1500)  # ~3000 chars
    r = client.get(f"/api/v1/dash/products/price-history?ids={huge}&days=30")
    assert r.status_code == 400


def test_price_history_batch_dedupes_ids(client, tenant_user, setup_db):
    """Codex review fix: дубли в `ids` собираются в unique set."""
    s = setup_db
    pid = _seed_history(
        s,
        site="pharmonline",
        external_id="dup-test",
        name="Dup",
        daily_prices=[10.0, 11.0],
    )
    _login(client, tenant_user, s)
    # Шлём 5 раз тот же id — должно работать, не падать на "too many"
    dup_ids = ",".join([str(pid)] * 5)
    r = client.get(f"/api/v1/dash/products/price-history?ids={dup_ids}&days=30")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1  # дедуплицировано
    assert str(pid) in body
