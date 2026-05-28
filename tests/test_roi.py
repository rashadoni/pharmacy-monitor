"""Тесты ROI/actions модуля на синтетических данных."""

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
    r = Run(started_at=utcnow(), status="ok")
    s.add(r)
    s.flush()
    return r


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
    run = Run(started_at=utcnow(), status="ok")
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

    roi.cache_actions(db_session, "pharmonline", actions, run_id=42)

    cached = roi.get_cached_actions(db_session, "pharmonline")
    assert cached is not None
    assert len(cached) == len(actions)
    # Структура совпадает с HTTP response — те же ключи
    keys = set(cached[0].keys())
    assert {"type", "severity", "title", "spread_pct", "unit_gap_azn"} <= keys


def test_get_cached_actions_returns_none_when_empty(db_session):
    """Если кэша нет — должен возвращать None, не raise."""
    assert roi.get_cached_actions(db_session, "pharmonline") is None


def test_get_cached_actions_returns_none_when_stale(db_session):
    """Кэш старше max_age_hours → None (forces fallback на inline compute)."""
    from datetime import timedelta
    from src.storage import RoiActionsCache

    db_session.add(
        RoiActionsCache(
            tenant_id=1,
            client_site="pharmonline",
            payload=[{"type": "price_raise", "severity": "info", "title": "test"}],
            computed_at=utcnow() - timedelta(hours=48),
            run_id=1,
        )
    )
    db_session.commit()

    assert roi.get_cached_actions(db_session, "pharmonline") is None
    # Но если повысить порог — возвращается
    assert roi.get_cached_actions(db_session, "pharmonline", max_age_hours=72) is not None


def test_cache_actions_upserts_existing(db_session):
    """Второй cache_actions для same (tenant, site) обновляет, не дублирует."""
    from src.storage import RoiActionsCache

    _make_cluster(
        db_session,
        "Aspirin",
        {"pharmonline": 5.00, "aptekonline": 7.00, "aloe": 6.50},
    )
    actions = roi.compute_actions(db_session, client_site="pharmonline")

    roi.cache_actions(db_session, "pharmonline", actions, run_id=1)
    roi.cache_actions(db_session, "pharmonline", actions, run_id=2)

    rows = db_session.query(RoiActionsCache).filter_by(client_site="pharmonline").all()
    assert len(rows) == 1
    assert rows[0].run_id == 2  # обновился


def test_refresh_all_cached_actions_covers_three_sites(db_session):
    """refresh_all_cached_actions создаёт 3 row (по одному на сайт)."""
    from src.storage import RoiActionsCache

    _make_cluster(
        db_session,
        "Aspirin",
        {"pharmonline": 5.00, "aptekonline": 7.00, "aloe": 6.50},
    )

    summary = roi.refresh_all_cached_actions(db_session, run_id=99)
    assert set(summary.keys()) == {"pharmonline", "aptekonline", "aloe"}
    rows = db_session.query(RoiActionsCache).all()
    assert {r.client_site for r in rows} == {"pharmonline", "aptekonline", "aloe"}
    assert all(r.run_id == 99 for r in rows)


# ─── Phase 4.5: MAP violations ────────────────────────────────────────────────


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


def _add_brand_snapshots(s, run, products_with_prices):
    """Adds N snapshots for product-price pairs."""
    for product, price in products_with_prices:
        s.add(PriceSnapshot(run_id=run.id, product_id=product.id, price=price))
    s.flush()


def test_map_violation_detected_when_client_below_brand_floor(db_session):
    """Client цена < brand floor у конкурентов → map_violation flagged."""
    # Конкуренты держат бренд Bayer на уровне 10.00+ ₼ (3 snapshots для валидности)
    apt = _add_product_with_brand(db_session, "aptekonline", "Aspirin Bayer 100", "Bayer", "apt-1")
    aloe = _add_product_with_brand(db_session, "aloe", "Aspirin Bayer 100", "Bayer", "aloe-1")
    r = _make_run(db_session, [(apt, 10.50), (aloe, 11.00)])
    # Добавим ещё один snapshot для floor-min (3 семпла минимум)
    db_session.add(PriceSnapshot(run_id=r.id, product_id=apt.id, price=10.00))
    db_session.flush()

    # Client (pharmonline) — 7.00 ₼ — это 30% ниже floor 10.00
    client = _add_product_with_brand(
        db_session, "pharmonline", "Aspirin Bayer 100", "Bayer", "ph-1"
    )
    db_session.add(PriceSnapshot(run_id=r.id, product_id=client.id, price=7.00))
    db_session.commit()

    actions = roi.compute_actions(db_session)
    map_actions = [a for a in actions if a.type == "map_violation"]
    assert len(map_actions) == 1, f"ожидали 1 map_violation, получили: {[a.type for a in actions]}"
    a = map_actions[0]
    assert a.severity == "critical", f"gap 30% >= 20% → critical, got {a.severity}"
    assert a.product_name == "Aspirin Bayer 100"
    assert a.current_value_azn == 7.00
    assert a.target_value_azn is not None and a.target_value_azn < 10.00


