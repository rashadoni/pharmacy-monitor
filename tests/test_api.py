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
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
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
    assert body["status"] == "degraded"
    assert body["full_catalog_status"] == "missing"
    assert body["full_catalog_verified"] is False
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


def test_roi_cold_cache_fails_closed_without_inline_compute(setup_db, monkeypatch):
    from src import roi

    calls: dict[str, int] = {}
    monkeypatch.setattr(
        api_module, "_require_financial_policy_ready", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(roi, "get_cached_actions", lambda *args, **kwargs: None)

    def fake_compute(session, client_site="pharmonline", *, tenant_id=1):
        calls["compute_tenant"] = tenant_id
        return []

    def fake_cache(session, client_site, actions, *, tenant_id=1, **kwargs):
        calls["cache_tenant"] = tenant_id

    monkeypatch.setattr(roi, "compute_actions", fake_compute)
    monkeypatch.setattr(roi, "cache_actions", fake_cache)

    import pytest

    with pytest.raises(HTTPException) as exc:
        api_module.dash_roi_actions(
            client_site="pharmonline",
            locale="ru",
            user=SimpleNamespace(tenant_id=2),
            db=setup_db,
        )

    assert exc.value.status_code == 503
    assert calls == {}


def test_health_endpoint_redis_unset_returns_null(client, monkeypatch):
    """No REDIS_URL is non-fatal; missing full-catalog history remains degraded."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["redis_ping_ms"] is None
    assert body["status"] == "degraded"
    assert body["full_catalog_status"] == "missing"


def test_system_status_exposes_queue_proxy_and_real_digest_schedule(
    client, auth_cookie, setup_db, monkeypatch
):
    monkeypatch.setenv("DECODO_USERNAME", "configured-user")
    monkeypatch.setenv("DECODO_PASSWORD", "configured-secret")
    monkeypatch.setenv("DECODO_SITES", "aloe,pharmonline")
    monkeypatch.setenv("DECODO_PORTS", "30001,30002")
    monkeypatch.setenv("DAILY_DIGEST_ENABLED", "0")
    monkeypatch.setenv("WEEKLY_DIGEST_ENABLED", "1")
    monkeypatch.setenv("WEEKLY_DIGEST_SCHEDULE_BAKU", "Monday 10:00")
    older = storage.ScrapeRequest(
        tenant_id=1,
        status="pending",
        mode="all",
        requested_at=utcnow() - timedelta(minutes=5),
    )
    running = storage.ScrapeRequest(
        tenant_id=1,
        status="running",
        mode="category",
    )
    foreign = storage.ScrapeRequest(tenant_id=2, status="pending", mode="all")
    setup_db.add_all([older, running, foreign])
    setup_db.commit()

    response = client.get("/api/v1/dash/system-status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queue"]["pending"] == 1
    assert body["queue"]["running"] == 1
    assert body["queue"]["oldest_pending_at"] is not None
    assert body["proxy"] == {
        "provider": "decodo",
        "configured": True,
        "sites": ["aloe", "pharmonline"],
        "pool_size": 2,
    }
    assert body["digests"]["daily"]["enabled"] is False
    assert body["digests"]["weekly"] == {
        "enabled": True,
        "schedule_baku": "Monday 10:00",
    }
    serialized = response.text
    assert "configured-user" not in serialized
    assert "configured-secret" not in serialized


def test_system_status_reports_effective_default_decodo_port_range(
    client, auth_cookie, monkeypatch
):
    monkeypatch.setenv("DECODO_USERNAME", "configured-user")
    monkeypatch.setenv("DECODO_PASSWORD", "configured-secret")
    monkeypatch.setenv("DECODO_SITES", "pharmonline")
    monkeypatch.delenv("DECODO_PORTS", raising=False)

    body = client.get("/api/v1/dash/system-status").json()

    assert body["proxy"]["pool_size"] == 10


def test_health_endpoint_degraded_when_latest_run_degraded(client, setup_db):
    setup_db.add(
        storage.Run(
            status="degraded",
            finished_at=utcnow(),
            run_quality={"sites": {"aloe": {"status": "degraded"}}},
        )
    )
    setup_db.commit()
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["last_run_status"] == "degraded"


def test_health_endpoint_does_not_hide_degraded_behind_running_run(client, setup_db):
    setup_db.add_all(
        [
            storage.Run(
                tenant_id=1,
                started_at=utcnow() - timedelta(hours=1),
                finished_at=utcnow() - timedelta(minutes=30),
                status="degraded",
                run_quality={"sites": {"aloe": {"status": "degraded"}}},
            ),
            storage.Run(tenant_id=1, started_at=utcnow(), status="running"),
        ]
    )
    setup_db.commit()

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["last_run_status"] == "degraded"


def test_health_endpoint_ignores_post_processing_run(client, setup_db):
    now = utcnow()
    setup_db.add_all(
        [
            storage.Run(
                tenant_id=1,
                started_at=now - timedelta(hours=1),
                finished_at=now - timedelta(minutes=30),
                status="degraded",
                catalog_scope="full",
                full_catalog_sites="pharmonline,aptekonline,aloe",
                catalog_verified=False,
                run_quality={
                    "baseline_enforced": True,
                    "full_catalog_verified": False,
                    "financially_eligible": False,
                    "sites": {"pharmonline": {"status": "degraded"}},
                },
            ),
            storage.Run(
                tenant_id=1,
                started_at=now,
                finished_at=None,
                status="running",
                catalog_scope="full",
                full_catalog_sites="pharmonline,aptekonline,aloe",
                catalog_verified=True,
                run_quality={
                    "baseline_enforced": True,
                    "full_catalog_verified": True,
                    "financially_eligible": True,
                    "sites": {
                        "pharmonline": {"status": "ok"},
                        "aptekonline": {"status": "ok"},
                        "aloe": {"status": "ok"},
                    },
                },
            ),
        ]
    )
    setup_db.commit()

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    assert body["last_run_status"] == "degraded"
    assert body["full_catalog_status"] == "degraded"
    assert body["full_catalog_verified"] is False


def test_health_endpoint_without_full_catalog_history_is_fail_closed(client, setup_db):
    setup_db.add(
        storage.Run(
            tenant_id=1,
            started_at=utcnow() - timedelta(hours=1),
            finished_at=utcnow(),
            status="ok",
            run_quality={
                "baseline_enforced": False,
                "financially_eligible": False,
                "sites": {"pharmonline": {"status": "ok"}},
            },
        )
    )
    setup_db.commit()

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    assert body["full_catalog_status"] == "missing"
    assert body["full_catalog_run_at"] is None
    assert body["full_catalog_verified"] is False


def test_health_endpoint_orders_terminal_runs_by_completion(client, setup_db):
    now = utcnow()
    setup_db.add_all(
        [
            storage.Run(
                tenant_id=1,
                started_at=now - timedelta(hours=2),
                finished_at=now,
                status="degraded",
                run_quality={"sites": {"pharmonline": {"status": "degraded"}}},
            ),
            storage.Run(
                tenant_id=1,
                started_at=now - timedelta(hours=1),
                finished_at=now - timedelta(minutes=30),
                status="ok",
                run_quality={"financially_eligible": False},
            ),
        ]
    )
    setup_db.commit()

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    assert body["last_run_status"] == "degraded"


def test_health_endpoint_partial_ok_preserves_full_catalog_failure(client, setup_db):
    now = utcnow()
    full = storage.Run(
        tenant_id=1,
        started_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=1),
        status="degraded",
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=False,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": False,
            "financially_eligible": False,
            "sites": {"pharmonline": {"status": "degraded"}},
        },
    )
    setup_db.add(full)
    setup_db.flush()
    setup_db.add(
        storage.Run(
            tenant_id=1,
            started_at=now - timedelta(minutes=30),
            finished_at=now,
            status="ok",
            catalog_scope="partial",
            full_catalog_sites=None,
            catalog_verified=False,
            run_quality={
                "baseline_enforced": False,
                "full_catalog_verified": False,
                "financially_eligible": False,
                "sites": {"pharmonline": {"status": "ok"}},
            },
        )
    )
    setup_db.commit()

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    assert body["last_run_status"] == "ok"
    assert body["full_catalog_status"] == "degraded"
    assert body["full_catalog_verified"] is False
    assert body["full_catalog_run_at"] is not None


def test_health_endpoint_aloe_full_does_not_mask_other_degraded_sites(client, setup_db):
    now = utcnow()
    setup_db.add(
        storage.Run(
            tenant_id=1,
            started_at=now - timedelta(hours=2),
            finished_at=now - timedelta(hours=1),
            status="degraded",
            catalog_scope="full",
            full_catalog_sites="pharmonline,aptekonline,aloe",
            catalog_verified=False,
            run_quality={
                "baseline_enforced": True,
                "full_catalog_verified": False,
                "financially_eligible": False,
                "sites": {
                    "pharmonline": {"status": "degraded"},
                    "aptekonline": {"status": "degraded"},
                    "aloe": {"status": "ok"},
                },
            },
        )
    )
    setup_db.add(
        storage.Run(
            tenant_id=1,
            started_at=now - timedelta(minutes=30),
            finished_at=now,
            status="ok",
            catalog_scope="full",
            full_catalog_sites="aloe",
            catalog_verified=True,
            run_quality={
                "baseline_enforced": True,
                "full_catalog_verified": True,
                "financially_eligible": True,
                "sites": {"aloe": {"status": "ok"}},
            },
        )
    )
    setup_db.commit()

    body = client.get("/health").json()

    assert body["last_run_status"] == "ok"
    assert body["status"] == "degraded"
    assert body["full_catalog_status"] == "degraded"
    assert body["full_catalog_verified"] is False


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
    assert sites["aloe"]["max_age_hours"] == 30


def test_health_endpoint_uses_weekly_aptekonline_threshold(client, setup_db):
    """aptekonline is weekly: 100h old is still inside the 198h backend threshold."""

    db = setup_db
    old = datetime.now(timezone.utc) - timedelta(hours=100)
    db.add(
        storage.Product(
            tenant_id=1,
            site="aptekonline",
            external_id="weekly-ok-1",
            url="https://example.com/aptek",
            name="Weekly OK",
            name_normalized="weekly ok",
            last_seen_at=old,
            first_seen_at=old,
        )
    )
    db.commit()

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["staleness_warning"] is False
    # Staleness is healthy, but no verified full-catalog lineage exists.
    assert body["status"] == "degraded"
    assert body["full_catalog_status"] == "missing"
    sites = {s["site"]: s for s in body["sites"]}
    assert sites["aptekonline"]["hours_since"] >= 100
    assert sites["aptekonline"]["max_age_hours"] == 198


def test_health_endpoint_degrades_on_latest_failed_run(client, setup_db):
    setup_db.add(
        storage.Run(
            tenant_id=1,
            status="failed",
            started_at=utcnow(),
            finished_at=utcnow(),
            error_message="identity revalidation failed",
        )
    )
    setup_db.commit()

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    assert body["last_run_status"] == "failed"


def _add_policy_ready_catalog(db, *, verified: bool = True) -> None:
    now = utcnow()
    run = storage.Run(
        tenant_id=1,
        status="ok",
        started_at=now,
        finished_at=now,
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=verified,
        catalog_verification_reason=("complete_nonzero_coverage_ok" if verified else "failed"),
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": verified,
            "financially_eligible": verified,
            "sites": {
                "pharmonline": {"status": "ok" if verified else "failed"},
                "aptekonline": {"status": "ok" if verified else "failed"},
                "aloe": {"status": "ok" if verified else "failed"},
            },
        },
    )
    db.add(run)
    db.flush()
    for site in ("pharmonline", "aptekonline", "aloe"):
        product = storage.Product(
            tenant_id=1,
            site=site,
            external_id=f"policy-{site}",
            url=f"https://example.com/{site}",
            name=f"Policy {site}",
            name_normalized=f"policy {site}",
            first_seen_at=now,
            last_seen_at=now,
            manufacturer_country_code="rs",
            country_resolution_status="resolved",
            offer_availability_status="in_stock",
            availability_observed_at=now,
        )
        db.add(product)
        db.flush()
        db.add(
            storage.OfferObservation(
                tenant_id=1,
                run_id=run.id,
                product_id=product.id,
                country_code="rs",
                country_raw="Serbia",
                country_resolution_status="resolved",
                country_source="test",
                availability_status="in_stock",
                availability_source="test",
                observed_at=now,
            )
        )
    db.commit()
    return run


def _mark_trusted_full_run(
    db,
    run: storage.Run,
    *,
    tenant_id: int = 1,
    sites: tuple[str, ...] = ("pharmonline", "aptekonline", "aloe"),
) -> storage.Run:
    """Make a test run eligible for money-facing endpoints.

    Production now fails closed unless snapshots come from a verified
    full-catalog run. Most legacy API tests only cared about comparison logic,
    so their bare `Run(status="ok")` fixtures need this explicit trust envelope.
    """
    now = utcnow()
    run.tenant_id = tenant_id
    run.status = "ok"
    run.started_at = run.started_at or now
    run.finished_at = run.finished_at or run.started_at
    run.catalog_scope = "full"
    run.full_catalog_sites = ",".join(sites)
    run.catalog_verified = True
    run.catalog_verification_reason = "test_verified_full_catalog"
    run.run_quality = {
        "baseline_enforced": True,
        "full_catalog_verified": True,
        "financially_eligible": True,
        "sites": {site: {"status": "ok"} for site in sites},
    }
    db.add(run)
    db.flush()
    return run


def test_health_policy_gate_requires_verified_full_catalog(
    client, setup_db, monkeypatch
):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "enforce")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "enforce")
    _add_policy_ready_catalog(setup_db, verified=False)

    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["product_policy"]["policy_ready"] is False
    assert body["product_policy"]["full_catalog_trust_ready"] is False
    assert all(
        row["full_catalog_run_id"] is None
        for row in body["product_policy"]["sites"]
    )


def test_health_policy_gate_opens_only_with_fresh_coverage(
    client, setup_db, monkeypatch
):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "enforce")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "enforce")
    _add_policy_ready_catalog(setup_db)

    body = client.get("/health").json()
    assert body["status"] == "up"
    assert body["product_policy"]["policy_ready"] is True
    assert body["product_policy"]["full_catalog_trust_ready"] is True
    assert all(row["ready"] for row in body["product_policy"]["sites"])


def test_newer_unverified_full_attempt_closes_previous_trust(
    client, setup_db, monkeypatch
):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "enforce")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "enforce")
    _add_policy_ready_catalog(setup_db, verified=True)
    now = utcnow()
    setup_db.add(
        storage.Run(
            tenant_id=1,
            status="ok",
            started_at=now,
            finished_at=now,
            catalog_scope="full",
            full_catalog_sites="pharmonline,aptekonline,aloe",
            catalog_verified=False,
            catalog_verification_reason="aloe:pages_skipped=1",
        )
    )
    setup_db.commit()

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    assert body["product_policy"]["policy_ready"] is False
    assert all(
        row["latest_full_attempt_verified"] is False
        and row["full_catalog_run_id"] is None
        for row in body["product_policy"]["sites"]
    )


def test_partial_product_refresh_cannot_rewrite_full_run_trust(
    client, setup_db, monkeypatch
):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "enforce")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "enforce")
    _add_policy_ready_catalog(setup_db)
    aloe = setup_db.scalar(
        select(storage.Product).where(storage.Product.site == "aloe")
    )
    observation = setup_db.scalar(
        select(storage.OfferObservation).where(
            storage.OfferObservation.product_id == aloe.id
        )
    )
    observation.country_code = None
    observation.country_resolution_status = "ambiguous"
    observation.availability_status = "unknown"
    # Mutable current state resembles a later successful partial observation.
    aloe.manufacturer_country_code = "rs"
    aloe.country_resolution_status = "resolved"
    aloe.offer_availability_status = "in_stock"
    aloe.availability_observed_at = utcnow()
    setup_db.commit()

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    aloe_policy = next(
        row for row in body["product_policy"]["sites"] if row["site"] == "aloe"
    )
    assert aloe_policy["country_coverage_pct"] == 0
    assert aloe_policy["availability_coverage_pct"] == 0


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


def test_dash_comparison_ignores_newer_untrusted_snapshot(
    client, auth_cookie, setup_db
):
    trusted = _add_policy_ready_catalog(setup_db)
    partial = storage.Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=utcnow(),
        status="degraded",
        catalog_scope="partial",
        catalog_verified=False,
        run_quality={
            "baseline_enforced": False,
            "full_catalog_verified": False,
            "financially_eligible": False,
            "sites": {"pharmonline": {"status": "degraded"}},
        },
    )
    setup_db.add(partial)
    setup_db.flush()
    match = storage.Match(tenant_id=1, canonical_name="Trusted comparison", confidence=1.0)
    setup_db.add(match)
    setup_db.flush()
    products = {}
    for site in ("pharmonline", "aloe"):
        product = storage.Product(
            tenant_id=1,
            site=site,
            external_id=f"cmp-{site}",
            url=f"https://example.com/{site}/cmp",
            name="Trusted comparison",
            name_normalized="trusted comparison",
            canonical_id=match.id,
            manufacturer_country_code="rs",
            country_resolution_status="resolved",
            offer_availability_status="in_stock",
            availability_observed_at=utcnow(),
        )
        setup_db.add(product)
        setup_db.flush()
        products[site] = product
    setup_db.add_all(
        [
            storage.PriceSnapshot(
                run_id=trusted.id,
                product_id=products["pharmonline"].id,
                price=10.0,
            ),
            storage.PriceSnapshot(
                run_id=trusted.id,
                product_id=products["aloe"].id,
                price=8.0,
            ),
            storage.PriceSnapshot(
                run_id=partial.id,
                product_id=products["pharmonline"].id,
                price=99.0,
            ),
            storage.PriceSnapshot(
                run_id=partial.id,
                product_id=products["aloe"].id,
                price=1.0,
            ),
        ]
    )
    setup_db.commit()

    response = client.get("/api/v1/dash/comparison?search=Trusted%20comparison")

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["prices"]["pharmonline"]["price"] == 10.0
    assert rows[0]["prices"]["aloe"]["price"] == 8.0


def test_dash_comparison_shadow_bootstraps_without_trusted_lineage(
    client, auth_cookie, setup_db, monkeypatch
):
    """Shadow rollout must not return an empty catalog before its first trusted full run."""
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "shadow")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    run = storage.Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=utcnow(),
        status="ok",
        catalog_scope="partial",
        catalog_verified=False,
        run_quality={
            "baseline_enforced": False,
            "full_catalog_verified": False,
            "financially_eligible": False,
            "sites": {},
        },
    )
    setup_db.add(run)
    setup_db.flush()
    match = storage.Match(tenant_id=1, canonical_name="Shadow comparison", confidence=1.0)
    setup_db.add(match)
    setup_db.flush()
    products = []
    for site, price in (("pharmonline", 10.0), ("aloe", 8.0)):
        product = storage.Product(
            tenant_id=1,
            site=site,
            external_id=f"shadow-{site}",
            url=f"https://example.com/{site}/shadow",
            name="Shadow comparison",
            name_normalized="shadow comparison",
            canonical_id=match.id,
            manufacturer_country_code="rs",
            country_resolution_status="resolved",
            offer_availability_status="in_stock",
            availability_observed_at=utcnow(),
            last_seen_at=utcnow(),
        )
        setup_db.add(product)
        setup_db.flush()
        products.append(product)
        setup_db.add(storage.PriceSnapshot(run_id=run.id, product_id=product.id, price=price))
    setup_db.commit()

    response = client.get("/api/v1/dash/comparison?search=Shadow%20comparison")

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["prices"]["pharmonline"]["price"] == 10.0
    assert rows[0]["prices"]["aloe"]["price"] == 8.0


def test_dash_runs_latest_by_site_includes_weekly_site(client, auth_cookie, setup_db):
    """Latest-by-site keeps aptekonline visible even when global recent runs are newer."""

    db = setup_db
    base = utcnow()
    aptek = storage.Run(
        tenant_id=1,
        started_at=base - timedelta(days=4),
        finished_at=base - timedelta(days=4) + timedelta(minutes=25),
        status="ok",
        products_scraped=93932,
        products_per_site={"aptekonline": 93932},
        sites_completed="aptekonline",
    )
    aloe = storage.Run(
        tenant_id=1,
        started_at=base - timedelta(hours=2),
        finished_at=base - timedelta(hours=2) + timedelta(minutes=1),
        status="ok",
        products_scraped=6237,
        products_per_site={"aloe": 6237},
        sites_completed="aloe",
    )
    pharm = storage.Run(
        tenant_id=1,
        started_at=base - timedelta(hours=1),
        finished_at=base - timedelta(hours=1) + timedelta(minutes=1),
        status="ok",
        products_scraped=166,
        products_per_site={"pharmonline": 166},
        sites_completed="pharmonline",
    )
    other_tenant_newer = storage.Run(
        tenant_id=2,
        started_at=base,
        finished_at=base,
        status="ok",
        products_scraped=999,
        products_per_site={"aptekonline": 999},
        sites_completed="aptekonline",
    )
    db.add_all([aptek, aloe, pharm, other_tenant_newer])
    db.commit()

    r = client.get("/api/v1/dash/runs/latest-by-site")
    assert r.status_code == 200, r.text
    rows = {row["site"]: row["run"] for row in r.json()}
    assert rows["pharmonline"]["id"] == pharm.id
    assert rows["aloe"]["id"] == aloe.id
    assert rows["aptekonline"]["id"] == aptek.id
    assert rows["aptekonline"]["products_per_site"] == {"aptekonline": 93932}

    recent = client.get("/api/v1/dash/runs?limit=5")
    assert recent.status_code == 200, recent.text
    recent_ids = {row["id"] for row in recent.json()}
    assert other_tenant_newer.id not in recent_ids
    assert pharm.id in recent_ids


def test_site_history_ignores_legacy_zero_placeholder_and_keeps_real_failure_visible(
    client, auth_cookie, setup_db
):
    db = setup_db
    base = utcnow()
    successful = storage.Run(
        tenant_id=1,
        started_at=base - timedelta(days=1),
        finished_at=base - timedelta(days=1) + timedelta(minutes=10),
        status="ok",
        products_scraped=6300,
        products_per_site={"aloe": 6300},
        sites_completed="aloe",
    )
    # Historical producer wrote all sites with zero even though only
    # pharmonline was in scope. It must not become aloe's latest attempt.
    placeholder = storage.Run(
        tenant_id=1,
        started_at=base - timedelta(hours=2),
        finished_at=base - timedelta(hours=2) + timedelta(minutes=1),
        status="degraded",
        products_scraped=200,
        products_per_site={"pharmonline": 200, "aptekonline": 0, "aloe": 0},
        sites_completed="pharmonline",
    )
    run_446_shape = storage.Run(
        tenant_id=1,
        started_at=base - timedelta(hours=3),
        finished_at=base - timedelta(hours=3) + timedelta(minutes=6),
        status="degraded",
        products_scraped=606,
        products_per_site={"pharmonline": 606, "aptekonline": 0, "aloe": 0},
        sites_completed="pharmonline,aptekonline,aloe",
        run_quality={
            "sites": {
                "pharmonline": {"status": "ok", "items_expected": 1, "products": 606},
                "aptekonline": {"status": "failed", "items_expected": 0, "products": 0, "reasons": ["no_items_requested"]},
                "aloe": {"status": "failed", "items_expected": 0, "products": 0, "reasons": ["no_items_requested"]},
            }
        },
    )
    # A real failed aloe attempt has expected work and remains visible as the
    # latest attempt, while the trusted positive run remains the display run.
    real_failure = storage.Run(
        tenant_id=1,
        started_at=base - timedelta(hours=1),
        finished_at=base - timedelta(minutes=50),
        status="degraded",
        products_scraped=0,
        products_per_site={"aloe": 0},
        run_quality={
            "sites": {
                "aloe": {
                    "status": "degraded",
                    "items_expected": 6,
                    "items_completed": 0,
                    "items_failed": 6,
                    "products": 0,
                }
            }
        },
    )
    db.add_all([successful, run_446_shape, placeholder, real_failure])
    db.commit()

    rows = {
        row["site"]: row
        for row in client.get("/api/v1/dash/runs/latest-by-site").json()
    }
    assert rows["aloe"]["run"]["id"] == successful.id
    assert rows["aloe"]["latest_attempt"]["id"] == real_failure.id
    assert rows["aptekonline"]["run"] is None
    assert rows["aptekonline"]["latest_attempt"] is None

    summary = client.get("/api/v1/dash/products/summary?site=aloe").json()
    assert summary["last_run_id"] == successful.id


def test_site_summary_does_not_reuse_another_sites_latest_run(
    client, auth_cookie, setup_db
):
    db = setup_db
    aloe = storage.Run(
        tenant_id=1,
        status="ok",
        started_at=utcnow() - timedelta(hours=2),
        products_scraped=5,
        products_per_site={"aloe": 5},
        sites_completed="aloe",
    )
    pharm = storage.Run(
        tenant_id=1,
        status="ok",
        started_at=utcnow() - timedelta(hours=1),
        products_scraped=7,
        products_per_site={"pharmonline": 7},
        sites_completed="pharmonline",
    )
    db.add_all([aloe, pharm])
    db.commit()

    summary = client.get("/api/v1/dash/products/summary?site=aloe").json()
    assert summary["last_run_id"] == aloe.id


def test_run_history_filters_by_real_site_scope_and_paginates(
    client, auth_cookie, setup_db
):
    rows = [
        storage.Run(
            tenant_id=1,
            status="ok",
            products_scraped=10 + i,
            products_per_site={"aloe": 10 + i},
            sites_completed="aloe",
        )
        for i in range(3)
    ]
    placeholder = storage.Run(
        tenant_id=1,
        status="degraded",
        products_per_site={"aloe": 0, "pharmonline": 5},
        sites_completed="pharmonline",
    )
    setup_db.add_all([*rows, placeholder])
    setup_db.commit()

    response = client.get("/api/v1/dash/runs/history?site=aloe&limit=2&offset=1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert placeholder.id not in {item["id"] for item in body["items"]}


def test_scrape_request_history_is_tenant_scoped_and_paginated(
    client, auth_cookie, setup_db
):
    own = [
        storage.ScrapeRequest(tenant_id=1, status="failed", mode="all")
        for _ in range(3)
    ]
    setup_db.add_all(
        [*own, storage.ScrapeRequest(tenant_id=2, status="failed", mode="all")]
    )
    setup_db.commit()

    response = client.get(
        "/api/v1/dash/scrape/requests/history?status=failed&limit=2"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_successful_dashboard_mutation_is_audited_without_request_body(
    client, auth_cookie, tenant_user, setup_db
):
    secret_marker = "22-08"
    response = client.patch(
        "/api/v1/dash/me/notifications",
        json={"quiet_hours": secret_marker},
    )
    assert response.status_code == 200, response.text

    audit = client.get("/api/v1/dash/audit-log")

    assert audit.status_code == 200, audit.text
    body = audit.json()
    assert body["total"] >= 1
    row = body["items"][0]
    assert row["actor_user_id"] == tenant_user.id
    assert row["actor_email"] == tenant_user.email
    assert row["action"] == "PATCH"
    assert row["resource"] == "/api/v1/dash/me/notifications"
    assert row["response_status"] == 200
    assert row["request_id"]
    assert secret_marker not in audit.text


def test_failed_dashboard_mutation_is_not_audited(client, auth_cookie):
    response = client.patch(
        "/api/v1/dash/me/notifications",
        json={"email_severity_min": "not-a-level"},
    )
    assert response.status_code == 422

    audit = client.get("/api/v1/dash/audit-log").json()
    assert audit["total"] == 0


@pytest.mark.asyncio
async def test_audit_middleware_uses_isolated_session(monkeypatch):
    from fastapi import Request, Response

    class FakeAuditSession:
        def __init__(self):
            self.added = []
            self.commits = 0
            self.rollbacks = 0
            self.closed = 0

        def add(self, row):
            self.added.append(row)

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closed += 1

    class BusinessSession:
        commits = 0

        def commit(self):
            self.commits += 1

    audit_session = FakeAuditSession()
    business_session = BusinessSession()
    monkeypatch.setattr(
        storage, "make_session", lambda: (lambda: audit_session)
    )
    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/api/v1/dash/categories/1",
            "query_string": b"",
            "headers": [],
            "server": ("test", 80),
            "client": ("test", 123),
            "scheme": "http",
        }
    )
    request.state.request_id = "audit-test"

    async def call_next(req):
        req.state.tenant_id = 1
        req.state.user_id = 7
        req.state.db = business_session
        return Response(status_code=200)

    response = await api_module._dashboard_audit_middleware(request, call_next)

    assert response.status_code == 200
    assert business_session.commits == 0
    assert audit_session.commits == 1
    assert audit_session.closed == 1
    assert len(audit_session.added) == 1


def _make_match_with_prices(db, run, *, canonical, prices, tenant_id=1, category=None):
    """Match + products на 2 сайтах + PriceSnapshot для каждого.

    `category` (опц.) проставляется всем products — для тестов
    /category-comparison и drill-down /comparison?category=.
    """
    _mark_trusted_full_run(db, run, tenant_id=tenant_id)
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


def test_comparison_fails_closed_when_enforce_lacks_full_catalog(
    client, tenant_user, setup_db, monkeypatch
):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "enforce")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "enforce")
    token = tenants.issue_magic_token(setup_db, tenant_user.email)
    client.get(f"/auth/verify?token={token}")

    response = client.get("/api/v1/dash/comparison")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "full_catalog_trust_not_ready"


def test_forecast_fails_closed_when_enforce_lacks_full_catalog(
    client, tenant_user, setup_db, monkeypatch
):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "enforce")
    token = tenants.issue_magic_token(setup_db, tenant_user.email)
    client.get(f"/auth/verify?token={token}")

    response = client.get("/api/v1/dash/forecast/movers")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "full_catalog_trust_not_ready"


def test_forecast_movers_ignores_newer_untrusted_snapshot(
    client, auth_cookie, setup_db
):
    product = storage.Product(
        tenant_id=1,
        site="aloe",
        external_id="forecast-trusted",
        url="https://aloe.example/forecast-trusted",
        name="Forecast trusted",
        name_normalized="forecast trusted",
        last_seen_at=utcnow(),
    )
    setup_db.add(product)
    setup_db.flush()
    old_run = _mark_trusted_full_run(
        setup_db,
        storage.Run(tenant_id=1, started_at=utcnow() - timedelta(days=10), status="ok"),
    )
    new_run = _mark_trusted_full_run(
        setup_db,
        storage.Run(tenant_id=1, started_at=utcnow() - timedelta(days=1), status="ok"),
    )
    partial_run = storage.Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=utcnow(),
        status="ok",
        catalog_scope="partial",
        catalog_verified=False,
        run_quality={
            "baseline_enforced": False,
            "full_catalog_verified": False,
            "financially_eligible": False,
            "sites": {"aloe": {"status": "ok"}},
        },
    )
    setup_db.add(partial_run)
    setup_db.flush()
    setup_db.add_all(
        [
            storage.PriceSnapshot(run_id=old_run.id, product_id=product.id, price=10.0),
            storage.PriceSnapshot(run_id=new_run.id, product_id=product.id, price=8.0),
            storage.PriceSnapshot(run_id=partial_run.id, product_id=product.id, price=1.0),
        ]
    )
    setup_db.commit()

    response = client.get("/api/v1/dash/forecast/movers?limit=10")

    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item["name"] == "Forecast trusted")
    assert row["first_price"] == 10.0
    assert row["last_price"] == 8.0
    assert row["change_pct"] == -20.0


def test_comparison_excludes_known_cross_country_cluster_even_in_shadow(
    client, tenant_user, setup_db
):
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    match = _make_match_with_prices(
        s,
        run,
        canonical="Ornafer cross-country",
        prices={"pharmonline": 26.2, "aloe": 25.4},
    )
    products = list(match.products)
    products[0].manufacturer_country_code = "lv"
    products[1].manufacturer_country_code = "gb"
    for product in products:
        product.country_resolution_status = "resolved"
    s.commit()

    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    rows = client.get("/api/v1/dash/comparison?min_sites=2").json()
    assert not any(row["name"] == "Ornafer cross-country" for row in rows)


def test_comparison_excludes_only_explicit_oos_offer(client, tenant_user, setup_db):
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    s.add(run)
    s.flush()
    match = _make_match_with_prices(
        s,
        run,
        canonical="Three offers",
        prices={"pharmonline": 10.0, "aptekonline": 9.0, "aloe": 8.0},
    )
    aloe = next(product for product in match.products if product.site == "aloe")
    aloe.offer_availability_status = "out_of_stock"
    aloe.availability_observed_at = utcnow()
    s.commit()

    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")
    rows = client.get("/api/v1/dash/comparison?min_sites=2").json()
    row = next(item for item in rows if item["name"] == "Three offers")
    assert set(row["prices"]) == {"pharmonline", "aptekonline"}


def _make_match_with_packs(db, run, *, canonical, prods, tenant_id=1):
    """Match + products с заданными (site, price, pack_size, name)."""
    _mark_trusted_full_run(db, run, tenant_id=tenant_id)
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
    _mark_trusted_full_run(db, run, tenant_id=tenant_id)
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


def test_alert_page_filters_and_paginates_on_server(
    client, auth_cookie, setup_db
):
    now = utcnow()
    sites = ["pharmonline", "aloe", "aptekonline", "pharmonline"]
    events = [
        storage.AlertEvent(
            tenant_id=1,
            rule_type="price_drop_pct",
            dedup_key=f"drop-{i}",
            severity="warning",
            title="pharmonline in title" if sites[i] == "aloe" else f"Drop {i}",
            payload={"site": sites[i]},
            created_at=now if i < 2 else now - timedelta(minutes=i),
            is_read=False,
        )
        for i in range(4)
    ]
    read_without_site = storage.AlertEvent(
        tenant_id=1,
        rule_type="new_product",
        dedup_key="read-one",
        severity="info",
        title="Read without site",
        created_at=now,
        is_read=True,
    )
    read_with_unknown_site = storage.AlertEvent(
        tenant_id=1,
        rule_type="new_product",
        dedup_key="read-unknown-site",
        severity="info",
        title="Read with unknown site",
        payload={"site": "unknown"},
        created_at=now - timedelta(seconds=1),
        is_read=True,
    )
    other_tenant = storage.AlertEvent(
        tenant_id=2,
        rule_type="price_drop_pct",
        dedup_key="tenant-two",
        severity="warning",
        title="Tenant two aloe",
        payload={"site": "aloe"},
        created_at=now,
        is_read=False,
    )
    setup_db.add_all(
        [*events, read_without_site, read_with_unknown_site, other_tenant]
    )
    setup_db.commit()

    first = client.get(
        "/api/v1/dash/alerts/page?view=inbox&severity=warning&limit=2"
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == 4
    assert len(body["items"]) == 2
    assert body["rule_types"] == ["new_product", "price_drop_pct"]
    assert [item["id"] for item in body["items"]] == [events[1].id, events[0].id]
    assert [item["site"] for item in body["items"]] == ["aloe", "pharmonline"]

    second = client.get(
        "/api/v1/dash/alerts/page?view=inbox&severity=warning&limit=2&offset=2"
    ).json()
    assert len(second["items"]) == 2
    assert {item["id"] for item in body["items"]}.isdisjoint(
        {item["id"] for item in second["items"]}
    )

    aloe_page = client.get(
        "/api/v1/dash/alerts/page?view=inbox&site=aloe&severity=warning"
    ).json()
    assert aloe_page["total"] == 1
    assert aloe_page["items"][0]["id"] == events[1].id
    assert aloe_page["items"][0]["title"] == "pharmonline in title"

    pharm_page = client.get(
        "/api/v1/dash/alerts/page?view=inbox&site=pharmonline&severity=warning&limit=1"
    ).json()
    assert pharm_page["total"] == 2
    assert len(pharm_page["items"]) == 1

    general_page = client.get(
        "/api/v1/dash/alerts/page?view=read&site=general&hours=0"
    ).json()
    assert general_page["total"] == 2
    assert {item["id"] for item in general_page["items"]} == {
        read_without_site.id,
        read_with_unknown_site.id,
    }
    assert all(item["site"] is None for item in general_page["items"])

    oldest_page = client.get(
        "/api/v1/dash/alerts/page?view=inbox&severity=warning&sort=oldest&hours=0"
    ).json()
    assert [item["id"] for item in oldest_page["items"]] == [
        events[3].id,
        events[2].id,
        events[0].id,
        events[1].id,
    ]

    site_page = client.get(
        "/api/v1/dash/alerts/page?view=inbox&severity=warning&sort=site&hours=0"
    ).json()
    assert [item["site"] for item in site_page["items"]] == [
        "aloe",
        "aptekonline",
        "pharmonline",
        "pharmonline",
    ]
    assert [item["id"] for item in site_page["items"][-2:]] == [
        events[0].id,
        events[3].id,
    ]


@pytest.mark.parametrize("query", ["site=unknown", "sort=random"])
def test_alert_page_rejects_unknown_site_or_sort(client, auth_cookie, query):
    response = client.get(f"/api/v1/dash/alerts/page?{query}")
    assert response.status_code == 422


def test_alert_page_site_sort_places_general_last(client, auth_cookie, setup_db):
    now = utcnow()
    rows = [
        storage.AlertEvent(
            tenant_id=1,
            rule_type="new_product",
            dedup_key=f"site-sort-{site or 'general'}",
            severity="info",
            title=site or "General",
            payload={"site": site} if site else None,
            created_at=now,
            is_read=False,
        )
        for site in ("pharmonline", None, "aloe", "aptekonline")
    ]
    setup_db.add_all(rows)
    setup_db.commit()

    page = client.get(
        "/api/v1/dash/alerts/page?view=inbox&sort=site&hours=0"
    ).json()
    assert [item["site"] for item in page["items"]] == [
        "aloe",
        "aptekonline",
        "pharmonline",
        None,
    ]


def test_alert_page_resolves_product_destination_tenant_safe(
    client, auth_cookie, setup_db
):
    product = storage.Product(
        tenant_id=1,
        site="aloe",
        external_id="pedikar-50-ml",
        url="https://aloe.az/pedikar-50-ml/",
        name="Pedikar 50 ml",
        name_normalized="pedikar 50 ml",
    )
    foreign_product = storage.Product(
        tenant_id=2,
        site="aloe",
        external_id="foreign-product",
        url="https://example.com/private-tenant-product",
        name="Private product",
        name_normalized="private product",
    )
    setup_db.add_all([product, foreign_product])
    setup_db.flush()
    linked = storage.AlertEvent(
        tenant_id=1,
        rule_type="new_product",
        dedup_key="pedikar-link",
        severity="info",
        title="New aloe product: Pedikar 50 ml",
        payload={"site": "aloe", "product_id": product.id},
        created_at=utcnow(),
        is_read=False,
    )
    cross_tenant = storage.AlertEvent(
        tenant_id=1,
        rule_type="new_product",
        dedup_key="cross-tenant-product-link",
        severity="info",
        title="Must not expose another tenant URL",
        payload={"site": "aloe", "product_id": foreign_product.id},
        created_at=utcnow() - timedelta(seconds=1),
        is_read=False,
    )
    setup_db.add_all([linked, cross_tenant])
    setup_db.commit()

    items = client.get("/api/v1/dash/alerts/page?hours=0").json()["items"]
    by_id = {item["id"]: item for item in items}

    assert by_id[linked.id]["destination_url"] == "https://aloe.az/pedikar-50-ml/"
    assert by_id[cross_tenant.id]["destination_url"] is None


def test_safe_alert_destination_rejects_non_http_schemes():
    assert api_module._safe_alert_destination("javascript:alert(1)") is None
    assert api_module._safe_alert_destination("/relative/path") is None
    assert api_module._safe_alert_destination("https://user:secret@aloe.az/p/") is None
    assert api_module._safe_alert_destination("http://[") is None
    assert api_module._safe_alert_destination("https://[::1") is None
    assert (
        api_module._safe_alert_destination("https://aloe.az/pedikar-50-ml/")
        == "https://aloe.az/pedikar-50-ml/"
    )


def test_alert_page_resolves_match_destination_by_requested_site(
    client, auth_cookie, setup_db
):
    match = storage.Match(
        tenant_id=1, canonical_name="Matched product", confidence=1.0
    )
    foreign_match = storage.Match(
        tenant_id=2, canonical_name="Foreign match", confidence=1.0
    )
    setup_db.add_all([match, foreign_match])
    setup_db.flush()
    products = [
        storage.Product(
            tenant_id=1,
            site="pharmonline",
            external_id="matched-pharmonline",
            url="https://pharmonline.az/product/matched",
            name="Matched product",
            name_normalized="matched product",
            canonical_id=match.id,
        ),
        storage.Product(
            tenant_id=1,
            site="aloe",
            external_id="matched-aloe-dead",
            url="https://aloe.az/dead-product/",
            name="Matched product old",
            name_normalized="matched product old",
            canonical_id=match.id,
            url_dead_at=utcnow(),
        ),
        storage.Product(
            tenant_id=1,
            site="aloe",
            external_id="matched-aloe-live",
            url="https://aloe.az/live-product/",
            name="Matched product",
            name_normalized="matched product",
            canonical_id=match.id,
        ),
        storage.Product(
            tenant_id=2,
            site="aloe",
            external_id="foreign-match-aloe",
            url="https://example.com/foreign-tenant-match",
            name="Foreign match",
            name_normalized="foreign match",
            canonical_id=foreign_match.id,
        ),
    ]
    setup_db.add_all(products)
    setup_db.flush()
    requested_site = storage.AlertEvent(
        tenant_id=1,
        rule_type="undercut_threshold",
        dedup_key="match-link-requested-site",
        severity="warning",
        title="Aloe undercut",
        payload={"site": "aloe", "match_id": match.id},
        created_at=utcnow(),
        is_read=False,
    )
    cross_tenant = storage.AlertEvent(
        tenant_id=1,
        rule_type="undercut_threshold",
        dedup_key="match-link-cross-tenant",
        severity="warning",
        title="Must not expose foreign match",
        payload={"site": "aloe", "match_id": foreign_match.id},
        created_at=utcnow() - timedelta(seconds=1),
        is_read=False,
    )
    setup_db.add_all([requested_site, cross_tenant])
    setup_db.commit()

    items = client.get("/api/v1/dash/alerts/page?hours=0").json()["items"]
    by_id = {item["id"]: item for item in items}

    assert by_id[requested_site.id]["destination_url"] == (
        "https://aloe.az/live-product/"
    )
    assert by_id[cross_tenant.id]["destination_url"] is None


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


def test_legacy_comparisons_shadow_bootstraps_without_trusted_lineage(
    client, setup_db, monkeypatch
):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "shadow")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    run = storage.Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=utcnow(),
        status="ok",
        catalog_scope="partial",
        catalog_verified=False,
        run_quality={
            "baseline_enforced": False,
            "full_catalog_verified": False,
            "financially_eligible": False,
            "sites": {},
        },
    )
    setup_db.add(run)
    setup_db.flush()
    match = storage.Match(tenant_id=1, canonical_name="Legacy shadow", confidence=1.0)
    setup_db.add(match)
    setup_db.flush()
    for site, price in (("pharmonline", 10.0), ("aloe", 8.0)):
        product = storage.Product(
            tenant_id=1,
            site=site,
            external_id=f"legacy-shadow-{site}",
            url=f"https://example.com/{site}/legacy-shadow",
            name="Legacy shadow",
            name_normalized="legacy shadow",
            canonical_id=match.id,
            manufacturer_country_code="rs",
            country_resolution_status="resolved",
            offer_availability_status="in_stock",
            availability_observed_at=utcnow(),
            last_seen_at=utcnow(),
        )
        setup_db.add(product)
        setup_db.flush()
        setup_db.add(
            storage.PriceSnapshot(run_id=run.id, product_id=product.id, price=price)
        )
    setup_db.commit()

    response = client.get(
        "/api/v1/comparisons",
        headers={"X-API-Key": "test-key-1234"},
    )

    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item["name"] == "Legacy shadow")
    assert row["prices"]["pharmonline"]["price"] == 10.0
    assert row["prices"]["aloe"]["price"] == 8.0


def test_legacy_comparisons_enforce_without_trusted_lineage_503(
    client, monkeypatch
):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "enforce")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "enforce")

    response = client.get(
        "/api/v1/comparisons",
        headers={"X-API-Key": "test-key-1234"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "full_catalog_trust_not_ready"


def test_legacy_comparisons_ignore_newer_untrusted_snapshot(client, setup_db):
    run = storage.Run(tenant_id=1, started_at=utcnow() - timedelta(hours=2), status="ok")
    setup_db.add(run)
    setup_db.flush()
    match = _make_match_with_prices(
        setup_db,
        run,
        canonical="Legacy trusted",
        prices={"pharmonline": 10.0, "aloe": 8.0},
    )
    aloe = next(product for product in match.products if product.site == "aloe")
    partial_run = storage.Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=utcnow(),
        status="ok",
        catalog_scope="partial",
        catalog_verified=False,
        run_quality={
            "baseline_enforced": False,
            "full_catalog_verified": False,
            "financially_eligible": False,
            "sites": {"aloe": {"status": "ok"}},
        },
    )
    setup_db.add(partial_run)
    setup_db.flush()
    setup_db.add(
        storage.PriceSnapshot(
            run_id=partial_run.id,
            product_id=aloe.id,
            price=1.0,
            captured_at=utcnow() + timedelta(seconds=1),
        )
    )
    setup_db.commit()

    response = client.get(
        "/api/v1/comparisons",
        headers={"X-API-Key": "test-key-1234"},
    )

    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item["name"] == "Legacy trusted")
    assert row["prices"]["aloe"]["price"] == 8.0


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
        json={"key": "test", "label_ru": "Test", "label_az": "Test AZ"},
    )
    assert r.status_code == 401


def test_categories_page_filters_paginates_and_returns_global_stats(
    client, auth_cookie, setup_db
):
    setup_db.add_all(
        [
            storage.Category(
                key=f"cat-{i}",
                label_ru=f"Категория {i}",
                label_az=f"Kateqoriya {i}",
                pharmonline_slug=f"p-{i}",
                aptekonline_slug=f"a-{i}" if i % 2 == 0 else None,
                aloe_slug=f"l-{i}" if i == 0 else None,
                is_active=i != 3,
            )
            for i in range(5)
        ]
    )
    setup_db.commit()

    response = client.get(
        "/api/v1/dash/categories/page?coverage=cross2&limit=1&offset=1"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 1
    assert body["stats"] == {
        "total": 5,
        "active": 4,
        "cross2": 3,
        "cross3": 1,
        "pharmonline": 5,
        "aptekonline": 3,
        "aloe": 1,
    }


def test_dash_categories_require_nonblank_az_label(client, auth_cookie):
    r = client.post(
        "/api/v1/dash/categories",
        json={"key": "blank-az", "label_ru": "Русское имя", "label_az": "   "},
    )
    assert r.status_code == 422


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
            "missing_site_counts": {"aloe": 0},
        }
    ]

    deleted = client.delete(f"/api/v1/dash/watchlist/categories/{tracked_id}")
    assert deleted.status_code == 204
    assert client.get("/api/v1/dash/watchlist/categories").json() == []


def test_dash_watchlist_categories_skip_comparison_without_pharmonline_slug(
    client, auth_cookie, setup_db, monkeypatch
):
    cat = storage.Category(
        key="aloe-only",
        label_ru="Aloe only",
        aloe_slug="aloe-only",
        is_active=True,
    )
    setup_db.add(cat)
    setup_db.add(
        storage.Product(
            tenant_id=1,
            site="aloe",
            external_id="aloe-only-1",
            url="https://aloe.example/only-1",
            name="Aloe only product",
            name_normalized="aloe only product",
            category="aloe-only",
        )
    )
    setup_db.commit()
    setup_db.refresh(cat)

    def fail_category_comparison(*args, **kwargs):
        raise AssertionError("category_comparison should not run without pharmonline slugs")

    monkeypatch.setattr(api_module.analytics, "category_comparison", fail_category_comparison)

    created = client.post("/api/v1/dash/watchlist/categories", json={"category_id": cat.id})
    assert created.status_code == 201, created.text

    item = client.get("/api/v1/dash/watchlist/categories").json()[0]
    assert item["pharmonline_slug"] is None
    assert item["product_count"] == 1
    assert item["matched_product_count"] == 0
    assert item["comparison_count"] == 0


def test_dash_watchlist_categories_mixed_slugs_only_enrich_pharmonline_categories(
    client, auth_cookie, setup_db, monkeypatch
):
    pharm_cat = storage.Category(
        key="vit",
        label_ru="Витамины",
        pharmonline_slug="vitamins",
        aloe_slug="aloe-vitamins",
        is_active=True,
    )
    aloe_cat = storage.Category(
        key="aloe-only",
        label_ru="Aloe only",
        aloe_slug="aloe-only",
        is_active=True,
    )
    setup_db.add_all([pharm_cat, aloe_cat])
    setup_db.commit()
    setup_db.refresh(pharm_cat)
    setup_db.refresh(aloe_cat)

    calls = []

    class Row:
        category = "vitamins"
        matched_skus = 7

    def fake_category_comparison(*args, **kwargs):
        calls.append((args, kwargs))
        return [Row()]

    monkeypatch.setattr(api_module.analytics, "category_comparison", fake_category_comparison)

    for cat in [pharm_cat, aloe_cat]:
        created = client.post("/api/v1/dash/watchlist/categories", json={"category_id": cat.id})
        assert created.status_code == 201, created.text

    items = {item["key"]: item for item in client.get("/api/v1/dash/watchlist/categories").json()}
    assert len(calls) == 1
    assert calls[0][1]["client_site"] == "pharmonline"
    assert calls[0][1]["tenant_id"] == 1
    assert calls[0][1]["categories"] == {"vitamins"}
    assert items["vit"]["comparison_count"] == 7
    assert items["aloe-only"]["comparison_count"] == 0


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
    aloe_product = setup_db.scalar(
        select(storage.Product).where(
            storage.Product.site == "aloe",
            storage.Product.external_id == "aloe-v1",
        )
    )
    assert aloe_product is not None
    aloe_product.category = "aloe-vitamins"
    setup_db.commit()
    low_conf = _make_match_with_prices(
        setup_db,
        run,
        canonical="low",
        prices={"pharmonline": 12.0, "aloe": 11.0},
        category="vitamins",
    )
    low_conf.confidence = 0.2
    low_aloe_product = setup_db.scalar(
        select(storage.Product).where(
            storage.Product.site == "aloe",
            storage.Product.external_id == "aloe-low",
        )
    )
    assert low_aloe_product is not None
    low_aloe_product.category = "aloe-vitamins"
    setup_db.commit()

    created = client.post("/api/v1/dash/watchlist/categories", json={"category_id": cat.id})
    assert created.status_code == 201, created.text

    item = client.get("/api/v1/dash/watchlist/categories").json()[0]
    assert item["product_count"] == 4
    assert item["matched_product_count"] == 4
    assert item["comparison_count"] == 1
    assert item["missing_site_counts"] == {"aloe": 0}


def test_dash_watchlist_categories_include_missing_site_counts(client, auth_cookie, setup_db):
    run = storage.Run(status="ok", tenant_id=1)
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)
    cat = storage.Category(
        key="supplies",
        label_ru="Медицинские средства",
        pharmonline_slug="supplies",
        aloe_slug="aloe-supplies",
        aptekonline_slug="aptek-supplies",
        is_active=True,
    )
    setup_db.add(cat)
    setup_db.commit()
    setup_db.refresh(cat)

    full = _make_match_with_prices(
        setup_db,
        run,
        canonical="full",
        prices={"pharmonline": 10.0, "aloe": 8.0, "aptekonline": 9.0},
        category="supplies",
    )
    missing_aptek = _make_match_with_prices(
        setup_db,
        run,
        canonical="missing_aptek",
        prices={"pharmonline": 11.0, "aloe": 8.5},
        category="supplies",
    )
    missing_both = _make_match_with_prices(
        setup_db,
        run,
        canonical="missing_both",
        prices={"pharmonline": 12.0},
        category="supplies",
    )
    for canonical in ("full", "missing_aptek"):
        aloe_product = setup_db.scalar(
            select(storage.Product).where(
                storage.Product.site == "aloe",
                storage.Product.external_id == f"aloe-{canonical}",
            )
        )
        assert aloe_product is not None
        aloe_product.category = "aloe-supplies"
    aptek_product = setup_db.scalar(
        select(storage.Product).where(
            storage.Product.site == "aptekonline",
            storage.Product.external_id == "aptekonline-full",
        )
    )
    assert aptek_product is not None
    aptek_product.category = "aptek-supplies"
    setup_db.commit()
    assert full.id
    assert missing_aptek.id
    assert missing_both.id

    created = client.post("/api/v1/dash/watchlist/categories", json={"category_id": cat.id})
    assert created.status_code == 201, created.text

    item = client.get("/api/v1/dash/watchlist/categories").json()[0]
    assert item["missing_site_counts"] == {"aloe": 1, "aptekonline": 2}


def test_watchlist_dead_target_member_stays_attachable(client, auth_cookie, setup_db):
    run = storage.Run(status="ok", tenant_id=1)
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)
    cat = storage.Category(
        key="dead-target",
        label_ru="Dead target",
        pharmonline_slug="supplies",
        aloe_slug="aloe-supplies",
        is_active=True,
    )
    setup_db.add(cat)
    setup_db.commit()
    setup_db.refresh(cat)
    match = _make_match_with_prices(
        setup_db,
        run,
        canonical="replace-dead",
        prices={"pharmonline": 10.0, "aloe": 8.0},
        category="supplies",
    )
    dead_aloe = setup_db.scalar(
        select(storage.Product).where(
            storage.Product.site == "aloe",
            storage.Product.external_id == "aloe-replace-dead",
        )
    )
    assert dead_aloe is not None
    dead_aloe.category = "aloe-supplies"
    dead_aloe.url_dead_at = utcnow()
    live_aloe = storage.Product(
        tenant_id=1,
        site="aloe",
        external_id="aloe-live-replacement",
        url="https://aloe.example/live-replacement",
        name="replace-dead aloe live",
        name_normalized="replace-dead aloe live",
        category="aloe-supplies",
    )
    setup_db.add(live_aloe)
    setup_db.commit()
    setup_db.refresh(live_aloe)

    created = client.post("/api/v1/dash/watchlist/categories", json={"category_id": cat.id})
    assert created.status_code == 201, created.text

    item = client.get("/api/v1/dash/watchlist/categories").json()[0]
    assert item["missing_site_counts"] == {"aloe": 1}
    counts = client.get("/api/v1/dash/matcher/counts")
    assert counts.status_code == 200, counts.text
    assert counts.json()["aloe"] == 1
    pairs = client.get("/api/v1/dash/unmatched-pairs?site=aloe&category=supplies")
    assert pairs.status_code == 200, pairs.text
    assert [row["match_id"] for row in pairs.json()["items"]] == [match.id]
    assert [p["site"] for p in pairs.json()["items"][0]["anchor_products"]] == ["pharmonline"]
    suggestions = client.get(f"/api/v1/dash/matches/{match.id}/candidate-analogs?site=aloe")
    assert suggestions.status_code == 200, suggestions.text
    assert [row["product_id"] for row in suggestions.json()["items"]] == [live_aloe.id]
    products = client.get("/api/v1/dash/products?site=aloe&search=replace-dead")
    assert products.status_code == 200, products.text
    assert [row["id"] for row in products.json()["items"]] == [live_aloe.id]
    dead_add = client.post(
        f"/api/v1/dash/matches/{match.id}/add-product",
        json={"product_id": dead_aloe.id},
    )
    assert dead_add.status_code == 400, dead_add.text

    added = client.post(
        f"/api/v1/dash/matches/{match.id}/add-product",
        json={"product_id": live_aloe.id},
    )
    assert added.status_code == 200, added.text
    setup_db.refresh(live_aloe)
    assert live_aloe.canonical_id == match.id


def test_matcher_ignores_fully_dead_clusters(client, auth_cookie, setup_db):
    match = storage.Match(tenant_id=1, canonical_name="Dead cluster", confidence=1.0)
    setup_db.add(match)
    setup_db.flush()
    setup_db.add(
        storage.Product(
            tenant_id=1,
            site="pharmonline",
            external_id="dead-ph",
            url="https://pharmonline.example/dead",
            name="Dead pharmonline",
            name_normalized="dead pharmonline",
            category="supplies",
            canonical_id=match.id,
            url_dead_at=utcnow(),
        )
    )
    setup_db.commit()

    counts = client.get("/api/v1/dash/matcher/counts")
    assert counts.status_code == 200, counts.text
    assert counts.json() == {"pharmonline": 0, "aptekonline": 0, "aloe": 0}
    pairs = client.get("/api/v1/dash/unmatched-pairs?site=aloe&category=supplies")
    assert pairs.status_code == 200, pairs.text
    assert pairs.json()["items"] == []
    facets = client.get("/api/v1/dash/products/facets?site=pharmonline")
    assert facets.status_code == 200, facets.text
    assert "supplies" not in {row["name"] for row in facets.json()["categories"]}


def test_product_facets_localize_category_labels_and_fallback(
    client,
    auth_cookie,
    setup_db,
):
    setup_db.add_all(
        [
            storage.Category(
                key="localized-vit",
                label_ru="Витамины",
                label_az="Vitaminlər",
                pharmonline_slug="localized-vit",
                is_active=True,
            ),
            storage.Category(
                key="localized-fallback",
                label_ru="Без перевода",
                label_az=None,
                pharmonline_slug="localized-fallback",
                is_active=True,
            ),
            storage.Category(
                key="cross-site-duplicate",
                label_ru="Широкая категория",
                label_az="Geniş kateqoriya",
                pharmonline_slug="localized-duplicate",
                aptekonline_slug="shared-duplicate",
                is_active=True,
            ),
            storage.Category(
                key="pharma_localized-duplicate",
                label_ru="Точная категория",
                label_az="Dəqiq kateqoriya",
                pharmonline_slug="localized-duplicate",
                is_active=True,
            ),
            storage.Product(
                tenant_id=1,
                site="pharmonline",
                external_id="localized-vit-1",
                url="https://pharmonline.example/localized-vit-1",
                name="Localized vitamin",
                name_normalized="localized vitamin",
                category="localized-vit",
            ),
            storage.Product(
                tenant_id=1,
                site="pharmonline",
                external_id="localized-fallback-1",
                url="https://pharmonline.example/localized-fallback-1",
                name="Localized fallback",
                name_normalized="localized fallback",
                category="localized-fallback",
            ),
            storage.Product(
                tenant_id=1,
                site="pharmonline",
                external_id="localized-duplicate-1",
                url="https://pharmonline.example/localized-duplicate-1",
                name="Localized duplicate",
                name_normalized="localized duplicate",
                category="localized-duplicate",
            ),
        ]
    )
    setup_db.commit()

    az_response = client.get("/api/v1/dash/products/facets?site=pharmonline&locale=az")
    assert az_response.status_code == 200, az_response.text
    az_labels = {row["name"]: row["label"] for row in az_response.json()["categories"]}
    assert az_labels["localized-vit"] == "Vitaminlər"
    assert az_labels["localized-fallback"] == "Localized fallback"
    assert az_labels["localized-duplicate"] == "Dəqiq kateqoriya"

    ru_response = client.get("/api/v1/dash/products/facets?site=pharmonline&locale=ru")
    assert ru_response.status_code == 200, ru_response.text
    ru_labels = {row["name"]: row["label"] for row in ru_response.json()["categories"]}
    assert ru_labels["localized-vit"] == "Витамины"

    invalid_response = client.get(
        "/api/v1/dash/products/facets?site=pharmonline&locale=unexpected"
    )
    assert invalid_response.status_code == 200, invalid_response.text
    invalid_labels = {
        row["name"]: row["label"] for row in invalid_response.json()["categories"]
    }
    assert invalid_labels["localized-vit"] == "Витамины"


def test_unmatched_pairs_category_paginates_and_filters_tenant_anchors(
    client, auth_cookie, setup_db
):
    matches = []
    products = []
    for idx in range(3):
        match = storage.Match(tenant_id=1, canonical_name=f"Anchor {idx}", confidence=1.0)
        setup_db.add(match)
        setup_db.flush()
        product = storage.Product(
            tenant_id=1,
            site="pharmonline",
            external_id=f"ph-{idx}",
            url=f"https://pharmonline.example/{idx}",
            name=f"Anchor {idx}",
            name_normalized=f"anchor {idx}",
            category="supplies",
            canonical_id=match.id,
        )
        setup_db.add(product)
        matches.append(match)
        products.append(product)
    setup_db.add(
        storage.Product(
            tenant_id=2,
            site="aptekonline",
            external_id="foreign-anchor",
            url="https://aptekonline.example/foreign",
            name="Foreign anchor",
            name_normalized="foreign anchor",
            category="supplies",
            canonical_id=matches[1].id,
        )
    )
    setup_db.commit()

    r = client.get("/api/v1/dash/unmatched-pairs?site=aloe&category=supplies&limit=2&offset=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [row["match_id"] for row in body["items"]] == [matches[1].id, matches[0].id]
    assert body["items"][0]["anchor_products"] == [
        {
            "product_id": products[1].id,
            "site": "pharmonline",
            "name": "Anchor 1",
            "brand": None,
            "category": "supplies",
            "url": "https://pharmonline.example/1",
            "price": None,
        }
    ]
    assert all(
        anchor["site"] == "pharmonline"
        for row in body["items"]
        for anchor in row["anchor_products"]
    )


def test_dash_watchlist_categories_counts_are_site_scoped(client, auth_cookie, setup_db):
    run = storage.Run(status="ok", tenant_id=1)
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)
    cat = storage.Category(
        key="supplies",
        label_ru="Медицинские средства",
        pharmonline_slug="shared-slug",
        aloe_slug="aloe-supplies",
        is_active=True,
    )
    setup_db.add(cat)
    setup_db.commit()
    setup_db.refresh(cat)

    # This pharmonline product is in the tracked category.
    setup_db.add(
        storage.Product(
            tenant_id=1,
            site="pharmonline",
            external_id="ph-1",
            url="https://pharmonline.example/ph-1",
            name="Tracked",
            name_normalized="tracked",
            category="shared-slug",
        )
    )
    # Same raw category string on a different site must not be counted unless
    # that site is mapped to the same slug.
    setup_db.add(
        storage.Product(
            tenant_id=1,
            site="aloe",
            external_id="al-1",
            url="https://aloe.example/al-1",
            name="Unmapped",
            name_normalized="unmapped",
            category="shared-slug",
        )
    )
    setup_db.commit()

    created = client.post("/api/v1/dash/watchlist/categories", json={"category_id": cat.id})
    assert created.status_code == 201, created.text

    item = client.get("/api/v1/dash/watchlist/categories").json()[0]
    assert item["product_count"] == 1
    assert item["matched_product_count"] == 0
    assert item["comparison_count"] == 0


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


def test_match_confirm_rejects_known_country_conflict(
    client, tenant_user, setup_db
):
    s = setup_db
    match = _make_match_with_products(
        s, confidence=0.5, needs_review=True, canonical="Country conflict"
    )
    products = list(match.products)
    products[0].manufacturer_country_code = "ua"
    products[1].manufacturer_country_code = "rs"
    for product in products:
        product.country_resolution_status = "resolved"
    s.commit()
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")

    response = client.post(f"/api/v1/dash/matches/{match.id}/confirm")

    assert response.status_code == 409
    assert "different manufacturing countries" in response.text
    s.refresh(match)
    assert match.is_manual is False


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
    assert body["batch_id"] is not None


def test_cost_csv_preview_is_read_only_then_import_can_be_rolled_back(
    client, auth_cookie, setup_db
):
    product = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="ROLL-001",
        url="http://x/roll",
        name="Rollback product",
        name_normalized="rollback product",
    )
    setup_db.add(product)
    setup_db.flush()
    original = storage.SupplierPrice(
        product_id=product.id,
        sku="ROLL-001",
        supplier_name="Vendor",
        purchase_price=3.0,
        currency="AZN",
        source="erp",
    )
    setup_db.add(original)
    setup_db.commit()
    csv_body = (
        b"sku,supplier_name,purchase_price,currency\n"
        b"ROLL-001,Vendor,4.50,AZN\n"
    )

    preview = client.post(
        "/api/v1/dash/settings/costs/preview",
        files={"file": ("costs.csv", csv_body, "text/csv")},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["changes"][0]["before"]["purchase_price"] == 3.0
    setup_db.refresh(original)
    assert original.purchase_price == 3.0

    imported = client.post(
        "/api/v1/dash/settings/costs/import",
        files={"file": ("costs.csv", csv_body, "text/csv")},
    ).json()
    setup_db.refresh(original)
    assert original.purchase_price == 4.5
    history = client.get("/api/v1/dash/settings/costs/imports").json()
    assert history[0]["id"] == imported["batch_id"]
    assert history[0]["can_rollback"] is True

    rollback = client.post(
        f"/api/v1/dash/settings/costs/imports/{imported['batch_id']}/rollback"
    )
    assert rollback.status_code == 200, rollback.text
    setup_db.refresh(original)
    assert original.purchase_price == 3.0
    assert original.source == "erp"


def test_cost_endpoints_reject_viewer(client, setup_db):
    viewer = _make_user(setup_db, "cost-viewer@x.az", role="viewer")
    token = tenants.issue_magic_token(setup_db, viewer.email)
    client.get(f"/auth/verify?token={token}")
    body = b"sku,supplier_name,purchase_price\nX,V,1\n"

    assert client.put(
        "/api/v1/dash/settings/pricing",
        json={
            "raise_threshold_pct": 1,
            "undercut_threshold_pct": 1,
            "max_spread_pct": 50,
            "min_margin_pct": 5,
            "max_per_type": 5,
        },
    ).status_code == 403
    for endpoint in ("preview", "import"):
        response = client.post(
            f"/api/v1/dash/settings/costs/{endpoint}",
            files={"file": ("costs.csv", body, "text/csv")},
        )
        assert response.status_code == 403
    assert client.get("/api/v1/dash/settings/costs/imports").status_code == 403
    assert (
        client.post("/api/v1/dash/settings/costs/imports/1/rollback").status_code
        == 403
    )


def test_cost_preview_uses_only_client_site_for_duplicate_external_id(
    client, auth_cookie, setup_db
):
    pharm = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="SHARED-SKU",
        url="https://pharmonline.az/shared",
        name="Client product",
        name_normalized="client product",
    )
    competitor = storage.Product(
        tenant_id=1,
        site="aloe",
        external_id="SHARED-SKU",
        url="https://aloe.az/shared",
        name="Competitor product",
        name_normalized="competitor product",
    )
    setup_db.add_all([pharm, competitor])
    setup_db.commit()
    body = b"sku,supplier_name,purchase_price\nSHARED-SKU,V,2.5\n"

    preview = client.post(
        "/api/v1/dash/settings/costs/preview",
        files={"file": ("costs.csv", body, "text/csv")},
    ).json()

    assert preview["changes"][0]["product_id"] == pharm.id
    assert preview["changes"][0]["product_name"] == "Client product"


def test_dashboard_cost_then_erp_push_upserts_same_product_supplier(
    client, auth_cookie, setup_db
):
    product = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="DASH-ERP-1",
        url="https://pharmonline.az/dash-erp",
        name="Dashboard ERP product",
        name_normalized="dashboard erp product",
    )
    setup_db.add(product)
    setup_db.commit()
    dashboard_csv = b"sku,supplier_name,purchase_price\nDASH-ERP-1,Vendor,2.5\n"
    imported = client.post(
        "/api/v1/dash/settings/costs/import",
        files={"file": ("costs.csv", dashboard_csv, "text/csv")},
    )
    assert imported.status_code == 200, imported.text

    pushed = client.post(
        "/api/v1/inventory/prices",
        headers={"X-API-Key": "test-key-1234"},
        json=[
            {
                "sku": "DASH-ERP-1",
                "supplier_name": "Vendor",
                "purchase_price": 4.25,
                "currency": "AZN",
            }
        ],
    )

    assert pushed.status_code == 200, pushed.text
    setup_db.expire_all()
    rows = setup_db.scalars(
        select(storage.SupplierPrice).where(
            storage.SupplierPrice.product_id == product.id,
            storage.SupplierPrice.supplier_name == "Vendor",
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].purchase_price == 4.25
    assert rows[0].source == "api_erp"


def test_cost_import_rolls_back_everything_when_cache_invalidation_fails(
    client, auth_cookie, setup_db, monkeypatch
):
    from sqlalchemy.orm import Query

    product = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="ATOMIC-SKU",
        url="https://pharmonline.az/atomic",
        name="Atomic product",
        name_normalized="atomic product",
    )
    setup_db.add(product)
    setup_db.commit()
    original_delete = Query.delete

    def fail_roi_delete(query, *args, **kwargs):
        entity = query.column_descriptions[0].get("entity")
        if entity is storage.RoiActionsCache:
            raise RuntimeError("cache delete failed")
        return original_delete(query, *args, **kwargs)

    monkeypatch.setattr(Query, "delete", fail_roi_delete)
    body = b"sku,supplier_name,purchase_price\nATOMIC-SKU,V,2.5\n"
    with pytest.raises(RuntimeError, match="cache delete failed"):
        client.post(
            "/api/v1/dash/settings/costs/import",
            files={"file": ("costs.csv", body, "text/csv")},
        )
    setup_db.rollback()
    assert setup_db.scalar(
        select(storage.SupplierPrice).where(
            storage.SupplierPrice.product_id == product.id
        )
    ) is None
    assert setup_db.scalar(
        select(storage.CostImportBatch).where(
            storage.CostImportBatch.tenant_id == 1
        )
    ) is None


def test_supplier_price_unique_product_supplier_invariant(setup_db):
    product = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="UNIQUE-SKU",
        url="https://pharmonline.az/unique",
        name="Unique product",
        name_normalized="unique product",
    )
    setup_db.add(product)
    setup_db.flush()
    setup_db.add_all(
        [
            storage.SupplierPrice(
                product_id=product.id,
                supplier_name="V",
                purchase_price=1,
            ),
            storage.SupplierPrice(
                product_id=product.id,
                supplier_name="V",
                purchase_price=2,
            ),
        ]
    )
    with pytest.raises(Exception):
        setup_db.commit()
    setup_db.rollback()


def test_cost_import_lock_serializes_same_tenant(setup_db):
    import threading
    import time

    entered_first = threading.Event()
    release_first = threading.Event()
    entered_second = threading.Event()

    def first():
        with api_module.inv_mod.supplier_price_write_lock(setup_db, 1):
            entered_first.set()
            release_first.wait(timeout=2)

    def second():
        entered_first.wait(timeout=2)
        with api_module.inv_mod.supplier_price_write_lock(setup_db, 1):
            entered_second.set()

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    two.start()
    assert entered_first.wait(timeout=1)
    time.sleep(0.05)
    assert not entered_second.is_set()
    release_first.set()
    one.join(timeout=2)
    two.join(timeout=2)
    assert entered_second.is_set()


def test_concurrent_erp_write_is_serialized_against_dashboard_rollback(
    setup_db, tenant_user, monkeypatch
):
    import threading
    import time

    product = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="ERP-RB-1",
        url="https://pharmonline.az/erp-rb",
        name="ERP rollback product",
        name_normalized="erp rollback product",
    )
    setup_db.add(product)
    setup_db.flush()
    setup_db.add(
        storage.SupplierPrice(
            product_id=product.id,
            sku="ERP-RB-1",
            supplier_name="Vendor",
            purchase_price=2.5,
            currency="AZN",
            source="dashboard_csv",
        )
    )
    batch = storage.CostImportBatch(
        tenant_id=1,
        actor_user_id=tenant_user.id,
        rows_processed=1,
        rows_imported=1,
        rows_skipped=0,
        changes=[
            {
                "product_id": product.id,
                "supplier_name": "Vendor",
                "before": None,
                "after": {
                    "purchase_price": 2.5,
                    "currency": "AZN",
                    "source": "dashboard_csv",
                    "sku": "ERP-RB-1",
                },
            }
        ],
    )
    setup_db.add(batch)
    setup_db.commit()

    SessionLocal = sessionmaker(setup_db.get_bind(), expire_on_commit=False)
    erp_db = SessionLocal()
    rollback_db = SessionLocal()
    erp_entered = threading.Event()
    release_erp = threading.Event()
    rollback_done = threading.Event()
    failures: list[BaseException] = []
    original_find = api_module.inv_mod._find_product_by_sku_or_name

    def blocking_find(*args, **kwargs):
        erp_entered.set()
        release_erp.wait(timeout=2)
        return original_find(*args, **kwargs)

    monkeypatch.setattr(
        api_module.inv_mod, "_find_product_by_sku_or_name", blocking_find
    )

    def erp_write():
        try:
            api_module.push_prices(
                [
                    api_module.PurchasePriceIn(
                        sku="ERP-RB-1",
                        supplier_name="Vendor",
                        purchase_price=4.0,
                        currency="AZN",
                    )
                ],
                source="api_erp",
                db=erp_db,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def rollback():
        try:
            api_module.dash_cost_import_rollback(
                batch.id, user=tenant_user, db=rollback_db
            )
        except BaseException as exc:
            failures.append(exc)
        finally:
            rollback_done.set()

    erp_thread = threading.Thread(target=erp_write)
    rollback_thread = threading.Thread(target=rollback)
    erp_thread.start()
    assert erp_entered.wait(timeout=1)
    rollback_thread.start()
    time.sleep(0.05)
    assert not rollback_done.is_set()
    release_erp.set()
    erp_thread.join(timeout=2)
    rollback_thread.join(timeout=2)
    erp_db.close()
    rollback_db.close()

    assert rollback_done.is_set()
    assert len(failures) == 1
    assert getattr(failures[0], "status_code", None) == 409
    setup_db.expire_all()
    current = setup_db.scalar(
        select(storage.SupplierPrice).where(
            storage.SupplierPrice.product_id == product.id,
            storage.SupplierPrice.supplier_name == "Vendor",
        )
    )
    assert current is not None
    assert current.purchase_price == 4.0
    assert current.source == "api_erp"


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


def test_mapping_create_keeps_az_category_views_free_of_russian(
    client,
    auth_cookie,
    setup_db,
):
    created = client.post(
        "/api/v1/dash/categories/mapping",
        json={
            "site_a": "pharmonline",
            "site_a_slug": "vitamin-kompleksi",
            "site_b": "aloe",
            "site_b_slug": "vitaminler",
            "label_ru": "Витаминный комплекс",
        },
    )
    assert created.status_code == 201, created.text
    cat = setup_db.get(storage.Category, created.json()["id"])
    assert cat is not None
    assert cat.label_az == "Vitamin kompleksi"

    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    setup_db.add(run)
    setup_db.flush()
    _make_match_with_prices(
        setup_db,
        run,
        canonical="vitamin-mapped",
        prices={"pharmonline": 10.0, "aloe": 9.0},
        category="vitamin-kompleksi",
    )
    setup_db.commit()

    response = client.get("/api/v1/dash/category-comparison?locale=az")
    assert response.status_code == 200, response.text
    rows = {row["category"]: row for row in response.json()}
    assert rows["vitamins_supplements"]["label"] == "Vitaminlər, BFƏ və təbii vasitələr"
    assert "Витаминный" not in rows["vitamins_supplements"]["label"]


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
    assert set(rows) == {"vitamins_supplements", "pain_musculoskeletal"}
    assert rows["vitamins_supplements"]["index"] == 125.0  # клиент 10 / конкурент 8
    assert rows["vitamins_supplements"]["per_site_avg"] == {"aloe": 8.0}
    assert rows["vitamins_supplements"]["pricier_count"] == 1
    assert rows["pain_musculoskeletal"]["index"] == 100.0
    assert rows["vitamins_supplements"]["label"] == "Витамины, БАД и натуральные средства"


def test_category_comparison_tenant_isolation(client, tenant_user, setup_db):
    """Категории чужого тенанта не видны."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    s = setup_db
    run = storage.Run(tenant_id=1, started_at=utcnow(), status="ok")
    run2 = storage.Run(tenant_id=2, started_at=utcnow(), status="ok")
    s.add_all([run, run2])
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
        run2,
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
    assert cats == {"vitamins_supplements"}  # тенант 2's "secret" исключён


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


def test_match_create_with_products_rejects_explicit_oos(
    client, tenant_user, setup_db
):
    s = setup_db
    now = utcnow()
    available = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="manual-active",
        url="https://pharm.example/manual-active",
        name="Manual active",
        name_normalized="manual active",
        offer_availability_status="in_stock",
        availability_observed_at=now,
    )
    unavailable = storage.Product(
        tenant_id=1,
        site="aptekonline",
        external_id="manual-oos",
        url="https://aptek.example/manual-oos",
        name="Manual unavailable",
        name_normalized="manual unavailable",
        offer_availability_status="out_of_stock",
        availability_observed_at=now,
    )
    s.add_all([available, unavailable])
    s.commit()
    token = tenants.issue_magic_token(s, tenant_user.email)
    client.get(f"/auth/verify?token={token}")

    response = client.post(
        "/api/v1/dash/matches/create-with-products",
        json={"product_ids": [available.id, unavailable.id]},
    )

    assert response.status_code == 409
    assert "out of stock" in response.text
    assert available.canonical_id is None
    assert unavailable.canonical_id is None


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


