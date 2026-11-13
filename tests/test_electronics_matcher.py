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
        # Неверные пары, найденные независимым разбором первой версии.
        # «+» в названии модели — другая модель.
        (
            "Samsung Galaxy S23+ 8/256GB Phantom Black",
            "Samsung Galaxy S23 8/256GB Phantom Black",
            None,
        ),
        (
            "Xiaomi Redmi Note 13 Pro+ 5G 12/512GB Midnight Black",
            "Xiaomi Redmi Note 13 Pro 5G 12/512GB Midnight Black",
            None,
        ),
        ("Samsung Galaxy Tab A9+ 4/64GB Graphite", "Samsung Galaxy Tab A9 4/64GB Graphite", None),
        ("Xiaomi Robot Vacuum S10+", "Xiaomi Robot Vacuum S10", None),
        (
            "Samsung Galaxy S23+ 8/256GB Black",
            "Samsung Galaxy S23 Plus 8/256GB Black",
            em.STRATEGY_SERIES,
        ),
        # Диагональ — признак товара.
        (
            'Apple MacBook Air 13" M2 8/256GB Midnight',
            'Apple MacBook Air 15" M2 8/256GB Midnight',
            None,
        ),
        (
            'Apple iPad Pro 11" M4 256GB Wi-Fi Space Black',
            'Apple iPad Pro 13" M4 256GB Wi-Fi Space Black',
            None,
        ),
        ('Samsung 55" Crystal UHD 4K CU7100', 'Samsung 65" Crystal UHD 4K CU7100', None),
        ("Monitor LG UltraGear 27 inch 27GP850", "Monitor LG UltraGear 32 inch 27GP850", None),
        # Тот же код, другая память, процессор или вариант.
        (
            "Samsung Galaxy A55 5G SM-A556E 8/256GB Navy",
            "Samsung Galaxy A55 5G SM-A556E 12/256GB Navy",
            None,
        ),
        ("Noutbuk HP 15-fd0052nia 8/512GB", "Noutbuk HP 15-fd0052nia 16/512GB", None),
        (
            "Lenovo IdeaPad 3 15IAU7 i3-1215U 8GB/256GB",
            "Lenovo IdeaPad 3 15IAU7 i5-1235U 8GB/256GB",
            None,
        ),
        (
            "Asus Vivobook 15 X1504ZA i5-1235U 8/512GB",
            "Asus Vivobook 15 X1504ZA i3-1215U 8/512GB",
            None,
        ),
        (
            "Asus TUF Gaming F15 FX507ZC4 i5-12500H RTX3050 16/512GB",
            "Asus TUF Gaming F15 FX507ZC4 i7-12700H RTX3050 16/512GB",
            None,
        ),
        # Характеристика — не код модели.
        ("HP 250 G9 i5-1235U 8/512GB Silver", "HP ProBook 450 G9 i5-1235U 8/512GB Silver", None),
        ("Powerbank Hoco J101 10000mAh Black", "Powerbank Hoco J87 10000mAh Black", None),
        ("Baseus Bipow 10000mAh 20W Black", "Baseus Adaman 10000mAh 22.5W Black", None),
        ("Xiaomi Redmi 13C 5000mAh 4/128GB Black", "Xiaomi Redmi 12 5000mAh 4/128GB Black", None),
        ("TP-Link Archer AX55 AX3000", "TP-Link Archer AX53 AX3000", None),
        ("TP-Link Archer C6 AC1200", "TP-Link Archer C64 AC1200", None),
        ("Kingston Fury Beast 16GB DDR4-3200", "Kingston Fury Renegade 16GB DDR4-3200", None),
        ("OPPO Reno12 F 12/256GB", "OPPO Reno12 12/256GB", None),
        # «Ağ» — белый, а не слово-тип.
        ("Samsung RB34T600FSA Ağ", "Samsung RB34T600FSA Qara", None),
        ("Sony WH-CH520 Ağ", "Sony WH-CH520 Qara", None),
        ("Xiaomi Redmi 13C 4/128GB Ağ", "Xiaomi Redmi 13C 4/128GB", None),
        ("Samsung RB34T600FSA Ağ", "Samsung RB34T600FSA White", em.STRATEGY_CODE),
        # Оттенок, технология экрана, связь.
        ("Samsung Galaxy A15 4/128GB Light Blue", "Samsung Galaxy A15 4/128GB Blue", None),
        ("Steam Deck OLED 512GB", "Steam Deck LCD 512GB", None),
        ("Nintendo Switch OLED White", "Nintendo Switch White", None),
        ("JBL Charge 5 Blue", "JBL Charge 5 Wi-Fi Blue", None),
        # Аксессуар под именем модели.
        ("Çexol Samsung Galaxy A55 Black", "Samsung Galaxy A55 Black", None),
        ("Samsung Galaxy S24 Ultra üçün çexol Black", "Samsung Galaxy S24 Ultra Black", None),
        # Вторая охота разбора: 4K, NFC, Nano, мышь и клавиатура — признаки товара.
        ("Xiaomi Mi TV Stick 4K Black", "Xiaomi Mi TV Stick Black", None),
        ("Google Chromecast 4K White", "Google Chromecast HD White", None),
        (
            "Xiaomi Redmi 13C NFC 4/128GB Midnight Black",
            "Xiaomi Redmi 13C 4/128GB Midnight Black",
            None,
        ),
        ("Xiaomi Smart Band 8 NFC Black", "Xiaomi Smart Band 8 Black", None),
        ("TP-Link Archer T3U Nano", "TP-Link Archer T3U", None),
        ("Apple Magic Mouse White", "Apple Magic Keyboard White", None),
        ("HP 150 Wired Mouse Black", "HP 150 Wired Keyboard Black", None),
        ("Logitech G Pro Mouse Black", "Logitech G Pro Keyboard Black", None),
        # Один код, а у каждой стороны своё слово — разные товары.
        (
            "Samsung Galaxy A55 5G SM-A556E 8/256GB Awesome Lilac",
            "Samsung Galaxy A55 5G SM-A556E 8/256GB Awesome Lemon",
            None,
        ),
        (
            "Samsung Galaxy A55 5G SM-A556E 8/256GB Awesome Iceblue",
            "Samsung Galaxy A55 5G SM-A556E 8/256GB Awesome Navy",
            None,
        ),
        ("Samsung Galaxy A55 5G SM-A556E 8/128GB", "Samsung Galaxy A55 5G SM-A556E 8/256GB", None),
        ("Bosch SMS25AW01R Inox", "Bosch SMS25AW01R Ağ", None),
        ("Sony WH-CH520 Tünd göy", "Sony WH-CH520 Açıq göy", None),
        (
            "Lenovo IdeaPad 3 15IAU7 i5-1235U 8/512GB DOS",
            "Lenovo IdeaPad 3 15IAU7 i5-1235U 8/512GB Windows",
            None,
        ),
        ("Canon EOS 250D 18-55mm IS STM", "Canon EOS 250D 18-55mm DC III", None),
        # Один код, а сайт дописал подробность — тот же товар.
        ("LG 55UR78006LK", "Televizor LG 55UR78006LK LED Smart TV", em.STRATEGY_CODE),
        ("LG 55UR78006LK", "Televizor LG 55UR78006LK 139 sm", em.STRATEGY_CODE),
        ("LG 55UR78006LK", "LG 55UR78006LK 55 Smart 4K UHD", em.STRATEGY_CODE),
        ("Samsung QE55Q60DAUXRU", 'Televizor Samsung QLED QE55Q60DAUXRU 55"', em.STRATEGY_CODE),
        ("LG OLED55C4RLA", 'Televizor LG OLED 55" OLED55C4RLA', em.STRATEGY_CODE),
        ("Bosch WAN28282GC", "Paltaryuyan Bosch Serie 4 WAN28282GC 8 kq", em.STRATEGY_CODE),
        (
            "Samsung WW80T4020CE/LP",
            "Paltaryuyan maşın Samsung WW80T4020CE/LP 8 kq 1200 dövr",
            em.STRATEGY_CODE,
        ),
        ("Soyuducu Bosch KGN39VL25R Boz", "Bosch KGN39VL25R Gray 203 sm", em.STRATEGY_CODE),
        ("Philips 55PUS8108/12", "Televizor Philips 55PUS8108", em.STRATEGY_CODE),
        ("HP 15-fd0052nia i5-1335U 8/512GB", "Noutbuk HP 15-fd0052nia", em.STRATEGY_CODE),
        (
            "Samsung RB34T600FSA Gümüşü",
            "Soyuducu Samsung RB34T600FSA Silver No Frost",
            em.STRATEGY_CODE,
        ),
        # Третья охота разбора. Комплект, уценка, аксессуар под тем же кодом.
        (
            "Sony PlayStation 5 Slim CFI-2016A",
            "Sony PlayStation 5 Slim CFI-2016A + EA Sports FC 25",
            None,
        ),
        ("LG 55UR78006LK", "LG 55UR78006LK + LG SN5 soundbar", None),
        ("Samsung UE55CU7100UXRU", "Samsung UE55CU7100UXRU divar kronşteyni ilə", None),
        ("Sony ILCE-6400", "Sony ILCE-6400 Kit 16-50mm", None),
        (
            "Samsung Galaxy A55 SM-A556E 8/256GB Navy",
            "Samsung Galaxy A55 SM-A556E 8/256GB Navy İşlənmiş",
            None,
        ),
        (
            "Samsung Galaxy A55 SM-A556E 8/256GB Navy",
            "Samsung Galaxy A55 SM-A556E 8/256GB Navy Vitrin",
            None,
        ),
        (
            "Samsung Galaxy S24 SM-S921B 8/256GB",
            "Samsung Galaxy S24 SM-S921B 8/256GB Enterprise Edition",
            None,
        ),
        (
            "Samsung Galaxy Tab S9 SM-X710 8/128GB",
            "Samsung Galaxy Tab S9 SM-X710 8/128GB Keyboard Cover",
            None,
        ),
        ("Lenovo IdeaPad 3 15IAU7 8/512GB", "Lenovo IdeaPad 3 15IAU7 8/512GB Touch", None),
        ("Samsung Galaxy A55 SM-A556E 8/256GB", "Çexol Samsung Galaxy A55 SM-A556E üçün", None),
        ("Noutbuk HP 15-fd0052nia", "HP 15-fd0052nia üçün adapter 65W", None),
        (
            "Sony PlayStation 5 Slim CFI-2016A + EA Sports FC 25",
            "PlayStation 5 Slim CFI-2016A + FC 25 Sony",
            em.STRATEGY_CODE,
        ),
        # Числа при совпавшем коде — в счёт, когда названы у обоих.
        ("Samsung 55 CU7100 Crystal UHD", "Samsung 65 CU7100 Crystal UHD", None),
        ("Philips 55 PUS8108", "Philips 65 PUS8108", None),
        ("Lenovo IdeaPad 3 15IAU7", "Lenovo IdeaPad 5 15IAU7", None),
        # Оба сайта дописали к коду своё описание — товар тот же.
        (
            "Paltaryuyan maşın Samsung WW80T4020CE/LP",
            "Samsung WW80T4020CE 8 kq ağ",
            em.STRATEGY_CODE,
        ),
        ("Paltaryuyan maşını Bosch WAN28282GC", "Bosch WAN28282GC Ağ", em.STRATEGY_CODE),
        (
            "Televizor Samsung UE55CU7100UXRU Crystal UHD",
            "Samsung UE55CU7100UXRU Tizen Smart TV",
            em.STRATEGY_CODE,
        ),
        ("LG 55UR78006LK NanoCell", "Televizor LG 55UR78006LK webOS", em.STRATEGY_CODE),
        (
            "Samsung Galaxy A55 5G SM-A556E 8/256GB Awesome Navy",
            "Smartfon Samsung Galaxy A55 SM-A556E/DS 8/256GB Navy",
            em.STRATEGY_CODE,
        ),
        # Прописная «İ» не ломает бренд.
        ("İndesit IWSC 51051", "Indesit IWSC 51051", None),
    ],
)
def test_pair_rule(first, second, strategy):
    assert em._same_product(sig(first), sig(second)) == strategy
    assert em._same_product(sig(second), sig(first)) == strategy


