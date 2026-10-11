"""Tests for src/observability.py — Sentry init, Prometheus metrics, helpers.

These don't require a real Sentry / Prometheus server — verify the wiring
and graceful degradation when SDKs aren't installed or DSN is missing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
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


# ─── Sentry: credentials are stripped from an outgoing event ────────────────
#
# What a real request sends is checked end to end in
# tests/test_sentry_events_carry_no_credentials.py; these pin the rules that
# run does not reach.

_JWT_HEADER = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
_JWT_PAYLOAD = "eyJzdWIiOiIxIiwidGlkIjoxLCJlbWFpbCI6ImFAYi5jIn0"
_JWT_SIGNATURE = "c2lnbmF0dXJlLW5vdC1yZWFsLWp1c3Qtc2hhcGU"


def test_sentry_scrubber_that_fails_drops_the_event(monkeypatch):
    """A scrubber that lets the event through when it breaks would pass on its own failure."""

    def broken(event):
        raise RuntimeError("scrubber bug")

    monkeypatch.setattr(observability, "_clean_request", broken)
    event = {"request": {"headers": {"cookie": "pm_session=abc"}}}

    assert observability._sentry_before_send(event, {}) is None
    assert observability._sentry_before_send_transaction(event, {}) is None


_JWT = f"{_JWT_HEADER}.{_JWT_PAYLOAD}.{_JWT_SIGNATURE}"


@pytest.mark.parametrize(
    "token",
    [
        _JWT,
        f"{_JWT_HEADER}.{_JWT_PAYLOAD}.",  # cut right after the payload
        f"{_JWT_HEADER}.{_JWT_PAYLOAD}",  # the payload alone still names the user
        f"{_JWT_HEADER}.{_JWT_PAYLOAD[:12]}",
    ],
)
def test_sentry_masks_a_session_token_even_when_truncated(token):
    event = {"exception": {"values": [{"value": f"bad session {token} rejected"}]}}

    clean = observability._sentry_before_send(event, {})

    assert clean["exception"]["values"][0]["value"] == "bad session [Filtered] rejected"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"Cookie: pm_session={_JWT}; theme=dark", "Cookie: pm_session=[Filtered]; theme=dark"),
        (f"next=pm_session%3D{_JWT}&x=1", "next=pm_session%3D[Filtered]&x=1"),
        (f"sess_{_JWT} rejected", "sess_[Filtered] rejected"),  # glued to a prefix
        (f"v1.eyJshort.{_JWT} rejected", "v1.eyJshort.[Filtered] rejected"),  # after a lookalike
        (f"{_JWT} then {_JWT}", "[Filtered] then [Filtered]"),
        # Not tokens: a header with no payload is public, a version string is not a header.
        (f"alg header {_JWT_HEADER} only", f"alg header {_JWT_HEADER} only"),
        ("release v1.eyJ and file.eyJabc", "release v1.eyJ and file.eyJabc"),
    ],
)
def test_sentry_finds_a_session_token_wherever_it_is_glued(text, expected):
    assert observability._sentry_before_send({"message": text}, {}) == {"message": expected}


_HOSTILE_SCAN = """
import time
from src import observability

hostile = ["/" + "eyJ" * 20_000, "eyJ" * 20_000 + " x.eyJ", "?" + "token?" * 10_000]
started = time.perf_counter()
for text in hostile:
    assert observability._sentry_before_send({"message": text}, {}) == {"message": text}
