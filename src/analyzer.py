"""Анализатор: сравнивает текущий прогон с предыдущим.

Формирует структурированный отчёт по 3 измерениям:
- Изменения цен (особенно: конкурент опустил ниже клиента)
- Новые товары (на конкурентах появились SKU, которых не было вчера)
- Изменения промо/акций
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import structlog
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from src import storage
from src.storage import Match, PriceSnapshot, Product, Promo, Run

log = structlog.get_logger()

CLIENT_SITE = "pharmonline"
COMPETITOR_SITES = ("aptekonline", "aloe")


@dataclass
class PriceChange:
    product_name: str
    site: str
    url: str
    prev_price: float | None
    curr_price: float | None
    delta_pct: float | None
    is_on_sale: bool
    promo_label: str | None = None


@dataclass
class CompetitorUndercut:
    """Конкурент опустил цену ниже клиента."""

    canonical_name: str
    client_price: float | None
    client_url: str | None
    competitor_site: str
    competitor_price: float
    competitor_url: str
    diff_pct: float  # на сколько % ниже клиента


@dataclass
class NewProduct:
    site: str
    name: str
    url: str
    price: float | None
    category: str | None


@dataclass
class PromoChange:
    site: str
    title: str
    landing_url: str | None
    is_new: bool


@dataclass
class AnalysisReport:
    run_id: int
    run_started_at: datetime
    prev_run_id: int | None
    prev_run_at: datetime | None
    price_changes: list[PriceChange] = field(default_factory=list)
    undercuts: list[CompetitorUndercut] = field(default_factory=list)
    new_products: list[NewProduct] = field(default_factory=list)
    promo_changes: list[PromoChange] = field(default_factory=list)
    summary_counts: dict = field(default_factory=dict)


def analyze(session: Session, current_run_id: int) -> AnalysisReport:
    """Главная точка входа."""
    current_run = session.get(Run, current_run_id)
    if not current_run:
        raise ValueError(f"Run {current_run_id} not found")

    verified_only = storage.run_is_financially_eligible(current_run)

    prev_stmt = select(Run).where(
        Run.id != current_run_id,
        Run.status == "ok",
        Run.tenant_id == current_run.tenant_id,
    )
    if verified_only:
        eligible_ids = storage.financially_eligible_run_ids(
            session,
            tenant_id=current_run.tenant_id,
        )
        prev_stmt = prev_stmt.where(Run.id.in_(eligible_ids))
    prev_run = session.scalars(
        prev_stmt.order_by(desc(Run.started_at)).limit(1)
    ).first()

    report = AnalysisReport(
        run_id=current_run.id,
        run_started_at=current_run.started_at,
        prev_run_id=prev_run.id if prev_run else None,
        prev_run_at=prev_run.started_at if prev_run else None,
    )

    # Undercut — это состояние "здесь и сейчас", считаем всегда (даже на первом прогоне)
    report.undercuts = _detect_undercuts(
        session,
        verified_only=verified_only,
        tenant_id=current_run.tenant_id,
    )

    if prev_run is None:
        log.info("analyzer_first_run", run_id=current_run.id)
        report.summary_counts = {
            **_count_first_run(session, current_run_id),
            "undercuts": len(report.undercuts),
        }
        return report

    report.price_changes = _detect_price_changes(
        session,
        current_run,
        verified_only=verified_only,
    )
    report.new_products = _detect_new_products(
        session,
        current_run,
        verified_only=verified_only,
    )
    report.promo_changes = _detect_promo_changes(
        session,
        prev_run.id,
        current_run.id,
        verified_only=verified_only,
        tenant_id=current_run.tenant_id,
    )
    report.summary_counts = {
        "price_changes": len(report.price_changes),
        "undercuts": len(report.undercuts),
        "new_products": len(report.new_products),
        "promo_changes": len(report.promo_changes),
    }
    return report


# curr_and_prev_snapshots_for_run перенесён в storage.py — общий helper.


def _detect_price_changes(
    session: Session,
    current_run: Run,
    *,
    verified_only: bool = False,
) -> list[PriceChange]:
    """Сравниваем curr-snap с последним snapshot'ом до current_run.started_at.

    После diff-only persist (2026-05-09) snapshots в curr_run — это уже
    сами по себе «изменения цены». Предыдущая цена — последний snapshot
    captured_at < run.started_at (НЕ обязательно из соседнего прогона:
    может быть из недельной давности, если цена была стабильна).
    """
    from src.storage import curr_and_prev_snapshots_for_run

    curr_snaps, prev_by_product = curr_and_prev_snapshots_for_run(
        session,
        current_run,
        financially_eligible_only=verified_only,
    )

    changes: list[PriceChange] = []
    for curr in curr_snaps:
        prev = prev_by_product.get(curr.product_id)
        if not prev:
            continue  # Новый продукт без истории → попадёт в new_products

        prev_price = prev.discount_price or prev.price
        curr_price = curr.discount_price or curr.price
        if prev_price is None or curr_price is None or prev_price <= 0:
            continue  # prev=0 (restock из нуля) → иначе деление на 0 в delta_pct
        if abs(curr_price - prev_price) < 0.01:
            continue
        delta_pct = round((curr_price - prev_price) / prev_price * 100, 2)
        product = curr.product
        changes.append(
            PriceChange(
                product_name=product.name,
                site=product.site,
                url=product.url,
                prev_price=prev_price,
                curr_price=curr_price,
                delta_pct=delta_pct,
                is_on_sale=curr.is_on_sale,
                promo_label=curr.promo_label,
            )
        )
    changes.sort(key=lambda c: abs(c.delta_pct or 0), reverse=True)
    return changes


def _detect_undercuts(
    session: Session,
    threshold_pct: float = 0.0,
    *,
    verified_only: bool = False,
    tenant_id: int = 1,
) -> list[CompetitorUndercut]:
    """Конкурент дешевле клиента на тот же canonical_match → undercut.

    Diff-only-aware (2026-05-09): «текущая цена» = latest snapshot per product
    глобально (не привязка к curr_run, т.к. после diff-only продукт может не
    иметь snapshot'а в каждом run'е, если цена не менялась). Используем
    aggregate `MAX(captured_at) GROUP BY product_id` + JOIN.
    """
    matches = session.scalars(
        select(Match).where(Match.products.any(), Match.tenant_id == tenant_id)
    ).all()

    # Все product_ids из всех matches
    all_match_product_ids: set[int] = set()
    for m in matches:
        for p in m.products:
            all_match_product_ids.add(p.id)
    if not all_match_product_ids:
        return []

    snaps_by_product = storage.latest_snapshots_per_product(
        session,
        all_match_product_ids,
        financially_eligible_only=verified_only,
        tenant_id=tenant_id,
    )

    undercuts: list[CompetitorUndercut] = []
    for m in matches:
        client_product: Product | None = None
        competitor_products: list[Product] = []
        for p in m.products:
            if p.site == CLIENT_SITE:
                client_product = p
            elif p.site in COMPETITOR_SITES:
                competitor_products.append(p)

        if not client_product:
            continue
        client_snap = snaps_by_product.get(client_product.id)
        if not client_snap:
            continue
        client_price = client_snap.discount_price or client_snap.price
        if client_price is None or client_price <= 0:
            continue  # цена 0 = нет в наличии → иначе деление на 0 в diff_pct (run #311)

        for cp in competitor_products:
            comp_snap = snaps_by_product.get(cp.id)
            if not comp_snap:
                continue
            comp_price = comp_snap.discount_price or comp_snap.price
            if comp_price is None:
                continue
            # Price-sanity filter (2026-05-11): aptekonline.az иногда
            # листингует single-piece SKU за 0.20 ₼ против pack за 28 ₼
            # (опечатка в их БД). Пропускаем явные outlier'ы — конкурент
            # «дешевле на 99%» это не реальный undercut, а data error.
            if comp_price < client_price * 0.1:
                continue
            diff_pct = round((client_price - comp_price) / client_price * 100, 2)
            if diff_pct > threshold_pct:
                undercuts.append(
                    CompetitorUndercut(
                        canonical_name=m.canonical_name,
                        client_price=client_price,
                        client_url=client_product.url,
                        competitor_site=cp.site,
                        competitor_price=comp_price,
                        competitor_url=cp.url,
                        diff_pct=diff_pct,
                    )
                )

    undercuts.sort(key=lambda u: u.diff_pct, reverse=True)
    return undercuts


def _detect_new_products(
    session: Session,
    current_run: Run,
    *,
    verified_only: bool = False,
) -> list[NewProduct]:
    """Товары, у которых до current_run.started_at не было ни одного snapshot'а.

    «Новый» = впервые видим. Если в `curr_and_prev_snapshots_for_run` для
    продукта нет prev_snapshot'а, значит до этого прогона его в БД не было.
    """
    from src.storage import curr_and_prev_snapshots_for_run

    curr_snaps, prev_by_product = curr_and_prev_snapshots_for_run(
        session,
        current_run,
        financially_eligible_only=verified_only,
    )

    new_products: list[NewProduct] = []
    for snap in curr_snaps:
        if snap.product_id in prev_by_product:
            continue  # есть более ранний snapshot → не новый
        product = snap.product
        new_products.append(
            NewProduct(
                site=product.site,
                name=product.name,
                url=product.url,
                price=snap.discount_price or snap.price,
                category=product.category,
            )
        )
    new_products.sort(key=lambda n: n.site)
    return new_products


def _detect_promo_changes(
    session: Session,
    prev_run_id: int,
    curr_run_id: int,
    *,
    verified_only: bool = False,
    tenant_id: int = 1,
) -> list[PromoChange]:
    curr = session.scalars(select(Promo).where(Promo.run_id == curr_run_id)).all()
    if verified_only:
        from sqlalchemy import and_, or_

        previous_by_site = storage.latest_financial_run_ids_by_site(
            session,
            {promo.site for promo in curr},
            tenant_id=tenant_id,
            before_run_id=curr_run_id,
        )
        if previous_by_site:
            prev = session.scalars(
                select(Promo).where(
                    or_(
                        *(
                            and_(Promo.site == site, Promo.run_id == run_id)
                            for site, run_id in previous_by_site.items()
                        )
                    )
                )
            ).all()
        else:
            prev = []
    else:
        prev = session.scalars(select(Promo).where(Promo.run_id == prev_run_id)).all()

    prev_keys: set[tuple[str, str]] = {(p.site, p.title) for p in prev}
    curr_keys: set[tuple[str, str]] = {(p.site, p.title) for p in curr}

    changes: list[PromoChange] = []
    for p in curr:
        if (p.site, p.title) not in prev_keys:
            changes.append(
                PromoChange(site=p.site, title=p.title, landing_url=p.landing_url, is_new=True)
            )
    for p in prev:
        if (p.site, p.title) not in curr_keys:
            changes.append(
                PromoChange(site=p.site, title=p.title, landing_url=p.landing_url, is_new=False)
            )
    return changes


def _count_first_run(session: Session, run_id: int) -> dict:
    n_products = session.scalar(
        select(PriceSnapshot)
        .where(PriceSnapshot.run_id == run_id)
        .with_only_columns(PriceSnapshot.id)
        .limit(0)
    )
    counts = {}
    for site in (CLIENT_SITE, *COMPETITOR_SITES):
        c = sum(1 for _ in session.scalars(select(Product).where(Product.site == site)))
        counts[site] = c
    counts["total_products"] = sum(counts.values())
    return counts
