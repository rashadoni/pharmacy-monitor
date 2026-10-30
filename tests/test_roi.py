"""Тесты ROI/actions модуля на синтетических данных."""

from contextlib import contextmanager

from datetime import timedelta

from src._time import utcnow

from src import roi, storage
from src.product_policy import policy_fingerprint, trusted_catalog_epoch
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


def _eligible_quality() -> dict:
    return {
        "version": 1,
        "baseline_enforced": True,
        "full_catalog_verified": True,
        "financially_eligible": True,
        "sites": {
            "pharmonline": {"status": "ok"},
            "aptekonline": {"status": "ok"},
            "aloe": {"status": "ok"},
        },
    }


def _make_run(s, products_with_prices: list[tuple[Product, float]]) -> Run:
    now = utcnow()
    r = Run(
        started_at=now,
        finished_at=now,
        status="ok",
        run_quality=_eligible_quality(),
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
    )
    s.add(r)
    s.flush()
    for product, price in products_with_prices:
        s.add(PriceSnapshot(run_id=r.id, product_id=product.id, price=price))
    s.flush()
    return r


def _make_cluster(s, name: str, sites_prices: dict[str, float], run: Run | None = None) -> Match:
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
    now = utcnow()
    r = Run(
        started_at=now,
        finished_at=now,
        status="ok",
        run_quality=_eligible_quality(),
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
    )
    s.add(r)
    s.flush()
    return r


def _trusted_epoch(s) -> str:
    epoch = trusted_catalog_epoch(s)
    assert epoch is not None
    return epoch


def test_price_raise_when_client_cheaper(db_session):
    """Клиент дешевле — должна быть price_raise opportunity."""
    _make_cluster(
        db_session,
        "Aspirin",
        {"pharmonline": 5.00, "aptekonline": 7.00, "aloe": 6.50},
    )
    actions = roi.compute_actions(db_session)
    raise_actions = [a for a in actions if a.type == "price_raise"]
    assert len(raise_actions) == 1
    a = raise_actions[0]
    assert a.severity == "opportunity"
    assert a.current_value_azn == 5.00
    assert a.target_value_azn > 5.00
    assert a.unit_gap_azn is not None and a.unit_gap_azn > 0
    assert a.spread_pct is not None and a.spread_pct > 0
    # estimated_monthly_impact_azn deprecated — всегда 0
    assert a.estimated_monthly_impact_azn == 0


def test_undercut_when_competitor_cheaper(db_session):
    _make_cluster(
        db_session,
        "Paracetamol",
        {"pharmonline": 10.00, "aptekonline": 7.00, "aloe": 9.00},
    )
    actions = roi.compute_actions(db_session)
    undercut_actions = [a for a in actions if a.type == "undercut"]
    assert len(undercut_actions) == 1
    a = undercut_actions[0]
    assert a.severity in ("warning", "critical")
    assert a.current_value_azn == 10.00
    assert a.target_value_azn < 10.00
    # unit_gap отрицательный (мы теряем margin per unit)
    assert a.unit_gap_azn is not None and a.unit_gap_azn < 0
    assert a.spread_pct is not None and a.spread_pct < 0  # отрицательный — конкурент дешевле
    assert a.competitor_site == "aptekonline"


def test_undercut_severity_critical_when_drop_over_10pct(db_session):
    _make_cluster(
        db_session,
        "BigDrop",
        {"pharmonline": 100.00, "aptekonline": 80.00, "aloe": 90.00},
    )
    actions = roi.compute_actions(db_session)
    a = next(a for a in actions if a.type == "undercut")
    assert a.severity == "critical"


def test_no_actions_when_prices_close(db_session):
    """Если разница меньше threshold — никаких действий не предлагаем."""
    _make_cluster(
        db_session,
        "Equal",
        {"pharmonline": 10.00, "aptekonline": 10.10, "aloe": 9.95},
    )
    actions = roi.compute_actions(db_session, raise_threshold_pct=5.0, undercut_threshold_pct=3.0)
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
    now = utcnow()
    run = Run(
        started_at=now,
        finished_at=now,
        status="ok",
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
        run_quality=_eligible_quality(),
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        Promo(
            run_id=run.id,
            site="aloe",
            title="Big Sale 30%",
            landing_url="http://aloe.az/promo",
        )
    )
    db_session.commit()

    actions = roi.compute_actions(db_session)
    promo_actions = [a for a in actions if a.type == "promo_response"]
    assert len(promo_actions) == 1
    assert "aloe" in promo_actions[0].title


