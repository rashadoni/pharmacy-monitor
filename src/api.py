"""REST API: ERP integration (X-API-Key) + frontend dashboard (JWT cookie).

Запуск:
    pharmacy-monitor api serve --port 8080
    # или напрямую:
    uvicorn src.api:app --host 0.0.0.0 --port 8080 --workers 2

Auth моделей две:
    1. **X-API-Key header** — для ERP-интеграций (push stock/prices, fetch alerts).
       Один shared key через PHARMACY_API_KEY в .env. Используется в legacy
       endpoints `/api/v1/products`, `/api/v1/comparisons`, `/api/v1/inventory/*`.
    2. **JWT cookie** — для frontend dashboard. Magic-link flow:
       `POST /auth/request` → email с magic_token → `GET /auth/verify?token=...`
       устанавливает httpOnly cookie с JWT. Endpoints `/api/v1/dash/*` требуют
       этот cookie и фильтруют по tenant_id.

Endpoints (v1):
    Public:
        GET  /health                       — alive check (no auth)

    Auth (no auth required, public):
        POST /auth/request                 — request magic-link by email
        GET  /auth/verify?token=...        — verify token, set JWT cookie
        POST /auth/logout                  — clear cookie

    Frontend (JWT cookie required):
        GET  /api/v1/dash/me               — current user info
        GET  /api/v1/dash/comparison       — cross-site matched products
        GET  /api/v1/dash/roi/actions      — ROI compute_actions
        GET  /api/v1/dash/alerts           — recent alert events
        GET  /api/v1/dash/match-quality    — analytics.match_quality
        GET  /api/v1/dash/brand-share      — analytics.brand_share
        GET  /api/v1/dash/price-index      — analytics.price_index_by_category
        GET  /api/v1/dash/forecast/movers  — forecast.top_movers
        GET  /api/v1/dash/runs             — recent runs history
        GET  /api/v1/dash/categories       — list categories
        POST /api/v1/dash/categories       — create category
        PATCH /api/v1/dash/categories/{id} — update category
        DELETE /api/v1/dash/categories/{id} — delete category
        GET  /api/v1/dash/watchlist        — list watchlist items
        POST /api/v1/dash/watchlist        — add item
        DELETE /api/v1/dash/watchlist/{id} — remove item
        POST /api/v1/dash/matches/{id}/reject — manually reject false match

    ERP (X-API-Key required):
        GET  /api/v1/alerts/recent         — alert events for webhooks
        GET  /api/v1/products              — all products (paginated)
        GET  /api/v1/comparisons           — cross-site comparison
        GET  /api/v1/margin                — margin report
        POST /api/v1/inventory/stock       — bulk push stock levels
        POST /api/v1/inventory/prices      — bulk push purchase prices
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import structlog
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Header,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from src import analytics
from src import inventory as inv_mod
from src import storage, tenants
from src._time import utcnow
from src.normalize import pack_unit_count

log = structlog.get_logger()

# ─── Optional deps (graceful degradation if not installed yet) ───────────────
try:
    from jose import JWTError, jwt  # python-jose

    _JWT_AVAILABLE = True
except ImportError:
    _JWT_AVAILABLE = False
    log.warning("jose_not_installed", impact="JWT auth disabled, magic-link won't work")

# Centralized observability (W11): Sentry + Prometheus
from src.observability import (
    init_observability,
    install_metrics_endpoint,
    sentry_set_request_id,
    sentry_set_tenant,
)

init_observability(service="api")


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Pharmacy Monitor API",
    description="REST API for ERP integration and frontend dashboard",
    version="1.0.0",
)

# CORS for frontend (Next.js on :3000 in dev, same-origin in prod via Caddy)
_CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus /metrics endpoint
install_metrics_endpoint(app)


# ─── Request ID middleware (Phase 0.5, 2026-05-26) ───────────────────────────
#
# Каждый запрос получает UUID4 (или принимается клиентский `X-Request-ID`,
# если присутствует и выглядит безопасно). ID:
#   1. Биндится в structlog.contextvars → все log-строки в рамках запроса
#      получают `request_id=<uuid>`.
#   2. Кладётся как тэг в Sentry scope → ошибки в Sentry searchable по id.
#   3. Возвращается в response header `X-Request-ID` → фронт может его
#      показать в error boundary и положить в bug report.
#
# Размер UUID-токена ограничен 128 символами, не-ASCII / control-символы
# заменяются на безопасный fallback — чтобы клиент не мог инжектнуть мусор
# в наши логи через заголовок.

import re
import uuid as _uuid

from structlog.contextvars import bind_contextvars, clear_contextvars

_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9\-_.]{1,128}$")


def _sanitize_request_id(raw: str | None) -> str:
    """Validate incoming `X-Request-ID`; otherwise generate fresh UUID4."""
    if raw and _REQUEST_ID_SAFE_RE.match(raw):
        return raw
    return _uuid.uuid4().hex


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    request_id = _sanitize_request_id(request.headers.get(_REQUEST_ID_HEADER))
    request.state.request_id = request_id
    bind_contextvars(request_id=request_id)
    sentry_set_request_id(request_id)
    try:
        response = await call_next(request)
    finally:
        # Avoid leaking request-scoped context into the next request handled by
        # the same uvicorn worker.
        clear_contextvars()
    response.headers[_REQUEST_ID_HEADER] = request_id
    return response


# ─── Phase 5.4 — HTTP request metrics ────────────────────────────────────────
# Counter + histogram per response, label cardinality bounded:
#   status: literal HTTP code как string ("200", "404", "500"…)
#   method: GET/POST/etc.
#   status_bucket для histogram'а: "2xx" / "4xx" / "5xx" (sniff'ить latency
#       per family — успешные обычно быстрее чем 5xx с DB-таймаутом).


@app.middleware("http")
async def _api_metrics_middleware(request: Request, call_next):
    from src.observability import metrics

    t0 = time.perf_counter()
    try:
        response = await call_next(request)
        status = str(response.status_code)
    except Exception:
        # Прокидываем дальше — FastAPI отдаст 500, но мы успеем посчитать.
        metrics.api_requests_total.labels(status="500", method=request.method).inc()
        metrics.api_request_duration_seconds.labels(status_bucket="5xx").observe(
            time.perf_counter() - t0
        )
        raise
    metrics.api_requests_total.labels(status=status, method=request.method).inc()
    metrics.api_request_duration_seconds.labels(status_bucket=f"{status[0]}xx").observe(
        time.perf_counter() - t0
    )
    return response


# ─── Rate limiting (Phase 5.2 — Redis-backed, per-user tiers) ───────────────
# Реальная реализация в src.rate_limit. Эти обёртки сохраняют старый
# `_check_rate_limit(key, limit=...)` API чтобы не трогать call-sites при
# миграции. `_rate_limit_user(user)` — новый tier-aware path.

from src.rate_limit import Tier as _Tier
from src.rate_limit import check_rate_limit as _rate_check
from src.rate_limit import tier_for_user as _tier_for_user


def _check_rate_limit(client_key: str, limit: int | None = None) -> None:
    """Backwards-compat обёртка.

    Без `limit` → Tier.VIEWER из rate_limit.py.
    С `limit` → explicit override (используется для auth endpoints).
    """
    if limit is None:
        _rate_check(client_key, tier=_Tier.VIEWER)
    else:
        _rate_check(client_key, limit=limit, window_sec=60)


def _rate_limit_user(user: storage.TenantUser) -> None:
    """Per-user rate-limit с tier-detection по user.role."""
    tier = _tier_for_user(user.role)
    _rate_check(f"user:{user.id}", tier=tier)


# ─── DB session dependency ───────────────────────────────────────────────────


def get_db() -> Iterator[Session]:
    Session_ = storage.make_session()
    db = Session_()
    try:
        yield db
    finally:
        db.close()


# ─── Auth: API key (legacy ERP endpoints) ────────────────────────────────────


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> None:
    expected = os.environ.get("PHARMACY_API_KEY")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="API key not configured (set PHARMACY_API_KEY in .env)",
        )
    if x_api_key != expected:
        client_ip = request.client.host if request.client else "unknown"
        _check_rate_limit(f"failed:{client_ip}", limit=10)
        raise HTTPException(status_code=401, detail="Invalid X-API-Key")
    _check_rate_limit(f"ok:{x_api_key[:8]}")


# ─── Auth: JWT cookie (frontend dashboard) ───────────────────────────────────

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-not-for-prod")
JWT_ALGO = "HS256"
JWT_EXPIRY_DAYS = int(os.environ.get("JWT_EXPIRY_DAYS", "7"))
COOKIE_NAME = "pm_session"


def _make_jwt(user_id: int, tenant_id: int, email: str) -> str:
    """Signed JWT with user identity. Used as session cookie."""
    if not _JWT_AVAILABLE:
        raise HTTPException(503, "JWT auth not available — install python-jose")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "tid": tenant_id,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=JWT_EXPIRY_DAYS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def _decode_jwt(token: str) -> dict | None:
    if not _JWT_AVAILABLE:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except JWTError as e:
        log.debug("jwt_decode_failed", error=str(e))
        return None


_AUTH_DISABLED = os.environ.get("PHARMACY_AUTH_DISABLED", "").lower() in ("1", "true", "yes")


def require_user(
    request: Request,
    pm_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
) -> storage.TenantUser:
    """Decode JWT cookie, return TenantUser. 401 if missing/invalid.

    If env PHARMACY_AUTH_DISABLED=true → return default admin without check
    (single-tenant open mode for pilot).
    """
    if _AUTH_DISABLED:
        # Open mode: return the default tenant's first admin user
        user = db.scalar(
            select(storage.TenantUser)
            .where(storage.TenantUser.is_active.is_(True))
            .order_by(storage.TenantUser.id)
            .limit(1)
        )
        if not user:
            # No user exists — auto-create one
            from src import tenants as _tenants

            t = _tenants.get_or_create_default(db)
            user = _tenants.add_user(db, t.id, "admin@local", name="Admin", role="admin")
            db.commit()
        request.state.tenant_id = user.tenant_id
        request.state.user_id = user.id
        return user

    if not pm_session:
        raise HTTPException(401, "Not authenticated")
    payload = _decode_jwt(pm_session)
    if not payload:
        raise HTTPException(401, "Invalid or expired session")
    user_id = int(payload["sub"])
    user = db.scalar(select(storage.TenantUser).where(storage.TenantUser.id == user_id))
    if not user or not user.is_active:
        raise HTTPException(401, "User not found or inactive")
    # Rate limit per user — tier зависит от user.role (admin > viewer)
    _rate_limit_user(user)
    # Stash tenant_id on request for endpoint use
    request.state.tenant_id = user.tenant_id
    request.state.user_id = user.id
    # Tag Sentry events with tenant_id for filtering
    sentry_set_tenant(user.tenant_id)
    return user


# ─── Schemas ─────────────────────────────────────────────────────────────────


class SiteStaleness(BaseModel):
    """Per-site freshness: when did we last successfully see a product on it?

    `hours_since` aggregates the gap between now and the max `last_seen_at`
    across all products tagged with `site`. `max_age_hours` is the site-specific
    threshold used by backend health checks; daily sites use the default, while
    weekly scrapes such as aptekonline/pharmonline have a larger threshold.
    """

    site: str
    last_seen_at: datetime | None
    hours_since: float | None
    max_age_hours: int


class HealthOut(BaseModel):
    status: str  # "up" | "degraded" — degraded if a dependency or scraper is stale
    last_run_at: datetime | None
    last_run_status: str | None
    db_ping_ms: float | None = None  # SELECT 1 round-trip
    redis_ping_ms: float | None = None  # PING round-trip, null if Redis unreachable
    sites: list[SiteStaleness] = Field(default_factory=list)
    staleness_warning: bool = False  # true if any site exceeds its freshness threshold
    full_catalog_run_at: datetime | None = None
    full_catalog_status: str | None = None
    full_catalog_verified: bool = False


class AuthRequestIn(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1] or len(v) > 255:
            raise ValueError("invalid email")
        return v


class PasswordLoginIn(BaseModel):
    """Password-based login.

    `login` — либо env ADMIN_LOGIN ("admin", bootstrap-админ), либо email
    конкретного пользователя. Пароль проверяется против user.password_hash
    (для админа — fallback на env ADMIN_PASSWORD_HASH). См. auth_login.
    """

    login: str
    password: str

    @field_validator("login", "password")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("empty")
        if len(v) > 200:
            raise ValueError("too long")
        return v


class AuthRequestOut(BaseModel):
    sent: bool
    detail: str


class MeOut(BaseModel):
    id: int
    email: str
    name: str | None
    role: str
    tenant_id: int


class StockItemIn(BaseModel):
    sku: str
    qty: float
    name: str | None = None


class PurchasePriceIn(BaseModel):
    sku: str
    supplier_name: str
    purchase_price: float
    currency: str = "AZN"
    name: str | None = None


class ComparisonRowOut(BaseModel):
    canonical_id: int
    name: str
    brand: str | None
    pack_size: str | None
    is_manual: bool
    sites_with_price: int
    min_price: float | None
    max_price: float | None
    spread_pct: float | None
    cheapest_site: str | None
    prices: dict[str, dict[str, Any]]
    confidence: float
    needs_review: bool
    # Per-unit normalization (2026-05-29). spread_basis="unit" когда товар в
    # разной фасовке и нормализация цены-за-штуку уменьшает spread (честное
    # сравнение); "raw" когда фасовки одинаковы или нормализация недостоверна.
    # Каждая запись в `prices` тогда содержит unit_price + pack_count.
    spread_basis: str = "raw"  # "raw" | "unit"


class CategoryComparisonOut(BaseModel):
    category: str  # slug (Product.category) — ключ для drill-down в /comparison
    label: str  # резолвится по locale (label_ru/az), fallback на slug
    matched_skus: int
    avg_client_price: float
    per_site_avg: dict[str, float]  # {"aptekonline": .., "aloe": ..}
    avg_competitor_price: float
    index: float  # 100 = паритет, <100 клиент дешевле, >100 дороже
    cheaper_count: int
    pricier_count: int
    parity_count: int
    cheaper_pct: float
    pricier_pct: float
    parity_pct: float


class CategoryIn(BaseModel):
    key: str
    label_ru: str
    label_az: str = Field(min_length=1)
    pharmonline_slug: str | None = None
    aptekonline_slug: str | None = None
    aloe_slug: str | None = None
    is_active: bool = True

    @field_validator("key", "label_ru", "label_az")
    @classmethod
    def strip_required_category_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class CategoryOut(CategoryIn):
    id: int


class WatchlistItemIn(BaseModel):
    canonical_name: str
    brand: str | None = None
    dosage: str | None = None
    pack_size: str | None = None
    search_query: str | None = None
    notes: str | None = None
    pharmonline_url: str | None = None
    aptekonline_url: str | None = None
    aloe_url: str | None = None


class WatchlistCategoryIn(BaseModel):
    category_id: int
    notes: str | None = None


# ─── Public endpoints ────────────────────────────────────────────────────────


# Default per-site staleness threshold (hours) for daily-cadence sites (>30h = at
# least one scheduled run skipped). Non-daily sites override via
# health._SITE_MAX_AGE_HOURS (aptekonline weekly → 198h), единый источник порогов.
# Surfaced in `/health.staleness_warning`.
_HEALTH_STALENESS_HOURS = 30


def _site_staleness_threshold(site: str) -> int:
    """Return the backend health threshold for a site's freshness card."""
    from src.health import _SITE_MAX_AGE_HOURS

    return _SITE_MAX_AGE_HOURS.get(site, _HEALTH_STALENESS_HOURS)


def _ping_db(db: Session) -> float | None:
    """SELECT 1 round-trip, returns ms or None on failure."""
    try:
        from sqlalchemy import text

        t0 = time.perf_counter()
        db.execute(text("SELECT 1"))
        return round((time.perf_counter() - t0) * 1000, 2)
    except Exception:
        return None


def _ping_redis() -> float | None:
    """Redis PING round-trip, returns ms or None if URL unset / unreachable."""
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis as _redis  # local import — keeps health endpoint cheap if dep missing

        client = _redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        t0 = time.perf_counter()
        client.ping()
        return round((time.perf_counter() - t0) * 1000, 2)
    except Exception:
        return None


def _staleness_per_site(db: Session) -> list[SiteStaleness]:
    """Max(Product.last_seen_at) per site, with hours-since-now diff.

    Diff-only persist (2026-05-09) updates `last_seen_at` on every run,
    independent of whether a snapshot was actually written — so this is
    the canonical "did we run today" signal per site.

    Project convention (src/_time.py): all DateTime columns store NAIVE UTC.
    We strip tzinfo from inputs to match.
    """
    rows = db.execute(
        select(storage.Product.site, func.max(storage.Product.last_seen_at)).group_by(
            storage.Product.site
        )
    ).all()
    now = utcnow()  # naive UTC by project convention
    out: list[SiteStaleness] = []
    for site, last_seen in rows:
        hours: float | None = None
        if last_seen is not None:
            ls = last_seen.replace(tzinfo=None) if last_seen.tzinfo else last_seen
            hours = round((now - ls).total_seconds() / 3600, 2)
        out.append(
            SiteStaleness(
                site=site,
                last_seen_at=last_seen,
                hours_since=hours,
                max_age_hours=_site_staleness_threshold(site),
            )
        )
    return out


