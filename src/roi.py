"""ROI / Actionable insights — превращаем сырые данные в конкретные действия.

Главная цель: вместо "вот метрики, разбирайся" дать клиенту:
    "Опусти Aspirin Cardio на 0.50 ₼ за единицу (−31% от текущей)"

Поддерживаемые типы действий:
    1. PRICE_RAISE     — клиент дешевле всех конкурентов, можно поднять
    2. UNDERCUT        — конкурент опустил цену ниже клиента (нужна реакция)
    3. ASSORTMENT_GAP  — товар есть у конкурента, нет у клиента
    4. PROMO_RESPONSE  — конкурент запустил акцию (нужно реагировать)

Что НЕ рассчитываем (и почему):
    «AZN/месяц» убран 2026-05-13 — старый код умножал разницу цен на
    захардкоженный объём `assumed_monthly_volume=30`. Это был placeholder
    без основания: у одного товара 200 шт/мес, у другого 2 шт/мес. Получался
    misleading вывод где приоритеты не отражали реальный профит.
    Теперь возвращаем ТОЛЬКО проверяемые цифры: `unit_gap_azn` (разница на
    единицу) и `spread_pct` (% спред). Клиент сам прикинет ×свой_объём.

    Когда у клиента будет ERP-интеграция с реальной stock-историей, можно
    будет deriv'ить `monthly_volume = (stock_start - stock_end + purchases)
    / days × 30` per-product и вернуть честные ₼/мес.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import structlog
from sqlalchemy import desc, select
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

ActionType = Literal[
    "price_raise",
    "undercut",
    "assortment_gap",
    "promo_response",
]


@dataclass
class ActionItem:
    """Одно конкретное действие, которое клиент может предпринять."""

    type: ActionType
    severity: Literal["info", "opportunity", "warning", "critical"]
    title: str
    detail: str
    product_name: str | None = None
    product_url: str | None = None
    current_value_azn: float | None = None
    target_value_azn: float | None = None
    # Разница цены за ЕДИНИЦУ товара (положительное = профит при подъёме,
    # отрицательное = маржа которую теряем если опустим до конкурента).
    unit_gap_azn: float | None = None
    # % спред между ценами. Положительный для price_raise (мы дешевле),
    # отрицательный для undercut (конкурент дешевле).
    spread_pct: float | None = None
    # DEPRECATED 2026-05-13: всегда 0.0. Раньше = unit_gap × assumed_volume=30,
    # но volume был placeholder без основания. Поле остаётся для backward-compat
    # с telegram_bot.py + старого dashboard.py. Не использовать в новом коде.
    estimated_monthly_impact_azn: float = 0.0
    competitor_site: str | None = None
    competitor_url: str | None = None
    extra: dict = field(default_factory=dict)


def _preload_snapshots(session: Session, run_id: int) -> dict[int, "PriceSnapshot"]:
    """Загрузить latest snapshot per product, для всех matched товаров.

    Diff-only-aware (2026-05-09): после оптимизации persist'а snapshot
    пишется только при изменении цены. «Цена в последнем прогоне» больше
    не имеет смысла per-product — нужна latest snapshot регардлес of run.

    Аргумент `run_id` оставлен для совместимости интерфейса, но семантика
    теперь — latest globally (внутренне ограничено матчевыми product_ids).
    """
    from src.storage import latest_snapshots_per_product

    matched_pids = session.scalars(
        select(Product.id).where(Product.canonical_id.is_not(None))
    ).all()
    if not matched_pids:
        return {}
    return latest_snapshots_per_product(session, matched_pids)


def compute_actions(
    session: Session,
    *,
    raise_threshold_pct: float = 5.0,  # минимальная разница чтобы советовать поднять
    undercut_threshold_pct: float = 3.0,  # минимальная просадка чтобы алерт
    max_spread_pct: float = 90.0,  # выше этого считаем bad-match и скрываем
    max_per_type: int = 10,
) -> list[ActionItem]:
    """Главная точка: собрать все действия, отсортировать по spread desc.

    Сортируем по |spread_pct| desc — самые большие разрывы вверху. Не по
    «месячному impact'у» потому что объёмы продаж нам неизвестны (см.
    module docstring).
    """
    actions: list[ActionItem] = []
    actions += _price_raise_opportunities(
        session, raise_threshold_pct, max_per_type
    )
    actions += _undercut_threats(
        session, undercut_threshold_pct, max_spread_pct, max_per_type
    )
    actions += _assortment_gaps(session, max_per_type)
    actions += _promo_responses(session, max_per_type)

    # Сортировка: critical → warning → opportunity → info; внутри — по |spread_pct| desc
    sev_order = {"critical": 0, "warning": 1, "opportunity": 2, "info": 3}
    actions.sort(
        key=lambda a: (
            sev_order.get(a.severity, 9),
            -abs(a.spread_pct or 0),
        )
    )
    return actions


def _latest_run_id(session: Session) -> int | None:
    return session.scalar(
        select(Run.id).where(Run.status == "ok").order_by(desc(Run.id)).limit(1)
    )


def _price_raise_opportunities(
    session: Session, threshold_pct: float, max_n: int
) -> list[ActionItem]:
    """Где клиент дешевле всех конкурентов более чем на threshold%."""
    run_id = _latest_run_id(session)
    if not run_id:
        return []

    matches = session.scalars(select(Match)).all()
    snaps_cache = _preload_snapshots(session, run_id)
    out: list[ActionItem] = []

    for m in matches:
        prices_by_site = _prices_for_match(session, m, run_id, snaps_cache)
        client_price = prices_by_site.get(CLIENT_SITE)
        if client_price is None:
            continue
        comp_prices = [
            (s, p) for s, p in prices_by_site.items()
            if s in COMPETITOR_SITES and p is not None
        ]
        if not comp_prices:
            continue

        median_comp = sorted(p for _, p in comp_prices)[len(comp_prices) // 2]
        if median_comp <= client_price:
            continue  # клиент не дешевле — не наш кейс
        gap_pct = (median_comp - client_price) / client_price * 100
        if gap_pct < threshold_pct:
            continue  # разница слишком мелкая

        # Целевая цена = медиана конкурентов − 2% буфер (чтоб остаться лучшим)
        target = round(median_comp * 0.98, 2)
        delta = round(target - client_price, 2)

        client_product = _client_product(m)
        # Если товара нет на складе — нет смысла советовать поднимать
        if client_product and not _is_in_stock(session, client_product.id):
            continue

        out.append(ActionItem(
            type="price_raise",
            severity="opportunity",
            title=f"Подними цену: {m.canonical_name}",
            detail=(
                f"Ты дешевле конкурентов на {gap_pct:.1f}%. "
                f"Подними с {client_price:.2f} до {target:.2f} ₼ — "
                f"останешься самым дешёвым, но получишь +{delta:.2f} ₼/ед."
            ),
            product_name=m.canonical_name,
            product_url=client_product.url if client_product else None,
            current_value_azn=client_price,
            target_value_azn=target,
            unit_gap_azn=delta,
            spread_pct=round(gap_pct, 1),
            extra={"median_competitor": median_comp},
        ))

    out.sort(key=lambda a: -(a.spread_pct or 0))
    return out[:max_n]


def _is_in_stock(session: Session, product_id: int) -> bool:
    """True если у клиента есть остаток на складе (или нет данных stock — считаем что есть)."""
    from src.inventory import get_stock_for_product
    stock = get_stock_for_product(session, product_id)
    return True if stock is None else bool(stock.is_in_stock)


def _undercut_threats(
    session: Session, threshold_pct: float, max_spread_pct: float, max_n: int
) -> list[ActionItem]:
    """Где конкурент опустил цену ниже клиента.

    Пропускаем спреды >= `max_spread_pct` (90% по умолчанию) — такое отклонение
    обычно говорит не о настоящем undercut'е, а о бракованном matching'е
    (например, поштучный товар слепился с упаковкой 10шт). Реальные ценовые
    войны не дают 99% дисконт.
    """
    run_id = _latest_run_id(session)
    if not run_id:
        return []

    matches = session.scalars(select(Match)).all()
    snaps_cache = _preload_snapshots(session, run_id)
    out: list[ActionItem] = []

    for m in matches:
        prices_by_site = _prices_for_match(session, m, run_id, snaps_cache)
        client_price = prices_by_site.get(CLIENT_SITE)
        if client_price is None:
            continue

        cheapest_comp_site = None
        cheapest_comp_price = None
        for site in COMPETITOR_SITES:
            p = prices_by_site.get(site)
            if p is None:
                continue
            if cheapest_comp_price is None or p < cheapest_comp_price:
                cheapest_comp_site = site
                cheapest_comp_price = p

        if cheapest_comp_price is None or cheapest_comp_price >= client_price:
            continue
        diff_pct = (client_price - cheapest_comp_price) / client_price * 100
        if diff_pct < threshold_pct:
            continue
        if diff_pct >= max_spread_pct:
            # Подозрительный спред — почти наверняка bad match. Скрываем
            # чтобы не вводить клиента в заблуждение «опусти до 0.25 ₼».
            continue

        # Рекомендованная новая цена = match competitor − 0.01 (быть на копейку дешевле)
        target = round(cheapest_comp_price - 0.01, 2)
        # Lost margin per unit (assuming we have to match)
        lost_per_unit = round(client_price - target, 2)

        client_product = _client_product(m)
        comp_url = next(
            (p.url for p in m.products if p.site == cheapest_comp_site), None
        )

        # Stock-aware: если у нас нет товара на складе — undercut неактуален
        if client_product and not _is_in_stock(session, client_product.id):
            continue

        severity: Literal["warning", "critical"] = (
            "critical" if diff_pct >= 10 else "warning"
        )

        # Margin-aware: если опускаем ниже purchase price — критически
        margin_warning = ""
        if client_product:
            from src.inventory import get_min_purchase_price
            purchase = get_min_purchase_price(session, client_product.id)
            if purchase is not None:
                if target <= purchase:
                    severity = "critical"
                    margin_warning = (
                        f" ⚠️ Целевая цена {target:.2f} ₼ ниже закупки {purchase:.2f} ₼ — "
                        f"продажа в убыток. Лучше держать текущую и принять потерю объёма."
                    )
                elif (target - purchase) / target < 0.10:
                    margin_warning = (
                        f" ⚠️ Маржа после снижения < 10% (закупка {purchase:.2f} ₼)."
                    )

        out.append(ActionItem(
            type="undercut",
            severity=severity,
            title=f"Конкурент дешевле: {m.canonical_name}",
            detail=(
                f"{cheapest_comp_site} продаёт за {cheapest_comp_price:.2f} ₼, "
                f"ты — за {client_price:.2f} ₼ (−{diff_pct:.1f}%). "
                f"Опусти до {target:.2f} чтобы остаться конкурентным." + margin_warning
            ),
            product_name=m.canonical_name,
            product_url=client_product.url if client_product else None,
            current_value_azn=client_price,
            target_value_azn=target,
            unit_gap_azn=-lost_per_unit,  # отрицательное — потерянная маржа на единицу
            spread_pct=-round(diff_pct, 1),  # отрицательный = конкурент дешевле
            competitor_site=cheapest_comp_site,
            competitor_url=comp_url,
        ))

    # Сортируем по |spread_pct| desc — самые серьёзные разрывы сверху
    out.sort(key=lambda a: -abs(a.spread_pct or 0))
    return out[:max_n]


def _assortment_gaps(session: Session, max_n: int) -> list[ActionItem]:
    """Товары на конкурентах которых нет у клиента (canonical_id is None).

    Diff-only-aware (2026-05-09): берём competitor unmatched products + их
    latest snapshot (а не «snapshot последнего прогона», который после
    diff-only пропускает продукты без price-changes).
    """
    from src.storage import latest_snapshots_per_product

    products = session.scalars(
        select(Product).where(
            Product.site.in_(COMPETITOR_SITES),
            Product.canonical_id.is_(None),
        )
    ).all()
    if not products:
        return []

    snaps_by_pid = latest_snapshots_per_product(session, [p.id for p in products])
    rows: list[tuple[Product, "PriceSnapshot"]] = []
    for p in products:
        snap = snaps_by_pid.get(p.id)
        if snap is None:
            continue
        rows.append((p, snap))
    # Сортируем по цене desc — самые дорогие unmatched товары вверху
    rows.sort(key=lambda r: (r[1].discount_price or r[1].price or 0), reverse=True)

    out: list[ActionItem] = []
    for product, snap in rows[:max_n]:
        price = snap.discount_price or snap.price
        if price is None or price < 1.0:
            continue
        out.append(ActionItem(
            type="assortment_gap",
            severity="opportunity",
            title=f"Расширь ассортимент: {product.name}",
            detail=(
                f"Этот товар есть у {product.site} ({price:.2f} ₼), но нет у тебя. "
                f"Возможный новый SKU для каталога."
            ),
            product_name=product.name,
            competitor_site=product.site,
            competitor_url=product.url,
            current_value_azn=price,
            extra={"category": product.category},
        ))
    return out


def _promo_responses(session: Session, max_n: int) -> list[ActionItem]:
    """Активные промо у конкурентов — могут потребовать ответа."""
    run_id = _latest_run_id(session)
    if not run_id:
        return []
    promos = session.scalars(
        select(Promo).where(
            Promo.run_id == run_id, Promo.site.in_(COMPETITOR_SITES)
        )
    ).all()
    out: list[ActionItem] = []
    for promo in promos[:max_n]:
        out.append(ActionItem(
            type="promo_response",
            severity="warning",
            title=f"Промо у {promo.site}: {promo.title[:60]}",
            detail=(
                f"Конкурент {promo.site} запустил акцию. "
                "Проверь, не задевает ли твои топ-категории."
            ),
            competitor_site=promo.site,
            competitor_url=promo.landing_url,
        ))
    return out


def _prices_for_match(
    session: Session,
    match: Match,
    run_id: int,
    snapshots_cache: dict[int, "PriceSnapshot"] | None = None,
) -> dict[str, float | None]:
    """Эффективная цена для каждого сайта в Match.

    Если передан `snapshots_cache` (preloaded {product_id: PriceSnapshot}) —
    используется он вместо отдельного SQL на каждый product. Это убирает N+1.
    """
    out: dict[str, float | None] = {
        CLIENT_SITE: None, "aptekonline": None, "aloe": None
    }
    for p in match.products:
        if snapshots_cache is not None:
            snap = snapshots_cache.get(p.id)
        else:
            snap = session.scalar(
                select(PriceSnapshot).where(
                    PriceSnapshot.product_id == p.id, PriceSnapshot.run_id == run_id
                )
            )
        if snap is None:
            continue
        eff = snap.discount_price if snap.discount_price else snap.price
        if eff is not None:
            out[p.site] = eff
    return out


def _client_product(match: Match) -> Product | None:
    return next((p for p in match.products if p.site == CLIENT_SITE), None)


def aggregate_impact(actions: list[ActionItem]) -> dict[str, float]:
    """Свёртка по unit-gap'ам — для информативных карточек.

    Возвращаем сумму unit_gap_azn по типам (положительное = возможный
    профит на единицу, отрицательное = потерянная маржа на единицу).
    Это per-unit агрегат, не «в месяц» — реальный месячный impact зависит
    от объёма продаж которого у нас нет.
    """
    out = {"opportunity": 0.0, "loss": 0.0, "total": 0.0, "count": 0}
    for a in actions:
        out["count"] += 1
        gap = a.unit_gap_azn or 0.0
        if gap > 0:
            out["opportunity"] += gap
        elif gap < 0:
            out["loss"] += gap
        out["total"] += gap
    return {k: round(v, 2) if isinstance(v, float) else v for k, v in out.items()}
