"""Тесты аналитического модуля: brand_share, promo_history, assortment_overlap, price_index."""

from datetime import timedelta
from src._time import utcnow

from src import analytics, storage
from src.brand_catalog import is_brand_blacklisted
from src.storage import Match, PriceSnapshot, Product, Promo, Run


def _add_run(s, started_at=None, *, tenant_id=1):
    started = started_at or utcnow()
    r = Run(
        tenant_id=tenant_id,
        started_at=started,
        finished_at=started,
        status="ok",
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {
                "pharmonline": {"status": "ok"},
                "aptekonline": {"status": "ok"},
                "aloe": {"status": "ok"},
            },
        },
    )
    s.add(r)
    s.flush()
    return r


def _add_product(
    s,
    site,
    name,
    brand=None,
    ext_id=None,
    canonical_id=None,
    category=None,
    tenant_id=1,
):
    p = Product(
        tenant_id=tenant_id,
        site=site,
        external_id=ext_id or f"{site}-{name}",
        url=f"http://{site}.az/p",
        name=name,
        name_normalized=name.lower(),
        brand=brand,
        canonical_id=canonical_id,
        category=category,
    )
    s.add(p)
    s.flush()
    return p


def _add_snap(s, run, product, price):
    s.add(PriceSnapshot(run_id=run.id, product_id=product.id, price=price))
    s.flush()


def _add_snap_at(s, run, product, price, captured_at=None, discount_price=None):
    s.add(
        PriceSnapshot(
            run_id=run.id,
            product_id=product.id,
            price=price,
            discount_price=discount_price,
            captured_at=captured_at or utcnow(),
        )
    )
    s.flush()


def test_brand_share_counts_per_site(db_session):
    run = _add_run(db_session)
    p1 = _add_product(db_session, "pharmonline", "Aspirin", brand="Bayer", ext_id="1")
    p2 = _add_product(db_session, "aloe", "Aspirin", brand="Bayer", ext_id="2")
    p3 = _add_product(db_session, "pharmonline", "Bepanthen", brand="Bayer", ext_id="3")
    p4 = _add_product(db_session, "pharmonline", "Solgar D", brand="Solgar", ext_id="4")
    for p in (p1, p2, p3, p4):
        _add_snap(db_session, run, p, 10.0)
    db_session.commit()

    brands = analytics.brand_share(db_session)
    bayer = next(b for b in brands if b.brand == "Bayer")
    solgar = next(b for b in brands if b.brand == "Solgar")
    assert bayer.counts["pharmonline"] == 2
    assert bayer.counts["aloe"] == 1
    assert bayer.total == 3
    assert bayer.sites_with_brand == 2
    assert bayer.exclusive_to is None
    assert solgar.exclusive_to == "pharmonline"


def test_brand_share_ignores_null_brand(db_session):
    run = _add_run(db_session)
    p = _add_product(db_session, "aloe", "Foo", brand=None, ext_id="x")
    _add_snap(db_session, run, p, 5.0)
    db_session.commit()
    assert analytics.brand_share(db_session) == []


def test_brand_share_diff_only_no_snapshot_in_latest_run(db_session):
    """Diff-only regression (2026-05-29): товар активен (last_seen_at свежий),
    но БЕЗ snapshot'а в последнем прогоне (цена не менялась). Раньше brand_share
    через PriceSnapshot.run_id join его пропускал → пустая страница. Теперь
    считается по last_seen_at."""
    # Старый run со снапшотом (имитирует первый прогон где цена записана)
    old_run = _add_run(db_session, started_at=utcnow() - timedelta(days=3))
    p = _add_product(db_session, "pharmonline", "Aspirin", brand="Bayer", ext_id="a1")
    _add_snap(db_session, old_run, p, 10.0)
    # Свежий прогон БЕЗ снапшота для p (diff-only: цена не менялась)
    _add_run(db_session, started_at=utcnow())
    # last_seen_at свежий (default=utcnow при создании продукта)
    db_session.commit()

    brands = analytics.brand_share(db_session)
    assert len(brands) == 1
    assert brands[0].brand == "Bayer"
    assert brands[0].counts["pharmonline"] == 1


def test_brand_share_excludes_stale_products(db_session):
    """Товар не виденный > 14 дней → исключается (давно снят с продажи)."""
    p = _add_product(db_session, "aloe", "DeadSKU", brand="GhostBrand", ext_id="d1")
    p.last_seen_at = utcnow() - timedelta(days=30)
    db_session.commit()
    assert analytics.brand_share(db_session) == []


