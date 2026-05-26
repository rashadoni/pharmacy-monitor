"""Прогнозирование цен — простой trend-based forecaster.

Без ML-зависимостей. Использует:
1. Линейная регрессия по последним N снимкам цен (numpy)
2. Скользящее среднее для сглаживания
3. Детекция трендов: rising / falling / stable

Когда наберётся 30+ дней истории — можно добавить seasonal decomposition / Prophet.
Сейчас простой подход покрывает 80% сценариев.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from src._time import utcnow
from typing import Literal

import structlog
from sqlalchemy import asc, select
from sqlalchemy.orm import Session

from src.storage import Match, PriceSnapshot, Product, Run, latest_snapshots_per_product

log = structlog.get_logger()

TrendDirection = Literal["rising", "falling", "stable"]


@dataclass
class PriceTrend:
    product_id: int
    site: str
    name: str
    n_points: int
    first_price: float
    last_price: float
    change_pct: float
    direction: TrendDirection
    forecast_7d_price: float | None
    confidence: Literal["low", "medium", "high"]


def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """y = a*x + b. Возвращает (slope, intercept). Pure Python — без numpy."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return 0.0, mean_y
    slope = num / den
    intercept = mean_y - slope * mean_x
    return slope, intercept


def _moving_average(values: list[float], window: int = 3) -> list[float]:
    """Скользящее среднее с шириной окна — сглаживает noise перед регрессией."""
    if len(values) <= window:
        return values[:]
    out = []
    for i in range(len(values)):
        lo = max(0, i - window // 2)
        hi = min(len(values), i + window // 2 + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _detect_seasonality(prices: list[float], timestamps: list) -> bool:
    """Грубая детекция: есть ли weekly паттерн.

    Если у нас >=14 дней истории и std day-of-week > std в среднем —
    есть сезонность (выходные/будни эффект). Возвращает True/False.
    """
    if len(prices) < 14:
        return False
    by_dow: dict[int, list[float]] = {}
    for p, t in zip(prices, timestamps):
        dow = t.weekday()
        by_dow.setdefault(dow, []).append(p)
    if len(by_dow) < 5:  # недостаточно дней разных типов
        return False
    means = []
    for dow_prices in by_dow.values():
        if dow_prices:
            means.append(sum(dow_prices) / len(dow_prices))
    if not means:
        return False
    overall_mean = sum(means) / len(means)
    variance = sum((m - overall_mean) ** 2 for m in means) / len(means)
    # Если коэфф. вариации > 5% — считаем что есть сезонность
    return overall_mean > 0 and (variance ** 0.5) / overall_mean > 0.05


def compute_trend(
    session: Session,
    product_id: int,
    *,
    days_window: int = 30,
    min_points: int = 3,
) -> PriceTrend | None:
    """Тренд по конкретному product_id за последние days_window дней.

    Diff-only-aware (2026-05-11): после оптимизации persist'а большинство
    продуктов имеют 0-2 snapshot'а в окне (цена стабильна → snapshot не
    пишется). Для них возвращаем тривиальный "stable" тренд если продукт
    действительно скрейпился недавно (`Product.last_seen_at` в окне), а
    не просто исчез с сайта.
    """
    cutoff = utcnow() - timedelta(days=days_window)
    snaps = session.scalars(
        select(PriceSnapshot)
        .join(Run, Run.id == PriceSnapshot.run_id)
        .where(
            PriceSnapshot.product_id == product_id,
            Run.status == "ok",
            Run.started_at >= cutoff,
        )
        .order_by(asc(Run.started_at))
    ).all()
    prices = []
    timestamps = []
    for s in snaps:
        p = s.discount_price or s.price
        if p is None or p <= 0:
            continue
        prices.append(p)
        timestamps.append(s.run.started_at)

    product = session.get(Product, product_id)
    if not product:
        return None

    # === Diff-only fast-path: продукт скрейпился, но цена не менялась ===
    if len(prices) < min_points:
        # Dead SKU: last_seen_at вне окна → не показываем trend вообще
        if product.last_seen_at < cutoff:
            return None

        # --- Case A: 0 снапшотов в окне ---
        # Продукт активен (last_seen_at свежий), но цена не менялась >days_window дней
        # → diff-only не писал снапшоты. Берём последнюю известную цену глобально.
        if not prices:
            snap_map = latest_snapshots_per_product(session, [product_id])
            latest_snap = snap_map.get(product_id)
            if latest_snap is None:
                return None
            p = latest_snap.discount_price or latest_snap.price
            if not p or p <= 0:
                return None
            return PriceTrend(
                product_id=product_id,
                site=product.site,
                name=product.name,
                n_points=0,
                first_price=round(p, 2),
                last_price=round(p, 2),
                change_pct=0.0,
                direction="stable",
                forecast_7d_price=round(p, 2),
                confidence="medium",
            )

        # --- Case B: 1-2 снапшота в окне, цена одинакова ---
        if len(set(round(p, 2) for p in prices)) == 1:
            p = prices[0]
            return PriceTrend(
                product_id=product_id,
                site=product.site,
                name=product.name,
                n_points=len(prices),
                first_price=round(p, 2),
                last_price=round(p, 2),
                change_pct=0.0,
                direction="stable",
                forecast_7d_price=round(p, 2),
                confidence="medium",
            )

        # --- Case C: 2 снапшота с разными ценами (реальный mover, мало точек) ---
        # Возвращаем направление и change_pct, но без прогноза (недостаточно точек).
        p1, p2 = prices[0], prices[-1]
        change_pct = (p2 - p1) / p1 * 100 if p1 else 0.0
        direction: TrendDirection = (
            "stable" if abs(change_pct) < 1.0
            else ("rising" if change_pct > 0 else "falling")
        )
        return PriceTrend(
            product_id=product_id,
            site=product.site,
            name=product.name,
            n_points=len(prices),
            first_price=round(p1, 2),
            last_price=round(p2, 2),
            change_pct=round(change_pct, 2),
            direction=direction,
            forecast_7d_price=None,  # мало точек для надёжного прогноза
            confidence="low",
        )

    first_p = prices[0]
    last_p = prices[-1]
    change_pct = (last_p - first_p) / first_p * 100 if first_p else 0.0

    # Сглаживание: 3-точечное moving average убирает single-point spikes
    smoothed = _moving_average(prices, window=3)

    # Линейная регрессия по timestamp в часах на сглаженных данных
    base_t = timestamps[0]
    xs = [(t - base_t).total_seconds() / 3600 for t in timestamps]
    slope, intercept = _linear_regression(xs, smoothed)

    # Forecast 7 дней вперёд (168ч)
    last_x = xs[-1]
    forecast_x = last_x + 7 * 24
    forecast_price = max(0.0, slope * forecast_x + intercept)

    # Если есть weekly seasonality — корректируем forecast средним за тот же weekday
    if _detect_seasonality(prices, timestamps):
        from datetime import timedelta as _td
        target_dow = (timestamps[-1] + _td(days=7)).weekday()
        same_dow_prices = [
            p for p, t in zip(prices, timestamps) if t.weekday() == target_dow
        ]
        if same_dow_prices:
            seasonal_avg = sum(same_dow_prices) / len(same_dow_prices)
            # Blend линейный прогноз и сезонное среднее 50/50
            forecast_price = (forecast_price + seasonal_avg) / 2

    # Confidence: по числу точек + variance
    if len(prices) >= 14:
        confidence: Literal["low", "medium", "high"] = "high"
    elif len(prices) >= 7:
        confidence = "medium"
    else:
        confidence = "low"

    # Direction: считаем по slope (₼/час)
    if abs(change_pct) < 1.0:
        direction: TrendDirection = "stable"
    elif change_pct > 0:
        direction = "rising"
    else:
        direction = "falling"

    return PriceTrend(
        product_id=product_id,
        site=product.site,
        name=product.name,
        n_points=len(prices),
        first_price=round(first_p, 2),
        last_price=round(last_p, 2),
        change_pct=round(change_pct, 2),
        direction=direction,
        forecast_7d_price=round(forecast_price, 2),
        confidence=confidence,
    )


def top_movers(
    session: Session,
    *,
    days_window: int = 30,
    min_change_pct: float = 5.0,
    limit: int = 50,
) -> list[PriceTrend]:
    """Товары с наибольшими движениями цены за окно (rising + falling).

    Оптимизировано: один SQL загружает все snapshots в окне, потом группируем
    по product_id в Python (вместо N запросов по одному product).
    """
    cutoff = utcnow() - timedelta(days=days_window)
    # Один query: все snapshots в окне с join'ом на Run для started_at
    rows = session.execute(
        select(
            PriceSnapshot.product_id,
            PriceSnapshot.price,
            PriceSnapshot.discount_price,
            Run.started_at,
        )
        .join(Run, Run.id == PriceSnapshot.run_id)
        .where(Run.status == "ok", Run.started_at >= cutoff)
        .order_by(Run.started_at)
    ).all()

    # Группировка: {product_id: [(price, ts), ...]}
    from collections import defaultdict
    history: dict[int, list[tuple[float, "datetime"]]] = defaultdict(list)
    for row in rows:
        pid, price, disc, ts = row
        eff = disc if disc else price
        if eff is None or eff <= 0:
            continue
        history[pid].append((eff, ts))

    if not history:
        return []

    # Diff-only gap: для продуктов с ровно 1 снапшотом в окне предыдущая цена
    # могла быть написана до cutoff (изменение только что произошло после долгой
    # стабильности). Подгружаем последний pre-cutoff снапшот → теперь видим
    # change_pct относительно реальной "старой" цены.
    single_snap_pids = [pid for pid, pts in history.items() if len(pts) == 1]
    if single_snap_pids:
        from sqlalchemy import func as _func
        # Для каждого product_id берём самый свежий снапшот ДО cutoff
        pre_rows = session.execute(
            select(
                PriceSnapshot.product_id,
                PriceSnapshot.price,
                PriceSnapshot.discount_price,
                Run.started_at,
            )
            .join(Run, Run.id == PriceSnapshot.run_id)
            .where(
                PriceSnapshot.product_id.in_(single_snap_pids),
                Run.status == "ok",
                Run.started_at < cutoff,
            )
            .order_by(PriceSnapshot.product_id, Run.started_at.desc())
        ).all()
        seen: set[int] = set()
        for row in pre_rows:
            pid, price, disc, ts = row
            if pid in seen:
                continue  # уже взяли самый свежий pre-cutoff
            seen.add(pid)
            eff = disc if disc else price
            if eff and eff > 0:
                history[pid].insert(0, (eff, ts))  # prepend как "старая" точка

    products = {p.id: p for p in session.scalars(
        select(Product).where(Product.id.in_(history.keys()))
    ).all()}

    trends: list[PriceTrend] = []
    for pid, points in history.items():
        # Diff-only-aware (2026-05-11): 2 точки достаточно для (last-first)/first.
        # Раньше 3 ставилось для устойчивости регрессии, но top_movers фильтрует
        # по `min_change_pct`, так что слабые шумы и так отсеются. После
        # diff-only product с одним big change имеет 2 точки — мы хотим его поймать.
        if len(points) < 2:
            continue
        product = products.get(pid)
        if not product:
            continue
        prices = [p for p, _ in points]
        timestamps = [ts for _, ts in points]

        first_p = prices[0]
        last_p = prices[-1]
        change_pct = (last_p - first_p) / first_p * 100 if first_p else 0.0
        if abs(change_pct) < min_change_pct:
            continue
        # Data-quality guard (2026-05-11): отсекаем артефакты старого aptekonline
        # parser bug'а — цена концатенировалась как «11 AZN 35.88 AZN» → 1135.88.
        # Реальные аптечные товары:
        # - стоят < 500 ₼ (есть исключения — мед.оборудование, но они не в movers)
        # - не падают/растут на > 95% за 30 дней (только если был typo).
        if first_p > 500 or last_p > 500:
            continue
        if abs(change_pct) > 95:
            continue

        base_t = timestamps[0]
        xs = [(t - base_t).total_seconds() / 3600 for t in timestamps]
        slope, intercept = _linear_regression(xs, prices)
        forecast_price = max(0.0, slope * (xs[-1] + 7 * 24) + intercept)

        if len(prices) >= 14:
            confidence: Literal["low", "medium", "high"] = "high"
        elif len(prices) >= 7:
            confidence = "medium"
        else:
            confidence = "low"

        if abs(change_pct) < 1.0:
            direction: TrendDirection = "stable"
        elif change_pct > 0:
            direction = "rising"
        else:
            direction = "falling"

        trends.append(PriceTrend(
            product_id=pid, site=product.site, name=product.name,
            n_points=len(prices),
            first_price=round(first_p, 2), last_price=round(last_p, 2),
            change_pct=round(change_pct, 2),
            direction=direction,
            forecast_7d_price=round(forecast_price, 2),
            confidence=confidence,
        ))

    trends.sort(key=lambda x: -abs(x.change_pct))
    return trends[:limit]


@dataclass
class CompetitorMoveProbability:
    """Вероятность что конкурент опустит цену в ближайшие 7 дней.

    Эвристика: если конкурент уже снизил >threshold% за последние N дней —
    высокая вероятность что снижение продолжится.
    """
    canonical_id: int
    canonical_name: str
    competitor_site: str
    current_price: float
    trend_7d_change_pct: float
    probability: Literal["low", "medium", "high"]
    expected_next_price: float | None


def predict_competitor_moves(
    session: Session, *, days_window: int = 7, max_n: int = 30
) -> list[CompetitorMoveProbability]:
    """Где у конкурентов выраженный тренд снижения — там клиенту готовиться."""
    out: list[CompetitorMoveProbability] = []
    matches = session.scalars(select(Match)).all()
    for m in matches:
        for product in m.products:
            if product.site == "pharmonline":
                continue
            trend = compute_trend(session, product.id, days_window=days_window)
            if trend is None or trend.direction == "stable":
                continue
            if trend.direction != "falling":
                continue
            # Высокая вероятность если падение >5% за неделю
            abs_change = abs(trend.change_pct)
            if abs_change > 10:
                prob: Literal["low", "medium", "high"] = "high"
            elif abs_change > 5:
                prob = "medium"
            else:
                continue  # слишком мало
            out.append(CompetitorMoveProbability(
                canonical_id=m.id,
                canonical_name=m.canonical_name,
                competitor_site=product.site,
                current_price=trend.last_price,
                trend_7d_change_pct=trend.change_pct,
                probability=prob,
                expected_next_price=trend.forecast_7d_price,
            ))
    out.sort(key=lambda x: x.trend_7d_change_pct)  # сильнее падение → выше
    return out[:max_n]
