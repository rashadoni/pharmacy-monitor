"""Тесты движка алертов: detection, dedup, dispatch routing."""

from contextlib import contextmanager

from datetime import timedelta
from src._time import utcnow


from src import alerts, storage
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
    started_at = started_at or utcnow()
    r = Run(
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=1),
        status=status,
        catalog_scope="full" if status == "ok" else "unknown",
        full_catalog_sites="pharmonline,aptekonline,aloe" if status == "ok" else None,
        catalog_verified=status == "ok",
        run_quality=(
            {
                "baseline_enforced": True,
                "full_catalog_verified": True,
                "financially_eligible": True,
                "sites": {
                    "pharmonline": {"status": "ok"},
                    "aptekonline": {"status": "ok"},
                    "aloe": {"status": "ok"},
                },
            }
            if status == "ok"
            else None
        ),
    )
    s.add(r)
    s.flush()
    return r


def test_evaluate_rules_skips_degraded_and_intentional_partial_runs(db_session):
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    degraded = _add_run(db_session, status="degraded")
    partial = _add_run(db_session, status="ok")
    partial.run_quality = {"financially_eligible": False}
    db_session.commit()

    assert alerts.evaluate_rules(db_session, degraded.id) == []
    assert alerts.evaluate_rules(db_session, partial.id) == []


def _add_product(s, site, name, ext_id, canonical_id=None) -> Product:
    p = Product(
        site=site,
        external_id=ext_id,
        url=f"http://{site}.az/{ext_id}",
        name=name,
        name_normalized=name.lower(),
        canonical_id=canonical_id,
    )
    s.add(p)
    s.flush()
    return p


def _add_snap(s, run, product, price, discount_price=None):
    s.add(
        PriceSnapshot(
            run_id=run.id,
            product_id=product.id,
            price=price,
            discount_price=discount_price,
        )
    )
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
    # Explicit run_id is the trusted in-pipeline path and intentionally works
    # before run.finished_at is stamped after post-processing.
    run.finished_at = None
    _add_snap(db_session, run, p_client, 10.0)
    _add_snap(db_session, run, p_comp, 8.0)  # 20% дешевле
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    db_session.commit()

    fired = alerts.evaluate_rules(db_session, run.id)
    assert len(fired) == 1
    assert fired[0].rule_type == "undercut_threshold"
    assert fired[0].severity == "critical"


def test_auto_evaluate_selects_latest_completed_fresh_run(db_session):
    match = _make_match(db_session, "Auto trusted")
    client = _add_product(
        db_session,
        "pharmonline",
        "Auto trusted",
        "auto-ph",
        canonical_id=match.id,
    )
    competitor = _add_product(
        db_session,
        "aloe",
        "Auto trusted",
        "auto-al",
        canonical_id=match.id,
    )
    run = _add_run(db_session)
    _add_snap(db_session, run, client, 10.0)
    _add_snap(db_session, run, competitor, 8.0)
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    db_session.commit()

    fired = alerts.evaluate_rules(db_session)

    assert len(fired) == 1
    assert fired[0].rule_type == "undercut_threshold"


def test_auto_evaluate_blocks_active_and_failed_newer_full_run(db_session):
    match = _make_match(db_session, "Auto blocked during run")
    client = _add_product(
        db_session,
        "pharmonline",
        "Auto blocked during run",
        "blocked-ph",
        canonical_id=match.id,
    )
    competitor = _add_product(
        db_session,
        "aloe",
        "Auto blocked during run",
        "blocked-al",
        canonical_id=match.id,
    )
    completed = _add_run(db_session, utcnow() - timedelta(hours=1))
    _add_snap(db_session, completed, client, 10.0)
    _add_snap(db_session, completed, competitor, 8.0)
    unfinished = _add_run(db_session)
    unfinished.finished_at = None
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    db_session.commit()

    # Run A would produce a valid event, but external auto-evaluation must not
    # read mutable Product/Match state while Run B is still changing it.
    assert alerts.evaluate_rules(db_session) == []

    unfinished.finished_at = utcnow()
    unfinished.status = "failed"
    unfinished.run_quality = None
    db_session.commit()

    # A terminal full attempt that failed before classification is still the
    # newest declared producer.  Falling back to Run A would evaluate alerts
    # against Product state that Run B may already have changed.
    assert alerts.evaluate_rules(db_session) == []