def test_map_violation_severity_buckets(db_session):
    """Three buckets: critical >= 20%, warning >= 10%, info >= 5%."""
    r = _shared_run(db_session)
    # Three brands with floor 10.00, three client products at different gaps
    for i, (brand, client_price, expected_sev) in enumerate(
        [
            ("BrandA", 7.50, "critical"),  # 25% below
            ("BrandB", 8.80, "warning"),  # 12% below
            ("BrandC", 9.50, "info"),  # 5% below
        ]
    ):
        comp_a = _add_product_with_brand(db_session, "aptekonline", f"P{i}-{brand}", brand, f"a{i}")
        comp_b = _add_product_with_brand(db_session, "aloe", f"P{i}-{brand}", brand, f"b{i}")
        client = _add_product_with_brand(db_session, "pharmonline", f"P{i}-{brand}", brand, f"c{i}")
        # 3 snapshots — нужны для валидного floor (n >= 3 в _map_violations)
        for p, price in [(comp_a, 10.00), (comp_b, 10.50), (comp_a, 11.00), (client, client_price)]:
            db_session.add(PriceSnapshot(run_id=r.id, product_id=p.id, price=price))
        db_session.flush()
    db_session.commit()

    actions = roi.compute_actions(db_session)
    by_brand = {
        (a.extra or {}).get("brand"): a.severity for a in actions if a.type == "map_violation"
    }
    assert by_brand.get("BrandA") == "critical", f"got: {by_brand}"
    assert by_brand.get("BrandB") == "warning", f"got: {by_brand}"
    assert by_brand.get("BrandC") == "info", f"got: {by_brand}"


def test_map_violation_skipped_when_client_above_floor(db_session):
    """Client >= floor → нет нарушения (это price_raise зона)."""
    apt = _add_product_with_brand(db_session, "aptekonline", "X", "BrandX", "a1")
    client = _add_product_with_brand(db_session, "pharmonline", "X", "BrandX", "c1")
    r = _make_run(
        db_session,
        [
            (apt, 5.00),
            (apt, 5.20),
            (apt, 5.50),  # floor = 5.00
            (client, 6.00),  # выше floor — НЕ нарушение
        ],
    )
    db_session.commit()

    actions = roi.compute_actions(db_session)
    assert not any(a.type == "map_violation" for a in actions)


def test_map_violation_skipped_when_insufficient_samples(db_session):
    """Меньше 3 snapshot-семплов для бренда — floor не статистически валиден."""
    apt = _add_product_with_brand(db_session, "aptekonline", "X", "RareBrand", "a1")
    client = _add_product_with_brand(db_session, "pharmonline", "X", "RareBrand", "c1")
    r = _make_run(
        db_session,
        [
            (apt, 10.00),  # только 1 семпл бренда у конкурентов
            (client, 5.00),  # 50% gap, но floor невалидный
        ],
    )
    db_session.commit()

    actions = roi.compute_actions(db_session)
    assert not any(a.type == "map_violation" for a in actions)


def test_map_violation_translation_az_en(db_session):
    """translate_action заменяет title/detail для az/en на map_violation."""
    apt = _add_product_with_brand(db_session, "aptekonline", "Z", "TestBrand", "a1")
    client = _add_product_with_brand(db_session, "pharmonline", "Z", "TestBrand", "c1")
    r = _make_run(
        db_session,
        [
            (apt, 10.00),
            (apt, 11.00),
            (apt, 10.50),  # 3 семпла, floor = 10
            (client, 7.00),  # 30% gap → critical
        ],
    )
    db_session.commit()

    actions = roi.compute_actions(db_session)
    map_actions = [a for a in actions if a.type == "map_violation"]
    assert len(map_actions) == 1

    # сериализуем как делает кэш
    raw = roi._action_to_dict(map_actions[0])
    az = roi.translate_action(raw, "az")
    assert "TestBrand" in az["title"] and "qaldırın" in az["title"]
    assert "rəqib floor" in az["detail"].lower() or "floor" in az["detail"].lower()

    en = roi.translate_action(raw, "en")
    assert "raise" in en["title"].lower() and "TestBrand" in en["title"]
    assert "competitor floor" in en["detail"].lower()