def test_promo_responses_use_latest_verified_run_per_competitor_site(db_session):
    now = utcnow()
    apt_run = Run(
        started_at=now,
        finished_at=now,
        status="ok",
        catalog_scope="full",
        full_catalog_sites="aptekonline",
        catalog_verified=True,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {"aptekonline": {"status": "ok"}},
        },
    )
    aloe_run = Run(
        started_at=now,
        finished_at=now,
        status="ok",
        catalog_scope="full",
        full_catalog_sites="aloe",
        catalog_verified=True,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {"aloe": {"status": "ok"}},
        },
    )
    pharm_run = Run(
        started_at=now,
        finished_at=now,
        status="ok",
        catalog_scope="full",
        full_catalog_sites="pharmonline",
        catalog_verified=True,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {"pharmonline": {"status": "ok"}},
        },
    )
    db_session.add_all([apt_run, aloe_run, pharm_run])
    db_session.flush()
    db_session.add_all(
        [
            Promo(run_id=apt_run.id, site="aptekonline", title="Apt Sale"),
            Promo(run_id=aloe_run.id, site="aloe", title="Aloe Sale"),
        ]
    )
    db_session.commit()

    promos = [item for item in roi.compute_actions(db_session) if item.type == "promo_response"]
    assert {(item.competitor_site, item.title) for item in promos} == {
        ("aptekonline", "Промо у aptekonline: Apt Sale"),
        ("aloe", "Промо у aloe: Aloe Sale"),
    }


def test_aggregate_impact_separates_opportunity_and_loss(db_session):
    run = _shared_run(db_session)
    _make_cluster(
        db_session,
        "RaiseUp",
        {"pharmonline": 5.00, "aptekonline": 7.00, "aloe": 6.50},
        run=run,
    )
    _make_cluster(
        db_session,
        "DropDown",
        {"pharmonline": 10.00, "aptekonline": 7.00, "aloe": 9.00},
        run=run,
    )
    actions = roi.compute_actions(db_session)
    agg = roi.aggregate_impact(actions)
    assert agg["opportunity"] > 0
    assert agg["loss"] < 0


def test_actions_sorted_by_severity_then_impact(db_session):
    run = _shared_run(db_session)
    _make_cluster(
        db_session,
        "BigUndercut",
        {"pharmonline": 100.00, "aptekonline": 80.00, "aloe": 100.00},
        run=run,
    )
    _make_cluster(
        db_session,
        "RaiseSmall",
        {"pharmonline": 5.00, "aptekonline": 6.00, "aloe": 5.50},
        run=run,
    )
    actions = roi.compute_actions(db_session)
    # critical undercut должен идти первым
    assert actions[0].type == "undercut"
    assert actions[0].severity == "critical"


def test_compute_actions_for_aloe_client_inverts_perspective(db_session):
    """client_site='aloe' → действия считаются с точки зрения aloe.

    Сценарий: aloe дешевле всех → price_raise для aloe.
    Раньше (hardcoded pharmonline) для этого же сценария был бы undercut.
    """
    _make_cluster(
        db_session,
        "ChepAloe",
        {"pharmonline": 12.00, "aptekonline": 11.00, "aloe": 8.00},
    )
    actions = roi.compute_actions(db_session, client_site="aloe")
    raise_actions = [a for a in actions if a.type == "price_raise"]
    assert len(raise_actions) == 1, "aloe дешевле всех — должна быть opportunity"
    a = raise_actions[0]
    assert a.current_value_azn == 8.00
    assert a.target_value_azn > 8.00