def test_promo_history_groups_by_site_title(db_session):
    base = utcnow() - timedelta(days=5)
    r1 = _add_run(db_session, base)
    r2 = _add_run(db_session, base + timedelta(days=2))
    db_session.add(Promo(run_id=r1.id, site="aloe", title="Sale 30%", captured_at=base))
    db_session.add(
        Promo(run_id=r2.id, site="aloe", title="Sale 30%", captured_at=base + timedelta(days=2))
    )
    db_session.add(
        Promo(
            run_id=r2.id,
            site="aptekonline",
            title="New launch",
            captured_at=base + timedelta(days=2),
        )
    )
    db_session.commit()

    history = analytics.promo_history(db_session, days=30)
    assert len(history) == 2
    aloe_promo = next(h for h in history if h.site == "aloe")
    assert aloe_promo.days_active >= 2


def test_assortment_overlap_counts_unmatched(db_session):
    m = Match(canonical_name="Foo", confidence=1.0)
    db_session.add(m)
    db_session.flush()

    run = _add_run(db_session)
    matched_client = _add_product(
        db_session, "pharmonline", "Foo", canonical_id=m.id, ext_id="ph-m1"
    )
    matched_aloe = _add_product(db_session, "aloe", "Foo", canonical_id=m.id, ext_id="al-m1")
    unmatched_client = _add_product(db_session, "pharmonline", "Excl Client", ext_id="ph-x")
    unmatched_aloe = _add_product(db_session, "aloe", "Excl Aloe", ext_id="al-x")
    for p in (matched_client, matched_aloe, unmatched_client, unmatched_aloe):
        _add_snap(db_session, run, p, 10.0)
    db_session.commit()

    overlap = analytics.assortment_overlap(db_session)
    assert overlap.matched_count == 1
    assert overlap.only_client == 1
    assert overlap.only_competitor_count["aloe"] == 1
    # 1 matched + 1 unmatched на pharmonline → coverage 50%
    assert overlap.coverage_pct == 50.0