def test_scrape_complete_preserves_degraded_terminal_status(client, setup_db):
    run = storage.Run(tenant_id=1, status="degraded", finished_at=utcnow())
    setup_db.add(run)
    setup_db.flush()
    request_row = storage.ScrapeRequest(
        tenant_id=1,
        mode="all",
        status="degraded",
        run_id=run.id,
        completed_at=utcnow(),
        error_message="aloe=degraded(incomplete_items)",
    )
    setup_db.add(request_row)
    setup_db.commit()

    response = client.post(
        f"/api/v1/internal/scrape-complete/{request_row.id}",
        headers={"X-API-Key": "test-key-1234"},
        json={"run_id": run.id},
    )
    assert response.status_code == 200
    assert response.json()["noop"] == "already degraded"
    setup_db.refresh(request_row)
    assert request_row.status == "degraded"
    assert request_row.error_message == "aloe=degraded(incomplete_items)"


def test_run_endpoints_serialize_quality(client, auth_cookie, setup_db):
    run = storage.Run(
        tenant_id=1,
        status="degraded",
        finished_at=utcnow(),
        products_scraped=5,
        products_per_site={"aloe": 5},
        products_per_site_category={"aloe": {"cat": 5}},
        run_quality={
            "version": 1,
            "mode": "category",
            "financially_eligible": False,
            "sites": {
                "aloe": {
                    "status": "degraded",
                    "items_expected": 1,
                    "items": {"dermanlar": {"status": "ok", "products": 5}},
                }
            },
        },
    )
    setup_db.add(run)
    setup_db.commit()

    rows = client.get("/api/v1/dash/runs?limit=1").json()
    assert rows[0]["run_quality"]["sites"]["aloe"]["status"] == "degraded"
    assert rows[0]["run_quality"]["sites"]["aloe"]["items_expected"] == 1
    assert "items" not in rows[0]["run_quality"]["sites"]["aloe"]
    detail = client.get(f"/api/v1/dash/runs/{run.id}/breakdown").json()
    assert detail["run_quality"]["financially_eligible"] is False
    assert detail["run_quality"]["sites"]["aloe"]["items"]["dermanlar"]["products"] == 5