def test_compute_actions_aloe_undercut_when_competitor_cheaper(db_session):
    """С перспективы aloe: если pharmonline/apt дешевле — это undercut для aloe."""
    _make_cluster(
        db_session,
        "DearAloe",
        {"pharmonline": 5.00, "aptekonline": 6.00, "aloe": 10.00},
    )
    actions = roi.compute_actions(db_session, client_site="aloe")
    undercut = [a for a in actions if a.type == "undercut"]
    assert len(undercut) == 1
    a = undercut[0]
    assert a.competitor_site == "pharmonline"  # самый дешёвый конкурент для aloe
    assert a.spread_pct is not None and a.spread_pct < 0


def test_compute_actions_default_pharmonline_unchanged(db_session):
    """Без параметра client_site дефолт pharmonline — backwards-compat."""
    _make_cluster(
        db_session,
        "Default",
        {"pharmonline": 5.00, "aptekonline": 7.00, "aloe": 6.50},
    )
    actions_default = roi.compute_actions(db_session)
    actions_explicit = roi.compute_actions(db_session, client_site="pharmonline")
    assert len(actions_default) == len(actions_explicit)
    # Снимем поля которые могут отличаться по ссылке — типов одинаковое
    assert sorted(a.type for a in actions_default) == sorted(a.type for a in actions_explicit)


def test_compute_actions_aloe_assortment_gap_excludes_aloe_products(db_session):
    """Когда client=aloe — gap должен показывать pharmonline/aptekonline продукты, не aloe."""
    p_aloe = _add_product(db_session, "aloe", "AloeOnly", "al-only")
    p_ph = _add_product(db_session, "pharmonline", "PhOnly", "ph-only")
    _make_run(db_session, [(p_aloe, 25.0), (p_ph, 20.0)])
    db_session.commit()

    actions = roi.compute_actions(db_session, client_site="aloe")
    gap_actions = [a for a in actions if a.type == "assortment_gap"]
    # client=aloe → конкуренты = (pharmonline, aptekonline). Только PhOnly должен быть в gap.
    assert len(gap_actions) == 1
    assert gap_actions[0].competitor_site == "pharmonline"
    assert "PhOnly" in gap_actions[0].title


# ─── ROI actions cache (P0.1 PO Audit 2026-05-17) ────────────────────────────


def test_cache_actions_round_trip(db_session):
    """compute_actions → cache_actions → get_cached_actions возвращает payload."""
    _make_cluster(
        db_session,
        "Aspirin",
        {"pharmonline": 5.00, "aptekonline": 7.00, "aloe": 6.50},
    )
    actions = roi.compute_actions(db_session, client_site="pharmonline")
    assert len(actions) >= 1

    eligible_run = _shared_run(db_session)
    db_session.commit()
    roi.cache_actions(db_session, "pharmonline", actions, run_id=eligible_run.id)

    cached = roi.get_cached_actions(db_session, "pharmonline")
    assert cached is not None
    assert len(cached) == len(actions)
    # Структура совпадает с HTTP response — те же ключи
    keys = set(cached[0].keys())
    assert {"type", "severity", "title", "spread_pct", "unit_gap_azn"} <= keys

    snapshot = roi.get_cached_actions_snapshot(db_session, "pharmonline")
    assert snapshot is not None
    payload, cache_row, source_run = snapshot
    assert payload == cached
    assert cache_row.run_id == eligible_run.id
    assert source_run.id == eligible_run.id


def test_get_cached_actions_returns_none_when_empty(db_session):
    """Если кэша нет — должен возвращать None, не raise."""
    assert roi.get_cached_actions(db_session, "pharmonline") is None


