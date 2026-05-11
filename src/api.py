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
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import structlog
from fastapi import Cookie, Depends, FastAPI, HTTPException, Header, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from src import inventory as inv_mod
from src import storage, tenants
from src._time import utcnow

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


# ─── Rate limiting (in-memory sliding window — to be replaced with Redis) ────

_RATE_LIMIT_RPM = int(os.environ.get("PHARMACY_API_RATE_LIMIT_RPM", "100"))
_RATE_BUCKETS: dict[str, deque[float]] = defaultdict(deque)


def _check_rate_limit(client_key: str, limit: int = _RATE_LIMIT_RPM) -> None:
    """Sliding window: не более `limit` запросов в минуту от одного клиента."""
    now = time.time()
    bucket = _RATE_BUCKETS[client_key]
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= limit:
        retry_after = int(60 - (now - bucket[0]))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({limit} req/min)",
            headers={"Retry-After": str(max(1, retry_after))},
        )
    bucket.append(now)


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
    # Rate limit per user
    _check_rate_limit(f"user:{user_id}")
    # Stash tenant_id on request for endpoint use
    request.state.tenant_id = user.tenant_id
    request.state.user_id = user.id
    # Tag Sentry events with tenant_id for filtering
    sentry_set_tenant(user.tenant_id)
    return user


# ─── Schemas ─────────────────────────────────────────────────────────────────


class HealthOut(BaseModel):
    status: str
    last_run_at: datetime | None
    last_run_status: str | None


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
    """Simple password-based login (single shared admin password).

    Validates the password against `ADMIN_PASSWORD_HASH` env var (bcrypt).
    Issues a JWT cookie tied to the first active admin user in the DB.
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


class CategoryIn(BaseModel):
    key: str
    label_ru: str
    label_az: str | None = None
    pharmonline_slug: str | None = None
    aptekonline_slug: str | None = None
    aloe_slug: str | None = None
    is_active: bool = True


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


# ─── Public endpoints ────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthOut)
def health_endpoint(db: Session = Depends(get_db)):
    last = db.scalars(
        select(storage.Run).order_by(desc(storage.Run.id)).limit(1)
    ).first()
    return HealthOut(
        status="up",
        last_run_at=last.started_at if last else None,
        last_run_status=last.status if last else None,
    )


# ─── Auth endpoints (frontend) ───────────────────────────────────────────────


@app.post("/auth/request", response_model=AuthRequestOut)
def auth_request(payload: AuthRequestIn, request: Request, db: Session = Depends(get_db)):
    """Request a magic-link by email. Always returns success to avoid email enumeration."""
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"auth_req:{client_ip}", limit=5)

    token = tenants.issue_magic_token(db, str(payload.email))
    if token:
        public_url = os.environ.get("PHARMACY_PUBLIC_URL", "http://localhost:3000")
        link = f"{public_url}/auth/verify?token={token}"
        try:
            from src import notifier

            notifier.send_email(
                subject="Pharmacy Monitor — magic link",
                html_body=(
                    f"<p>Click the link below to sign in (expires in 30 minutes):</p>"
                    f"<p><a href='{link}'>{link}</a></p>"
                    f"<p>If you didn't request this, ignore this email.</p>"
                ),
                to=[str(payload.email)],
            )
        except Exception as e:
            log.warning("magic_link_email_failed", error=str(e))
    # Always return success (don't leak whether email exists)
    return AuthRequestOut(sent=True, detail="If the email is registered, a magic-link was sent.")


@app.get("/auth/verify")
def auth_verify(token: str, response: Response, db: Session = Depends(get_db)):
    """Verify magic token, set JWT cookie."""
    user = tenants.verify_magic_token(db, token)
    if not user:
        raise HTTPException(401, "Invalid or expired token")
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


@app.post("/auth/login")
def auth_login(payload: PasswordLoginIn, response: Response, request: Request, db: Session = Depends(get_db)):
    """Password login. DB-first (TenantUser.password_hash), env fallback.

    Раньше (до 2026-05-11) сравнивал только с ADMIN_LOGIN + ADMIN_PASSWORD_HASH
    в env. Теперь:
    1. Находим пользователя по login (= local-part email или env ADMIN_LOGIN).
    2. Если у user.password_hash есть значение — verify против DB.
    3. Иначе fallback на env ADMIN_PASSWORD_HASH (bootstrap mode пока клиент
       не сменил пароль через UI).
    """
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"login:{client_ip}", limit=10)

    expected_login = os.environ.get("ADMIN_LOGIN", "admin")
    env_pw_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")

    # Constant-time login compare против env (для backward-compat — login
    # фиксированный в env, в DB user identifier'ом служит email)
    import hmac
    if not hmac.compare_digest(payload.login.lower(), expected_login.lower()):
        _check_rate_limit(f"login_fail:{client_ip}", limit=5)
        raise HTTPException(401, "Неверный логин или пароль")

    # Find first active admin
    user = db.scalar(
        select(storage.TenantUser)
        .where(storage.TenantUser.is_active.is_(True))
        .order_by(storage.TenantUser.id)
        .limit(1)
    )
    if not user:
        from src import tenants as _tenants
        t = _tenants.get_or_create_default(db)
        user = _tenants.add_user(db, t.id, "admin@local", name="Admin", role="admin")
        db.commit()

    # Verify password: DB-first if set, fallback to env
    pw_to_check = user.password_hash or env_pw_hash
    if not pw_to_check:
        raise HTTPException(503, "Login not configured (no DB hash and no ADMIN_PASSWORD_HASH env)")
    if not _verify_bcrypt(payload.password, pw_to_check):
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


@app.post("/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/v1/dash/me", response_model=MeOut)
def dash_me(user: storage.TenantUser = Depends(require_user)):
    return MeOut(
        id=user.id, email=user.email, name=user.name,
        role=user.role, tenant_id=user.tenant_id,
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
    sites: list[str] | None = None  # ["pharmonline", "aptekonline"] etc


@app.post("/api/v1/dash/scrape/trigger", status_code=202)
def dash_scrape_trigger(
    payload: ScrapeTriggerIn,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Поставить scrape-запрос в очередь. Mac launchd-watcher подберёт в течение ~60 секунд.

    Использует таблицу `scrape_requests`. Возвращает 202 + id запроса —
    UI polls статус до status='ok'/'failed'.
    """
    if user.role not in ("admin", "owner"):
        raise HTTPException(403, "Admin role required")
    if payload.mode not in ("all", "category"):
        raise HTTPException(400, "mode must be 'all' or 'category'")
    if payload.mode == "category" and not payload.category_id:
        raise HTTPException(400, "category_id required for mode='category'")

    # Анти-spam: не более одной pending заявки на tenant одновременно
    existing_pending = db.scalar(
        select(storage.ScrapeRequest)
        .where(
            storage.ScrapeRequest.tenant_id == user.tenant_id,
            storage.ScrapeRequest.status.in_(("pending", "running")),
        )
        .limit(1)
    )
    if existing_pending:
        raise HTTPException(
            409,
            f"Уже в очереди запрос #{existing_pending.id} (status={existing_pending.status}). "
            "Дождись его завершения.",
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

    Для request'ов со status='ok' (то есть scrape завершился успешно и есть run_id)
    подмешиваем агрегаты из Run: products_scraped (total) + products_per_site
    ({site: count}). UI показывает «Готово ✓ — N товаров (pharm: X, apt: Y)».
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
            select(storage.Run).where(storage.Run.id.in_(run_ids))
        ).all()
        runs_map = {run.id: run for run in rows}

    out = []
    for r in reqs:
        run = runs_map.get(r.run_id) if r.run_id else None
        out.append({
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
        })
    return out