@app.get("/health", response_model=HealthOut)
def health_endpoint(db: Session = Depends(get_db)):
    """Deep health check (Phase 0.3, 2026-05-26).

    Used by ops + Caddy upstream health probe. NEVER throws — degraded
    dependencies report as `null` ping or `staleness_warning=true`, but the
    endpoint itself stays 200 so we can distinguish "API up but DB slow"
    from "API down entirely".
    """
    last = storage.latest_terminal_run(db, tenant_id=1)
    full_attempts = storage.latest_full_catalog_attempts_by_site(
        db,
        storage.FULL_CATALOG_SITES,
        tenant_id=1,
    )
    db_ms = _ping_db(db)
    redis_ms = _ping_redis()
    sites = _staleness_per_site(db)
    stale = any(
        s.hours_since is not None
        and s.hours_since > s.max_age_hours
        for s in sites
    )
    run_unhealthy = last is not None and last.status in {"degraded", "failed"}
    full_verified = bool(
        set(full_attempts) == set(storage.FULL_CATALOG_SITES)
        and all(
            attempt.status == "ok"
            and (attempt.run_quality or {}).get("full_catalog_verified") is True
            and (attempt.run_quality or {}).get("financially_eligible") is True
            and (
                ((attempt.run_quality or {}).get("sites") or {}).get(site) or {}
            ).get("status")
            == "ok"
            for site, attempt in full_attempts.items()
        )
    )
    full_unhealthy = bool(full_attempts) and not full_verified
    full_completed_at = max(
        (attempt.finished_at or attempt.started_at for attempt in full_attempts.values()),
        default=None,
    )
    full_status = None
    if full_attempts:
        full_status = "ok" if full_verified else "degraded"
        if any(attempt.status == "failed" for attempt in full_attempts.values()):
            full_status = "failed"
    status_label = (
        "degraded" if (stale or db_ms is None or run_unhealthy or full_unhealthy) else "up"
    )
    return HealthOut(
        status=status_label,
        last_run_at=last.started_at if last else None,
        last_run_status=last.status if last else None,
        db_ping_ms=db_ms,
        redis_ping_ms=redis_ms,
        sites=sites,
        staleness_warning=stale,
        full_catalog_run_at=full_completed_at,
        full_catalog_status=full_status,
        full_catalog_verified=full_verified,
    )


# ─── Auth endpoints (frontend) ───────────────────────────────────────────────


def _wants_html(request: Request) -> bool:
    """True, если запрос — навигация браузера (Accept содержит text/html).

    Используется для content-negotiation: браузер по клику из письма должен
    получить redirect на дашборд, а программные/XHR-вызовы (Accept */*) —
    привычный JSON-контракт.
    """
    return "text/html" in request.headers.get("accept", "").lower()


def _send_login_link(db: Session, email: str, *, invite: bool = False) -> bool:
    """Выпустить magic-token для email и отправить ссылку на вход. Best-effort.

    Возвращает True, если активный пользователь с таким email найден (токен
    выпущен, попытка отправки сделана), False — если такого активного юзера нет.
    Сбой SMTP логируется, но НЕ пробрасывается: создание/инвайт пользователя не
    должно падать из-за временной проблемы с почтой (fail-open).

    Цель ссылки выбирается по тому, есть ли у пользователя свой пароль:
    - нет пароля (`password_hash` пуст) → `/set-password` — юзер задаёт пароль и
      сразу входит. Дальше логинится по email+паролю в любой момент.
    - есть пароль → `/auth/verify` — magic-link просто логинит (passwordless вход).
    `invite=True` — приветственная формулировка письма (новый пользователь).
    """
    email = email.strip().lower()
    user = db.scalar(
        select(storage.TenantUser).where(
            storage.TenantUser.email == email,
            storage.TenantUser.is_active.is_(True),
        )
    )
    if not user:
        return False
    token = tenants.issue_magic_token(db, email)
    if not token:
        return False
    needs_password = not user.password_hash
    public_url = os.environ.get("PHARMACY_PUBLIC_URL", "http://localhost:3000")
    if needs_password:
        link = f"{public_url}/set-password?token={token}"
        welcome = (
            "<p>Sizə Pharmacy Monitor monitorinq paneli üçün giriş açıldı.</p>"
            "<p>Вам открыт доступ к панели Pharmacy Monitor.</p>"
            if invite
            else ""
        )
        subject = "Pharmacy Monitor — parol yaradın / создайте пароль"
        intro = (
            f"{welcome}"
            "<p>Daxil olmaq üçün parol təyin edin (keçid 30 dəqiqə qüvvədədir):</p>"
            "<p>Задайте пароль для входа (ссылка действует 30 минут):</p>"
        )
    else:
        link = f"{public_url}/auth/verify?token={token}"
        subject = "Pharmacy Monitor — giriş keçidi / ссылка для входа"
        intro = (
            "<p>Daxil olmaq üçün keçid (30 dəqiqə qüvvədədir):</p>"
            "<p>Ссылка для входа (действует 30 минут):</p>"
        )
    try:
        from src import notifier

        notifier.send_email(
            subject=subject,
            html_body=(
                f"{intro}"
                f"<p><a href='{link}'>{link}</a></p>"
                f"<p>Əgər bunu siz tələb etməmisinizsə, məktubu nəzərə almayın. "
                f"Если вы этого не запрашивали — просто проигнорируйте письмо.</p>"
            ),
            to=[email],
        )
    except Exception as e:
        log.warning("login_link_email_failed", error=str(e), email=email)
    return True


@app.post("/auth/request", response_model=AuthRequestOut)
def auth_request(payload: AuthRequestIn, request: Request, db: Session = Depends(get_db)):
    """Request a magic-link by email. Always returns success to avoid email enumeration."""
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"auth_req:{client_ip}", limit=5)

    _send_login_link(db, str(payload.email).strip().lower())
    # Always return success (don't leak whether email exists)
    return AuthRequestOut(sent=True, detail="If the email is registered, a magic-link was sent.")


