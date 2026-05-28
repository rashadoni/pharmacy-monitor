"""Тесты аналитического модуля: brand_share, promo_history, assortment_overlap, price_index."""

from datetime import timedelta
from src._time import utcnow

from src import analytics
from src.storage import Match, PriceSnapshot, Product, Promo, Run


def _add_run(s, started_at=None):
    r = Run(started_at=started_at or utcnow(), status="ok")
    s.add(r)
    s.flush()
    return r


def _add_product(s, site, name, brand=None, ext_id=None, canonical_id=None, category=None):
    p = Product(
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


def test_empty_db_returns_empty(db_session):
    assert analytics.brand_share(db_session) == []
    assert analytics.promo_history(db_session) == []
    assert analytics.price_index_by_category(db_session) == []
    overlap = analytics.assortment_overlap(db_session)
    assert overlap.matched_count == 0
    assert overlap.coverage_pct == 0.0