def test_get_cached_actions_returns_none_when_stale(db_session):
    """Кэш старше срока жизни → None. Срок — ритм сбора с запасом, 30ч."""
    from datetime import timedelta
    from src.storage import RoiActionsCache

    assert roi._CACHE_MAX_AGE_HOURS == 30
    eligible_run = _shared_run(db_session)
    cache_row = RoiActionsCache(
        tenant_id=1,
        client_site="pharmonline",
        payload=[{"type": "price_raise", "severity": "info", "title": "test"}],
        computed_at=utcnow() - timedelta(hours=31),
        run_id=eligible_run.id,
        policy_fingerprint=policy_fingerprint(),
        trust_epoch=_trusted_epoch(db_session),
    )
    db_session.add(cache_row)
    db_session.commit()

    assert roi.get_cached_actions(db_session, "pharmonline") is None
    # Но если повысить порог — возвращается
    assert roi.get_cached_actions(db_session, "pharmonline", max_age_hours=240) is not None

    cache_row.computed_at = utcnow() - timedelta(hours=29)
    db_session.commit()
    assert roi.get_cached_actions(db_session, "pharmonline") is not None


def test_cached_actions_survive_until_the_next_nightly_collection(db_session):
    """Рекомендации видны весь день до следующего ночного сбора и ещё с запасом.

    Кэш пишется только в конце подтверждённого полного сбора, а срок его жизни
    — ритм сбора с запасом (src/cadence.py). Срок короче ритма скрывал бы
    рекомендации до следующего сбора: при недельном ритме прежние 26ч прятали
    их со вторника до понедельника.
    """
    from datetime import timedelta
    from src.storage import RoiActionsCache

    collected_at = utcnow() - timedelta(hours=28)
    weekly_run = Run(
        started_at=collected_at,
        finished_at=collected_at,
        status="ok",
        run_quality=_eligible_quality(),
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
    )
    db_session.add(weekly_run)
    db_session.flush()
    db_session.add(
        RoiActionsCache(
            tenant_id=1,
            client_site="pharmonline",
            payload=[{"type": "price_raise", "severity": "info", "title": "test"}],
            computed_at=collected_at,
            run_id=weekly_run.id,
            policy_fingerprint=policy_fingerprint(),
            trust_epoch=_trusted_epoch(db_session),
        )
    )
    db_session.commit()

    assert roi.get_cached_actions(db_session, "pharmonline") is not None


def test_roi_cache_rejected_after_newer_degraded_full_attempt(db_session):
    from src.storage import RoiActionsCache

    cached_run = _shared_run(db_session)
    cached_run.started_at = utcnow() - timedelta(hours=2)
    cached_run.finished_at = utcnow() - timedelta(hours=1)
    db_session.add(
        RoiActionsCache(
            tenant_id=1,
            client_site="pharmonline",
            payload=[{"type": "price_raise", "severity": "info", "title": "trusted"}],
            computed_at=utcnow(),
            run_id=cached_run.id,
            policy_fingerprint=policy_fingerprint(),
            trust_epoch=_trusted_epoch(db_session),
        )
    )
    db_session.commit()
    assert roi.get_cached_actions(db_session, "pharmonline") is not None

    db_session.add(
        Run(
            tenant_id=1,
            started_at=utcnow() - timedelta(minutes=30),
            finished_at=utcnow(),
            status="degraded",
            catalog_scope="full",
            full_catalog_sites="pharmonline,aptekonline,aloe",
            catalog_verified=False,
            run_quality={
                "baseline_enforced": True,
                "full_catalog_verified": False,
                "financially_eligible": False,
                "sites": {"pharmonline": {"status": "degraded"}},
            },
        )
    )
    db_session.commit()

    assert roi.get_cached_actions(db_session, "pharmonline") is None


def test_per_site_attempt_chain_stays_closed_until_failed_site_recovers(db_session):
    cached_run = _shared_run(db_session)
    cached_run.started_at = utcnow() - timedelta(hours=3)
    cached_run.finished_at = utcnow() - timedelta(hours=3)
    db_session.commit()

    aptek_degraded = Run(
        tenant_id=1,
        started_at=utcnow() - timedelta(hours=2),
        finished_at=utcnow() - timedelta(hours=2),
        status="degraded",
        catalog_scope="full",
        full_catalog_sites="aptekonline",
        catalog_verified=False,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": False,
            "financially_eligible": False,
            "sites": {"aptekonline": {"status": "degraded"}},
        },
    )
    aloe_ok = Run(
        tenant_id=1,
        started_at=utcnow() - timedelta(hours=1),
        finished_at=utcnow() - timedelta(hours=1),
        status="ok",
        catalog_scope="full",
        full_catalog_sites="aloe",
        catalog_verified=True,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {"aloe": {"status": "ok"}},
        },
    )
    db_session.add_all([aptek_degraded, aloe_ok])
    db_session.commit()

    assert roi.financial_inputs_are_fresh(db_session, tenant_id=1) is False

    aptek_ok = Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=utcnow(),
        status="ok",
        catalog_scope="full",
        full_catalog_sites="aptekonline",
        catalog_verified=True,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {"aptekonline": {"status": "ok"}},
        },
    )
    db_session.add(aptek_ok)
    db_session.commit()

    assert roi.financial_inputs_are_fresh(db_session, tenant_id=1) is True