# ─── Internal endpoints для Mac launchd-watcher ─────────────────────────────


@app.get("/api/v1/internal/pending-scrape", dependencies=[Depends(require_api_key)])
def internal_pending_scrape(db: Session = Depends(get_db)):
    """Возвращает старейший pending запрос (для Mac watcher'а).

    Auth via X-API-Key header (require_api_key, same as legacy ERP endpoints).
    """
    req = db.scalar(
        select(storage.ScrapeRequest)
        .where(storage.ScrapeRequest.status == "pending")
        .order_by(storage.ScrapeRequest.id)
        .limit(1)
    )
    if not req:
        return {"pending": None}
    # Mark as running immediately, чтобы не подобрать дважды
    req.status = "running"
    req.started_at = datetime.utcnow()
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


@app.post("/api/v1/internal/scrape-complete/{request_id}",
          dependencies=[Depends(require_api_key)])
def internal_scrape_complete(
    request_id: int,
    payload: ScrapeCompleteIn,
    db: Session = Depends(get_db),
):
    """Mac watcher вызывает после завершения. status → 'ok' или 'failed'.

    Идемпотентность: если запрос уже помечен 'ok' (через `pharmacy-monitor run
    --request-id` сразу после persist phase) — НЕ откатываем обратно в 'failed',
    даже если pharmacy-monitor позже упал в matcher/analyzer. UI уже показал
    клиенту «Готово — N товаров», менять статус задним числом некорректно.
    """
    req = db.scalar(select(storage.ScrapeRequest).where(storage.ScrapeRequest.id == request_id))
    if not req:
        raise HTTPException(404, "Request not found")
    if req.status == "ok" and payload.error_message:
        # Уже завершено успешно (early-complete от pharmacy-monitor); ошибка в
        # post-persist фазе (matcher/analyzer) логируется в error_message но не
        # меняет статус.
        req.error_message = (
            f"{req.error_message or ''} | post-persist: {payload.error_message}"
        ).strip(" |")
        db.commit()
        return {"ok": True, "noop": "already ok"}
    req.status = "failed" if payload.error_message else "ok"
    req.completed_at = datetime.utcnow()
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
            if os.environ.get("SCRAPER_API_SITES") else [],
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


