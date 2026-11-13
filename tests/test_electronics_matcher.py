"""Сопоставление товаров электроники по модели."""

from __future__ import annotations

import pytest

from src import electronics_matcher as em
from src import site_profile
from src.storage import Match, MatchRejection, PriceSnapshot, Product, Run

SITES = ("kontakt", "irshad", "bakuelectronics")


@pytest.fixture(autouse=True)
def electronics_sites(monkeypatch):
    # Набор тестов идёт под аптечным профилем; сопоставлению подставляются
    # сайты электроники. Экземпляр целиком проверяет отдельный процесс
    # (tests/test_electronics_instance.py).
    monkeypatch.setattr(site_profile, "SITES", SITES)
    monkeypatch.setattr(site_profile, "CLIENT_SITE", "kontakt")


def sig(name):
    return em.signature(name)


def test_phone_signature_reads_brand_model_memory_and_colour():
    kontakt = sig("Samsung Galaxy A55 8/256GB Awesome Navy")
    irshad = sig("Samsung Galaxy A55 8/256 GB Navy")
    baku = sig("Smartfon Samsung Galaxy A55 8GB/256GB Awesome Navy")
    assert kontakt == irshad == baku
    assert (kontakt.brand, kontakt.code, kontakt.ram, kontakt.storage) == ("samsung", None, 8, 256)
    assert kontakt.words == frozenset({"galaxy", "a55"})
    assert kontakt.colors == frozenset({"navy"})


def test_model_code_is_found_whatever_surrounds_it():
    names = [
        'LG 55UR78006LK 55" 4K Smart TV',
        "LG 55UR78006LK",
        'Televizor LG 55UR78006LK.AMCE 55"',
    ]
    assert {sig(name).code for name in names} == {"55ur78006lk"}
    assert {sig(name).brand for name in names} == {"lg"}
    # Память, диагональ и сеть — не код модели.
    assert sig("Apple iPhone 15 128GB Black").code is None
    assert sig("Xiaomi Redmi Note 13 Pro 5G 8/256GB").code is None


@pytest.mark.parametrize(
    ("first", "second", "strategy"),
    [
        ("LG 55UR78006LK", 'Televizor LG 55UR78006LK 55" Smart', em.STRATEGY_CODE),
        ("Apple iPhone 15 128GB Black", "Smartfon Apple iPhone 15 128 GB Qara", em.STRATEGY_SERIES),
        (
            "Apple iPhone 15 Pro Max 256GB Natural Titanium",
            "iPhone 15 Pro Max 256GB Natural Titanium Apple",
            em.STRATEGY_SERIES,
        ),
        (
            "Samsung Galaxy S24 Ultra 12/1TB Titanium Gray",
            "Samsung Galaxy S24 Ultra 12GB/1TB Titanium Grey",
            em.STRATEGY_SERIES,
        ),
        # Разное — не один товар.
        ("Apple iPhone 15 128GB Black", "Apple iPhone 15 256GB Black", None),
        ("Apple iPhone 15 128GB Black", "Apple iPhone 15 128GB Blue", None),
        ("Apple iPhone 15 128GB Black", "Apple iPhone 15 Pro 128GB Black", None),
        ("Apple iPhone 15 128GB Black", "Apple iPhone 15 Plus 128GB Black", None),
        ("Xiaomi Redmi Note 13 8/256GB Black", "Xiaomi Redmi Note 13 5G 8/256GB Black", None),
        ("Samsung Galaxy A55 8/256GB Navy", "Samsung Galaxy A55 8/256GB Blue", None),
        ("Samsung Galaxy A55 8/256GB Navy", "Samsung Galaxy A55 6/128GB Navy", None),
        ("LG 55UR78006LK", "LG 65UR78006LK", None),
        ("LG 55UR78006LK", "Samsung 55UR78006LK", None),
        # Чего название не говорит, то не додумывается.
        ("Apple iPhone 15 128GB Black", "Apple iPhone 15 128GB", None),
        ("Apple iPhone 15", "Apple iPhone 15", None),
        ("JBL Tune 520BT", "Qulaqlıq JBL Tune 520BT", em.STRATEGY_SERIES),
        ("Xiaomi Redmi Note 13 8/256GB Black", "Redmi Note 13 8/256 GB Qara", em.STRATEGY_SERIES),
        ("JBL Tune 520BT", "JBL Tune 720BT", None),
        ("Samsung RB34T600FSA White", "Samsung RB34T600FSA Black", None),
    ],
)
def test_pair_rule(first, second, strategy):
    assert em._same_product(sig(first), sig(second)) == strategy
    assert em._same_product(sig(second), sig(first)) == strategy


def test_name_without_a_brand_has_no_signature():
    assert sig("") is None
    assert sig(None) is None
    assert sig("Televizor") is None


