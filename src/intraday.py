"""Phase 5.1 (Вариант C) — intraday category rotation.

В отличие от full scheduled scrape, intraday
делает СУПЛЕМЕНТАЛЬНЫЕ прогоны: каждый час business hours (05-17 UTC) выбирает
одну категорию из top-N volatile и скрейпит её на одном сайте round-robin.

Это даёт квази-intraday обновление цен по самым активным категориям без
expensive full rescrape (полный pharmonline = 50 мин, 272k visits).

Design:
- top_volatile_categories(): top-30 категорий с самыми частыми price-changes
  за последние 7 дней (через price_snapshots count). Не зависит от watchlist.
  Для ротации берутся только категории, у которых есть раздел на сайте из
  INTRADAY_SITES: остальные тик обслужить не может.
- Указатель ротации — Redis-ключ `intraday:rotation:idx`. Тик читает его (GET),
  а сдвигает (INCR) только когда прогон действительно взят: пропущенный тик
  очередь категории не съедает.
- acquire_site_lock(): Redis SETNX с TTL 2 часа per site — гарантия что один
  сайт не получит больше одного intraday-прогона каждые 2 часа.

Запуск из systemd:
    pharmacy-monitor intraday-tick
который picks one (site, category) и делает category-mode scrape.

Пропуск тика — штатный исход; причина пишется в лог событием
`intraday_skipped` с полем `reason` (см. SKIP_*). Если Redis unreachable —
тоже пропуск (intraday не critical).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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

# Только Aloe допускается в intraday rotation. Pharmonline намеренно исключён:
# каждый короткий тик открывал новый Meteor WebSocket через residential proxy и
# 5–7 раз в день воспроизводил transient opening-handshake timeout. Его каталог
# обновляет отдельный полный недельный timer с полноценной retry/verification
# семантикой. Aptekonline также остаётся на отдельном полном расписании.
INTRADAY_SITES = ("aloe",)

# Окно для подсчёта volatility (count price changes per category per N days).
VOLATILITY_WINDOW_DAYS = 7

# В каком поле Category лежит раздел сайта. Пустое поле = у категории на этом
# сайте раздела нет, и тик этого сайта её обслужить не может.
_SITE_SLUG_FIELD = {
    "pharmonline": "pharmonline_slug",
    "aptekonline": "aptekonline_slug",
    "aloe": "aloe_slug",
}

_ROTATION_KEY = "intraday:rotation:idx"

# Причины пропуска тика (поле `reason` события `intraday_skipped`).
# Ни у одной категории с разделом на сайте из INTRADAY_SITES не менялись цены.
SKIP_NO_SERVABLE_CATEGORY = "no_servable_category"
# Категория выбрана, но сайт недавно уже получил intraday-прогон.
SKIP_SITE_RATE_LIMITED = "site_rate_limited"
# Указатель ротации прочитать не удалось (Redis недоступен или не настроен).
SKIP_ROTATION_UNAVAILABLE = "rotation_state_unavailable"


@dataclass(frozen=True)
class TickDecision:
    """Что делать тику: либо цель, либо причина пропуска.

    `skip_detail` — та же причина словами, для вывода команды в journald.
    """

    target: tuple[str, storage.Category] | None = None
    skip_reason: str | None = None
    skip_detail: str = ""


def servable_sites(cat: storage.Category, sites: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """Сайты тика, на которых у категории есть раздел (в порядке `sites`)."""
    if sites is None:
        sites = INTRADAY_SITES
    return tuple(s for s in sites if s in _SITE_SLUG_FIELD and getattr(cat, _SITE_SLUG_FIELD[s]))


# ─── Volatility scoring ───────────────────────────────────────────────────────


def top_volatile_categories(
    session: Session,
    n: int = INTRADAY_TOP_N_CATEGORIES,
    *,
    sites: tuple[str, ...] | None = None,
) -> list[storage.Category]:
    """Top-N категорий с наибольшей price-change активностью за окно.

    `sites` — оставить только категории, у которых есть раздел хотя бы на одном
    из этих сайтов. Отбор идёт ДО среза top-N: иначе N мест занимают категории
    сайтов, которые тик не обслуживает, и ротация крутится вхолостую (прод,
    2026-10-05…07: после полных сборов aptekonline и pharmonline 24–26 мест из
    30 достались их категориям, 29 тиков из 32 вышли пропуском).

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
        if sites is not None and not servable_sites(cat, sites):
            continue
        slugs = {cat.pharmonline_slug, cat.aptekonline_slug, cat.aloe_slug}
        # Берём max score среди slugs этой категории (на случай если slugs разные
        # на разных сайтах, но категория концептуально одна).
        scores = [slug_to_score.get(s, 0) for s in slugs if s]
        if scores and max(scores) > 0:
            cat_score[cat.id] = max(scores)

    if not cat_score:
        return []

    # Top-N по убыванию score. При равном score — по id: порядок строк из БД не
    # гарантирован, а ротация ходит по этому списку по индексу.
    top_ids = sorted(cat_score.keys(), key=lambda cid: (-cat_score[cid], cid))[:n]

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