# ─── Frontend dashboard endpoints (JWT cookie) ───────────────────────────────


@app.get("/api/v1/dash/comparison")
def dash_comparison(
    search: str | None = None,
    min_sites: int = 2,
    site_filter: str | None = None,
    limit: int = 500,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cross-site comparison rows. Filtered by tenant_id automatically."""
    last_run = db.scalar(
        select(storage.Run.id)
        .where(storage.Run.status == "ok", storage.Run.tenant_id == user.tenant_id)
        .order_by(desc(storage.Run.id))
        .limit(1)
    )
    if not last_run:
        return []

    matches_q = (
        select(storage.Match)
        .where(storage.Match.tenant_id == user.tenant_id)
        .limit(limit)
    )
    if search:
        like = f"%{search.lower()}%"
        matches_q = matches_q.where(
            storage.Match.canonical_name.ilike(like)
            | storage.Match.canonical_brand.ilike(like)
        )
    matches = db.scalars(matches_q).all()

    # Pre-fetch latest snapshots для всех product_id одной агрегатной SELECT'ой.
    # Раньше делалось N+1 (snapshot per match × per site = ~3 за match), плюс
    # после diff-only persist'а (2026-05-09) прошлая логика `WHERE run_id ==
    # last_run` пропускала продукты без price-changes в last_run.
    all_pids = [p.id for m in matches for p in m.products]
    snaps_by_pid = storage.latest_snapshots_per_product(db, all_pids)

    out: list[ComparisonRowOut] = []
    for m in matches:
        raw_prices: dict[str, dict[str, Any]] = {}
        for p in m.products:
            snap = snaps_by_pid.get(p.id)
            price = (snap.discount_price or snap.price) if snap else None
            if price is not None and price > 0:
                raw_prices[p.site] = {
                    "price": price,
                    "is_on_sale": snap.is_on_sale if snap else False,
                    "url": p.url,
                    "product_id": p.id,
                }

        # Price-sanity filter (2026-05-11): на aptekonline.az встречаются
        # явные опечатки — товар стоит 0.20 ₼ против 28 ₼ на других сайтах
        # (вероятно single-piece price вместо pack-price). Если в кластере
        # есть цена < 10% медианы — это data error, не реальный undercut.
        # Filterим такой outlier, чтобы клиент не видел false-undercut алерты.
        prices = raw_prices
        if len(raw_prices) >= 2:
            vals = sorted(d["price"] for d in raw_prices.values())
            median = vals[len(vals) // 2]
            outlier_threshold = median * 0.1  # 10× cheaper than median
            prices = {
                site: data for site, data in raw_prices.items()
                if data["price"] >= outlier_threshold
            }

        sites_with_price = len(prices)
        if sites_with_price < min_sites:
            continue
        if site_filter and site_filter not in prices:
            continue
        price_vals = [d["price"] for d in prices.values()]
        min_p = min(price_vals) if price_vals else None
        max_p = max(price_vals) if price_vals else None
        cheapest = min(prices.items(), key=lambda x: x[1]["price"])[0] if prices else None
        spread = round((max_p - min_p) / max_p * 100, 1) if max_p else None
        out.append(ComparisonRowOut(
            canonical_id=m.id,
            name=m.canonical_name,
            brand=m.canonical_brand,
            pack_size=m.canonical_pack_size,
            is_manual=m.is_manual,
            sites_with_price=sites_with_price,
            min_price=min_p, max_price=max_p,
            spread_pct=spread,
            cheapest_site=cheapest,
            prices=prices,
        ))
    return out


@app.get("/api/v1/dash/roi/actions")
def dash_roi_actions(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    from src import roi
    actions = roi.compute_actions(db)
    return [
        {
            "type": a.type,
            "severity": a.severity,
            "title": a.title,
            "detail": a.detail,
            "product_name": a.product_name,
            "product_url": a.product_url,
            "current_value_azn": a.current_value_azn,
            "target_value_azn": a.target_value_azn,
            "estimated_monthly_impact_azn": a.estimated_monthly_impact_azn,
            "competitor_site": a.competitor_site,
        }
        for a in actions
    ]


@app.get("/api/v1/dash/alerts")
def dash_alerts(
    limit: int = 100,
    severity: str | None = None,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    stmt = (
        select(storage.AlertEvent)
        .where(storage.AlertEvent.tenant_id == user.tenant_id)
        .order_by(desc(storage.AlertEvent.created_at))
        .limit(limit)
    )
    if severity:
        stmt = stmt.where(storage.AlertEvent.severity == severity)
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
        }
        for e in events
    ]


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


@app.get("/api/v1/dash/brand-share")
def dash_brand_share(
    top_n: int = 30,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    from src import analytics
    rows = analytics.brand_share(db, top_n=top_n)
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
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    from src import analytics
    rows = analytics.price_index_by_category(db)
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


@app.get("/api/v1/dash/forecast/movers")
def dash_forecast_movers(
    limit: int = 20,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    from src import forecast
    movers = forecast.top_movers(db, limit=limit)
    return movers


@app.get("/api/v1/dash/runs")
def dash_runs(
    limit: int = 30,
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    runs = db.scalars(
        select(storage.Run)
        .order_by(desc(storage.Run.id))
        .limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "status": r.status,
            "products_scraped": r.products_scraped,
            "products_per_site": r.products_per_site,
            "sites_completed": r.sites_completed,
            "error_message": r.error_message,
        }
        for r in runs
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
      "products_per_site_category": {site: {category: count}}
    }
    """
    run = db.scalar(select(storage.Run).where(storage.Run.id == run_id))
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
        "sites_completed": run.sites_completed,
    }