@app.get("/auth/verify")
def auth_verify(token: str, request: Request, response: Response, db: Session = Depends(get_db)):
    """Verify magic token, set JWT cookie.

    Этот эндпоинт кликают прямо из письма, поэтому для браузера (Accept:
    text/html) после установки cookie делаем 303-redirect на дашборд — иначе
    пользователь видел бы сырой JSON. Программные/XHR-вызовы (Accept */*)
    получают прежний JSON-контракт.
    """
    wants_html = _wants_html(request)
    user = tenants.verify_magic_token(db, token)
    if not user:
        if wants_html:
            return RedirectResponse(url="/login?error=link", status_code=303)
        raise HTTPException(401, "Invalid or expired token")
    jwt_token = _make_jwt(user.id, user.tenant_id, user.email)
    secure = os.environ.get("PHARMACY_COOKIE_SECURE", "false").lower() in ("1", "true")
    if wants_html:
        redirect = RedirectResponse(url="/overview", status_code=303)
        redirect.set_cookie(
            key=COOKIE_NAME,
            value=jwt_token,
            max_age=JWT_EXPIRY_DAYS * 24 * 3600,
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        return redirect
    response.set_cookie(
        key=COOKIE_NAME,
        value=jwt_token,
        max_age=JWT_EXPIRY_DAYS * 24 * 3600,
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    return {"ok": True, "user_id": user.id}


def _verify_bcrypt(password: str, hash_str: str) -> bool:
    """Verify bcrypt password. Used by auth.login и change-password."""
    try:
        import bcrypt

        return bcrypt.checkpw(password.encode(), hash_str.encode())
    except ImportError:
        from passlib.hash import bcrypt as bcrypt_pl

        return bcrypt_pl.verify(password, hash_str)


def _hash_bcrypt(password: str) -> str:
    """Generate bcrypt hash."""
    try:
        import bcrypt

        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    except ImportError:
        from passlib.hash import bcrypt as bcrypt_pl

        return bcrypt_pl.hash(password)


# Dummy-хеш для постоянного времени в auth_login (см. там) — bcrypt запускается
# даже когда у логина нет пароля, чтобы не было timing-оракула энумерации.
_DUMMY_BCRYPT_HASH = _hash_bcrypt("not-a-real-password-timing-equalizer")


@app.post("/auth/login")
def auth_login(
    payload: PasswordLoginIn, response: Response, request: Request, db: Session = Depends(get_db)
):
    """Password login. Вход по email+паролю (любой юзер) или admin-bootstrap.

    Два пути резолва пользователя:
    1. `login` == env ADMIN_LOGIN ("admin") — bootstrap: первый активный админ.
       Для него разрешён fallback на env ADMIN_PASSWORD_HASH, пока клиент не задал
       свой пароль через UI.
    2. иначе `login` трактуется как email — конкретный активный пользователь.
       Для именованного юзера env-хеш НЕ применяется: войти можно только своим
       `password_hash` (его задают по ссылке-приглашению /set-password). Это
       не даёт постороннему войти под чужим email общим админ-паролем.
    """
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"login:{client_ip}", limit=10)

    import hmac

    expected_login = os.environ.get("ADMIN_LOGIN", "admin")
    env_pw_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
    login_raw = payload.login.strip()

    if hmac.compare_digest(login_raw.lower(), expected_login.lower()):
        # Bootstrap-админ: первый активный АДМИН + допускается env-хеш
        user = db.scalar(
            select(storage.TenantUser)
            .where(
                storage.TenantUser.is_active.is_(True),
                storage.TenantUser.role == "admin",
            )
            .order_by(storage.TenantUser.id)
            .limit(1)
        )
        if not user:
            from src import tenants as _tenants

            t = _tenants.get_or_create_default(db)
            user = _tenants.add_user(db, t.id, "admin@local", name="Admin", role="admin")
            db.commit()
        pw_to_check = user.password_hash or env_pw_hash
    else:
        # Email-based login для конкретного пользователя — без env-fallback
        user = db.scalar(
            select(storage.TenantUser)
            .where(
                storage.TenantUser.email == login_raw.lower(),
                storage.TenantUser.is_active.is_(True),
            )
            .order_by(storage.TenantUser.id)
            .limit(1)
        )
        pw_to_check = user.password_hash if user else ""

    # Единообразный 401 (не раскрываем, существует ли логин и задан ли пароль).
    # bcrypt выполняется ВСЕГДА (против dummy-хеша, если пароля нет) — иначе
    # разница во времени ответа выдала бы, у какого email задан пароль.
    pw_ok = _verify_bcrypt(payload.password, pw_to_check or _DUMMY_BCRYPT_HASH)
    if not user or not pw_to_check or not pw_ok:
        _check_rate_limit(f"login_fail:{client_ip}", limit=5)
        raise HTTPException(401, "Неверный логин или пароль")

    jwt_token = _make_jwt(user.id, user.tenant_id, user.email)
    secure = os.environ.get("PHARMACY_COOKIE_SECURE", "false").lower() in ("1", "true")
    response.set_cookie(
        key=COOKIE_NAME,
        value=jwt_token,
        max_age=JWT_EXPIRY_DAYS * 24 * 3600,
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    return {"ok": True, "user_id": user.id, "email": user.email}


class SetPasswordIn(BaseModel):
    """Задать пароль по magic-token из письма-приглашения."""

    token: str
    new_password: str

    @field_validator("token")
    @classmethod
    def _strip_token(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 512:
            raise ValueError("invalid token")
        return v

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Пароль должен быть минимум 6 символов")
        if len(v) > 200:
            raise ValueError("too long")
        return v


@app.post("/auth/set-password")
def auth_set_password(
    payload: SetPasswordIn, response: Response, request: Request, db: Session = Depends(get_db)
):
    """Задать свой пароль по одноразовому magic-token (ссылка-приглашение).

    Проверяет token (одноразовый, TTL 30 мин), сохраняет bcrypt(new_password) в
    user.password_hash и сразу логинит (ставит JWT cookie). Дальше пользователь
    входит по email+паролю через /auth/login. Невалидный/просроченный token → 401.
    """
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"setpw:{client_ip}", limit=10)

    user = tenants.verify_magic_token(db, payload.token)
    if not user:
        _check_rate_limit(f"setpw_fail:{client_ip}", limit=5)
        raise HTTPException(401, "Ссылка недействительна или истекла")

    # Защита от перехвата (Codex MED): set-password только для ПЕРВИЧНОЙ установки.
    # У юзера с паролем magic-link ведёт на /auth/verify (вход), а не сюда; иначе
    # перезапись пароля по перехваченной login-ссылке = persistent takeover.
    if user.password_hash:
        raise HTTPException(400, "Пароль уже задан — войдите по email и паролю")

    user.password_hash = _hash_bcrypt(payload.new_password)
    db.commit()

    jwt_token = _make_jwt(user.id, user.tenant_id, user.email)
    secure = os.environ.get("PHARMACY_COOKIE_SECURE", "false").lower() in ("1", "true")
    response.set_cookie(
        key=COOKIE_NAME,
        value=jwt_token,
        max_age=JWT_EXPIRY_DAYS * 24 * 3600,
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    return {"ok": True, "user_id": user.id, "email": user.email}


@app.post("/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/v1/dash/me", response_model=MeOut)
def dash_me(user: storage.TenantUser = Depends(require_user)):
    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        tenant_id=user.tenant_id,
    )


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/v1/dash/me/password")
def dash_change_password(
    payload: PasswordChangeIn,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Сменить пароль текущего пользователя.

    Проверяет current_password против DB-hash или env-fallback (bootstrap mode).
    На success — сохраняет bcrypt(new_password) в user.password_hash. С этого
    момента env ADMIN_PASSWORD_HASH перестаёт использоваться для этого user'а.
    """
    if len(payload.new_password) < 6:
        raise HTTPException(400, "Пароль должен быть минимум 6 символов")
    if payload.current_password == payload.new_password:
        raise HTTPException(400, "Новый пароль не должен совпадать с текущим")

    env_pw_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
    pw_to_check = user.password_hash or env_pw_hash
    if not pw_to_check or not _verify_bcrypt(payload.current_password, pw_to_check):
        raise HTTPException(401, "Текущий пароль неверный")

    user.password_hash = _hash_bcrypt(payload.new_password)
    db.commit()
    return {"ok": True}


class ScrapeTriggerIn(BaseModel):
    mode: str = "all"  # 'all' | 'category'
    category_id: int | None = None
    sites: list[str] | None = None  # ["pharmonline", "aptekonline", "aloe"] etc


@app.post("/api/v1/dash/scrape/trigger", status_code=202)
def dash_scrape_trigger(
    payload: ScrapeTriggerIn,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Поставить scrape-запрос в очередь. Server watcher подберёт в течение ~60 секунд.

    Использует таблицу `scrape_requests`. Возвращает 202 + id запроса —
    UI polls статус до status='ok'/'degraded'/'failed'.
    """
    if user.role not in ("admin", "owner"):
        raise HTTPException(403, "Admin role required")
    if user.tenant_id != 1:
        raise HTTPException(
            409,
            "Server-side scraping is not enabled for this tenant yet",
        )
    if payload.mode not in ("all", "category"):
        raise HTTPException(400, "mode must be 'all' or 'category'")
    if payload.mode == "category" and not payload.category_id:
        raise HTTPException(400, "category_id required for mode='category'")

    # Анти-spam #1: не более 5 pending заявок одновременно (watcher разгребёт серийно).
    pending_count = (
        db.scalar(
            select(func.count(storage.ScrapeRequest.id)).where(
                storage.ScrapeRequest.tenant_id == user.tenant_id,
                storage.ScrapeRequest.status.in_(("pending", "running")),
            )
        )
        or 0
    )
    MAX_PENDING = 5
    if pending_count >= MAX_PENDING:
        raise HTTPException(
            409,
            f"В очереди уже {pending_count} запросов. Дождитесь их завершения.",
        )

    # Анти-spam #2: дубликат тех же mode+category_id — отбиваем (нет смысла
    # ставить две одинаковые задачи).
    same_pending = db.scalar(
        select(storage.ScrapeRequest)
        .where(
            storage.ScrapeRequest.tenant_id == user.tenant_id,
            storage.ScrapeRequest.status.in_(("pending", "running")),
            storage.ScrapeRequest.mode == payload.mode,
            storage.ScrapeRequest.category_id == payload.category_id,
        )
        .limit(1)
    )
    if same_pending:
        raise HTTPException(
            409,
            f"Такой же запрос #{same_pending.id} уже {same_pending.status}.",
        )

    # Анти-spam #3: если уже идёт mode='all' — нет смысла добавлять конкретную
    # категорию (она уже включена в all).
    if payload.mode == "category":
        all_pending = db.scalar(
            select(storage.ScrapeRequest)
            .where(
                storage.ScrapeRequest.tenant_id == user.tenant_id,
                storage.ScrapeRequest.status.in_(("pending", "running")),
                storage.ScrapeRequest.mode == "all",
            )
            .limit(1)
        )
        if all_pending:
            raise HTTPException(
                409,
                f"Идёт полное сканирование #{all_pending.id} — оно включает эту категорию.",
            )

    req = storage.ScrapeRequest(
        tenant_id=user.tenant_id,
        requested_by_user_id=user.id,
        mode=payload.mode,
        category_id=payload.category_id,
        sites=",".join(payload.sites) if payload.sites else None,
        status="pending",
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return {"id": req.id, "status": "pending"}


@app.get("/api/v1/dash/scrape/requests")
def dash_scrape_requests(
    limit: int = 10,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Последние scrape-запросы tenant'а. UI polls этот endpoint для статуса.

    Для terminal request'ов с run_id подмешиваем агрегаты из Run:
    products_scraped (total) + products_per_site ({site: count}). UI отличает
    подтверждённый ok от частичного degraded и failed.
    """
    reqs = db.scalars(
        select(storage.ScrapeRequest)
        .where(storage.ScrapeRequest.tenant_id == user.tenant_id)
        .order_by(desc(storage.ScrapeRequest.id))
        .limit(limit)
    ).all()

    # Pre-fetch runs одним запросом по run_id'ам (избегаем N+1)
    run_ids = [r.run_id for r in reqs if r.run_id]
    runs_map: dict[int, storage.Run] = {}
    if run_ids:
        rows = db.scalars(
            select(storage.Run).where(
                storage.Run.id.in_(run_ids),
                storage.Run.tenant_id == user.tenant_id,
            )
        ).all()
        runs_map = {run.id: run for run in rows}

    out = []
    for r in reqs:
        run = runs_map.get(r.run_id) if r.run_id else None
        out.append(
            {
                "id": r.id,
                "mode": r.mode,
                "category_id": r.category_id,
                "sites": r.sites,
                "status": r.status,
                "requested_at": r.requested_at.isoformat() if r.requested_at else None,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "run_id": r.run_id,
                "error_message": r.error_message,
                "products_scraped": run.products_scraped if run else None,
                "products_per_site": run.products_per_site if run else None,
            }
        )
    return out


# ─── Internal endpoints для server-side scrape watcher ──────────────────────


@app.get("/api/v1/internal/pending-scrape", dependencies=[Depends(require_api_key)])
def internal_pending_scrape(db: Session = Depends(get_db)):
    """Возвращает старейший pending запрос (для server watcher'а).

    Auth via X-API-Key header (require_api_key, same as legacy ERP endpoints).
    """
    req = db.scalar(
        select(storage.ScrapeRequest)
        .where(
            storage.ScrapeRequest.status == "pending",
            storage.ScrapeRequest.tenant_id == 1,
        )
        .order_by(storage.ScrapeRequest.id)
        .limit(1)
    )
    if not req:
        return {"pending": None}
    # Mark as running immediately, чтобы не подобрать дважды
    req.status = "running"
    req.started_at = utcnow()
    db.commit()
    return {
        "pending": {
            "id": req.id,
            "mode": req.mode,
            "category_id": req.category_id,
            "sites": (req.sites.split(",") if req.sites else None),
        }
    }


class ScrapeCompleteIn(BaseModel):
    run_id: int | None = None
    error_message: str | None = None


@app.post("/api/v1/internal/scrape-complete/{request_id}", dependencies=[Depends(require_api_key)])
def internal_scrape_complete(
    request_id: int,
    payload: ScrapeCompleteIn,
    db: Session = Depends(get_db),
):
    """Server watcher callback after command exit.

    `run --request-id` writes the authoritative scrape-phase terminal state
    (ok/degraded/failed) before matcher/analyzer. The watcher must never replace
    degraded with ok merely because the process later exited zero.
    """
    req = db.scalar(select(storage.ScrapeRequest).where(storage.ScrapeRequest.id == request_id))
    if not req:
        raise HTTPException(404, "Request not found")
    payload_run = None
    if payload.run_id is not None:
        payload_run = db.scalar(
            select(storage.Run).where(
                storage.Run.id == payload.run_id,
                storage.Run.tenant_id == req.tenant_id,
            )
        )
        if payload_run is None:
            raise HTTPException(400, "Run does not belong to scrape request tenant")
    if req.status in {"ok", "degraded", "failed"}:
        if payload.error_message:
            req.error_message = (
                f"{req.error_message or ''} | post-persist: {payload.error_message}"
            ).strip(" |")
        if payload.run_id is not None:
            req.run_id = payload.run_id
        req.completed_at = req.completed_at or utcnow()
        db.commit()
        return {"ok": True, "noop": f"already {req.status}"}
    req.status = "failed" if payload.error_message else "ok"
    req.completed_at = utcnow()
    if payload.run_id is not None:
        req.run_id = payload.run_id
    req.error_message = payload.error_message
    db.commit()
    return {"ok": True}


@app.get("/api/v1/dash/integrations")
def dash_integrations(user: storage.TenantUser = Depends(require_user)):
    """Статус серверных интеграций. Фронт показывает в /settings — клиент
    видит явно что настроено, что нет. Раньше клиент не понимал почему
    «привязал Telegram, а уведомления не идут» — теперь видно «❌ нет токена».

    Не возвращаем сами секреты — только bool.
    """
    import os

    return {
        "smtp": bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_PASSWORD")),
        "smtp_from": os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or None,
        "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "telegram_bot_username": os.environ.get("TELEGRAM_BOT_USERNAME") or None,
        "sentry": bool(os.environ.get("SENTRY_DSN")),
        "scraperapi": bool(os.environ.get("SCRAPER_API_KEY")),
        "scraperapi_sites": (os.environ.get("SCRAPER_API_SITES") or "").split(",")
        if os.environ.get("SCRAPER_API_SITES")
        else [],
    }


# ─── Notification preferences (W9) ───────────────────────────────────────────


class NotifPrefsOut(BaseModel):
    telegram_chat_id: str | None
    email_severity_min: str | None
    telegram_severity_min: str | None
    quiet_hours: str | None
    daily_digest: bool
    weekly_digest: bool


class NotifPrefsIn(BaseModel):
    email_severity_min: str | None = None
    telegram_severity_min: str | None = None
    quiet_hours: str | None = None
    daily_digest: bool | None = None
    weekly_digest: bool | None = None

    @field_validator("email_severity_min", "telegram_severity_min")
    @classmethod
    def _validate_sev(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in ("off", "info", "warning", "critical"):
            raise ValueError("severity must be off/info/warning/critical")
        return v

    @field_validator("quiet_hours")
    @classmethod
    def _validate_qh(cls, v: str | None) -> str | None:
        if not v:
            return None
        # Format: HH-HH (e.g. "22-08")
        try:
            parts = v.split("-")
            if len(parts) != 2:
                raise ValueError
            start, end = int(parts[0]), int(parts[1])
            if not (0 <= start <= 23 and 0 <= end <= 23):
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError("quiet_hours must be 'HH-HH' (e.g. '22-08')")
        return v


@app.get("/api/v1/dash/me/notifications", response_model=NotifPrefsOut)
def dash_notifications_get(user: storage.TenantUser = Depends(require_user)):
    return NotifPrefsOut(
        telegram_chat_id=user.telegram_chat_id,
        email_severity_min=user.email_severity_min,
        telegram_severity_min=user.telegram_severity_min,
        quiet_hours=user.quiet_hours,
        daily_digest=user.daily_digest or False,
        weekly_digest=user.weekly_digest or False,
    )


@app.patch("/api/v1/dash/me/notifications")
def dash_notifications_update(
    payload: NotifPrefsIn,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    # Re-fetch in this session (require_user yields a different session)
    u = db.scalar(select(storage.TenantUser).where(storage.TenantUser.id == user.id))
    if not u:
        raise HTTPException(404, "User not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(u, k, v)
    db.commit()
    return {"ok": True}


@app.delete("/api/v1/dash/me/notifications/telegram", status_code=204)
def dash_notifications_unbind_telegram(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove telegram binding (chat_id = NULL)."""
    u = db.scalar(select(storage.TenantUser).where(storage.TenantUser.id == user.id))
    if u:
        u.telegram_chat_id = None
        db.commit()
    return Response(status_code=204)


@app.post("/api/v1/dash/digest/send-test")
def dash_digest_send_test(
    kind: str = "daily",
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Отправить test digest всем получателям с `daily_digest=true`.

    Используется из Quick Actions меню. Тот же код что и cron-таймер,
    просто on-demand. Только admin.
    """
    if user.role != "admin":
        raise HTTPException(403, "Admin role required")
    if kind not in ("daily", "weekly"):
        raise HTTPException(400, "kind must be 'daily' or 'weekly'")
    from src import notifications

    try:
        count = (
            notifications.send_daily_digest(db, tenant_id=user.tenant_id)
            if kind == "daily"
            else notifications.send_weekly_digest(db, tenant_id=user.tenant_id)
        )
        log.info("digest_sent_manual", kind=kind, count=count, by_user_id=user.id)
        return {"ok": True, "recipients_sent": count}
    except Exception as e:
        log.error("digest_send_failed", error=str(e))
        raise HTTPException(500, f"Не удалось отправить: {e}")


# ─── Recipients management (admin only) ──────────────────────────────────────
# Управление получателями email-digest. Admin может добавить любого получателя
# (клиента, бухгалтера, партнёра) с настройкой severity_min и opt-in для
# daily/weekly. Каждый recipient это TenantUser с is_active=true. Digest-cron
# собирает их через `daily_digest=true` и шлёт каждому отдельное письмо.


class RecipientOut(BaseModel):
    id: int
    email: str
    name: str | None
    role: str
    is_active: bool
    daily_digest: bool
    weekly_digest: bool
    email_severity_min: str | None
    telegram_chat_id: str | None
    last_login_at: str | None
    created_at: str | None


class RecipientCreate(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    name: str | None = None
    role: str = "viewer"
    daily_digest: bool = True
    weekly_digest: bool = False
    email_severity_min: str | None = "warning"

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or len(v) < 5:
            raise ValueError("invalid email")
        return v

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in ("admin", "viewer"):
            raise ValueError("role must be admin or viewer")
        return v

    @field_validator("email_severity_min")
    @classmethod
    def _sev(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if v not in ("off", "info", "warning", "critical"):
            raise ValueError("severity must be off/info/warning/critical")
        return v


class RecipientUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    daily_digest: bool | None = None
    weekly_digest: bool | None = None
    email_severity_min: str | None = None

    @field_validator("role")
    @classmethod
    def _role(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in ("admin", "viewer"):
            raise ValueError("role must be admin or viewer")
        return v

    @field_validator("email_severity_min")
    @classmethod
    def _sev(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in ("off", "info", "warning", "critical"):
            raise ValueError("severity must be off/info/warning/critical")
        return v


def _require_admin(user: storage.TenantUser) -> None:
    if user.role != "admin":
        raise HTTPException(403, "Admin role required")


def _is_last_active_admin(db: Session, tenant_id: int, exclude_id: int) -> bool:
    """True если в тенанте НЕТ других активных админов кроме exclude_id.

    Защищает от состояния «0 админов» при demote/deactivate/delete чужого
    admin-аккаунта (self-lockout гарды покрывают только себя).
    """
    others = (
        db.scalar(
            select(func.count(storage.TenantUser.id)).where(
                storage.TenantUser.tenant_id == tenant_id,
                storage.TenantUser.role == "admin",
                storage.TenantUser.is_active.is_(True),
                storage.TenantUser.id != exclude_id,
            )
        )
        or 0
    )
    return others == 0


def _to_recipient_out(u: storage.TenantUser) -> RecipientOut:
    return RecipientOut(
        id=u.id,
        email=u.email,
        name=u.name,
        role=u.role,
        is_active=bool(u.is_active),
        daily_digest=bool(u.daily_digest),
        weekly_digest=bool(u.weekly_digest),
        email_severity_min=u.email_severity_min,
        telegram_chat_id=u.telegram_chat_id,
        last_login_at=u.last_login_at.isoformat() if u.last_login_at else None,
        created_at=u.created_at.isoformat() if u.created_at else None,
    )


@app.get("/api/v1/dash/recipients", response_model=list[RecipientOut])
def dash_recipients_list(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    users = db.scalars(
        select(storage.TenantUser)
        .where(storage.TenantUser.tenant_id == user.tenant_id)
        .order_by(desc(storage.TenantUser.created_at))
    ).all()
    return [_to_recipient_out(u) for u in users]


@app.post("/api/v1/dash/recipients", response_model=RecipientOut)
def dash_recipients_create(
    payload: RecipientCreate,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    exists = db.scalar(
        select(storage.TenantUser).where(
            storage.TenantUser.tenant_id == user.tenant_id,
            storage.TenantUser.email == payload.email,
        )
    )
    if exists:
        raise HTTPException(409, f"User with email {payload.email} already exists")

    new_user = storage.TenantUser(
        tenant_id=user.tenant_id,
        email=payload.email,
        name=payload.name,
        role=payload.role,
        is_active=True,
        daily_digest=payload.daily_digest,
        weekly_digest=payload.weekly_digest,
        email_severity_min=payload.email_severity_min,
        created_at=utcnow(),
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    # Авто-инвайт: новый юзер получает magic-link на вход сразу (без него у
    # него нет способа залогиниться — общий пароль логинит как админа, а формы
    # запроса ссылки в UI нет). Best-effort: сбой почты не валит создание.
    invite_sent = _send_login_link(db, new_user.email, invite=True)
    log.info(
        "recipient_created",
        id=new_user.id,
        email=new_user.email,
        by_user_id=user.id,
        invite_sent=invite_sent,
    )
    return _to_recipient_out(new_user)


@app.patch("/api/v1/dash/recipients/{recipient_id}", response_model=RecipientOut)
def dash_recipients_update(
    recipient_id: int,
    payload: RecipientUpdate,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    r = db.scalar(
        select(storage.TenantUser).where(
            storage.TenantUser.id == recipient_id,
            storage.TenantUser.tenant_id == user.tenant_id,
        )
    )
    if not r:
        raise HTTPException(404, "Recipient not found")
    data = payload.model_dump(exclude_unset=True)
    # Защита от self-lockout: нельзя демотнуть себя в viewer или деактивировать
    if recipient_id == user.id:
        if data.get("role") == "viewer":
            raise HTTPException(
                400, "Нельзя сменить себе роль на viewer — потеряете доступ к админке"
            )
        if data.get("is_active") is False:
            raise HTTPException(400, "Нельзя деактивировать себя")
    # Нельзя оставить тенант без активного администратора (demote/deactivate чужого)
    if (
        r.role == "admin"
        and r.is_active
        and (data.get("role") == "viewer" or data.get("is_active") is False)
        and _is_last_active_admin(db, user.tenant_id, r.id)
    ):
        raise HTTPException(400, "Нельзя убрать последнего активного администратора")
    for k, v in data.items():
        setattr(r, k, v)
    db.commit()
    db.refresh(r)
    log.info(
        "recipient_updated",
        id=r.id,
        by_user_id=user.id,
        fields=list(data.keys()),
    )
    return _to_recipient_out(r)


@app.delete("/api/v1/dash/recipients/{recipient_id}", status_code=204)
def dash_recipients_delete(
    recipient_id: int,
    hard: bool = False,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Удаление получателя.

    По умолчанию (`hard=false`) — soft delete: is_active=false + дайджесты off,
    строка остаётся (история/аудит цел, обратимо галочкой Aktiv).

    `hard=true` — физическое удаление строки. Используется для уже
    деактивированных юзеров, когда нужно реально освободить email (UI показывает
    кнопку только у деактивированных). Единственный FK на tenant_users —
    `scrape_requests.requested_by_user_id` — обнуляется явно ПЕРЕД DELETE, чтобы
    не упереться в FK независимо от ondelete-настройки на проде.
    """
    _require_admin(user)
    if recipient_id == user.id:
        raise HTTPException(400, "Нельзя удалить себя")
    r = db.scalar(
        select(storage.TenantUser).where(
            storage.TenantUser.id == recipient_id,
            storage.TenantUser.tenant_id == user.tenant_id,
        )
    )
    if not r:
        raise HTTPException(404, "Recipient not found")
    if r.role == "admin" and r.is_active and _is_last_active_admin(db, user.tenant_id, r.id):
        raise HTTPException(400, "Нельзя удалить последнего активного администратора")

    if hard:
        from sqlalchemy import update as _update

        db.execute(
            _update(storage.ScrapeRequest)
            .where(storage.ScrapeRequest.requested_by_user_id == r.id)
            .values(requested_by_user_id=None)
        )
        db.delete(r)
        db.commit()
        log.info("recipient_hard_deleted", id=recipient_id, by_user_id=user.id)
        return Response(status_code=204)

    r.is_active = False
    r.daily_digest = False
    r.weekly_digest = False
    db.commit()
    log.info("recipient_deleted", id=r.id, by_user_id=user.id)
    return Response(status_code=204)


@app.post("/api/v1/dash/recipients/{recipient_id}/send-login-link")
def dash_recipients_send_login_link(
    recipient_id: int,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Повторно отправить пользователю magic-link на вход (admin-only).

    В отличие от публичного /auth/request (анти-enumeration → всегда 200), этот
    эндпоинт admin-only, поэтому может честно вернуть 404/400 — утечки наличия
    email тут нет.
    """
    _require_admin(user)
    r = db.scalar(
        select(storage.TenantUser).where(
            storage.TenantUser.id == recipient_id,
            storage.TenantUser.tenant_id == user.tenant_id,
        )
    )
    if not r:
        raise HTTPException(404, "Recipient not found")
    if not r.is_active:
        raise HTTPException(400, "Пользователь деактивирован — сначала активируйте его")
    _send_login_link(db, r.email, invite=False)
    log.info("recipient_login_link_sent", id=r.id, by_user_id=user.id)
    return {"ok": True, "email": r.email}


# ─── Frontend dashboard endpoints (JWT cookie) ───────────────────────────────


# Порог свежести цены для comparison (2026-05-29). Считается по
# Product.last_seen_at (обновляется КАЖДЫЙ прогон под diff-only), НЕ по
# snapshot.captured_at (пишется только при смене цены — у товара со стабильной
# ценой captured_at старый, но товар живой). Цена старше порога: показывается
# с бейджем «N дн. назад», но НЕ участвует в расчёте spread/min/max/cheapest,
# чтобы устаревшая цена не давала ложный undercut/spread. Строка всё равно
# отображается (ничего не теряется — клиент видит обе цены + контекст возраста).
_COMPARISON_STALE_DAYS = 14


def _price_age_days(now: datetime, last_seen: datetime | None) -> int | None:
    """Возраст цены в днях по last_seen_at. None → None (last_seen_at=NULL).

    tz-defensive: prod-колонка naive (Postgres timestamp without tz), но если
    прилетит aware datetime — нормализуем (иначе naive-aware вычитание упало бы
    TypeError'ом). Будущие timestamp'ы (clock skew между scraper runtime и API)
    клампятся в 0, чтобы age не уходил в минус.
    """
    if last_seen is None:
        return None
    if last_seen.tzinfo is not None:
        last_seen = last_seen.replace(tzinfo=None)
    return max(0, (now - last_seen).days)


def _comparison_spread(
    prices: dict[str, dict[str, Any]],
) -> tuple[str, float | None, float | None, str | None, float | None]:
    """Per-unit-aware spread для comparison-строки (учёт свежести).

    Мутирует `prices`: (а) добавляет каждому сайту `pack_count` + `unit_price`,
    (б) УДАЛЯЕТ сайты с явно-битой СВЕЖЕЙ ценой (outlier < 12% медианы — это
    parse-ошибка/не та единица, не реальный undercut). Возвращает
    (basis, min, max, cheapest_site, spread_pct) на выбранном basis.

    Свежесть (2026-05-29): записи с `stale=True` (last_seen_at старше
    _COMPARISON_STALE_DAYS) ОСТАЮТСЯ в `prices` для отображения с бейджем, но в
    расчёт basis/outlier/stats НЕ входят — устаревшая цена не должна двигать
    spread/undercut. Если свежих цен < 2 → spread не считается (None), но строка
    всё равно показывается со stale-ценами.

    Логика per-unit (2026-05-29, Perplexity+Codex consensus):
    - Считаем count штук в упаковке + unit_price = price/count.
    - Outlier-фильтр работает на UNIT-цене (не raw): для маски поштучно 0.20
      vs пачки N50 за 10.00 unit price у обоих 0.20 → ни один не выбрасывается
      (старый raw-фильтр ошибочно дропал 0.20 как «<10% от 10»).
    - basis="unit" ТОЛЬКО если фасовки реально различаются (max/min count ≥ 2,
      ≥1 high-confidence) И нормализация УМЕНЬШАЕТ spread. Это снимает риск
      «один сайт N50, другой не распарсился → ложный 50×»: если нормализация
      раздувает spread — это parse-артефакт, остаёмся на raw.
    """
    for data in prices.values():
        cnt, conf = pack_unit_count(data.get("pack_size"), data.get("name"))
        data["pack_count"] = cnt
        data["count_conf"] = conf
        data["unit_price"] = data["price"] / cnt if cnt > 0 else data["price"]

    # Свежие = участвуют в расчёте. Stale остаются в `prices` для показа (бейдж).
    fresh = {s: d for s, d in prices.items() if not d.get("stale")}

    def _spread_over(key: str) -> float:
        vs = [d[key] for d in fresh.values()]
        return (max(vs) - min(vs)) / max(vs) * 100 if vs and max(vs) else 0.0

    # ── Шаг 1: выбрать basis ДО outlier-фильтра (только по свежим) ──
    # (фильтровать надо по той цене, которой доверяем; иначе при недостоверных
    #  count'ах unit-цены ложные и дропнут легитимный сайт — risk #4.)
    basis = "raw"
    if len(fresh) >= 2:
        counts = [d["pack_count"] for d in fresh.values()]
        has_high = any(d["count_conf"] == "high" for d in fresh.values())
        counts_differ = max(counts) / min(counts) >= 2 if min(counts) > 0 else False
        # unit только если фасовки различаются И нормализация УМЕНЬШАЕТ spread.
        if has_high and counts_differ and _spread_over("unit_price") < _spread_over("price"):
            basis = "unit"

    key = "unit_price" if basis == "unit" else "price"

    # ── Шаг 2: СИММЕТРИЧНЫЙ outlier-фильтр по свежим (parse-ошибки/wrong-match) ──
    # Низкий выброс (<12% медианы = >8.3× дешевле): почти всегда parse-ошибка /
    #   не та единица (Thiogamma aloe 8.90 vs pharm 89.00 = 10×).
    # Высокий выброс (>8.3× медианы): ловится ТОЛЬКО при 3+ сайтах, где медиана —
    #   реальный консенсус. Напр. wrong-match «Aspirin C» 8.02 при aptek/aloe
    #   0.30/0.30 → дропаем 8.02, остаётся консенсус 0.30/0.30 (spread 0%).
    #   При 2 сайтах median == max, высокий порог не срабатывает → поведение
    #   2-сайтовых строк без изменений. Реальный 2-5× undercut остаётся виден.
    # Битую ТЕКУЩУЮ цену дропаем из `prices` (не показываем); stale не трогаем.
    if len(fresh) >= 2:
        vals = sorted(d[key] for d in fresh.values())
        median = vals[len(vals) // 2]
        low_t = median * 0.12
        high_t = median / 0.12 if median > 0 else float("inf")
        for site in [s for s, d in fresh.items() if d[key] < low_t or d[key] > high_t]:
            del prices[site]
            del fresh[site]

    # <2 свежих цен (все stale или одна осталась после outlier-дропа) —
    # сравнивать не с чем: возвращаем ВСЁ None. Иначе единственная свежая цена
    # дала бы spread=None, но min/max/cheapest были бы выставлены, и frontend
    # ложно подсветил бы её как «дешёвую» (Codex MED, 2026-05-29). Stale-записи
    # остаются в `prices` для отображения с бейджем.
    if len(fresh) < 2:
        return basis, None, None, None, None

    # ── Шаг 3: stats только по свежим ──
    items = [(s, d[key]) for s, d in fresh.items()]
    mn = min(v for _, v in items)
    mx = max(v for _, v in items)
    cheap = min(items, key=lambda x: x[1])[0]
    spr = round((mx - mn) / mx * 100, 1) if mx else 0.0
    return basis, mn, mx, cheap, spr


@app.get("/api/v1/dash/comparison")
def dash_comparison(
    search: str | None = None,
    min_sites: int = 2,
    site_filter: str | None = None,
    limit: int = 5000,
    min_confidence: float = 0.70,
    category: str | None = None,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cross-site comparison rows. Filtered by tenant_id automatically.

    `min_confidence` (default 0.70): отсекаем низко-достоверные fuzzy-матчи —
    они почти всегда РАЗНЫЕ товары (Bio Kolik капли ↔ Bio sprey, Vitamin C
    таблетки ↔ Vitamin C ампула), сматченные по generic-токенам, и дают
    ложный гигантский spread в топе. is_manual=True матчи (подтверждены
    человеком) показываются всегда, независимо от confidence.
    """
    last_run = db.scalar(
        select(storage.Run.id)
        .where(storage.Run.status == "ok", storage.Run.tenant_id == user.tenant_id)
        .order_by(desc(storage.Run.id))
        .limit(1)
    )
    if not last_run:
        return []

    # Bug fix 2026-05-29: раньше `.limit(limit)` стоял на raw matches query —
    # бралось первые 500 matches по id, ПОТОМ фильтровалось по min_sites +
    # свежести цен → итог всегда ~497 независимо от объёма скрейпа (при 4824
    # matches видно только 10%). Теперь fetch ВСЕ matches, фильтруем, сортируем
    # по spread desc, и limit применяем к ОТФИЛЬТРОВАННОМУ выходу.
    # `selectinload(products)` грузит products одним доп. запросом — устраняет
    # N+1 (4824 lazy-load'а превратились бы в 4824 SELECT'а).
    matches_q = (
        select(storage.Match)
        .where(storage.Match.tenant_id == user.tenant_id)
        .options(selectinload(storage.Match.products))
    )
    if search:
        like = f"%{search.lower()}%"
        matches_q = matches_q.where(
            storage.Match.canonical_name.ilike(like) | storage.Match.canonical_brand.ilike(like)
        )
    matches = db.scalars(matches_q).all()

    # Pre-fetch latest snapshots для всех product_id одной агрегатной SELECT'ой.
    # Раньше делалось N+1 (snapshot per match × per site = ~3 за match), плюс
    # после diff-only persist'а (2026-05-09) прошлая логика `WHERE run_id ==
    # last_run` пропускала продукты без price-changes в last_run.
    all_pids = [p.id for m in matches for p in m.products]
    snaps_by_pid = storage.latest_snapshots_per_product(db, all_pids)

    now = utcnow()  # naive UTC; last_seen_at тоже naive (src/_time) — вычитание ок
    out: list[ComparisonRowOut] = []
    for m in matches:
        # Drill-down из /category-comparison: фильтр по категории товара-клиента
        # (pharmonline). None → без фильтра (обычный режим страницы сравнения).
        if category is not None:
            client_p = next((p for p in m.products if p.site == "pharmonline"), None)
            if client_p is None or client_p.category != category:
                continue
        # Confidence floor: низко-достоверные fuzzy-матчи (Bio Kolik ↔ Bio sprey)
        # дают ложный spread. Ручные (is_manual) показываем всегда.
        conf = m.confidence if m.confidence is not None else 1.0
        if not m.is_manual and conf < min_confidence:
            continue
        # Если товар-КЛИЕНТ (pharmonline) мёртв (404) — матч бесполезен (нечего
        # сравнивать с ценой клиента), дропаем целиком, а не показываем competitor-only
        # строку (аудит M1; зеркалит analytics._iter_matched_prices).
        if any(p.site == "pharmonline" and p.url_dead_at is not None for p in m.products):
            continue
        raw_prices: dict[str, dict[str, Any]] = {}
        for p in m.products:
            # «Фантомные» товары (страница 404, помечены validate-links) — скрываем,
            # чтобы не показывать матч с мёртвой ссылкой на конкурента.
            if p.url_dead_at is not None:
                continue
            snap = snaps_by_pid.get(p.id)
            price = (snap.discount_price or snap.price) if snap else None
            if price is not None and price > 0:
                # Свежесть по last_seen_at (видели в прогоне), НЕ по captured_at:
                # под diff-only стабильная цена имеет старый captured_at, но свежий
                # last_seen_at — товар активен. last_seen_at=NULL → не stale.
                age_days = _price_age_days(now, p.last_seen_at)
                raw_prices[p.site] = {
                    "price": price,
                    "is_on_sale": snap.is_on_sale if snap else False,
                    "url": p.url,
                    "product_id": p.id,
                    # pack_size + name нужны для per-unit нормализации (ниже)
                    "pack_size": p.pack_size,
                    "name": p.name,
                    "age_days": age_days,
                    "stale": age_days is not None and age_days > _COMPARISON_STALE_DAYS,
                }

        # Per-unit-aware spread + outlier-фильтр (2026-05-29). Заменяет прежний
        # raw-price 10%-median фильтр (он ошибочно дропал легитимную цену-за-
        # штуку). `_comparison_spread` нормализует цену за штуку когда фасовки
        # различаются и это уменьшает spread, и дропает parse-ошибки на unit-цене.
        # Мутирует prices (annotate + drop).
        prices = dict(raw_prices)
        basis, min_p, max_p, cheapest, spread = _comparison_spread(prices)

        sites_with_price = len(prices)
        if sites_with_price < min_sites:
            continue
        if site_filter and site_filter not in prices:
            continue
        # Убираем служебные поля из payload (оставляем pack_size/count/unit_price).
        for d in prices.values():
            d.pop("name", None)
            d.pop("count_conf", None)
        out.append(
            ComparisonRowOut(
                canonical_id=m.id,
                name=m.canonical_name,
                brand=m.canonical_brand,
                pack_size=m.canonical_pack_size,
                is_manual=m.is_manual,
                sites_with_price=sites_with_price,
                min_price=min_p,
                max_price=max_p,
                spread_pct=spread,
                cheapest_site=cheapest,
                prices=prices,
                confidence=m.confidence if m.confidence is not None else 1.0,
                needs_review=(spread is not None and spread >= 50.0),
                spread_basis=basis,
            )
        )
    # Сортируем по |spread| desc (самое полезное для PO — где конкурент бьёт
    # по цене / где можно поднять) и применяем limit к отфильтрованному выходу.
    out.sort(key=lambda r: r.spread_pct if r.spread_pct is not None else -1.0, reverse=True)
    return out[:limit]


_VALID_SITES = ("pharmonline", "aptekonline", "aloe")
_VALID_LOCALES = ("ru", "az", "en")


def _require_site(value: str) -> str:
    if value not in _VALID_SITES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown site '{value}'. Allowed: {', '.join(_VALID_SITES)}",
        )
    return value


def _normalize_locale(value: str) -> str:
    return value if value in _VALID_LOCALES else "ru"


def _humanize_category_slug(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").strip().capitalize() or value


def _localized_category_label(
    *,
    label_ru: str | None,
    label_az: str | None,
    locale: str,
    fallback: str,
) -> str:
    """Resolve a category label without leaking another language into AZ UI.

    English currently has no dedicated DB column, so it intentionally follows
    the existing Russian fallback.  Azerbaijani falls back to a readable slug,
    never to Russian; missing translations therefore stay visible to operators
    without silently presenting mixed-language UI.
    """
    normalized = _normalize_locale(locale)
    if normalized == "az":
        return (label_az or "").strip() or _humanize_category_slug(fallback)
    preferred = label_ru
    return preferred or label_ru or label_az or fallback


def _category_label_priority(
    *,
    site: str,
    slug: str,
    key: str,
    is_active: bool,
    category_id: int,
) -> tuple[bool, bool, int]:
    """Rank duplicate site-slug mappings deterministically.

    A site-native category (``pharma_<slug>``, ``aptek_<slug>`` or
    ``aloe_<slug>``) describes that site's own facet more precisely than a
    cross-site mapping which happens to reuse the same slug.  The production
    catalogue contains legitimate many-to-one mappings, so deleting duplicate
    rows or adding a uniqueness constraint would break category comparison.
    """
    prefix = {
        "pharmonline": "pharma",
        "aptekonline": "aptek",
        "aloe": "aloe",
    }[site]
    return (key == f"{prefix}_{slug}", bool(is_active), category_id)


@app.get("/api/v1/dash/roi/actions")
def dash_roi_actions(
    client_site: str = "pharmonline",
    locale: str = "ru",
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """ROI actions для client_site с локализацией.

    P0.1 (PO Audit 2026-05-17): compute_actions для 3к матчей занимает 15-30с
    и frontend timeout'ит на 15с (API 408 на 4 экранах). Сейчас читаем из
    roi_actions_cache (pre-computed только после verified full-catalog run).
    Если кэш отсутствует, stale или относится к partial/degraded run — 503.
    Inline fallback запрещён: он читал globally-latest snapshots и мог тихо
    смешать подтверждённые данные с частичным прогоном.

    Параметр locale (ru/az/en) применяется поверх кэша — title/detail
    реконструируются из структурных полей, кэш не инвалидируется.
    """
    from src import roi

    _require_site(client_site)
    locale = _normalize_locale(locale)

    cached = roi.get_cached_actions(db, client_site, tenant_id=user.tenant_id)
    if cached is not None:
        return [roi.translate_action(a, locale) for a in cached]

    raise HTTPException(
        503,
        "Verified full-catalog recommendations are not available yet",
    )


@app.get("/api/v1/dash/alerts")
def dash_alerts(
    limit: int = 100,
    severity: str | None = None,
    include_read: bool = False,
    include_snoozed: bool = False,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """In-app inbox (P1.4 PO Audit 2026-05-17).

    Default фильтры — скрыть прочитанные + те, что отложены до будущего
    времени (snoozed_until > NOW). Это даёт «inbox-style» feed: только то,
    что требует внимания.
    """
    from src._time import utcnow as _now

    stmt = (
        select(storage.AlertEvent)
        .where(storage.AlertEvent.tenant_id == user.tenant_id)
        .order_by(desc(storage.AlertEvent.created_at))
        .limit(limit)
    )
    if severity:
        stmt = stmt.where(storage.AlertEvent.severity == severity)
    if not include_read:
        stmt = stmt.where(storage.AlertEvent.is_read.is_(False))
    if not include_snoozed:
        now = _now()
        stmt = stmt.where(
            (storage.AlertEvent.snoozed_until.is_(None)) | (storage.AlertEvent.snoozed_until <= now)
        )
    events = db.scalars(stmt).all()
    return [
        {
            "id": e.id,
            "rule_type": getattr(e, "rule_type", None),
            "severity": e.severity,
            "title": e.title,
            "detail": e.detail,
            "payload": e.payload,
            "created_at": e.created_at.isoformat(),
            "is_read": bool(getattr(e, "is_read", False)),
            "read_at": e.read_at.isoformat() if getattr(e, "read_at", None) else None,
            "snoozed_until": (
                e.snoozed_until.isoformat() if getattr(e, "snoozed_until", None) else None
            ),
        }
        for e in events
    ]


@app.get("/api/v1/dash/alerts/counts")
def dash_alerts_counts(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Counts: unread / snoozed / read — для nav-badge."""
    from src._time import utcnow as _now

    now = _now()
    base = (
        select(func.count())
        .select_from(storage.AlertEvent)
        .where(storage.AlertEvent.tenant_id == user.tenant_id)
    )
    unread = (
        db.scalar(
            base.where(
                storage.AlertEvent.is_read.is_(False),
                (storage.AlertEvent.snoozed_until.is_(None))
                | (storage.AlertEvent.snoozed_until <= now),
            )
        )
        or 0
    )
    snoozed = (
        db.scalar(
            base.where(
                storage.AlertEvent.is_read.is_(False),
                storage.AlertEvent.snoozed_until.is_not(None),
                storage.AlertEvent.snoozed_until > now,
            )
        )
        or 0
    )
    read = db.scalar(base.where(storage.AlertEvent.is_read.is_(True))) or 0
    total = db.scalar(base) or 0
    return {
        "unread": int(unread),
        "snoozed": int(snoozed),
        "read": int(read),
        "total": int(total),
    }


class _AlertPatchPayload(BaseModel):
    """PATCH single alert: пометить read или snooze до даты."""

    is_read: bool | None = None
    snooze_hours: int | None = None  # alias: snooze на N часов от now


@app.patch("/api/v1/dash/alerts/{alert_id}")
def dash_alert_patch(
    alert_id: int,
    payload: _AlertPatchPayload,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    from datetime import timedelta as _td
    from src._time import utcnow as _now

    alert = db.scalar(
        select(storage.AlertEvent).where(
            storage.AlertEvent.id == alert_id,
            storage.AlertEvent.tenant_id == user.tenant_id,
        )
    )
    if not alert:
        raise HTTPException(404, "Alert not found")
    if payload.is_read is not None:
        alert.is_read = payload.is_read
        alert.read_at = _now() if payload.is_read else None
    if payload.snooze_hours is not None:
        if payload.snooze_hours == 0:
            alert.snoozed_until = None
        else:
            alert.snoozed_until = _now() + _td(hours=payload.snooze_hours)
    db.commit()
    return {"ok": True, "id": alert.id}


class _AlertBulkPayload(BaseModel):
    """Bulk action на список ID. action ∈ {mark_read, mark_unread, snooze_24h, snooze_7d, snooze_clear}."""

    ids: list[int]
    action: str


@app.post("/api/v1/dash/alerts/bulk")
def dash_alerts_bulk(
    payload: _AlertBulkPayload,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    from datetime import timedelta as _td
    from sqlalchemy import update as _update
    from src._time import utcnow as _now

    if not payload.ids:
        return {"affected": 0}

    base_filter = (storage.AlertEvent.id.in_(payload.ids)) & (
        storage.AlertEvent.tenant_id == user.tenant_id
    )
    now = _now()
    if payload.action == "mark_read":
        values = {"is_read": True, "read_at": now}
    elif payload.action == "mark_unread":
        values = {"is_read": False, "read_at": None}
    elif payload.action == "snooze_24h":
        values = {"snoozed_until": now + _td(hours=24)}
    elif payload.action == "snooze_7d":
        values = {"snoozed_until": now + _td(days=7)}
    elif payload.action == "snooze_clear":
        values = {"snoozed_until": None}
    else:
        raise HTTPException(400, f"Unknown action: {payload.action}")

    result = db.execute(_update(storage.AlertEvent).where(base_filter).values(**values))
    db.commit()
    return {"affected": result.rowcount, "action": payload.action}


@app.post("/api/v1/dash/alerts/mark-all-read")
def dash_alerts_mark_all_read(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Пометить все непрочитанные алерты (inbox) как прочитанные одним запросом."""
    from sqlalchemy import update as _update
    from src._time import utcnow as _now

    now = _now()
    result = db.execute(
        _update(storage.AlertEvent)
        .where(
            storage.AlertEvent.tenant_id == user.tenant_id,
            storage.AlertEvent.is_read == False,  # noqa: E712
        )
        .values(is_read=True, read_at=now)
    )
    db.commit()
    return {"affected": result.rowcount}


@app.get("/api/v1/dash/match-quality")
def dash_match_quality(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    from src import analytics

    mq = analytics.match_quality(db)
    return {
        "total_matches": mq.total_matches,
        "auto_matches": mq.auto_matches,
        "manual_matches": mq.manual_matches,
        "rejected_pairs": mq.rejected_pairs,
        "products_total": mq.products_total,
        "products_matched": mq.products_matched,
        "coverage_pct": mq.coverage_pct,
        "manual_pct": mq.manual_pct,
    }


@app.get("/api/v1/dash/normalize/stats")
def dash_normalize_stats(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Coverage AI-нормализатора и распределение match.strategy.

    Используется на /overview как KPI здоровья. Когда coverage падает <80%,
    это сигнал что промпт перестал работать на новых SKU или бюджет исчерпан.
    """
    products_total = (
        db.scalar(
            select(func.count(storage.Product.id)).where(
                storage.Product.tenant_id == user.tenant_id
            )
        )
        or 0
    )

    # Нормализованным считается продукт у которого заполнен name_normalized
    products_normalized = (
        db.scalar(
            select(func.count(storage.Product.id)).where(
                storage.Product.tenant_id == user.tenant_id,
                storage.Product.name_normalized != "",
            )
        )
        or 0
    )

    # needs_review — маркер на Match (spread ≥50% между сайтами)
    needs_review = (
        db.scalar(
            select(func.count(storage.Match.id)).where(
                storage.Match.tenant_id == user.tenant_id,
                storage.Match.needs_review == True,  # noqa: E712
            )
        )
        or 0
    )

    # Распределение matches по типу: ручной vs авто
    # SQLAlchemy column comparisons require `==` operator (not Python truth),
    # hence E712 noqa.
    matches_by_strategy = {
        "auto": db.scalar(
            select(func.count(storage.Match.id)).where(
                storage.Match.tenant_id == user.tenant_id,
                storage.Match.is_manual == False,  # noqa: E712
            )
        )
        or 0,
        "manual": db.scalar(
            select(func.count(storage.Match.id)).where(
                storage.Match.tenant_id == user.tenant_id,
                storage.Match.is_manual == True,  # noqa: E712
            )
        )
        or 0,
    }

    coverage_pct = round(products_normalized / products_total * 100, 1) if products_total else 0.0
    return {
        "products_total": products_total,
        "products_normalized": products_normalized,
        "needs_review": needs_review,
        "coverage_pct": coverage_pct,
        "last_normalized_at": None,  # не отслеживается пока
        "matches_by_strategy": matches_by_strategy,
    }


@app.get("/api/v1/dash/brand-share")
def dash_brand_share(
    top_n: int = 30,
    site: str | None = None,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    from src import analytics

    if site is not None:
        _require_site(site)
    rows = analytics.brand_share(db, top_n=top_n, site=site)
    return [
        {
            "brand": r.brand,
            "counts": dict(r.counts),
            "total": r.total,
            "sites_with_brand": r.sites_with_brand,
            "exclusive_to": r.exclusive_to,
        }
        for r in rows
    ]


@app.get("/api/v1/dash/price-index")
def dash_price_index(
    client_site: str = "pharmonline",
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    from src import analytics

    _require_site(client_site)
    rows = analytics.price_index_by_category(db, client_site=client_site, tenant_id=user.tenant_id)
    return [
        {
            "category": getattr(r, "category", None),
            "avg_client_price": getattr(r, "avg_client_price", None),
            "avg_competitor_price": getattr(r, "avg_competitor_price", None),
            "index": getattr(r, "index", None),
            "matched_skus": getattr(r, "matched_skus", None),
        }
        for r in rows
    ]


@app.get("/api/v1/dash/category-comparison", response_model=list[CategoryComparisonOut])
def dash_category_comparison(
    client_site: str = "pharmonline",
    locale: str = "ru",
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Сравнение цен по категориям (client_site vs конкуренты), tenant-scoped.

    Параллель товарному /comparison, но агрегировано по категории товара-клиента:
    per-site средние (aptekonline/aloe раздельно), ценовой индекс, % SKU где
    клиент дешевле/дороже/паритет. `label` резолвится по locale (label_ru/az)
    с graceful fallback на сырой slug (если категории нет в таблице Category).
    Дефолт-сортировка — наибольший мисприсинг (|index-100|×matched_skus).
    """
    from src import analytics

    _require_site(client_site)
    if locale not in ("ru", "az", "en"):
        locale = "ru"

    rows = analytics.category_comparison(db, client_site=client_site, tenant_id=user.tenant_id)
    out: list[CategoryComparisonOut] = []
    for r in rows:
        label = _localized_category_label(
            label_ru=r.label_ru,
            label_az=r.label_az,
            locale=locale,
            fallback=r.category,
        )
        out.append(
            CategoryComparisonOut(
                category=r.category,
                label=label,
                matched_skus=r.matched_skus,
                avg_client_price=r.avg_client_price,
                per_site_avg=r.per_site_avg,
                avg_competitor_price=r.avg_competitor_price,
                index=r.index,
                cheaper_count=r.cheaper_count,
                pricier_count=r.pricier_count,
                parity_count=r.parity_count,
                cheaper_pct=r.cheaper_pct,
                pricier_pct=r.pricier_pct,
                parity_pct=r.parity_pct,
            )
        )
    return out


@app.get("/api/v1/dash/products/{product_id}/price-history")
def dash_product_price_history(
    product_id: int,
    days: int = 30,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """История цен одного продукта за последние N дней (sparkline data).

    Для каждого дня берётся latest snapshot этого продукта в данных сутках
    (UTC). Возвращает массив `[{date, price, is_on_sale}]` отсортированный
    по дате asc. Пустые дни пропускаются (без интерполяции — клиент рисует
    sparkline как-есть).
    """
    days = max(1, min(days, 365))
    cutoff = utcnow() - timedelta(days=days)

    product = db.scalar(
        select(storage.Product).where(
            storage.Product.id == product_id,
            storage.Product.tenant_id == user.tenant_id,
        )
    )
    if not product:
        raise HTTPException(404, "Product not found")

    rows = db.execute(
        select(
            func.date(storage.PriceSnapshot.captured_at).label("d"),
            func.max(storage.PriceSnapshot.captured_at).label("ts"),
        )
        .where(
            storage.PriceSnapshot.product_id == product_id,
            storage.PriceSnapshot.captured_at >= cutoff,
        )
        .group_by("d")
        .order_by("d")
    ).all()
    timestamps = [r.ts for r in rows]
    if not timestamps:
        return {"product_id": product_id, "site": product.site, "points": []}

    snaps = db.scalars(
        select(storage.PriceSnapshot).where(
            storage.PriceSnapshot.product_id == product_id,
            storage.PriceSnapshot.captured_at.in_(timestamps),
        )
    ).all()
    points = sorted(
        [
            {
                "date": s.captured_at.date().isoformat(),
                "price": s.discount_price or s.price,
                "is_on_sale": bool(s.is_on_sale),
            }
            for s in snaps
            if (s.discount_price or s.price) is not None
        ],
        key=lambda p: p["date"],
    )

    # delta % за период
    delta_pct = None
    if len(points) >= 2:
        first = points[0]["price"]
        last = points[-1]["price"]
        if first:
            delta_pct = round((last - first) / first * 100, 1)

    return {
        "product_id": product_id,
        "site": product.site,
        "name": product.name,
        "days": days,
        "points": points,
        "delta_pct": delta_pct,
        "current": points[-1]["price"] if points else None,
    }


# Batch версия price-history — устраняет N+1 на /comparison (TrendPanel
# раньше делал по 3 fetch'а на каждую раскрытую строку). Принимает
# ?ids=1,2,3&days=30, возвращает dict[product_id_str] → ту же payload-схему,
# что и single-product endpoint. Лимит ids ≤ 50 чтобы не уехать в slow-query
# при злоумышленном wildcard'е.
_BATCH_PRICE_HISTORY_MAX_IDS = 50


@app.get("/api/v1/dash/products/price-history")
def dash_products_price_history_batch(
    ids: str,
    days: int = 30,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, dict[str, Any]]:
    """Batch версия истории цен — за один запрос для нескольких продуктов.

    Принимает `ids=1,2,3` (comma-separated, max 50). Возвращает dict
    keyed by product_id (как строка — JSON ключи всегда строки):

        {
          "<product_id>": {
            "product_id": int,
            "site": str,
            "name": str,
            "days": int,
            "points": [{"date", "price", "is_on_sale"}],
            "delta_pct": float | None,
            "current": float | None,
          },
          ...
        }

    Продукты не принадлежащие tenant'у или не найденные просто пропускаются
    (нет в ответе). Frontend проверяет наличие ключа.
    """
    days = max(1, min(days, 365))
    cutoff = utcnow() - timedelta(days=days)

    # DoS guard #1 (Codex review 2026-05-28): raw `ids` string length cap.
    # 50 IDs × 10 digits + commas ≈ 550 chars — берём с запасом 2K.
    if len(ids) > 2048:
        raise HTTPException(400, "ids parameter too long")

    try:
        id_list = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers")
    if not id_list:
        return {}
    # DoS guard #2: dedupe + cap. Дубли в `ids=1,1,1,1,...` раньше проходили
    # как 50 уникальных слотов с одним product_id.
    id_list = list(set(id_list))
    if len(id_list) > _BATCH_PRICE_HISTORY_MAX_IDS:
        raise HTTPException(
            400,
            f"too many ids (max {_BATCH_PRICE_HISTORY_MAX_IDS})",
        )

    # Tenant-safe lookup продуктов
    products = db.scalars(
        select(storage.Product).where(
            storage.Product.id.in_(id_list),
            storage.Product.tenant_id == user.tenant_id,
        )
    ).all()
    if not products:
        return {}
    product_by_id = {p.id: p for p in products}
    valid_ids = list(product_by_id.keys())

    # Single SQL fetch: все snapshots в окне для valid_ids, затем reduce
    # в Python к "latest per (product_id, date)". Это правильнее старого
    # подхода (2 запроса с huge IN-list of timestamps — могло достигать
    # 50*365=18250 элементов в WHERE clause при days=365).
    # DoS guard #3 (Codex review): убираем потенциально гигантский `IN (...)`.
    all_snaps = db.scalars(
        select(storage.PriceSnapshot)
        .where(
            storage.PriceSnapshot.product_id.in_(valid_ids),
            storage.PriceSnapshot.captured_at >= cutoff,
        )
        .order_by(
            storage.PriceSnapshot.product_id,
            storage.PriceSnapshot.captured_at,
        )
    ).all()

    if not all_snaps:
        return {
            str(p.id): {
                "product_id": p.id,
                "site": p.site,
                "name": p.name,
                "days": days,
                "points": [],
                "delta_pct": None,
                "current": None,
            }
            for p in products
        }

    # Reduce в Python: latest snapshot per (product_id, day).
    # Группируем по (pid, date) и берём самый поздний captured_at.
    by_pid_day: dict[tuple[int, str], storage.PriceSnapshot] = {}
    for s in all_snaps:
        key = (s.product_id, s.captured_at.date().isoformat())
        prev = by_pid_day.get(key)
        if prev is None or s.captured_at > prev.captured_at:
            by_pid_day[key] = s

    snaps_by_pid: dict[int, list[storage.PriceSnapshot]] = defaultdict(list)
    for (pid, _date), s in by_pid_day.items():
        snaps_by_pid[pid].append(s)

    out: dict[str, dict[str, Any]] = {}
    for p in products:
        product_snaps = snaps_by_pid.get(p.id, [])
        points = sorted(
            [
                {
                    "date": s.captured_at.date().isoformat(),
                    "price": s.discount_price or s.price,
                    "is_on_sale": bool(s.is_on_sale),
                }
                for s in product_snaps
                if (s.discount_price or s.price) is not None
            ],
            key=lambda x: x["date"],
        )
        delta_pct = None
        if len(points) >= 2:
            first = points[0]["price"]
            last = points[-1]["price"]
            if first:
                delta_pct = round((last - first) / first * 100, 1)
        out[str(p.id)] = {
            "product_id": p.id,
            "site": p.site,
            "name": p.name,
            "days": days,
            "points": points,
            "delta_pct": delta_pct,
            "current": points[-1]["price"] if points else None,
        }
    return out


@app.get("/api/v1/dash/forecast/movers")
def dash_forecast_movers(
    limit: int = 20,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    from src import forecast

    movers = forecast.top_movers(db, limit=limit)
    return movers


@app.get("/api/v1/dash/products")
def dash_products(
    site: str,
    category: str | None = None,
    brand: str | None = None,
    search: str | None = None,
    on_sale: bool | None = None,
    limit: int = 200,
    offset: int = 0,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Список продуктов одного сайта с актуальной ценой.

    Используется страницей /aloe (и потенциально /pharmonline, /aptekonline).
    Возвращает latest snapshot per product (diff-only-aware).
    """
    _require_site(site)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    stmt = (
        select(storage.Product)
        .where(
            storage.Product.site == site,
            storage.Product.tenant_id == user.tenant_id,
            storage.Product.url_dead_at.is_(None),
        )
        .order_by(desc(storage.Product.last_seen_at))
    )
    if category:
        stmt = stmt.where(storage.Product.category == category)
    if brand:
        stmt = stmt.where(storage.Product.brand == brand)
    if search:
        like = f"%{search.lower()}%"
        stmt = stmt.where(storage.Product.name.ilike(like) | storage.Product.brand.ilike(like))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    products = db.scalars(stmt.offset(offset).limit(limit)).all()
    if not products:
        return {"items": [], "total": total, "limit": limit, "offset": offset}

    snaps_by_pid = storage.latest_snapshots_per_product(db, [p.id for p in products])

    items = []
    for p in products:
        snap = snaps_by_pid.get(p.id)
        eff_price = (snap.discount_price or snap.price) if snap else None
        if on_sale is not None:
            is_sale = bool(snap and snap.is_on_sale) if snap else False
            if on_sale != is_sale:
                continue
        items.append(
            {
                "id": p.id,
                "external_id": p.external_id,
                "name": p.name,
                "brand": p.brand,
                "category": p.category,
                "url": p.url,
                "image_url": p.image_url,
                "price": snap.price if snap else None,
                "discount_price": snap.discount_price if snap else None,
                "effective_price": eff_price,
                "is_on_sale": bool(snap and snap.is_on_sale) if snap else False,
                "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/dash/products/facets")
def dash_products_facets(
    site: str,
    locale: str = "ru",
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Уникальные категории/бренды одного сайта — для UI-фильтров.

    Возвращает счётчики per-category и per-brand, отсортированные desc.
    Лёгкая операция: один GROUP BY на сайт.
    """
    _require_site(site)

    cat_rows = db.execute(
        select(storage.Product.category, func.count(storage.Product.id))
        .where(
            storage.Product.site == site,
            storage.Product.tenant_id == user.tenant_id,
            storage.Product.category.is_not(None),
            storage.Product.url_dead_at.is_(None),
        )
        .group_by(storage.Product.category)
        .order_by(desc(func.count(storage.Product.id)))
    ).all()

    brand_rows = db.execute(
        select(storage.Product.brand, func.count(storage.Product.id))
        .where(
            storage.Product.site == site,
            storage.Product.tenant_id == user.tenant_id,
            storage.Product.brand.is_not(None),
            storage.Product.url_dead_at.is_(None),
        )
        .group_by(storage.Product.brand)
        .order_by(desc(func.count(storage.Product.id)))
        .limit(100)
    ).all()

    locale = _normalize_locale(locale)

    # Lookup slug → localized label. Slug-format на каждом сайте свой, но
    # Category-таблица связывает их и хранит RU/AZ display labels.
    slug_col = {
        "pharmonline": storage.Category.pharmonline_slug,
        "aptekonline": storage.Category.aptekonline_slug,
        "aloe": storage.Category.aloe_slug,
    }[site]
    label_rows = db.execute(
        select(
            slug_col,
            storage.Category.id,
            storage.Category.key,
            storage.Category.label_ru,
            storage.Category.label_az,
            storage.Category.is_active,
        ).where(slug_col.is_not(None))
    ).all()
    preferred_labels: dict[str, tuple[tuple[bool, bool, int], str]] = {}
    for slug, category_id, key, label_ru, label_az, is_active in label_rows:
        if not slug:
            continue
        priority = _category_label_priority(
            site=site,
            slug=slug,
            key=key,
            is_active=is_active,
            category_id=category_id,
        )
        label = _localized_category_label(
            label_ru=label_ru,
            label_az=label_az,
            locale=locale,
            fallback=slug,
        )
        current = preferred_labels.get(slug)
        if current is None or priority > current[0]:
            preferred_labels[slug] = (priority, label)
    slug_to_label = {slug: value[1] for slug, value in preferred_labels.items()}

    def _filter_internal(name: str) -> bool:
        """Скрыть технические category-маркеры из UI (e.g. product_field=bestseller)."""
        return not (name.startswith("product_field=") or name.startswith("__"))

    return {
        "categories": [
            {"name": c, "label": slug_to_label.get(c) or c, "count": n}
            for c, n in cat_rows
            if c and _filter_internal(c)
        ],
        "brands": [{"name": b, "count": n} for b, n in brand_rows if b],
    }


@app.get("/api/v1/dash/products/summary")
def dash_products_summary(
    site: str,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """KPI-карточки страницы сайта: всего продуктов, брендов, exclusive, %sale.

    Использует latest snapshot per product (diff-only-aware) для подсчёта
    `% on_sale`. Exclusive_brands считается через brand_share с site=… —
    переиспользует общую логику.
    """
    _require_site(site)
    from src import analytics

    total_products = (
        db.scalar(
            select(func.count(storage.Product.id)).where(
                storage.Product.site == site,
                storage.Product.tenant_id == user.tenant_id,
            )
        )
        or 0
    )

    total_brands = (
        db.scalar(
            select(func.count(func.distinct(storage.Product.brand))).where(
                storage.Product.site == site,
                storage.Product.tenant_id == user.tenant_id,
                storage.Product.brand.is_not(None),
            )
        )
        or 0
    )

    brand_rows = analytics.brand_share(db, top_n=10_000, site=site)
    exclusive_brands = sum(1 for r in brand_rows if r.exclusive_to == site)

    pids = db.scalars(
        select(storage.Product.id).where(
            storage.Product.site == site,
            storage.Product.tenant_id == user.tenant_id,
        )
    ).all()
    snaps_by_pid = storage.latest_snapshots_per_product(db, pids)
    on_sale = sum(1 for s in snaps_by_pid.values() if s.is_on_sale)
    on_sale_pct = round(on_sale / total_products * 100, 1) if total_products else 0.0

    last_run = db.scalar(
        select(storage.Run)
        .where(
            storage.Run.status == "ok",
            storage.Run.tenant_id == user.tenant_id,
        )
        .order_by(desc(storage.Run.id))
        .limit(1)
    )

    return {
        "total_products": total_products,
        "total_brands": total_brands,
        "exclusive_brands": exclusive_brands,
        "on_sale_count": on_sale,
        "on_sale_pct": on_sale_pct,
        "last_run_at": last_run.started_at.isoformat()
        if last_run and last_run.started_at
        else None,
        "last_run_id": last_run.id if last_run else None,
    }


@app.get("/api/v1/dash/runs")
def dash_runs(
    limit: int = 30,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    runs = db.scalars(
        select(storage.Run)
        .where(storage.Run.tenant_id == user.tenant_id)
        .order_by(desc(storage.Run.id))
        .limit(limit)
    ).all()
    return [_run_row_out(r) for r in runs]


def _run_row_out(r: storage.Run) -> dict:
    return {
        "id": r.id,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "status": r.status,
        "products_scraped": r.products_scraped,
        "products_per_site": r.products_per_site,
        "run_quality": r.run_quality,
        "sites_completed": r.sites_completed,
        "error_message": r.error_message,
    }


@app.get("/api/v1/dash/runs/latest-by-site")
def dash_runs_latest_by_site(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Latest run that touched each monitored site.

    `/overview` also shows the last N runs globally. Weekly sites such as
    aptekonline can be pushed out of that short list by intraday pharmonline
    and aloe runs, so this endpoint returns one row per site.
    """
    runs = db.scalars(
        select(storage.Run)
        .where(storage.Run.tenant_id == user.tenant_id)
        .order_by(desc(storage.Run.id))
        .limit(500)
    ).all()
    latest: dict[str, storage.Run] = {}
    for run in runs:
        per_site = run.products_per_site or {}
        for site in _VALID_SITES:
            if site in latest:
                continue
            if site in per_site:
                latest[site] = run
        if len(latest) == len(_VALID_SITES):
            break

    return [
        {"site": site, "run": _run_row_out(latest[site]) if site in latest else None}
        for site in _VALID_SITES
    ]


@app.get("/api/v1/dash/runs/{run_id}/breakdown")
def dash_run_breakdown(
    run_id: int,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Подробный breakdown прогона: сколько товаров на каждом сайте по каждой
    категории. Используется UI «Coverage» панелью на /overview и для debug.

    Returns: {
      "run_id": int, "started_at": iso, "finished_at": iso, "status": str,
      "products_scraped": int,
      "products_per_site": {site: total_count},
      "products_per_site_category": {site: {category: count}},
      "run_quality": {version, mode, financially_eligible, sites}
    }
    """
    run = db.scalar(
        select(storage.Run).where(
            storage.Run.id == run_id,
            storage.Run.tenant_id == user.tenant_id,
        )
    )
    if not run:
        raise HTTPException(404, "Run not found")
    return {
        "run_id": run.id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "status": run.status,
        "products_scraped": run.products_scraped or 0,
        "products_per_site": run.products_per_site or {},
        "products_per_site_category": run.products_per_site_category or {},
        "run_quality": run.run_quality,
        "sites_completed": run.sites_completed,
    }


@app.get("/api/v1/dash/categories")
def dash_categories_list(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    cats = db.scalars(select(storage.Category).order_by(storage.Category.id)).all()
    return [
        {
            "id": c.id,
            "key": c.key,
            "label_ru": c.label_ru,
            "label_az": c.label_az,
            "pharmonline_slug": c.pharmonline_slug,
            "aptekonline_slug": c.aptekonline_slug,
            "aloe_slug": c.aloe_slug,
            "is_active": c.is_active,
        }
        for c in cats
    ]


@app.post("/api/v1/dash/categories", response_model=CategoryOut, status_code=201)
def dash_categories_create(
    payload: CategoryIn,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    if user.role not in ("admin", "owner"):
        raise HTTPException(403, "Admin role required")
    if db.scalar(select(storage.Category).where(storage.Category.key == payload.key)):
        raise HTTPException(409, f"Category with key '{payload.key}' already exists")
    cat = storage.Category(**payload.model_dump())
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return CategoryOut(id=cat.id, **payload.model_dump())


@app.patch("/api/v1/dash/categories/{cat_id}")
def dash_categories_update(
    cat_id: int,
    payload: CategoryIn,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    if user.role not in ("admin", "owner"):
        raise HTTPException(403, "Admin role required")
    cat = db.scalar(select(storage.Category).where(storage.Category.id == cat_id))
    if not cat:
        raise HTTPException(404, "Category not found")
    # Если key меняется, проверяем что не конфликтует с другой строкой
    # (key — UNIQUE в БД; raw IntegrityError даёт 500, отдадим 409 явно).
    if payload.key != cat.key:
        existing = db.scalar(
            select(storage.Category).where(
                storage.Category.key == payload.key,
                storage.Category.id != cat_id,
            )
        )
        if existing:
            raise HTTPException(409, f"Category with key '{payload.key}' already exists")
    for k, v in payload.model_dump().items():
        setattr(cat, k, v)
    db.commit()
    return {"ok": True}


@app.delete("/api/v1/dash/categories/{cat_id}", status_code=204)
def dash_categories_delete(
    cat_id: int,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    if user.role not in ("admin", "owner"):
        raise HTTPException(403, "Admin role required")
    cat = db.scalar(select(storage.Category).where(storage.Category.id == cat_id))
    if not cat:
        raise HTTPException(404, "Category not found")
    db.delete(cat)
    db.commit()
    return Response(status_code=204)


@app.get("/api/v1/dash/categories/suggestions")
def dash_category_suggestions(
    site_a: str,
    site_b: str,
    min_overlap: int = 3,
    limit: int = 30,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """P1.1 (PO Audit 2026-05-17): Категория-мапер — подсказки.

    Для каждой пары (slug_a на site_a, slug_b на site_b) считаем shared
    brand count. Высокий overlap = реальные сабжовые категории на разных
    сайтах. Skip pair'ы где обе slug уже в одной Category row (mapping есть).

    Cross-3 categories = 1 (только uşaq-qidası). Через этот endpoint оператор
    видит топ-30 кандидатов на mapping → одним кликом создаёт Category row.

    Returns: [{
        site_a_slug, site_b_slug, shared_brands_count,
        sample_brands (top 5), a_products, b_products,
        already_mapped (bool)
    }]
    """
    _require_site(site_a)
    _require_site(site_b)
    if site_a == site_b:
        raise HTTPException(400, "site_a и site_b должны различаться")

    # Кол-во продуктов и shared brands per (slug_a, slug_b) пара
    # Используем self-JOIN по brand. Один SQL, agg на стороне БД.
    Product = storage.Product
    PA = Product.__table__.alias("pa")
    PB = Product.__table__.alias("pb")
    rows = db.execute(
        select(
            PA.c.category.label("slug_a"),
            PB.c.category.label("slug_b"),
            func.count(func.distinct(PA.c.brand)).label("shared_brands"),
        )
        .where(
            PA.c.site == site_a,
            PB.c.site == site_b,
            PA.c.brand.is_not(None),
            PB.c.brand.is_not(None),
            PA.c.brand == PB.c.brand,
            PA.c.category.is_not(None),
            PB.c.category.is_not(None),
        )
        .group_by(PA.c.category, PB.c.category)
        .having(func.count(func.distinct(PA.c.brand)) >= min_overlap)
        .order_by(func.count(func.distinct(PA.c.brand)).desc())
        .limit(limit * 3)  # extra slots для filter'а already_mapped
    ).all()

    # Уже-mapped pairs из Category table
    cat_slug_a = getattr(storage.Category, f"{site_a}_slug")
    cat_slug_b = getattr(storage.Category, f"{site_b}_slug")
    existing_mappings = set(
        db.execute(
            select(cat_slug_a, cat_slug_b).where(cat_slug_a.is_not(None), cat_slug_b.is_not(None))
        ).all()
    )

    out = []
    for slug_a, slug_b, n_brands in rows:
        already = (slug_a, slug_b) in existing_mappings
        # Top-5 shared brands as sample
        sample = db.scalars(
            select(PA.c.brand)
            .where(
                PA.c.site == site_a,
                PA.c.brand.is_not(None),
                PA.c.category == slug_a,
                PA.c.brand.in_(
                    select(PB.c.brand).where(
                        PB.c.site == site_b,
                        PB.c.brand.is_not(None),
                        PB.c.category == slug_b,
                    )
                ),
            )
            .distinct()
            .limit(5)
        ).all()
        # Counts
        a_count = (
            db.scalar(
                select(func.count())
                .select_from(Product)
                .where(Product.site == site_a, Product.category == slug_a)
            )
            or 0
        )
        b_count = (
            db.scalar(
                select(func.count())
                .select_from(Product)
                .where(Product.site == site_b, Product.category == slug_b)
            )
            or 0
        )
        out.append(
            {
                "site_a_slug": slug_a,
                "site_b_slug": slug_b,
                "shared_brands_count": int(n_brands),
                "sample_brands": list(sample),
                "site_a_products": int(a_count),
                "site_b_products": int(b_count),
                "already_mapped": already,
            }
        )
        if len([o for o in out if not o["already_mapped"]]) >= limit:
            break
    return out


class _MapCategoryPayload(BaseModel):
    """Create или extend Category mapping одним кликом."""

    site_a: str
    site_a_slug: str
    site_b: str
    site_b_slug: str
    label_ru: str | None = None
    label_az: str | None = None

    @field_validator("label_ru", "label_az")
    @classmethod
    def strip_optional_category_label(cls, value: str | None) -> str | None:
        value = value.strip() if value else ""
        return value or None


@app.post("/api/v1/dash/categories/mapping", status_code=201)
def dash_category_mapping_create(
    payload: _MapCategoryPayload,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Создать новую Category row из suggested mapping.

    Логика: если уже есть Category row где `{site_a}_slug == payload.site_a_slug` —
    extend её (добавим site_b_slug). Иначе создать новую.
    """
    if user.role not in ("admin", "owner"):
        raise HTTPException(403, "Admin role required")
    _require_site(payload.site_a)
    _require_site(payload.site_b)
    if payload.site_a == payload.site_b:
        raise HTTPException(400, "site_a и site_b должны различаться")

    col_a = getattr(storage.Category, f"{payload.site_a}_slug")
    col_b = getattr(storage.Category, f"{payload.site_b}_slug")

    # Existing row для site_a slug?
    existing = db.scalar(select(storage.Category).where(col_a == payload.site_a_slug))
    if existing:
        # Extend: добавим site_b slug если ещё нет
        if (
            getattr(existing, f"{payload.site_b}_slug")
            and getattr(existing, f"{payload.site_b}_slug") != payload.site_b_slug
        ):
            raise HTTPException(
                409,
                f"Категория уже маппирована на {payload.site_b}: "
                f"{getattr(existing, f'{payload.site_b}_slug')}",
            )
        setattr(existing, f"{payload.site_b}_slug", payload.site_b_slug)
        if not (existing.label_az or "").strip():
            existing.label_az = payload.label_az or _humanize_category_slug(
                payload.site_a_slug
                if not payload.site_a_slug.isdigit()
                else payload.site_b_slug
            )
        db.commit()
        return {"id": existing.id, "action": "extended", "key": existing.key}

    # Existing row для site_b slug? (symmetric)
    existing_b = db.scalar(select(storage.Category).where(col_b == payload.site_b_slug))
    if existing_b:
        if (
            getattr(existing_b, f"{payload.site_a}_slug")
            and getattr(existing_b, f"{payload.site_a}_slug") != payload.site_a_slug
        ):
            raise HTTPException(
                409,
                f"Категория уже маппирована на {payload.site_a}: "
                f"{getattr(existing_b, f'{payload.site_a}_slug')}",
            )
        setattr(existing_b, f"{payload.site_a}_slug", payload.site_a_slug)
        if not (existing_b.label_az or "").strip():
            existing_b.label_az = payload.label_az or _humanize_category_slug(
                payload.site_b_slug
                if not payload.site_b_slug.isdigit()
                else payload.site_a_slug
            )
        db.commit()
        return {"id": existing_b.id, "action": "extended", "key": existing_b.key}

    # New row — генерим уникальный key из slug'а
    base_key = payload.site_a_slug.replace("/", "-").replace("=", "-")[:80]
    key = base_key
    seq = 2
    while db.scalar(select(storage.Category).where(storage.Category.key == key)):
        key = f"{base_key}_{seq}"
        seq += 1

    label = payload.label_ru or _humanize_category_slug(payload.site_a_slug)
    label_az = payload.label_az or _humanize_category_slug(
        payload.site_a_slug if not payload.site_a_slug.isdigit() else payload.site_b_slug
    )
    cat = storage.Category(
        key=key,
        label_ru=label,
        label_az=label_az,
        pharmonline_slug=payload.site_a_slug
        if payload.site_a == "pharmonline"
        else (payload.site_b_slug if payload.site_b == "pharmonline" else None),
        aptekonline_slug=payload.site_a_slug
        if payload.site_a == "aptekonline"
        else (payload.site_b_slug if payload.site_b == "aptekonline" else None),
        aloe_slug=payload.site_a_slug
        if payload.site_a == "aloe"
        else (payload.site_b_slug if payload.site_b == "aloe" else None),
        is_active=True,
    )
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return {"id": cat.id, "action": "created", "key": cat.key}


# ── Phase 2.5 (2026-05-27) — borderline-match suggestion queue ─────────────
# Listing matches that look uncertain (low confidence OR needs_review flag set)
# so a human can quickly confirm / reject from the dashboard. Goal: drive
# false-match rate from 8.4% toward <2% by cleaning the tail.


class MatchSuggestionProduct(BaseModel):
    """One product in a borderline match cluster."""

    product_id: int
    site: str
    name: str
    url: str
    price: float | None
    brand: str | None
    pack_size: str | None
    dosage: str | None
    image_url: str | None
    barcode: str | None


class MatchSuggestionOut(BaseModel):
    """One borderline match ready for human review."""

    match_id: int
    canonical_name: str
    confidence: float
    needs_review: bool
    spread_pct: float | None  # price disagreement percentage (max-min)/max
    products: list[MatchSuggestionProduct]


@app.get("/api/v1/dash/matches/suggestions", response_model=list[MatchSuggestionOut])
def dash_match_suggestions(
    confidence_max: float = 0.85,
    only_needs_review: bool = False,
    limit: int = 100,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List borderline matches awaiting human review.

    Filter:
      - is_manual=False (already-confirmed matches are skipped)
      - confidence < confidence_max (default 0.85)
      - optional needs_review=True restricts to flagged ones

    Sorted by confidence ASC (most uncertain first → review priority).
    Pre-fetches latest snapshots в одной SELECT (no N+1).
    """
    matches = db.scalars(
        select(storage.Match)
        .where(
            storage.Match.tenant_id == user.tenant_id,
            storage.Match.is_manual.is_(False),
            storage.Match.confidence < confidence_max,
            *([storage.Match.needs_review.is_(True)] if only_needs_review else []),
        )
        .order_by(storage.Match.confidence.asc())
        .limit(limit)
    ).all()

    all_pids = [p.id for m in matches for p in m.products]
    snaps_by_pid = storage.latest_snapshots_per_product(db, all_pids)

    out: list[MatchSuggestionOut] = []
    for m in matches:
        prods: list[MatchSuggestionProduct] = []
        prices: list[float] = []
        for p in m.products:
            snap = snaps_by_pid.get(p.id)
            price = (snap.discount_price or snap.price) if snap else None
            if price is not None and price > 0:
                prices.append(price)
            prods.append(
                MatchSuggestionProduct(
                    product_id=p.id,
                    site=p.site,
                    name=p.name,
                    url=p.url,
                    price=price,
                    brand=p.brand,
                    pack_size=p.pack_size,
                    dosage=p.dosage,
                    image_url=p.image_url,
                    barcode=p.barcode,
                )
            )
        spread = None
        if len(prices) >= 2:
            spread = round((max(prices) - min(prices)) / max(prices) * 100, 1)
        out.append(
            MatchSuggestionOut(
                match_id=m.id,
                canonical_name=m.canonical_name,
                confidence=m.confidence,
                needs_review=m.needs_review,
                spread_pct=spread,
                products=prods,
            )
        )
    return out


@app.post("/api/v1/dash/matches/{match_id}/confirm", status_code=204)
def dash_match_confirm(
    match_id: int,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Confirm a borderline match — sets is_manual=true so auto-matcher won't
    re-cluster on next nightly run. Inverse of /reject. Used by suggestion UI.
    """
    match = db.scalar(
        select(storage.Match).where(
            storage.Match.id == match_id,
            storage.Match.tenant_id == user.tenant_id,
        )
    )
    if not match:
        raise HTTPException(404, "Match not found")
    if not match.is_manual:
        match.is_manual = True
        # Clear needs_review since user just resolved it.
        match.needs_review = False
        db.commit()
        log.info("match_confirmed", match_id=match_id, user_id=user.id)
    return Response(status_code=204)


@app.post("/api/v1/dash/matches/{match_id}/reject", status_code=204)
def dash_match_reject(
    match_id: int,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manually reject a match: remove canonical_id from products + record rejection pair."""
    from src import match_actions

    match = db.scalar(select(storage.Match).where(storage.Match.id == match_id))
    if not match:
        raise HTTPException(404, "Match not found")
    products = list(match.products)
    if len(products) >= 2:
        # Record all pairs as rejected
        for i, a in enumerate(products):
            for b in products[i + 1 :]:
                match_actions.add_rejection(db, a.id, b.id, reason=f"manual:user={user.id}")
    # Unlink products from this match
    for p in products:
        p.canonical_id = None
    # Delete the match itself
    db.delete(match)
    db.commit()
    return Response(status_code=204)


class MatchRelinkIn(BaseModel):
    site: str
    url: str


def _external_id_from_url(url: str) -> str:
    """Последний сегмент пути URL (без query/fragment) = external_id товара.

    Совпадает с тем, как скрейперы формируют external_id (pharmonline:
    `_external_id_from_href`; aptekonline/aloe: url_id = слаг после /product/).
    """
    path = (url or "").split("?")[0].split("#")[0].rstrip("/")
    return path.split("/")[-1].strip()


@app.post("/api/v1/dash/matches/{match_id}/relink")
def dash_match_relink(
    match_id: int,
    body: MatchRelinkIn,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Вручную переназначить товар сайта в кластере по URL.

    Матчер ошибся → пользователь вставляет ссылку на ПРАВИЛЬНЫЙ товар. Старый
    товар этого сайта отвязывается (+ rejection, чтобы auto-matcher не вернул),
    новый привязывается, match помечается `is_manual` (rematch его не тронет).
    Если сайта в кластере ещё не было — товар просто добавляется.
    Товар должен быть в нашем каталоге (иначе нет данных о цене).
    """
    from src import match_actions

    match = db.scalar(
        select(storage.Match).where(
            storage.Match.id == match_id, storage.Match.tenant_id == user.tenant_id
        )
    )
    if not match:
        raise HTTPException(404, "Match not found")

    site = (body.site or "").strip().lower()
    if site not in ("pharmonline", "aptekonline", "aloe"):
        raise HTTPException(400, f"Неизвестный сайт: {site!r}")

    ext = _external_id_from_url(body.url)
    if not ext:
        raise HTTPException(400, "Не удалось разобрать ссылку")

    prod = db.scalar(
        select(storage.Product).where(
            storage.Product.tenant_id == user.tenant_id,
            storage.Product.site == site,
            storage.Product.external_id == ext,
        )
    )
    if prod is None:  # fallback: по подстроке URL (на случай иной формы external_id)
        prod = db.scalar(
            select(storage.Product).where(
                storage.Product.tenant_id == user.tenant_id,
                storage.Product.site == site,
                storage.Product.url.ilike(f"%{ext}%"),
            )
        )
    if prod is None:
        raise HTTPException(
            404, f"Товар по этой ссылке не найден в каталоге {site} (возможно, не заскрейплен)"
        )
    if prod.canonical_id is not None and prod.canonical_id != match_id:
        raise HTTPException(
            409,
            f"Этот товар уже в другом сравнении (#{prod.canonical_id}) — сначала отклоните его там",
        )

    ok = match_actions.swap_alternative(db, match_id, site, prod.id)
    if not ok:
        raise HTTPException(400, "Не удалось переназначить (возможно, это уже текущий товар сайта)")

    log.info("match_relinked", match_id=match_id, site=site, product_id=prod.id, user_id=user.id)
    return {"ok": True, "product_id": prod.id, "name": prod.name, "site": site}


@app.get("/api/v1/dash/matches/{match_id}/alternatives")
def dash_match_alternatives(
    match_id: int,
    site: str,
    limit: int = 6,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Похожие unmatched-товары сайта `site` — кандидаты на ЗАМЕНУ ошибочного
    члена кластера. Для кнопки «Неправильное сравнение?» в /comparison: оператор
    видит «другие варианты» и выбирает верный (или вставляет свою ссылку →
    relink). Обёртка над match_actions.find_alternatives + актуальная цена.
    """
    _require_site(site)
    limit = max(1, min(limit, 20))
    from src import match_actions

    match = db.scalar(
        select(storage.Match).where(
            storage.Match.id == match_id, storage.Match.tenant_id == user.tenant_id
        )
    )
    if not match:
        raise HTTPException(404, "Match not found")

    alts = [
        (p, sc)
        for (p, sc) in match_actions.find_alternatives(db, match_id, site, limit=limit * 3)
        if p.tenant_id == user.tenant_id
    ][:limit]
    snaps = storage.latest_snapshots_per_product(db, [p.id for p, _ in alts])
    items = []
    for p, sc in alts:
        snap = snaps.get(p.id)
        items.append(
            {
                "product_id": p.id,
                "name": p.name,
                "url": p.url,
                "price": (snap.discount_price or snap.price) if snap else None,
                "score": int(sc),
            }
        )
    return {"items": items}


# ── Phase 4.1+4.3+4.6 (2026-05-27) — Pricing settings + cost CSV import ─────


class PricingConfigOut(BaseModel):
    raise_threshold_pct: float
    undercut_threshold_pct: float
    max_spread_pct: float
    min_margin_pct: float
    max_per_type: int
    updated_at: datetime | None = None


class PricingConfigIn(BaseModel):
    raise_threshold_pct: float = Field(ge=0.0, le=100.0)
    undercut_threshold_pct: float = Field(ge=0.0, le=100.0)
    max_spread_pct: float = Field(ge=0.0, le=100.0)
    min_margin_pct: float = Field(ge=0.0, le=100.0)
    max_per_type: int = Field(ge=1, le=200)


@app.get("/api/v1/dash/settings/pricing", response_model=PricingConfigOut)
def dash_pricing_get(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return current pricing config row для tenant (or default if missing)."""
    cfg = storage.load_pricing_config(db, tenant_id=user.tenant_id)
    db.commit()  # in case default row was just created
    return PricingConfigOut(
        raise_threshold_pct=cfg.raise_threshold_pct,
        undercut_threshold_pct=cfg.undercut_threshold_pct,
        max_spread_pct=cfg.max_spread_pct,
        min_margin_pct=cfg.min_margin_pct,
        max_per_type=cfg.max_per_type,
        updated_at=cfg.updated_at,
    )


@app.put("/api/v1/dash/settings/pricing", response_model=PricingConfigOut)
def dash_pricing_update(
    payload: PricingConfigIn,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update pricing thresholds. Invalidates ROI cache so next request re-computes."""
    cfg = storage.load_pricing_config(db, tenant_id=user.tenant_id)
    cfg.raise_threshold_pct = payload.raise_threshold_pct
    cfg.undercut_threshold_pct = payload.undercut_threshold_pct
    cfg.max_spread_pct = payload.max_spread_pct
    cfg.min_margin_pct = payload.min_margin_pct
    cfg.max_per_type = payload.max_per_type
    cfg.updated_at = utcnow()
    # Invalidate ROI cache so next /roi/actions re-computes with new thresholds.
    db.query(storage.RoiActionsCache).filter(
        storage.RoiActionsCache.tenant_id == user.tenant_id
    ).delete()
    db.commit()
    log.info("pricing_config_updated", tenant_id=user.tenant_id, user_id=user.id)
    return PricingConfigOut(
        raise_threshold_pct=cfg.raise_threshold_pct,
        undercut_threshold_pct=cfg.undercut_threshold_pct,
        max_spread_pct=cfg.max_spread_pct,
        min_margin_pct=cfg.min_margin_pct,
        max_per_type=cfg.max_per_type,
        updated_at=cfg.updated_at,
    )


class CostImportResult(BaseModel):
    rows_processed: int
    rows_imported: int
    rows_skipped: int
    errors: list[str] = Field(default_factory=list)


@app.post("/api/v1/dash/settings/costs/import", response_model=CostImportResult)
async def dash_cost_csv_import(
    file: UploadFile = File(...),
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Import purchase prices from CSV.

    Expected columns (header required):
      sku, supplier_name, purchase_price, currency, name

    `sku` matches Product.external_id. `supplier_name` is free-form (used as
    SupplierPrice.supplier). `purchase_price` is float in AZN. `currency`
    defaults to "AZN" if missing. `name` is optional (logged for human review).

    Idempotent: existing (product_id, supplier) pair updated, others inserted.
    """
    import csv
    import io

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")  # strip BOM
    except UnicodeDecodeError:
        raise HTTPException(400, "File is not UTF-8 encoded")
    reader = csv.DictReader(io.StringIO(text))

    required_cols = {"sku", "supplier_name", "purchase_price"}
    actual_cols = set(reader.fieldnames or [])
    missing = required_cols - actual_cols
    if missing:
        raise HTTPException(
            400,
            f"Missing required columns: {sorted(missing)}. Got: {sorted(actual_cols)}",
        )

    rows_processed = 0
    rows_imported = 0
    rows_skipped = 0
    errors: list[str] = []

    # Build sku → Product map for tenant (one query, not N+1).
    products_by_sku: dict[str, storage.Product] = {
        p.external_id: p
        for p in db.scalars(
            select(storage.Product).where(storage.Product.tenant_id == user.tenant_id)
        ).all()
    }

    for line_num, row in enumerate(reader, start=2):  # line 1 = header
        rows_processed += 1
        sku = (row.get("sku") or "").strip()
        if not sku:
            errors.append(f"line {line_num}: empty sku")
            rows_skipped += 1
            continue
        product = products_by_sku.get(sku)
        if product is None:
            errors.append(f"line {line_num}: sku={sku!r} not found in products")
            rows_skipped += 1
            continue
        try:
            price = float((row.get("purchase_price") or "").strip())
        except (ValueError, TypeError):
            errors.append(f"line {line_num}: invalid purchase_price {row.get('purchase_price')!r}")
            rows_skipped += 1
            continue
        if price <= 0:
            errors.append(f"line {line_num}: purchase_price must be > 0")
            rows_skipped += 1
            continue
        supplier = (row.get("supplier_name") or "default").strip() or "default"
        currency = (row.get("currency") or "AZN").strip() or "AZN"
        # Upsert (product_id, supplier_name) row.
        existing = db.scalar(
            select(storage.SupplierPrice).where(
                storage.SupplierPrice.product_id == product.id,
                storage.SupplierPrice.supplier_name == supplier,
            )
        )
        if existing:
            existing.purchase_price = price
            existing.currency = currency
            existing.source = "dashboard_csv"
            existing.updated_at = utcnow()
        else:
            db.add(
                storage.SupplierPrice(
                    product_id=product.id,
                    sku=sku,
                    supplier_name=supplier,
                    purchase_price=price,
                    currency=currency,
                    source="dashboard_csv",
                    updated_at=utcnow(),
                )
            )
        rows_imported += 1

    db.commit()
    # Drop ROI cache so margin-aware logic picks up new costs immediately.
    db.query(storage.RoiActionsCache).filter(
        storage.RoiActionsCache.tenant_id == user.tenant_id
    ).delete()
    db.commit()
    log.info(
        "cost_csv_imported",
        tenant_id=user.tenant_id,
        user_id=user.id,
        rows_imported=rows_imported,
        rows_skipped=rows_skipped,
    )
    # Cap errors list to first 20 for response size sanity.
    return CostImportResult(
        rows_processed=rows_processed,
        rows_imported=rows_imported,
        rows_skipped=rows_skipped,
        errors=errors[:20],
    )


# ─── Manual aloe-matcher endpoints ───────────────────────────────────────────
# Дополняет AI-нормализатор: human-in-the-loop для needs_review хвоста и для
# продуктов где AI не сработал. Поток: пользователь выбирает категорию, видит
# pharmonline/aptekonline продукты без aloe в кластере → ищет aloe-аналог →
# одним кликом привязывает к существующему Match.


@app.get("/api/v1/dash/unmatched-pairs")
def dash_unmatched_pairs(
    site: str,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Match-кластеры, в которых отсутствует продукт указанного `site`.

    Используется UI /aloe-matcher: показать ph/apt продукты, которым ручник
    может найти aloe-аналог. Фильтр `category` — по anchor-продукту в кластере.
    """
    _require_site(site)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    # Подзапрос: match_id'ы где УЖЕ есть продукт с этим site
    has_site_subq = (
        select(storage.Product.canonical_id)
        .where(
            storage.Product.site == site,
            storage.Product.canonical_id.is_not(None),
            storage.Product.tenant_id == user.tenant_id,
            storage.Product.url_dead_at.is_(None),
        )
        .distinct()
        .subquery()
    )

    live_anchor_exists = (
        select(storage.Product.id)
        .where(
            storage.Product.canonical_id == storage.Match.id,
            storage.Product.tenant_id == user.tenant_id,
            storage.Product.url_dead_at.is_(None),
        )
        .exists()
    )

    # Базовый набор matches: tenant + НЕТ live target-site product + есть live anchor.
    conditions = [
        storage.Match.tenant_id == user.tenant_id,
        storage.Match.id.not_in(select(has_site_subq.c.canonical_id)),
        live_anchor_exists,
    ]

    if category:
        category_anchor_exists = (
            select(storage.Product.id)
            .where(
                storage.Product.canonical_id == storage.Match.id,
                storage.Product.tenant_id == user.tenant_id,
                storage.Product.url_dead_at.is_(None),
                storage.Product.category == category,
            )
            .exists()
        )
        conditions.append(category_anchor_exists)

    total = db.scalar(select(func.count(storage.Match.id)).where(*conditions)) or 0
    page = list(
        db.scalars(
            select(storage.Match)
            .options(selectinload(storage.Match.products))
            .where(*conditions)
            .order_by(storage.Match.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )
    page_product_ids = [
        p.id
        for m in page
        for p in m.products
        if p.tenant_id == user.tenant_id and p.url_dead_at is None
    ]
    snaps_by_pid = storage.latest_snapshots_per_product(db, page_product_ids)

    items = []
    for m in page:
        anchors = []
        for p in m.products:
            if p.tenant_id != user.tenant_id or p.url_dead_at is not None:
                continue
            snap = snaps_by_pid.get(p.id)
            price = (snap.discount_price or snap.price) if snap else None
            anchors.append(
                {
                    "product_id": p.id,
                    "site": p.site,
                    "name": p.name,
                    "brand": p.brand,
                    "category": p.category,
                    "url": p.url,
                    "price": price,
                }
            )
        items.append(
            {
                "match_id": m.id,
                "canonical_name": m.canonical_name,
                "canonical_brand": m.canonical_brand,
                "canonical_dosage": m.canonical_dosage,
                "canonical_pack_size": m.canonical_pack_size,
                "anchor_products": anchors,
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/dash/matches/{match_id}/candidate-analogs")
def dash_match_candidate_analogs(
    match_id: int,
    site: str,
    limit: int = 6,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ранжированные guard-passing аналоги для кластера на недостающем `site`.

    Заменяет ручной текст-поиск в /matcher: берём имена членов кластера, блокируем
    по первому значимому токену, ищем unmatched-продукты `site` с тем же токеном,
    ранжируем по token_set_ratio и ОТФИЛЬТРОВЫВАЕМ всё, что конфликтует с любым
    членом (_hard_conflict + _pairwise_spec_conflict) или уже отклонено
    (match_rejections). Флаг ``auto_safe`` — кандидат ultra-эквивалентен якорю
    (равные токены/доза/объём/форма/коды) → one-click без раздумий. Accept на
    фронте зовёт add-product (is_manual=True). Дешёво: один токен-блок, не скан.
    """
    _require_site(site)
    limit = max(1, min(limit, 20))
    from rapidfuzz import fuzz

    from src import match_actions, matcher

    match = db.scalar(
        select(storage.Match).where(
            storage.Match.id == match_id,
            storage.Match.tenant_id == user.tenant_id,
        )
    )
    if not match:
        raise HTTPException(404, "Match not found")
    members = list(match.products)
    live_members = [p for p in members if p.url_dead_at is None]
    if not live_members or any(p.site == site for p in live_members):
        return {"items": []}  # nothing to add (empty cluster or site already present)

    # first significant token of each member → coarse block
    block_tokens: set[str] = set()
    for m in live_members:
        nn = m.name_normalized or ""
        toks = matcher._significant_name_tokens(nn)
        first = next((t for t in nn.split() if t in toks), None)
        if first:
            block_tokens.add(first)
    if not block_tokens:
        return {"items": []}

    conds = [storage.Product.name_normalized.ilike(f"%{t}%") for t in block_tokens]
    cand_rows = db.scalars(
        select(storage.Product)
        .where(
            storage.Product.site == site,
            storage.Product.tenant_id == user.tenant_id,
            storage.Product.canonical_id.is_(None),
            storage.Product.url_dead_at.is_(None),
            or_(*conds),
        )
        .limit(500)
    ).all()

    scored: list[tuple[float, bool, storage.Product]] = []
    for c in cand_rows:
        if not c.name_normalized:
            continue
        if any(
            matcher._hard_conflict(c, m)
            or matcher._pairwise_spec_conflict(c, m)
            or match_actions.is_rejected(db, c.id, m.id)
            for m in live_members
        ):
            continue
        score = max(
            fuzz.token_set_ratio(c.name_normalized, m.name_normalized or "") for m in live_members
        )
        if score < 80:
            continue
        auto_safe = any(matcher.ultra_equal(c, m) for m in live_members)
        scored.append((score, auto_safe, c))

    scored.sort(key=lambda r: (r[1], r[0]), reverse=True)
    top = scored[:limit]
    snaps = storage.latest_snapshots_per_product(db, [c.id for _s, _a, c in top])
    items = []
    for score, auto_safe, c in top:
        snap = snaps.get(c.id)
        items.append(
            {
                "product_id": c.id,
                "site": c.site,
                "name": c.name,
                "brand": c.brand,
                "url": c.url,
                "image_url": c.image_url,
                "price": (snap.discount_price or snap.price) if snap else None,
                "score": round(float(score), 1),
                "auto_safe": auto_safe,
            }
        )
    return {"items": items}


@app.get("/api/v1/dash/matcher/counts")
def dash_matcher_counts(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Кол-во unmatched кластеров для каждого из 3 сайтов.

    Кластер считается «unmatched для сайта S», если в нём нет продукта с S.
    Используется UI /matcher для бейджей-счётчиков в site selector — оператор
    сразу видит, по какому сайту больше работы.

    Возвращает: ``{"aloe": N, "pharmonline": M, "aptekonline": K}``.
    """
    out: dict[str, int] = {}
    # Подмножество match_id'ов, у которых вообще есть хотя бы 1 продукт (защита
    # от пустых orphaned Match'ей, которых не должно быть, но bы).
    has_any_product_subq = (
        select(storage.Product.canonical_id)
        .where(
            storage.Product.canonical_id.is_not(None),
            storage.Product.tenant_id == user.tenant_id,
            storage.Product.url_dead_at.is_(None),
        )
        .distinct()
        .subquery()
    )
    for site in _VALID_SITES:
        has_site_subq = (
            select(storage.Product.canonical_id)
            .where(
                storage.Product.site == site,
                storage.Product.canonical_id.is_not(None),
                storage.Product.tenant_id == user.tenant_id,
                storage.Product.url_dead_at.is_(None),
            )
            .distinct()
            .subquery()
        )
        count = db.scalar(
            select(func.count(storage.Match.id)).where(
                storage.Match.tenant_id == user.tenant_id,
                storage.Match.id.in_(select(has_any_product_subq.c.canonical_id)),
                storage.Match.id.not_in(select(has_site_subq.c.canonical_id)),
            )
        )
        out[site] = int(count or 0)
    return out


class _AddProductPayload(BaseModel):
    product_id: int = Field(gt=0)


@app.post("/api/v1/dash/matches/{match_id}/add-product")
def dash_match_add_product(
    match_id: int,
    payload: _AddProductPayload,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Привязать продукт к существующему Match.

    Валидация:
    - Match существует и принадлежит tenant'у
    - Product существует, tenant'у, не в этом кластере уже
    - В кластере ещё нет продукта с тем же site (one product per site per match)
    - Если product был в другом match'е — переезжает (старый match теряет связь)
    Метит Match.is_manual=True (ручной выбор → защищён от auto-перематчивания).
    """
    match = db.scalar(
        select(storage.Match).where(
            storage.Match.id == match_id,
            storage.Match.tenant_id == user.tenant_id,
        )
    )
    if not match:
        raise HTTPException(404, "Match not found")

    product = db.scalar(
        select(storage.Product).where(
            storage.Product.id == payload.product_id,
            storage.Product.tenant_id == user.tenant_id,
        )
    )
    if not product:
        raise HTTPException(404, "Product not found")
    if product.url_dead_at is not None:
        raise HTTPException(400, "Product is marked dead")

    if product.canonical_id == match.id:
        raise HTTPException(400, "Product already in this match")

    existing_sites = {p.site for p in match.products if p.url_dead_at is None}
    if product.site in existing_sites:
        raise HTTPException(409, f"Match already has a product from {product.site}")

    product.canonical_id = match.id
    match.is_manual = True
    match.match_strategy = "manual"
    if match.confidence is None or match.confidence < 1.0:
        match.confidence = 1.0
    db.commit()

    log.info(
        "match_product_added",
        match_id=match.id,
        product_id=product.id,
        user_id=user.id,
        site=product.site,
    )

    db.refresh(match)
    return {
        "match_id": match.id,
        "canonical_name": match.canonical_name,
        "is_manual": match.is_manual,
        "match_strategy": match.match_strategy,
        "products": [
            {"product_id": p.id, "site": p.site, "name": p.name, "url": p.url}
            for p in match.products
        ],
    }


class _CreateMatchPayload(BaseModel):
    product_ids: list[int] = Field(min_length=2, max_length=10)


@app.post("/api/v1/dash/matches/create-with-products")
def dash_match_create_with_products(
    payload: _CreateMatchPayload,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Создать новый Match с заданными продуктами (manual cluster).

    Полезно когда оба anchor-продукта были unmatched (нет существующего
    кластера). Берёт canonical_name/brand/... из первого продукта. Метит
    is_manual=True. Возвращает созданный Match.
    """
    products = list(
        db.scalars(
            select(storage.Product).where(
                storage.Product.id.in_(payload.product_ids),
                storage.Product.tenant_id == user.tenant_id,
            )
        ).all()
    )
    if len(products) != len(payload.product_ids):
        raise HTTPException(404, "One or more products not found")

    sites = [p.site for p in products]
    if len(set(sites)) != len(sites):
        raise HTTPException(409, "Products must come from distinct sites")

    first = products[0]
    match = storage.Match(
        tenant_id=user.tenant_id,
        canonical_name=first.name,
        canonical_brand=first.brand,
        canonical_dosage=first.dosage,
        canonical_pack_size=first.pack_size,
        confidence=1.0,
        is_manual=True,
        match_strategy="manual",
    )
    db.add(match)
    db.flush()
    for p in products:
        p.canonical_id = match.id
    db.commit()
    db.refresh(match)

    log.info(
        "match_created_manual",
        match_id=match.id,
        product_ids=[p.id for p in products],
        user_id=user.id,
    )
    return {
        "match_id": match.id,
        "canonical_name": match.canonical_name,
        "is_manual": True,
        "match_strategy": "manual",
        "products": [
            {"product_id": p.id, "site": p.site, "name": p.name, "url": p.url}
            for p in match.products
        ],
    }


@app.get("/api/v1/dash/watchlist")
def dash_watchlist_list(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    items = db.scalars(
        select(storage.TrackedProduct)
        .where(storage.TrackedProduct.tenant_id == user.tenant_id)
        .order_by(desc(storage.TrackedProduct.created_at))
    ).all()
    return [
        {
            "id": t.id,
            "canonical_name": t.canonical_name,
            "brand": t.brand,
            "dosage": t.dosage,
            "pack_size": t.pack_size,
            "search_query": t.search_query,
            "notes": t.notes,
            "is_active": t.is_active,
            "links": [
                {
                    "site": link.site,
                    "url": link.url,
                    "external_id": link.external_id,
                    "status": link.status,
                }
                for link in t.links
            ]
            if hasattr(t, "links")
            else [],
        }
        for t in items
    ]


@app.post("/api/v1/dash/watchlist", status_code=201)
def dash_watchlist_create(
    payload: WatchlistItemIn,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    tp = storage.TrackedProduct(
        canonical_name=payload.canonical_name,
        brand=payload.brand,
        dosage=payload.dosage,
        pack_size=payload.pack_size,
        search_query=payload.search_query,
        notes=payload.notes,
        is_active=True,
        tenant_id=user.tenant_id,
    )
    db.add(tp)
    db.flush()
    for site, url in [
        ("pharmonline", payload.pharmonline_url),
        ("aptekonline", payload.aptekonline_url),
        ("aloe", payload.aloe_url),
    ]:
        if url:
            db.add(
                storage.TrackedProductLink(
                    tracked_product_id=tp.id,
                    site=site,
                    url=url,
                    status="pending",
                )
            )
    db.commit()
    return {"id": tp.id}


def _watchlist_category_payload(
    tc: storage.TrackedCategory,
    db: Session,
    *,
    category_comparison_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    c = tc.category
    site_category_filters = [
        (storage.Product.site == site) & (storage.Product.category == slug)
        for site, slug in (
            ("pharmonline", c.pharmonline_slug),
            ("aptekonline", c.aptekonline_slug),
            ("aloe", c.aloe_slug),
        )
        if slug
    ]
    product_count = 0
    matched_product_count = 0
    comparison_count = 0
    missing_site_counts: dict[str, int] = {}
    if site_category_filters:
        product_count = db.scalar(
            select(func.count(storage.Product.id)).where(
                storage.Product.tenant_id == tc.tenant_id,
                or_(*site_category_filters),
                storage.Product.url_dead_at.is_(None),
            )
        ) or 0
        matched_product_count = db.scalar(
            select(func.count(storage.Product.id)).where(
                storage.Product.tenant_id == tc.tenant_id,
                or_(*site_category_filters),
                storage.Product.canonical_id.is_not(None),
                storage.Product.url_dead_at.is_(None),
            )
        ) or 0
    if c.pharmonline_slug and category_comparison_counts is not None:
        comparison_count = category_comparison_counts.get(c.pharmonline_slug, 0)
    elif c.pharmonline_slug:
        other = aliased(storage.Product)
        comparison_count = db.scalar(
            select(func.count(func.distinct(storage.Product.canonical_id))).where(
                storage.Product.tenant_id == tc.tenant_id,
                storage.Product.site == "pharmonline",
                storage.Product.category == c.pharmonline_slug,
                storage.Product.canonical_id.is_not(None),
                storage.Product.url_dead_at.is_(None),
                select(other.id)
                .where(
                    other.tenant_id == tc.tenant_id,
                    other.canonical_id == storage.Product.canonical_id,
                    other.site != "pharmonline",
                    other.url_dead_at.is_(None),
                )
                .exists(),
            )
        ) or 0
    if c.pharmonline_slug:
        for site, slug in (("aloe", c.aloe_slug), ("aptekonline", c.aptekonline_slug)):
            if not slug:
                continue
            target = aliased(storage.Product)
            missing_site_counts[site] = db.scalar(
                select(func.count(func.distinct(storage.Product.canonical_id))).where(
                    storage.Product.tenant_id == tc.tenant_id,
                    storage.Product.site == "pharmonline",
                    storage.Product.category == c.pharmonline_slug,
                    storage.Product.canonical_id.is_not(None),
                    storage.Product.url_dead_at.is_(None),
                    ~select(target.id)
                    .where(
                        target.tenant_id == tc.tenant_id,
                        target.canonical_id == storage.Product.canonical_id,
                        target.site == site,
                        target.url_dead_at.is_(None),
                    )
                    .exists(),
                )
            ) or 0
    return {
        "id": tc.id,
        "category_id": c.id,
        "key": c.key,
        "label_ru": c.label_ru,
        "label_az": c.label_az,
        "pharmonline_slug": c.pharmonline_slug,
        "aptekonline_slug": c.aptekonline_slug,
        "aloe_slug": c.aloe_slug,
        "notes": tc.notes,
        "is_active": tc.is_active,
        "product_count": product_count,
        "matched_product_count": matched_product_count,
        "comparison_count": comparison_count,
        "missing_site_counts": missing_site_counts,
    }


@app.get("/api/v1/dash/watchlist/categories")
def dash_watchlist_categories_list(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    items = db.scalars(
        select(storage.TrackedCategory)
        .options(selectinload(storage.TrackedCategory.category))
        .join(storage.TrackedCategory.category)
        .where(storage.TrackedCategory.tenant_id == user.tenant_id)
        .order_by(storage.Category.label_ru)
    ).all()
    if not items:
        return []
    comparison_counts = {}
    pharmonline_slugs = {
        item.category.pharmonline_slug for item in items if item.category.pharmonline_slug
    }
    if pharmonline_slugs:
        comparison_counts = {
            row.category: row.matched_skus
            for row in analytics.category_comparison(
                db,
                client_site="pharmonline",
                tenant_id=user.tenant_id,
                categories=pharmonline_slugs,
            )
        }
    return [
        _watchlist_category_payload(
            item,
            db,
            category_comparison_counts=comparison_counts,
        )
        for item in items
    ]


@app.post("/api/v1/dash/watchlist/categories", status_code=201)
def dash_watchlist_categories_create(
    payload: WatchlistCategoryIn,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    category = db.scalar(select(storage.Category).where(storage.Category.id == payload.category_id))
    if not category:
        raise HTTPException(404, "Category not found")
    existing = db.scalar(
        select(storage.TrackedCategory).where(
            storage.TrackedCategory.tenant_id == user.tenant_id,
            storage.TrackedCategory.category_id == payload.category_id,
        )
    )
    if existing:
        existing.notes = payload.notes if payload.notes is not None else existing.notes
        existing.is_active = True
        db.commit()
        db.refresh(existing)
        return {"id": existing.id}
    item = storage.TrackedCategory(
        tenant_id=user.tenant_id,
        category_id=payload.category_id,
        notes=payload.notes.strip() if payload.notes else None,
        is_active=True,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id}


@app.delete("/api/v1/dash/watchlist/categories/{tracked_category_id}", status_code=204)
def dash_watchlist_categories_delete(
    tracked_category_id: int,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    item = db.scalar(
        select(storage.TrackedCategory).where(
            storage.TrackedCategory.id == tracked_category_id,
            storage.TrackedCategory.tenant_id == user.tenant_id,
        )
    )
    if not item:
        raise HTTPException(404, "Watchlist category not found")
    db.delete(item)
    db.commit()
    return Response(status_code=204)


@app.delete("/api/v1/dash/watchlist/{tp_id}", status_code=204)
def dash_watchlist_delete(
    tp_id: int,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    tp = db.scalar(
        select(storage.TrackedProduct).where(
            storage.TrackedProduct.id == tp_id,
            storage.TrackedProduct.tenant_id == user.tenant_id,
        )
    )
    if not tp:
        raise HTTPException(404, "Watchlist item not found")
    db.delete(tp)
    db.commit()
    return Response(status_code=204)


# ─── Legacy ERP endpoints (X-API-Key auth) ───────────────────────────────────


@app.get("/api/v1/alerts/recent", dependencies=[Depends(require_api_key)])
def alerts_recent(limit: int = 50, db: Session = Depends(get_db)):
    events = db.scalars(
        select(storage.AlertEvent).order_by(desc(storage.AlertEvent.created_at)).limit(limit)
    ).all()
    return [
        {
            "id": e.id,
            "severity": e.severity,
            "title": e.title,
            "detail": e.detail,
            "payload": e.payload,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


@app.get("/api/v1/products", dependencies=[Depends(require_api_key)])
def products_list(
    site: str | None = None,
    limit: int = 500,
    db: Session = Depends(get_db),
):
    stmt = select(storage.Product).limit(limit)
    if site:
        stmt = stmt.where(storage.Product.site == site)
    products = db.scalars(stmt).all()
    return [
        {
            "id": p.id,
            "site": p.site,
            "external_id": p.external_id,
            "url": p.url,
            "name": p.name,
            "brand": p.brand,
            "category": p.category,
            "canonical_id": p.canonical_id,
        }
        for p in products
    ]


@app.get("/api/v1/comparisons", dependencies=[Depends(require_api_key)])
def comparisons(db: Session = Depends(get_db)):
    last_run = db.scalar(
        select(storage.Run.id)
        .where(storage.Run.status == "ok")
        .order_by(desc(storage.Run.id))
        .limit(1)
    )
    if not last_run:
        return []
    matches = db.scalars(select(storage.Match)).all()
    # Diff-only-aware: латест на product_id, не последний run (см. /dash/comparison).
    all_pids = [p.id for m in matches for p in m.products]
    snaps_by_pid = storage.latest_snapshots_per_product(db, all_pids)
    out = []
    for m in matches:
        prices: dict[str, Any] = {}
        for p in m.products:
            snap = snaps_by_pid.get(p.id)
            if snap:
                prices[p.site] = {
                    "price": snap.discount_price or snap.price,
                    "is_on_sale": snap.is_on_sale,
                    "url": p.url,
                }
        out.append(
            {
                "canonical_id": m.id,
                "name": m.canonical_name,
                "brand": m.canonical_brand,
                "is_manual": m.is_manual,
                "prices": prices,
            }
        )
    return out


@app.get("/api/v1/margin", dependencies=[Depends(require_api_key)])
def margin_endpoint(db: Session = Depends(get_db)):
    rows = inv_mod.margin_report(db)
    return [
        {
            "product_id": r.product_id,
            "name": r.name,
            "sale_price": r.sale_price,
            "purchase_price": r.purchase_price,
            "margin_azn": r.margin_azn,
            "margin_pct": r.margin_pct,
            "in_stock": r.in_stock,
        }
        for r in rows
    ]


@app.post("/api/v1/inventory/stock", dependencies=[Depends(require_api_key)])
def push_stock(
    items: list[StockItemIn],
    source: str = "api_erp",
    db: Session = Depends(get_db),
):
    from sqlalchemy import delete as _del

    db.execute(_del(storage.StockLevel).where(storage.StockLevel.source == source))
    matched = 0
    for item in items:
        product = inv_mod._find_product_by_sku_or_name(db, item.sku, item.name)
        if product:
            matched += 1
        db.add(
            storage.StockLevel(
                product_id=product.id if product else None,
                canonical_id=product.canonical_id if product else None,
                sku=item.sku,
                name=item.name or (product.name if product else None),
                qty=item.qty,
                is_in_stock=item.qty > 0,
                source=source,
                updated_at=utcnow(),
            )
        )
    db.commit()
    return {"received": len(items), "matched_to_product": matched}


@app.post("/api/v1/inventory/prices", dependencies=[Depends(require_api_key)])
def push_prices(
    items: list[PurchasePriceIn],
    source: str = "api_erp",
    db: Session = Depends(get_db),
):
    from sqlalchemy import delete as _del

    db.execute(_del(storage.SupplierPrice).where(storage.SupplierPrice.source == source))
    matched = 0
    for item in items:
        if item.purchase_price <= 0:
            continue
        product = inv_mod._find_product_by_sku_or_name(db, item.sku, item.name)
        if product:
            matched += 1
        db.add(
            storage.SupplierPrice(
                product_id=product.id if product else None,
                canonical_id=product.canonical_id if product else None,
                sku=item.sku,
                name=item.name or (product.name if product else None),
                supplier_name=item.supplier_name,
                purchase_price=item.purchase_price,
                currency=item.currency,
                source=source,
                updated_at=utcnow(),
            )
        )
    db.commit()
    return {"received": len(items), "matched_to_product": matched}
