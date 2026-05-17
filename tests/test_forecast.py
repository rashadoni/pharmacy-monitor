"""Тесты forecast: линейная регрессия + детекция тренда + вероятности."""

from datetime import datetime, timedelta
from src._time import utcnow

from src import forecast
from src.storage import Match, PriceSnapshot, Product, Run


def _add_run(s, started_at):
    r = Run(started_at=started_at, status="ok")
    s.add(r)
    s.flush()
    return r


def _add_product(s, site, name, ext_id, canonical_id=None):
    p = Product(
        site=site, external_id=ext_id, url=f"http://x/{ext_id}",
        name=name, name_normalized=name.lower(), canonical_id=canonical_id,
    )
    s.add(p)
    s.flush()
    return p


def _add_history(s, product, prices_per_day):
    """Создать N runs (по дням назад) с указанными ценами для product."""
    base = utcnow()
    for days_ago, price in enumerate(reversed(prices_per_day)):
        run = _add_run(s, base - timedelta(days=days_ago))
        s.add(PriceSnapshot(run_id=run.id, product_id=product.id, price=price))
    s.flush()


def test_linear_regression_simple():
    slope, intercept = forecast._linear_regression([0, 1, 2, 3], [10, 12, 14, 16])
    assert slope == 2.0
    assert intercept == 10.0


def test_linear_regression_single_point():
    slope, intercept = forecast._linear_regression([5.0], [100.0])
    assert slope == 0.0
    assert intercept == 100.0


def test_compute_trend_returns_none_when_too_few_points(db_session):
    p = _add_product(db_session, "aloe", "X", "x1")
    _add_history(db_session, p, [10.0])  # только 1 точка
    # last_seen_at в далёком прошлом → не stable, а dead SKU
    p.last_seen_at = utcnow() - timedelta(days=100)
    db_session.commit()
    t = forecast.compute_trend(db_session, p.id, min_points=3)
    assert t is None


def test_compute_trend_stable_single_snapshot_seen_recently(db_session):
    """Diff-only: продукт с 1 snapshot + recent last_seen_at → stable forecast.

    Под diff-only stable продукты имеют 0-2 snapshots. Раньше возвращало
    None для них, и trend coverage обвалилась.
    """
    p = _add_product(db_session, "aloe", "Stable1", "s1")
    _add_history(db_session, p, [12.5])  # 1 точка
    p.last_seen_at = utcnow() - timedelta(hours=2)  # видели час-два назад
    db_session.commit()

    t = forecast.compute_trend(db_session, p.id, min_points=3)
    assert t is not None
    assert t.direction == "stable"
    assert t.last_price == 12.5
    assert t.forecast_7d_price == 12.5
    assert t.change_pct == 0.0
    assert t.confidence == "medium"


def test_compute_trend_stable_two_same_price_snapshots(db_session):
    """2 snapshot'а с одинаковой ценой → stable (а не None)."""
    p = _add_product(db_session, "aloe", "Stable2", "s2")
    _add_history(db_session, p, [8.0, 8.0])  # 2 точки, цена не менялась
    p.last_seen_at = utcnow() - timedelta(hours=1)
    db_session.commit()

    t = forecast.compute_trend(db_session, p.id, min_points=3)
    assert t is not None
    assert t.direction == "stable"
    assert t.n_points == 2


def test_compute_trend_stable_when_last_snapshot_outside_window(db_session):
    """Diff-only edge case: продукт виден сегодня, но последний snapshot 60 дней назад.

    Сценарий: generic medicine со стабильной ценой 90+ дней. Под diff-only
    snapshot пишется только при изменении, поэтому в окне 30 дней может быть
    0 snapshots. Раньше compute_trend возвращал None для таких продуктов —
    coverage тренд-графиков просела. Fix: fallback на latest_snapshots_per_product
    когда `last_seen_at` свежий.
    """
    p = _add_product(db_session, "aloe", "VeryStable", "vs1")
    # 1 snapshot, 60 дней назад
    base = utcnow()
    old_run = _add_run(db_session, base - timedelta(days=60))
    db_session.add(PriceSnapshot(run_id=old_run.id, product_id=p.id, price=5.0))
    # Продукт всё ещё виден сегодня
    p.last_seen_at = utcnow() - timedelta(hours=2)
    db_session.commit()

    # Окно 30 дней — snapshot не попадает, но fallback на latest снаружи окна
    t = forecast.compute_trend(db_session, p.id, days_window=30, min_points=3)
    assert t is not None
    assert t.direction == "stable"
    assert t.last_price == 5.0
    assert t.first_price == 5.0
    assert t.forecast_7d_price == 5.0
    assert t.change_pct == 0.0
    assert t.confidence == "medium"
    assert t.n_points == 1


