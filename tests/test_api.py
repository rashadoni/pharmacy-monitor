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
from sqlalchemy import select
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
    """Daily-cadence site older than 30h → staleness_warning=true, status=degraded."""

    db = setup_db
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    db.add(
        storage.Product(
            tenant_id=1,
                site="aloe",
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
    assert "aloe" in sites
    assert sites["aloe"]["hours_since"] >= 48


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


def _make_match_with_prices(db, run, *, canonical, prices, tenant_id=1, category=None):
    """Match + products на 2 сайтах + PriceSnapshot для каждого.

    `category` (опц.) проставляется всем products — для тестов
    /category-comparison и drill-down /comparison?category=.
    """
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
            category=category,
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


def _make_match_with_packs(db, run, *, canonical, prods, tenant_id=1):
    """Match + products с заданными (site, price, pack_size, name)."""
    m = storage.Match(tenant_id=tenant_id, canonical_name=canonical, confidence=1.0)
    db.add(m)
    db.flush()
    for site, price, pack, name in prods:
        p = storage.Product(
            tenant_id=tenant_id,
            site=site,
            external_id=f"{site}-{canonical}",
            url=f"https://{site}/{canonical}",
            name=name,
            name_normalized=name.lower(),
            pack_size=pack,
            canonical_id=m.id,
        )
        db.add(p)
        db.flush()
        db.add(
            storage.PriceSnapshot(run_id=run.id, product_id=p.id, price=price, captured_at=utcnow())
        )
    db.commit()
    return m


def test_comparison_per_unit_normalizes_pack_vs_single(client, tenant_user, setup_db):
    """Per-unit fix: маска поштучно 0.20 vs пачка N50 за 10.00 → spread должен
    схлопнуться (0.20/шт у обоих), basis='unit'."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    _make_match_with_packs(
        s,
        run,
        canonical="Tibbi maska",
        prods=[
            ("aptekonline", 10.0, "n50", "Tibbi maska N50"),
            ("pharmonline", 0.20, None, "Tibbi maska"),
        ],
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/comparison?min_sites=2")
    row = next(x for x in r.json() if x["name"] == "Tibbi maska")
    assert row["spread_basis"] == "unit"
    assert row["spread_pct"] < 5.0  # 0.20 vs 0.20 → ~0
    # unit_price проставлен
    assert abs(row["prices"]["aptekonline"]["unit_price"] - 0.20) < 0.01
    assert row["prices"]["aptekonline"]["pack_count"] == 50


def test_comparison_per_unit_avoids_false_spread(client, tenant_user, setup_db):
    """Критический guard (#4): оба сайта — пачка 50, но у одного count не
    распарсился (default 1). Нормализация РАЗДУЛА бы spread (10/50 vs 10/1) →
    должны остаться на raw (spread ~0), basis='raw'."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    _make_match_with_packs(
        s,
        run,
        canonical="Bint",
        prods=[
            ("aptekonline", 10.0, "n50", "Bint elastik N50"),
            ("pharmonline", 10.5, None, "Bint elastik"),  # тоже пачка, count не виден
        ],
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/comparison?min_sites=2")
    row = next(x for x in r.json() if x["name"] == "Bint")
    # raw spread (10 vs 10.5) ~5%; unit нормализация дала бы 0.2 vs 10.5 = 98% →
    # отвергается, остаёмся на raw
    assert row["spread_basis"] == "raw"
    assert row["spread_pct"] < 10.0


def test_comparison_same_pack_stays_raw(client, tenant_user, setup_db):
    """Одинаковая фасовка → basis='raw', реальный spread сохраняется."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    _make_match_with_packs(
        s,
        run,
        canonical="Aspirin",
        prods=[
            ("aptekonline", 10.0, "n20", "Aspirin N20"),
            ("pharmonline", 15.0, "n20", "Aspirin N20"),
        ],
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/comparison?min_sites=2")
    row = next(x for x in r.json() if x["name"] == "Aspirin")
    assert row["spread_basis"] == "raw"
    assert abs(row["spread_pct"] - 33.3) < 1.0  # (15-10)/15 = 33%


def test_comparison_excludes_low_confidence_matches(client, tenant_user, setup_db):
    """2026-05-29: низко-достоверные fuzzy-матчи (Bio Kolik капли ↔ Bio sprey,
    confidence 0.68 — разные товары) исключаются дефолтным min_confidence=0.70,
    чтобы не давать ложный spread в топе."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    # low-conf wrong match
    bad = storage.Match(tenant_id=1, canonical_name="Bio thing", confidence=0.68)
    s.add(bad)
    s.flush()
    for site, price, nm in [
        ("aptekonline", 4.45, "Bio Kolik N20"),
        ("pharmonline", 14.6, "Bio sprey 30ml"),
    ]:
        p = storage.Product(
            tenant_id=1,
            site=site,
            external_id=f"{site}-bio",
            url=f"http://{site}/bio",
            name=nm,
            name_normalized=nm.lower(),
            canonical_id=bad.id,
        )
        s.add(p)
        s.flush()
        s.add(
            storage.PriceSnapshot(run_id=run.id, product_id=p.id, price=price, captured_at=utcnow())
        )
    # high-conf good match (control)
    _make_match_with_packs(
        s,
        run,
        canonical="GoodDrug",
        prods=[
            ("aptekonline", 10.0, "n20", "GoodDrug N20"),
            ("pharmonline", 11.0, "n20", "GoodDrug N20"),
        ],
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    names = [r["name"] for r in client.get("/api/v1/dash/comparison?min_sites=2").json()]
    assert "Bio thing" not in names  # 0.68 < 0.70 floor → excluded
    assert "GoodDrug" in names
    # явный low floor показывает их обратно
    resp = client.get("/api/v1/dash/comparison?min_sites=2&min_confidence=0")
    assert "Bio thing" in [r["name"] for r in resp.json()]


def test_comparison_drops_extreme_price_outlier(client, tenant_user, setup_db):
    """Thiogamma-класс: одинаковая фасовка (обе N10), но одна цена 10× битая
    (aloe 8.90 vs pharm 89.00 — per-unit scrape error). Битая цена дропается
    (>8.3× от медианы), строка уходит из сравнения (остаётся 1 сайт)."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    _make_match_with_packs(
        s,
        run,
        canonical="Thiogamma turbo",
        prods=[
            ("aloe", 8.9, "n10", "Thiogamma Turbo 50 ml, 10 əd"),
            ("pharmonline", 89.0, "n10", "Thiogamma turbo 50 ml № 10"),
        ],
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    rows = client.get("/api/v1/dash/comparison?min_sites=2").json()
    # 8.90 = 10% of 89 → dropped (< 12% threshold) → only 1 site → row excluded
    assert not any(r["name"] == "Thiogamma turbo" for r in rows)


def test_comparison_drops_high_outlier_3site_wrong_match(client, tenant_user, setup_db):
    """3-сайтовый wrong-match: 2 сайта сходятся (0.30/0.30), 1 — высокий выброс
    8.02 ('Aspirin C' ошибочно сматчен с 'Aspirin'). Высокая цена >8.3× медианы
    дропается → строка показывает консенсус 0.30/0.30 без ложного 96% spread.
    (Низкий-выброс фильтр такое не ловил — это симметричный high-outlier guard.)
    """
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    _make_match_with_packs(
        s,
        run,
        canonical="Aspirin",
        prods=[
            ("aptekonline", 0.30, "n10", "Aspirin 500 mq N10"),
            ("aloe", 0.30, "n10", "Aspirin 500 mq 10 əd"),
            ("pharmonline", 8.02, "n10", "Aspirin C N10 (effervescent)"),
        ],
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    rows = client.get("/api/v1/dash/comparison?min_sites=2").json()
    row = next(x for x in rows if x["name"] == "Aspirin")
    # высокий выброс pharm 8.02 убран; остаётся консенсус aptek+aloe
    assert "pharmonline" not in row["prices"]
    assert set(row["prices"].keys()) == {"aptekonline", "aloe"}
    assert row["spread_pct"] < 5.0  # 0.30 vs 0.30 → ~0, не ложные 96%


def test_comparison_2site_high_ratio_not_dropped(client, tenant_user, setup_db):
    """Регрессия симметричного фильтра: при 2 сайтах median==max, высокий порог
    НЕ срабатывает. Реальный 4× undercut (4.40 vs 1.10, оба n10) остаётся виден,
    обе цены сохраняются, spread ~75%."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    _make_match_with_packs(
        s,
        run,
        canonical="Doksisiklin",
        prods=[
            ("pharmonline", 4.40, "n10", "Doksisiklin 100 mq N10"),
            ("aptekonline", 1.10, "n10", "Doksisiklin 100 mq N10"),
        ],
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    rows = client.get("/api/v1/dash/comparison?min_sites=2").json()
    row = next(x for x in rows if x["name"] == "Doksisiklin")
    assert set(row["prices"].keys()) == {"pharmonline", "aptekonline"}
    assert abs(row["spread_pct"] - 75.0) < 1.0


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


# ─── Comparison freshness (stale-цены, 2026-05-29) ───────────────────────────


def _make_match_with_freshness(db, run, *, canonical, prods, tenant_id=1):
    """Match + products с заданными (site, price, last_seen_at, captured_at).

    Фасовка одинаковая (n20) у всех → basis остаётся raw, изолируем freshness.
    """
    m = storage.Match(tenant_id=tenant_id, canonical_name=canonical, confidence=1.0)
    db.add(m)
    db.flush()
    for site, price, last_seen, captured in prods:
        p = storage.Product(
            tenant_id=tenant_id,
            site=site,
            external_id=f"{site}-{canonical}",
            url=f"https://{site}/{canonical}",
            name=f"{canonical} N20",
            name_normalized=canonical.lower(),
            pack_size="n20",
            canonical_id=m.id,
            last_seen_at=last_seen,
        )
        db.add(p)
        db.flush()
        db.add(
            storage.PriceSnapshot(run_id=run.id, product_id=p.id, price=price, captured_at=captured)
        )
    db.commit()
    return m


def test_price_age_days_tz_defensive():
    """_price_age_days: naive/aware/None/future (Codex HIGH fix 2026-05-29).

    tz-aware вход НЕ должен падать (naive-aware вычитание = TypeError); future
    timestamp (clock skew Mac↔prod) клампится в 0, age не уходит в минус.
    """
    now = utcnow()  # naive
    assert api_module._price_age_days(now, now - timedelta(days=5)) == 5
    # aware вход нормализуется, не падает
    aware = (now - timedelta(days=10)).replace(tzinfo=timezone.utc)
    assert api_module._price_age_days(now, aware) == 10
    # None → None (last_seen_at=NULL)
    assert api_module._price_age_days(now, None) is None
    # future → clamp 0 (не отрицательное)
    assert api_module._price_age_days(now, now + timedelta(days=3)) == 0


def test_comparison_stale_price_excluded_from_spread(client, tenant_user, setup_db):
    """Stale-цена (last_seen > 14д) показывается с stale=True + age_days, но НЕ
    участвует в spread. aptek свежий 10.0, pharm устаревший 100.0 (30д): без
    freshness был бы ложный spread 90%; теперь 1 свежая цена → spread=None,
    строка видна с обеими ценами (ничего не теряется)."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    fresh = utcnow()
    old = utcnow() - timedelta(days=30)
    _make_match_with_freshness(
        s,
        run,
        canonical="Staletest",
        prods=[
            ("aptekonline", 10.0, fresh, fresh),
            ("pharmonline", 100.0, old, old),
        ],
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    rows = client.get("/api/v1/dash/comparison?min_sites=2").json()
    row = next(x for x in rows if x["name"] == "Staletest")
    # обе цены показаны
    assert set(row["prices"].keys()) == {"aptekonline", "pharmonline"}
    assert row["sites_with_price"] == 2
    # pharm помечен stale + возраст ~30д; aptek свежий
    assert row["prices"]["pharmonline"]["stale"] is True
    assert row["prices"]["pharmonline"]["age_days"] >= 28
    assert row["prices"]["aptekonline"]["stale"] is False
    # spread НЕ посчитан (1 свежая цена) — устаревшая 100.0 не раздувает spread
    assert row["spread_pct"] is None
    # <2 свежих → min/max/cheapest тоже None (единственную свежую не подсвечиваем
    # как «дешёвую» — сравнивать не с чем). Codex MED fix 2026-05-29.
    assert row["min_price"] is None
    assert row["max_price"] is None
    assert row["cheapest_site"] is None


def test_comparison_stable_price_not_stale(client, tenant_user, setup_db):
    """diff-only корректность: товар со СТАБИЛЬНОЙ ценой имеет старый
    captured_at (снапшот пишется только при смене цены), но СВЕЖИЙ last_seen_at
    (видели в каждом прогоне). Свежесть считается по last_seen_at → НЕ stale,
    spread считается нормально. Если бы считали по captured_at — ложно скрыли
    бы половину каталога."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    fresh = utcnow()
    old_snapshot = utcnow() - timedelta(days=40)  # цена не менялась 40 дней
    _make_match_with_freshness(
        s,
        run,
        canonical="Stableprice",
        prods=[
            # last_seen свежий (видели сегодня), но captured_at старый
            ("aptekonline", 10.0, fresh, old_snapshot),
            ("pharmonline", 15.0, fresh, old_snapshot),
        ],
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    rows = client.get("/api/v1/dash/comparison?min_sites=2").json()
    row = next(x for x in rows if x["name"] == "Stableprice")
    assert row["prices"]["aptekonline"]["stale"] is False
    assert row["prices"]["pharmonline"]["stale"] is False
    # spread считается (обе свежие): (15-10)/15 = 33%
    assert abs(row["spread_pct"] - 33.3) < 1.0


def test_comparison_all_stale_still_shown_no_spread(client, tenant_user, setup_db):
    """Обе цены stale → строка всё равно показывается (клиент видит обе старые
    цены с бейджами), но spread=None — нет свежей базы для сравнения."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    old = utcnow() - timedelta(days=20)
    _make_match_with_freshness(
        s,
        run,
        canonical="Allstale",
        prods=[
            ("aptekonline", 10.0, old, old),
            ("pharmonline", 30.0, old, old),
        ],
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    rows = client.get("/api/v1/dash/comparison?min_sites=2").json()
    row = next(x for x in rows if x["name"] == "Allstale")
    assert row["sites_with_price"] == 2
    assert row["prices"]["aptekonline"]["stale"] is True
    assert row["prices"]["pharmonline"]["stale"] is True
    assert row["spread_pct"] is None


def test_comparison_fresh_outlier_still_dropped_with_stale_present(client, tenant_user, setup_db):
    """Регрессия: outlier-фильтр (parse-ошибка) работает по свежим даже когда в
    кластере есть stale-цена. 3 сайта: 2 свежих (10 + битая 0.5), 1 stale.
    Битая свежая 0.5 (<12% медианы) дропается; stale остаётся для показа."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    fresh = utcnow()
    old = utcnow() - timedelta(days=25)
    _make_match_with_freshness(
        s,
        run,
        canonical="Outlierstale",
        prods=[
            ("aptekonline", 10.0, fresh, fresh),
            ("pharmonline", 0.5, fresh, fresh),  # свежая parse-ошибка → дроп
            ("aloe", 11.0, old, old),  # stale → показываем, но не в spread
        ],
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    rows = client.get("/api/v1/dash/comparison?min_sites=2").json()
    row = next(x for x in rows if x["name"] == "Outlierstale")
    # битая свежая 0.5 убрана из payload
    assert "pharmonline" not in row["prices"]
    # aptek (свежий) + aloe (stale) остаются
    assert row["prices"]["aptekonline"]["stale"] is False
    assert row["prices"]["aloe"]["stale"] is True
    # spread None: после дропа осталась 1 свежая (aptek), aloe stale не в расчёте
    assert row["spread_pct"] is None


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


def test_dash_watchlist_categories_crud(client, auth_cookie, setup_db):
    cat = storage.Category(
        key="vit",
        label_ru="Витамины",
        label_az="Vitaminlər",
        pharmonline_slug="vitamins",
        aloe_slug="dermanlar",
        is_active=True,
    )
    setup_db.add(cat)
    setup_db.commit()
    setup_db.refresh(cat)

    created = client.post(
        "/api/v1/dash/watchlist/categories",
        json={"category_id": cat.id, "notes": "priority"},
    )
    assert created.status_code == 201, created.text
    tracked_id = created.json()["id"]

    listed = client.get("/api/v1/dash/watchlist/categories")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body == [
        {
            "id": tracked_id,
            "category_id": cat.id,
            "key": "vit",
            "label_ru": "Витамины",
            "label_az": "Vitaminlər",
            "pharmonline_slug": "vitamins",
            "aptekonline_slug": None,
            "aloe_slug": "dermanlar",
            "notes": "priority",
            "is_active": True,
            "product_count": 0,
            "matched_product_count": 0,
            "comparison_count": 0,
        }
    ]

    deleted = client.delete(f"/api/v1/dash/watchlist/categories/{tracked_id}")
    assert deleted.status_code == 204
    assert client.get("/api/v1/dash/watchlist/categories").json() == []


def test_dash_watchlist_categories_include_comparison_counts(client, auth_cookie, setup_db):
    run = storage.Run(status="ok", tenant_id=1)
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)
    cat = storage.Category(
        key="vit",
        label_ru="Витамины",
        label_az="Vitaminlər",
        pharmonline_slug="vitamins",
        aloe_slug="aloe-vitamins",
        is_active=True,
    )
    setup_db.add(cat)
    setup_db.commit()
    setup_db.refresh(cat)
    _make_match_with_prices(
        setup_db,
        run,
        canonical="v1",
        prices={"pharmonline": 10.0, "aloe": 8.0},
        category="vitamins",
    )

    created = client.post("/api/v1/dash/watchlist/categories", json={"category_id": cat.id})
    assert created.status_code == 201, created.text

    item = client.get("/api/v1/dash/watchlist/categories").json()[0]
    assert item["product_count"] == 2
    assert item["matched_product_count"] == 2
    assert item["comparison_count"] == 1


def test_dash_watchlist_categories_requires_auth(client, setup_db):
    cat = storage.Category(key="x", label_ru="X", is_active=True)
    setup_db.add(cat)
    setup_db.commit()
    r = client.post("/api/v1/dash/watchlist/categories", json={"category_id": cat.id})
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


# ── Manual relink by URL (2026-05-29) ────────────────────────────────────────


def test_match_relink_swaps_by_url(client, tenant_user, setup_db):
    """POST /relink: вставить URL правильного товара → swap товара сайта."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    m = _make_match_with_products(s, confidence=0.8, canonical="Relinkable", products_per_site=2)
    old = next(p for p in m.products if p.site == "aptekonline")
    new = storage.Product(
        tenant_id=1,
        site="aptekonline",
        external_id="correct-999",
        url="https://www.aptekonline.az/product/correct-999",
        name="Correct Item",
        name_normalized="correct item",
    )
    s.add(new)
    s.commit()
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    # URL с query (?lng=en) — должен резолвиться по external_id из последнего сегмента
    r = client.post(
        f"/api/v1/dash/matches/{m.id}/relink",
        json={
            "site": "aptekonline",
            "url": "https://www.aptekonline.az/product/correct-999?lng=en",
        },
    )
    assert r.status_code == 200, r.text
    s.refresh(new)
    s.refresh(old)
    s.refresh(m)
    assert new.canonical_id == m.id  # новый привязан
    assert old.canonical_id is None  # старый отвязан
    assert m.is_manual is True  # rematch не тронет


def test_match_relink_adds_missing_site(client, tenant_user, setup_db):
    """Если сайта в кластере не было — товар просто добавляется."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    m = _make_match_with_products(s, confidence=0.8, canonical="TwoSite", products_per_site=2)
    # кластер pharmonline+aptekonline; добавим aloe
    new = storage.Product(
        tenant_id=1,
        site="aloe",
        external_id="aloe-add-1",
        url="https://aloe.az/aloe-add-1/",
        name="Aloe Add",
        name_normalized="aloe add",
    )
    s.add(new)
    s.commit()
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.post(
        f"/api/v1/dash/matches/{m.id}/relink",
        json={"site": "aloe", "url": "https://aloe.az/aloe-add-1/"},
    )
    assert r.status_code == 200, r.text
    s.refresh(new)
    assert new.canonical_id == m.id


def test_match_relink_404_unknown_url(client, tenant_user, setup_db):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    m = _make_match_with_products(s, confidence=0.8, canonical="NoTarget", products_per_site=2)
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.post(
        f"/api/v1/dash/matches/{m.id}/relink",
        json={
            "site": "aptekonline",
            "url": "https://www.aptekonline.az/product/does-not-exist-xyz",
        },
    )
    assert r.status_code == 404


def test_match_relink_409_already_matched(client, tenant_user, setup_db):
    """Товар уже в другом кластере → 409 (сначала отклони там)."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    m1 = _make_match_with_products(s, confidence=0.8, canonical="ClusterA", products_per_site=2)
    m2 = _make_match_with_products(s, confidence=0.8, canonical="ClusterB", products_per_site=2)
    other = next(p for p in m2.products if p.site == "aptekonline")
    other.external_id = "busy-777"
    other.url = "https://www.aptekonline.az/product/busy-777"
    s.commit()
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.post(
        f"/api/v1/dash/matches/{m1.id}/relink",
        json={"site": "aptekonline", "url": "https://www.aptekonline.az/product/busy-777"},
    )
    assert r.status_code == 409


def test_match_relink_requires_auth(client, setup_db):
    r = client.post(
        "/api/v1/dash/matches/1/relink",
        json={"site": "aptekonline", "url": "https://x/y"},
    )
    assert r.status_code == 401


# ─── /dash/category-comparison + /comparison?category= ───────────────────────


def test_category_comparison_without_cookie_401(client):
    r = client.get("/api/v1/dash/category-comparison")
    assert r.status_code == 401


def test_category_comparison_groups_and_indexes(client, tenant_user, setup_db):
    """Группировка по категории клиента: per-site средние + index + label-fallback."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    _make_match_with_prices(
        s, run, canonical="v1", prices={"pharmonline": 10.0, "aloe": 8.0}, category="vitamins"
    )
    _make_match_with_prices(
        s, run, canonical="p1", prices={"pharmonline": 20.0, "aloe": 20.0}, category="pain"
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/category-comparison")
    assert r.status_code == 200, r.text
    rows = {row["category"]: row for row in r.json()}
    assert set(rows) == {"vitamins", "pain"}
    assert rows["vitamins"]["index"] == 125.0  # клиент 10 / конкурент 8
    assert rows["vitamins"]["per_site_avg"] == {"aloe": 8.0}
    assert rows["vitamins"]["pricier_count"] == 1
    assert rows["pain"]["index"] == 100.0
    # Нет записи Category → label graceful fallback на сырой slug.
    assert rows["vitamins"]["label"] == "vitamins"


def test_category_comparison_tenant_isolation(client, tenant_user, setup_db):
    """Категории чужого тенанта не видны."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    _make_match_with_prices(
        s,
        run,
        canonical="v1",
        prices={"pharmonline": 10.0, "aloe": 8.0},
        category="vitamins",
        tenant_id=1,
    )
    _make_match_with_prices(
        s,
        run,
        canonical="x1",
        prices={"pharmonline": 10.0, "aloe": 8.0},
        category="secret",
        tenant_id=2,
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/category-comparison")
    assert r.status_code == 200, r.text
    cats = {row["category"] for row in r.json()}
    assert cats == {"vitamins"}  # тенант 2's "secret" исключён


def test_comparison_category_filter(client, tenant_user, setup_db):
    """drill-down: /comparison?category=X возвращает только матчи этой категории."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    _make_match_with_prices(
        s, run, canonical="v1", prices={"pharmonline": 10.0, "aloe": 8.0}, category="vitamins"
    )
    _make_match_with_prices(
        s, run, canonical="p1", prices={"pharmonline": 20.0, "aloe": 25.0}, category="pain"
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/comparison?category=vitamins")
    assert r.status_code == 200, r.text
    assert [row["name"] for row in r.json()] == ["v1"]
    # Без фильтра — обе категории.
    r2 = client.get("/api/v1/dash/comparison")
    assert {row["name"] for row in r2.json()} == {"v1", "p1"}


def test_comparison_excludes_dead_aptekonline(client, tenant_user, setup_db):
    """Товар с url_dead_at (404) скрыт; матч остаётся через pharmonline+aloe."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    m = _make_match_with_prices(
        s,
        run,
        canonical="phantom",
        prices={"pharmonline": 10.0, "aptekonline": 8.0, "aloe": 9.0},
    )
    next(p for p in m.products if p.site == "aptekonline").url_dead_at = utcnow()
    s.commit()
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/comparison?min_sites=2")
    assert r.status_code == 200, r.text
    rows = {row["name"]: row for row in r.json()}
    assert "phantom" in rows  # ещё виден (pharmonline + aloe)
    assert set(rows["phantom"]["prices"]) == {"pharmonline", "aloe"}  # мёртвый aptekonline скрыт


def test_comparison_hides_match_when_only_dead_competitor(client, tenant_user, setup_db):
    """Единственный конкурент мёртв → матч падает ниже min_sites и не виден."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    m = _make_match_with_prices(
        s, run, canonical="solodead", prices={"pharmonline": 10.0, "aptekonline": 8.0}
    )
    next(p for p in m.products if p.site == "aptekonline").url_dead_at = utcnow()
    s.commit()
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/comparison?min_sites=2")
    assert r.status_code == 200, r.text
    assert "solodead" not in [row["name"] for row in r.json()]


def test_price_index_endpoint_200(client, tenant_user, setup_db):
    """Регресс (аудит): /price-index 500'ил TypeError'ом (client_site= в сломанной
    сигнатуре), а юнит-тест звал функцию напрямую → suite был зелёный. Endpoint-smoke
    ловит весь wire-контракт: assert не-500."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    _make_match_with_prices(
        s, run, canonical="v1", prices={"pharmonline": 10.0, "aloe": 8.0}, category="vitamins"
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/price-index")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def test_comparison_drops_match_when_client_dead(client, tenant_user, setup_db):
    """Мёртв сам клиент (pharmonline 404) → матч дропается целиком, а не competitor-only."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    m = _make_match_with_prices(
        s,
        run,
        canonical="clientdead",
        prices={"pharmonline": 10.0, "aloe": 8.0, "aptekonline": 9.0},
    )
    next(p for p in m.products if p.site == "pharmonline").url_dead_at = utcnow()
    s.commit()
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get("/api/v1/dash/comparison?min_sites=2")
    assert r.status_code == 200, r.text
    assert "clientdead" not in [row["name"] for row in r.json()]


# ─── /matches/{id}/candidate-analogs — guard-ranked recall suggestions ────────


def _add_unmatched(db, *, site, name, run, price=None):
    from src.normalize import normalize_name

    p = storage.Product(
        tenant_id=1,
        site=site,
        external_id=f"{site}-{name}",
        url=f"https://{site}.example/{name}",
        name=name,
        name_normalized=normalize_name(name),
    )
    db.add(p)
    db.flush()
    if price is not None:
        db.add(
            storage.PriceSnapshot(run_id=run.id, product_id=p.id, price=price, captured_at=utcnow())
        )
    db.commit()
    return p


def test_match_create_with_products_sets_manual_strategy(client, tenant_user, setup_db):
    """Regression: create-with-products must not 500 when setting match_strategy."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    from src.normalize import normalize_name

    s = setup_db
    p1 = storage.Product(
        tenant_id=1,
        site="aptekonline",
        external_id="aptek-zanzarella",
        url="https://aptek.example/zanzarella",
        name="Zanzarella Ambiente 170 q",
        name_normalized=normalize_name("Zanzarella Ambiente 170 q"),
        brand="Zanzarella",
    )
    p2 = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="pharm-zanzarella",
        url="https://pharm.example/zanzarella",
        name="Zanzarella teravetlendirici 170 q",
        name_normalized=normalize_name("Zanzarella teravetlendirici 170 q"),
        brand="Zanzarella",
    )
    s.add_all([p1, p2])
    s.commit()

    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.post(
        "/api/v1/dash/matches/create-with-products",
        json={"product_ids": [p1.id, p2.id]},
    )

    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["match_strategy"] == "manual"
    assert payload["is_manual"] is True
    s.refresh(p1)
    s.refresh(p2)
    assert p1.canonical_id == payload["match_id"]
    assert p2.canonical_id == payload["match_id"]


def test_candidate_analogs_ranks_guard_passing_and_flags_auto_safe(client, tenant_user, setup_db):
    """Endpoint suggests the missing-site twin, flags ultra-equal as auto_safe,
    and filters out spec-conflicting (different pack) + dissimilar candidates."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    from src.normalize import normalize_name

    m = storage.Match(tenant_id=1, canonical_name="Tussor 100 ml", confidence=1.0)
    s.add(m)
    s.flush()
    for site, nm in [
        ("pharmonline", "Tussor 100 ml (Sirop)"),
        ("aptekonline", "Tussor şərbət 100 ml"),
    ]:
        s.add(
            storage.Product(
                tenant_id=1,
                site=site,
                external_id=f"{site}-tussor",
                url=f"https://{site}.example/tussor",
                name=nm,
                name_normalized=normalize_name(nm),
                canonical_id=m.id,
            )
        )
    s.flush()
    # ultra-equal twin: same tokens/pack AND form (şərbət≡sirop → syrup)
    twin = _add_unmatched(s, site="aloe", name="Tussor şərbət 100 ml", run=run, price=7.5)
    _add_unmatched(s, site="aloe", name="Tussor 200 ml", run=run)  # different pack → conflict
    _add_unmatched(s, site="aloe", name="Aspirin 500 mq N20", run=run)  # unrelated → low fuzz

    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get(f"/api/v1/dash/matches/{m.id}/candidate-analogs?site=aloe")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    ids = {it["product_id"] for it in items}
    assert twin.id in ids
    twin_row = next(it for it in items if it["product_id"] == twin.id)
    assert twin_row["auto_safe"] is True
    assert twin_row["price"] == 7.5
    names = {it["name"] for it in items}
    assert "Tussor 200 ml" not in names
    assert "Aspirin 500 mq N20" not in names


def test_candidate_analogs_empty_when_site_already_present(client, tenant_user, setup_db):
    """If the cluster already has the requested site, nothing to suggest."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    m = _make_match_with_prices(
        s, run, canonical="Tussor", prices={"pharmonline": 10.0, "aloe": 8.0}
    )
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get(f"/api/v1/dash/matches/{m.id}/candidate-analogs?site=aloe")
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


def test_match_alternatives_ranks_similar_unmatched(client, tenant_user, setup_db):
    """alternatives endpoint (the «Неправильное сравнение?» panel) returns
    similar unmatched products on the requested site, ranked by similarity."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    m = _make_match_with_prices(
        s, run, canonical="Çaytikanı yağı", prices={"pharmonline": 10.6, "aptekonline": 6.7}
    )
    # unmatched aptekonline candidates the operator could swap in
    good = _add_unmatched(s, site="aptekonline", name="Çaytikanı yağı 100 ml", run=run, price=7.0)
    _add_unmatched(s, site="aptekonline", name="Paracetamol 500 mq N20", run=run)  # dissimilar

    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    r = client.get(f"/api/v1/dash/matches/{m.id}/alternatives?site=aptekonline")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "expected at least one alternative"
    # the similar oil ranks first; dissimilar paracetamol is far down or absent
    assert items[0]["product_id"] == good.id
    assert items[0]["score"] >= items[-1]["score"]


# ─── Recipients / user management (admin) ────────────────────────────────────


def _verify_login(client, session, email):
    """Magic-link login as `email`; returns the verify response."""
    token = tenants.issue_magic_token(session, email)
    return client.get(f"/auth/verify?token={token}")


def test_is_last_active_admin_helper(setup_db):
    """Helper: последний активный админ → True; есть второй активный → False."""
    s = setup_db
    t = tenants.get_or_create_default(s)
    a1 = storage.TenantUser(
        tenant_id=t.id, email="a1@x.az", role="admin", is_active=True, created_at=utcnow()
    )
    s.add(a1)
    s.commit()
    s.refresh(a1)
    assert api_module._is_last_active_admin(s, t.id, exclude_id=a1.id) is True
    a2 = storage.TenantUser(
        tenant_id=t.id, email="a2@x.az", role="admin", is_active=True, created_at=utcnow()
    )
    s.add(a2)
    s.commit()
    assert api_module._is_last_active_admin(s, t.id, exclude_id=a1.id) is False
    a2.is_active = False
    s.commit()
    assert api_module._is_last_active_admin(s, t.id, exclude_id=a1.id) is True


def test_recipients_list_requires_admin(client, setup_db):
    """viewer не может смотреть список получателей (403)."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("JWT unavailable")
    s = setup_db
    t = tenants.get_or_create_default(s)
    s.add(
        storage.TenantUser(
            tenant_id=t.id, email="v@x.az", role="viewer", is_active=True, created_at=utcnow()
        )
    )
    s.commit()
    assert _verify_login(client, s, "v@x.az").status_code == 200
    assert client.get("/api/v1/dash/recipients").status_code == 403


def test_recipient_create_and_list(client, auth_cookie, setup_db):
    """admin добавляет получателя — появляется в списке с нужной ролью."""
    r = client.post(
        "/api/v1/dash/recipients",
        json={"email": "New@X.az", "name": "New", "role": "viewer", "daily_digest": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "new@x.az"  # нормализован в lowercase
    assert r.json()["role"] == "viewer"
    lst = client.get("/api/v1/dash/recipients").json()
    assert any(u["email"] == "new@x.az" for u in lst)


def test_cannot_delete_self(client, auth_cookie, tenant_user):
    """admin не может удалить себя (self-lockout)."""
    assert client.delete(f"/api/v1/dash/recipients/{tenant_user.id}").status_code == 400


def test_cannot_demote_self_to_viewer(client, auth_cookie, tenant_user):
    """admin не может понизить себя до viewer."""
    r = client.patch(f"/api/v1/dash/recipients/{tenant_user.id}", json={"role": "viewer"})
    assert r.status_code == 400, r.text


def test_can_demote_other_admin_when_multiple(client, auth_cookie, tenant_user, setup_db):
    """С 2+ активными админами понижение ДРУГОГО админа разрешено — last-admin гард
    НЕ переблокирует легитимное понижение (защита от регрессии over-block)."""
    s = setup_db
    t = tenants.get_or_create_default(s)
    other = storage.TenantUser(
        tenant_id=t.id, email="admin2@x.az", role="admin", is_active=True, created_at=utcnow()
    )
    s.add(other)
    s.commit()
    s.refresh(other)
    r = client.patch(f"/api/v1/dash/recipients/{other.id}", json={"role": "viewer"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "viewer"


def test_recipient_create_issues_login_token(client, auth_cookie, setup_db):
    """Создание юзера авто-выпускает magic-token (инвайт-письмо). Без SMTP в
    тест-окружении письмо — no-op, но токен должен быть выпущен в БД."""
    r = client.post(
        "/api/v1/dash/recipients",
        json={"email": "invite@x.az", "name": "Inv", "role": "viewer"},
    )
    assert r.status_code == 200, r.text
    u = setup_db.scalar(select(storage.TenantUser).where(storage.TenantUser.email == "invite@x.az"))
    assert u is not None
    assert u.magic_token is not None  # инвайт выпустил токен на вход
    assert u.magic_token_expires_at is not None


def test_send_login_link_requires_admin(client, setup_db):
    """viewer не может слать login-link (403)."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("JWT unavailable")
    s = setup_db
    t = tenants.get_or_create_default(s)
    s.add(
        storage.TenantUser(
            tenant_id=t.id, email="vv@x.az", role="viewer", is_active=True, created_at=utcnow()
        )
    )
    s.commit()
    assert _verify_login(client, s, "vv@x.az").status_code == 200
    target = s.scalar(select(storage.TenantUser).where(storage.TenantUser.email == "vv@x.az"))
    assert client.post(f"/api/v1/dash/recipients/{target.id}/send-login-link").status_code == 403


def test_send_login_link_ok(client, auth_cookie, setup_db):
    """admin шлёт login-link активному юзеру → 200 + выпущен свежий токен."""
    s = setup_db
    t = tenants.get_or_create_default(s)
    u = storage.TenantUser(
        tenant_id=t.id, email="reuse@x.az", role="viewer", is_active=True, created_at=utcnow()
    )
    s.add(u)
    s.commit()
    s.refresh(u)
    r = client.post(f"/api/v1/dash/recipients/{u.id}/send-login-link")
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "reuse@x.az"
    s.refresh(u)
    assert u.magic_token is not None


def test_send_login_link_404(client, auth_cookie):
    """Несуществующий получатель → 404."""
    assert client.post("/api/v1/dash/recipients/999999/send-login-link").status_code == 404


def test_send_login_link_inactive_400(client, auth_cookie, setup_db):
    """Деактивированному юзеру ссылку не шлём (400)."""
    s = setup_db
    t = tenants.get_or_create_default(s)
    u = storage.TenantUser(
        tenant_id=t.id, email="off@x.az", role="viewer", is_active=False, created_at=utcnow()
    )
    s.add(u)
    s.commit()
    s.refresh(u)
    assert client.post(f"/api/v1/dash/recipients/{u.id}/send-login-link").status_code == 400


# ─── Set-password flow (invite link → юзер задаёт свой пароль) ───────────────


def _make_user(session, email, *, role="viewer", password=None, active=True):
    t = tenants.get_or_create_default(session)
    u = storage.TenantUser(
        tenant_id=t.id, email=email, role=role, is_active=active, created_at=utcnow()
    )
    if password:
        u.password_hash = api_module._hash_bcrypt(password)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_set_password_valid_token_sets_hash_and_cookie(client, setup_db):
    """Валидный magic-token → password_hash сохранён, JWT cookie выставлен."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("JWT unavailable")
    s = setup_db
    u = _make_user(s, "newbie@x.az")
    token = tenants.issue_magic_token(s, "newbie@x.az")
    r = client.post("/auth/set-password", json={"token": token, "new_password": "mypass123"})
    assert r.status_code == 200, r.text
    assert api_module.COOKIE_NAME in r.cookies
    s.refresh(u)
    assert u.password_hash
    assert api_module._verify_bcrypt("mypass123", u.password_hash)
    assert u.magic_token is None  # одноразовый — погашен


def test_set_password_invalid_token_401(client, setup_db):
    r = client.post(
        "/auth/set-password", json={"token": "bogus-token", "new_password": "mypass123"}
    )
    assert r.status_code == 401


def test_set_password_short_password_422(client, setup_db):
    """Короткий пароль отсекается валидатором модели (422)."""
    s = setup_db
    _make_user(s, "shorty@x.az")
    token = tenants.issue_magic_token(s, "shorty@x.az")
    r = client.post("/auth/set-password", json={"token": token, "new_password": "ab"})
    assert r.status_code == 422


def test_set_password_token_single_use(client, setup_db):
    """Повторное использование того же токена → 401 (token погашен)."""
    s = setup_db
    _make_user(s, "once@x.az")
    token = tenants.issue_magic_token(s, "once@x.az")
    assert (
        client.post(
            "/auth/set-password", json={"token": token, "new_password": "mypass123"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/auth/set-password", json={"token": token, "new_password": "other1234"}
        ).status_code
        == 401
    )


def test_login_by_email_success(client, setup_db):
    """Юзер со своим password_hash логинится по email+паролю → JWT того же юзера."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("JWT unavailable")
    s = setup_db
    u = _make_user(s, "emailuser@x.az", password="secret123")
    r = client.post("/auth/login", json={"login": "emailuser@x.az", "password": "secret123"})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == u.id
    assert api_module.COOKIE_NAME in r.cookies


def test_login_by_email_wrong_password_401(client, setup_db):
    s = setup_db
    _make_user(s, "emailuser2@x.az", password="secret123")
    r = client.post("/auth/login", json={"login": "emailuser2@x.az", "password": "WRONG"})
    assert r.status_code == 401


def test_login_by_email_no_password_does_not_use_env_hash(client, setup_db, monkeypatch):
    """SECURITY: именованный юзер БЕЗ своего пароля НЕ логинится общим админ-хешем
    из env (иначе — эскалация: любой вошёл бы под чужим email)."""
    s = setup_db
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", api_module._hash_bcrypt("sharedadmin"))
    _make_user(s, "viewer-nopw@x.az", role="viewer")  # password_hash отсутствует
    r = client.post("/auth/login", json={"login": "viewer-nopw@x.az", "password": "sharedadmin"})
    assert r.status_code == 401


def test_login_admin_bootstrap_env_hash(client, tenant_user, monkeypatch):
    """admin-literal + env ADMIN_PASSWORD_HASH (bootstrap) → 200, логинит первого
    активного админа."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("JWT unavailable")
    monkeypatch.setenv("ADMIN_LOGIN", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", api_module._hash_bcrypt("adminpw"))
    r = client.post("/auth/login", json={"login": "admin", "password": "adminpw"})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == tenant_user.id


def test_send_login_link_targets_set_password_for_new_user(
    client, auth_cookie, setup_db, monkeypatch
):
    """Юзер без пароля → письмо ведёт на /set-password (создание пароля)."""
    captured = {}

    def fake_send_email(*, subject, html_body, to):
        captured["html"] = html_body

    from src import notifier

    monkeypatch.setattr(notifier, "send_email", fake_send_email)
    s = setup_db
    u = _make_user(s, "freshlink@x.az")  # без пароля
    r = client.post(f"/api/v1/dash/recipients/{u.id}/send-login-link")
    assert r.status_code == 200, r.text
    assert "/set-password?token=" in captured["html"]
    assert "/auth/verify?token=" not in captured["html"]


def test_send_login_link_targets_auto_login_for_existing_password(
    client, auth_cookie, setup_db, monkeypatch
):
    """Юзер со своим паролем → письмо — обычный login-link (/auth/verify)."""
    captured = {}

    def fake_send_email(*, subject, html_body, to):
        captured["html"] = html_body

    from src import notifier

    monkeypatch.setattr(notifier, "send_email", fake_send_email)
    s = setup_db
    u = _make_user(s, "haspw@x.az", password="secret123")
    r = client.post(f"/api/v1/dash/recipients/{u.id}/send-login-link")
    assert r.status_code == 200, r.text
    assert "/auth/verify?token=" in captured["html"]
    assert "/set-password?token=" not in captured["html"]


def test_auth_verify_browser_accept_redirects(client, setup_db):
    """Браузерная навигация (Accept: text/html) → 303 redirect + cookie, не JSON."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("JWT unavailable")
    s = setup_db
    _make_user(s, "browser@x.az")
    token = tenants.issue_magic_token(s, "browser@x.az")
    r = client.get(
        f"/auth/verify?token={token}",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/overview"
    assert api_module.COOKIE_NAME in r.cookies


def test_auth_verify_browser_invalid_token_redirects_to_login(client, setup_db):
    """Браузер + битый токен → redirect на /login (не 401-страница)."""
    r = client.get(
        "/auth/verify?token=bogus",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


# ─── Codex-review hardening (takeover guard, inactive token, bootstrap role) ──


def test_set_password_rejected_when_already_set(client, setup_db):
    """SECURITY (Codex MED): set-password нельзя использовать для ПЕРЕЗАПИСИ
    существующего пароля (иначе перехваченная login-ссылка = takeover)."""
    s = setup_db
    _make_user(s, "haspw2@x.az", password="orig12345")
    token = tenants.issue_magic_token(s, "haspw2@x.az")
    r = client.post("/auth/set-password", json={"token": token, "new_password": "new12345"})
    assert r.status_code == 400
    # пароль не изменён
    u = s.scalar(select(storage.TenantUser).where(storage.TenantUser.email == "haspw2@x.az"))
    assert api_module._verify_bcrypt("orig12345", u.password_hash)


def test_verify_magic_token_rejects_inactive_user(setup_db):
    """SECURITY (Codex LOW): токен, выпущенный до деактивации, не валиден после."""
    s = setup_db
    _make_user(s, "willdisable@x.az")
    token = tenants.issue_magic_token(s, "willdisable@x.az")
    assert token is not None
    u = s.scalar(select(storage.TenantUser).where(storage.TenantUser.email == "willdisable@x.az"))
    u.is_active = False
    s.commit()
    assert tenants.verify_magic_token(s, token) is None


def test_login_admin_bootstrap_picks_admin_not_lower_id_viewer(client, setup_db, monkeypatch):
    """Codex LOW: admin-bootstrap резолвит первого активного АДМИНА, даже если
    у viewer'а меньший id."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("JWT unavailable")
    s = setup_db
    viewer = _make_user(s, "lowid-viewer@x.az", role="viewer")  # меньший id
    admin = _make_user(s, "the-admin@x.az", role="admin")  # больший id
    assert viewer.id < admin.id
    monkeypatch.setenv("ADMIN_LOGIN", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", api_module._hash_bcrypt("adminpw"))
    r = client.post("/auth/login", json={"login": "admin", "password": "adminpw"})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == admin.id


# ─── Recipient hard-delete (permanent) ───────────────────────────────────────


def test_recipient_soft_delete_is_default(client, auth_cookie, setup_db):
    """DELETE без ?hard — soft: строка остаётся, is_active=false."""
    s = setup_db
    u = _make_user(s, "softdel@x.az")
    assert client.delete(f"/api/v1/dash/recipients/{u.id}").status_code == 204
    s.expire_all()
    row = s.scalar(select(storage.TenantUser).where(storage.TenantUser.id == u.id))
    assert row is not None  # строка цела
    assert row.is_active is False


def test_recipient_hard_delete_removes_row(client, auth_cookie, setup_db):
    """DELETE ?hard=true — физически удаляет строку."""
    s = setup_db
    uid = _make_user(s, "harddel@x.az").id  # plain int — объект будет удалён
    assert client.delete(f"/api/v1/dash/recipients/{uid}?hard=true").status_code == 204
    s.expire_all()
    assert s.scalar(select(storage.TenantUser).where(storage.TenantUser.id == uid)) is None


def test_recipient_hard_delete_nulls_scrape_request_fk(client, auth_cookie, setup_db):
    """Перед DELETE обнуляется единственный FK (scrape_requests.requested_by_user_id)."""
    s = setup_db
    uid = _make_user(s, "fkuser@x.az").id
    sr = storage.ScrapeRequest(
        tenant_id=1, requested_by_user_id=uid, mode="all", status="ok", requested_at=utcnow()
    )
    s.add(sr)
    s.commit()
    srid = sr.id
    assert client.delete(f"/api/v1/dash/recipients/{uid}?hard=true").status_code == 204
    s.expire_all()
    assert s.scalar(select(storage.TenantUser).where(storage.TenantUser.id == uid)) is None
    sr2 = s.scalar(select(storage.ScrapeRequest).where(storage.ScrapeRequest.id == srid))
    assert sr2 is not None  # сам запрос цел
    assert sr2.requested_by_user_id is None  # указатель обнулён


def test_recipient_hard_delete_self_400(client, auth_cookie, tenant_user):
    """admin не может удалить себя даже hard."""
    assert client.delete(f"/api/v1/dash/recipients/{tenant_user.id}?hard=true").status_code == 400


def test_recipient_hard_delete_requires_admin(client, setup_db):
    """viewer не может hard-delete (403) — admin-гард до ветки hard."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("JWT unavailable")
    s = setup_db
    target_id = _make_user(s, "vtarget@x.az", role="viewer").id
    _make_user(s, "vactor@x.az", role="viewer")
    assert _verify_login(client, s, "vactor@x.az").status_code == 200
    assert client.delete(f"/api/v1/dash/recipients/{target_id}?hard=true").status_code == 403


def test_recreate_email_after_hard_delete(client, auth_cookie, setup_db):
    """После hard-delete email освобождается → создать заново с тем же email можно."""
    s = setup_db
    u = _make_user(s, "recreate@x.az")
    assert client.delete(f"/api/v1/dash/recipients/{u.id}?hard=true").status_code == 204
    r = client.post(
        "/api/v1/dash/recipients",
        json={"email": "recreate@x.az", "name": "Again", "role": "viewer"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "recreate@x.az"


def test_recreate_email_after_soft_delete_still_409(client, auth_cookie, setup_db):
    """После soft-delete строка остаётся → создать тот же email = 409 (поведение
    не изменилось; для пересоздания нужен hard-delete или реактивация)."""
    s = setup_db
    u = _make_user(s, "stillthere@x.az")
    assert client.delete(f"/api/v1/dash/recipients/{u.id}").status_code == 204
    r = client.post(
        "/api/v1/dash/recipients",
        json={"email": "stillthere@x.az", "name": "X", "role": "viewer"},
    )
    assert r.status_code == 409
