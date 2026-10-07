"""SQLAlchemy data model and session helpers.

Schema overview:
    runs            — каждый ежедневный прогон
    products        — товары как они увидены на сайтах (один SKU = одна строка per site)
    price_snapshots — цена/промо на момент конкретного прогона
    matches         — связки одного товара через 3 сайта (canonical_id)
    promos          — обнаруженные промо-баннеры/кампании
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from src._time import utcnow
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)


class Base(DeclarativeBase):
    pass


_SESSION_CACHE_LOCK = threading.Lock()
_SESSION_FACTORY_CACHE: dict[str, sessionmaker] = {}


def _env_int(name: str, default: int, *, min_value: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(min_value, value)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="running"
    )  # running/ok/degraded/failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    products_scraped: Mapped[int] = mapped_column(Integer, default=0)
    sites_completed: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Per-site breakdown {site: count}. Nullable для backward-compat со старыми
    # runs (до 2026-05-11). Используется smoke_test'ом для точной per-site
    # baseline в multi-site/server-side прогонах.
    products_per_site: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Per-(site,category) breakdown {site: {category_label: count}}. Заполняется
    # после persist'а: показывает сколько товаров скрейпер увидел по каждому
    # category-маршруту на каждом сайте. Используется в UI «Coverage» панели и
    # для debug — клиент видит «pharm scraped 100 vitamins, apt scraped 320».
    products_per_site_category: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Versioned quality envelope populated after the scrape phase. Contains
    # per-site and per-category/URL expected/completed/failed counters, reasons
    # and bounded errors. Financial alerts may only use status='ok' runs.
    run_quality: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Trust gate for financial consumers. Only an unbounded category run with
    # healthy per-site coverage may be marked as a verified full catalog.
    catalog_scope: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    full_catalog_sites: Mapped[str | None] = mapped_column(String(200), nullable=True)
    catalog_verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    catalog_verification_reason: Mapped[str | None] = mapped_column(
        String(300), nullable=True
    )

    snapshots: Mapped[list["PriceSnapshot"]] = relationship(
        back_populates="run", foreign_keys="PriceSnapshot.run_id"
    )


def run_is_financially_eligible(run: Run | None) -> bool:
    """Only a verified full-catalog run may drive money recommendations."""
    if run is None:
        return False
    if run.catalog_scope != "full" or not bool(run.catalog_verified):
        return False
    if (run.run_quality or {}).get("financially_eligible") is not True:
        return False
    if run.status == "ok":
        return True
    if run.status == "running":
        from src.product_policy import is_finalizing_trusted_run

        return is_finalizing_trusted_run(run.id)
    return False


def run_is_watchlist_price_alert_eligible(run: Run | None) -> bool:
    """Whether a completed pinned-SKU tick may emit *local* price-drop alerts.

    A watchlist refresh is deliberately not a financial catalog epoch: it must
    never refresh ROI, cross-site comparisons, or undercut recommendations.
    It can nevertheless safely report that the *same pinned product URL*
    changed price, provided every requested URL completed successfully.  Keep
    this narrow exception separate from :func:`run_is_financially_eligible` so
    a partial run can never accidentally unlock money-facing consumers.
    """
    if run is None or run.status != "ok" or run.catalog_scope != "partial":
        return False
    quality = run.run_quality or {}
    if quality.get("mode") != "watchlist":
        return False
    sites = quality.get("sites")
    return bool(sites) and all(
        isinstance(details, dict) and details.get("status") == "ok"
        for details in sites.values()
    )


class ScrapeRequest(Base):
    """Очередь scrape-запросов, запущенных пользователем через UI.

    Клиент жмёт «▶️ Запустить scan сейчас» → backend создаёт row здесь со
    status='pending'. Server-side watcher (`pharmacy-monitor-scrape-watcher.timer`)
    polls API на pending → если есть, исполняет `pharmacy-monitor run ...` на
    прод-сервере через оплаченные proxy/direct scrape-пути → PATCH запись
    terminal status (ok/degraded/failed) + run_id.

    Очередь нужна, чтобы UI не держал HTTP request во время долгого scrape и
    чтобы watcher сериализовал тяжёлые run/scrape/rematch задачи.
    """

    __tablename__ = "scrape_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenant_users.id", ondelete="SET NULL"), nullable=True
    )
    # Scope: 'all' (все active categories), 'category' (--category-id), 'site' (--site X)
    mode: Mapped[str] = mapped_column(String(20), default="all")
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    sites: Mapped[str | None] = mapped_column(String(200), nullable=True)  # CSV
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending → running → ok/degraded/failed
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    """Immutable trail of successful dashboard mutations.

    Payload bodies are intentionally not stored: pricing uploads, passwords and
    integration secrets must never leak into an audit row.  The request path,
    actor, method, response status and request id are enough to establish who
    changed which resource and correlate with server logs.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenant_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(20))
    resource: Mapped[str] = mapped_column(String(500), index=True)
    response_status: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Product(Base):
    """Товар как он представлен на конкретном сайте."""

    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("site", "external_id", name="uq_site_extid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    site: Mapped[str] = mapped_column(String(50), index=True)  # pharmonline/aptekonline/aloe
    external_id: Mapped[str] = mapped_column(String(200), index=True)  # SKU/URL-id на сайте
    url: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(500), index=True)
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    manufacturer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Authoritative manufacturing country for this concrete SKU.  Kept separate
    # from manufacturer company/brand so identity policy does not conflate them.
    manufacturer_country_code: Mapped[str | None] = mapped_column(
        String(2), nullable=True, index=True
    )
    manufacturer_country_raw: Mapped[str | None] = mapped_column(String(160), nullable=True)
    country_resolution_status: Mapped[str] = mapped_column(
        String(20), default="unknown", index=True
    )
    country_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    country_observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # A conflicting one-off observation is quarantined rather than immediately
    # changing identity and repartitioning a cluster.
    country_candidate_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    country_candidate_seen_count: Mapped[int] = mapped_column(Integer, default=0)
    country_candidate_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    country_candidate_run_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # Current website offer state.  This is independent from StockLevel, which
    # represents the client's ERP stock.
    offer_availability_status: Mapped[str] = mapped_column(
        String(20), default="unknown", index=True
    )
    offer_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    availability_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    availability_observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    availability_run_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    category: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    # Operator override for the source-independent comparison taxonomy.
    # Kept separate from scraper-owned ``category`` so future runs do not erase it.
    manual_category_key: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    dosage: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 500mg, 10ml...
    pack_size: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 30 tab, 100ml...
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 2.1 (2026-05-27) — canonical barcode/EAN/GTIN/UPC. All four labels
    # represent the same identifier in pharma; we collapse into one column.
    # Indexed for matcher v2 priority-0 lookup. Nullable until scraper fills.
    barcode: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    name_normalized: Mapped[str] = mapped_column(String(500), index=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    canonical_id: Mapped[int | None] = mapped_column(
        ForeignKey("matches.id"), nullable=True, index=True
    )

    # 2026-05-30: страница товара отдаёт 404. aptekonline JSON API листит
    # «фантомные» товары (в каталоге, но без живой страницы) → они скрейпятся,
    # matched, last_seen свежий, но ссылка ведёт на 404. Ставится `validate-links`
    # CLI (HTTP-проверка); comparison исключает такие товары. NULL = жива/не
    # проверялась. Единственный надёжный сигнал — реальный HTTP-чек URL.
    url_dead_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 2026-05-31: настоящий бренд, восстановленный из АВТОРИТЕТНОГО источника
    # (pharmonline — токен бренда в slug; aptekonline — JSON `"brand":{}` на
    # странице товара; aloe — уже чистое поле brand). В отличие от `brand`,
    # который из-за first-word fallback в extract_brand() у 92% pharmonline хранит
    # generic-имя («Alaqanqal»), а не фирму. Используется brand-conflict guard'ом
    # матчера: разные ПОТРЕБИТЕЛЬСКИЕ бренды (Biola≠Herba Flora) → разные товары;
    # компании-заводы (Merck KGaA, Egis İlaç) считаются неразличающими. NULL =
    # не восстановлен (guard не срабатывает, recall сохраняется).
    brand_verified: Mapped[str | None] = mapped_column(String(200), nullable=True)

    snapshots: Mapped[list[PriceSnapshot]] = relationship(back_populates="product")
    canonical: Mapped[Match | None] = relationship(back_populates="products")


class PharmonlinePublicAPIIdentityReconciliation(Base):
    """Immutable proof for a guarded Pharmonline legacy-ID reconciliation.

    The recovery changes a Product in place, preserving its primary key and
    every attached snapshot, observation, match, stock and supplier record.
    This append-only record preserves the before/after identifiers, exact
    staged source and catalog proof; it is never a mutable trust flag.
    """

    __tablename__ = "pharmonline_public_api_identity_reconciliations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "product_id",
            name="uq_pharmonline_public_api_reconciliation_product",
        ),
        UniqueConstraint(
            "tenant_id",
            "legacy_external_id",
            name="uq_pharmonline_public_api_reconciliation_legacy_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "public_api_external_id",
            name="uq_pharmonline_public_api_reconciliation_public_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    legacy_external_id: Mapped[str] = mapped_column(String(200))
    public_api_external_id: Mapped[str] = mapped_column(String(200))
    legacy_canonical_url: Mapped[str] = mapped_column(String(500))
    public_api_canonical_url: Mapped[str] = mapped_column(String(500))
    proof_version: Mapped[str] = mapped_column(String(40))
    source_manifest_sha256: Mapped[str] = mapped_column(String(64))
    catalog_fingerprint_sha256: Mapped[str] = mapped_column(String(64))
    source_transport: Mapped[str] = mapped_column(String(40))
    preflight_run_ref: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PharmonlinePublicAPIIdentityAdmission(Base):
    """Immutable first-party admission proof for a current public identity.

    Unlike ``PharmonlinePublicAPIIdentityReconciliation``, this ledger never
    claims a legacy-ID transition.  It records either a current native-ID row
    that was already present in the tenant catalog or a genuinely new product
    created from a fully verified public catalog.  Those two cases must remain
    explicit: a public-source admission is trusted only for the guarded public
    API path and never changes or impersonates DDP provenance.
    """

    __tablename__ = "pharmonline_public_api_identity_admissions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "product_id",
            name="uq_pharmonline_public_api_admission_product",
        ),
        UniqueConstraint(
            "tenant_id",
            "public_api_external_id",
            name="uq_pharmonline_public_api_admission_public_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    # ``existing_native_id`` means the exact native ID and URL were already
    # stored. ``new_public_product`` means no Product with that ID or URL was
    # present in any tenant before the guarded creation.
    admission_kind: Mapped[str] = mapped_column(String(40))
    public_api_external_id: Mapped[str] = mapped_column(String(200))
    public_api_canonical_url: Mapped[str] = mapped_column(String(500))
    proof_version: Mapped[str] = mapped_column(String(40))
    source_manifest_sha256: Mapped[str] = mapped_column(String(64))
    catalog_fingerprint_sha256: Mapped[str] = mapped_column(String(64))
    source_transport: Mapped[str] = mapped_column(String(40))
    preflight_run_ref: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PharmonlinePublicAPIIdentityQuarantine(Base):
    """Immutable separation proof for a contradicted legacy native identity.

    A first-party legacy URL can prove that an old stored product is still a
    distinct page even when the current public API reuses its native ID at a
    different URL.  In that narrow case the old Product remains in place so
    all attached observations and snapshots retain their original meaning;
    its external ID is archived and a separate current Product is admitted.
    This ledger records both sides of that split and is deliberately not a
    mutable trust flag.
    """

    __tablename__ = "pharmonline_public_api_identity_quarantines"
    __table_args__ = (
        CheckConstraint(
            "legacy_product_id <> replacement_product_id",
            name="ck_pharmonline_public_api_quarantine_distinct_products",
        ),
        UniqueConstraint(
            "tenant_id",
            "legacy_product_id",
            name="uq_pharmonline_public_api_quarantine_legacy_product",
        ),
        UniqueConstraint(
            "tenant_id",
            "replacement_product_id",
            name="uq_pharmonline_public_api_quarantine_replacement_product",
        ),
        UniqueConstraint(
            "tenant_id",
            "public_api_external_id",
            name="uq_pharmonline_public_api_quarantine_public_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    legacy_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    replacement_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    quarantine_kind: Mapped[str] = mapped_column(String(40))
    legacy_external_id: Mapped[str] = mapped_column(String(200))
    archived_external_id: Mapped[str] = mapped_column(String(200))
    legacy_canonical_url: Mapped[str] = mapped_column(String(500))
    public_api_external_id: Mapped[str] = mapped_column(String(200))
    public_api_canonical_url: Mapped[str] = mapped_column(String(500))
    proof_version: Mapped[str] = mapped_column(String(40))
    source_manifest_sha256: Mapped[str] = mapped_column(String(64))
    catalog_fingerprint_sha256: Mapped[str] = mapped_column(String(64))
    source_transport: Mapped[str] = mapped_column(String(40))
    preflight_run_ref: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PharmonlinePublicAPICatalogBaseline(Base):
    """Append-only non-ratcheting catalog floor for public-API recovery."""

    __tablename__ = "pharmonline_public_api_catalog_baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    catalog_item_count: Mapped[int] = mapped_column(Integer)
    minimum_catalog_item_count: Mapped[int] = mapped_column(Integer)
    verified_identity_count: Mapped[int] = mapped_column(Integer)
    trusted_ddp_item_count: Mapped[int] = mapped_column(Integer)
    retired_ddp_item_count: Mapped[int] = mapped_column(Integer)
    reconciled_item_count: Mapped[int] = mapped_column(Integer)
    proof_version: Mapped[str] = mapped_column(String(40))
    source_manifest_sha256: Mapped[str] = mapped_column(String(64))
    catalog_fingerprint_sha256: Mapped[str] = mapped_column(String(64))
    source_transport: Mapped[str] = mapped_column(String(40))
    preflight_run_ref: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PriceSnapshot(Base):
    """Цена и промо-статус товара на момент конкретного прогона."""

    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)  # обычная цена
    discount_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # после скидки
    discount_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_on_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    promo_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Проверенный полный сбор, который увидел товар с этой же ценой и ничего не
    # записал (diff-only). Строка остаётся «изменением цены» прогона `run_id`, но
    # денежным выводам она доверена — см. `trusted_snapshot_filter`.
    confirmed_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )

    run: Mapped[Run] = relationship(back_populates="snapshots", foreign_keys=[run_id])
    product: Mapped[Product] = relationship(back_populates="snapshots")


class Match(Base):
    """Канонический товар, объединяющий до 3 SKU с разных сайтов."""

    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    canonical_name: Mapped[str] = mapped_column(String(500))
    canonical_brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    canonical_dosage: Mapped[str | None] = mapped_column(String(100), nullable=True)
    canonical_pack_size: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)  # 0..1
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)  # подтверждено вручную
    match_strategy: Mapped[str | None] = mapped_column(String(30), nullable=True)
    needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False
    )  # флаг UI: подозрительное расхождение цен
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    products: Mapped[list[Product]] = relationship(back_populates="canonical")