def test_capital_dotted_i_does_not_break_the_brand():
    assert sig("İndesit IWSC51051").brand == "indesit"
    assert sig("İPHONE 15 128GB Black") == sig("iPhone 15 128GB Black")


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
    # И следующий прогон не подсаживает в готовую пару первого из двух.
    em.match_products(db_session)
    em.match_products(db_session)
    assert _clusters(db_session) == [(em.STRATEGY_SERIES, ["bakuelectronics", "kontakt"])]


def test_colourless_name_is_not_given_one_of_two_colours(db_session):
    # Kontakt не назвал цвет, у Irshad чёрный и серебристый: какой наш — не выбрать.
    kontakt = _product(db_session, "kontakt", "Sony WH-1000XM5", 600)
    _product(db_session, "irshad", "Sony WH-1000XM5 Black", 610)
    _product(db_session, "irshad", "Sony WH-1000XM5 Silver", 615)
    for _ in range(2):
        em.match_products(db_session)
    assert kontakt.canonical_id is None and _clusters(db_session) == []


def test_site_taken_by_a_dead_member_stays_taken(db_session):
    from src._time import utcnow

    _product(db_session, "kontakt", "LG 55UR78006LK", 1299)
    old = _product(db_session, "irshad", "LG 55UR78006LK", 1249, external_id="old")
    em.match_products(db_session)
    old.url_dead_at = utcnow()
    db_session.flush()
    fresh = _product(db_session, "irshad", "LG 55UR78006LK", 1259, external_id="new")
    em.match_products(db_session)
    assert fresh.canonical_id is None
    assert _clusters(db_session) == [(em.STRATEGY_CODE, ["irshad", "kontakt"])]


