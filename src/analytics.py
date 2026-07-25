"""Аналитические агрегаты — бренды, промо-история, ассортимент-overlap.

Используется на вкладке 📈 Аналитика дашборда + в weekly email.
Все функции берут данные из последнего успешного прогона.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from src._time import utcnow

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, selectinload

from src.storage import (
    Category,
    Match,
    PriceSnapshot,
    Product,
    Promo,
    Run,
    financially_eligible_run_ids,
    latest_snapshots_per_product,
)

log = structlog.get_logger()

CLIENT_SITE = "pharmonline"
COMPETITOR_SITES = ("aptekonline", "aloe")
ALL_SITES = (CLIENT_SITE, *COMPETITOR_SITES)

# Окно «активного» товара для brand_share под diff-only persist. 14 дней —
# щедро покрывает суточный цикл скрейпа + переживает пропущенный прогон, но
# исключает давно-снятые SKU.
_BRAND_SHARE_ACTIVE_DAYS = 14


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
    tenant_id: int = 1,
) -> list[BrandRow]:
    """Сводка: каких брендов сколько на каждом сайте.

    Возвращает список отсортированный по `total` desc, ограниченный top_n.
    Брэнды с пустым именем игнорируются.

    Если `site` указан — оставляем только бренды представленные на этом сайте
    (для site-specific dashboard view). Counts всё равно показывают разбивку
    по всем сайтам — нужно для понимания exclusivity.

    Diff-only fix (2026-05-29): раньше JOIN'илось на `PriceSnapshot.run_id ==
    last_run`. После diff-only persist последний run (особенно intraday-tick на
    1 категорию) пишет snapshot'ы только для товаров с изменившейся ценой →
    join отдавал почти пусто → страница /analytics брендов была пустой. Теперь
    считаем АКТИВНЫЕ товары по `Product.last_seen_at` (обновляется каждый прогон
    независимо от записи snapshot'а — это и есть «видели недавно»), как и
    остальные diff-only-aware консьюмеры. `run_id` оставлен для обратной
    совместимости: если явно передан — используем старый snapshot-join.
    """
    if run_id is not None:
        # Explicit run_id (legacy/тесты) — точечный snapshot-join по этому прогону.
        stmt = (
            select(Product.brand, Product.site, func.count(Product.id))
            .join(PriceSnapshot, PriceSnapshot.product_id == Product.id)
            .where(
                PriceSnapshot.run_id == run_id,
                Product.brand.is_not(None),
                Product.tenant_id == tenant_id,
            )
            .group_by(Product.brand, Product.site)
        )
    else:
        # Diff-only-correct: активные товары по last_seen_at в окне.
        cutoff = utcnow() - timedelta(days=_BRAND_SHARE_ACTIVE_DAYS)
        stmt = (
            select(Product.brand, Product.site, func.count(Product.id))
            .where(
                Product.last_seen_at >= cutoff,
                Product.brand.is_not(None),
                Product.tenant_id == tenant_id,
            )
            .group_by(Product.brand, Product.site)
        )
    rows = session.execute(stmt).all()

    # P0.2 (PO Audit 2026-05-17): runtime фильтр generic-слов попавших в
    # brand-поле. Backfill cleanup-скрипт чистит БД, но если новые скрейпы
    # ещё не успели пройти через обновлённый extract_brand — пропустим
    # blacklisted сюда. Двойная защита.
    from src.brand_catalog import is_brand_blacklisted

    # Aggregate
    by_brand: dict[str, dict[str, int]] = defaultdict(lambda: {s: 0 for s in ALL_SITES})
    for brand, site_name, n in rows:
        if not brand or is_brand_blacklisted(brand):
            continue
        by_brand[brand][site_name] = n

    out: list[BrandRow] = []
    for brand, counts in by_brand.items():
        total = sum(counts.values())
        sites_with = sum(1 for v in counts.values() if v > 0)
        # Site filter (Phase 6-fix 2026-05-28): если запросили конкретный сайт,
        # оставляем только бренды представленные на этом сайте. Counts всё равно
        # показывают полный per-site breakdown — для exclusivity analysis.
        if site is not None and counts.get(site, 0) == 0:
            continue
        exclusive = None
        if sites_with == 1:
            exclusive = next(s for s, v in counts.items() if v > 0)
        out.append(
            BrandRow(
                brand=brand,
                counts=dict(counts),
                total=total,
                sites_with_brand=sites_with,
                exclusive_to=exclusive,
            )
        )
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


def promo_history(session: Session, *, days: int = 30) -> list[PromoStats]:
    """История промо-кампаний за последние N дней.

    Группирует по (site, title), вычисляет first/last seen и длительность.
    """
    cutoff = utcnow() - timedelta(days=days)
    promos = session.scalars(select(Promo).where(Promo.captured_at >= cutoff)).all()

    grouped: dict[tuple[str, str], list[Promo]] = defaultdict(list)
    for p in promos:
        grouped[(p.site, p.title)].append(p)

    out: list[PromoStats] = []
    for (site, title), items in grouped.items():
        items.sort(key=lambda x: x.captured_at)
        first = items[0].captured_at
        last = items[-1].captured_at
        days_active = max(1, (last - first).days + 1)
        out.append(
            PromoStats(
                site=site,
                title=title,
                landing_url=items[0].landing_url,
                first_seen=first,
                last_seen=last,
                days_active=days_active,
            )
        )
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
            matched_count=0,
            only_client=0,
            only_competitor_count={s: 0 for s in COMPETITOR_SITES},
            coverage_pct=0.0,
        )

    # Сматченные кластеры
    matches = session.scalars(select(Match)).all()
    matched_count = len(matches)

    # Клиентские товары без canonical_id (эксклюзив клиента)
    only_client = (
        session.scalar(
            select(func.count(Product.id))
            .join(PriceSnapshot, PriceSnapshot.product_id == Product.id)
            .where(
                PriceSnapshot.run_id == run_id,
                Product.site == CLIENT_SITE,
                Product.canonical_id.is_(None),
            )
        )
        or 0
    )

    # Эксклюзив каждого конкурента
    only_comp: dict[str, int] = {}
    for site in COMPETITOR_SITES:
        only_comp[site] = (
            session.scalar(
                select(func.count(Product.id))
                .join(PriceSnapshot, PriceSnapshot.product_id == Product.id)
                .where(
                    PriceSnapshot.run_id == run_id,
                    Product.site == site,
                    Product.canonical_id.is_(None),
                )
            )
            or 0
        )

    # Coverage: client matched / total client
    total_client = (
        session.scalar(
            select(func.count(Product.id))
            .join(PriceSnapshot, PriceSnapshot.product_id == Product.id)
            .where(PriceSnapshot.run_id == run_id, Product.site == CLIENT_SITE)
        )
        or 0
    )
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
class CategoryComparison:
    """Расширенное сравнение цен клиента vs конкурентов в разрезе категории.

    `index` 100 = паритет, <100 клиент дешевле (хорошо), >100 дороже.
    `per_site_avg` — средняя цена КАЖДОГО конкурента отдельно (aptekonline/aloe),
    чтобы видеть кто именно бьёт по цене. `avg_competitor_price` — комбинированная
    (среднее по сайтам), на ней строится index.
    win/lose считается per-SKU против СРЕДНЕГО конкурента (та же база, что index):
    cheaper = клиент дешевле, pricier = дороже, parity = в пределах ±0.5%.
    """

    category: str  # slug (Product.category) — стабильный ключ группировки
    label_ru: str | None  # из Category.label_ru, иначе None → фронт покажет slug
    label_az: str | None
    matched_skus: int
    avg_client_price: float
    per_site_avg: dict[str, float]  # {"aptekonline": .., "aloe": ..}
    avg_competitor_price: float
    index: float
    cheaper_count: int
    pricier_count: int
    parity_count: int
    cheaper_pct: float
    pricier_pct: float
    parity_pct: float


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


def match_quality(session: Session, *, tenant_id: int = 1) -> MatchQuality:
    """Сводка качества матчинга — для отображения в дашборде."""
    from src.storage import Match, MatchRejection

    total_matches = (
        session.scalar(select(func.count(Match.id)).where(Match.tenant_id == tenant_id)) or 0
    )
    manual_matches = (
        session.scalar(
            select(func.count(Match.id)).where(
                Match.tenant_id == tenant_id,
                Match.is_manual.is_(True),
            )
        )
        or 0
    )
    auto_matches = total_matches - manual_matches
    rejected = (
        session.scalar(
            select(func.count(MatchRejection.id)).where(MatchRejection.tenant_id == tenant_id)
        )
        or 0
    )

    products_total = (
        session.scalar(select(func.count(Product.id)).where(Product.tenant_id == tenant_id)) or 0
    )
    products_matched = (
        session.scalar(
            select(func.count(Product.id)).where(
                Product.tenant_id == tenant_id,
                Product.canonical_id.is_not(None),
            )
        )
        or 0
    )
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


# Допуск паритета для per-SKU win/lose: max(0.5% относительно, 1 qəpik абсолютно).
# Абсолютный пол (аудит H4): 0.5% от дешёвого товара (1-2 AZN) < 1 qəpik —
# гранулярности цены, и бакет «паритет» почти не срабатывал бы. 1 qəpik = «та же цена».
_PARITY_EPS = 0.005
_PARITY_ABS = 0.01  # AZN


# Текущая цена товара: discount_price (если есть) иначе price, и только > 0.
def _current_price(snap: PriceSnapshot | None) -> float | None:
    if snap is None:
        return None
    price = snap.discount_price or snap.price
    if price is None or price <= 0:
        return None
    return price


def _iter_matched_prices(
    session: Session,
    *,
    client_site: str,
    tenant_id: int | None,
    min_confidence: float,
    categories: Collection[str] | None = None,
) -> list[tuple[str, float, dict[str, float]]]:
    """Per-match записи `(category, client_price, comp_price_by_site)`.

    Общий фундамент для `price_index_by_category` и `category_comparison`.

    Фиксы относительно прежней логики:
    - **diff-only:** `latest_snapshots_per_product` вместо snapshot'ов одного
      run_id — берёт актуальную цену независимо от того, менялась ли она в
      последнем прогоне (под diff-only стабильная цена пишется редко).
    - **tenant:** опц. фильтр `Match.tenant_id` (None → без фильтра, для
      не-HTTP вызовов вроде weekly email).
    - **confidence floor:** дропаем `confidence < min_confidence` (кроме
      `is_manual`) — как в `dash_comparison`, иначе мусорные fuzzy-матчи
      искажают средние и числа расходятся с товарным сравнением.

    Для каждого сайта-конкурента цена усредняется (если в матче >1 товар
    с этого сайта). Возвращаются только матчи с ценой клиента + ≥1 конкурента.
    """
    from src.product_policy import policy_rollout_eligibility

    q = select(Match).options(selectinload(Match.products))
    if tenant_id is not None:
        rollout = policy_rollout_eligibility(session, tenant_id=tenant_id)
        if not rollout.eligible:
            log.warning(
                "analytics_policy_gate_closed",
                tenant_id=tenant_id,
                reason=rollout.reason,
            )
            return []
        q = q.where(Match.tenant_id == tenant_id)
    category_filter = set(categories) if categories is not None else None
    if category_filter is not None and not category_filter:
        return []

    matches = session.scalars(q).all()
    if category_filter is not None:
        matches = [
            m
            for m in matches
            if any(
                p.site == client_site
                and p.url_dead_at is None
                and (p.manual_category_key or p.category or "(без категории)")
                in category_filter
                for p in m.products
            )
        ]

    all_pids = [p.id for m in matches for p in m.products]
    if tenant_id is None:
        pids_by_tenant: dict[int, list[int]] = defaultdict(list)
        for m in matches:
            for p in m.products:
                pids_by_tenant[int(p.tenant_id or 1)].append(p.id)
        snaps = {}
        for current_tenant_id, tenant_pids in pids_by_tenant.items():
            rollout = policy_rollout_eligibility(session, tenant_id=current_tenant_id)
            if not rollout.eligible:
                log.warning(
                    "analytics_policy_gate_closed",
                    tenant_id=current_tenant_id,
                    reason=rollout.reason,
                )
                continue
            trusted_lineage_available = bool(
                financially_eligible_run_ids(session, tenant_id=current_tenant_id)
            )
            snaps.update(
                latest_snapshots_per_product(
                    session,
                    tenant_pids,
                    financially_eligible_only=trusted_lineage_available,
                    tenant_id=current_tenant_id,
                )
            )
    else:
        # Mirror /comparison's safe bootstrap contract. In shadow policy mode
        # one site's eligible diff-only run is not a complete cross-site price
        # lineage and may contain no snapshots. Keep effective prices until all
        # required sites form a fresh trusted epoch, matching /comparison.
        from src.product_policy import trusted_catalog_epoch

        trusted_lineage_available = (
            trusted_catalog_epoch(session, tenant_id=tenant_id) is not None
        )
        snaps = latest_snapshots_per_product(
            session,
            all_pids,
            financially_eligible_only=trusted_lineage_available,
            tenant_id=tenant_id,
        )

    records: list[tuple[str, float, dict[str, float]]] = []
    for m in matches:
        from src.product_policy import policy_identity_eligibility, policy_offer_eligibility

        if not policy_identity_eligibility(list(m.products)).eligible:
            continue
        conf = m.confidence if m.confidence is not None else 1.0
        if not m.is_manual and conf < min_confidence:
            continue
        client_products = [
            p
            for p in m.products
            if p.site == client_site
            and p.url_dead_at is None
            and policy_offer_eligibility(p).eligible
            and (
                category_filter is None
                or (p.manual_category_key or p.category or "(без категории)")
                in category_filter
            )
        ]
        if category_filter is None:
            client_products = client_products[:1]
        # «Фантомный» клиент (страница 404, помечен validate-links) → матч бесполезен.
        if not client_products:
            continue

        comp_by_site: dict[str, list[float]] = defaultdict(list)
        for p in m.products:
            if (
                p.site == client_site
                or p.url_dead_at is not None
                or not policy_offer_eligibility(p).eligible
            ):
                continue
            price = _current_price(snaps.get(p.id))
            if price is not None:
                comp_by_site[p.site].append(price)
        if not comp_by_site:
            continue

        comp_price_by_site = {site: sum(v) / len(v) for site, v in comp_by_site.items()}
        for client_p in client_products:
            client_price = _current_price(snaps.get(client_p.id))
            if client_price is None:
                continue
            cat = client_p.manual_category_key or client_p.category or "(без категории)"
            records.append((cat, client_price, comp_price_by_site))
    return records


def price_index_by_category(
    session: Session,
    *,
    client_site: str = CLIENT_SITE,
    tenant_id: int | None = None,
    min_confidence: float = 0.70,
) -> list[PriceIndex]:
    """Для каждой категории — средняя цена клиента vs конкурентов (index).

    diff-only-safe (latest_snapshots) + tenant/confidence-aware. См.
    `_iter_matched_prices`. `category_comparison` — расширенная версия с
    per-site разбивкой и win/lose.
    """
    records = _iter_matched_prices(
        session, client_site=client_site, tenant_id=tenant_id, min_confidence=min_confidence
    )
    by_cat: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"client": [], "comp": []})
    for cat, client_price, comp_by_site in records:
        comp_mean = sum(comp_by_site.values()) / len(comp_by_site)
        by_cat[cat]["client"].append(client_price)
        by_cat[cat]["comp"].append(comp_mean)

    out: list[PriceIndex] = []
    for cat, prices in by_cat.items():
        if not prices["client"]:
            continue
        avg_c = sum(prices["client"]) / len(prices["client"])
        avg_comp = sum(prices["comp"]) / len(prices["comp"])
        idx = (avg_c / avg_comp * 100) if avg_comp else 100.0
        out.append(
            PriceIndex(
                category=cat,
                avg_client_price=round(avg_c, 2),
                avg_competitor_price=round(avg_comp, 2),
                index=round(idx, 1),
                matched_skus=len(prices["client"]),
            )
        )
    out.sort(key=lambda x: -x.matched_skus)
    return out


def category_comparison(
    session: Session,
    *,
    client_site: str = CLIENT_SITE,
    tenant_id: int | None = None,
    min_confidence: float = 0.70,
    categories: Collection[str] | None = None,
    canonical: bool = False,
) -> list[CategoryComparison]:
    """Сравнение цен по категориям: per-site средние + index + win/lose.

    Категория = `Product.category` товара-клиента (slug сайта). Человекочитаемый
    ярлык подтягивается из `Category.pharmonline_slug` одним запросом; если slug
    не в таблице (напр. сырой Mongo `_id` при промахе DDP-карты) — label_ru/az
    остаются None, фронт показывает сырой slug. Математика стабильна в любом случае.
    """
    records = _iter_matched_prices(
        session,
        client_site=client_site,
        tenant_id=tenant_id,
        min_confidence=min_confidence,
        categories=categories,
    )
    if not records:
        return []

    canonical_labels = {}
    fallback_labels: dict[str, tuple[str | None, str | None]] = {}
    if canonical:
        from src.category_taxonomy import (
            CANONICAL_CATEGORIES,
            CanonicalCategory,
            classify_source_category_detailed,
            source_category_labels,
        )

        source_labels = source_category_labels(session)
        canonical_by_key = {category.key: category for category in CANONICAL_CATEGORIES}
        # Классификация зависит только от категории-источника, а не от матча:
        # мемоизируем по raw_category (иначе ~200 правил × ~4k матчей регэкспов
        # вместо ~200 × ~178 категорий).
        resolved: dict[str, tuple[CanonicalCategory | None, str]] = {}

        def _canonical_for(raw_category: str) -> tuple[CanonicalCategory | None, str]:
            if raw_category in canonical_by_key:
                return canonical_by_key[raw_category], "manual_override"
            if raw_category not in resolved:
                label_ru, label_az = source_labels.get((client_site, raw_category), (None, None))
                classification = classify_source_category_detailed(
                    client_site,
                    raw_category,
                    label_ru=label_ru,
                    label_az=label_az,
                )
                resolved[raw_category] = (classification.category, classification.reason)
            return resolved[raw_category]

        canonical_records: list[tuple[str, float, dict[str, float]]] = []
        for raw_category, client_price, comp_by_site in records:
            mapped, reason = _canonical_for(raw_category)
            # Формы выпуска и заведомо широкие корневые разделы не являются
            # товарными категориями и по-прежнему не создают шум в дашборде.
            if mapped is None:
                if reason in {"blocked_broad_bucket", "blocked_dosage_form", "no_slug"}:
                    continue
                # Неизвестная или неоднозначная категория не должна скрывать
                # реально matched SKU. Показываем её отдельной строкой под
                # исходным slug/label, пока taxonomy не научится её объединять.
                canonical_records.append((raw_category, client_price, comp_by_site))
                fallback_labels[raw_category] = source_labels.get(
                    (client_site, raw_category), (None, None)
                )
            else:
                canonical_records.append((mapped.key, client_price, comp_by_site))
                canonical_labels[mapped.key] = mapped
        records = canonical_records
        if not records:
            return []

    groups: dict[str, dict] = defaultdict(
        lambda: {
            "client": [],
            "per_site": defaultdict(list),
            "comp": [],
            "cheaper": 0,
            "pricier": 0,
            "parity": 0,
        }
    )
    for cat, client_price, comp_by_site in records:
        g = groups[cat]
        g["client"].append(client_price)
        for site, price in comp_by_site.items():
            g["per_site"][site].append(price)
        comp_mean = sum(comp_by_site.values()) / len(comp_by_site)
        g["comp"].append(comp_mean)
        # win/lose против среднего конкурента ЭТОГО матча (та же база, что index).
        # NB: индекс категории — mean(comp_mean по МАТЧАМ) → взвешен по числу matched
        # SKU (каждый матч равен), НЕ mean(per_site_avg) — одиночный SKU сайта не
        # перекашивает индекс. per_site_avg — только для отображения колонок.
        band = max(comp_mean * _PARITY_EPS, _PARITY_ABS)
        if client_price < comp_mean - band:
            g["cheaper"] += 1
        elif client_price > comp_mean + band:
            g["pricier"] += 1
        else:
            g["parity"] += 1

    # Ярлыки одним запросом: slug → (label_ru, label_az). Canonical mode uses
    # compact source-independent labels instead.
    slugs = list(groups.keys())
    labels: dict[str, tuple[str | None, str | None]] = {}
    if canonical:
        labels = {
            key: (category.label_ru, category.label_az)
            for key, category in canonical_labels.items()
            if category is not None
        }
        labels.update(fallback_labels)
    elif slugs:
        for c in session.scalars(
            select(Category).where(Category.pharmonline_slug.in_(slugs))
        ).all():
            labels[c.pharmonline_slug] = (c.label_ru, c.label_az)

    out: list[CategoryComparison] = []
    for cat, g in groups.items():
        n = len(g["client"])
        if n == 0:
            continue
        avg_c = sum(g["client"]) / n
        avg_comp = sum(g["comp"]) / len(g["comp"])
        idx = (avg_c / avg_comp * 100) if avg_comp else 100.0
        per_site_avg = {
            site: round(sum(vals) / len(vals), 2) for site, vals in g["per_site"].items() if vals
        }
        label_ru, label_az = labels.get(cat, (None, None))
        out.append(
            CategoryComparison(
                category=cat,
                label_ru=label_ru,
                label_az=label_az,
                matched_skus=n,
                avg_client_price=round(avg_c, 2),
                per_site_avg=per_site_avg,
                avg_competitor_price=round(avg_comp, 2),
                index=round(idx, 1),
                cheaper_count=g["cheaper"],
                pricier_count=g["pricier"],
                parity_count=g["parity"],
                cheaper_pct=round(g["cheaper"] / n * 100, 1),
                pricier_pct=round(g["pricier"] / n * 100, 1),
                parity_pct=round(g["parity"] / n * 100, 1),
            )
        )
    # Дефолт-сортировка: наибольший «мисприсинг» = |index-100| × matched_skus.
    # Категории где клиент сильнее всего отклонён от рынка И с весом SKU — вверху.
    out.sort(key=lambda x: -abs(x.index - 100.0) * x.matched_skus)
    return out