def test_roi_cache_tie_breaks_equal_completion_by_run_id(db_session):
    from src.storage import RoiActionsCache

    completed_at = utcnow()
    cached_run = _shared_run(db_session)
    cached_run.started_at = completed_at - timedelta(hours=1)
    cached_run.finished_at = completed_at
    db_session.add(
        RoiActionsCache(
            tenant_id=1,
            client_site="pharmonline",
            payload=[],
            computed_at=completed_at,
            run_id=cached_run.id,
            policy_fingerprint=policy_fingerprint(),
            trust_epoch=_trusted_epoch(db_session),
        )
    )
    db_session.flush()
    db_session.add(
        Run(
            tenant_id=1,
            started_at=completed_at - timedelta(minutes=30),
            finished_at=completed_at,
            status="degraded",
            catalog_scope="full",
            full_catalog_sites="aptekonline",
            catalog_verified=False,
            run_quality={
                "baseline_enforced": True,
                "full_catalog_verified": False,
                "financially_eligible": False,
                "sites": {"aptekonline": {"status": "degraded"}},
            },
        )
    )
    db_session.commit()

    assert roi.get_cached_actions(db_session, "pharmonline") is None


def test_roi_cache_fails_closed_during_new_full_attempt_until_refresh(db_session):
    from src.storage import RoiActionsCache

    completed_at = utcnow() - timedelta(hours=1)
    cached_run = _shared_run(db_session)
    cached_run.started_at = completed_at - timedelta(minutes=30)
    cached_run.finished_at = completed_at
    db_session.add(
        RoiActionsCache(
            tenant_id=1,
            client_site="pharmonline",
            payload=[{"type": "price_raise", "title": "trusted"}],
            computed_at=utcnow(),
            run_id=cached_run.id,
            policy_fingerprint=policy_fingerprint(),
            trust_epoch=_trusted_epoch(db_session),
        )
    )
    post_processing = Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=None,
        status="running",
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
        run_quality=_eligible_quality(),
    )
    db_session.add(post_processing)
    db_session.commit()

    assert roi.financial_inputs_are_fresh(db_session, tenant_id=1) is True
    assert roi.get_cached_actions(db_session, "pharmonline") is None

    post_processing.status = "ok"
    post_processing.finished_at = utcnow()
    db_session.commit()

    assert roi.financial_inputs_are_fresh(db_session, tenant_id=1) is True
    assert roi.get_cached_actions(db_session, "pharmonline") is None