def _peek_rotation_pick(
    redis_client: Any, categories: list[storage.Category]
) -> storage.Category | None:
    """Чья очередь: categories[idx % len], БЕЗ сдвига указателя.

    None если список пустой или Redis недоступен. Ключа ещё нет → idx 0, то
    есть первая категория списка.
    """
    if not categories or redis_client is None:
        return None
    try:
        raw = redis_client.get(_ROTATION_KEY)
        return categories[(int(raw) if raw else 0) % len(categories)]
    except Exception as e:  # noqa: BLE001
        log.warning("intraday_redis_peek_failed", error=str(e))
        return None


def _advance_rotation(redis_client: Any) -> None:
    """Передать очередь следующей категории (atomic INCR).

    Зовётся только когда тик взял прогон. TTL 90 дней — чтоб ключ не висел
    вечно если intraday отключат. Сбой Redis здесь прогон не отменяет: та же
    категория просто получит ещё один тик.
    """
    if redis_client is None:
        return
    try:
        redis_client.incr(_ROTATION_KEY)
        redis_client.expire(_ROTATION_KEY, 90 * 24 * 3600)
    except Exception as e:  # noqa: BLE001
        log.warning("intraday_redis_incr_failed", error=str(e))


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


def _skip(reason: str, detail: str, **fields: Any) -> TickDecision:
    log.info("intraday_skipped", reason=reason, **fields)
    return TickDecision(skip_reason=reason, skip_detail=detail)


def pick_next_scrape_target(
    session: Session,
    redis_client: Any | None = None,
    *,
    commit_state: bool = True,
) -> TickDecision:
    """Выбрать (site, category) для следующего intraday-прогона.

    Args:
        session: SQLAlchemy session для DB queries.
        redis_client: Redis client (auto-lookup из REDIS_URL если None).
        commit_state: True (default) → SETNX lock + INCR rotation index.
            False → read-only preview: видим что выбрали бы, но не меняем
            Redis state. Используется в `intraday-tick --dry-run`.

    Логика:
    1. Top-N volatile категорий — только тех, что тик может обслужить (есть
       раздел на сайте из INTRADAY_SITES).
    2. Читаем указатель ротации: чья очередь.
    3. Среди сайтов категории берём первый, у которого lock can be acquired
       (последний прогон > 2h назад).
    4. Прогон взят → сдвигаем указатель. Не взят → указатель на месте, та же
       категория получит следующий тик.

    Returns: TickDecision с `target` либо с причиной пропуска.
    """
    if redis_client is None:
        redis_client = _redis_client()

    sites_label = ",".join(INTRADAY_SITES)
    cats = top_volatile_categories(session, sites=INTRADAY_SITES)
    if not cats:
        return _skip(
            SKIP_NO_SERVABLE_CATEGORY,
            f"no category with a section on {sites_label} had price changes "
            f"in the last {VOLATILITY_WINDOW_DAYS} days",
            sites=sites_label,
            window_days=VOLATILITY_WINDOW_DAYS,
        )

    cat = _peek_rotation_pick(redis_client, cats)
    if cat is None:
        return _skip(
            SKIP_ROTATION_UNAVAILABLE,
            "rotation state unavailable (Redis unreachable or REDIS_URL not set)",
        )

    longest_wait = 0
    for site in servable_sites(cat):
        if commit_state:
            acquired = acquire_site_lock(redis_client, site)
        else:
            # Preview: проверяем lock без SETNX.
            acquired = _peek_site_lock_free(redis_client, site)
        if not acquired:
            ttl = time_until_lock_expires(redis_client, site) or 0
            longest_wait = max(longest_wait, ttl)
            log.info(
                "intraday_site_locked",
                site=site,
                ttl_sec=ttl,
                category_id=cat.id,
                category_key=cat.key,
            )
            continue
        # Lock acquired — этот сайт берёт прогон, очередь переходит дальше.
        if commit_state:
            _advance_rotation(redis_client)
        log.info(
            "intraday_picked",
            site=site,
            category_id=cat.id,
            category_key=cat.key,
            rotation_size=len(cats),
            dry_run=not commit_state,
        )
        return TickDecision(target=(site, cat))

    cat_sites = ",".join(servable_sites(cat))
    return _skip(
        SKIP_SITE_RATE_LIMITED,
        f"{cat_sites} already had an intraday run within the last "
        f"{INTRADAY_PER_SITE_MIN_GAP_SEC // 3600}h (free in {longest_wait}s); "
        f"category {cat.key} keeps its turn",
        sites=cat_sites,
        ttl_sec=longest_wait,
        category_id=cat.id,
        category_key=cat.key,
    )


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