class Tenant(Base):
    """Изолированный клиент SaaS (multi-tenant фундамент).

    На текущей стадии: один Tenant = один клиент (pharmonline). Все остальные
    таблицы получат опциональный `tenant_id` чтобы в Q2 включить multi-tenant
    queries без миграции данных. Сейчас все записи могут принадлежать default
    tenant'у (id=1) для backward compatibility.
    """

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    client_site: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    plan: Mapped[str] = mapped_column(String(50), default="trial")  # trial/basic/pro
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TenantUser(Base):
    """Пользователь привязанный к Tenant.

    Сейчас минимум: email + опц. magic-link токен. В Q2 — добавим OAuth/SSO.
    """

    __tablename__ = "tenant_users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_tenant_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(200), index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="admin")  # admin/viewer
    # Per-user password hash (2026-05-11). Если NULL — auth.login использует
    # global ADMIN_PASSWORD_HASH из /etc/pharmacy-monitor/env (bootstrap mode).
    # Когда пользователь меняет пароль через UI — пишется сюда, и далее auth
    # сначала проверяет DB-hash, фолбэк на env только если DB-hash NULL.
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    magic_token: Mapped[str | None] = mapped_column(String(100), nullable=True)
    magic_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Notification preferences (W9). All optional.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    # Per-channel severity threshold: "info" / "warning" / "critical" / "off"
    # If None, defaults to "warning" for both channels.
    email_severity_min: Mapped[str | None] = mapped_column(String(20), nullable=True)
    telegram_severity_min: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Quiet hours: "22-08" means no telegram between 22:00 and 08:00 local time. None = always on.
    quiet_hours: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Daily / weekly digest opt-in.
    daily_digest: Mapped[bool] = mapped_column(Boolean, default=False)
    weekly_digest: Mapped[bool] = mapped_column(Boolean, default=False)


