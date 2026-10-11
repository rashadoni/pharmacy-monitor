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
import re
import time
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import unquote

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

        try:
            sentry_sdk.init(
                dsn=dsn,
                environment=env,
                release=release,
                traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
                profiles_sample_rate=float(os.environ.get("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
                integrations=[FastApiIntegration(), SqlalchemyIntegration()],
                send_default_pii=False,  # don't send IP/cookies by default
                # Frame locals are where the request's credentials sit verbatim:
                # the ASGI `scope` and the `Request` object carry the session
                # cookie and the API key, a pydantic payload carries the
                # password. The SDK scrubs locals by variable name, and none of
                # those names gives the contents away. The traceback stays.
                include_local_variables=False,
                # A request body holds passwords under names the SDK's scrubber
                # does not know (`new_password`, `current_password`), and it is
                # attached to performance traces of successful requests too.
                max_request_body_size="never",
                attach_stacktrace=True,
                before_send=_sentry_before_send,
                before_send_transaction=_sentry_before_send_transaction,
            )
        except TypeError as exc:
            # The SDK rejects an option it does not know with TypeError, and
            # this runs while `src.api` is imported: the deploy does not install
            # dependencies, so an SDK older than the options above would stop
            # the API from starting. Error reporting is not worth an outage;
            # `sentry_is_running()` tells the settings page it is off. Any other
            # failure stops the start, as it always did.
            if not str(exc).startswith("Unknown option"):
                raise
            log.error(
                "sentry_init_failed",
                error=str(exc),  # "Unknown option '…'" — names the option, nothing else
                sdk_version=getattr(sentry_sdk, "VERSION", "unknown"),
            )
            return
        sentry_sdk.set_tag("service", service)
        log.info("sentry_initialized", service=service, env=env, release=release[:8])
        _SENTRY_INITIALIZED = True
    except ImportError:
        log.warning("sentry_sdk_not_installed")


def sentry_is_running() -> bool:
    """True once `init_sentry` has succeeded — a DSN in the environment alone does not say so."""
    return _SENTRY_INITIALIZED


def _sentry_before_send(event: dict, hint: dict) -> dict | None:
    """Drop low-noise errors and strip request credentials before sending.

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
    return _without_credentials(event)


def _sentry_before_send_transaction(event: dict, hint: dict) -> dict | None:
    """Performance traces carry the same request block as error events."""
    return _without_credentials(event)


# ─── Sentry: no credentials in an outgoing event ────────────────────────────
#
# Two layers:
#
#   1. The SDK does not collect (`sentry_sdk.init` arguments above): no frame
#      locals, no request body, no cookies. This layer does not depend on any
#      pattern below being right, and it keeps the session cookie, the API key
#      and request-body passwords out by itself.
#   2. The hooks below remove the same things from the finished event, so that
#      an edit of the init arguments or a new SDK default does not reopen the
#      leak — and they are the only guard for what the SDK has no switch for:
#      a query string (`/auth/verify?token=…`, which uvicorn's access log also
#      quotes into a breadcrumb), the values a database error text quotes, and
#      a token quoted in free text.
#
# tests/test_sentry_events_carry_no_credentials.py switches the layers off one
# at a time. The SDK's `EventScrubber` is not part of this: it matches
# dictionary keys against a denylist, and the cookie leaked under the keys
# `scope`, `request` and `conn`.
#
# Not covered: a secret of an unknown shape placed in the PATH of a URL, and
# anything sent through a channel that bypasses these hooks (`enable_logs`,
# span streaming) — the test fails on those, which is why it must be run after
# any change to the init arguments.

_FILTERED = "[Filtered]"

# A header or query parameter whose name says its value is a credential.
_CREDENTIAL_NAME_RE = re.compile(
    r"auth|cookie|token|secret|session|passw|pwd|signature|api[-_]?key", re.IGNORECASE
)

# Where the SDK puts a query string: the incoming request, and the breadcrumb
# and span of an outgoing one.
_QUERY_KEYS = frozenset({"query_string", "http.query"})

# A query string inside a URL that some string quotes whole: the access-log
# line `"GET /auth/verify?token=… HTTP/1.1" 500` arrives as a breadcrumb. The
# run goes on to the next whitespace, fragment included: under the h11 parser
# a raw `#` is part of the value the application reads.
_URL_QUERY_RE = re.compile(r"\?\S+")
_PARAMETER_BREAK_RE = re.compile(r"[?&;#\s]")

# Decoding `%XX` is the one step whose cost grows with the caller's input
# faster than a scan does. Nothing ordinary carries this many escapes; a string
# that does is treated as naming a credential without being decoded.
_MAX_ESCAPES = 4096

# The session cookie is a JWT. Matched by shape wherever a string quotes it —
# exception text, breadcrumb, tag: `<header>.<payload>`, the signature optional,
# so a token cut short by truncation is still caught. Group 1 is whatever is
# glued in front of the header (`sess_eyJ…`) and is kept.
#
# The first line of the pattern is there for speed, not meaning: a match may
# begin only where a run of token characters begins, and only if that run is
# followed by `.eyJ`. Without it a long `eyJeyJeyJ…` in a request line is
# rescanned from every `eyJ` — a second of CPU per 60 KB.
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]+\.eyJ)"
    r"([A-Za-z0-9_-]*?)eyJ[A-Za-z0-9_-]{8,}"
    r"\.eyJ[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]*)?"
)

# A Telegram bot token, `<bot id>:<secret starting with A>`: 35 characters in
# the shape secret scanners use, 34 in Telegram's own documentation examples.
# The bounded length keeps `<number>:<md5 | sha1 | uuid>` out. The token is part
# of the PATH of every Bot API URL (`/bot<token>/sendMessage`), and the SDK
# records the URL of an outgoing request in a breadcrumb and a span.
_TELEGRAM_TOKEN_RE = re.compile(
    r"(?<!\d)\d{5,16}(?::|%3[Aa])A[A-Za-z0-9_-]{33,34}(?![A-Za-z0-9_-])"
)

# A database error text quotes values in two places. SQLAlchemy ends it with the
# bound parameters of the failed statement — the login token from
# `WHERE magic_token = ?`, a password hash, an address: everything from the
# marker on goes, the statement before it stays. PostgreSQL itself adds DETAIL
# (`Failing row contains (…)`, `Key (email)=(…) already exists`) and CONTEXT
# lines: they go up to the statement, because a value in the row may hold a
# line break. Only the English labels are known.
_SQL_PARAMETERS_RE = re.compile(r"\[parameters: .*", re.DOTALL)
_SQL_DETAIL_RE = re.compile(r"^(DETAIL|CONTEXT):.*?(?=\n\[SQL: |\Z)", re.DOTALL | re.MULTILINE)


def _without_credentials(event: dict) -> dict | None:
    """Return the event without credentials, or None to drop it."""
    try:
        _clean_request(event)
        return _scrubbed(event)
    except Exception as exc:
        # An event the scrubber could not process is not sent: letting it
        # through would make the scrubber pass on its own failure.
        log.warning("sentry_event_dropped", reason="scrubber_failed", error_type=type(exc).__name__)
        return None


def _clean_request(event: dict) -> None:
    """Keep method, URL and harmless headers; drop body, cookies and credential headers."""
    request = event.get("request")
    if not isinstance(request, dict):
        return
    request.pop("data", None)
    request.pop("cookies", None)
    headers = request.get("headers")
    if isinstance(headers, dict):
        for name in headers:
            if _CREDENTIAL_NAME_RE.search(str(name)):
                headers[name] = _FILTERED
    elif headers is not None:
        del request["headers"]


def _scrubbed(node):
    """Copy of an event tree without frame locals, credential queries and tokens."""
    if isinstance(node, str):
        return _scrubbed_text(node)
    if isinstance(node, dict):
        clean = {}
        for key, value in node.items():
            if key == "frames" and isinstance(value, list):
                # Exception, thread and bare stacktraces all keep frames under this key.
                value = [_without_locals(frame) for frame in value]
            elif key in _QUERY_KEYS and not _query_is_harmless(value):
                value = _FILTERED
            clean[_scrubbed(key)] = _scrubbed(value)
        return clean
    if isinstance(node, (list, tuple)):
        return [_scrubbed(item) for item in node]
    return node


def _scrubbed_text(text: str) -> str:
    # Each pattern is linear in the text; the substring checks keep the common
    # case — a string with nothing to mask — off the regex engine altogether.
    if ".eyJ" in text:
        text = _JWT_RE.sub(rf"\1{_FILTERED}", text)
    if ":" in text or "%3" in text:
        text = _TELEGRAM_TOKEN_RE.sub(_FILTERED, text)
    if "[parameters: " in text:
        text = _SQL_PARAMETERS_RE.sub(f"[parameters: {_FILTERED}]", text)
    if "DETAIL:" in text or "CONTEXT:" in text:
        text = _SQL_DETAIL_RE.sub(rf"\1: {_FILTERED}", text)
    if "?" in text and ("=" in text or "%3D" in text or "%3d" in text):
        # One verdict for the string: if any of its query strings names a
        # credential, all of them go. Deciding run by run would cost a Python
        # call per run, and a request header is the caller's to fill with them.
        queries = _URL_QUERY_RE.findall(text)
        if queries and not _query_is_harmless(" ".join(queries)):
            text = _URL_QUERY_RE.sub(f"?{_FILTERED}", text)
    return text


def _without_locals(frame):
    if isinstance(frame, dict):
        return {key: value for key, value in frame.items() if key != "vars"}
    return frame


def _query_is_harmless(query) -> bool:
    """False if any parameter that has a value is named like a credential.

    The caller then replaces the whole query string, not one value: in an
    unquoted string the end of a value cannot be told apart from the start of
    the next parameter. The name is checked after unquoting — the application
    decodes `%74oken` to `token`, and `token%3D…` may reach it decoded — and a
    URL nested in a value or a fragment (`next=/x?token=…`, `#access_token=…`)
    is read as parameters of its own.
    """
    if not query:
        return True
    if not isinstance(query, str) or query.count("%") > _MAX_ESCAPES:
        return False
    for parameter in _PARAMETER_BREAK_RE.split(unquote(query)):
        name, has_value, _ = parameter.partition("=")
        if has_value and _CREDENTIAL_NAME_RE.search(name):
            return False
    return True


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