def test_price_index_by_category(db_session):
    m = Match(canonical_name="Test", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    run = _add_run(db_session)

    client = _add_product(
        db_session,
        "pharmonline",
        "Test",
        canonical_id=m.id,
        category="vitamins",
        ext_id="ph",
    )
    comp = _add_product(
        db_session,
        "aloe",
        "Test",
        canonical_id=m.id,
        category="vitamins",
        ext_id="al",
    )
    _add_snap(db_session, run, client, 10.0)
    _add_snap(db_session, run, comp, 8.0)  # клиент дороже на 25%
    db_session.commit()

    idx = analytics.price_index_by_category(db_session)
    assert len(idx) == 1
    pi = idx[0]
    assert pi.category == "vitamins"
    assert pi.avg_client_price == 10.0
    assert pi.avg_competitor_price == 8.0
    # index = 10/8 * 100 = 125.0 (клиент дороже)
    assert pi.index == 125.0
    assert pi.matched_skus == 1


def test_match_quality_basic(db_session):
    """match_quality() считает auto vs manual matches и coverage."""
    from src.storage import Match, MatchRejection

    # 2 auto match + 1 manual match
    m1 = Match(canonical_name="A", confidence=1.0, is_manual=False)
    m2 = Match(canonical_name="B", confidence=1.0, is_manual=False)
    m3 = Match(canonical_name="C", confidence=1.0, is_manual=True)
    db_session.add_all([m1, m2, m3])
    db_session.flush()

    p1 = _add_product(db_session, "pharmonline", "A1", canonical_id=m1.id, ext_id="a1")
    p2 = _add_product(db_session, "aloe", "A2", canonical_id=m1.id, ext_id="a2")
    p3 = _add_product(db_session, "pharmonline", "B1", canonical_id=m2.id, ext_id="b1")
    p4 = _add_product(db_session, "aloe", "B2", canonical_id=m2.id, ext_id="b2")
    p5 = _add_product(db_session, "pharmonline", "C1", canonical_id=m3.id, ext_id="c1")
    p_unmatched = _add_product(db_session, "aloe", "Lonely", ext_id="lonely")

    db_session.add(MatchRejection(product_a_id=p1.id, product_b_id=p_unmatched.id))
    db_session.commit()

    q = analytics.match_quality(db_session)
    assert q.total_matches == 3
    assert q.auto_matches == 2
    assert q.manual_matches == 1
    assert q.rejected_pairs == 1
    assert q.products_total == 6
    assert q.products_matched == 5
    assert q.coverage_pct == round(5 / 6 * 100, 1)
    assert q.manual_pct == round(1 / 3 * 100, 1)


def test_match_quality_empty(db_session):
    q = analytics.match_quality(db_session)
    assert q.total_matches == 0
    assert q.products_matched == 0
    assert q.coverage_pct == 0.0


def test_match_quality_is_tenant_scoped(db_session):
    from src.storage import Match, MatchRejection

    own_match = Match(tenant_id=1, canonical_name="Own", confidence=1.0)
    foreign_match = Match(tenant_id=2, canonical_name="Foreign", confidence=1.0)
    db_session.add_all([own_match, foreign_match])
    db_session.flush()
    own_product = _add_product(
        db_session, "pharmonline", "Own", canonical_id=own_match.id, ext_id="tenant-own"
    )
    foreign_product = _add_product(
        db_session, "aloe", "Foreign", canonical_id=foreign_match.id, ext_id="tenant-foreign"
    )
    foreign_product.tenant_id = 2
    db_session.add(
        MatchRejection(
            tenant_id=2,
            product_a_id=own_product.id,
            product_b_id=foreign_product.id,
        )
    )
    db_session.commit()

    own = analytics.match_quality(db_session, tenant_id=1)
    foreign = analytics.match_quality(db_session, tenant_id=2)

    assert (own.total_matches, own.products_total, own.rejected_pairs) == (1, 1, 0)
    assert (foreign.total_matches, foreign.products_total, foreign.rejected_pairs) == (1, 1, 1)


def test_empty_db_returns_empty(db_session):
    assert analytics.brand_share(db_session) == []
    assert analytics.promo_history(db_session) == []
    assert analytics.price_index_by_category(db_session) == []
    overlap = analytics.assortment_overlap(db_session)
    assert overlap.matched_count == 0
    assert overlap.coverage_pct == 0.0


# ── category_comparison ──────────────────────────────────────────────────────


def test_category_comparison_multi_category_and_labels(db_session):
    """2 категории, per-site средние, index, win/lose + ярлык из Category/fallback."""
    from src.storage import Category

    # Ярлык только для vitamins; pain — без записи Category (fallback на slug).
    db_session.add(
        Category(
            key="vitamins",
            label_ru="Витамины",
            label_az="Vitaminlər",
            pharmonline_slug="vitamins",
        )
    )
    run = _add_run(db_session)

    m1 = Match(canonical_name="V1", confidence=1.0)
    m2 = Match(canonical_name="V2", confidence=1.0)
    m3 = Match(canonical_name="P1", confidence=1.0)
    db_session.add_all([m1, m2, m3])
    db_session.flush()

    # SKU1 (vitamins): client 10 vs aptekonline 8 → клиент дороже.
    c1 = _add_product(
        db_session, "pharmonline", "V1", canonical_id=m1.id, category="vitamins", ext_id="c1"
    )
    a1 = _add_product(
        db_session, "aptekonline", "V1", canonical_id=m1.id, category="apt-v", ext_id="a1"
    )
    _add_snap_at(db_session, run, c1, 10.0)
    _add_snap_at(db_session, run, a1, 8.0)

    # SKU2 (vitamins): client 5 vs aptekonline 6 + aloe 10 → comp_mean 8 → дешевле.
    c2 = _add_product(
        db_session, "pharmonline", "V2", canonical_id=m2.id, category="vitamins", ext_id="c2"
    )
    a2 = _add_product(
        db_session, "aptekonline", "V2", canonical_id=m2.id, category="apt-v", ext_id="a2"
    )
    l2 = _add_product(db_session, "aloe", "V2", canonical_id=m2.id, category="aloe-v", ext_id="l2")
    _add_snap_at(db_session, run, c2, 5.0)
    _add_snap_at(db_session, run, a2, 6.0)
    _add_snap_at(db_session, run, l2, 10.0)

    # SKU3 (pain): client 20 vs aloe 20 → паритет.
    c3 = _add_product(
        db_session, "pharmonline", "P1", canonical_id=m3.id, category="pain", ext_id="c3"
    )
    l3 = _add_product(db_session, "aloe", "P1", canonical_id=m3.id, category="aloe-p", ext_id="l3")
    _add_snap_at(db_session, run, c3, 20.0)
    _add_snap_at(db_session, run, l3, 20.0)
    db_session.commit()

    rows = analytics.category_comparison(db_session)
    by = {r.category: r for r in rows}
    assert set(by) == {"vitamins", "pain"}

    v = by["vitamins"]
    assert v.matched_skus == 2
    assert v.label_ru == "Витамины"
    assert v.label_az == "Vitaminlər"
    assert v.per_site_avg == {"aptekonline": 7.0, "aloe": 10.0}
    assert v.avg_client_price == 7.5
    assert v.avg_competitor_price == 8.0
    assert v.index == 93.8  # 7.5/8*100
    assert v.cheaper_count == 1  # SKU2
    assert v.pricier_count == 1  # SKU1
    assert v.parity_count == 0
    assert v.cheaper_pct == 50.0

    p = by["pain"]
    assert p.matched_skus == 1
    assert p.label_ru is None  # нет записи Category → fallback на slug на фронте
    assert p.label_az is None
    assert p.index == 100.0
    assert p.parity_count == 1
    assert p.per_site_avg == {"aloe": 20.0}

    # Дефолт-сортировка: vitamins (|93.8-100|*2=12.4) выше pain (0).
    assert rows[0].category == "vitamins"


def test_category_comparison_diff_only_old_run(db_session):
    """Регрессия diff-only: товар со snapshot'ом ТОЛЬКО в старом прогоне всё равно
    учитывается. На прежней run_id-логике (snapshots последнего ok-run) он выпал бы:
    новый ok-run без его snapshot (цена не менялась) → пустой результат."""
    old = _add_run(db_session, started_at=utcnow() - timedelta(days=10))
    _add_run(db_session, started_at=utcnow())  # новый ok-run БЕЗ snapshot'ов

    m = Match(canonical_name="Stable", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    c = _add_product(
        db_session, "pharmonline", "Stable", canonical_id=m.id, category="herbs", ext_id="c"
    )
    a = _add_product(db_session, "aloe", "Stable", canonical_id=m.id, category="aloe-h", ext_id="a")
    _add_snap_at(db_session, old, c, 12.0, captured_at=utcnow() - timedelta(days=10))
    _add_snap_at(db_session, old, a, 10.0, captured_at=utcnow() - timedelta(days=10))
    db_session.commit()

    rows = analytics.category_comparison(db_session)
    assert len(rows) == 1
    assert rows[0].category == "herbs"
    assert rows[0].matched_skus == 1
    assert rows[0].avg_client_price == 12.0
    assert rows[0].index == 120.0  # 12/10*100, клиент дороже


def test_category_comparison_keeps_prices_until_cross_site_trusted_epoch(db_session):
    """Один eligible run сайта не должен включать неполный lineage-фильтр."""
    old = Run(tenant_id=1, started_at=utcnow() - timedelta(days=1), status="ok")
    db_session.add(old)
    db_session.flush()

    m = Match(tenant_id=1, canonical_name="Stable partial epoch", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    client = _add_product(
        db_session,
        "pharmonline",
        "Stable partial epoch",
        canonical_id=m.id,
        category="stable-category",
        ext_id="stable-client",
    )
    competitor = _add_product(
        db_session,
        "aloe",
        "Stable partial epoch",
        canonical_id=m.id,
        ext_id="stable-aloe",
    )
    _add_snap_at(db_session, old, client, 12.0, captured_at=old.started_at)
    _add_snap_at(db_session, old, competitor, 10.0, captured_at=old.started_at)

    aloe_only = _add_run(db_session, started_at=utcnow())
    aloe_only.full_catalog_sites = "aloe"
    aloe_only.run_quality["sites"] = {"aloe": {"status": "ok"}}
    db_session.commit()

    assert storage.financially_eligible_run_ids(db_session, tenant_id=1) == [aloe_only.id]
    rows = analytics.category_comparison(db_session, tenant_id=1)

    assert len(rows) == 1
    assert rows[0].category == "stable-category"
    assert rows[0].matched_skus == 1


def test_category_comparison_ignores_newer_untrusted_snapshot(db_session):
    trusted = _add_run(db_session, started_at=utcnow() - timedelta(hours=2))
    partial = Run(
        started_at=utcnow(),
        finished_at=utcnow(),
        status="degraded",
        catalog_scope="partial",
        catalog_verified=False,
        run_quality={
            "baseline_enforced": False,
            "full_catalog_verified": False,
            "financially_eligible": False,
            "sites": {"pharmonline": {"status": "degraded"}},
        },
    )
    db_session.add(partial)
    db_session.flush()

    m = Match(canonical_name="Trusted price", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    client = _add_product(
        db_session,
        "pharmonline",
        "Trusted price",
        canonical_id=m.id,
        category="trusted-cat",
        ext_id="trusted-client",
    )
    competitor = _add_product(
        db_session,
        "aloe",
        "Trusted price",
        canonical_id=m.id,
        category="trusted-cat",
        ext_id="trusted-aloe",
    )
    aptek_policy_product = _add_product(
        db_session,
        "aptekonline",
        "Policy witness",
        canonical_id=None,
        category="trusted-cat",
        ext_id="trusted-aptek-policy",
    )
    _add_snap_at(db_session, trusted, client, 10.0)
    _add_snap_at(db_session, trusted, competitor, 8.0)
    _add_snap_at(db_session, partial, client, 99.0)
    _add_snap_at(db_session, partial, competitor, 1.0)
    for product in (client, competitor, aptek_policy_product):
        product.manufacturer_country_code = "rs"
        product.country_resolution_status = "resolved"
        product.offer_availability_status = "in_stock"
        product.availability_observed_at = utcnow()
        db_session.add(
            storage.OfferObservation(
                tenant_id=product.tenant_id,
                run_id=trusted.id,
                product_id=product.id,
                country_code="rs",
                country_raw="Serbia",
                country_resolution_status="resolved",
                availability_status="in_stock",
                observed_at=utcnow(),
            )
        )
    db_session.commit()
    from src import roi

    assert storage.run_is_financially_eligible(trusted) is True
    assert storage.financially_eligible_run_ids(db_session) == [trusted.id]
    assert roi.financial_inputs_are_fresh(db_session, tenant_id=1) is True
    trusted_snaps = storage.latest_snapshots_per_product(
        db_session,
        [client.id, competitor.id],
        financially_eligible_only=True,
        tenant_id=1,
    )
    assert trusted_snaps[client.id].price == 10.0
    assert trusted_snaps[competitor.id].price == 8.0

    rows = analytics.category_comparison(db_session, categories={"trusted-cat"})

    assert len(rows) == 1
    assert rows[0].avg_client_price == 10.0
    assert rows[0].avg_competitor_price == 8.0
    assert rows[0].index == 125.0


def test_category_comparison_uses_discount_price(db_session):
    """Текущая цена = discount_price (если есть), иначе price."""
    run = _add_run(db_session)
    m = Match(canonical_name="D", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    c = _add_product(db_session, "pharmonline", "D", canonical_id=m.id, category="d", ext_id="c")
    a = _add_product(db_session, "aloe", "D", canonical_id=m.id, category="ad", ext_id="a")
    _add_snap_at(db_session, run, c, 20.0, discount_price=10.0)  # клиент по скидке 10
    _add_snap_at(db_session, run, a, 10.0)
    db_session.commit()

    rows = analytics.category_comparison(db_session)
    assert rows[0].avg_client_price == 10.0  # discount_price, не 20
    assert rows[0].index == 100.0


def test_category_comparison_confidence_floor(db_session):
    """Низко-достоверный авто-матч отсекается; ручной (is_manual) — всегда включён."""
    run = _add_run(db_session)
    m_low = Match(canonical_name="Low", confidence=0.5, is_manual=False)
    m_manual = Match(canonical_name="Man", confidence=0.1, is_manual=True)
    db_session.add_all([m_low, m_manual])
    db_session.flush()
    cl = _add_product(
        db_session, "pharmonline", "Low", canonical_id=m_low.id, category="low", ext_id="cl"
    )
    al = _add_product(
        db_session, "aloe", "Low", canonical_id=m_low.id, category="aloe-l", ext_id="al"
    )
    cm = _add_product(
        db_session, "pharmonline", "Man", canonical_id=m_manual.id, category="man", ext_id="cm"
    )
    am = _add_product(
        db_session, "aloe", "Man", canonical_id=m_manual.id, category="aloe-m", ext_id="am"
    )
    for p, pr in ((cl, 10.0), (al, 8.0), (cm, 10.0), (am, 8.0)):
        _add_snap_at(db_session, run, p, pr)
    db_session.commit()

    cats = {r.category for r in analytics.category_comparison(db_session)}
    assert cats == {"man"}  # low (0.5 авто) отсечён, manual (0.1 ручной) включён


def test_category_comparison_tenant_isolation(db_session):
    """tenant_id фильтрует матчи; None → все тенанты (для не-HTTP вызовов)."""
    run = _add_run(db_session, tenant_id=1)
    run2 = _add_run(db_session, tenant_id=2)
    m1 = Match(canonical_name="T1", confidence=1.0, tenant_id=1)
    m2 = Match(canonical_name="T2", confidence=1.0, tenant_id=2)
    db_session.add_all([m1, m2])
    db_session.flush()
    c1 = _add_product(
        db_session, "pharmonline", "T1", canonical_id=m1.id, category="t1cat", ext_id="c1"
    )
    a1 = _add_product(db_session, "aloe", "T1", canonical_id=m1.id, category="aloe1", ext_id="a1")
    c2 = _add_product(
        db_session,
        "pharmonline",
        "T2",
        canonical_id=m2.id,
        category="t2cat",
        ext_id="c2",
        tenant_id=2,
    )
    a2 = _add_product(
        db_session,
        "aloe",
        "T2",
        canonical_id=m2.id,
        category="aloe2",
        ext_id="a2",
        tenant_id=2,
    )
    for p, pr in ((c1, 10.0), (a1, 8.0)):
        _add_snap_at(db_session, run, p, pr)
    for p, pr in ((c2, 10.0), (a2, 8.0)):
        _add_snap_at(db_session, run2, p, pr)
    db_session.commit()

    assert {r.category for r in analytics.category_comparison(db_session, tenant_id=1)} == {"t1cat"}
    assert {r.category for r in analytics.category_comparison(db_session, tenant_id=2)} == {"t2cat"}
    assert {r.category for r in analytics.category_comparison(db_session)} == {"t1cat", "t2cat"}


def test_category_comparison_can_filter_categories(db_session):
    run = _add_run(db_session)
    m1 = Match(canonical_name="Keep", confidence=1.0)
    m2 = Match(canonical_name="Skip", confidence=1.0)
    db_session.add_all([m1, m2])
    db_session.flush()
    c1 = _add_product(
        db_session, "pharmonline", "Keep", canonical_id=m1.id, category="keep-cat", ext_id="c1"
    )
    a1 = _add_product(db_session, "aloe", "Keep", canonical_id=m1.id, category="aloe1", ext_id="a1")
    c2 = _add_product(
        db_session, "pharmonline", "Skip", canonical_id=m2.id, category="skip-cat", ext_id="c2"
    )
    a2 = _add_product(db_session, "aloe", "Skip", canonical_id=m2.id, category="aloe2", ext_id="a2")
    for p, pr in ((c1, 10.0), (a1, 8.0), (c2, 10.0), (a2, 8.0)):
        _add_snap_at(db_session, run, p, pr)
    db_session.commit()

    rows = analytics.category_comparison(db_session, categories={"keep-cat"})
    assert [r.category for r in rows] == ["keep-cat"]
    assert analytics.category_comparison(db_session, categories=set()) == []


def test_category_comparison_filter_selects_matching_client_category(db_session):
    run = _add_run(db_session)
    m = Match(canonical_name="Dirty multi client", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    keep = _add_product(
        db_session,
        "pharmonline",
        "Keep",
        canonical_id=m.id,
        category="keep-cat",
        ext_id="keep",
    )
    skip = _add_product(
        db_session,
        "pharmonline",
        "Skip",
        canonical_id=m.id,
        category="skip-cat",
        ext_id="skip",
    )
    aloe = _add_product(db_session, "aloe", "Keep", canonical_id=m.id, category="aloe", ext_id="a")
    _add_snap_at(db_session, run, keep, 10.0)
    _add_snap_at(db_session, run, skip, 30.0)
    _add_snap_at(db_session, run, aloe, 8.0)
    db_session.commit()

    rows = analytics.category_comparison(db_session, categories={"keep-cat"})
    assert [r.category for r in rows] == ["keep-cat"]
    assert rows[0].avg_client_price == 10.0


def test_category_comparison_empty(db_session):
    assert analytics.category_comparison(db_session) == []


def test_category_comparison_canonical_collapses_source_categories(db_session):
    run = _add_run(db_session)
    first = Match(canonical_name="Heart 1", confidence=1.0)
    second = Match(canonical_name="Heart 2", confidence=1.0)
    db_session.add_all([first, second])
    db_session.flush()

    products = [
        _add_product(
            db_session,
            "pharmonline",
            "Heart 1",
            canonical_id=first.id,
            category="antihipertenziv-dermanlar",
            ext_id="heart-client-1",
        ),
        _add_product(
            db_session,
            "aloe",
            "Heart 1",
            canonical_id=first.id,
            category="dermanlar",
            ext_id="heart-aloe-1",
        ),
        _add_product(
            db_session,
            "pharmonline",
            "Heart 2",
            canonical_id=second.id,
            category="aritmiya-ve-stenokardiya-zamani",
            ext_id="heart-client-2",
        ),
        _add_product(
            db_session,
            "aptekonline",
            "Heart 2",
            canonical_id=second.id,
            category="260",
            ext_id="heart-aptek-2",
        ),
    ]
    for product, price in zip(products, (10.0, 9.0, 20.0, 18.0)):
        _add_snap_at(db_session, run, product, price)
    db_session.commit()

    rows = analytics.category_comparison(db_session, canonical=True)

    assert len(rows) == 1
    assert rows[0].category == "cardiovascular_blood"
    assert rows[0].label_ru == "Сердце, сосуды и кровь"
    assert rows[0].label_az == "Ürək, damarlar və qan"
    assert rows[0].matched_skus == 2


def test_category_comparison_canonical_skips_format_only_categories(db_session):
    run = _add_run(db_session)
    match = Match(canonical_name="Suppository", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    client = _add_product(
        db_session,
        "pharmonline",
        "Suppository",
        canonical_id=match.id,
        category="shamlar-3",
        ext_id="format-client",
    )
    competitor = _add_product(
        db_session,
        "aloe",
        "Suppository",
        canonical_id=match.id,
        category="dermanlar",
        ext_id="format-aloe",
    )
    _add_snap_at(db_session, run, client, 10.0)
    _add_snap_at(db_session, run, competitor, 9.0)
    db_session.commit()

    assert analytics.category_comparison(db_session, canonical=True) == []


def test_category_comparison_canonical_keeps_unclassified_matched_category(db_session):
    run = _add_run(db_session)
    match = Match(canonical_name="Unknown category product", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    client = _add_product(
        db_session,
        "pharmonline",
        "Unknown category product",
        canonical_id=match.id,
        category="new-unclassified-category",
        ext_id="unknown-category-client",
    )
    competitor = _add_product(
        db_session,
        "aloe",
        "Unknown category product",
        canonical_id=match.id,
        category="dermanlar",
        ext_id="unknown-category-aloe",
    )
    _add_snap_at(db_session, run, client, 10.0)
    _add_snap_at(db_session, run, competitor, 9.0)
    db_session.commit()

    rows = analytics.category_comparison(db_session, canonical=True)

    assert len(rows) == 1
    assert rows[0].category == "new-unclassified-category"
    assert rows[0].matched_skus == 1


def test_category_comparison_shadow_bootstrap_uses_latest_prices(db_session):
    partial = Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=utcnow(),
        status="degraded",
        catalog_scope="partial",
        catalog_verified=False,
        run_quality={"financially_eligible": False, "sites": {}},
    )
    db_session.add(partial)
    db_session.flush()
    match = Match(canonical_name="Bootstrap", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    client = _add_product(
        db_session,
        "pharmonline",
        "Bootstrap",
        canonical_id=match.id,
        category="goz-xestelikleri-uchun-vasiteler",
        ext_id="bootstrap-client",
    )
    competitor = _add_product(
        db_session,
        "aloe",
        "Bootstrap",
        canonical_id=match.id,
        category="dermanlar",
        ext_id="bootstrap-aloe",
    )
    _add_snap_at(db_session, partial, client, 10.0)
    _add_snap_at(db_session, partial, competitor, 8.0)
    db_session.commit()

    rows = analytics.category_comparison(db_session, canonical=True)

    assert [row.category for row in rows] == ["eye_health"]
    assert rows[0].matched_skus == 1


def test_price_index_and_category_comparison_kwargs_no_typeerror(db_session):
    """Регрессия: API зовёт обе функции с client_site=/tenant_id= — не TypeError.

    (Старая `price_index_by_category(session)` бросала TypeError на api.py:1940.)
    """
    assert (
        analytics.price_index_by_category(db_session, client_site="pharmonline", tenant_id=1) == []
    )
    assert analytics.category_comparison(db_session, client_site="pharmonline", tenant_id=1) == []


def test_category_comparison_excludes_dead_url(db_session):
    """Товар с url_dead_at (страница 404) исключается из сравнения."""
    run = _add_run(db_session)
    m = Match(canonical_name="D", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    c = _add_product(db_session, "pharmonline", "D", canonical_id=m.id, category="cat", ext_id="c")
    a_dead = _add_product(
        db_session, "aptekonline", "D", canonical_id=m.id, category="ac", ext_id="a"
    )
    al = _add_product(db_session, "aloe", "D", canonical_id=m.id, category="lc", ext_id="l")
    a_dead.url_dead_at = utcnow()  # фантомный конкурент
    db_session.flush()
    _add_snap_at(db_session, run, c, 10.0)
    _add_snap_at(db_session, run, a_dead, 8.0)
    _add_snap_at(db_session, run, al, 9.0)
    db_session.commit()

    rows = analytics.category_comparison(db_session)
    assert len(rows) == 1
    # aptekonline (dead) исключён, остаётся только aloe
    assert "aptekonline" not in rows[0].per_site_avg
    assert rows[0].per_site_avg == {"aloe": 9.0}


def test_category_comparison_skips_match_with_dead_client(db_session):
    """Если фантомен сам клиент (pharmonline 404) — матч выпадает целиком."""
    run = _add_run(db_session)
    m = Match(canonical_name="DC", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    c = _add_product(db_session, "pharmonline", "DC", canonical_id=m.id, category="cat", ext_id="c")
    a = _add_product(db_session, "aloe", "DC", canonical_id=m.id, category="lc", ext_id="a")
    c.url_dead_at = utcnow()
    db_session.flush()
    _add_snap_at(db_session, run, c, 10.0)
    _add_snap_at(db_session, run, a, 8.0)
    db_session.commit()

    assert analytics.category_comparison(db_session) == []


def test_brand_quality_rejects_pack_tokens_and_product_descriptors():
    for value in ("0", "N120", "№20", "Şpris", "Qlükoza", "Elektron"):
        assert is_brand_blacklisted(value), value
    assert not is_brand_blacklisted("3M")


def test_category_comparison_excludes_oos_offer_but_keeps_active_competitor(
    db_session,
):
    run = _add_run(db_session)
    match = Match(canonical_name="Stock policy", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    client = _add_product(
        db_session,
        "pharmonline",
        "Stock policy",
        canonical_id=match.id,
        category="cat",
        ext_id="stock-client",
    )
    oos = _add_product(
        db_session,
        "aptekonline",
        "Stock policy",
        canonical_id=match.id,
        ext_id="stock-oos",
    )
    active = _add_product(
        db_session,
        "aloe",
        "Stock policy",
        canonical_id=match.id,
        ext_id="stock-active",
    )
    oos.offer_availability_status = "out_of_stock"
    oos.availability_observed_at = utcnow()
    _add_snap_at(db_session, run, client, 10.0)
    _add_snap_at(db_session, run, oos, 5.0)
    _add_snap_at(db_session, run, active, 9.0)
    db_session.commit()

    rows = analytics.category_comparison(db_session)

    assert len(rows) == 1
    assert rows[0].per_site_avg == {"aloe": 9.0}


def test_category_comparison_excludes_country_conflict(db_session):
    run = _add_run(db_session)
    match = Match(canonical_name="Country policy", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    client = _add_product(
        db_session,
        "pharmonline",
        "Country policy",
        canonical_id=match.id,
        category="cat",
        ext_id="country-client",
    )
    competitor = _add_product(
        db_session,
        "aloe",
        "Country policy",
        canonical_id=match.id,
        ext_id="country-competitor",
    )
    client.manufacturer_country_code = "ua"
    competitor.manufacturer_country_code = "rs"
    client.country_resolution_status = "resolved"
    competitor.country_resolution_status = "resolved"
    _add_snap_at(db_session, run, client, 10.0)
    _add_snap_at(db_session, run, competitor, 5.0)
    db_session.commit()

    assert analytics.category_comparison(db_session) == []