def _product(session, site, name, price, external_id=None):
    product = Product(
        site=site,
        external_id=external_id or f"{site}-{name}"[:190],
        url=f"https://{site}.invalid/{abs(hash((site, name)))}",
        name=name,
        name_normalized=name.lower(),
    )
    session.add(product)
    session.flush()
    run = session.query(Run).first()
    if run is None:
        run = Run(status="ok")
        session.add(run)
        session.flush()
    session.add(PriceSnapshot(product_id=product.id, run_id=run.id, price=price))
    session.flush()
    return product


def _clusters(session):
    out = []
    for match in session.query(Match).order_by(Match.id).all():
        members = session.query(Product).filter(Product.canonical_id == match.id).all()
        out.append((match.match_strategy, sorted(product.site for product in members)))
    return out


def test_three_sites_come_together_and_the_client_names_the_cluster(db_session):
    _product(db_session, "irshad", "Samsung Galaxy A55 8/256 GB Navy", 879)
    kontakt = _product(db_session, "kontakt", "Samsung Galaxy A55 8/256GB Awesome Navy", 899)
    _product(
        db_session, "bakuelectronics", "Smartfon Samsung Galaxy A55 8GB/256GB Awesome Navy", 869
    )
    _product(db_session, "irshad", "LG 55UR78006LK", 1249)
    _product(db_session, "bakuelectronics", 'Televizor LG 55UR78006LK 55"', 1279)
    _product(db_session, "kontakt", "Apple iPhone 15 128GB Black", 1899)

    assert em.match_products(db_session) == 2
    db_session.commit()
    assert _clusters(db_session) == [
        (em.STRATEGY_SERIES, ["bakuelectronics", "irshad", "kontakt"]),
        (em.STRATEGY_CODE, ["bakuelectronics", "irshad"]),
    ]
    match = db_session.get(Match, kontakt.canonical_id)
    assert match.canonical_name == kontakt.name and match.canonical_brand == "samsung"
    assert (match.confidence, match.is_manual) == (0.9, False)

    # Второй прогон ничего не меняет.
    assert em.match_products(db_session) == 0
    assert len(_clusters(db_session)) == 2


def test_late_arrival_joins_the_existing_cluster(db_session):
    _product(db_session, "irshad", "LG 55UR78006LK", 1249)
    _product(db_session, "bakuelectronics", "Televizor LG 55UR78006LK", 1279)
    em.match_products(db_session)
    late = _product(db_session, "kontakt", "LG 55UR78006LK 4K Smart TV", 1299)
    assert em.match_products(db_session) == 0
    assert _clusters(db_session) == [(em.STRATEGY_CODE, ["bakuelectronics", "irshad", "kontakt"])]
    assert late.canonical_id is not None


def test_two_alike_products_of_one_site_leave_that_site_out(db_session):
    # У Irshad два неотличимых по названию товара: какой из них наш — не выбрать.
    _product(db_session, "kontakt", "Apple iPhone 15 128GB Black", 1899)
    _product(db_session, "irshad", "Apple iPhone 15 128GB Black", 1949, external_id="i1")
    _product(db_session, "irshad", "Apple iPhone 15 128 GB Qara", 1899, external_id="i2")
    _product(db_session, "bakuelectronics", "Smartfon Apple iPhone 15 128GB Black", 1899)
    em.match_products(db_session)
    assert _clusters(db_session) == [(em.STRATEGY_SERIES, ["bakuelectronics", "kontakt"])]


def test_price_far_apart_is_not_the_same_product(db_session):
    # Тот же код, а цена втрое другая: аксессуар или комплект под именем модели.
    _product(db_session, "kontakt", "LG 55UR78006LK", 1299)
    _product(db_session, "irshad", "LG 55UR78006LK", 399)
    assert em.match_products(db_session) == 0


def test_operator_rejection_and_manual_cluster_are_respected(db_session):
    a = _product(db_session, "kontakt", "LG 55UR78006LK", 1299)
    b = _product(db_session, "irshad", "LG 55UR78006LK", 1249)
    db_session.add(
        MatchRejection(product_a_id=min(a.id, b.id), product_b_id=max(a.id, b.id), reason="нет")
    )
    db_session.flush()
    assert em.match_products(db_session) == 0

    # Ручная пара: автомат в неё никого не добавляет.
    c = _product(db_session, "kontakt", "Sony KD55X75WL", 1500)
    d = _product(db_session, "irshad", "Sony KD55X75WL", 1480)
    manual = Match(canonical_name="Sony", is_manual=True)
    db_session.add(manual)
    db_session.flush()
    c.canonical_id = d.canonical_id = manual.id
    late = _product(db_session, "bakuelectronics", "Televizor Sony KD55X75WL", 1490)
    em.match_products(db_session)
    assert late.canonical_id is None


def test_dead_page_and_foreign_site_are_not_matched(db_session):
    from src._time import utcnow

    dead = _product(db_session, "kontakt", "LG 55UR78006LK", 1299)
    dead.url_dead_at = utcnow()
    _product(db_session, "irshad", "LG 55UR78006LK", 1249)
    _product(db_session, "aloe", "LG 55UR78006LK", 1249)
    db_session.flush()
    assert em.match_products(db_session) == 0