def test_direct_compute_fails_closed_after_degraded_full_attempt(db_session):
    trusted = _shared_run(db_session)
    _make_cluster(
        db_session,
        "Fail closed",
        {"pharmonline": 10.0, "aloe": 8.0},
        run=trusted,
    )
    degraded_at = utcnow()
    db_session.add(
        Run(
            tenant_id=1,
            started_at=degraded_at,
            finished_at=degraded_at,
            status="degraded",
            catalog_scope="full",
            full_catalog_sites="pharmonline,aptekonline,aloe",
            catalog_verified=False,
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

    assert roi.compute_actions(db_session, client_site="pharmonline") == []


def test_direct_compute_ignores_unfinished_eligible_snapshots(db_session):
    match = Match(canonical_name="Snapshot boundary", confidence=1.0)
    db_session.add(match)
    db_session.flush()
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
    completed = _shared_run(db_session)
    completed.started_at = utcnow() - timedelta(hours=1)
    completed.finished_at = completed.started_at
    db_session.add_all(
        [
            PriceSnapshot(run_id=completed.id, product_id=client.id, price=10.0),
            PriceSnapshot(run_id=completed.id, product_id=competitor.id, price=12.0),
        ]
    )
    unfinished = Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=None,
        status="ok",
        run_quality=_eligible_quality(),
    )
    db_session.add(unfinished)
    db_session.flush()
    db_session.add_all(
        [
            PriceSnapshot(run_id=unfinished.id, product_id=client.id, price=10.0),
            PriceSnapshot(run_id=unfinished.id, product_id=competitor.id, price=5.0),
        ]
    )
    db_session.commit()

    actions = roi.compute_actions(db_session, client_site="pharmonline")

    assert not [item for item in actions if item.type == "undercut"]


def test_direct_compute_blocks_during_run_but_completed_cache_remains_available(db_session):
    completed = _shared_run(db_session)
    _make_cluster(
        db_session,
        "Mutable graph boundary",
        {"pharmonline": 10.0, "aptekonline": 7.0, "aloe": 9.0},
        run=completed,
    )
    actions = roi.compute_actions(db_session, client_site="pharmonline")
    assert actions
    roi.cache_actions(
        db_session,
        "pharmonline",
        actions,
        run_id=completed.id,
    )

    unfinished = Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=None,
        status="running",
    )
    db_session.add(unfinished)
    db_session.commit()

    assert roi.compute_actions(db_session, client_site="pharmonline") == []
    cached = roi.get_cached_actions(db_session, "pharmonline")
    assert cached is not None
    assert len(cached) == len(actions)


def test_direct_compute_skips_when_scrape_lock_is_busy(db_session, monkeypatch):
    @contextmanager
    def busy_lock(_session):
        yield False

    monkeypatch.setattr(roi, "try_shared_scrape_read_lock", busy_lock)

    assert roi.compute_actions(db_session, client_site="pharmonline") == []


def test_cache_actions_upserts_existing(db_session):
    """Второй cache_actions для same (tenant, site) обновляет, не дублирует."""
    from src.storage import RoiActionsCache

    _make_cluster(
        db_session,
        "Aspirin",
        {"pharmonline": 5.00, "aptekonline": 7.00, "aloe": 6.50},
    )
    actions = roi.compute_actions(db_session, client_site="pharmonline")

    first_run = _shared_run(db_session)
    db_session.commit()
    roi.cache_actions(db_session, "pharmonline", actions, run_id=first_run.id)
    second_run = _shared_run(db_session)
    db_session.commit()
    roi.cache_actions(db_session, "pharmonline", actions, run_id=second_run.id)

    rows = db_session.query(RoiActionsCache).filter_by(client_site="pharmonline").all()
    assert len(rows) == 1
    assert rows[0].run_id == second_run.id  # обновился


def test_refresh_all_cached_actions_covers_three_sites(db_session):
    """refresh_all_cached_actions создаёт 3 row (по одному на сайт)."""
    from src.storage import RoiActionsCache

    _make_cluster(
        db_session,
        "Aspirin",
        {"pharmonline": 5.00, "aptekonline": 7.00, "aloe": 6.50},
    )

    eligible_run = _shared_run(db_session)
    db_session.commit()
    summary = roi.refresh_all_cached_actions(db_session, run_id=eligible_run.id)
    assert set(summary.keys()) == {"pharmonline", "aptekonline", "aloe"}
    rows = db_session.query(RoiActionsCache).all()
    assert {r.client_site for r in rows} == {"pharmonline", "aptekonline", "aloe"}
    assert all(r.run_id == eligible_run.id for r in rows)


def test_roi_cache_rejects_unverified_run(db_session):
    run = Run(
        started_at=utcnow(),
        status="degraded",
        run_quality={"financially_eligible": False},
    )
    db_session.add(run)
    db_session.commit()

    import pytest

    with pytest.raises(ValueError, match="financially eligible"):
        roi.cache_actions(db_session, "pharmonline", [], run_id=run.id)


