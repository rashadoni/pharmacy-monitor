"""Тесты fuzzy-матчинга товаров между сайтами."""

from src import match_actions, matcher, storage


def _make_product(s, **kw) -> storage.Product:
    p = storage.Product(
        site=kw.get("site", "pharmonline"),
        external_id=kw.get("external_id", "id-1"),
        url=kw.get("url", "http://example.com/p"),
        name=kw["name"],
        name_normalized=kw.get("name_normalized", kw["name"].lower()),
        brand=kw.get("brand"),
        dosage=kw.get("dosage"),
        pack_size=kw.get("pack_size"),
    )
    s.add(p)
    s.flush()
    return p


def test_exact_match_across_sites(db_session):
    """Один товар на 3 сайтах с разной написанием → один Match."""
    a = _make_product(
        db_session,
        site="pharmonline",
        external_id="ph-1",
        name="Paracetamol 500mg N20",
        name_normalized="paracetamol",
        brand="Bayer",
        dosage="500mg",
        pack_size="n20",
    )
    b = _make_product(
        db_session,
        site="aptekonline",
        external_id="ap-1",
        name="PARACETAMOL 500MG #20",
        name_normalized="paracetamol",
        brand="Bayer",
        dosage="500mg",
        pack_size="n20",
    )
    c = _make_product(
        db_session,
        site="aloe",
        external_id="al-1",
        name="Paracetamol tab 500 mg 20 шт",
        name_normalized="paracetamol",
        brand="Bayer",
        dosage="500mg",
        pack_size="n20",
    )
    db_session.commit()

    n = matcher.match_products(db_session)
    assert n >= 1
    db_session.refresh(a)
    db_session.refresh(b)
    db_session.refresh(c)
    assert a.canonical_id is not None
    assert a.canonical_id == b.canonical_id == c.canonical_id


def test_no_match_for_different_brands(db_session):
    """Разные бренды одного действующего вещества — не должны матчиться."""
    a = _make_product(
        db_session,
        site="pharmonline",
        external_id="x1",
        name="Aspirin",
        name_normalized="aspirin",
        brand="Bayer",
        dosage="100mg",
        pack_size="n30",
    )
    b = _make_product(
        db_session,
        site="aloe",
        external_id="x2",
        name="Aspirin Generic",
        name_normalized="aspirin generic",
        brand="Generic",
        dosage="100mg",
        pack_size="n30",
    )
    db_session.commit()
    matcher.match_products(db_session)
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.canonical_id != b.canonical_id or (
        a.canonical_id is None and b.canonical_id is None
    )


def test_rejection_blocks_auto_match(db_session):
    """Если пара (a, b) в MatchRejection — auto-matcher не должен их склеить."""
    a = _make_product(
        db_session,
        site="pharmonline",
        external_id="ph-r1",
        name="Diazolin 0.1g N10",
        name_normalized="diazolin",
        brand="Darnitsa",
        dosage="0.1g",
        pack_size="n10",
    )
    b = _make_product(
        db_session,
        site="aloe",
        external_id="al-r1",
        name="Diazolin 0.1g N10",
        name_normalized="diazolin",
        brand="Darnitsa",
        dosage="0.1g",
        pack_size="n10",
    )
    db_session.commit()

    # Сначала проверяем, что без rejection они бы склеились
    n = matcher.match_products(db_session)
    assert n >= 1
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.canonical_id == b.canonical_id and a.canonical_id is not None

    # Развязываем + чистим Match
    match_actions.break_match(db_session, a.canonical_id, b.id, reason="test reject")
    db_session.refresh(a)
    db_session.refresh(b)

    # Теперь rejection должен блокировать повторный матч
    matcher.match_products(db_session)
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.canonical_id is None or b.canonical_id is None or a.canonical_id != b.canonical_id


def test_manual_override_preserved(db_session):
    """Если match помечен is_manual=True — автоматика его не должна менять."""
    m = storage.Match(canonical_name="Manually matched", confidence=1.0, is_manual=True)
    db_session.add(m)
    db_session.flush()

    a = _make_product(
        db_session,
        site="pharmonline",
        external_id="ph-2",
        name="Foo 100mg",
        name_normalized="foo",
        brand="X",
        dosage="100mg",
        pack_size="n10",
    )
    b = _make_product(
        db_session,
        site="aloe",
        external_id="al-2",
        name="Foo 100mg",
        name_normalized="foo",
        brand="X",
        dosage="100mg",
        pack_size="n10",
    )
    a.canonical_id = m.id
    b.canonical_id = m.id
    db_session.commit()

    matcher.match_products(db_session)
    db_session.refresh(a)
    db_session.refresh(b)
    db_session.refresh(m)
    assert a.canonical_id == b.canonical_id == m.id
    assert m.is_manual is True
