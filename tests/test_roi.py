"""Тесты ROI/actions модуля на синтетических данных."""

from datetime import datetime
from src._time import utcnow

from src import roi
from src.storage import Match, PriceSnapshot, Product, Promo, Run


def _add_product(s, site, name, ext_id, canonical_id=None):
    p = Product(
        site=site,
        external_id=ext_id,
        url=f"http://{site}.az/p/{ext_id}",
        name=name,
        name_normalized=name.lower(),
        canonical_id=canonical_id,
    )
    s.add(p)
    s.flush()
    return p


def _make_run(s, products_with_prices: list[tuple[Product, float]]) -> Run:
    r = Run(started_at=utcnow(), status="ok")
    s.add(r)
    s.flush()
    for product, price in products_with_prices:
        s.add(PriceSnapshot(run_id=r.id, product_id=product.id, price=price))
    s.flush()
    return r


def _make_cluster(
    s, name: str, sites_prices: dict[str, float], run: Run | None = None
) -> Match:
    """Создать Match-кластер; если run передан — все snapshots привязываются к нему."""
    m = Match(canonical_name=name, confidence=1.0)
    s.add(m)
    s.flush()
    products_prices = []
    for site, price in sites_prices.items():
        p = _add_product(s, site, name, f"{site}-{name}", canonical_id=m.id)
        products_prices.append((p, price))
    s.flush()
    if run is None:
        _make_run(s, products_prices)
    else:
        for product, price in products_prices:
            s.add(PriceSnapshot(run_id=run.id, product_id=product.id, price=price))
        s.flush()
    s.commit()
    return m


def _shared_run(s) -> Run:
    """Создать пустой Run для совместного использования в тестах с несколькими кластерами."""
    r = Run(started_at=utcnow(), status="ok")
    s.add(r)
    s.flush()
    return r


def test_price_raise_when_client_cheaper(db_session):
    """Клиент дешевле — должна быть price_raise opportunity."""
    _make_cluster(
        db_session, "Aspirin",
        {"pharmonline": 5.00, "aptekonline": 7.00, "aloe": 6.50},
    )
    actions = roi.compute_actions(db_session, assumed_monthly_volume=30)
    raise_actions = [a for a in actions if a.type == "price_raise"]
    assert len(raise_actions) == 1
    a = raise_actions[0]
    assert a.severity == "opportunity"
    assert a.current_value_azn == 5.00
    assert a.target_value_azn > 5.00
    assert a.estimated_monthly_impact_azn > 0


def test_undercut_when_competitor_cheaper(db_session):
    _make_cluster(
        db_session, "Paracetamol",
        {"pharmonline": 10.00, "aptekonline": 7.00, "aloe": 9.00},
    )
    actions = roi.compute_actions(db_session, assumed_monthly_volume=30)
    undercut_actions = [a for a in actions if a.type == "undercut"]
    assert len(undercut_actions) == 1
    a = undercut_actions[0]
    assert a.severity in ("warning", "critical")
    assert a.current_value_azn == 10.00
    assert a.target_value_azn < 10.00
    # impact отрицательный (мы теряем margin)
    assert a.estimated_monthly_impact_azn < 0
    assert a.competitor_site == "aptekonline"


def test_undercut_severity_critical_when_drop_over_10pct(db_session):
    _make_cluster(
        db_session, "BigDrop",
        {"pharmonline": 100.00, "aptekonline": 80.00, "aloe": 90.00},
    )
    actions = roi.compute_actions(db_session)
    a = next(a for a in actions if a.type == "undercut")
    assert a.severity == "critical"


def test_no_actions_when_prices_close(db_session):
    """Если разница меньше threshold — никаких действий не предлагаем."""
    _make_cluster(
        db_session, "Equal",
        {"pharmonline": 10.00, "aptekonline": 10.10, "aloe": 9.95},
    )
    actions = roi.compute_actions(
        db_session, raise_threshold_pct=5.0, undercut_threshold_pct=3.0
    )
    assert all(a.type not in ("price_raise", "undercut") for a in actions)


def test_assortment_gap_detected(db_session):
    """Товары конкурентов без canonical_id должны попасть в gap."""
    p1 = _add_product(db_session, "aloe", "Exclusive Aloe Drug", "ex1")
    p2 = _add_product(db_session, "aptekonline", "Exclusive Aptek Drug", "ex2")
    _make_run(db_session, [(p1, 25.00), (p2, 15.00)])
    db_session.commit()

    actions = roi.compute_actions(db_session)
    gap_actions = [a for a in actions if a.type == "assortment_gap"]
    assert len(gap_actions) == 2


def test_promo_response_action(db_session):
    """Промо на сайте конкурента → promo_response action."""
    run = Run(started_at=utcnow(), status="ok")
    db_session.add(run)
    db_session.flush()
    db_session.add(Promo(
        run_id=run.id, site="aloe", title="Big Sale 30%",
        landing_url="http://aloe.az/promo",
    ))
    db_session.commit()

    actions = roi.compute_actions(db_session)
    promo_actions = [a for a in actions if a.type == "promo_response"]
    assert len(promo_actions) == 1
    assert "aloe" in promo_actions[0].title


def test_aggregate_impact_separates_opportunity_and_loss(db_session):
    run = _shared_run(db_session)
    _make_cluster(
        db_session, "RaiseUp",
        {"pharmonline": 5.00, "aptekonline": 7.00, "aloe": 6.50},
        run=run,
    )
    _make_cluster(
        db_session, "DropDown",
        {"pharmonline": 10.00, "aptekonline": 7.00, "aloe": 9.00},
        run=run,
    )
    actions = roi.compute_actions(db_session, assumed_monthly_volume=30)
    agg = roi.aggregate_impact(actions)
    assert agg["opportunity"] > 0
    assert agg["loss"] < 0


def test_actions_sorted_by_severity_then_impact(db_session):
    run = _shared_run(db_session)
    _make_cluster(
        db_session, "BigUndercut",
        {"pharmonline": 100.00, "aptekonline": 80.00, "aloe": 100.00},
        run=run,
    )
    _make_cluster(
        db_session, "RaiseSmall",
        {"pharmonline": 5.00, "aptekonline": 6.00, "aloe": 5.50},
        run=run,
    )
    actions = roi.compute_actions(db_session)
    # critical undercut должен идти первым
    assert actions[0].type == "undercut"
    assert actions[0].severity == "critical"
