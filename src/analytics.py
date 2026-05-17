"""Аналитические агрегаты — бренды, промо-история, ассортимент-overlap.

Используется на вкладке 📈 Аналитика дашборда + в weekly email.
Все функции берут данные из последнего успешного прогона.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from src._time import utcnow

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from src.storage import (
    Match,
    PriceSnapshot,
    Product,
    Promo,
    Run,
)

log = structlog.get_logger()

CLIENT_SITE = "pharmonline"
COMPETITOR_SITES = ("aptekonline", "aloe")
ALL_SITES = (CLIENT_SITE, *COMPETITOR_SITES)


def _competitors_for(client_site: str) -> tuple[str, ...]:
    return tuple(s for s in ALL_SITES if s != client_site)


@dataclass
class BrandRow:
    brand: str
    counts: dict[str, int]  # site → product count
    total: int
    sites_with_brand: int
    exclusive_to: str | None  # site если бренд только у одного, иначе None


def brand_share(
    session: Session,
    *,
    run_id: int | None = None,
    top_n: int = 30,
    site: str | None = None,
) -> list[BrandRow]:
    """Сводка: каких брендов сколько на каждом сайте.

    Возвращает список отсортированный по `total` desc, ограниченный top_n.
    Брэнды с пустым именем игнорируются.

    Если передан `site` — возвращает только бренды, представленные на этом
    сайте; counts остаются по всем сайтам (чтобы видеть exclusive-to флаг).

    Diff-only-aware (2026-05-17 fix): считаем по всем существующим продуктам
    в БД (не привязка к `run_id`), потому что после diff-only persist + failed
    aloe-scrape запрос `WHERE run_id == last_ok_run` возвращал 0 строк (когда
    last_ok_run = run где этот сайт не участвовал). Параметр `run_id` оставлен
    для backward-compat, но игнорируется когда `site` указан.
    """
    # Когда явно фильтруем по сайту — считаем по `Product` напрямую
    # (избегаем зависимости от run_id который может быть свежий-но-пустой).
    if site is not None:
        rows = session.execute(
            select(Product.brand, Product.site, func.count(Product.id))
            .where(Product.brand.is_not(None))
            .group_by(Product.brand, Product.site)
        ).all()
    else:
        # Legacy path (без site фильтра) — оставлен совместимым с тестами:
        # использует snapshot последнего ok-run если есть, иначе fallback на all products
        if run_id is None:
            run_id = session.scalar(
                select(Run.id).where(Run.status == "ok").order_by(desc(Run.id)).limit(1)
            )
        if run_id is None:
            rows = session.execute(
                select(Product.brand, Product.site, func.count(Product.id))
                .where(Product.brand.is_not(None))
                .group_by(Product.brand, Product.site)
            ).all()
        else:
            rows = session.execute(
                select(Product.brand, Product.site, func.count(Product.id))
                .join(PriceSnapshot, PriceSnapshot.product_id == Product.id)
                .where(PriceSnapshot.run_id == run_id, Product.brand.is_not(None))
                .group_by(Product.brand, Product.site)
            ).all()

    # Aggregate
    by_brand: dict[str, dict[str, int]] = defaultdict(lambda: {s: 0 for s in ALL_SITES})
    for brand, site_name, n in rows:
        if not brand:
            continue
        by_brand[brand][site_name] = n

    out: list[BrandRow] = []
    for brand, counts in by_brand.items():
        total = sum(counts.values())
        sites_with = sum(1 for v in counts.values() if v > 0)
        exclusive = None
        if sites_with == 1:
            exclusive = next(s for s, v in counts.items() if v > 0)
        if site is not None and counts.get(site, 0) == 0:
            continue
        out.append(BrandRow(
            brand=brand, counts=dict(counts), total=total,
            sites_with_brand=sites_with, exclusive_to=exclusive,
        ))
    out.sort(key=lambda b: -b.total)
    return out[:top_n]


@dataclass
class PromoStats:
    site: str
    title: str
    landing_url: str | None
    first_seen: datetime
    last_seen: datetime
    days_active: int


def promo_history(
    session: Session, *, days: int = 30
) -> list[PromoStats]:
    """История промо-кампаний за последние N дней.

    Группирует по (site, title), вычисляет first/last seen и длительность.
    """
    cutoff = utcnow() - timedelta(days=days)
    promos = session.scalars(
        select(Promo).where(Promo.captured_at >= cutoff)
    ).all()

    grouped: dict[tuple[str, str], list[Promo]] = defaultdict(list)
    for p in promos:
        grouped[(p.site, p.title)].append(p)

    out: list[PromoStats] = []
    for (site, title), items in grouped.items():
        items.sort(key=lambda x: x.captured_at)
        first = items[0].captured_at
        last = items[-1].captured_at
        days_active = max(1, (last - first).days + 1)
        out.append(PromoStats(
            site=site, title=title,
            landing_url=items[0].landing_url,
            first_seen=first, last_seen=last,
            days_active=days_active,
        ))
    out.sort(key=lambda p: -p.days_active)
    return out


@dataclass
class AssortmentOverlap:
    matched_count: int
    only_client: int  # сматченных нет на других сайтах
    only_competitor_count: dict[str, int]  # site → unmatched count
    coverage_pct: float  # сколько % товаров клиента есть хотя бы у 1 конкурента


def assortment_overlap(session: Session) -> AssortmentOverlap:
    """Анализ перекрытия ассортимента: что есть у всех, что эксклюзивно."""
    run_id = session.scalar(
        select(Run.id).where(Run.status == "ok").order_by(desc(Run.id)).limit(1)
    )
    if run_id is None:
        return AssortmentOverlap(
            matched_count=0, only_client=0,
            only_competitor_count={s: 0 for s in COMPETITOR_SITES},
            coverage_pct=0.0,
        )

    # Сматченные кластеры
    matches = session.scalars(select(Match)).all()
    matched_count = len(matches)

    # Клиентские товары без canonical_id (эксклюзив клиента)
    only_client = session.scalar(
        select(func.count(Product.id))
        .join(PriceSnapshot, PriceSnapshot.product_id == Product.id)
        .where(
            PriceSnapshot.run_id == run_id,
            Product.site == CLIENT_SITE,
            Product.canonical_id.is_(None),
        )
    ) or 0

    # Эксклюзив каждого конкурента
    only_comp: dict[str, int] = {}
    for site in COMPETITOR_SITES:
        only_comp[site] = session.scalar(
            select(func.count(Product.id))
            .join(PriceSnapshot, PriceSnapshot.product_id == Product.id)
            .where(
                PriceSnapshot.run_id == run_id,
                Product.site == site,
                Product.canonical_id.is_(None),
            )
        ) or 0

    # Coverage: client matched / total client
    total_client = session.scalar(
        select(func.count(Product.id))
        .join(PriceSnapshot, PriceSnapshot.product_id == Product.id)
        .where(PriceSnapshot.run_id == run_id, Product.site == CLIENT_SITE)
    ) or 0
    matched_client = total_client - only_client
    coverage_pct = (matched_client / total_client * 100) if total_client else 0.0

    return AssortmentOverlap(
        matched_count=matched_count,
        only_client=only_client,
        only_competitor_count=only_comp,
        coverage_pct=round(coverage_pct, 1),
    )


@dataclass
class PriceIndex:
    """Ценовой индекс клиента vs конкурентов по категории."""
    category: str
    avg_client_price: float
    avg_competitor_price: float
    index: float  # 100 = paritet, <100 = клиент дешевле, >100 = клиент дороже
    matched_skus: int


@dataclass
class MatchQuality:
    """Метрика качества автоматического матчинга."""

    total_matches: int
    auto_matches: int
    manual_matches: int  # is_manual=True
    rejected_pairs: int
    products_total: int
    products_matched: int
    coverage_pct: float
    manual_pct: float


def match_quality(session: Session) -> MatchQuality:
    """Сводка качества матчинга — для отображения в дашборде."""
    from src.storage import Match, MatchRejection

    total_matches = session.scalar(select(func.count(Match.id))) or 0
    manual_matches = session.scalar(
        select(func.count(Match.id)).where(Match.is_manual.is_(True))
    ) or 0
    auto_matches = total_matches - manual_matches
    rejected = session.scalar(select(func.count(MatchRejection.id))) or 0

    products_total = session.scalar(select(func.count(Product.id))) or 0
    products_matched = session.scalar(
        select(func.count(Product.id)).where(Product.canonical_id.is_not(None))
    ) or 0
    coverage = (products_matched / products_total * 100) if products_total else 0.0
    manual_pct = (manual_matches / total_matches * 100) if total_matches else 0.0

    return MatchQuality(
        total_matches=total_matches,
        auto_matches=auto_matches,
        manual_matches=manual_matches,
        rejected_pairs=rejected,
        products_total=products_total,
        products_matched=products_matched,
        coverage_pct=round(coverage, 1),
        manual_pct=round(manual_pct, 1),
    )


def price_index_by_category(
    session: Session,
    *,
    client_site: str = CLIENT_SITE,
) -> list[PriceIndex]:
    """Для каждой категории — среднее по клиенту vs конкурентам.

    Diff-only-aware: используем `latest_snapshots_per_product` вместо
    snapshot'ов одного run_id. После 2026-05-09 snapshot пишется только
    при изменении цены, поэтому фильтр `WHERE run_id == last_run` пропустил
    бы продукты со стабильной ценой — и для бутиковых сайтов (aloe) это
    давало пустой результат.
    """
    from src.storage import latest_snapshots_per_product

    competitor_sites = _competitors_for(client_site)
    matches = session.scalars(select(Match)).all()
    if not matches:
        return []

    all_pids = [p.id for m in matches for p in m.products]
    snaps_by_pid = latest_snapshots_per_product(session, all_pids)

    by_cat: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"client": [], "competitor": []}
    )

    for m in matches:
        client_p = next((p for p in m.products if p.site == client_site), None)
        if not client_p:
            continue
        client_snap = snaps_by_pid.get(client_p.id)
        if client_snap is None:
            continue
        client_price = client_snap.discount_price or client_snap.price
        if client_price is None:
            continue

        comp_prices: list[float] = []
        for p in m.products:
            if p.site == client_site or p.site not in competitor_sites:
                continue
            snap = snaps_by_pid.get(p.id)
            if snap is None:
                continue
            ep = snap.discount_price or snap.price
            if ep is not None:
                comp_prices.append(ep)
        if not comp_prices:
            continue

        cat = client_p.category or "(без категории)"
        by_cat[cat]["client"].append(client_price)
        by_cat[cat]["competitor"].append(sum(comp_prices) / len(comp_prices))

    out: list[PriceIndex] = []
    for cat, prices in by_cat.items():
        if not prices["client"]:
            continue
        avg_c = sum(prices["client"]) / len(prices["client"])
        avg_comp = sum(prices["competitor"]) / len(prices["competitor"])
        idx = (avg_c / avg_comp * 100) if avg_comp else 100.0
        out.append(PriceIndex(
            category=cat,
            avg_client_price=round(avg_c, 2),
            avg_competitor_price=round(avg_comp, 2),
            index=round(idx, 1),
            matched_skus=len(prices["client"]),
        ))
    out.sort(key=lambda x: -x.matched_skus)
    return out