def test_run_breakdown_is_tenant_scoped(client, auth_cookie, setup_db):
    foreign_run = storage.Run(
        tenant_id=2,
        status="degraded",
        run_quality={"sites": {"aloe": {"items": {"secret-url": {}}}}},
    )
    setup_db.add(foreign_run)
    setup_db.commit()

    response = client.get(f"/api/v1/dash/runs/{foreign_run.id}/breakdown")
    assert response.status_code == 404


def test_scrape_complete_rejects_cross_tenant_run(client, setup_db):
    request_row = storage.ScrapeRequest(tenant_id=2, mode="all", status="running")
    foreign_run = storage.Run(tenant_id=1, status="ok")
    setup_db.add_all([request_row, foreign_run])
    setup_db.commit()

    response = client.post(
        f"/api/v1/internal/scrape-complete/{request_row.id}",
        headers={"X-API-Key": "test-key-1234"},
        json={"run_id": foreign_run.id},
    )
    assert response.status_code == 400
    setup_db.refresh(request_row)
    assert request_row.status == "running"
    assert request_row.run_id is None


def test_pending_scrape_worker_ignores_non_pilot_tenant(client, setup_db):
    foreign_request = storage.ScrapeRequest(tenant_id=2, mode="all", status="pending")
    pilot_request = storage.ScrapeRequest(tenant_id=1, mode="all", status="pending")
    setup_db.add_all([foreign_request, pilot_request])
    setup_db.commit()

    response = client.get(
        "/api/v1/internal/pending-scrape",
        headers={"X-API-Key": "test-key-1234"},
    )
    assert response.status_code == 200
    assert response.json()["pending"]["id"] == pilot_request.id
    setup_db.refresh(foreign_request)
    assert foreign_request.status == "pending"