@app.get("/api/v1/dash/categories")
def dash_categories_list(
    user: storage.TenantUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    cats = db.scalars(
        select(storage.Category).order_by(storage.Category.id)
    ).all()
    return [
        {
            "id": c.id, "key": c.key,
            "label_ru": c.label_ru, "label_az": c.label_az,
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
            for b in products[i + 1:]:
                match_actions.add_rejection(db, a.id, b.id, reason=f"manual:user={user.id}")
    # Unlink products from this match
    for p in products:
        p.canonical_id = None
    # Delete the match itself
    db.delete(match)
    db.commit()
    return Response(status_code=204)


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
                {"site": link.site, "url": link.url, "external_id": link.external_id, "status": link.status}
                for link in t.links
            ] if hasattr(t, "links") else [],
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
            db.add(storage.TrackedProductLink(
                tracked_product_id=tp.id,
                site=site,
                url=url,
                status="pending",
            ))
    db.commit()
    return {"id": tp.id}


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
        select(storage.AlertEvent)
        .order_by(desc(storage.AlertEvent.created_at))
        .limit(limit)
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
            "id": p.id, "site": p.site, "external_id": p.external_id,
            "url": p.url, "name": p.name, "brand": p.brand,
            "category": p.category, "canonical_id": p.canonical_id,
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
        out.append({
            "canonical_id": m.id,
            "name": m.canonical_name,
            "brand": m.canonical_brand,
            "is_manual": m.is_manual,
            "prices": prices,
        })
    return out


@app.get("/api/v1/margin", dependencies=[Depends(require_api_key)])
def margin_endpoint(db: Session = Depends(get_db)):
    rows = inv_mod.margin_report(db)
    return [
        {
            "product_id": r.product_id, "name": r.name,
            "sale_price": r.sale_price, "purchase_price": r.purchase_price,
            "margin_azn": r.margin_azn, "margin_pct": r.margin_pct,
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
        db.add(storage.StockLevel(
            product_id=product.id if product else None,
            canonical_id=product.canonical_id if product else None,
            sku=item.sku, name=item.name or (product.name if product else None),
            qty=item.qty, is_in_stock=item.qty > 0,
            source=source, updated_at=utcnow(),
        ))
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
        db.add(storage.SupplierPrice(
            product_id=product.id if product else None,
            canonical_id=product.canonical_id if product else None,
            sku=item.sku, name=item.name or (product.name if product else None),
            supplier_name=item.supplier_name,
            purchase_price=item.purchase_price, currency=item.currency,
            source=source, updated_at=utcnow(),
        ))
    db.commit()
    return {"received": len(items), "matched_to_product": matched}