def test_compute_trend_returns_none_if_not_seen_recently(db_session):
    """Продукт исчез с сайта (last_seen_at давно) → None даже с 1 snapshot.

    Защита от dead SKUs: не показываем "stable" forecast для товаров,
    которые перестали скрейпиться.
    """
    p = _add_product(db_session, "aloe", "Dead", "d1")
    _add_history(db_session, p, [15.0])
    p.last_seen_at = utcnow() - timedelta(days=60)  # 60 дней назад
    db_session.commit()

    t = forecast.compute_trend(db_session, p.id, days_window=30, min_points=3)
    assert t is None


def test_compute_trend_falling(db_session):
    p = _add_product(db_session, "aloe", "X", "x1")
    _add_history(db_session, p, [10.0, 9.5, 9.0, 8.5, 8.0])
    db_session.commit()
    t = forecast.compute_trend(db_session, p.id)
    assert t is not None
    assert t.direction == "falling"
    assert t.change_pct < -10
    assert t.forecast_7d_price is not None


def test_compute_trend_rising(db_session):
    p = _add_product(db_session, "aloe", "Y", "y1")
    _add_history(db_session, p, [10.0, 10.5, 11.0, 11.5, 12.0])
    db_session.commit()
    t = forecast.compute_trend(db_session, p.id)
    assert t.direction == "rising"
    assert t.change_pct > 10


def test_compute_trend_stable(db_session):
    p = _add_product(db_session, "aloe", "Z", "z1")
    _add_history(db_session, p, [10.0, 10.05, 10.0, 10.1, 10.0])
    db_session.commit()
    t = forecast.compute_trend(db_session, p.id)
    assert t.direction == "stable"


def test_top_movers_filters_by_min_change(db_session):
    p_big = _add_product(db_session, "aloe", "Big", "big")
    p_small = _add_product(db_session, "aloe", "Small", "small")
    _add_history(db_session, p_big, [10.0, 8.0, 6.0, 4.0])  # -60%
    _add_history(db_session, p_small, [10.0, 10.05, 10.1, 10.15])  # +1.5%
    db_session.commit()

    movers = forecast.top_movers(db_session, min_change_pct=5.0)
    assert len(movers) == 1
    assert movers[0].name == "Big"


def test_top_movers_includes_2point_real_movers(db_session):
    """Diff-only: продукт с одним big-change (2 snapshot'а) — попадает в movers.

    Раньше требовалось ≥3 точек → mover с одним резким изменением
    игнорировался. После diff-only такие — норма (snapshot пишется при
    каждом изменении, между ними тишина).
    """
    p = _add_product(db_session, "aloe", "TwoPointMover", "tpm")
    _add_history(db_session, p, [10.0, 5.0])  # ровно 2 точки, -50%
    db_session.commit()

    movers = forecast.top_movers(db_session, min_change_pct=5.0)
    assert any(m.name == "TwoPointMover" for m in movers)
    m = next(m for m in movers if m.name == "TwoPointMover")
    assert m.change_pct == -50.0
    assert m.direction == "falling"


def test_predict_competitor_moves(db_session):
    """Конкурент с падающей ценой → high probability."""
    m = Match(canonical_name="Foo", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    client = _add_product(db_session, "pharmonline", "Foo", "ph", canonical_id=m.id)
    comp = _add_product(db_session, "aloe", "Foo", "al", canonical_id=m.id)
    _add_history(db_session, client, [10.0] * 5)  # стабильно
    _add_history(db_session, comp, [12.0, 11.0, 10.0, 9.0, 8.0])  # падает -33%
    db_session.commit()

    moves = forecast.predict_competitor_moves(db_session)
    assert len(moves) >= 1
    m_pred = moves[0]
    assert m_pred.competitor_site == "aloe"
    assert m_pred.probability == "high"
    assert m_pred.trend_7d_change_pct < 0