def test_roi_actions_refuses_inline_compute_without_verified_cache(
    client, auth_cookie
):
    response = client.get("/api/v1/dash/roi/actions")
    assert response.status_code == 503
    assert "Verified full-catalog" in response.json()["detail"]


def test_roi_status_is_unavailable_without_verified_cache(client, auth_cookie):
    response = client.get("/api/v1/dash/roi/status")

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "client_site": "pharmonline",
        "run_id": None,
        "computed_at": None,
        "run_started_at": None,
        "run_finished_at": None,
        "item_count": 0,
    }
    recommendations = client.get("/api/v1/dash/roi/recommendations")
    assert recommendations.status_code == 503
    assert "Verified full-catalog" in recommendations.json()["detail"]


def test_roi_actions_serves_cache_from_financially_eligible_run(
    client, auth_cookie, setup_db
):
    from src.product_policy import policy_fingerprint, trusted_catalog_epoch

    run = _add_policy_ready_catalog(setup_db)
    epoch = trusted_catalog_epoch(setup_db)
    assert epoch is not None
    setup_db.add(
        storage.RoiActionsCache(
            tenant_id=1,
            client_site="pharmonline",
            run_id=run.id,
            computed_at=utcnow(),
            payload=[],
            policy_fingerprint=policy_fingerprint(),
            trust_epoch=epoch,
        )
    )
    setup_db.commit()
    response = client.get("/api/v1/dash/roi/actions")
    assert response.status_code == 200
    assert response.json() == []

    status = client.get("/api/v1/dash/roi/status").json()
    assert status["available"] is True
    assert status["run_id"] == run.id
    assert status["item_count"] == 0
    assert status["computed_at"] is not None
    assert status["run_finished_at"] is not None

    recommendations = client.get("/api/v1/dash/roi/recommendations").json()
    assert recommendations["items"] == []
    assert recommendations["provenance"]["run_id"] == run.id
    assert recommendations["provenance"]["item_count"] == 0


