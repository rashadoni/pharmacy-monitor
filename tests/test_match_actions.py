"""Тесты helper-операций для ручной коррекции матчей."""

from src import match_actions as ma
from src.storage import Match, Product


def _make_product(s, **kw) -> Product:
    p = Product(
        site=kw.get("site", "pharmonline"),
        external_id=kw.get("external_id", "id-1"),
        url=kw.get("url", "http://example.com/p"),
        name=kw["name"],
        name_normalized=kw.get("name_normalized", kw["name"].lower()),
        canonical_id=kw.get("canonical_id"),
    )
    s.add(p)
    s.flush()
    return p


def _make_match_cluster(s, name: str, sites: list[str]) -> tuple[Match, list[Product]]:
    m = Match(canonical_name=name, confidence=1.0, is_manual=False)
    s.add(m)
    s.flush()
    products = []
    for i, site in enumerate(sites):
        p = _make_product(
            s,
            site=site,
            external_id=f"{site}-{i}",
            name=name,
            canonical_id=m.id,
        )
        products.append(p)
    s.commit()
    return m, products


def test_add_rejection_normalizes_pair_order(db_session):
    """add_rejection(b, a) и (a, b) дают одну запись."""
    p1 = _make_product(db_session, name="Foo", external_id="1")
    p2 = _make_product(db_session, name="Bar", external_id="2")
    db_session.commit()

    r1 = ma.add_rejection(db_session, p1.id, p2.id)
    r2 = ma.add_rejection(db_session, p2.id, p1.id)
    assert r1.id == r2.id
    # min, max
    assert r1.product_a_id == min(p1.id, p2.id)
    assert r1.product_b_id == max(p1.id, p2.id)


def test_is_rejected_symmetric(db_session):
    p1 = _make_product(db_session, name="Foo", external_id="1")
    p2 = _make_product(db_session, name="Bar", external_id="2")
    db_session.commit()

    assert ma.is_rejected(db_session, p1.id, p2.id) is False
    ma.add_rejection(db_session, p1.id, p2.id, reason="test")
    db_session.commit()
    assert ma.is_rejected(db_session, p1.id, p2.id) is True
    assert ma.is_rejected(db_session, p2.id, p1.id) is True


def test_confirm_match_sets_is_manual(db_session):
    m, _ = _make_match_cluster(db_session, "Paracetamol", ["pharmonline", "aloe"])
    assert m.is_manual is False
    result = ma.confirm_match(db_session, m.id)
    assert result is not None
    assert result.is_manual is True


def test_break_match_detach_product_creates_rejections(db_session):
    """Break: detach один Product → rejection с КАЖДЫМ из остальных + clear canonical_id."""
    m, [p1, p2, p3] = _make_match_cluster(
        db_session, "Aspirin", ["pharmonline", "aptekonline", "aloe"]
    )

    n_rejections = ma.break_match(db_session, m.id, p2.id, reason="wrong product")
    assert n_rejections == 2  # rejection между p2-p1 и p2-p3

    db_session.refresh(p2)
    assert p2.canonical_id is None
    # p1, p3 остались в кластере
    db_session.refresh(p1)
    db_session.refresh(p3)
    assert p1.canonical_id == m.id
    assert p3.canonical_id == m.id


def test_break_match_dissolves_cluster_if_only_one_left(db_session):
    m, [p1, p2] = _make_match_cluster(db_session, "Foo", ["pharmonline", "aloe"])
    match_id = m.id
    ma.break_match(db_session, match_id, p1.id)

    # Match удалён, оставшийся p2 тоже теряет canonical_id
    db_session.refresh(p2)
    assert p2.canonical_id is None
    assert db_session.get(Match, match_id) is None


def test_find_alternatives_ranks_by_similarity(db_session):
    m, _ = _make_match_cluster(db_session, "Paracetamol 500mg", ["pharmonline", "aloe"])
    # Кандидаты на aptekonline (unmatched)
    closer = _make_product(
        db_session, site="aptekonline", external_id="ap-close", name="Paracetamol 500mg generic"
    )
    further = _make_product(
        db_session, site="aptekonline", external_id="ap-far", name="Aspirin Cardio 100mg"
    )
    matched_already = _make_product(
        db_session,
        site="aptekonline",
        external_id="ap-matched",
        name="Paracetamol other",
        canonical_id=m.id,  # уже сматченный — не должен попасть
    )
    db_session.commit()

    alts = ma.find_alternatives(db_session, m.id, "aptekonline")
    ids = [p.id for p, _score in alts]
    assert closer.id in ids
    assert further.id in ids
    assert matched_already.id not in ids
    # Closer должен быть выше further по score
    closer_pos = ids.index(closer.id)
    further_pos = ids.index(further.id)
    assert closer_pos < further_pos


def test_swap_alternative_replaces_product(db_session):
    m, [p1, p2] = _make_match_cluster(db_session, "Paracetamol", ["pharmonline", "aloe"])
    # Новый кандидат на aloe
    new_p = _make_product(
        db_session, site="aloe", external_id="aloe-new", name="Paracetamol Generic"
    )
    db_session.commit()

    ok = ma.swap_alternative(db_session, m.id, "aloe", new_p.id)
    assert ok is True

    db_session.refresh(new_p)
    db_session.refresh(p2)
    db_session.refresh(m)
    assert new_p.canonical_id == m.id
    assert p2.canonical_id is None
    assert m.is_manual is True

    # Между p2 (старый aloe) и new_p должен быть rejection
    assert ma.is_rejected(db_session, p2.id, new_p.id) is True


def test_list_rejections_for_product(db_session):
    p1 = _make_product(db_session, name="A", external_id="1")
    p2 = _make_product(db_session, name="B", external_id="2")
    p3 = _make_product(db_session, name="C", external_id="3")
    db_session.commit()

    ma.add_rejection(db_session, p1.id, p2.id)
    ma.add_rejection(db_session, p1.id, p3.id)
    db_session.commit()

    rej_for_p1 = ma.list_rejections_for_product(db_session, p1.id)
    assert sorted(rej_for_p1) == sorted([p2.id, p3.id])
    rej_for_p2 = ma.list_rejections_for_product(db_session, p2.id)
    assert rej_for_p2 == [p1.id]