def test_auto_evaluate_skips_when_scrape_lock_is_busy(db_session, monkeypatch):
    @contextmanager
    def busy_lock(_session):
        yield False

    monkeypatch.setattr(alerts, "try_shared_scrape_read_lock", busy_lock)

    assert alerts.evaluate_rules(db_session) == []


def test_auto_evaluate_does_not_mix_completed_run_with_unfinished_snapshots(db_session):
    match = _make_match(db_session, "Snapshot boundary")
    client = _add_product(
        db_session,
        "pharmonline",
        "Snapshot boundary",
        "boundary-ph",
        canonical_id=match.id,
    )
    competitor = _add_product(
        db_session,
        "aloe",
        "Snapshot boundary",
        "boundary-al",
        canonical_id=match.id,
    )
    completed = _add_run(db_session, utcnow() - timedelta(hours=1))
    _add_snap(db_session, completed, client, 10.0)
    _add_snap(db_session, completed, competitor, 12.0)
    unfinished = _add_run(db_session, utcnow())
    unfinished.finished_at = None
    _add_snap(db_session, unfinished, client, 10.0)
    _add_snap(db_session, unfinished, competitor, 5.0)
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    db_session.commit()

    assert alerts.evaluate_rules(db_session) == []

    explicit = alerts.evaluate_rules(db_session, unfinished.id)
    assert len(explicit) == 1
    assert explicit[0].payload["competitor_price"] == 5.0


def test_auto_evaluate_rejects_superseded_eligible_run(db_session):
    trusted = _add_run(db_session, utcnow() - timedelta(hours=1))
    _add_rule(db_session, "new_product")
    degraded_at = utcnow()
    db_session.add(
        Run(
            started_at=degraded_at,
            finished_at=degraded_at,
            status="degraded",
            run_quality={
                "baseline_enforced": True,
                "full_catalog_verified": False,
                "financially_eligible": False,
                "sites": {
                    "pharmonline": {"status": "degraded"},
                    "aptekonline": {"status": "ok"},
                    "aloe": {"status": "ok"},
                },
            },
        )
    )
    db_session.commit()

    assert storage.run_is_financially_eligible(trusted)
    assert alerts.evaluate_rules(db_session) == []


def test_undercut_prefetches_verified_snapshots_once(db_session, monkeypatch):
    run = _add_run(db_session)
    for index in range(2):
        match = _make_match(db_session, f"Product {index}")
        client = _add_product(
            db_session,
            "pharmonline",
            f"Product {index}",
            f"ph-{index}",
            canonical_id=match.id,
        )
        competitor = _add_product(
            db_session,
            "aloe",
            f"Product {index}",
            f"al-{index}",
            canonical_id=match.id,
        )
        _add_snap(db_session, run, client, 10.0)
        _add_snap(db_session, run, competitor, 8.0)
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    db_session.commit()

    original = storage.latest_snapshots_per_product
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "latest_snapshots_per_product", counted)
    assert len(alerts.evaluate_rules(db_session, run.id)) == 2
    assert calls == 1


def test_undercut_detector_does_not_leak_foreign_tenant_match(db_session):
    run = _add_run(db_session)
    foreign_match = Match(tenant_id=2, canonical_name="Foreign", confidence=1.0)
    db_session.add(foreign_match)
    db_session.flush()
    client = _add_product(
        db_session, "pharmonline", "Foreign", "foreign-ph", canonical_id=foreign_match.id
    )
    competitor = _add_product(
        db_session, "aloe", "Foreign", "foreign-al", canonical_id=foreign_match.id
    )
    client.tenant_id = 2
    competitor.tenant_id = 2
    _add_snap(db_session, run, client, 10.0)
    _add_snap(db_session, run, competitor, 1.0)
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    db_session.commit()

    assert alerts.evaluate_rules(db_session, run.id) == []


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


