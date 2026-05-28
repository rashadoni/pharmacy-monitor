"""Observability: Sentry + Prometheus + structured logs.

Single-import setup for both FastAPI and CLI/scraper processes:

    from src.observability import init_observability, metrics

    init_observability(service="api")  # or "scraper" / "cron"
    metrics.scrape_products_total.labels(site="pharmonline").inc(231)

Public:
  - init_observability(service)        — call once at process start
  - metrics                            — namespace with all custom metrics
  - sentry_capture(exc, **tags)        — explicit error capture with tags
  - install_metrics_endpoint(app)      — wire /metrics route to FastAPI app

Metrics (Prometheus naming convention):
  - pharmacy_scrape_duration_seconds   Histogram  labels: site
  - pharmacy_scrape_products_total     Counter    labels: site
  - pharmacy_scrape_failures_total     Counter    labels: site, reason
  - pharmacy_captcha_hits_total        Counter    labels: site
  - pharmacy_matcher_clusters_total    Counter
  - pharmacy_alert_events_total        Counter    labels: severity, rule_type
  - pharmacy_runs_total                Counter    labels: site, status
  - pharmacy_db_query_seconds          Histogram  labels: query_type
  - pharmacy_app_info                  Info       version, env
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

import structlog

log = structlog.get_logger()

# ─── Sentry ─────────────────────────────────────────────────────────────────

_SENTRY_INITIALIZED = False


def init_sentry(service: str) -> None:
    """Initialize Sentry SDK if SENTRY_DSN is set. Idempotent."""
    global _SENTRY_INITIALIZED
    if _SENTRY_INITIALIZED:
        return
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        log.debug("sentry_skipped", reason="no SENTRY_DSN")
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        env = os.environ.get("SENTRY_ENVIRONMENT", "production")
        release = os.environ.get("SENTRY_RELEASE") or _git_short_sha()

        sentry_sdk.init(
            dsn=dsn,
            environment=env,
            release=release,
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            profiles_sample_rate=float(os.environ.get("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
            integrations=[FastApiIntegration(), SqlalchemyIntegration()],
            send_default_pii=False,  # don't send IP/cookies by default
            attach_stacktrace=True,
            before_send=_sentry_before_send,
        )
        sentry_sdk.set_tag("service", service)
        log.info("sentry_initialized", service=service, env=env, release=release[:8])
        _SENTRY_INITIALIZED = True
    except ImportError:
        log.warning("sentry_sdk_not_installed")


def _sentry_before_send(event: dict, hint: dict) -> dict | None:
    """Filter sensitive data + low-noise errors before sending to Sentry.

    Drop if: keyboard interrupt, client-disconnected, expected captcha.
    """
    exc_info = hint.get("exc_info")
    if exc_info:
        exc_type = exc_info[0].__name__ if exc_info[0] else ""
        # Drop noise — these aren't bugs we need to know about
        if exc_type in ("KeyboardInterrupt", "SystemExit", "ConnectionResetError"):
            return None
        # Captcha is expected, log only
        if "CaptchaDetected" in exc_type:
            return None
    return event


def _git_short_sha() -> str:
    """Return short git SHA for release tagging (or 'unknown')."""
    try:
        import subprocess

        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def sentry_capture(exc: BaseException, **tags: str) -> None:
    """Explicitly capture an exception with custom tags."""
    if not _SENTRY_INITIALIZED:
        return
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            for k, v in tags.items():
                scope.set_tag(k, str(v))
            sentry_sdk.capture_exception(exc)
    except ImportError:
        pass


def sentry_set_tenant(tenant_id: int) -> None:
    """Set tenant_id as Sentry tag for the current request scope."""
    if not _SENTRY_INITIALIZED:
        return
    try:
        import sentry_sdk

        sentry_sdk.set_tag("tenant_id", tenant_id)
    except ImportError:
        pass


def sentry_set_request_id(request_id: str) -> None:
    """Set request_id as Sentry tag for the current request scope.

    Lets us cross-reference Sentry events with backend structlog lines and the
    `X-Request-ID` header surfaced to the frontend.
    """
    if not _SENTRY_INITIALIZED:
        return
    try:
        import sentry_sdk

        sentry_sdk.set_tag("request_id", request_id)
    except ImportError:
        pass


# ─── Prometheus metrics ─────────────────────────────────────────────────────


class _Metrics:
    """Lazy-init Prometheus metrics. Available after `init_observability()`.

    Why lazy: prometheus_client may not be installed in dev/test envs, and
    we don't want imports to crash. All counters use no-op fallback if missing.
    """

    def __init__(self) -> None:
        self._initialized = False
        # Placeholders — replaced on first init_metrics() call
        self.scrape_duration_seconds: any = _NoopMetric()
        self.scrape_products_total: any = _NoopMetric()
        self.scrape_failures_total: any = _NoopMetric()
        self.captcha_hits_total: any = _NoopMetric()
        self.matcher_clusters_total: any = _NoopMetric()
        self.alert_events_total: any = _NoopMetric()
        self.runs_total: any = _NoopMetric()
        self.db_query_seconds: any = _NoopMetric()
        self.app_info: any = _NoopMetric()
        # Phase 5.4 (2026-05-28) — HTTP-level metrics for API alerting rules.
        # Labels: status (2xx/3xx/4xx/5xx bucket — высокая cardinality для exact
        # code'ов исключена), endpoint (path или route name).
        self.api_requests_total: any = _NoopMetric()
        self.api_request_duration_seconds: any = _NoopMetric()

    def init(self) -> None:
        if self._initialized:
            return
        try:
            from prometheus_client import Counter, Histogram, Info, REGISTRY

            # If a previous test/process registered same metric name, skip re-create
            existing = {m._name for m in REGISTRY._collector_to_names.keys() if hasattr(m, "_name")}
            if "pharmacy_scrape_duration_seconds" in existing:
                self._initialized = True
                return

            self.scrape_duration_seconds = Histogram(
                "pharmacy_scrape_duration_seconds",
                "Time spent scraping a single site",
                ["site"],
                buckets=(10, 30, 60, 180, 300, 600, 1800, 3600, 7200),
            )
            self.scrape_products_total = Counter(
                "pharmacy_scrape_products_total",
                "Total products scraped",
                ["site"],
            )
            self.scrape_failures_total = Counter(
                "pharmacy_scrape_failures_total",
                "Total scrape failures",
                ["site", "reason"],
            )
            self.captcha_hits_total = Counter(
                "pharmacy_captcha_hits_total",
                "Captcha / bot-wall hits",
                ["site"],
            )
            self.matcher_clusters_total = Counter(
                "pharmacy_matcher_clusters_total",
                "Cross-site match clusters created or updated",
            )
            self.alert_events_total = Counter(
                "pharmacy_alert_events_total",
                "Alert events fired",
                ["severity", "rule_type"],
            )
            self.runs_total = Counter(
                "pharmacy_runs_total",
                "Pharmacy-monitor runs",
                ["site", "status"],
            )
            self.db_query_seconds = Histogram(
                "pharmacy_db_query_seconds",
                "DB query duration",
                ["query_type"],
                buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1, 5),
            )
            self.api_requests_total = Counter(
                "pharmacy_api_requests_total",
                "HTTP requests handled by FastAPI",
                ["status", "method"],
            )
            self.api_request_duration_seconds = Histogram(
                "pharmacy_api_request_duration_seconds",
                "FastAPI request duration",
                ["status_bucket"],
                buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1, 2, 5),
            )
            self.app_info = Info(
                "pharmacy_app",
                "Application info: version, environment",
            )
            self.app_info.info(
                {
                    "version": os.environ.get("APP_VERSION", "0.1.0"),
                    "environment": os.environ.get("SENTRY_ENVIRONMENT", "production"),
                    "git_sha": _git_short_sha(),
                }
            )
            self._initialized = True
            log.info("prometheus_metrics_initialized")
        except ImportError:
            log.warning("prometheus_client_not_installed")


class _NoopMetric:
    """Stand-in for Counter/Histogram/Info when prometheus_client is not installed."""

    def labels(self, *args, **kwargs):
        return self

    def inc(self, amount: float = 1) -> None:
        pass

    def observe(self, amount: float) -> None:
        pass

    def info(self, _info: dict) -> None:
        pass


metrics = _Metrics()


@contextmanager
def time_observation(metric, **labels) -> Iterator[None]:
    """Context manager that observes elapsed seconds into a Histogram with labels.

    with time_observation(metrics.scrape_duration_seconds, site="pharmonline"):
        await scrape(...)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        try:
            metric.labels(**labels).observe(elapsed)
        except Exception:
            pass


# ─── /metrics endpoint integration ──────────────────────────────────────────


def install_metrics_endpoint(app) -> None:
    """Wire `/metrics` Prometheus scrape endpoint to a FastAPI app.

    No-op if prometheus_client is not installed.
    """
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        from fastapi import Response

        @app.get("/metrics", include_in_schema=False)
        def metrics_endpoint():
            return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

        log.info("metrics_endpoint_installed", path="/metrics")
    except ImportError:
        log.warning("metrics_endpoint_skipped_no_prometheus_client")


# ─── One-call setup ─────────────────────────────────────────────────────────


def init_observability(service: str) -> None:
    """Initialize Sentry + Prometheus + log setup. Call once at process start.

    Service tag distinguishes API / scraper / cron in Sentry / metrics.
    """
    init_sentry(service)
    metrics.init()