class MatchRejection(Base):
    """Анти-матч: пара Product'ов которые НЕ являются одним товаром.

    Auto-matcher должен пропускать любую пару (a, b) присутствующую в этой таблице.
    Создаётся когда юзер нажимает "✗ Не один товар" в UI Сравнения.

    Нормализация: всегда храним в порядке (min_id, max_id) чтобы (a,b) и (b,a)
    были одной записью. UniqueConstraint защищает от дубликатов.
    """

    __tablename__ = "match_rejections"
    __table_args__ = (UniqueConstraint("product_a_id", "product_b_id", name="uq_rejection_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_a_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    product_b_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reason_type: Mapped[str] = mapped_column(String(40), default="manual", index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)


class OfferObservation(Base):
    """Trusted per-run observation of country and website availability."""

    __tablename__ = "offer_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    country_raw: Mapped[str | None] = mapped_column(String(160), nullable=True)
    country_resolution_status: Mapped[str] = mapped_column(String(20), default="unknown")
    country_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    availability_status: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    availability_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MatchPolicyAudit(Base):
    """Rollback record for machine-driven match partitioning."""

    __tablename__ = "match_policy_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    match_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AloeCountryMapping(Base):
    """Versioned, detail-page-verified Aloe manufacturer-country dictionary."""

    __tablename__ = "aloe_country_mappings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "country_id", name="uq_aloe_country_mapping"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    country_id: Mapped[str] = mapped_column(String(40), index=True)
    country_code: Mapped[str] = mapped_column(String(2), index=True)
    country_raw: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(Text)
    sample_count: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    verified_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Category(Base):
    """Категория для скрейпинга в category-режиме.

    Каждая запись — одна категория с маппингом slug на каждом из 3 сайтов.
    Если slug=null для сайта — этот сайт пропускается для данной категории.

    Раньше жили в config/categories.yaml; теперь в БД с CRUD через CLI и Dashboard.
    YAML остаётся только как seed для первого запуска (миграция).
    """

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True)  # уникальный код
    label_ru: Mapped[str] = mapped_column(String(200))
    label_az: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pharmonline_slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    aptekonline_slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    aloe_slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Recipient(Base):
    """Получатели email-рассылки + Telegram-подписки. CRUD через CLI и дашборд."""

    __tablename__ = "recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AlertRule(Base):
    """Правило когда срабатывает алерт.

    Каждое правило имеет тип и параметры (JSON). Engine проверяет правило на
    каждом прогоне и создаёт AlertEvent если условие выполнено.
    """

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    rule_type: Mapped[str] = mapped_column(String(50), index=True)
    # Типы: undercut_threshold, price_drop_pct, price_change_pct, new_product, promo_started,
    #       price_raise_opportunity
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    # channels — список: ["email", "telegram"]
    channels: Mapped[list | None] = mapped_column(JSON, nullable=True, default=lambda: ["email"])
    cooldown_hours: Mapped[int] = mapped_column(Integer, default=12)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AlertEvent(Base):
    """История срабатываний правил — для dedup и UI feed."""

    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    rule_type: Mapped[str] = mapped_column(String(50), index=True)
    # dedup_key — комбинация полей которая идентифицирует "ту же ситуацию"
    # (например "undercut|product_id=42|competitor=aloe").
    # Если такой же event уже был в течение cooldown_hours — не дублируем.
    dedup_key: Mapped[str] = mapped_column(String(300), index=True)
    severity: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(500))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    channels_sent: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # multi-tenant: каждый event принадлежит одному tenant. Default=1 — обратная
    # совместимость для single-tenant pilot.
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)

    # P1.4 (PO Audit 2026-05-17): in-app inbox state. Global (не per-user) пока
    # pilot — multi-user сценарий вынесем в alert_user_state позже.
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class RoiActionsCache(Base):
    """Pre-computed ROI actions per (tenant, client_site).

    `roi.compute_actions` делает N+1 SQL по матчам — 15-30с для 3к матчей.
    Frontend timeout'ит на 15с (`/dash/roi/actions`), показывая 408 на 4
    страницах из 11 (P0.1 в PO Audit 2026-05-17).

    Решение: compute_actions запускается в фоне после успешного `persist_results`
    (см. `src/main.py:_refresh_roi_cache`). HTTP-handler читает payload здесь —
    JSON-сериализованный list[ActionItem]. Если cache отсутствует или
    `computed_at` старше 26 часов (∼суточный cron + jitter) — fallback на
    inline compute с увеличенным таймаутом.

    Один row per (tenant_id, client_site). Upsert при каждом scrape success.
    """

    __tablename__ = "roi_actions_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    client_site: Mapped[str] = mapped_column(String(32))
    # JSON-сериализованный list[dict] — те же поля что в HTTP-response API.
    # Структура совпадает с ActionItem (см. src/roi.py).
    payload: Mapped[list] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Какой `Run.id` сгенерил кэш — для отладки «откуда устаревшие цифры»
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Financial output differs between shadow/enforce.  Old cache rows are
    # invalidated automatically when either product-policy mode changes.
    policy_fingerprint: Mapped[str] = mapped_column(
        String(80), default="legacy", nullable=False
    )
    # Exact verified full-catalog Run IDs used to calculate this payload.
    # Age alone is insufficient: a newer full scan can change country/stock
    # immediately while an otherwise "fresh" cache is still present.
    trust_epoch: Mapped[str] = mapped_column(
        String(240), default="legacy", nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "client_site", name="uq_roi_cache_tenant_site"),
    )


