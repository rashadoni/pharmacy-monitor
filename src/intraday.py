"""Phase 5.1 (Вариант C) — intraday category rotation.

В отличие от full nightly scrape (01:00 UTC pharmonline, 03:00 aloe), intraday
делает СУПЛЕМЕНТАЛЬНЫЕ прогоны: каждый час business hours (05-17 UTC) выбирает
одну категорию из top-N volatile и скрейпит её на одном сайте round-robin.

Это даёт квази-intraday обновление цен по самым активным категориям без
expensive full rescrape (полный pharmonline = 50 мин, 272k visits).

Design:
- top_volatile_categories(): top-30 категорий с самыми частыми price-changes
  за последние 7 дней (через price_snapshots count). Не зависит от watchlist.
- next_rotation_pick(): Redis INCR на ключ `intraday:rotation:idx`, возвращает
  i % len(categories) — atomic, безопасно при race condition.
- rate_limit_per_site(): Redis SETNX с TTL 2 часа per site — гарантия что один
  сайт не получит больше одного intraday-прогона каждые 2 часа.

Запуск из systemd:
    pharmacy-monitor intraday-tick
который picks one (site, category) и делает category-mode scrape.

Если Redis unreachable — silent no-op (intraday не critical).
"""
from __future__ import annotations

import os
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src import storage

log = structlog.get_logger(__name__)


# ─── Configuration ────────────────────────────────────────────────────────────

# Сколько top-volatile категорий брать в rotation set.
INTRADAY_TOP_N_CATEGORIES = 30

# Мин промежуток между intraday-прогонами на ОДНОМ сайте (Redis TTL).
INTRADAY_PER_SITE_MIN_GAP_SEC = 2 * 3600  # 2 часа

# Какие сайты допустимы в intraday rotation. aptekonline-on-prod забанен,
# Mac launchd только раз/день — поэтому исключён. aloe и pharmonline — OK.
INTRADAY_SITES = ("pharmonline", "aloe")

# Окно для подсчёта volatility (count price changes per category per N days).
VOLATILITY_WINDOW_DAYS = 7


# ─── Volatility scoring ───────────────────────────────────────────────────────


def top_volatile_categories(
    session: Session, n: int = INTRADAY_TOP_N_CATEGORIES
) -> list[storage.Category]:
    """Top-N категорий с наибольшей price-change активностью за окно.

    Volatility = count(price_snapshots) per category за `VOLATILITY_WINDOW_DAYS` дней.
    После diff-only persist snapshots пишутся только при реальном изменении цены,
    поэтому count(snapshots) — точный proxy для "сколько раз цена менялась".

    Возвращает Category list, отсортированный по volatility desc. Только active
    категории. Cap N=30 по умолчанию.

    Семантика: используем `Product.category` (slug-строка) для join, так как
    snapshots ↔ products ↔ category — это естественный путь. Мэп Product.category
    на Category.key через slug-поля (pharmonline_slug / aptekonline_slug / aloe_slug).
    Категории без матча product.category пропускаются.
    """
    from datetime import timedelta

    from src._time import utcnow

    cutoff = utcnow() - timedelta(days=VOLATILITY_WINDOW_DAYS)

    # Подсчёт snapshots per (site, product.category) за окно
    rows = session.execute(
        select(
            storage.Product.site,
            storage.Product.category,
            func.count(storage.PriceSnapshot.id).label("changes"),
        )
        .join(
            storage.PriceSnapshot,
            storage.PriceSnapshot.product_id == storage.Product.id,
        )
        .where(
            storage.PriceSnapshot.captured_at >= cutoff,
            storage.Product.category.is_not(None),
        )
        .group_by(storage.Product.site, storage.Product.category)
    ).all()

    # Свёртка по slug — суммируем counts через все сайты (если slug одинаков).
    slug_to_score: dict[str, int] = {}
    for site, slug, n_changes in rows:
        slug_to_score[slug] = slug_to_score.get(slug, 0) + int(n_changes)

    if not slug_to_score:
        log.warning("intraday_no_volatility_data", window_days=VOLATILITY_WINDOW_DAYS)
        return []

    # Резолвим slug → Category. Category.{site}_slug может быть == slug.
    all_cats = session.scalars(
        select(storage.Category).where(storage.Category.is_active.is_(True))
    ).all()

    cat_score: dict[int, int] = {}  # category.id → volatility score
    for cat in all_cats:
        slugs = {cat.pharmonline_slug, cat.aptekonline_slug, cat.aloe_slug}
        # Берём max score среди slugs этой категории (на случай если slugs разные
        # на разных сайтах, но категория концептуально одна).
        scores = [slug_to_score.get(s, 0) for s in slugs if s]
        if scores and max(scores) > 0:
            cat_score[cat.id] = max(scores)

    if not cat_score:
        return []

    # Top-N по убыванию score.
    top_ids = sorted(cat_score.keys(), key=lambda cid: -cat_score[cid])[:n]

    # Возвращаем в том же порядке.
    by_id = {c.id: c for c in all_cats}
    return [by_id[cid] for cid in top_ids if cid in by_id]


# ─── Redis-backed rotation index ──────────────────────────────────────────────


def _redis_client() -> Any | None:
    """Lazy Redis client. None если REDIS_URL не задан или Redis недоступен."""
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis as _redis

        client = _redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()  # eager validation
        return client
    except Exception as e:  # noqa: BLE001
        log.warning("intraday_redis_unreachable", error=str(e))
        return None