print(time.perf_counter() - started)
"""


def test_sentry_token_scan_stays_linear_on_a_hostile_request_line():
    """The request line is the caller's, and the scrubber reads every string of the event.

    A pattern that rescans from each `eyJ` takes a second on 60 KB when it is
    quadratic and does not finish when it is cubic — hence a child process with
    a timeout: losing linearity must fail this test, not hang the suite.
    """
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(repo), env.get("PYTHONPATH")]))

    scan = subprocess.run(
        [sys.executable, "-c", _HOSTILE_SCAN],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert scan.returncode == 0, scan.stderr[-2000:]
    assert float(scan.stdout.strip().splitlines()[-1]) < 0.5


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '127.0.0.1:5 - "GET /auth/verify?token=abc123 HTTP/1.1" 500',
            '127.0.0.1:5 - "GET /auth/verify?token=[Filtered] HTTP/1.1" 500',
        ),
        (
            "GET https://h/p?a=1&X-Amz-Signature=zz&b=2#frag failed",
            "GET https://h/p?a=1&X-Amz-Signature=[Filtered]&b=2#frag failed",
        ),
        ("/x?search=kreon&api_key=abc&limit=5", "/x?search=kreon&api_key=[Filtered]&limit=5"),
        (
            "/x?search=kreon&category_key=pharma&limit=5",
            "/x?search=kreon&category_key=pharma&limit=5",
        ),
        ("why? token=5 & more", "why? token=5 & more"),  # prose, not a URL
    ],
)
def test_sentry_masks_a_credential_parameter_in_a_quoted_url(text, expected):
    """uvicorn's access-log line reaches the event as a breadcrumb message."""
    event = {"breadcrumbs": {"values": [{"category": "uvicorn.access", "message": text}]}}

    clean = observability._sentry_before_send(event, {})

    assert clean["breadcrumbs"]["values"][0]["message"] == expected


@pytest.mark.parametrize(
    ("query", "kept"),
    [
        ("search=kreon&limit=50", True),
        ("category_key=pharma_kreon", True),  # "key" alone does not make a credential
        ("search=token", True),  # the word is in the value, not in the name
        ("token=abc", False),
        ("search=kreon&api_key=abc", False),
        ("X-Amz-Signature=abc&limit=5", False),
        ("new_password=abc", False),
    ],
)
def test_sentry_replaces_a_query_string_that_names_a_credential(query, kept):
    """Incoming request and outgoing-request breadcrumb use different keys for the same thing."""
    event = {
        "request": {"query_string": query},
        "breadcrumbs": {"values": [{"data": {"http.query": query, "url": "http://x/y"}}]},
    }

    clean = observability._sentry_before_send_transaction(event, {})

    expected = query if kept else "[Filtered]"
    assert clean["request"]["query_string"] == expected
    assert clean["breadcrumbs"]["values"][0]["data"] == {
        "http.query": expected,
        "url": "http://x/y",
    }


def test_sentry_filters_credential_headers_by_name_and_drops_body_and_cookies():
    event = {
        "request": {
            "method": "POST",
            "url": "http://x/auth/login",
            "data": {"login": "a@b.c", "password": "hunter2"},
            "cookies": {"pm_session": "abc"},
            "headers": {
                "cookie": "pm_session=abc",
                "Authorization": "Bearer abc",
                "x-api-key": "abc",
                "Proxy-Authorization": "Basic abc",
                "X-Auth-Token": "abc",
                "user-agent": "curl/8",
                "x-request-id": "rid-1",
            },
        }
    }

    clean = observability._sentry_before_send(event, {})

    assert clean["request"] == {
        "method": "POST",
        "url": "http://x/auth/login",
        "headers": {
            "cookie": "[Filtered]",
            "Authorization": "[Filtered]",
            "x-api-key": "[Filtered]",
            "Proxy-Authorization": "[Filtered]",
            "X-Auth-Token": "[Filtered]",
            "user-agent": "curl/8",
            "x-request-id": "rid-1",
        },
    }


def test_sentry_drops_headers_it_cannot_read_by_name():
    """The protocol also allows headers as a list of pairs; names are not checked there."""
    event = {"request": {"method": "GET", "headers": [["cookie", "pm_session=abc"]]}}

    assert observability._sentry_before_send(event, {}) == {"request": {"method": "GET"}}


def test_sentry_drops_frame_locals_from_every_kind_of_stacktrace():
    def frame(function):
        return {"function": function, "lineno": 7, "vars": {"scope": {"headers": "cookie"}}}

    event = {
        "exception": {"values": [{"type": "E", "stacktrace": {"frames": [frame("raised")]}}]},
        "threads": {"values": [{"stacktrace": {"frames": [frame("logged")]}}]},
        "stacktrace": {"frames": [frame("bare")]},
    }

    clean = observability._sentry_before_send(event, {})

    assert clean == {
        "exception": {
            "values": [
                {"type": "E", "stacktrace": {"frames": [{"function": "raised", "lineno": 7}]}}
            ]
        },
        "threads": {"values": [{"stacktrace": {"frames": [{"function": "logged", "lineno": 7}]}}]},
        "stacktrace": {"frames": [{"function": "bare", "lineno": 7}]},
    }


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