class PricingConfig(Base):
    """Per-tenant configurable ROI thresholds (Phase 4.1, 2026-05-27).

    Was: hardcoded `raise_threshold_pct=5.0`, `undercut_threshold_pct=3.0`,
         `max_spread_pct=80.0` в compute_actions(). Меняешь — нужен deploy.
    Now: загружается из DB через `load_pricing_config(session, tenant_id)`.
         Defaults на створении row = старые hardcoded values (back-compat).

    Поля:
      - raise_threshold_pct (%): минимальная разница чтобы советовать поднять
      - undercut_threshold_pct (%): минимальная просадка чтобы алерт «конкурент дешевле»
      - max_spread_pct (%): скрываем матчи с большим спредом (вероятно bad match)
      - min_margin_pct (%): не предлагать undercut если маржа после < этого
      - max_per_type: top-N действий на тип

    Один row per tenant. Создаётся при первом запросе с дефолтами.
    """

    __tablename__ = "pricing_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer,
        default=1,
        index=True,
        unique=True,
    )
    raise_threshold_pct: Mapped[float] = mapped_column(Float, default=5.0)
    undercut_threshold_pct: Mapped[float] = mapped_column(Float, default=3.0)
    max_spread_pct: Mapped[float] = mapped_column(Float, default=80.0)
    min_margin_pct: Mapped[float] = mapped_column(Float, default=10.0)
    max_per_type: Mapped[int] = mapped_column(Integer, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
    )


def load_pricing_config(session, tenant_id: int = 1) -> "PricingConfig":
    """Get-or-create config row для tenant. Returns mutable ORM instance."""
    from sqlalchemy import select as _select

    cfg = session.scalar(_select(PricingConfig).where(PricingConfig.tenant_id == tenant_id))
    if cfg is None:
        cfg = PricingConfig(tenant_id=tenant_id)
        session.add(cfg)
        session.flush()
    return cfg


class TrackedProduct(Base):
    """Конкретные SKU из watchlist клиента. Каждая запись — товар, который надо отслеживать.

    Связь с реальными `Product` записями делается через `TrackedProductLink` (один к трём сайтам).
    """

    __tablename__ = "tracked_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(500))
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    dosage: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pack_size: Mapped[str | None] = mapped_column(String(100), nullable=True)
    search_query: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )  # текст для site search; если null — берём canonical_name
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)

    links: Mapped[list[TrackedProductLink]] = relationship(
        back_populates="tracked_product", cascade="all, delete-orphan"
    )


class TrackedCategory(Base):
    """Категория, которую клиент хочет держать под рукой в watchlist.

    Сравнение берётся из уже существующих category-comparison/comparison экранов;
    эта таблица только хранит приоритетные категории на tenant.
    """

    __tablename__ = "tracked_categories"
    __table_args__ = (
        UniqueConstraint("tenant_id", "category_id", name="uq_tracked_category_tenant_category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    category: Mapped[Category] = relationship()


class TrackedProductLink(Base):
    """Привязка watchlist-записи к конкретной странице на конкретном сайте.

    Workflow:
    1. Клиент добавляет TrackedProduct с canonical_name + (опц.) фиксированными URL
    2. На первом прогоне scraper либо использует pinned URL, либо ищет через site search
    3. Найденный URL пинуется (status=confirmed) и в дальнейшем переиспользуется
    """

    __tablename__ = "tracked_product_links"
    __table_args__ = (UniqueConstraint("tracked_product_id", "site", name="uq_tracked_site"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tracked_product_id: Mapped[int] = mapped_column(
        ForeignKey("tracked_products.id", ondelete="CASCADE"), index=True
    )
    site: Mapped[str] = mapped_column(String(50))  # pharmonline/aptekonline/aloe
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending/confirmed/not_found
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    tracked_product: Mapped[TrackedProduct] = relationship(back_populates="links")


class StockLevel(Base):
    """Текущий уровень склада клиента по товару.

    Импортируется CSV'ом из ERP / 1C клиента. Привязка к Product по
    (site=pharmonline, external_id) ИЛИ по canonical_id для "одного товара
    на разных сайтах". Если qty=0 — товар out-of-stock у клиента, не
    нужно алертить про undercut (мы всё равно не продаём).

    Логика обновления: один stock-import переписывает все записи для
    данного `source` (например 'erp_main' или 'manual_csv').
    """

    __tablename__ = "stock_levels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Одна из двух привязок (другая может быть null):
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=True, index=True
    )
    canonical_id: Mapped[int | None] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sku: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    is_in_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(50), default="manual_csv")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SupplierPrice(Base):
    """Закупочная цена от поставщика.

    Используется для расчёта маржи. Один товар может иметь >=1 поставщика
    (берём самого дешёвого = лучшая margin). Аналогично StockLevel —
    импортируется CSV'ом с переписыванием по `source`.
    """

    __tablename__ = "supplier_prices"
    __table_args__ = (
        UniqueConstraint(
            "product_id", "supplier_name", name="uq_supplier_price_product_supplier"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=True, index=True
    )
    canonical_id: Mapped[int | None] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sku: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    supplier_name: Mapped[str] = mapped_column(String(200))
    purchase_price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), default="AZN")
    source: Mapped[str] = mapped_column(String(50), default="manual_csv")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CostImportBatch(Base):
    """Reversible dashboard CSV import metadata and before/after values."""

    __tablename__ = "cost_import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenant_users.id", ondelete="SET NULL"), nullable=True
    )
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rows_processed: Mapped[int] = mapped_column(Integer, default=0)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    changes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rolled_back_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenant_users.id", ondelete="SET NULL"), nullable=True
    )


