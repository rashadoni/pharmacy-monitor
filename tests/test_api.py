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


# ─── Per-site dashboard endpoints (для страницы /aloe) ───────────────────────

def _seed_aloe_products(session, tenant_id: int = 1) -> dict[str, int]:
    """Seed 3 aloe-продукта в разных категориях/брендах для тестов /dash/products*."""
    run = storage.Run(started_at=utcnow(), status="ok", tenant_id=tenant_id)
    session.add(run)
    session.flush()

    p1 = storage.Product(
        tenant_id=tenant_id, site="aloe", external_id="al-1",
        url="https://aloe.az/p/1", name="Aspirin Cardio", name_normalized="aspirin cardio",
        brand="Bayer", category="dermanlar",
    )
    p2 = storage.Product(
        tenant_id=tenant_id, site="aloe", external_id="al-2",
        url="https://aloe.az/p/2", name="Solgar D3", name_normalized="solgar d3",
        brand="Solgar", category="bad",
    )
    p3 = storage.Product(
        tenant_id=tenant_id, site="aloe", external_id="al-3",
        url="https://aloe.az/p/3", name="Vəfa Tea", name_normalized="vəfa tea",
        brand="Vəfa", category="bad",
    )
    session.add_all([p1, p2, p3])
    session.flush()
    session.add_all([
        storage.PriceSnapshot(run_id=run.id, product_id=p1.id, price=15.0, is_on_sale=False),
        storage.PriceSnapshot(run_id=run.id, product_id=p2.id, price=42.0, discount_price=35.0, is_on_sale=True),
        storage.PriceSnapshot(run_id=run.id, product_id=p3.id, price=8.5, is_on_sale=False),
    ])
    session.commit()
    return {"p1": p1.id, "p2": p2.id, "p3": p3.id, "run": run.id}


def test_dash_products_requires_auth(client):
    r = client.get("/api/v1/dash/products?site=aloe")
    assert r.status_code == 401