def test_roi_cache_rejects_eligible_run_from_another_tenant(db_session):
    run = Run(
        tenant_id=2,
        started_at=utcnow(),
        status="ok",
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
        run_quality=_eligible_quality(),
    )
    db_session.add(run)
    db_session.commit()

    import pytest

    with pytest.raises(ValueError, match="financially eligible"):
        roi.cache_actions(db_session, "pharmonline", [], run_id=run.id, tenant_id=1)


def test_roi_cache_waits_until_every_site_has_verified_full_scan(db_session):
    run = Run(
        started_at=utcnow(),
        status="ok",
        catalog_scope="full",
        full_catalog_sites="aloe",
        catalog_verified=True,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {"aloe": {"status": "ok"}},
        },
    )
    db_session.add(run)
    db_session.commit()

    import pytest

    with pytest.raises(ValueError, match="every site"):
        roi.cache_actions(db_session, "pharmonline", [], run_id=run.id)

    from src.storage import RoiActionsCache

    db_session.add(
        RoiActionsCache(
            tenant_id=1,
            client_site="pharmonline",
            payload=[],
            computed_at=utcnow(),
            run_id=run.id,
        )
    )
    db_session.commit()
    assert roi.get_cached_actions(db_session, "pharmonline") is None


def test_roi_cache_rejects_stale_verified_site_inputs(db_session):
    # 31ч — сразу за суточным порогом (ритм 24ч + запас 6ч, см. src/cadence.py);
    # 29ч — ещё в нём: порог входов рекомендаций следует карте ритма.
    run = Run(
        started_at=utcnow() - timedelta(hours=31),
        status="ok",
        finished_at=utcnow() - timedelta(hours=31),
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
        run_quality=_eligible_quality(),
    )
    db_session.add(run)
    db_session.commit()

    import pytest

    with pytest.raises(ValueError, match="fresh verified"):
        roi.cache_actions(db_session, "pharmonline", [], run_id=run.id)
    assert roi.financial_inputs_are_fresh(db_session, tenant_id=1) is False

    run.started_at = utcnow() - timedelta(hours=29)
    run.finished_at = run.started_at
    db_session.commit()
    assert roi.financial_inputs_are_fresh(db_session, tenant_id=1) is True


def test_latest_financial_snapshot_ignores_newer_partial_run(db_session):
    product = _add_product(db_session, "pharmonline", "Trusted", "trusted-1")
    trusted_run = _make_run(db_session, [(product, 10.0)])
    partial_run = Run(
        started_at=utcnow(),
        status="ok",
        run_quality={"full_catalog_verified": False, "financially_eligible": False},
    )
    db_session.add(partial_run)
    db_session.flush()
    trusted_snapshot = db_session.query(PriceSnapshot).filter_by(
        run_id=trusted_run.id,
        product_id=product.id,
    ).one()
    db_session.add(
        PriceSnapshot(
            run_id=partial_run.id,
            product_id=product.id,
            price=1.0,
            captured_at=trusted_snapshot.captured_at,
        )
    )
    db_session.commit()

    snapshots = storage.latest_snapshots_per_product(
        db_session,
        [product.id],
        financially_eligible_only=True,
    )
    assert snapshots[product.id].run_id == trusted_run.id
    assert snapshots[product.id].price == 10.0


# ─── MAP по бренду снят 2026-10-30 ────────────────────────────────────────────


def _add_product_with_brand(s, site, name, brand, ext_id, canonical_id=None):
    p = Product(
        site=site,
        external_id=ext_id,
        url=f"http://{site}.az/p/{ext_id}",
        name=name,
        name_normalized=name.lower(),
        brand=brand,
        canonical_id=canonical_id,
    )
    s.add(p)
    s.flush()
    return p


