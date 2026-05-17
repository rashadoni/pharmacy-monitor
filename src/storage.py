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
from datetime import datetime
from src._time import utcnow
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
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


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running/ok/failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    products_scraped: Mapped[int] = mapped_column(Integer, default=0)
    sites_completed: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Per-site breakdown {site: count}. Nullable для backward-compat со старыми
    # runs (до 2026-05-11). Используется smoke_test'ом для точной per-site
    # baseline в multi-site прогонах (Mac launchd pharmonline+aptekonline).
    products_per_site: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Per-(site,category) breakdown {site: {category_label: count}}. Заполняется
    # после persist'а: показывает сколько товаров скрейпер увидел по каждому
    # category-маршруту на каждом сайте. Используется в UI «Coverage» панели и
    # для debug — клиент видит «pharm scraped 100 vitamins, apt scraped 320».
    products_per_site_category: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    snapshots: Mapped[list["PriceSnapshot"]] = relationship(back_populates="run")


class ScrapeRequest(Base):
    """Очередь scrape-запросов, запущенных пользователем через UI.

    Клиент жмёт «▶️ Запустить scan сейчас» → backend создаёт row здесь со
    status='pending'. Mac launchd-watcher (плистом com.pharmacy-monitor.watch,
    тикает каждые 60 секунд) polls API на pending → если есть, исполняет
    `pharmacy-monitor run ...` через SSH-tunnel → PATCH запись status='ok'+run_id.

    Зачем не выполнять сразу на проде: pharmonline+aptekonline бенят Hetzner-IP,
    реально scrape идёт только с Mac (Baku-IP). Поэтому очередь.
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
    # pending → running → ok/failed
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    category: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    dosage: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 500mg, 10ml...
    pack_size: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 30 tab, 100ml...
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    name_normalized: Mapped[str] = mapped_column(String(500), index=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    canonical_id: Mapped[int | None] = mapped_column(
        ForeignKey("matches.id"), nullable=True, index=True
    )

    # AI-normalized pharmaceutical attributes (active_ingredient, dosage_mg,
    # pack_count, form, brand_canonical, confidence). Populated by ai_normalize
    # module after persist. Matcher uses these for structured-attr matching
    # instead of fuzzy name comparison. Null → matcher falls back to legacy
    # fuzzy path.
    normalized_attrs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    normalized_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    # sha256 of (site, name, brand, dosage, pack_size) — cache key. If unchanged
    # between runs, ai_normalize skips the LLM call entirely.
    normalize_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    snapshots: Mapped[list[PriceSnapshot]] = relationship(back_populates="product")
    canonical: Mapped[Match | None] = relationship(back_populates="products")


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

    run: Mapped[Run] = relationship(back_populates="snapshots")
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
    # Какая стратегия дала match — для аудита и подсчёта lift'а:
    # 'ai_attrs_strict' (active_ingredient + dosage_mg + pack_count + brand)
    # 'ai_attrs_partial' (active_ingredient + 1-2 поля)
    # 'legacy_fuzzy' (старый rapidfuzz token_set_ratio)
    # 'manual' (пользователь подтвердил через UI)
    match_strategy: Mapped[str | None] = mapped_column(String(30), nullable=True)
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
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(200), index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="admin")  # admin/viewer
    # Per-user password hash (2026-05-11). Если NULL — auth.login использует
    # global ADMIN_PASSWORD_HASH из /etc/pharmacy-monitor/env (bootstrap mode).
    # Когда пользователь меняет пароль через UI — пишется сюда, и далее auth
    # сначала проверяет DB-hash, фолбэк на env только если DB-hash NULL.
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    magic_token: Mapped[str | None] = mapped_column(String(100), nullable=True)
    magic_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
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
    __table_args__ = (
        UniqueConstraint("product_a_id", "product_b_id", name="uq_rejection_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_a_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    product_b_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)


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
    telegram_chat_id: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True
    )
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
    # Типы: undercut_threshold, price_drop_pct, new_product, promo_started,
    #       price_raise_opportunity
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    # channels — список: ["email", "telegram"]
    channels: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=lambda: ["email"]
    )
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
    snoozed_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )


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
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, index=True
    )
    # Какой `Run.id` сгенерил кэш — для отладки «откуда устаревшие цифры»
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "client_site", name="uq_roi_cache_tenant_site"),
    )


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


class TrackedProductLink(Base):
    """Привязка watchlist-записи к конкретной странице на конкретном сайте.

    Workflow:
    1. Клиент добавляет TrackedProduct с canonical_name + (опц.) фиксированными URL
    2. На первом прогоне scraper либо использует pinned URL, либо ищет через site search
    3. Найденный URL пинуется (status=confirmed) и в дальнейшем переиспользуется
    """

    __tablename__ = "tracked_product_links"
    __table_args__ = (
        UniqueConstraint("tracked_product_id", "site", name="uq_tracked_site"),
    )

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
            # 2026-05-11: per-site breakdown для smoke_test (multi-site Mac runs)
            ("runs", "products_per_site", "JSON"),
            # 2026-05-11 (вечер): per-(site,category) breakdown для UI coverage panel
            ("runs", "products_per_site_category", "JSON"),
            # 2026-05-11 (ночь): password в DB (вместо global env-hash)
            ("tenant_users", "password_hash", "VARCHAR(200)"),
            # 2026-05-16: AI-normalized pharmaceutical attributes
            ("products", "normalized_attrs", "JSON"),
            ("products", "normalized_at", "DATETIME"),
            ("products", "normalize_hash", "VARCHAR(64)"),
            # 2026-05-16: match strategy для аудита/lift метрик
            ("matches", "match_strategy", "VARCHAR(30)"),
        ]
        for table, column, coltype in migrations:
            try:
                cols = conn.execute(
                    text(f"PRAGMA table_info({table})")
                ).all()
                if not cols:
                    continue  # таблицы ещё нет (свежая БД — create_all создаст)
                existing = {c[1] for c in cols}
                if column not in existing:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                    )
            except Exception:
                pass  # не валим init-db


def make_session(database_url: str | None = None):
    engine = make_engine(database_url)
    return sessionmaker(engine, expire_on_commit=False, autoflush=False)


# ============================================================================
# Query helpers — общие для api / analyzer / alerts / roi / health / forecast
# ============================================================================


def curr_and_prev_snapshots_for_run(
    session, current_run: "Run"
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
    prev_max_subq = (
        select(
            PriceSnapshot.product_id,
            func.max(PriceSnapshot.captured_at).label("max_at"),
        )
        .join(Run, Run.id == PriceSnapshot.run_id)
        .where(
            PriceSnapshot.product_id.in_(product_ids),
            Run.started_at < current_run.started_at,
        )
        .group_by(PriceSnapshot.product_id)
        .subquery()
    )
    prev_snaps = session.scalars(
        select(PriceSnapshot).join(
            prev_max_subq,
            (PriceSnapshot.product_id == prev_max_subq.c.product_id)
            & (PriceSnapshot.captured_at == prev_max_subq.c.max_at),
        )
    ).all()
    prev_by_product: dict[int, PriceSnapshot] = {}
    for s in prev_snaps:
        prev_by_product.setdefault(s.product_id, s)
    return curr_snaps, prev_by_product


def latest_snapshots_per_product(
    session, product_ids
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
    from sqlalchemy import func, select

    product_ids = list(product_ids) if not isinstance(product_ids, list) else product_ids
    if not product_ids:
        return {}

    latest_at_subq = (
        select(
            PriceSnapshot.product_id,
            func.max(PriceSnapshot.captured_at).label("max_at"),
        )
        .where(PriceSnapshot.product_id.in_(product_ids))
        .group_by(PriceSnapshot.product_id)
        .subquery()
    )
    snaps = session.scalars(
        select(PriceSnapshot).join(
            latest_at_subq,
            (PriceSnapshot.product_id == latest_at_subq.c.product_id)
            & (PriceSnapshot.captured_at == latest_at_subq.c.max_at),
        )
    ).all()
    out: dict[int, PriceSnapshot] = {}
    for s in snaps:
        out.setdefault(s.product_id, s)
    return out