def test_roi_actions_rejects_cache_after_newer_degraded_full_run(
    client, auth_cookie, setup_db
):
    from src.product_policy import policy_fingerprint, trusted_catalog_epoch

    cached_run = storage.Run(
        tenant_id=1,
        started_at=utcnow() - timedelta(hours=2),
        finished_at=utcnow() - timedelta(hours=1),
        status="ok",
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {
                "pharmonline": {"status": "ok"},
                "aptekonline": {"status": "ok"},
                "aloe": {"status": "ok"},
            },
        },
    )
    setup_db.add(cached_run)
    setup_db.flush()
    epoch = trusted_catalog_epoch(setup_db)
    assert epoch is not None
    setup_db.add(
        storage.RoiActionsCache(
            tenant_id=1,
            client_site="pharmonline",
            run_id=cached_run.id,
            computed_at=utcnow(),
            payload=[],
            policy_fingerprint=policy_fingerprint(),
            trust_epoch=epoch,
        )
    )
    setup_db.add(
        storage.Run(
            tenant_id=1,
            started_at=utcnow() - timedelta(minutes=30),
            finished_at=utcnow(),
            status="degraded",
            catalog_scope="full",
            full_catalog_sites="pharmonline,aptekonline,aloe",
            catalog_verified=False,
            run_quality={
                "baseline_enforced": True,
                "full_catalog_verified": False,
                "financially_eligible": False,
                "sites": {"pharmonline": {"status": "degraded"}},
            },
        )
    )
    setup_db.add(
        storage.Run(
            tenant_id=1,
            started_at=utcnow() - timedelta(minutes=10),
            finished_at=utcnow() + timedelta(seconds=1),
            status="ok",
            catalog_scope="full",
            full_catalog_sites="aloe",
            catalog_verified=True,
            run_quality={
                "baseline_enforced": True,
                "full_catalog_verified": True,
                "financially_eligible": True,
                "sites": {"aloe": {"status": "ok"}},
            },
        )
    )
    setup_db.commit()

    response = client.get("/api/v1/dash/roi/actions")

    assert response.status_code == 503
    assert "Verified full-catalog" in response.json()["detail"]
    assert client.get("/api/v1/dash/roi/status").json()["available"] is False
    assert client.get("/api/v1/dash/roi/recommendations").status_code == 503