def next_rotation_pick(
    redis_client: Any, categories: list[storage.Category]
) -> storage.Category | None:
    """Atomic Redis INCR → возвращаем categories[i % len].

    None если список пустой или Redis недоступен. INCR создаёт ключ при первом
    вызове (стартовое значение 1) — то есть первая итерация возьмёт index 0.

    Wraparound автоматически через модуль. Ключ TTL = 90 дней (perm-style),
    обновляется на каждом INCR.
    """
    if not categories or redis_client is None:
        return None
    try:
        idx = int(redis_client.incr("intraday:rotation:idx"))
        # TTL 90 дней — чтоб ключ не висел вечно если intraday отключат.
        redis_client.expire("intraday:rotation:idx", 90 * 24 * 3600)
        return categories[(idx - 1) % len(categories)]
    except Exception as e:  # noqa: BLE001
        log.warning("intraday_redis_incr_failed", error=str(e))
        return None


# ─── Per-site rate-limit ──────────────────────────────────────────────────────


def acquire_site_lock(redis_client: Any, site: str) -> bool:
    """Atomic Redis SETNX — пытается взять lock на `site` с TTL.

    True если lock взят (можно запускать scrape). False если другой intraday-tick
    уже запустился на этом сайте в течение последних `INTRADAY_PER_SITE_MIN_GAP_SEC`
    секунд.

    None redis_client → возвращаем True (best-effort, без rate-limit).
    """
    if redis_client is None:
        return True
    try:
        key = f"intraday:lock:site:{site}"
        # SET NX EX — atomic create-if-not-exists с TTL.
        acquired = redis_client.set(key, "1", nx=True, ex=INTRADAY_PER_SITE_MIN_GAP_SEC)
        return bool(acquired)
    except Exception as e:  # noqa: BLE001
        log.warning("intraday_lock_failed", site=site, error=str(e))
        return True  # fail-open — лучше двойной прогон чем стоять


def time_until_lock_expires(redis_client: Any, site: str) -> int | None:
    """TTL до истечения lock'а на site (секунды), None если lock'а нет."""
    if redis_client is None:
        return None
    try:
        ttl = redis_client.ttl(f"intraday:lock:site:{site}")
        return int(ttl) if ttl > 0 else None
    except Exception:  # noqa: BLE001
        return None


# ─── Orchestration ────────────────────────────────────────────────────────────


def pick_next_scrape_target(
    session: Session,
    redis_client: Any | None = None,
    *,
    commit_state: bool = True,
) -> tuple[str, storage.Category] | None:
    """Выбрать (site, category) для следующего intraday-прогона.

    Args:
        session: SQLAlchemy session для DB queries.
        redis_client: Redis client (auto-lookup из REDIS_URL если None).
        commit_state: True (default) → INCR rotation index + SETNX lock.
            False → read-only preview: видим что выбрали бы, но не меняем
            Redis state. Используется в `intraday-tick --dry-run`.

    Логика:
    1. Получаем top-N volatile категории.
    2. (commit_state=True) Через Redis INCR берём следующую в rotation.
       (commit_state=False) Читаем текущий idx через GET (+1 если не задан).
    3. Среди INTRADAY_SITES берём первый, у которого:
       - есть slug для этой category
       - lock can be acquired (последний прогон > 2h назад)
    4. Если ни один не подходит → возвращаем None (skip tick).

    Returns: (site, category) или None.
    """
    if redis_client is None:
        redis_client = _redis_client()

    cats = top_volatile_categories(session)
    if not cats:
        log.info("intraday_skipped_no_categories")
        return None

    if commit_state:
        cat = next_rotation_pick(redis_client, cats)
    else:
        # Preview: peek текущий index без INCR.
        cat = _peek_rotation_pick(redis_client, cats)
    if cat is None:
        return None

    # Какие сайты доступны для этой категории?
    site_to_slug = {
        "pharmonline": cat.pharmonline_slug,
        "aloe": cat.aloe_slug,
        # aptekonline исключён — не intraday-able с прода (Mac launchd only).
    }

    for site in INTRADAY_SITES:
        if not site_to_slug.get(site):
            continue
        if commit_state:
            acquired = acquire_site_lock(redis_client, site)
        else:
            # Preview: проверяем lock без SETNX.
            acquired = _peek_site_lock_free(redis_client, site)
        if not acquired:
            ttl = time_until_lock_expires(redis_client, site) or 0
            log.info(
                "intraday_site_locked",
                site=site,
                ttl_sec=ttl,
                category_id=cat.id,
                category_key=cat.key,
            )
            continue
        # Lock acquired — этот сайт берёт прогон.
        log.info(
            "intraday_picked",
            site=site,
            category_id=cat.id,
            category_key=cat.key,
            dry_run=not commit_state,
        )
        return (site, cat)

    log.info(
        "intraday_all_sites_locked",
        category_id=cat.id,
        category_key=cat.key,
    )
    return None


def _peek_rotation_pick(
    redis_client: Any, categories: list[storage.Category]
) -> storage.Category | None:
    """Read-only вариант next_rotation_pick: возвращает что взяли бы следующим
    БЕЗ инкремента. Используется в dry-run mode."""
    if not categories or redis_client is None:
        return None
    try:
        raw = redis_client.get("intraday:rotation:idx")
        idx = (int(raw) if raw else 0) + 1
        return categories[(idx - 1) % len(categories)]
    except Exception as e:  # noqa: BLE001
        log.warning("intraday_redis_peek_failed", error=str(e))
        return None


def _peek_site_lock_free(redis_client: Any, site: str) -> bool:
    """Read-only check: True если lock на site СВОБОДЕН (никаких SET).

    Mirrors acquire_site_lock semantics:
      - Redis недоступен → True (fail-open)
      - Redis exception → True (fail-open)
      - Key exists → False (locked)
      - Key absent → True (would be acquired in commit mode)
    """
    if redis_client is None:
        return True
    try:
        exists = redis_client.exists(f"intraday:lock:site:{site}")
        return not bool(exists)
    except Exception:  # noqa: BLE001
        return True