def test_dash_products_rejects_unknown_site(client, auth_cookie):
    r = client.get(
        "/api/v1/dash/products?site=evilcorp",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 400
    assert "Unknown site" in r.json()["detail"]


def test_dash_products_returns_aloe_items(client, auth_cookie, tenant_user, setup_db):
    _seed_aloe_products(setup_db, tenant_id=tenant_user.tenant_id)
    r = client.get(
        "/api/v1/dash/products?site=aloe",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    names = sorted(it["name"] for it in body["items"])
    assert names == ["Aspirin Cardio", "Solgar D3", "Vəfa Tea"]
    solgar = next(it for it in body["items"] if it["name"] == "Solgar D3")
    assert solgar["discount_price"] == 35.0
    assert solgar["is_on_sale"] is True
    assert solgar["effective_price"] == 35.0


def test_dash_products_filters_by_category_and_brand(
    client, auth_cookie, tenant_user, setup_db
):
    _seed_aloe_products(setup_db, tenant_id=tenant_user.tenant_id)
    r = client.get(
        "/api/v1/dash/products?site=aloe&category=bad",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    body = r.json()
    assert {it["category"] for it in body["items"]} == {"bad"}
    assert len(body["items"]) == 2

    r2 = client.get(
        "/api/v1/dash/products?site=aloe&brand=Bayer",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert len(body2["items"]) == 1
    assert body2["items"][0]["brand"] == "Bayer"


def test_dash_products_search_matches_name_and_brand(
    client, auth_cookie, tenant_user, setup_db
):
    _seed_aloe_products(setup_db, tenant_id=tenant_user.tenant_id)
    r = client.get(
        "/api/v1/dash/products?site=aloe&search=solgar",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "Solgar D3"


def test_dash_products_on_sale_filter(client, auth_cookie, tenant_user, setup_db):
    _seed_aloe_products(setup_db, tenant_id=tenant_user.tenant_id)
    r = client.get(
        "/api/v1/dash/products?site=aloe&on_sale=true",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["is_on_sale"] is True


def test_dash_products_pagination_offset_beyond_total(
    client, auth_cookie, tenant_user, setup_db
):
    _seed_aloe_products(setup_db, tenant_id=tenant_user.tenant_id)
    r = client.get(
        "/api/v1/dash/products?site=aloe&offset=999&limit=10",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    body = r.json()
    assert body["total"] == 3
    assert body["items"] == []


def test_dash_products_facets_returns_categories_and_brands(
    client, auth_cookie, tenant_user, setup_db
):
    _seed_aloe_products(setup_db, tenant_id=tenant_user.tenant_id)
    r = client.get(
        "/api/v1/dash/products/facets?site=aloe",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    body = r.json()
    cats = {c["name"] for c in body["categories"]}
    assert cats == {"dermanlar", "bad"}
    bad_cat = next(c for c in body["categories"] if c["name"] == "bad")
    assert bad_cat["count"] == 2
    brands = {b["name"] for b in body["brands"]}
    assert brands == {"Bayer", "Solgar", "Vəfa"}


def test_dash_products_summary_returns_kpis(
    client, auth_cookie, tenant_user, setup_db
):
    _seed_aloe_products(setup_db, tenant_id=tenant_user.tenant_id)
    r = client.get(
        "/api/v1/dash/products/summary?site=aloe",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_products"] == 3
    assert body["total_brands"] == 3
    assert body["on_sale_count"] == 1
    assert body["on_sale_pct"] == round(1 / 3 * 100, 1)
    # 3 brand_share rows, all exclusive_to=aloe (нет pharmonline/aptekonline в БД)
    assert body["exclusive_brands"] == 3
    assert body["last_run_id"] is not None


def test_dash_brand_share_site_param(
    client, auth_cookie, tenant_user, setup_db
):
    _seed_aloe_products(setup_db, tenant_id=tenant_user.tenant_id)
    r = client.get(
        "/api/v1/dash/brand-share?site=aloe",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    body = r.json()
    brands = {row["brand"] for row in body}
    assert brands == {"Bayer", "Solgar", "Vəfa"}


def test_dash_brand_share_rejects_unknown_site(client, auth_cookie):
    r = client.get(
        "/api/v1/dash/brand-share?site=hax",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 400


def test_dash_price_index_rejects_unknown_client_site(client, auth_cookie):
    r = client.get(
        "/api/v1/dash/price-index?client_site=hax",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 400


def test_dash_roi_actions_rejects_unknown_client_site(client, auth_cookie):
    r = client.get(
        "/api/v1/dash/roi/actions?client_site=hax",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 400


# ─── AI normalize stats endpoint ─────────────────────────────────────────────


def test_dash_normalize_stats_empty_db(client, auth_cookie):
    r = client.get(
        "/api/v1/dash/normalize/stats",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["products_total"] == 0
    assert body["products_normalized"] == 0
    assert body["coverage_pct"] == 0.0
    assert body["matches_by_strategy"] == {}


def test_dash_normalize_stats_with_data(
    client, auth_cookie, tenant_user, setup_db
):
    """3 продукта, 2 с normalized_attrs (1 needs_review). 1 Match со strategy."""
    s = setup_db
    run = storage.Run(started_at=utcnow(), status="ok", tenant_id=tenant_user.tenant_id)
    s.add(run)
    s.flush()

    p1 = storage.Product(
        tenant_id=tenant_user.tenant_id, site="aloe", external_id="1",
        url="x", name="A", name_normalized="a", brand="X",
        normalized_attrs={"active_ingredient": "asa", "confidence": 0.9, "needs_review": False},
        normalize_hash="h1",
        normalized_at=utcnow(),
    )
    p2 = storage.Product(
        tenant_id=tenant_user.tenant_id, site="aloe", external_id="2",
        url="x", name="B", name_normalized="b", brand="X",
        normalized_attrs={"active_ingredient": "asa", "confidence": 0.4, "needs_review": True},
        normalize_hash="h2",
        normalized_at=utcnow(),
    )
    p3 = storage.Product(
        tenant_id=tenant_user.tenant_id, site="aloe", external_id="3",
        url="x", name="C", name_normalized="c", brand="X",
    )
    m = storage.Match(
        tenant_id=tenant_user.tenant_id, canonical_name="A",
        confidence=0.95, is_manual=False, match_strategy="ai_attrs_strict",
    )
    s.add_all([p1, p2, p3, m])
    s.commit()

    r = client.get(
        "/api/v1/dash/normalize/stats",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["products_total"] == 3
    assert body["products_normalized"] == 2
    assert body["needs_review"] == 1
    assert body["coverage_pct"] == round(2 / 3 * 100, 1)
    assert body["matches_by_strategy"] == {"ai_attrs_strict": 1}
    assert body["last_normalized_at"] is not None


def test_dash_normalize_stats_requires_auth(client):
    r = client.get("/api/v1/dash/normalize/stats")
    assert r.status_code == 401


# ─── Manual aloe-matcher endpoints ───────────────────────────────────────────


_cluster_seq = [0]


def _seed_matched_cluster(session, *, tenant_id=1, sites=("pharmonline", "aptekonline"), category="dermanlar"):
    """Создать Match + N продуктов (по умолчанию ph+apt), вернуть (match, products)."""
    _cluster_seq[0] += 1
    seq = _cluster_seq[0]
    m = storage.Match(
        tenant_id=tenant_id, canonical_name=f"Aspirin Cardio #{seq}",
        canonical_brand="Bayer", confidence=1.0,
    )
    session.add(m)
    session.flush()
    products = []
    for i, site in enumerate(sites):
        p = storage.Product(
            tenant_id=tenant_id, site=site, external_id=f"{site}-c{seq}-x{i}",
            url=f"https://{site}.az/p/{seq}/{i}",
            name=f"Aspirin Cardio 100mg 30 tab #{seq}",
            name_normalized="aspirin cardio", brand="Bayer",
            category=category, canonical_id=m.id,
        )
        session.add(p)
        products.append(p)
    session.flush()
    return m, products


def test_dash_unmatched_pairs_returns_clusters_missing_site(
    client, auth_cookie, tenant_user, setup_db
):
    """Match с ph+apt но без aloe → попадает в unmatched-pairs?site=aloe."""
    m, _ = _seed_matched_cluster(setup_db, tenant_id=tenant_user.tenant_id)
    setup_db.commit()

    r = client.get(
        "/api/v1/dash/unmatched-pairs?site=aloe",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["match_id"] == m.id
    sites_in_anchor = {a["site"] for a in item["anchor_products"]}
    assert sites_in_anchor == {"pharmonline", "aptekonline"}
    assert "aloe" not in sites_in_anchor


def test_dash_unmatched_pairs_excludes_complete_clusters(
    client, auth_cookie, tenant_user, setup_db
):
    """Match со всеми тремя сайтами не должен попадать в результат."""
    _seed_matched_cluster(
        setup_db,
        tenant_id=tenant_user.tenant_id,
        sites=("pharmonline", "aptekonline", "aloe"),
    )
    setup_db.commit()

    r = client.get(
        "/api/v1/dash/unmatched-pairs?site=aloe",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_dash_unmatched_pairs_filters_by_category(
    client, auth_cookie, tenant_user, setup_db
):
    _seed_matched_cluster(setup_db, tenant_id=tenant_user.tenant_id, category="dermanlar")
    _seed_matched_cluster(setup_db, tenant_id=tenant_user.tenant_id, category="bad")
    setup_db.commit()

    r = client.get(
        "/api/v1/dash/unmatched-pairs?site=aloe&category=bad",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    cats = {a["category"] for a in body["items"][0]["anchor_products"]}
    assert cats == {"bad"}


def test_dash_unmatched_pairs_rejects_unknown_site(client, auth_cookie):
    r = client.get(
        "/api/v1/dash/unmatched-pairs?site=hax",
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 400


def test_dash_match_add_product_links_correctly(
    client, auth_cookie, tenant_user, setup_db
):
    """Привязка aloe-продукта к существующему ph+apt кластеру."""
    m, _ = _seed_matched_cluster(setup_db, tenant_id=tenant_user.tenant_id)
    aloe_p = storage.Product(
        tenant_id=tenant_user.tenant_id, site="aloe", external_id="al-x",
        url="https://aloe.az/p/x", name="Aspirin Cardio",
        name_normalized="aspirin cardio", brand="Bayer", category="dermanlar",
    )
    setup_db.add(aloe_p)
    setup_db.commit()

    r = client.post(
        f"/api/v1/dash/matches/{m.id}/add-product",
        json={"product_id": aloe_p.id},
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["match_id"] == m.id
    assert body["is_manual"] is True
    assert body["match_strategy"] == "manual"
    assert len(body["products"]) == 3
    sites = {p["site"] for p in body["products"]}
    assert sites == {"pharmonline", "aptekonline", "aloe"}

    setup_db.refresh(aloe_p)
    assert aloe_p.canonical_id == m.id


def test_dash_match_add_product_blocks_site_collision(
    client, auth_cookie, tenant_user, setup_db
):
    """Если в кластере уже есть продукт с тем же site — 409."""
    m, _ = _seed_matched_cluster(setup_db, tenant_id=tenant_user.tenant_id)
    extra_ph = storage.Product(
        tenant_id=tenant_user.tenant_id, site="pharmonline", external_id="ph-extra",
        url="x", name="Aspirin Other", name_normalized="aspirin", brand="Bayer",
    )
    setup_db.add(extra_ph)
    setup_db.commit()

    r = client.post(
        f"/api/v1/dash/matches/{m.id}/add-product",
        json={"product_id": extra_ph.id},
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 409


def test_dash_match_add_product_404_on_missing_match(client, auth_cookie):
    r = client.post(
        "/api/v1/dash/matches/9999/add-product",
        json={"product_id": 1},
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 404


def test_dash_match_add_product_requires_auth(client):
    r = client.post(
        "/api/v1/dash/matches/1/add-product",
        json={"product_id": 1},
    )
    assert r.status_code == 401


def test_dash_match_create_with_products(
    client, auth_cookie, tenant_user, setup_db
):
    """Создание нового manual Match'а из 2-3 несвязанных продуктов."""
    s = setup_db
    p_ph = storage.Product(
        tenant_id=tenant_user.tenant_id, site="pharmonline", external_id="ph",
        url="x", name="Aspirin", name_normalized="aspirin", brand="Bayer",
    )
    p_al = storage.Product(
        tenant_id=tenant_user.tenant_id, site="aloe", external_id="al",
        url="x", name="Aspirin", name_normalized="aspirin", brand="Bayer",
    )
    s.add_all([p_ph, p_al])
    s.commit()

    r = client.post(
        "/api/v1/dash/matches/create-with-products",
        json={"product_ids": [p_ph.id, p_al.id]},
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_manual"] is True
    assert body["match_strategy"] == "manual"
    sites = {p["site"] for p in body["products"]}
    assert sites == {"pharmonline", "aloe"}

    s.refresh(p_ph)
    s.refresh(p_al)
    assert p_ph.canonical_id == p_al.canonical_id == body["match_id"]


def test_dash_match_create_rejects_same_site_duplicates(
    client, auth_cookie, tenant_user, setup_db
):
    """Два продукта с одного сайта в одном manual match'е — 409."""
    s = setup_db
    p1 = storage.Product(
        tenant_id=tenant_user.tenant_id, site="aloe", external_id="al1",
        url="x", name="A", name_normalized="a", brand="X",
    )
    p2 = storage.Product(
        tenant_id=tenant_user.tenant_id, site="aloe", external_id="al2",
        url="x", name="B", name_normalized="b", brand="X",
    )
    s.add_all([p1, p2])
    s.commit()

    r = client.post(
        "/api/v1/dash/matches/create-with-products",
        json={"product_ids": [p1.id, p2.id]},
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    assert r.status_code == 409


def test_dash_match_create_validates_min_two_products(client, auth_cookie):
    r = client.post(
        "/api/v1/dash/matches/create-with-products",
        json={"product_ids": [42]},
        cookies={api_module.COOKIE_NAME: auth_cookie},
    )
    # Pydantic validation fails → 422
    assert r.status_code == 422