def test_normalize_stats_uses_product_units_and_tenant_scope(
    client, auth_cookie, setup_db
):
    own_match = storage.Match(
        tenant_id=1,
        canonical_name="Review own",
        confidence=0.5,
        needs_review=True,
    )
    foreign_match = storage.Match(
        tenant_id=2,
        canonical_name="Review foreign",
        confidence=0.5,
        needs_review=True,
    )
    setup_db.add_all([own_match, foreign_match])
    setup_db.flush()
    setup_db.add_all(
        [
            storage.Product(
                tenant_id=1,
                site="pharmonline",
                external_id="trust-own-1",
                url="https://example.test/own-1",
                name="Own one",
                name_normalized="own one",
                canonical_id=own_match.id,
            ),
            storage.Product(
                tenant_id=1,
                site="aloe",
                external_id="trust-own-2",
                url="https://example.test/own-2",
                name="Own two",
                name_normalized="own two",
                canonical_id=own_match.id,
            ),
            storage.Product(
                tenant_id=2,
                site="aptekonline",
                external_id="trust-foreign",
                url="https://example.test/foreign",
                name="Foreign",
                name_normalized="foreign",
                canonical_id=foreign_match.id,
            ),
        ]
    )
    setup_db.commit()

    response = client.get("/api/v1/dash/normalize/stats")

    assert response.status_code == 200
    body = response.json()
    assert body["products_total"] == 2
    assert body["matches_needing_review"] == 1
    assert body["products_needing_review"] == 2
    assert body["needs_review"] == 1