def test_product_wanted_by_two_clusters_joins_neither_and_starts_no_pair(db_session):
    # Две готовые пары одной модели (такое оставляют ручные правки): свободный
    # товар подходит обеим — не входит ни в одну и новую пару не начинает.
    for number in (1, 2):
        member = _product(db_session, "irshad", "LG 55UR78006LK", 1249, external_id=f"i{number}")
        match = Match(canonical_name=f"LG {number}", is_manual=False)
        db_session.add(match)
        db_session.flush()
        member.canonical_id = match.id
    kontakt = _product(db_session, "kontakt", "LG 55UR78006LK", 1299)
    baku = _product(db_session, "bakuelectronics", "Televizor LG 55UR78006LK", 1279)
    assert em.match_products(db_session) == 0
    assert kontakt.canonical_id is None and baku.canonical_id is None
    assert db_session.query(Match).count() == 2


def test_newcomer_must_suit_a_dead_member_and_a_fellow_newcomer(db_session):
    from src._time import utcnow

    # Чёрный холодильник с мёртвой ссылкой остаётся товаром пары: белый к ней
    # не присоединяется.
    _product(db_session, "kontakt", "Samsung RB34T600FSA", 1500)
    black = _product(db_session, "irshad", "Samsung RB34T600FSA Qara", 1480)
    em.match_products(db_session)
    black.url_dead_at = utcnow()
    db_session.flush()
    white = _product(db_session, "bakuelectronics", "Samsung RB34T600FSA Ağ", 1490)
    em.match_products(db_session)
    assert white.canonical_id is None

    # К паре из одного члена разом идут чёрный и серебристый с разных сайтов:
    # между собой они не один товар — входит только первый.
    lone = _product(db_session, "kontakt", "Sony WH-1000XM5", 600)
    match = Match(canonical_name="Sony", is_manual=False)
    db_session.add(match)
    db_session.flush()
    lone.canonical_id = match.id
    first = _product(db_session, "bakuelectronics", "Sony WH-1000XM5 Black", 610)
    second = _product(db_session, "irshad", "Sony WH-1000XM5 Silver", 615)
    em.match_products(db_session)
    assert [first.canonical_id, second.canonical_id].count(match.id) == 1