class SavedView(Base):
    """Сохранённый набор фильтров дашборда — 'Моя категория', 'Топ-100', etc.

    JSON params содержит все фильтры выбранные пользователем; UI применяет
    при выборе из dropdown.
    """

    __tablename__ = "saved_views"
    __table_args__ = (UniqueConstraint("name", name="uq_saved_view_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    scope: Mapped[str] = mapped_column(String(50), default="comparison")
    # comparison/overview/analytics
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)


class Promo(Base):
    """Промо-кампании/баннеры обнаруженные на сайтах (не привязанные к конкретному SKU)."""

    __tablename__ = "promos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    site: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    landing_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    valid_until: Mapped[str | None] = mapped_column(String(50), nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)


def make_engine(database_url: str | None = None):
    url = database_url or os.getenv("DATABASE_URL", "sqlite:///data/db.sqlite")
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        rel = url.replace("sqlite:///", "", 1)
        Path(rel).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(url, echo=False, future=True)
    if url.startswith("sqlite"):
        return create_engine(url, echo=False, future=True)

    return create_engine(
        url,
        echo=False,
        future=True,
        pool_size=_env_int("DB_POOL_SIZE", 5, min_value=1),
        max_overflow=_env_int("DB_MAX_OVERFLOW", 5, min_value=0),
        pool_timeout=_env_int("DB_POOL_TIMEOUT", 10, min_value=1),
        pool_recycle=_env_int("DB_POOL_RECYCLE", 1800, min_value=30),
        pool_pre_ping=True,
        pool_use_lifo=True,
    )


def init_db(database_url: str | None = None) -> None:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    _apply_lightweight_migrations(engine)
    _ensure_default_tenant(engine)


def _ensure_default_tenant(engine) -> None:
    """Создаёт default-тенант если его нет (backward-compat для не-multi-tenant БД)."""
    Session = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    with Session() as s:
        try:
            from src import tenants as _t

            _t.get_or_create_default(s)
        except Exception:
            # default-tenant не критичен, не валим init-db
            pass


def _apply_lightweight_migrations(engine) -> None:
    """Простая миграция: добавляет недостающие колонки в существующие таблицы.

    Используется вместо Alembic для маленького проекта (только SQLite, pilot mode).
    Postgres использует Alembic — этот legacy путь там не нужен.
    """
    # Skip for Postgres — Alembic is the source of truth
    if not str(engine.url).startswith("sqlite"):
        return

    from sqlalchemy import text

    with engine.begin() as conn:
        # Каждая колонка которую мы добавляли после первого релиза:
        migrations = [
            ("recipients", "telegram_chat_id", "VARCHAR(50)"),
            # Multi-tenant: default 1 (default tenant) для backward-compat
            ("runs", "tenant_id", "INTEGER DEFAULT 1"),
            ("products", "tenant_id", "INTEGER DEFAULT 1"),
            ("matches", "tenant_id", "INTEGER DEFAULT 1"),
            ("categories", "tenant_id", "INTEGER DEFAULT 1"),
            ("recipients", "tenant_id", "INTEGER DEFAULT 1"),
            ("alert_rules", "tenant_id", "INTEGER DEFAULT 1"),
            ("alert_events", "tenant_id", "INTEGER DEFAULT 1"),
            ("tracked_products", "tenant_id", "INTEGER DEFAULT 1"),
            ("stock_levels", "tenant_id", "INTEGER DEFAULT 1"),
            ("supplier_prices", "tenant_id", "INTEGER DEFAULT 1"),
            # 2026-05-11: per-site breakdown для smoke_test (multi-site runs)
            ("runs", "products_per_site", "JSON"),
            # 2026-05-11 (вечер): per-(site,category) breakdown для UI coverage panel
            ("runs", "products_per_site_category", "JSON"),
            # 2026-05-11 (ночь): password в DB (вместо global env-hash)
            ("tenant_users", "password_hash", "VARCHAR(200)"),
            # NB: новые колонки — через alembic (migrations/versions/), НЕ сюда.
            # Этот SQLite-only путь — легаси до alembic; см. 0009_product_url_dead_at.
        ]
        for table, column, coltype in migrations:
            try:
                cols = conn.execute(text(f"PRAGMA table_info({table})")).all()
                if not cols:
                    continue  # таблицы ещё нет (свежая БД — create_all создаст)
                existing = {c[1] for c in cols}
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
            except Exception:
                pass  # не валим init-db


def make_session(database_url: str | None = None):
    url = database_url or os.getenv("DATABASE_URL", "sqlite:///data/db.sqlite")
    if url.startswith("sqlite"):
        engine = make_engine(url)
        return sessionmaker(engine, expire_on_commit=False, autoflush=False)

    with _SESSION_CACHE_LOCK:
        cached = _SESSION_FACTORY_CACHE.get(url)
        if cached is not None:
            return cached
        engine = make_engine(url)
        factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
        _SESSION_FACTORY_CACHE[url] = factory
        return factory


# ============================================================================
# Query helpers — общие для api / analyzer / alerts / roi / health / forecast
# ============================================================================


def curr_and_prev_snapshots_for_run(
    session,
    current_run: "Run",
    *,
    financially_eligible_only: bool = False,
) -> tuple[list[PriceSnapshot], dict[int, PriceSnapshot]]:
    """Snapshots в current_run + последний snapshot из предыдущих прогонов.

    Возвращает (curr_snaps, prev_by_product) — стандартный паттерн для
    diff-детекторов (analyzer, alerts, потенциально roi/forecast).

    - **curr_snaps** — `PriceSnapshot` в current_run, с eager-loaded `Product`
    - **prev_by_product** — для product_id из curr_run, последний snapshot
      из прогона со `Run.started_at < current_run.started_at` (если есть).

    Работает в FULL и DIFF-ONLY persist режимах: фильтр по `Run.started_at`
    устойчив к близким (микросекунды) timestamp'ам в тестах.

    Использует две SELECT'и:
    1. Snapshots в current_run + JOIN(Product) — eager-load
    2. Aggregate MAX(captured_at) GROUP BY product_id JOIN Run WHERE
       Run.started_at < current_run.started_at — поиск prev
    """
    from sqlalchemy import func, select
    from sqlalchemy.orm import joinedload

    curr_snaps = list(
        session.scalars(
            select(PriceSnapshot)
            .options(joinedload(PriceSnapshot.product))
            .where(PriceSnapshot.run_id == current_run.id)
        ).unique()
    )
    if not curr_snaps:
        return curr_snaps, {}

    product_ids = list({s.product_id for s in curr_snaps})
    previous_filters = [
        PriceSnapshot.product_id.in_(product_ids),
        Run.started_at < current_run.started_at,
    ]
    eligible_ids: list[int] | None = None
    if financially_eligible_only:
        eligible_ids = financially_eligible_run_ids(
            session,
            tenant_id=current_run.tenant_id,
        )
        previous_filters.append(trusted_snapshot_filter(eligible_ids))
    prev_max_subq = (
        select(
            PriceSnapshot.product_id,
            func.max(PriceSnapshot.captured_at).label("max_at"),
        )
        .join(Run, Run.id == PriceSnapshot.run_id)
        .where(*previous_filters)
        .group_by(PriceSnapshot.product_id)
        .subquery()
    )
    prev_stmt = select(PriceSnapshot).join(
            prev_max_subq,
            (PriceSnapshot.product_id == prev_max_subq.c.product_id)
            & (PriceSnapshot.captured_at == prev_max_subq.c.max_at),
        )
    if eligible_ids is not None:
        prev_stmt = prev_stmt.where(trusted_snapshot_filter(eligible_ids))
    prev_snaps = session.scalars(
        prev_stmt.order_by(PriceSnapshot.product_id, PriceSnapshot.id.desc())
    ).all()
    prev_by_product: dict[int, PriceSnapshot] = {}
    for s in prev_snaps:
        prev_by_product.setdefault(s.product_id, s)
    return curr_snaps, prev_by_product


def trusted_snapshot_filter(eligible_run_ids):
    """Условие «этой записи о цене доверяют денежные выводы».

    Запись доверена, если её сделал проверенный полный сбор ИЛИ такой сбор позже
    увидел товар с той же ценой (`confirmed_run_id`). Второе нужно из-за
    diff-only: проверенный сбор, увидевший прежнюю цену, своей строки не пишет.
    До 2026-10-07 доверие читалось только по `run_id`, и цена, записанная до
    появления проверки, частичным тиком или сбором, не прошедшим её, оставалась
    недоверенной, пока не изменится: на проде так выпали 85% товаров.

    Любой запрос, выбирающий snapshot'ы «только проверенные», обязан брать
    условие отсюда, а не писать `PriceSnapshot.run_id.in_(...)` сам —
    `tests/test_trusted_prices.py` следит за этим.
    """
    from sqlalchemy import or_

    return or_(
        PriceSnapshot.run_id.in_(eligible_run_ids),
        PriceSnapshot.confirmed_run_id.in_(eligible_run_ids),
    )


def confirm_prices_observed_by_run(session, run: "Run") -> int:
    """Проверенный полный сбор подтверждает цены, которые увидел без изменений.

    Для каждого товара, который `run` наблюдал (`offer_observations`) и для
    которого не записал snapshot, берётся запись, с которой `persist_results`
    сравнил цену и счёл её прежней, — последняя на момент наблюдения. Если ей
    ещё не доверяют, в неё ставится `confirmed_run_id = run.id`. Новых строк нет:
    `price_snapshots` остаётся журналом изменений цены.

    Сбор, не прошедший проверку, ничего не подтверждает. Подтверждение другого
    проверенного сбора не затирается: если `run` позже упадёт в постобработке,
    прежнее доверие должно остаться. Повторный вызов ничего не меняет.

    Возвращает число помеченных записей. Коммит — за вызывающим.
    """
    from sqlalchemy import func, or_, select, update

    if not run_is_financially_eligible(run):
        return 0
    trusted_run_ids = financially_eligible_run_ids(
        session,
        tenant_id=run.tenant_id,
        include_run_id=run.id,
    )
    if run.id not in trusted_run_ids:
        return 0

    observed = (
        select(
            OfferObservation.product_id,
            func.max(OfferObservation.observed_at).label("observed_at"),
        )
        .where(OfferObservation.run_id == run.id)
        .group_by(OfferObservation.product_id)
        .subquery()
    )
    # Товар, цену которого записал сам `run`, в подтверждении не нуждается, а
    # его предыдущая запись — это как раз НЕ та цена, которую `run` увидел.
    written_by_run = select(PriceSnapshot.product_id).where(PriceSnapshot.run_id == run.id)
    compared_at = (
        select(
            PriceSnapshot.product_id,
            func.max(PriceSnapshot.captured_at).label("max_at"),
        )
        .join(observed, observed.c.product_id == PriceSnapshot.product_id)
        .where(
            PriceSnapshot.captured_at <= observed.c.observed_at,
            PriceSnapshot.product_id.not_in(written_by_run),
        )
        .group_by(PriceSnapshot.product_id)
        .subquery()
    )
    # Несколько записей с одним captured_at (товар дважды в одном чанке):
    # последней считается запись с большим id — так же решают и
    # `persist_results`, и читатели (`_latest_snapshot_stmt`).
    compared = (
        select(func.max(PriceSnapshot.id).label("id"))
        .join(
            compared_at,
            (PriceSnapshot.product_id == compared_at.c.product_id)
            & (PriceSnapshot.captured_at == compared_at.c.max_at),
        )
        .group_by(PriceSnapshot.product_id)
        .subquery()
    )
    result = session.execute(
        update(PriceSnapshot)
        .where(
            PriceSnapshot.id.in_(select(compared.c.id)),
            PriceSnapshot.run_id.not_in(trusted_run_ids),
            or_(
                PriceSnapshot.confirmed_run_id.is_(None),
                PriceSnapshot.confirmed_run_id.not_in(trusted_run_ids),
            ),
        )
        .values(confirmed_run_id=run.id)
        # Пайплайн живёт в одной сессии, и эти же записи в ней уже загружены
        # (`persist_results`): пометка должна дойти и до них.
        .execution_options(synchronize_session="fetch")
    )
    return int(result.rowcount or 0)


def confirm_prices_of_latest_verified_runs(session, current_run: "Run") -> dict[int, int]:
    """Подтвердить цены `current_run` и последних проверенных сборов каждого сайта.

    Кроме текущего прогона берутся последний проверенный сбор каждого сайта и
    последний проверенный сбор ДО текущего. Первое даёт алертам и ROI этого
    прогона доверенные цены чужих сайтов, второе — доверенную ПРЕЖНЮЮ цену
    своего: без неё первый сбор сайта после 2026-10-07 не с чем сравнить, и
    падение цены в нём осталось бы без алерта. Проверенные сборы до этой даты
    цен не подтверждали; подтвердить задним числом можно, потому что
    `confirm_prices_observed_by_run` смотрит на момент наблюдения.

    Возвращает `{run_id: число помеченных записей}`. Коммит — за вызывающим.
    """
    run_ids = {current_run.id}
    for before_run_id in (None, current_run.id):
        run_ids.update(
            latest_financial_run_ids_by_site(
                session,
                FULL_CATALOG_SITES,
                tenant_id=current_run.tenant_id,
                before_run_id=before_run_id,
            ).values()
        )
    confirmed: dict[int, int] = {}
    for run_id in sorted(run_ids):
        run = current_run if run_id == current_run.id else session.get(Run, run_id)
        confirmed[run_id] = confirm_prices_observed_by_run(session, run)
    return confirmed


def financially_eligible_run_ids(
    session,
    *,
    tenant_id: int = 1,
    include_run_id: int | None = None,
) -> list[int]:
    """Return completed run ids whose quality envelope permits money outputs.

    Keep the JSON interpretation in Python so the trust gate behaves the same
    on PostgreSQL and SQLite (tests/dev). Legacy rows without the envelope are
    deliberately excluded. ``include_run_id`` is the one explicit in-pipeline
    exception for a classified current run whose post-processing is not done.
    """
    from sqlalchemy import or_, select
    from src.product_policy import is_finalizing_trusted_run

    completion_filter = Run.finished_at.is_not(None)
    finalizing_run_ids = [
        int(run_id)
        for run_id in session.scalars(
            select(Run.id).where(Run.tenant_id == tenant_id, Run.status == "running")
        )
        if is_finalizing_trusted_run(int(run_id))
    ]
    if finalizing_run_ids:
        completion_filter = or_(completion_filter, Run.id.in_(finalizing_run_ids))
    if include_run_id is not None:
        completion_filter = or_(completion_filter, Run.id == include_run_id)

    runs = session.scalars(
        select(Run).where(
            Run.tenant_id == tenant_id,
            Run.status.in_(["ok", "running"]),
            Run.run_quality.is_not(None),
            completion_filter,
        )
    ).all()
    return [int(run.id) for run in runs if run_is_financially_eligible(run)]


def has_unfinished_run(session, *, tenant_id: int = 1) -> bool:
    """Whether scrape or post-processing can still mutate live product state.

    Products and their match membership are intentionally mutable rather than
    versioned by run.  External live financial calculations therefore cannot
    combine a completed snapshot lineage with that state while any run is
    unfinished.  Stale orphan runs also remain fail-closed until recovery.
    """
    from sqlalchemy import select
    from src.product_policy import is_finalizing_trusted_run

    unfinished_ids = session.scalars(
        select(Run.id).where(
            Run.tenant_id == tenant_id,
            Run.finished_at.is_(None),
        )
    ).all()
    return any(not is_finalizing_trusted_run(int(run_id)) for run_id in unfinished_ids)


def _terminal_run_ordering():
    """Cross-database ordering for runs whose post-processing has finished."""
    from sqlalchemy import desc

    return (
        desc(Run.finished_at),
        desc(Run.id),
    )


FULL_CATALOG_SITES = ("pharmonline", "aptekonline", "aloe")


def latest_terminal_run(session, *, tenant_id: int | None = 1) -> Run | None:
    """Return the terminal run that most recently finished.

    Full scans can overlap short partial ticks. ``started_at`` and id ordering
    can therefore let an earlier-finishing partial run hide a full run that
    completed later with degraded quality.
    """
    from sqlalchemy import select

    stmt = select(Run).where(
        Run.status != "running",
        Run.finished_at.is_not(None),
    )
    if tenant_id is not None:
        stmt = stmt.where(Run.tenant_id == tenant_id)
    return session.scalars(stmt.order_by(*_terminal_run_ordering()).limit(1)).first()


def latest_full_catalog_attempts_by_site(
    session,
    sites,
    *,
    tenant_id: int = 1,
) -> dict[str, Run]:
    """Return each site's newest terminal full-catalog attempt.

    A successful single-site run must not hide a newer degraded attempt for a
    different site. Runs are streamed newest-completion-first and each site is
    filled exactly once.
    """
    from sqlalchemy import and_, desc, or_, select
    from src.product_policy import is_finalizing_trusted_run

    wanted = set(sites)
    if not wanted:
        return {}
    out: dict[str, Run] = {}
    finalizing_run_id = None
    for candidate in session.scalars(
        select(Run.id).where(Run.tenant_id == tenant_id, Run.status == "running")
    ):
        if is_finalizing_trusted_run(int(candidate)):
            finalizing_run_id = int(candidate)
            break
    terminal_condition = and_(Run.status != "running", Run.finished_at.is_not(None))
    terminal_or_finalizing = (
        or_(terminal_condition, Run.id == finalizing_run_id)
        if finalizing_run_id is not None
        else terminal_condition
    )
    ordering = (desc(Run.id),) if finalizing_run_id is not None else _terminal_run_ordering()
    rows = session.scalars(
        select(Run)
        .where(
            Run.tenant_id == tenant_id,
            terminal_or_finalizing,
            Run.catalog_scope == "full",
            Run.full_catalog_sites.is_not(None),
        )
        .order_by(*ordering)
    ).yield_per(100)
    for run in rows:
        declared_sites = {
            site.strip()
            for site in (run.full_catalog_sites or "").split(",")
            if site.strip()
        }
        for site in (wanted - out.keys()) & declared_sites:
            out[site] = run
        if wanted.issubset(out):
            break
    return out


def latest_financial_run_ids_by_site(
    session,
    sites,
    *,
    tenant_id: int = 1,
    before_run_id: int | None = None,
) -> dict[str, int]:
    """Latest verified full-catalog run lineage for each requested site."""
    from sqlalchemy import desc, select

    wanted = set(sites)
    if not wanted:
        return {}
    eligible_ids = financially_eligible_run_ids(session, tenant_id=tenant_id)
    if before_run_id is not None:
        eligible_ids = [run_id for run_id in eligible_ids if run_id < before_run_id]
    if not eligible_ids:
        return {}
    runs = session.scalars(
        select(Run)
        .where(
            Run.id.in_(eligible_ids),
        )
        .order_by(desc(Run.id))
    ).all()
    out: dict[str, int] = {}
    for run in runs:
        for site, details in ((run.run_quality or {}).get("sites") or {}).items():
            if site in wanted and site not in out and details.get("status") == "ok":
                out[site] = run.id
        if wanted.issubset(out):
            break
    return out


def new_product_cutoffs_by_site(session, current_run: "Run", sites) -> dict[str, datetime]:
    """С какого момента товар сайта считается «новым» для проверенного прогона.

    «Нет проверенной цены в прошлом» не значит «товар новый»: цену могли записать
    до появления проверки, частичным тиком или сбором, не прошедшим проверку.
    2026-10-04 первый за месяц проверенный сбор aptekonline так объявил новыми
    774 товара, лежавших в каталоге с мая. Новым считается товар, появившийся
    после начала предыдущего проверенного полного сбора своего сайта; если
    такого сбора ещё не было — появившийся в текущем прогоне.

    Это только нижняя граница даты появления. Кандидаты по-прежнему — записи
    текущего прогона, поэтому товар, который первым увидел частичный тик и чья
    цена к проверенному сбору не изменилась, новым не объявляется: записи в
    проверенном сборе у него нет. Так было и до этой правки.
    """
    from sqlalchemy import select

    wanted = set(sites)
    cutoffs = {site: current_run.started_at for site in wanted}
    previous_by_site = latest_financial_run_ids_by_site(
        session,
        wanted,
        tenant_id=current_run.tenant_id,
        before_run_id=current_run.id,
    )
    if previous_by_site:
        started_at_by_run = dict(
            session.execute(
                select(Run.id, Run.started_at).where(Run.id.in_(set(previous_by_site.values())))
            ).all()
        )
        for site, run_id in previous_by_site.items():
            if run_id in started_at_by_run:
                cutoffs[site] = started_at_by_run[run_id]
    return cutoffs


def _latest_snapshot_stmt(
    session,
    entities,
    product_ids,
    *,
    financially_eligible_only: bool,
    tenant_id: int,
    include_run_id: int | None,
):
    """SELECT последнего snapshot'а на товар; `None`, если выбирать нечего.

    Общая часть `latest_snapshots_per_product` (ORM-объекты) и
    `latest_prices_per_product` (только нужные колонки): правило «какой snapshot
    считать текущим» должно жить в одном месте.
    """
    from sqlalchemy import func, select

    product_ids = list(product_ids) if not isinstance(product_ids, list) else product_ids
    if not product_ids:
        return None

    eligible_run_ids: list[int] | None = None
    if financially_eligible_only:
        eligible_run_ids = financially_eligible_run_ids(
            session,
            tenant_id=tenant_id,
            include_run_id=include_run_id,
        )
        if not eligible_run_ids:
            return None

    filters = [PriceSnapshot.product_id.in_(product_ids)]
    if eligible_run_ids is not None:
        filters.append(trusted_snapshot_filter(eligible_run_ids))

    latest_at_subq = (
        select(
            PriceSnapshot.product_id,
            func.max(PriceSnapshot.captured_at).label("max_at"),
        )
        .where(*filters)
        .group_by(PriceSnapshot.product_id)
        .subquery()
    )
    latest_stmt = select(*entities).join(
        latest_at_subq,
        (PriceSnapshot.product_id == latest_at_subq.c.product_id)
        & (PriceSnapshot.captured_at == latest_at_subq.c.max_at),
    )
    if eligible_run_ids is not None:
        latest_stmt = latest_stmt.where(trusted_snapshot_filter(eligible_run_ids))
    return latest_stmt.order_by(PriceSnapshot.product_id, PriceSnapshot.id.desc())


def latest_prices_per_product(
    session,
    product_ids,
    *,
    financially_eligible_only: bool = False,
    tenant_id: int = 1,
) -> dict[int, tuple[float | None, float | None, bool]]:
    """`product_id → (price, discount_price, is_on_sale)` последнего snapshot'а.

    То же правило выбора, что у `latest_snapshots_per_product`, но без сборки
    ORM-объектов: страница сравнения читает цены тысяч товаров на каждый запрос,
    и гидратация `PriceSnapshot` стоила там больше, чем сам SQL.
    """
    latest_stmt = _latest_snapshot_stmt(
        session,
        (
            PriceSnapshot.product_id,
            PriceSnapshot.price,
            PriceSnapshot.discount_price,
            PriceSnapshot.is_on_sale,
        ),
        product_ids,
        financially_eligible_only=financially_eligible_only,
        tenant_id=tenant_id,
        include_run_id=None,
    )
    if latest_stmt is None:
        return {}
    out: dict[int, tuple[float | None, float | None, bool]] = {}
    for product_id, price, discount_price, is_on_sale in session.execute(latest_stmt):
        out.setdefault(product_id, (price, discount_price, bool(is_on_sale)))
    return out


def latest_snapshots_per_product(
    session,
    product_ids,
    *,
    financially_eligible_only: bool = False,
    tenant_id: int = 1,
    include_run_id: int | None = None,
) -> dict[int, PriceSnapshot]:
    """Для каждого product_id из списка → его последний `PriceSnapshot`.

    Работает в обоих режимах persist'а:
    - **FULL** (snapshot пишется каждый прогон): latest = snapshot последнего
      прогона, который видел этот продукт. Эквивалентно WHERE run_id == last_run.
    - **DIFF-ONLY** (snapshot пишется только при изменении цены): latest =
      snapshot прогона, в котором цена реально менялась. Содержит актуальную
      цену независимо от того, сколько прогонов прошло без изменений.

    Использует одну агрегатную SELECT с MAX(captured_at) GROUP BY product_id —
    O(N) PG-операция через btree(product_id) индекс. Безопасна для SSH-туннеля
    при любом размере inputs (∼1000 product_ids = тысячи мелких group-by'ев).

    Дубли по `(product_id, captured_at)` — крайне маловероятны, но
    разрулятся: возвращается первый встреченный.
    """
    latest_stmt = _latest_snapshot_stmt(
        session,
        (PriceSnapshot,),
        product_ids,
        financially_eligible_only=financially_eligible_only,
        tenant_id=tenant_id,
        include_run_id=include_run_id,
    )
    if latest_stmt is None:
        return {}
    snaps = session.scalars(latest_stmt).all()
    out: dict[int, PriceSnapshot] = {}
    for s in snaps:
        out.setdefault(s.product_id, s)
    return out
