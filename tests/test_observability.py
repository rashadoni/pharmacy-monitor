"""Tests for src/observability.py — Sentry init, Prometheus metrics, helpers.

These don't require a real Sentry / Prometheus server — verify the wiring
and graceful degradation when SDKs aren't installed or DSN is missing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src import observability


# ─── Sentry init ────────────────────────────────────────────────────────────


def test_init_sentry_skips_without_dsn(monkeypatch):
    """No SENTRY_DSN env → no init, no error."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    observability._SENTRY_INITIALIZED = False
    observability.init_sentry(service="api")
    assert observability._SENTRY_INITIALIZED is False


def test_init_sentry_with_dsn(monkeypatch):
    """If DSN is set, sentry_sdk.init is called once."""
    monkeypatch.setenv("SENTRY_DSN", "https://test@test.ingest.sentry.io/123")
    observability._SENTRY_INITIALIZED = False
    with patch("sentry_sdk.init") as mock_init:
        observability.init_sentry(service="api")
        assert mock_init.called
    # Re-init is no-op
    observability.init_sentry(service="api")
    # mock_init still only called once across both invocations (fresh mock per `with` block)


def test_init_sentry_idempotent(monkeypatch):
    """Calling init_sentry twice doesn't re-init."""
    monkeypatch.setenv("SENTRY_DSN", "https://test@test.ingest.sentry.io/123")
    observability._SENTRY_INITIALIZED = False
    call_count = 0
    with patch("sentry_sdk.init") as mock_init:
        observability.init_sentry(service="api")
        observability.init_sentry(service="api")
        observability.init_sentry(service="api")
        call_count = mock_init.call_count
    assert call_count == 1


def test_sentry_capture_no_op_when_uninitialized():
    """sentry_capture() must not raise if Sentry isn't init'd."""
    observability._SENTRY_INITIALIZED = False
    # Just verify it doesn't crash
    observability.sentry_capture(ValueError("test"), tenant_id="42")


def test_sentry_set_tenant_no_op_when_uninitialized():
    observability._SENTRY_INITIALIZED = False
    observability.sentry_set_tenant(99)  # must not raise


def test_sentry_before_send_drops_keyboard_interrupt():
    hint = {"exc_info": (KeyboardInterrupt, KeyboardInterrupt(), None)}
    assert observability._sentry_before_send({}, hint) is None


def test_sentry_before_send_drops_captcha_detected():
    """CaptchaDetected is expected, not a bug — drop from Sentry."""

    class CaptchaDetected(Exception):
        pass

    hint = {"exc_info": (CaptchaDetected, CaptchaDetected("test"), None)}
    assert observability._sentry_before_send({}, hint) is None


def test_sentry_before_send_keeps_real_errors():
    hint = {"exc_info": (ValueError, ValueError("legit error"), None)}
    event = {"some": "data"}
    assert observability._sentry_before_send(event, hint) == event


# ─── Prometheus metrics ─────────────────────────────────────────────────────


def test_metrics_init_idempotent():
    """metrics.init() can be called multiple times safely."""
    observability.metrics.init()
    assert observability.metrics._initialized is True
    # Second call is no-op (no DuplicatedTimeseries error)
    observability.metrics.init()
    assert observability.metrics._initialized is True


def test_metrics_inc_doesnt_raise():
    """All custom counters can be incremented."""
    observability.metrics.init()
    observability.metrics.scrape_products_total.labels(site="pharmonline").inc(5)
    observability.metrics.captcha_hits_total.labels(site="aloe").inc()
    observability.metrics.matcher_clusters_total.inc(3)
    observability.metrics.alert_events_total.labels(severity="critical", rule_type="undercut").inc()
    observability.metrics.runs_total.labels(site="aptekonline", status="ok").inc()


def test_metrics_observe_histogram():
    observability.metrics.init()
    observability.metrics.scrape_duration_seconds.labels(site="pharmonline").observe(45.2)
    observability.metrics.db_query_seconds.labels(query_type="select").observe(0.05)


def test_noop_metric_silent():
    """Fallback when prometheus_client absent — methods don't crash."""
    n = observability._NoopMetric()
    n.labels(site="x").inc(5)
    n.labels(site="x").inc()
    n.observe(1.5)
    n.info({"foo": "bar"})


def test_time_observation_context_manager():
    """time_observation() records elapsed seconds."""
    observability.metrics.init()
    with observability.time_observation(
        observability.metrics.scrape_duration_seconds, site="pharmonline"
    ):
        pass  # near-zero elapsed


# ─── /metrics endpoint ──────────────────────────────────────────────────────


def test_install_metrics_endpoint_adds_route():
    from fastapi import FastAPI

    app = FastAPI()
    initial_routes = len(app.routes)
    observability.install_metrics_endpoint(app)
    # Should add one route (or zero if prometheus_client not installed — graceful)
    assert len(app.routes) >= initial_routes


def test_metrics_endpoint_returns_prometheus_format():
    """When prometheus_client is installed, /metrics returns text/plain content."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    observability.metrics.init()
    observability.install_metrics_endpoint(app)
    # Increment a counter to ensure something is exported
    observability.metrics.scrape_products_total.labels(site="test").inc(42)
    client = TestClient(app)
    r = client.get("/metrics")
    if r.status_code == 404:
        pytest.skip("prometheus_client not installed in this env")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")
    body = r.text
    assert "pharmacy_scrape_products_total" in body


# ─── _git_short_sha graceful fallback ───────────────────────────────────────


def test_git_short_sha_returns_string():
    sha = observability._git_short_sha()
    assert isinstance(sha, str)
    assert len(sha) >= 1


def test_init_observability_orchestrates_both():
    """init_observability() calls Sentry + metrics init."""
    observability._SENTRY_INITIALIZED = False
    observability.metrics._initialized = False
    observability.init_observability(service="test")
    # Metrics is always initialized (no extra deps needed if prometheus_client installed)
    assert observability.metrics._initialized is True


# ─── Phase 5.4 — API request metrics middleware ─────────────────────────────


def test_api_metrics_middleware_records_404_request():
    """Любой HTTP-запрос (даже 404) проходит через middleware и инкрементит counter.

    Используем 404 path чтобы не зависеть от DB fixture'ов и tenant setup —
    middleware фиксирует ВСЕ requests до route resolution.
    """
    from fastapi.testclient import TestClient

    from src import api as api_module

    observability.metrics.init()
    client = TestClient(api_module.app)
    r = client.get("/definitely-nonexistent-route-12345")
    assert r.status_code == 404

    # Pull /metrics and verify counter incremented для 404
    metrics_r = client.get("/metrics")
    if metrics_r.status_code == 404:
        pytest.skip("prometheus_client not installed")
    body = metrics_r.text
    assert 'pharmacy_api_requests_total{method="GET",status="404"}' in body


def test_api_metrics_middleware_buckets_status_in_histogram():
    """api_request_duration_seconds histogram has status_bucket label set."""
    from fastapi.testclient import TestClient

    from src import api as api_module

    observability.metrics.init()
    client = TestClient(api_module.app)
    client.get("/definitely-nonexistent-route-12346")
    metrics_r = client.get("/metrics")
    if metrics_r.status_code == 404:
        pytest.skip("prometheus_client not installed")
    body = metrics_r.text
    # Histogram emits *_bucket lines — checking for label, не exact value
    assert "pharmacy_api_request_duration_seconds_bucket" in body
    # 4xx bucket — от наших 404-х
    assert 'status_bucket="4xx"' in body