def test_undercut_explicit_oos_competitor_no_fire(db_session):
    match = _make_match(db_session, "OOS")
    client = _add_product(db_session, "pharmonline", "OOS", "ph", canonical_id=match.id)
    competitor = _add_product(db_session, "aloe", "OOS", "al", canonical_id=match.id)
    competitor.offer_availability_status = "out_of_stock"
    competitor.availability_observed_at = utcnow()
    run = _add_run(db_session)
    _add_snap(db_session, run, client, 10.0)
    _add_snap(db_session, run, competitor, 5.0)
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    db_session.commit()

    assert alerts.evaluate_rules(db_session, run.id) == []


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
    ev = db_session.scalars(AlertEvent.__table__.select().limit(1)).first()
    db_session.execute(
        AlertEvent.__table__.update().values(created_at=utcnow() - timedelta(hours=2))
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


def test_confirmed_watchlist_tick_only_emits_local_price_change(db_session):
    """A successful pinned-SKU tick cannot emit cross-site money advice."""
    product = _add_product(db_session, "aloe", "Pinned", "pinned")
    previous_full = _add_run(db_session, utcnow() - timedelta(days=1))
    watchlist_tick = Run(
        started_at=utcnow(),
        status="ok",
        catalog_scope="partial",
        catalog_verified=False,
        catalog_verification_reason="bounded_or_watchlist_run",
        run_quality={
            "mode": "watchlist",
            "financially_eligible": False,
            "sites": {"aloe": {"status": "ok", "items_failed": 0}},
        },
    )
    db_session.add(watchlist_tick)
    db_session.flush()
    _add_snap(db_session, previous_full, product, 100.0)
    _add_snap(db_session, watchlist_tick, product, 115.0)
    _add_rule(db_session, "price_change_pct", {"min_pct": 10.0})
    _add_rule(db_session, "undercut_threshold", {"min_pct": 5.0})
    db_session.commit()

    fired = alerts.evaluate_rules(db_session, watchlist_tick.id)

    assert storage.run_is_watchlist_price_alert_eligible(watchlist_tick)
    assert [event.rule_type for event in fired] == ["price_change_pct"]
    assert fired[0].payload["source_run_id"] == watchlist_tick.id
    assert fired[0].payload["direction"] == "up"
    assert fired[0].payload["change_pct"] == 15.0


def test_degraded_watchlist_tick_cannot_emit_price_drop(db_session):
    tick = Run(
        started_at=utcnow(),
        status="degraded",
        catalog_scope="partial",
        run_quality={"mode": "watchlist", "sites": {"aloe": {"status": "degraded"}}},
    )
    db_session.add(tick)
    _add_rule(db_session, "price_drop_pct", {"min_pct": 10.0})
    db_session.commit()

    assert not storage.run_is_watchlist_price_alert_eligible(tick)
    assert alerts.evaluate_rules(db_session, tick.id) == []


def test_price_drop_ignores_newer_partial_snapshot_as_baseline(db_session):
    product = _add_product(db_session, "aloe", "Trusted", "trusted")
    trusted = _add_run(db_session, utcnow() - timedelta(days=2))
    partial = _add_run(db_session, utcnow() - timedelta(days=1))
    partial.run_quality = {"financially_eligible": False}
    current = _add_run(db_session, utcnow())
    _add_snap(db_session, trusted, product, 100.0)
    _add_snap(db_session, partial, product, 200.0)
    _add_snap(db_session, current, product, 90.0)
    _add_rule(db_session, "price_drop_pct", {"min_pct": 20.0})
    db_session.commit()

    # Verified 100 → 90 is only 10%. The newer partial value 200 must not
    # manufacture a false 55% money alert.
    assert alerts.evaluate_rules(db_session, current.id) == []


def test_price_drop_explicit_oos_product_no_fire(db_session):
    product = _add_product(db_session, "aloe", "Unavailable", "oos")
    product.offer_availability_status = "out_of_stock"
    product.availability_observed_at = utcnow()
    yesterday = _add_run(db_session, utcnow() - timedelta(days=1))
    today = _add_run(db_session, utcnow())
    _add_snap(db_session, yesterday, product, 100.0)
    _add_snap(db_session, today, product, 50.0)
    _add_rule(db_session, "price_drop_pct", {"min_pct": 10.0})
    db_session.commit()

    assert alerts.evaluate_rules(db_session, today.id) == []


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


def test_new_product_explicit_oos_no_fire(db_session):
    today = _add_run(db_session, utcnow())
    product = _add_product(db_session, "aloe", "Unavailable new", "new-oos")
    product.offer_availability_status = "out_of_stock"
    product.availability_observed_at = utcnow()
    _add_snap(db_session, today, product, 7.0)
    _add_rule(db_session, "new_product")
    db_session.commit()

    assert alerts.evaluate_rules(db_session, today.id) == []


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


def test_promo_started_compares_previous_verified_run_of_same_site(db_session):
    aloe_previous = _add_run(db_session, utcnow() - timedelta(days=2))
    interleaved_aptek = _add_run(db_session, utcnow() - timedelta(days=1))
    current = _add_run(db_session, utcnow())
    aloe_previous.run_quality = {
        "financially_eligible": True,
        "sites": {"aloe": {"status": "ok"}},
    }
    interleaved_aptek.run_quality = {
        "financially_eligible": True,
        "sites": {"aptekonline": {"status": "ok"}},
    }
    current.run_quality = {
        "financially_eligible": True,
        "sites": {"aloe": {"status": "ok"}},
    }
    db_session.add_all(
        [
            Promo(run_id=aloe_previous.id, site="aloe", title="Existing"),
            Promo(run_id=interleaved_aptek.id, site="aptekonline", title="Other Site"),
            Promo(run_id=current.id, site="aloe", title="Existing"),
            Promo(run_id=current.id, site="aloe", title="New Aloe Promo"),
        ]
    )
    _add_rule(db_session, "promo_started")
    db_session.commit()

    fired = alerts.evaluate_rules(db_session, current.id)
    assert [event.payload["title"] for event in fired] == ["New Aloe Promo"]


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


# === DETECTOR — price_raise_opportunity ===


def test_price_raise_opportunity_fires_when_client_below_median(db_session):
    """Клиент дешевле медианы конкурентов на >= min_pct → fires."""
    m = _make_match(db_session, "Vitamins")
    p_client = _add_product(db_session, "pharmonline", "V", "ph", canonical_id=m.id)
    p_a = _add_product(db_session, "aptekonline", "V", "ap", canonical_id=m.id)
    p_o = _add_product(db_session, "aloe", "V", "al", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 8.0)
    _add_snap(db_session, run, p_a, 10.0)
    _add_snap(db_session, run, p_o, 11.0)
    db_session.commit()

    cands = alerts._detect_price_raise_opportunity(db_session, run.id, {"min_pct": 5.0})
    assert len(cands) == 1
    assert cands[0].severity == "info"
    assert "Vitamins" in cands[0].title
    # gap_pct = (10 - 8) / 8 * 100 = 25% (median = 10)
    assert cands[0].payload["gap_pct"] >= 24.0


def test_price_raise_opportunity_no_fire_when_below_min_pct(db_session):
    """Gap < min_pct → no fire."""
    m = _make_match(db_session, "X")
    p_client = _add_product(db_session, "pharmonline", "X", "ph", canonical_id=m.id)
    p_a = _add_product(db_session, "aloe", "X", "al", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 9.5)
    _add_snap(db_session, run, p_a, 10.0)  # 5.3% gap
    db_session.commit()
    cands = alerts._detect_price_raise_opportunity(db_session, run.id, {"min_pct": 10.0})
    assert cands == []


def test_price_raise_opportunity_no_fire_when_client_higher(db_session):
    """Клиент дороже конкурентов → не предлагаем поднять (бессмысленно)."""
    m = _make_match(db_session, "X")
    p_client = _add_product(db_session, "pharmonline", "X", "ph", canonical_id=m.id)
    p_a = _add_product(db_session, "aloe", "X", "al", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 12.0)
    _add_snap(db_session, run, p_a, 10.0)
    db_session.commit()
    cands = alerts._detect_price_raise_opportunity(db_session, run.id, {"min_pct": 5.0})
    assert cands == []


def test_price_raise_opportunity_skips_match_without_competitor(db_session):
    """Only-client match → не fire (нечего сравнивать)."""
    m = _make_match(db_session, "X")
    p_client = _add_product(db_session, "pharmonline", "X", "ph", canonical_id=m.id)
    run = _add_run(db_session)
    _add_snap(db_session, run, p_client, 10.0)
    db_session.commit()
    cands = alerts._detect_price_raise_opportunity(db_session, run.id, {"min_pct": 5.0})
    assert cands == []


# === DISPATCH ===


def _stub_notifier(monkeypatch):
    """Подменяем notifier.send_email и send_telegram_message."""
    import src.notifier as real_notifier

    calls = {"email": [], "telegram": []}

    def fake_email(subject, html_body):
        calls["email"].append({"subject": subject, "html_body": html_body})

    def fake_telegram(chat_id, text, parse_mode="Markdown"):
        calls["telegram"].append({"chat_id": chat_id, "text": text})
        return True

    monkeypatch.setattr(real_notifier, "send_email", fake_email)
    monkeypatch.setattr(real_notifier, "send_telegram_message", fake_telegram)
    return calls


def test_dispatch_event_default_email_only(db_session, monkeypatch):
    """Event без rule (rule_id=None) → default email channel."""
    calls = _stub_notifier(monkeypatch)
    e = AlertEvent(
        severity="warning",
        title="Test alert",
        detail="Some detail",
        rule_type="test",
        dedup_key="test-1",
    )
    db_session.add(e)
    db_session.commit()
    result = alerts.dispatch_event(db_session, e)
    assert result == {"email": "sent"}
    assert len(calls["email"]) == 1
    assert "Test alert" in calls["email"][0]["subject"]
    assert e.channels_sent == ["email"]


def test_dispatch_event_email_and_telegram(db_session, monkeypatch):
    """rule.channels=['email','telegram'] + recipient с chat_id → отправка по обоим."""
    calls = _stub_notifier(monkeypatch)
    rule = _add_rule(db_session, "undercut_threshold", channels=["email", "telegram"])
    # Recipient с привязанным chat_id
    from src.storage import Recipient

    db_session.add(Recipient(email="user@x", is_active=True, telegram_chat_id="555"))
    e = AlertEvent(
        severity="critical",
        title="Big alert",
        detail="D",
        rule_type="undercut_threshold",
        dedup_key="k1",
        rule_id=rule.id,
    )
    db_session.add(e)
    db_session.commit()
    result = alerts.dispatch_event(db_session, e)
    assert result["email"] == "sent"
    assert "sent to 1/1" in result["telegram"]
    assert len(calls["email"]) == 1
    assert len(calls["telegram"]) == 1
    assert calls["telegram"][0]["chat_id"] == "555"


def test_dispatch_event_telegram_no_chat_ids_skipped(db_session, monkeypatch):
    """Если нет recipient'ов с chat_id → 'skipped: no telegram chat_ids'."""
    _stub_notifier(monkeypatch)
    rule = _add_rule(db_session, "undercut_threshold", channels=["telegram"])
    e = AlertEvent(
        severity="warning",
        title="T",
        detail="d",
        rule_type="undercut_threshold",
        dedup_key="k2",
        rule_id=rule.id,
    )
    db_session.add(e)
    db_session.commit()
    result = alerts.dispatch_event(db_session, e)
    assert "skipped" in result["telegram"]


def test_dispatch_event_email_smtp_failure_returns_error(db_session, monkeypatch):
    """SMTP-exception → result['email'] starts with 'error:'."""
    import src.notifier as real_notifier

    def boom(*_a, **_kw):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(real_notifier, "send_email", boom)

    rule = _add_rule(db_session, "undercut_threshold", channels=["email"])
    e = AlertEvent(
        severity="warning",
        title="Boom",
        detail="d",
        rule_type="undercut_threshold",
        dedup_key="k3",
        rule_id=rule.id,
    )
    db_session.add(e)
    db_session.commit()
    result = alerts.dispatch_event(db_session, e)
    assert result["email"].startswith("error:")


def test_dispatch_event_telegram_send_failure_returns_partial(db_session, monkeypatch):
    """Если send_telegram_message возвращает False для какого-то chat_id —
    counter sent < total."""
    import src.notifier as real_notifier
    from src.storage import Recipient

    def fail_tg(chat_id, text, parse_mode="Markdown"):
        return chat_id == "ok-chat"

    monkeypatch.setattr(real_notifier, "send_telegram_message", fail_tg)
    monkeypatch.setattr(real_notifier, "send_email", lambda *_a, **_kw: None)

    rule = _add_rule(db_session, "undercut_threshold", channels=["telegram"])
    db_session.add_all(
        [
            Recipient(email="a@x", is_active=True, telegram_chat_id="ok-chat"),
            Recipient(email="b@x", is_active=True, telegram_chat_id="bad-chat"),
        ]
    )
    e = AlertEvent(
        severity="warning",
        title="Multi",
        detail="d",
        rule_type="undercut_threshold",
        dedup_key="k4",
        rule_id=rule.id,
    )
    db_session.add(e)
    db_session.commit()
    result = alerts.dispatch_event(db_session, e)
    # 1 из 2 успешно
    assert "sent to 1/2" in result["telegram"]


def test_send_email_alert_unknown_severity_uses_dot(db_session, monkeypatch):
    """severity='??' → emoji '•' (fallback в _send_email_alert)."""
    calls = _stub_notifier(monkeypatch)
    e = AlertEvent(
        severity="strange",
        title="X",
        detail="d",
        rule_type="t",
        dedup_key="k",
    )
    db_session.add(e)
    db_session.commit()
    alerts.dispatch_event(db_session, e)
    assert "[STRANGE]" in calls["email"][0]["subject"]