def test_other_products_of_the_brand_give_no_price_advice(db_session):
    """Случай со скриншота 2026-10-26: пачка Pampers на 12 штук против других
    товаров бренда (большая пачка, салфетки). Прежнее правило брало самую низкую
    цену конкурентов по бренду и советовало поднять 11.50 до 32.17 ₼. Того же
    товара у конкурентов нет — совета нет никакого."""
    big_pack = _add_product_with_brand(
        db_session, "aptekonline", "Uşaq bezi Pampers 4 Premium Care N34", "Pampers", "apt-34"
    )
    wipes = _add_product_with_brand(
        db_session, "aloe", "Nəm salfet Pampers №52", "Pampers", "aloe-52"
    )
    client = _add_product_with_brand(
        db_session, "pharmonline", 'Uşaq bezi "Pampers - 4" 9-14 kq № 12', "Pampers", "ph-12"
    )
    _make_run(
        db_session,
        [(big_pack, 32.50), (big_pack, 32.17), (big_pack, 33.00), (wipes, 34.00), (client, 11.50)],
    )
    db_session.commit()

    actions = roi.compute_actions(db_session)
    # Расчёт состоялся: товары конкурентов без пары у клиента — «нет в ассортименте».
    assert {a.type for a in actions} == {"assortment_gap"}
    assert [a for a in actions if a.product_name == client.name] == []


def test_same_product_cheaper_than_competitors_is_one_price_raise_card(db_session):
    """Тот же товар (кластер) дешевле у клиента — одна карточка «Подними цену».
    Прежнее правило по бренду давало на него вторую, красную, с другой целью."""
    match = Match(canonical_name="Aspirin Bayer 100", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    apt = _add_product_with_brand(
        db_session, "aptekonline", "Aspirin Bayer 100", "Bayer", "apt-1", canonical_id=match.id
    )
    aloe = _add_product_with_brand(
        db_session, "aloe", "Aspirin Bayer 100", "Bayer", "aloe-1", canonical_id=match.id
    )
    client = _add_product_with_brand(
        db_session, "pharmonline", "Aspirin Bayer 100", "Bayer", "ph-1", canonical_id=match.id
    )
    _make_run(db_session, [(apt, 10.50), (apt, 10.00), (aloe, 11.00), (client, 7.00)])
    db_session.commit()

    actions = roi.compute_actions(db_session)
    for_product = [a for a in actions if a.product_name == "Aspirin Bayer 100"]
    assert [a.type for a in for_product] == ["price_raise"]
    assert for_product[0].current_value_azn == 7.00


def test_cached_map_violation_written_before_removal_is_not_served(db_session):
    """Кэш, записанный до снятия типа, отдаёт остальные советы без MAP — сразу
    после выкладки, не дожидаясь пересчёта после ночного сбора."""
    _make_cluster(
        db_session,
        "Aspirin",
        {"pharmonline": 5.00, "aptekonline": 7.00, "aloe": 6.50},
    )
    actions = roi.compute_actions(db_session, client_site="pharmonline")
    assert actions
    eligible_run = _shared_run(db_session)
    db_session.commit()
    roi.cache_actions(db_session, "pharmonline", actions, run_id=eligible_run.id)

    row = db_session.query(storage.RoiActionsCache).one()
    stale = {
        "type": "map_violation",
        "severity": "critical",
        "title": "MAP: Pampers — поднять Uşaq bezi Pampers 4 № 12",
        "detail": "Floor конкурентов по бренду Pampers за 30д = 32.17 ₼.",
        "product_name": "Uşaq bezi Pampers 4 № 12",
        "current_value_azn": 11.5,
        "target_value_azn": 32.17,
        "unit_gap_azn": 20.67,
        "spread_pct": 64.6,
        "extra": {"brand": "Pampers", "floor_window_days": 30},
    }
    row.payload = [stale, *row.payload]
    db_session.commit()

    cached = roi.get_cached_actions(db_session, "pharmonline")
    assert cached is not None
    assert [a["type"] for a in cached] == [a.type for a in actions]
    payload, _row, _run = roi.get_cached_actions_snapshot(db_session, "pharmonline")
    assert payload == cached
    items = roi.get_cached_action_items(db_session, "pharmonline")
    assert [a.type for a in items] == [a.type for a in actions]
