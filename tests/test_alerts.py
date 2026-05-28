"""Тесты движка алертов: detection, dedup, dispatch routing."""

from datetime import timedelta
from src._time import utcnow


from src import alerts
from src.storage import (
    AlertEvent,
    AlertRule,
    Match,
    PriceSnapshot,
    Product,
    Promo,
    Run,
)


def _add_run(s, started_at=None, status="ok") -> Run:
    r = Run(started_at=started_at or utcnow(), status=status)
    s.add(r)
    s.flush()
    return r


def _add_product(s, site, name, ext_id, canonical_id=None) -> Product:
    p = Product(
        site=site, external_id=ext_id, url=f"http://{site}.az/{ext_id}",
        name=name, name_normalized=name.lower(),
        canonical_id=canonical_id,
    )
    s.add(p)
    s.flush()
    return p


def _add_snap(s, run, product, price, discount_price=None):
    s.add(PriceSnapshot(
        run_id=run.id, product_id=product.id,
        price=price, discount_price=discount_price,
    ))
    s.flush()


def _make_match(s, name) -> Match:
    m = Match(canonical_name=name, confidence=1.0)
    s.add(m)
    s.flush()
    return m


def _add_rule(s, rule_type, params=None, channels=None, cooldown=12) -> AlertRule:
    r = AlertRule(
        name=f"Test {rule_type}",
        rule_type=rule_type,
        params=params or {},
        channels=channels or ["email"],
        cooldown_hours=cooldown,
        is_active=True,
    )
    s.add(r)
    s.flush()
    return r


def test_undercut_threshold_fires(db_session):
    m = _make_match(db_session, "Foo")
    p_client = _add_product(db_session, "pharmonline", "Foo", "ph", canonical_id=m.id)
    p_comp = _add_product(db_session, "aloe", "Foo", "al", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 10.0)
    _add_snap(db_session, run, p_comp, 8.0)  # 20% дешевле
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    db_session.commit()

    fired = alerts.evaluate_rules(db_session, run.id)
    assert len(fired) == 1
    assert fired[0].rule_type == "undercut_threshold"
    assert fired[0].severity == "critical"


def test_undercut_below_threshold_no_fire(db_session):
    m = _make_match(db_session, "Foo")
    p_client = _add_product(db_session, "pharmonline", "Foo", "ph", canonical_id=m.id)
    p_comp = _add_product(db_session, "aloe", "Foo", "al", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 10.0)
    _add_snap(db_session, run, p_comp, 9.8)  # 2% дешевле — ниже порога
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    db_session.commit()

    fired = alerts.evaluate_rules(db_session, run.id)
    assert fired == []


def test_dedup_within_cooldown(db_session):
    """Один и тот же event не должен дублироваться в течение cooldown."""
    m = _make_match(db_session, "Foo")
    p_client = _add_product(db_session, "pharmonline", "Foo", "ph", canonical_id=m.id)
    p_comp = _add_product(db_session, "aloe", "Foo", "al", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 10.0)
    _add_snap(db_session, run, p_comp, 8.0)
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0}, cooldown=12)
    db_session.commit()

    first = alerts.evaluate_rules(db_session, run.id)
    second = alerts.evaluate_rules(db_session, run.id)
    assert len(first) == 1
    assert len(second) == 0  # дубликат в течение 12ч


def test_dedup_after_cooldown_fires_again(db_session):
    """После cooldown_hours должен сработать снова."""
    m = _make_match(db_session, "Foo")
    p_client = _add_product(db_session, "pharmonline", "Foo", "ph", canonical_id=m.id)
    p_comp = _add_product(db_session, "aloe", "Foo", "al", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 10.0)
    _add_snap(db_session, run, p_comp, 8.0)
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0}, cooldown=1)
    db_session.commit()

    alerts.evaluate_rules(db_session, run.id)
    # "Сделать вид" что прошлый event был 2 часа назад
    ev = db_session.scalars(
        AlertEvent.__table__.select().limit(1)
    ).first()
    db_session.execute(
        AlertEvent.__table__.update()
        .values(created_at=utcnow() - timedelta(hours=2))
    )
    db_session.commit()
    second = alerts.evaluate_rules(db_session, run.id)
    assert len(second) == 1


def test_price_drop_detects_yesterday_to_today(db_session):
    p = _add_product(db_session, "aloe", "Foo", "al")
    yesterday = _add_run(db_session, utcnow() - timedelta(days=1))
    today = _add_run(db_session, utcnow())
    _add_snap(db_session, yesterday, p, 100.0)
    _add_snap(db_session, today, p, 70.0)  # 30% drop
    _add_rule(db_session, "price_drop_pct", {"min_pct": 10.0})
    db_session.commit()

    fired = alerts.evaluate_rules(db_session, today.id)
    assert len(fired) == 1
    assert fired[0].severity == "critical"
    assert fired[0].payload["drop_pct"] == 30.0


def test_new_product_detected(db_session):
    yesterday = _add_run(db_session, utcnow() - timedelta(days=1))
    today = _add_run(db_session, utcnow())
    old = _add_product(db_session, "aloe", "Old", "old")
    new = _add_product(db_session, "aloe", "New SKU", "new")
    _add_snap(db_session, yesterday, old, 5.0)
    _add_snap(db_session, today, old, 5.0)
    _add_snap(db_session, today, new, 7.0)
    _add_rule(db_session, "new_product")
    db_session.commit()

    fired = alerts.evaluate_rules(db_session, today.id)
    assert len(fired) == 1
    assert fired[0].rule_type == "new_product"
    assert "New SKU" in fired[0].title


def test_promo_started_detected(db_session):
    yesterday = _add_run(db_session, utcnow() - timedelta(days=1))
    today = _add_run(db_session, utcnow())
    db_session.add(Promo(run_id=yesterday.id, site="aloe", title="Old promo"))
    db_session.add(Promo(run_id=today.id, site="aloe", title="Old promo"))
    db_session.add(Promo(run_id=today.id, site="aloe", title="Brand New Sale"))
    _add_rule(db_session, "promo_started")
    db_session.commit()

    fired = alerts.evaluate_rules(db_session, today.id)
    new_promos = [f for f in fired if f.rule_type == "promo_started"]
    assert len(new_promos) == 1
    assert "Brand New Sale" in new_promos[0].title


def test_inactive_rule_not_evaluated(db_session):
    m = _make_match(db_session, "Foo")
    p_client = _add_product(db_session, "pharmonline", "Foo", "ph", canonical_id=m.id)
    p_comp = _add_product(db_session, "aloe", "Foo", "al", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 10.0)
    _add_snap(db_session, run, p_comp, 5.0)
    rule = _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    rule.is_active = False
    db_session.commit()

    fired = alerts.evaluate_rules(db_session, run.id)
    assert fired == []


def test_only_specific_rule_when_rule_ids_filter(db_session):
    m = _make_match(db_session, "Foo")
    p_client = _add_product(db_session, "pharmonline", "Foo", "ph", canonical_id=m.id)
    p_comp = _add_product(db_session, "aloe", "Foo", "al", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 10.0)
    _add_snap(db_session, run, p_comp, 5.0)
    r1 = _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    r2 = _add_rule(db_session, "price_raise_opportunity", {"min_pct": 5.0})
    db_session.commit()

    fired = alerts.evaluate_rules(db_session, run.id, rule_ids=[r1.id])
    assert all(f.rule_id == r1.id for f in fired)