def test_one_sided_choice_is_not_a_pair(db_session):
    # Для Kontakt кандидат у Irshad один, а для него у Kontakt — два равных.
    _product(db_session, "kontakt", "LG 55UR78006LK", 1299, external_id="k1")
    _product(db_session, "kontakt", "Televizor LG 55UR78006LK", 1289, external_id="k2")
    _product(db_session, "irshad", "LG 55UR78006LK", 1249)
    assert em.match_products(db_session) == 0


def test_price_ratio_boundary(db_session):
    _product(db_session, "kontakt", "LG 55UR78006LK", 1000)
    _product(db_session, "irshad", "LG 55UR78006LK", 1600)
    _product(db_session, "kontakt", "Sony KD55X75WL", 1000)
    _product(db_session, "irshad", "Sony KD55X75WL", 1610)
    assert em.match_products(db_session) == 1
    assert _clusters(db_session)[0][1] == ["irshad", "kontakt"]
    assert db_session.query(Match).one().canonical_name == "LG 55UR78006LK"


def test_other_tenants_products_are_left_alone(db_session):
    _product(db_session, "kontakt", "LG 55UR78006LK", 1299)
    foreign = _product(db_session, "irshad", "LG 55UR78006LK", 1249)
    foreign.tenant_id = 2
    db_session.flush()
    assert em.match_products(db_session) == 0


def test_matching_ten_thousand_products_is_not_quadratic(db_session):
    # Перебор всех пар бренда на таком каталоге шёл бы минуты; по ключам — секунды.
    import time

    rows = []
    for number in range(3400):
        for site in SITES:
            rows.append(
                Product(
                    site=site,
                    external_id=f"{site}-{number}",
                    url=f"https://{site}.invalid/{number}",
                    name=f"Samsung Galaxy A{number:04d} 8/256GB Black",
                    name_normalized=str(number),
                )
            )
    db_session.add_all(rows)
    db_session.flush()
    started = time.monotonic()
    assert em.match_products(db_session) == 3400
    assert time.monotonic() - started < 30


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
