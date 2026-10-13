"""Покрытие сопоставления: один товар в разной записи на разных сайтах.

Замер 2026-10-07 на копии прод-каталога: пара у конкурента была у 33% товаров
клиента, и свежий полный прогон матчера добавлял +1 кластер — это был потолок
логики, а не устаревшее состояние. Клиент жаловался, что «не находятся» Kreon,
Veqovi, Ozempik: товары лежали в каталоге всех сайтов, но не сопоставлялись.
Каждый тест ниже — один класс таких потерь либо защита от ложной пары, которую
расширение сопоставления могло бы внести.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from src import matcher, storage
from src._time import utcnow
from src.normalize import extract_dosage, extract_pack_size, normalize_name


def _product(session, site: str, name: str, **kw) -> storage.Product:
    """Товар с полями, выведенными из названия так же, как при записи прогона."""
    product = storage.Product(
        tenant_id=1,
        site=site,
        external_id=kw.pop("external_id", f"{site}-{name}"),
        url=kw.pop("url", f"https://{site}.example/{abs(hash((site, name)))}"),
        name=name,
        name_normalized=normalize_name(name),
        brand=kw.pop("brand", name.split()[0]),
        dosage=extract_dosage(name),
        pack_size=extract_pack_size(name),
        offer_availability_status=kw.pop("status", "in_stock"),
        availability_observed_at=utcnow(),
        **kw,
    )
    session.add(product)
    session.flush()
    return product


def _fake(name: str, **kw) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        name_normalized=normalize_name(name),
        dosage=extract_dosage(name),
        pack_size=extract_pack_size(name),
        brand=kw.pop("brand", name.split()[0]),
        url=kw.pop("url", None),
        site=kw.pop("site", "pharmonline"),
        **kw,
    )


def _same_cluster(*products: storage.Product) -> bool:
    ids = {p.canonical_id for p in products}
    return None not in ids and len(ids) == 1


# ── Ключ бакета ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # написание торгового имени
        (("Atiqen 100 ml (Gel)", "Atiqen"), ("Atigen gel  100 ml", "Atigen")),
        (("Aziraq 500 mq  № 3", "Aziraq"), ("Azirag-500   500 mq  N3", "Azirag-")),
        # aloe пишет в brand производителя, которого в названии нет
        (("Kreon 10000 №20 (Kapsula)", "Kreon"), ("Creon 10000 20 əd.", "Abbot")),
        # brand заполнен по-разному или пуст
        (
            ("Memoqinkar Q 100 ml (Şərbət)", "Memoqinkar"),
            ("Memoginkar-Q şərbət 100 ml", "Memoginkar-Q"),
        ),
        (("Ko Amlessa 4 mq/5 mq №30", None), ("Ko-Amlessa  4 mq/ 5 mq  N30", "Ko-Amlessa")),
        # запись дозы
        (("Ozempik 1.0 mq/doza 3.0 ml № 1", "Ozempik"), ("Ozempik  1 mq/doza  N1", "Ozempik")),
        (("Ramloden 10 mq / 5 mq № 30", "Ramloden"), ("Ramloden  5 mq/ 10 mq  N30", "Ramloden")),
        (
            ("Lukastin plus (10+5)mq № 30", "Lukastin"),
            ("Lukastin Plus  5 mq/ 10 mq  N30", "Lukastin"),
        ),
        (("Siqnum 50 mq / 2 ml № 6", "Siqnum"), ("Siqnum  50 mq  2 ml  N6", "Siqnum")),
        (("Yodomarin 200 mkq № 100", "Yodomarin"), ("İodomarin 200 mkg. 100 əd.", "Berlin-chemie")),
    ],
)
def test_bucket_key_is_the_same_for_one_product_written_differently(a, b):
    first, second = (_fake(name, brand=brand) for name, brand in (a, b))
    assert matcher._bucket_key(first) == matcher._bucket_key(second)


def test_bucket_key_still_separates_strength_and_pack():
    base = matcher._bucket_key(_fake("Prestans 5 mq/10 mq №30"))
    assert base != matcher._bucket_key(_fake("Prestans 5 mq/5 mq №30"))
    assert base != matcher._bucket_key(_fake("Prestans 5 mq/10 mq №90"))


# ── Guard'ы ──────────────────────────────────────────────────────────────────


def test_orphan_number_accepts_the_same_number_written_with_a_unit():
    a, b = "Aspirin 500 N20", "Aspirin 500 mq № 20"
    args = (normalize_name(a), normalize_name(b))
    assert matcher._has_conflicting_orphan_number(*args) is True  # без исходных названий — строго
    assert matcher._has_conflicting_orphan_number(*args, a, b) is False


def test_orphan_number_does_not_take_pack_count_for_strength():
    a, b = "Brand 30 N10", "Brand 10 mq №30"
    assert matcher._has_conflicting_orphan_number(normalize_name(a), normalize_name(b), a, b)


def test_dose_from_shared_unit_list_instead_of_glued_url_number():
    # Слаг pharmonline склеивает «(10+5)mq» в «105mq»; раньше из-за этого
    # revalidate распускал верную пару и ставил на неё постоянный отказ.
    pharm = _fake("Lukastin plus (10+5)mq № 30", url="https://x/lukastin-plus-105mq-30")
    aptek = _fake("Lukastin Plus  5 mq/ 10 mq  N30")
    assert matcher._doses_mg_for_product(pharm) == {5.0, 10.0}
    assert matcher._has_conflicting_dose(pharm, aptek) is False


def test_dose_total_mass_against_components_is_not_a_conflict():
    total = _fake("Ketoklin  N7 (vaginal şamlar)", url="https://x/product/ketoclin-500mg-n7")
    parts = _fake("Ketoklin (100+400)mq № 7")
    assert matcher._has_conflicting_dose(total, parts) is False


def test_dose_from_url_tolerates_a_lost_decimal_separator_only():
    name = _fake("Mesartan 20/12,5 mq 56 əd.")
    slug = _fake("Mesartan №56 (Tabletlər)", url="https://x/product/mesartan-20mq-125mq-56")
    assert matcher._has_conflicting_dose(name, slug) is False
    other = _fake("Mesartan №56 (Tabletlər)", url="https://x/product/mesartan-40mq-125mq-56")
    assert matcher._has_conflicting_dose(name, other) is True


def test_pen_dose_is_the_per_dose_value_not_content_or_concentration():
    per_dose = "Veqovi   0.25 mq/doza   1 mq  1.5 ml   N1  (Wegovy)"
    with_concentration = "Veqovi 0.25 mq (0.68 mq/ml) 1,5 ml № 1"
    assert matcher._doses_mg(per_dose) == matcher._doses_mg(with_concentration) == {0.25}
    # концентрация — единственная величина: сравнивается она
    assert matcher._doses_mg("Pulmares 0.25 mq/ml 2 ml №20") == {0.25}


def test_route_of_administration_separates_products_of_one_line():
    assert matcher._has_conflicting_route(
        "Otipaks 15 ml (qulaq damcısı)", "Otipaks 15 ml (göz damcısı)"
    )
    # рот и горло — один класс; путь назван только у одной стороны — не конфликт
    assert not matcher._has_conflicting_route("Lorstop (oral sprey)", "Lorstop (Sprey boğaz üçün)")
    assert not matcher._has_conflicting_route("Kartan 10 ml №10 (Ampulalar)", "Kartan N10 (oral)")


def test_container_is_compatible_with_its_content_but_not_with_tablets():
    assert not matcher._has_conflicting_form(
        "Magne B6 №10 (Ampulalar)", "Maqne B6 N10 (oral məhlul)"
    )
    assert not matcher._has_conflicting_form("Montel 4 mq №28 (Saşe)", "Montel 4 mq N28 (toz)")
    assert matcher._has_conflicting_form("Montel 4 mq №28 (Saşe)", "Montel 4 mq №28 (Tabletlər)")


def test_concentration_and_bottle_volume_are_not_unit_volumes():
    # «/ml» без числа — концентрация; «100 ml» без счёта штук — флакон целиком
    assert not matcher._has_conflicting_pack_volume(
        _fake("Biomeksin  2 ml  N10"), _fake("Biomeksin 50 mq/ml 10 əd")
    )
    assert not matcher._has_conflicting_pack_volume(
        _fake("Sirop 100 mq/5 ml"), _fake("Sirop 100 ml")
    )
    assert not matcher._has_conflicting_pack_volume(
        _fake("Yunafer  20 mq/ml  5 ml  N5"), _fake("Yunafer 20mg/1 ml № 5 (Ampulalar)")
    )
    # флакон, записанный знаменателем: «200 mq/15 ml» — это «200 mq/5 ml 15 ml»
    assert not matcher._has_conflicting_pack_volume(
        _fake("Azoksin 200 mq/5 ml 15 ml"), _fake("Azoksin 200 mq/15 ml 1 əd.")
    )


def test_unit_volume_written_as_denominator_or_separately():
    assert not matcher._has_conflicting_pack_volume(
        _fake("Siqnum 50 mq / 2 ml № 6"), _fake("Siqnum  50 mq  2 ml  N6")
    )
    assert matcher._has_conflicting_pack_volume(
        _fake("Dolpan 75 mq/3 ml № 10"), _fake("Dolpan   75 mq  2 ml  N10")
    )


# ── Проходы матчера ──────────────────────────────────────────────────────────


def test_one_product_written_three_ways_becomes_one_cluster(db_session):
    """Жалоба клиента: Kreon 10000 есть на всех трёх сайтах, пары не было."""
    pharm = _product(db_session, "pharmonline", "Kreon 10000 №20 (Kapsula)")
    aptek = _product(db_session, "aptekonline", "Kreon 10000  N20")
    aloe = _product(db_session, "aloe", "Creon 10000 20 əd.", brand="Abbot")
    other_strength = _product(db_session, "aptekonline", "Kreon  25000  N20")
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(pharm, aptek, aloe)
    assert other_strength.canonical_id is None


def test_pen_products_match_by_dose(db_session):
    pairs = [
        (
            _product(db_session, "pharmonline", f"Veqovi {dose} mq ({conc} mq/ml) {vol} ml № 1"),
            _product(
                db_session,
                "aptekonline",
                f"Veqovi   {dose} mq/doza   {total} mq  {vol} ml   N1  (Wegovy)",
            ),
        )
        for dose, conc, total, vol in [("0.25", "0.68", "1", "1.5"), ("1", "1.34", "4", "3")]
    ]
    db_session.commit()

    matcher.match_products(db_session)

    assert all(_same_cluster(pharm, aptek) for pharm, aptek in pairs)
    assert pairs[0][0].canonical_id != pairs[1][0].canonical_id


def test_out_of_stock_twin_does_not_block_the_in_stock_pair(db_session):
    """Раньше кластер собирался с товаром не в наличии и отвергался целиком."""
    pharm = _product(db_session, "pharmonline", "Bonlak №30 (Tabletlər)")
    aloe = _product(db_session, "aloe", "Bonlak 30 əd", brand="Aspar ilaç")
    gone = _product(db_session, "aptekonline", "Bonlak  N30", status="out_of_stock")
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(pharm, aloe)
    assert gone.canonical_id is None


def test_matched_product_is_not_pulled_into_another_cluster(db_session):
    """Новый сосед по бакету не перетаскивает товар, у которого пара уже есть."""
    old = storage.Match(tenant_id=1, canonical_name="Hepatic N30", confidence=1.0)
    db_session.add(old)
    db_session.flush()
    aloe = _product(db_session, "aloe", "Hepatic 30 əd.", brand="Vefa ilaç", canonical_id=old.id)
    aptek_old = _product(db_session, "aptekonline", "Hepatic  N30", canonical_id=old.id)
    aptek_new = _product(db_session, "aptekonline", "Hepatik   N30 (Hepatic)", external_id="a2")
    pharm = _product(db_session, "pharmonline", "Hepatik № 30 ( Tabletlər)")
    db_session.commit()

    matcher.match_products(db_session)

    assert aloe.canonical_id == aptek_old.canonical_id == old.id
    assert aptek_new.canonical_id != old.id


def test_swapped_dose_order_matches_only_where_order_does_not_tell_products_apart(db_session):
    # Ramloden: у каждого сайта один порядок записи → это один товар.
    ramloden = [
        _product(db_session, "pharmonline", "Ramloden 10 mq / 5 mq № 30"),
        _product(db_session, "aptekonline", "Ramloden  5 mq/ 10 mq  N30"),
    ]
    # Prestans: оба порядка продаются на одном сайте → порядок значим.
    p_5_10 = _product(db_session, "pharmonline", "Prestans 5 mq/10 mq №30 (Tabletlər)")
    p_10_5 = _product(db_session, "pharmonline", "Prestans 10 mq/5 mq №30 (Tabletlər)")
    a_5_10 = _product(db_session, "aptekonline", "Prestans   5 mq/ 10 mq  N30")
    a_10_5 = _product(db_session, "aptekonline", "Prestans   10 mq/ 5 mq  N30")
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(*ramloden)
    assert _same_cluster(p_5_10, a_5_10)
    assert _same_cluster(p_10_5, a_10_5)
    assert p_5_10.canonical_id != p_10_5.canonical_id


def test_candidate_goes_to_its_exact_twin_not_to_the_first_anchor(db_session):
    baby = _product(db_session, "pharmonline", "Foral baby 30 ml (Sprey)")
    plain = _product(db_session, "pharmonline", "Foral sprey 30 ml (Sprey)", external_id="p2")
    other = _product(db_session, "aptekonline", "Foral  30 ml (boğaz spreyi)")
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(plain, other)
    assert baby.canonical_id is None


def test_generic_that_fits_two_incompatible_products_is_left_unmatched(db_session):
    """У aloe форма не названа, а у конкурента под этим именем и саше, и таблетки."""
    sachet = _product(db_session, "pharmonline", "Montel 4 mq №28 (Saşe)")
    tablets = _product(db_session, "pharmonline", "Montel 4 mq №28 (Tabletlər)", external_id="p2")
    unknown = _product(db_session, "aloe", "Montel 4 mq 28 əd", brand="Neutec")
    db_session.commit()

    matcher.match_products(db_session)

    assert unknown.canonical_id is None
    assert sachet.canonical_id is None and tablets.canonical_id is None


def test_crowded_first_word_requires_the_whole_name_to_agree(db_session):
    """«Şpris 5 ml» и «Şpris 5 ml Braun» — разные товары; у редкого торгового
    имени лишнее слово остаётся подробностью записи."""
    for i in range(matcher._CROWDED_FIRST_TOKEN):
        _product(
            db_session,
            "aptekonline",
            f"Şpris model{i} 50 ml",
            external_id=f"filler-{i}",
            brand=None,
        )
    generic = _product(db_session, "aloe", "Şpris 5 ml", brand=None)
    branded = _product(db_session, "pharmonline", 'Şpris 5 ml "Braun"', brand=None)
    rare = _product(db_session, "aptekonline", "Benoral B  N50")
    rare_verbose = _product(db_session, "pharmonline", "Benoral B kompleksi № 50 (Həblər)")
    db_session.commit()

    matcher.match_products(db_session)

    assert generic.canonical_id is None and branded.canonical_id is None
    assert _same_cluster(rare, rare_verbose)


def _blocked_by_name(a: str, b: str) -> bool:
    na, nb = normalize_name(a), normalize_name(b)
    return (
        matcher._has_conflicting_variant_tokens(na, nb)
        or matcher._has_conflicting_modifier(na, nb)
        or matcher._has_conflicting_orphan_number(na, nb, a, b)
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Lopril-H 10 mq N30", "Lopril 10 mq № 30"),
        ("Bivoksa-D 0.5% 5 ml (göz damcısı)", "Bivoksa 0,5% 5 ml (Göz damcısı)"),
        ("Uroseptin-N 30 əd", "Uroseptin  N30"),
        ("Smektit-Plus N10 (toz)", "Smektit 10 əd"),
        ("Qlükoza-E  5%  200 ml", "Qlükoza 5% 200 ml (Məhlul)"),
        ("Enap-HL 10 mq N20", "Enap 10 mq №20"),
    ],
)
def test_letter_code_after_a_hyphen_is_part_of_the_name(a, b):
    """Разбор по словам не должен превращать «X-H» в «X» с подробностью."""
    assert _blocked_by_name(a, b) and _blocked_by_name(b, a)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # те же слова, с дефисом и без
        ("Sitramon P  N6", "Sitramon-P № 6 (Tabletlər)"),
        ("Klion D 100 mq № 10", "Klion-D 100 mq 10 əd."),
        ("Smektit Plus N10", "Smektit-Plus 10 əd"),
        ("Neo-Terjinan  N10", "Neo Terjinan № 10"),
        ("Amoksi Denk 1000 mq № 10", "Amoksi-Denk   1000 mq  N10 (Amoxi-Denk)"),
        ("Agron-150  N30", "Aqron-150 № 30 (Kapsulalar)"),
        ("D-Kolerol  5000 BV  N28 (D-Colerol)", "D colerol 5000 I.U № 28"),
        # латинское написание в скобках, которое свёртка не сводит с основным (x ↔ ch)
        ("Kolxikum-Dispert 0.5 mq N50 (Colchicum-Dispert)", "Kolxikum-Dispert 0.5 mq № 50"),
        # число после дефиса — доза, она же записана у другой стороны с единицей
        ("Azirag-500   500 mq  N3", "Aziraq 500 mq  № 3"),
        # производитель после дефиса — подробность одной стороны
        ("Sefazolin-Akos 1 q N1", "Sefazolin 1 qr №1"),
        # буква в начале составного слова — название, а не модификатор
        ("Delabs D-3 15 ml", "Delabs damcı 15 ml"),
    ],
)
def test_hyphen_does_not_split_one_product_in_two(a, b):
    assert not _blocked_by_name(a, b) and not _blocked_by_name(b, a)


def test_leftover_does_not_pair_with_a_variant_when_its_twin_is_taken(db_session):
    """Вторая строка «Almagel A» осталась без пары: её двойник «Almaqel A» уже
    в кластере, и место её сайта там занято. Парой для «Almaqel» (без A) она от
    этого не становится."""
    taken = storage.Match(tenant_id=1, canonical_name="Almaqel A", confidence=1.0)
    db_session.add(taken)
    db_session.flush()
    _product(db_session, "pharmonline", "Almaqel A 170 ml (Suspenziya)", canonical_id=taken.id)
    _product(db_session, "aptekonline", "Almagel A suspenziya  170 ml", canonical_id=taken.id)
    plain = _product(db_session, "pharmonline", "Almaqel 170 ml (Suspenziya)", external_id="p2")
    leftover = _product(db_session, "aptekonline", "Almagel A suspenziya 170 ml", external_id="a2")
    db_session.commit()

    matcher.match_products(db_session)

    assert leftover.canonical_id is None
    assert plain.canonical_id is None


def test_two_rows_of_one_site_cannot_join_a_cluster_in_one_run(db_session):
    """Состав кластера после записи читается заново: товар, принятый только что,
    уже занимает место своего сайта."""
    match = storage.Match(tenant_id=1, canonical_name="Almagel", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    aptek = _product(db_session, "aptekonline", "Almagel suspenziya  170 ml", canonical_id=match.id)
    aloe = _product(db_session, "aloe", "Almagel 170 ml", brand="Balkan", canonical_id=match.id)
    first = _product(db_session, "pharmonline", "Almaqel 170 ml (Suspenziya)")
    second = _product(db_session, "pharmonline", "Almaqel 170 ml", external_id="p2")
    db_session.commit()

    assert matcher._persist_match(db_session, [aptek, first]) == 1
    assert matcher._persist_match(db_session, [aloe, second]) == 0

    assert (first.canonical_id, second.canonical_id) == (match.id, None)


def test_operator_rejection_holds_when_joining_through_a_third_member(db_session):
    from src import match_actions

    match = storage.Match(tenant_id=1, canonical_name="Bonlak", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    aptek = _product(db_session, "aptekonline", "Bonlak  N30", canonical_id=match.id)
    aloe = _product(db_session, "aloe", "Bonlak 30 əd", brand="Aspar", canonical_id=match.id)
    newcomer = _product(db_session, "pharmonline", "Bonlak №30 (Tabletlər)")
    match_actions.add_rejection(db_session, aloe.id, newcomer.id, reason="operator")
    db_session.commit()

    assert matcher._persist_match(db_session, [aptek, newcomer]) == 0
    assert newcomer.canonical_id is None


def test_newcomer_is_checked_against_every_member_of_the_cluster_it_joins(db_session):
    """Проход видит только членов из своего бакета; остальных сверяет запись."""
    existing = storage.Match(tenant_id=1, canonical_name="Otipaks", confidence=1.0)
    db_session.add(existing)
    db_session.flush()
    terse = _product(db_session, "aptekonline", "Otipaks  15 ml", canonical_id=existing.id)
    eye = _product(
        db_session,
        "aloe",
        "Otipaks göz damcısı 15 q",  # другая фасовка в записи → другой бакет
        brand="Biocodex",
        canonical_id=existing.id,
    )
    ear = _product(db_session, "pharmonline", "Otipaks 15 ml (Qulaq damcısı)")
    db_session.commit()

    matcher.match_products(db_session)

    assert ear.canonical_id is None
    assert terse.canonical_id == eye.canonical_id == existing.id


# ── Служебные проходы ────────────────────────────────────────────────────────


def test_refresh_derived_fields_recomputes_what_the_name_says(db_session):
    stale = _product(db_session, "aptekonline", "Kreon 10000  N20")
    stale.name_normalized, stale.dosage, stale.pack_size = "kreon", "1.0mq", "n20"
    fresh = _product(db_session, "pharmonline", "Kreon 10000 №20 (Kapsula)")
    db_session.commit()

    assert matcher.refresh_derived_fields(db_session) == 1
    assert stale.name_normalized == "kreon 10000"
    assert stale.dosage == "1.0mq"  # в названии дозы нет — прежнее значение не затираем
    assert fresh.name_normalized == "kreon 10000"


def _stale_cluster(session, *, twin_url_same: bool, manual: bool = False, **twin_kw):
    match = storage.Match(tenant_id=1, canonical_name="Ferrovef", confidence=1.0, is_manual=manual)
    session.add(match)
    session.flush()
    long_ago = utcnow() - timedelta(days=120)
    old = _product(
        session,
        "pharmonline",
        "Ferrovef № 60 (Kapsulalar)",
        external_id="slug",
        url="https://pharmonline.example/ferrovef-60",
        canonical_id=match.id,
        last_seen_at=long_ago,
    )
    partner = _product(session, "aptekonline", "Ferrovef   N60", canonical_id=match.id)
    twin = _product(
        session,
        "pharmonline",
        "Ferrovef № 60 (Kapsulalar)",
        external_id="hash",
        url=old.url if twin_url_same else "https://pharmonline.example/other",
        **twin_kw,
    )
    session.commit()
    return match, old, partner, twin


@pytest.mark.parametrize("twin_url_same", [True, False])
def test_relink_gives_the_cluster_slot_to_the_live_twin(db_session, twin_url_same):
    match, old, partner, twin = _stale_cluster(db_session, twin_url_same=twin_url_same)

    assert matcher.relink_stale_members(db_session, dry_run=True) and twin.canonical_id is None
    relinked = matcher.relink_stale_members(db_session)

    assert relinked == [
        {"match_id": match.id, "site": "pharmonline", "old": old.id, "new": twin.id}
    ]
    assert (old.canonical_id, twin.canonical_id, partner.canonical_id) == (None, match.id, match.id)


def test_relink_leaves_manual_clusters_and_other_countries_alone(db_session):
    manual = _stale_cluster(db_session, twin_url_same=True, manual=True)
    assert matcher.relink_stale_members(db_session) == []
    assert manual[1].canonical_id == manual[0].id

    for product in manual[1:]:
        db_session.delete(product)
    db_session.commit()
    _, old, _, twin = _stale_cluster(
        db_session,
        twin_url_same=True,
        manufacturer_country_code="de",
        country_resolution_status="resolved",
    )
    old.manufacturer_country_code, old.country_resolution_status = "tr", "resolved"
    db_session.commit()

    assert matcher.relink_stale_members(db_session) == []
    assert twin.canonical_id is None


def test_relink_then_match_in_one_unflushed_session_keeps_one_row_per_site(db_session):
    """Как в пайплайне: без commit между шагами, сессия без autoflush.

    Без flush в relink матчер читал состав кластера из БД, видел прежних членов
    и добавлял старую строку обратно — два товара одного сайта в кластере.
    """
    match, old, partner, twin = _stale_cluster(db_session, twin_url_same=True)

    # Без точки сохранения из пайплайна: её закрытие само делает flush и
    # скрыло бы ошибку, а функция должна быть верна и при прямом вызове.
    matcher.refresh_derived_fields(db_session)
    matcher.relink_stale_members(db_session)
    matcher.match_products(db_session)

    assert (old.canonical_id, twin.canonical_id, partner.canonical_id) == (None, match.id, match.id)
    assert sorted(p.site for p in db_session.get(storage.Match, match.id).products) == [
        "aptekonline",
        "pharmonline",
    ]


def test_relink_twin_must_be_the_same_product_as_the_row_it_replaces(db_session):
    """Форма и путь введения вырезаны из нормализованного имени — имена равны."""
    match = storage.Match(tenant_id=1, canonical_name="Otipaks", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    old = _product(
        db_session,
        "pharmonline",
        "Otipaks 15 ml (göz damcısı)",
        external_id="old",
        canonical_id=match.id,
        last_seen_at=utcnow() - timedelta(days=120),
    )
    _product(db_session, "aptekonline", "Otipaks  15 ml", canonical_id=match.id)
    other = _product(db_session, "pharmonline", "Otipaks 15 ml (qulaq damcısı)", external_id="new")
    db_session.commit()

    assert matcher.relink_stale_members(db_session) == []
    assert (old.canonical_id, other.canonical_id) == (match.id, None)


def test_relink_checks_twins_against_each_other(db_session):
    """Два двойника из разных стран не должны сойтись в одном кластере."""
    match = storage.Match(tenant_id=1, canonical_name="Serovin", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    long_ago = utcnow() - timedelta(days=120)
    for site in ("pharmonline", "aptekonline"):
        _product(
            db_session,
            site,
            "Serovin № 30",
            external_id=f"{site}-old",
            url=f"https://{site}.example/serovin",
            canonical_id=match.id,
            last_seen_at=long_ago,
        )
    twins = [
        _product(
            db_session,
            site,
            "Serovin № 30",
            external_id=f"{site}-new",
            url=f"https://{site}.example/serovin",
            manufacturer_country_code=country,
            country_resolution_status="resolved",
        )
        for site, country in (("pharmonline", "tr"), ("aptekonline", "de"))
    ]
    db_session.commit()

    relinked = matcher.relink_stale_members(db_session)

    assert len(relinked) == 1
    assert sum(1 for twin in twins if twin.canonical_id == match.id) == 1


def test_relink_needs_the_same_name_not_just_the_same_url(db_session):
    """На aptekonline оттенки краски и модели очков делят один адрес страницы."""
    match = storage.Match(tenant_id=1, canonical_name="Maxx Deluxe 6.0", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    url = "https://aptekonline.example/product/kr-maxx-deluxe"
    old = _product(
        db_session,
        "aptekonline",
        'Saç boyası "Maxx Deluxe" 6.0 tünd sarışın 2x50 ml',
        external_id="shade-6",
        url=url,
        canonical_id=match.id,
        last_seen_at=utcnow() - timedelta(days=120),
    )
    _product(
        db_session, "pharmonline", "Saç boyası Maxx Deluxe 6.0 tünd sarışın", canonical_id=match.id
    )
    other_shade = _product(
        db_session,
        "aptekonline",
        'Saç boyası "Maxx Deluxe" 7.1 küllü sarışın 2x50 ml',
        external_id="shade-7",
        url=url,
    )
    db_session.commit()

    assert matcher.relink_stale_members(db_session) == []
    assert (old.canonical_id, other_shade.canonical_id) == (match.id, None)


def test_relink_waits_until_most_of_the_site_catalog_was_seen_recently(db_session):
    """Идут только частичные тики: «давно не видели» — это «сбор не проходил»."""
    match, old, _partner, twin = _stale_cluster(db_session, twin_url_same=True)
    for i in range(3):
        _product(
            db_session,
            "pharmonline",
            f"Unseen product {i} № 10",
            external_id=f"unseen-{i}",
            last_seen_at=utcnow() - timedelta(days=120),
        )
    db_session.commit()

    assert matcher.relink_stale_members(db_session) == []
    assert (old.canonical_id, twin.canonical_id) == (match.id, None)


# ── Двойник уже с парой: один товар лежит двумя кластерами ───────────────────
#
# Жалоба клиента 2026-10-09: «Kreon 25000» показывал цены aloe и aptekonline без
# цены pharmonline. У aloe в июле сменился адрес товара; товар клиента остался в
# паре со старой строкой aloe, а новая сошлась с aptekonline отдельным
# кластером. Замер на проде 2026-10-12: 79 пропавших членов кластеров, у которых
# живой двойник состоит в другом кластере.


def _split_product(session, *, lost_site="aloe", lost_kw=None, live_kw=None, client_kw=None):
    """Один товар двумя кластерами: в `left` пропавшая строка, в `right` живой двойник."""
    left = storage.Match(tenant_id=1, canonical_name="Kreon 25000", confidence=1.0)
    right = storage.Match(tenant_id=1, canonical_name="Kreon 25000", confidence=1.0)
    session.add_all([left, right])
    session.flush()
    lost = _product(
        session,
        lost_site,
        "Kreon 25000, 20 əd",
        external_id="old-slug",
        brand="Abbot",
        canonical_id=left.id,
        last_seen_at=utcnow() - timedelta(days=100),
        **(lost_kw or {}),
    )
    client = _product(
        session,
        "pharmonline",
        "Kreon 25000 №20 (Kapsula)",
        canonical_id=left.id,
        **(client_kw or {}),
    )
    live = _product(
        session,
        lost_site,
        "Kreon 25000, 20 əd",
        external_id="new-number",
        brand="Abbot",
        canonical_id=right.id,
        **(live_kw or {}),
    )
    third = _product(session, "aptekonline", "Kreon  25000  N20", canonical_id=right.id)
    session.commit()
    return SimpleNamespace(left=left, right=right, lost=lost, client=client, live=live, third=third)


class _MatcherLog:
    """События журнала матчера вместе с полями.

    `structlog.testing.capture_logs` здесь не годится: тест, вошедший в CLI
    раньше, оставляет журнал на уровне INFO до конца прогона, и событие уровня
    debug до перехватчика не доходит (в одиночку файл проходил, в общем прогоне
    падал).
    """

    def __init__(self):
        self.events: list[dict] = []

    def __getattr__(self, level):
        def record(event, **fields):
            self.events.append({"event": event, **fields})

        return record

    def refusals(self) -> list[tuple[str, int]]:
        return [
            (entry["reason"], entry["stale_id"])
            for entry in self.events
            if entry["event"] == "matcher_split_merge_refused"
        ]

    def kept_strangers(self) -> list[tuple[str, int]]:
        return [
            (entry["reason"], entry["product"])
            for entry in self.events
            if entry["event"] == "matcher_price_stranger_kept"
        ]


def _clusters(session) -> dict[int, list[tuple[str, str]]]:
    session.expire_all()
    return {
        match.id: sorted((product.site, product.external_id) for product in match.products)
        for match in session.query(storage.Match).all()
    }


def test_relink_merges_the_cluster_of_a_lost_row_with_the_cluster_of_its_live_twin(db_session):
    case = _split_product(db_session)
    before = _clusters(db_session)

    assert matcher.relink_stale_members(db_session, dry_run=True)
    # dry_run не меняет ни базу, ни объекты сессии: изменённый объект ушёл бы в
    # базу с ближайшим commit вызывающего.
    assert not db_session.dirty and not db_session.deleted
    assert _clusters(db_session) == before

    merged = matcher.relink_stale_members(db_session)

    # Остаётся кластер, где членов больше: меняется меньше назначений.
    assert merged == [
        {
            "match_id": case.right.id,
            "site": "aloe",
            "old": case.lost.id,
            "new": case.live.id,
            "merged": case.left.id,
            "moved": [case.client.id],
            "dropped": [],
        }
    ]
    assert _same_cluster(case.client, case.live, case.third)
    assert case.lost.canonical_id is None
    assert list(_clusters(db_session)) == [case.right.id]  # пустая запись кластера удалена
    assert matcher.relink_stale_members(db_session) == []  # второй прогон ничего не находит


def test_merge_keeps_the_older_cluster_when_both_are_the_same_size(db_session):
    case = _split_product(db_session)
    extra = _product(db_session, "extra-site", "Kreon 25000 N20", canonical_id=case.left.id)
    db_session.commit()

    merged = matcher.relink_stale_members(db_session)

    assert (merged[0]["match_id"], merged[0]["merged"]) == (case.left.id, case.right.id)
    assert merged[0]["moved"] == [case.live.id, case.third.id]
    assert _same_cluster(case.client, case.live, case.third, extra)


def test_merge_refuses_two_live_rows_of_one_site(db_session):
    """Два живых товара одного сайта — два предложения, а не один товар."""
    case = _split_product(db_session)
    rival = _product(
        db_session,
        "aptekonline",
        "Kreon  25000  N20",
        external_id="another-listing",
        canonical_id=case.left.id,
    )
    db_session.commit()
    before = _clusters(db_session)

    assert matcher.relink_stale_members(db_session) == []
    assert _clusters(db_session) == before
    assert rival.canonical_id == case.left.id


@pytest.mark.parametrize("same_product", [True, False])
def test_merge_drops_the_lost_twin_each_cluster_holds_for_the_other(db_session, same_product):
    """Кластеры держат пропавшие строки друг друга: остаются только живые.

    Пропавшая строка сайта клиента в кластере двойника уступает живой, только
    если это тот же товар; другая фасовка — другой товар, и слияния нет.
    """
    case = _split_product(db_session)
    lost_client = _product(
        db_session,
        "pharmonline",
        "Kreon 25000 №20 (Kapsula)" if same_product else "Kreon 25000 №50 (Kapsula)",
        external_id="client-old-slug",
        canonical_id=case.right.id,
        last_seen_at=utcnow() - timedelta(days=100),
    )
    db_session.commit()
    before = _clusters(db_session)

    merged = matcher.relink_stale_members(db_session)

    if not same_product:
        assert merged == [] and _clusters(db_session) == before
        return
    assert len(merged) == 1 and merged[0]["dropped"] == [lost_client.id]
    assert _same_cluster(case.client, case.live, case.third)
    assert (case.lost.canonical_id, lost_client.canonical_id) == (None, None)
    assert sorted(product.site for product in case.live.canonical.products) == [
        "aloe",
        "aptekonline",
        "pharmonline",
    ]


@pytest.mark.parametrize(
    "obstacle",
    ["rejection", "country", "manual_left", "manual_right", "sold_out", "sold_out_client"],
)
def test_merge_leaves_both_clusters_alone_when_members_must_not_meet(db_session, obstacle):
    from src import match_actions

    client_kw = {}
    if obstacle == "country":
        # Страна — часть идентичности: товар клиента из Турции, у конкурента Германия.
        client_kw = {"manufacturer_country_code": "tr", "country_resolution_status": "resolved"}
    case = _split_product(db_session, client_kw=client_kw)
    if obstacle == "country":
        case.third.manufacturer_country_code = "de"
        case.third.country_resolution_status = "resolved"
    elif obstacle == "rejection":
        match_actions.add_rejection(db_session, case.client.id, case.third.id, reason="operator")
    elif obstacle == "manual_left":
        case.left.is_manual = True
    elif obstacle == "manual_right":
        case.right.is_manual = True
    elif obstacle == "sold_out":
        case.third.offer_availability_status = "out_of_stock"
    elif obstacle == "sold_out_client":
        case.client.offer_availability_status = "out_of_stock"
    db_session.commit()
    before = _clusters(db_session)

    assert matcher.relink_stale_members(db_session) == []
    assert _clusters(db_session) == before


def test_merge_needs_the_twin_to_be_the_same_product_as_the_lost_row(db_session):
    """Страна пропавшей строки известна, у «двойника» другая — это другой товар."""
    case = _split_product(
        db_session,
        lost_kw={"manufacturer_country_code": "de", "country_resolution_status": "resolved"},
        live_kw={"manufacturer_country_code": "tr", "country_resolution_status": "resolved"},
    )
    before = _clusters(db_session)

    assert matcher.relink_stale_members(db_session) == []
    assert _clusters(db_session) == before
    assert case.lost.canonical_id == case.left.id


def test_merge_then_match_in_one_unflushed_session_keeps_one_row_per_site(db_session):
    """Как в пайплайне: подготовка, сопоставление и перепроверка без commit между ними."""
    case = _split_product(db_session)

    matcher.refresh_derived_fields(db_session)
    matcher.relink_stale_members(db_session)
    matcher.match_products(db_session)
    matcher.revalidate_split(db_session)

    assert _same_cluster(case.client, case.live, case.third)
    assert case.lost.canonical_id is None
    sites = [product.site for product in db_session.get(storage.Match, case.right.id).products]
    assert sorted(sites) == ["aloe", "aptekonline", "pharmonline"]


def test_merge_carries_costs_bound_to_the_dropped_cluster(db_session):
    """Закупочная цена привязана к кластеру: удаление его записи унесло бы её каскадом."""
    case = _split_product(db_session)
    db_session.add_all(
        [
            storage.SupplierPrice(canonical_id=case.left.id, supplier_name="s", purchase_price=9.5),
            storage.StockLevel(canonical_id=case.left.id, qty=3, is_in_stock=True),
        ]
    )
    db_session.commit()

    matcher.relink_stale_members(db_session)
    db_session.commit()

    assert db_session.query(storage.SupplierPrice).one().canonical_id == case.right.id
    assert db_session.query(storage.StockLevel).one().canonical_id == case.right.id


def test_merge_needs_a_twin_that_knows_the_country_of_the_lost_row(db_session):
    """Двойник не должен знать о стране меньше пропавшей строки."""
    case = _split_product(
        db_session,
        lost_kw={"manufacturer_country_code": "de", "country_resolution_status": "resolved"},
    )
    before = _clusters(db_session)

    assert matcher.relink_stale_members(db_session) == []
    assert _clusters(db_session) == before
    assert case.live.manufacturer_country_code is None


@pytest.mark.parametrize(
    "flaw", ["dead_url", "site_not_scraped", "second_twin", "rival_without_pair"]
)
def test_merge_needs_exactly_one_live_twin_on_a_site_that_was_scraped(db_session, flaw):
    case = _split_product(db_session)
    if flaw == "dead_url":
        # Страница двойника отдаёт 404: такая строка место не принимает. Остальной
        # каталог aloe свежий — отказ именно из-за ссылки, а не «сайт не собран».
        case.live.url_dead_at = utcnow()
        for i in range(3):
            _product(db_session, "aloe", f"Fresh aloe product {i} № 10", external_id=f"fresh-{i}")
    elif flaw == "site_not_scraped":
        # Большую часть каталога aloe давно не видели: «пропала» значит «не собирали».
        for i in range(4):
            _product(
                db_session,
                "aloe",
                f"Unseen aloe product {i} № 10",
                external_id=f"unseen-{i}",
                last_seen_at=utcnow() - timedelta(days=100),
            )
    elif flaw == "second_twin":
        # Второй живой двойник — в третьем кластере: какой из двух продолжение, не решить.
        elsewhere = storage.Match(tenant_id=1, canonical_name="Kreon 25000", confidence=1.0)
        db_session.add(elsewhere)
        db_session.flush()
        _product(
            db_session,
            "aloe",
            "Kreon 25000, 20 əd",
            external_id="another-number",
            brand="Abbot",
            canonical_id=elsewhere.id,
        )
        _product(db_session, "fourth-site", "Kreon 25000 N20", canonical_id=elsewhere.id)
    elif flaw == "rival_without_pair":
        # У сайта ещё одна живая строка с этим названием, без пары и другой страны:
        # место ей не достаётся, но и двойник с парой уже не единственный.
        _product(
            db_session,
            "aloe",
            "Kreon 25000, 20 əd",
            external_id="rival",
            brand="Abbot",
            manufacturer_country_code="tr",
            country_resolution_status="resolved",
        )
        case.client.manufacturer_country_code = "de"
        case.client.country_resolution_status = "resolved"
    db_session.commit()
    before = _clusters(db_session)

    assert matcher.relink_stale_members(db_session) == []
    assert _clusters(db_session) == before


def test_merge_refuses_a_cluster_that_already_holds_a_site_twice(db_session):
    case = _split_product(db_session)
    _product(
        db_session,
        "aptekonline",
        "Kreon  25000  N20",
        external_id="duplicate-in-the-twin-cluster",
        canonical_id=case.right.id,
    )
    db_session.commit()
    before = _clusters(db_session)

    assert matcher.relink_stale_members(db_session) == []
    assert _clusters(db_session) == before


@pytest.mark.parametrize("same_product", [True, False])
def test_merge_drops_a_lost_row_only_for_its_own_live_twin(db_session, same_product):
    """В кластере пропавшей строки — ещё одна пропавшая, третьего сайта.

    Живая строка того же сайта в другом кластере сменяет её, только если это тот
    же товар; иначе на сайте оказалось бы две строки, и слияния нет.
    """
    case = _split_product(db_session)
    lost_third = _product(
        db_session,
        "aptekonline",
        "Kreon  25000  N20" if same_product else "Kreon  25000  N50",
        external_id="third-old",
        canonical_id=case.left.id,
        last_seen_at=utcnow() - timedelta(days=100),
    )
    db_session.commit()
    before = _clusters(db_session)

    merged = matcher.relink_stale_members(db_session, dry_run=True)
    assert merged == matcher.relink_stale_members(db_session)

    if not same_product:
        assert merged == [] and _clusters(db_session) == before
        return
    # Вторая пропавшая строка того же кластера уже отвязана первым слиянием:
    # повторно кластер не обрабатывается ни на деле, ни в прикидке.
    assert len(merged) == 1 and merged[0]["dropped"] == [lost_third.id]
    assert _same_cluster(case.client, case.live, case.third)
    assert (case.lost.canonical_id, lost_third.canonical_id) == (None, None)


def test_merge_checks_the_price_per_unit_like_the_passes_do(db_session):
    """Цена за упаковку против цены за штуку — не один товар, как бы ни совпали названия."""
    case = _split_product(db_session)
    run = storage.Run(started_at=utcnow(), status="ok")
    db_session.add(run)
    db_session.flush()
    for product, price in ((case.client, 193.9), (case.live, 19.39), (case.third, 19.39)):
        db_session.add(storage.PriceSnapshot(run_id=run.id, product_id=product.id, price=price))
    db_session.commit()
    before = _clusters(db_session)

    assert matcher.relink_stale_members(db_session) == []
    assert _clusters(db_session) == before


def test_merge_keeps_the_record_of_a_cluster_someone_else_still_belongs_to(db_session):
    """Товар другого арендатора шаг не видит: запись кластера при нём не удаляется."""
    case = _split_product(db_session)
    foreign = _product(db_session, "fourth-site", "Kreon 25000 N20", canonical_id=case.left.id)
    foreign.tenant_id = 2
    db_session.commit()

    matcher.relink_stale_members(db_session)
    db_session.commit()

    assert _same_cluster(case.client, case.live, case.third)
    assert db_session.get(storage.Match, case.left.id) is not None
    assert foreign.canonical_id == case.left.id


# ── Пропавшая строка без пары новую пару не начинает ─────────────────────────


def _lost_everywhere(session, *, scraped: bool, days: int = 100):
    """Старые строки одного товара на двух сайтах, обе без пары."""
    long_ago = utcnow() - timedelta(days=days)
    rows = [
        _product(session, "pharmonline", "Ferrovef № 60 (Kapsulalar)", last_seen_at=long_ago),
        _product(session, "aptekonline", "Ferrovef   N60", last_seen_at=long_ago),
    ]
    if scraped:
        # На обоих сайтах есть свежий каталог: давно не виденная строка — пропавшая.
        for site in ("pharmonline", "aptekonline"):
            for i in range(2):
                _product(session, site, f"Fresh {site} product {i} № 10", external_id=f"{site}-{i}")
    session.commit()
    return rows


def test_rows_gone_from_their_sites_do_not_start_a_cluster(db_session):
    rows = _lost_everywhere(db_session, scraped=True)

    matcher.match_products(db_session)

    assert [row.canonical_id for row in rows] == [None, None]


def test_a_row_missed_by_one_weekly_scrape_is_not_gone_yet(db_session):
    """Пропавшей строка становится после двух пропущенных недельных сборов, не одного."""
    rows = _lost_everywhere(db_session, scraped=True, days=10)

    matcher.match_products(db_session)

    assert _same_cluster(*rows)


def test_rows_gone_from_their_sites_do_not_pair_by_barcode_either(db_session):
    rows = _lost_everywhere(db_session, scraped=True)
    for row in rows:
        row.barcode = "4770001234567"
    db_session.commit()

    matcher.match_products(db_session)

    assert [row.canonical_id for row in rows] == [None, None]


def test_rows_of_a_site_that_was_not_scraped_are_not_called_gone(db_session):
    """Сбор не проходил — давно не виденные строки остаются обычными кандидатами."""
    rows = _lost_everywhere(db_session, scraped=False)

    matcher.match_products(db_session)

    assert _same_cluster(*rows)


def test_rows_freed_by_a_merge_do_not_pair_up_in_the_same_stage(db_session):
    """Слияние освобождает две старые строки одного товара на двух сайтах.

    Без запрета они тут же сходились между собой: второй кластер того же товара
    из одних давно не виденных строк — вторая строка сравнения с устаревшими
    ценами (разбор 2026-10-12, «Depantol» на копии каталога).
    """
    case = _split_product(db_session)
    lost_client = _product(
        db_session,
        "pharmonline",
        "Kreon 25000 №20 (Kapsula)",
        external_id="client-old-slug",
        canonical_id=case.right.id,
        last_seen_at=utcnow() - timedelta(days=100),
    )
    db_session.commit()

    matcher.refresh_derived_fields(db_session)
    matcher.relink_stale_members(db_session)
    matcher.match_products(db_session)
    matcher.revalidate_split(db_session)
    db_session.commit()

    assert (case.lost.canonical_id, lost_client.canonical_id) == (None, None)
    assert len(_clusters(db_session)) == 1
    assert _same_cluster(case.client, case.live, case.third)


def test_a_row_dropped_by_a_merge_is_not_handed_a_twin_afterwards(db_session):
    """Вторая пропавшая строка кластера отвязана слиянием — и на этом всё.

    У неё есть ещё и живой двойник без пары. Обработай шаг её повторно, двойник
    получил бы место в кластере, которого уже нет.
    """
    case = _split_product(db_session)
    lost_third = _product(
        db_session,
        "aptekonline",
        "Kreon  25000  N20",
        external_id="third-old",
        canonical_id=case.left.id,
        last_seen_at=utcnow() - timedelta(days=100),
    )
    spare = _product(db_session, "aptekonline", "Kreon  25000  N20", external_id="third-spare")
    db_session.commit()

    merged = matcher.relink_stale_members(db_session)
    db_session.commit()

    assert len(merged) == 1 and merged[0]["dropped"] == [lost_third.id]
    assert spare.canonical_id is None
    assert list(_clusters(db_session)) == [case.right.id]


def test_merges_chain_through_a_row_moved_earlier_in_the_same_call(db_session):
    """Двойник второй пропавшей строки уже перенесён первым слиянием.

    Шаг обязан искать его в новом кластере, а не там, где он лежал до вызова:
    иначе пропавшая строка отвязывается, а её соседи никуда не попадают.
    """
    case = _split_product(db_session)
    fourth = _product(db_session, "fourth-site", "Kreon 25000 N20", canonical_id=case.left.id)
    far = storage.Match(tenant_id=1, canonical_name="Kreon 25000", confidence=1.0)
    db_session.add(far)
    db_session.flush()
    lost_third = _product(
        db_session,
        "aptekonline",
        "Kreon  25000  N20",
        external_id="third-old",
        canonical_id=far.id,
        last_seen_at=utcnow() - timedelta(days=100),
    )
    fifth = _product(db_session, "fifth-site", "Kreon 25000 N20", canonical_id=far.id)
    db_session.commit()

    dry = matcher.relink_stale_members(db_session, dry_run=True)
    merged = matcher.relink_stale_members(db_session)
    db_session.commit()

    assert merged == dry and [item["merged"] for item in merged] == [case.right.id, far.id]
    assert _same_cluster(case.client, fourth, case.live, case.third, fifth)
    assert (case.lost.canonical_id, lost_third.canonical_id) == (None, None)
    assert list(_clusters(db_session)) == [case.left.id]


def test_merge_refuses_when_both_rows_of_a_site_are_lost(db_session, monkeypatch):
    """Обе строки сайта давно не видели — выбрать, какая останется, нечем."""
    case = _split_product(db_session)
    for cluster, external_id in ((case.left, "fourth-a"), (case.right, "fourth-b")):
        _product(
            db_session,
            "fourth-site",
            "Kreon 25000 N20",
            external_id=external_id,
            canonical_id=cluster.id,
            last_seen_at=utcnow() - timedelta(days=100),
        )
    # Свежий каталог четвёртого сайта: его строки именно пропали, а не «не собирались».
    for i in range(3):
        _product(db_session, "fourth-site", f"Fresh product {i} № 10", external_id=f"fresh-{i}")
    db_session.commit()
    before = _clusters(db_session)
    journal = _MatcherLog()
    monkeypatch.setattr(matcher, "log", journal)

    assert matcher.relink_stale_members(db_session) == []
    assert _clusters(db_session) == before
    assert ("two_lost_rows_of_one_site", case.lost.id) in journal.refusals()


def test_a_lost_row_that_has_a_pair_still_brings_a_newcomer_into_its_cluster(db_session):
    """Запрет касается строк без пары: существующий кластер по-прежнему растёт."""
    match = storage.Match(tenant_id=1, canonical_name="Ferrovef", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    long_ago = utcnow() - timedelta(days=100)
    old = [
        _product(
            db_session,
            site,
            name,
            external_id=f"{site}-old",
            canonical_id=match.id,
            last_seen_at=long_ago,
        )
        for site, name in (
            ("pharmonline", "Ferrovef № 60 (Kapsulalar)"),
            ("aptekonline", "Ferrovef   N60"),
        )
    ]
    for site in ("pharmonline", "aptekonline"):
        for i in range(2):
            _product(db_session, site, f"Fresh {site} product {i} № 10", external_id=f"{site}-{i}")
    newcomer = _product(db_session, "aloe", "Ferrovef 60 əd", brand="Ferrovef")
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(*old, newcomer)


def test_second_merge_sees_the_rows_the_first_one_brought_in(db_session):
    """Два кластера с живым товаром клиента в каждом и один общий двойник.

    Первое слияние приводит товар клиента в кластер двойника; второе обязано
    это видеть и отказать — иначе в кластере два товара одного сайта.
    """
    case = _split_product(db_session)
    second = storage.Match(tenant_id=1, canonical_name="Kreon 25000", confidence=1.0)
    db_session.add(second)
    db_session.flush()
    _product(
        db_session,
        "aloe",
        "Kreon 25000, 20 əd",
        external_id="older-slug",
        brand="Abbot",
        canonical_id=second.id,
        last_seen_at=utcnow() - timedelta(days=100),
    )
    other_listing = _product(
        db_session,
        "pharmonline",
        "Kreon 25000 №20 (Kapsula)",
        external_id="second-listing",
        canonical_id=second.id,
    )
    # У aloe две пропавшие строки: свежего каталога должно быть больше половины.
    for i in range(2):
        _product(db_session, "aloe", f"Fresh aloe product {i} № 10", external_id=f"fresh-{i}")
    db_session.commit()

    merged = matcher.relink_stale_members(db_session)
    db_session.commit()

    assert [item["merged"] for item in merged] == [case.left.id]
    assert other_listing.canonical_id == second.id
    sites = [product.site for product in db_session.get(storage.Match, case.right.id).products]
    assert sorted(sites) == ["aloe", "aptekonline", "pharmonline"]


@pytest.mark.parametrize(
    ("obstacle", "reason"),
    [
        ("two_live_rows", "two_rows_of_one_site"),
        ("other_country", "twin_is_another_product"),
        ("rejection", "members_must_not_meet"),
        ("manual_twin_cluster", "manual_cluster"),
        ("rival_without_pair", "live_row_without_pair_too"),
    ],
)
def test_a_refused_merge_names_its_reason(db_session, monkeypatch, obstacle, reason):
    """Причина отказа — словарь RUNBOOK: по ней оператор понимает, что с товаром."""
    from src import match_actions

    lost_kw = live_kw = None
    if obstacle == "other_country":
        lost_kw = {"manufacturer_country_code": "de", "country_resolution_status": "resolved"}
        live_kw = {"manufacturer_country_code": "tr", "country_resolution_status": "resolved"}
    case = _split_product(db_session, lost_kw=lost_kw, live_kw=live_kw)
    if obstacle == "two_live_rows":
        _product(
            db_session,
            "aptekonline",
            "Kreon  25000  N20",
            external_id="another-listing",
            canonical_id=case.left.id,
        )
    elif obstacle == "rejection":
        match_actions.add_rejection(db_session, case.client.id, case.third.id, reason="operator")
    elif obstacle == "manual_twin_cluster":
        case.right.is_manual = True
    elif obstacle == "rival_without_pair":
        _product(db_session, "aloe", "Kreon 25000, 20 əd", external_id="rival", brand="Abbot")
        match_actions.add_rejection(db_session, case.client.id, case.live.id, reason="operator")
        rival = db_session.query(storage.Product).filter_by(external_id="rival").one()
        match_actions.add_rejection(db_session, case.client.id, rival.id, reason="operator")
    db_session.commit()

    journal = _MatcherLog()
    monkeypatch.setattr(matcher, "log", journal)

    assert matcher.relink_stale_members(db_session, dry_run=True) == []
    assert journal.refusals() == [(reason, case.lost.id)]


# ── Цена как признак одного товара ───────────────────────────────────────────
#
# Жалоба клиента 2026-10-12: в четырёх алертах «конкурент дешевле» сравнивались
# разные товары — таблетки со свечами («Polioksidonium 12 mq № 10» у клиента
# дважды, 19,20 и 27,60), крем с мазью («Ametazon 30 q»), обычный «Septolete
# total» с лимонным. У сайта было два товара, одинаково подходящих по названию;
# проход взял первого и на цену не смотрел, а рядом лежал товар с ценой клиента
# до копейки. Замер на копии каталога: цена совпадает у 74% пар.


def _priced(
    session,
    product: storage.Product,
    price: float,
    *,
    discount: float | None = None,
    days_ago: int = 0,
) -> storage.Product:
    """Запись о цене; `days_ago` — прежняя цена из журнала."""
    run = session.query(storage.Run).first()
    if run is None:
        run = storage.Run(started_at=utcnow(), status="ok")
        session.add(run)
        session.flush()
    session.add(
        storage.PriceSnapshot(
            run_id=run.id,
            product_id=product.id,
            price=price,
            discount_price=discount,
            captured_at=utcnow() - timedelta(days=days_ago),
        )
    )
    session.flush()
    return product


def _cluster(session, name: str, **kw) -> storage.Match:
    match = storage.Match(tenant_id=1, canonical_name=name, confidence=1.0, **kw)
    session.add(match)
    session.flush()
    return match


def _tablets_paired_with_suppositories(session, **twin_kw):
    """Свечи клиента (27,60) в паре с таблетками конкурента (19,20); таблетки клиента — без пары."""
    match = _cluster(session, "Polioksidonium")
    suppositories = _priced(
        session,
        _product(
            session,
            "pharmonline",
            "Polioksidonium 12 mq № 10",
            external_id="supp",
            canonical_id=match.id,
        ),
        27.6,
    )
    competitor = _priced(
        session,
        _product(session, "aptekonline", "Polioksidonium   12 mq  N10", canonical_id=match.id),
        19.2,
    )
    tablets = _priced(
        session,
        _product(
            session, "pharmonline", "Polioksidonium 12 mq  № 10", external_id="tabs", **twin_kw
        ),
        19.2,
    )
    session.commit()
    return SimpleNamespace(
        match=match, suppositories=suppositories, competitor=competitor, tablets=tablets
    )


def test_a_price_stranger_gives_its_place_to_the_sibling_with_the_cluster_price(db_session):
    case = _tablets_paired_with_suppositories(db_session)
    # Состав кластера уже прочитан в сессию, как бывает в пайплайне.
    assert set(case.match.products) == {case.suppositories, case.competitor}

    planned = matcher.replace_price_strangers(db_session, dry_run=True)
    assert not db_session.dirty and not db_session.deleted
    assert case.tablets.canonical_id is None

    replaced = matcher.replace_price_strangers(db_session)

    assert (
        replaced
        == planned
        == [
            {
                "match_id": case.match.id,
                "site": "pharmonline",
                "old": case.suppositories.id,
                "new": case.tablets.id,
                "price": 19.2,
                "old_price": 27.6,
                "from_match": None,
                "swapped": False,
                "joined": [],
                "left_alone": [],
            }
        ]
    )
    assert _same_cluster(case.tablets, case.competitor)
    assert case.suppositories.canonical_id is None
    assert set(case.match.products) == {case.tablets, case.competitor}
    assert matcher.replace_price_strangers(db_session) == []


def test_the_member_that_disagrees_with_the_majority_is_the_one_replaced(db_session):
    """Крем клиента (7,78) стоял с мазью двух конкурентов (7,00 и 7,00)."""
    match = _cluster(db_session, "Ametazon")
    cream = _priced(
        db_session,
        _product(db_session, "pharmonline", "Ametazon 30 qr (Krem)", canonical_id=match.id),
        7.78,
    )
    others = [
        _priced(db_session, _product(db_session, site, name, canonical_id=match.id), 7.0)
        for site, name in (
            ("aptekonline", "Ametazon məlhəm   0.1%  30 q"),
            ("aloe", "Ametazon 30 q"),
        )
    ]
    ointment = _priced(
        db_session,
        _product(db_session, "pharmonline", "Ametazon 30 qr (Məlhəm)", external_id="ointment"),
        7.0,
    )
    db_session.commit()

    replaced = matcher.replace_price_strangers(db_session)

    assert [(item["old"], item["new"]) for item in replaced] == [(cream.id, ointment.id)]
    assert _same_cluster(ointment, *others) and cream.canonical_id is None


def _crossed_pairs(session, *, client_b="Septolete total № 16", competitor_b="Septolete Total N16"):
    """Два кластера крест-накрест: цены сходятся, если поменять товары конкурента местами."""
    first, second = _cluster(session, "A"), _cluster(session, "B")
    client_a = _priced(
        session,
        _product(session, "pharmonline", "Septolete total № 16", canonical_id=first.id),
        6.61,
    )
    wrong_a = _priced(
        session,
        _product(
            session,
            "aptekonline",
            competitor_b + " (limon)",
            external_id="comp-lemon",
            canonical_id=first.id,
        ),
        5.27,
    )
    client_b_row = _priced(
        session,
        _product(
            session,
            "pharmonline",
            client_b + " (limon)",
            external_id="client-lemon",
            canonical_id=second.id,
        ),
        5.27,
    )
    wrong_b = _priced(
        session,
        _product(
            session, "aptekonline", competitor_b, external_id="comp-plain", canonical_id=second.id
        ),
        6.61,
    )
    session.commit()
    return SimpleNamespace(
        first=first,
        second=second,
        client_plain=client_a,
        competitor_lemon=wrong_a,
        client_lemon=client_b_row,
        competitor_plain=wrong_b,
    )


def test_two_crossed_pairs_swap_their_members(db_session):
    case = _crossed_pairs(db_session)

    replaced = matcher.replace_price_strangers(db_session)

    assert len(replaced) == 1 and replaced[0]["swapped"] is True
    assert _same_cluster(case.client_plain, case.competitor_plain)
    assert _same_cluster(case.client_lemon, case.competitor_lemon)
    assert matcher.replace_price_strangers(db_session) == []


def test_a_sibling_misplaced_in_its_own_cluster_moves_when_a_swap_does_not_fit(db_session):
    """Лимонные названы на сайтах разными словами — обмен не подходит.

    Обычный товар конкурента всё равно уходит к обычному товару клиента: в
    своём кластере он тоже чужой по цене. Лимонные остаются без пары, а кластер
    из одного лимонного распускается — один товар не пара.
    """
    case = _crossed_pairs(db_session)
    case.client_lemon.name = "Septolete total limon və ağcaqayın № 16"
    case.competitor_lemon.name = "Septolete Total limon, kəndalaş N16"
    for row in (case.client_lemon, case.competitor_lemon):
        row.name_normalized = normalize_name(row.name)
    db_session.commit()
    second_id = case.second.id

    planned = matcher.replace_price_strangers(db_session, dry_run=True)
    assert not db_session.dirty and not db_session.deleted
    assert case.client_lemon.canonical_id == second_id
    replaced = matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert replaced == planned
    assert [(item["swapped"], item["from_match"], item["left_alone"]) for item in replaced] == [
        (False, second_id, [case.client_lemon.id])
    ]
    assert _same_cluster(case.client_plain, case.competitor_plain)
    assert case.competitor_lemon.canonical_id is None
    assert case.client_lemon.canonical_id is None
    assert list(_clusters(db_session)) == [case.first.id]
    assert db_session.get(storage.Match, second_id) is None


def test_a_cluster_that_keeps_a_pair_after_its_member_left_is_not_dissolved(db_session):
    """Обычный товар конкурента стоял третьим в кластере двух лимонных."""
    case = _crossed_pairs(db_session)
    case.client_lemon.name = "Septolete total limon və ağcaqayın № 16"
    case.competitor_lemon.name = "Septolete Total limon, kəndalaş N16"
    for row in (case.client_lemon, case.competitor_lemon):
        row.name_normalized = normalize_name(row.name)
    third = _priced(
        db_session,
        _product(
            db_session,
            "aloe",
            "Septolete total limon və ağcaqayın 16 əd",
            canonical_id=case.second.id,
        ),
        5.27,
    )
    db_session.commit()
    assert len(case.second.products) == 3  # состав прочитан в сессию до шага

    replaced = matcher.replace_price_strangers(db_session)

    assert [(item["swapped"], item["left_alone"]) for item in replaced] == [(False, [])]
    assert set(case.second.products) == {case.client_lemon, third}
    assert _same_cluster(case.client_plain, case.competitor_plain)
    assert _same_cluster(case.client_lemon, third)


def test_a_sibling_paired_with_a_gone_row_moves_and_the_gone_row_loses_its_cluster(db_session):
    """Сайт сменил адрес товара: старая строка в паре с товаром конкурента,
    новая — с другим товаром того же конкурента («Qlipsidan M» и «Qlipsidan 50»).
    """
    stale_pair, live_pair = _cluster(db_session, "old"), _cluster(db_session, "new")
    gone = _priced(
        db_session,
        _product(
            db_session,
            "pharmonline",
            "Qlipsidan M № 30 (Tabletlər) (Hindistan)",
            external_id="gone",
            canonical_id=stale_pair.id,
            last_seen_at=utcnow() - timedelta(days=100),
        ),
        14.76,
    )
    competitor = _priced(
        db_session,
        _product(db_session, "aptekonline", "Qlipsidan M   N30", canonical_id=stale_pair.id),
        14.76,
    )
    client = _priced(
        db_session,
        _product(
            db_session,
            "pharmonline",
            "Qlipsidan M № 30 (Tabletlər)",
            external_id="live",
            canonical_id=live_pair.id,
        ),
        14.76,
    )
    wrong = _priced(
        db_session,
        _product(
            db_session,
            "aptekonline",
            "Qlipsidan   N30",
            external_id="wrong",
            canonical_id=live_pair.id,
        ),
        12.0,
    )
    for i in range(2):  # свежий каталог сайта: строка именно пропала
        _product(db_session, "pharmonline", f"Fresh product {i} № 10", external_id=f"f{i}")
    db_session.commit()
    stale_pair_id = stale_pair.id

    replaced = matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert [(item["old"], item["new"], item["left_alone"]) for item in replaced] == [
        (wrong.id, competitor.id, [gone.id])
    ]
    assert _same_cluster(client, competitor)
    assert gone.canonical_id is None and wrong.canonical_id is None
    assert db_session.get(storage.Match, stale_pair_id) is None


@pytest.mark.parametrize(
    "obstacle",
    [
        "manual",
        "prices_almost_equal",
        "two_twins",
        "twin_is_further_by_name",
        "sibling_price_is_close_but_not_the_same",
        "operator_rejection",
        "twin_gone_from_the_site",
        "twin_without_a_price",
        "twin_out_of_stock",
        "twin_page_is_dead",
        "twin_price_is_zero",
        "twin_has_the_other_dose_order",
        "prices_agreed_before",
        "prices_agreed_before_at_a_discount",
        "prices_agreed_before_within_half_a_percent",
        "prices_agreed_before_with_a_member_now_out_of_stock",
        "client_site_not_scraped",
    ],
)
def test_a_price_stranger_stays_when_the_evidence_is_not_enough(db_session, monkeypatch, obstacle):
    from src import match_actions

    twin_kw = {}
    if obstacle == "twin_gone_from_the_site":
        twin_kw = {"last_seen_at": utcnow() - timedelta(days=100)}
    case = _tablets_paired_with_suppositories(db_session, **twin_kw)
    if obstacle == "manual":
        case.match.is_manual = True
    elif obstacle == "prices_almost_equal":
        # 19,20 против 19,30: два листинга одного товара, переставлять незачем.
        db_session.query(storage.PriceSnapshot).filter_by(product_id=case.suppositories.id).update(
            {"price": 19.3}
        )
    elif obstacle == "two_twins":
        _priced(
            db_session,
            _product(db_session, "pharmonline", "Polioksidonium 12 mq № 10", external_id="third"),
            19.2,
        )
    elif obstacle == "twin_is_further_by_name":
        # Лишнее слово без конфликта: дальше по названию, чем заменяемый.
        case.tablets.name = "Polioksidonium Petrovaks 12 mq № 10"
        case.tablets.name_normalized = normalize_name(case.tablets.name)
        assert not matcher._hard_conflict(case.tablets, case.competitor)
        assert matcher._name_gap(case.competitor, case.tablets) > matcher._name_gap(
            case.competitor, case.suppositories
        )
    elif obstacle == "sibling_price_is_close_but_not_the_same":
        # 19,60 против 19,20 — два процента: похоже, но не «до копейки».
        db_session.query(storage.PriceSnapshot).filter_by(product_id=case.tablets.id).update(
            {"price": 19.6}
        )
    elif obstacle == "operator_rejection":
        match_actions.add_rejection(db_session, case.tablets.id, case.competitor.id, reason="op")
    elif obstacle == "twin_gone_from_the_site":
        for i in range(2):  # свежий каталог сайта: строка именно пропала
            _product(db_session, "pharmonline", f"Fresh product {i} № 10", external_id=f"f{i}")
    elif obstacle == "twin_without_a_price":
        db_session.query(storage.PriceSnapshot).filter_by(product_id=case.tablets.id).delete()
    elif obstacle == "twin_out_of_stock":
        case.tablets.offer_availability_status = "out_of_stock"
    elif obstacle == "twin_page_is_dead":
        case.tablets.url_dead_at = utcnow()
    elif obstacle == "twin_price_is_zero":
        db_session.query(storage.PriceSnapshot).filter_by(product_id=case.tablets.id).update(
            {"price": 0.0}
        )
        db_session.query(storage.PriceSnapshot).filter_by(product_id=case.competitor.id).update(
            {"price": 0.0}
        )
    elif obstacle == "twin_has_the_other_dose_order":
        # Правило есть только у проходов: на сайте оба порядка — значит, товары разные.
        for row, name in (
            (case.suppositories, "Prestans 5 mq/10 mq № 30"),
            (case.competitor, "Prestans   5 mq/ 10 mq  N30"),
            (case.tablets, "Prestans 10 mq/5 mq № 30"),
        ):
            row.name, row.name_normalized = name, normalize_name(name)
            row.dosage, row.pack_size = extract_dosage(name), extract_pack_size(name)
            row.brand = "Prestans"
    elif obstacle == "prices_agreed_before":
        # Свечи стоили как таблетки конкурента: пара была верной, цена изменилась.
        _priced(db_session, case.suppositories, 19.2, days_ago=30)
    elif obstacle == "prices_agreed_before_at_a_discount":
        _priced(db_session, case.suppositories, 30.0, discount=19.2, days_ago=30)
    elif obstacle == "prices_agreed_before_within_half_a_percent":
        _priced(db_session, case.suppositories, 19.25, days_ago=30)
    elif obstacle == "prices_agreed_before_with_a_member_now_out_of_stock":
        # Третий член кластера сейчас не в наличии, но свечи стоили, как он:
        # это такое же свидетельство, что свечи в кластере на месте.
        gone_from_stock = _product(
            db_session,
            "aloe",
            "Polioksidonium 12 mq 10 əd",
            canonical_id=case.match.id,
            status="out_of_stock",
        )
        _priced(db_session, gone_from_stock, 27.6)
    elif obstacle == "client_site_not_scraped":
        # Сайт клиента не собирался: его строки не «пропали», о них просто
        # ничего не известно. Пары с такими строками не трогаем.
        for row in (case.suppositories, case.tablets):
            row.last_seen_at = utcnow() - timedelta(days=100)
    db_session.commit()
    before = _clusters(db_session)
    journal = _MatcherLog()
    monkeypatch.setattr(matcher, "log", journal)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before
    # Причина пишется, только когда сосед с ценой остальных нашёлся.
    reason = (
        "several_siblings"
        if obstacle == "two_twins"
        else "prices_agreed_before"
        if obstacle.startswith("prices_agreed")
        else None
    )
    assert journal.kept_strangers() == ([(reason, case.suppositories.id)] if reason else [])


def test_a_cluster_whose_other_members_disagree_on_price_is_left_alone(db_session):
    """У остальных цена не одна — под какую искать замену, неясно."""
    match = _cluster(db_session, "Ametazon")
    rows = [
        _priced(db_session, _product(db_session, site, name, canonical_id=match.id), price)
        for site, name, price in (
            ("pharmonline", "Ametazon 30 qr (Krem)", 7.78),
            ("aptekonline", "Ametazon   30 q", 7.0),
            ("aloe", "Ametazon 30 q", 6.5),
        )
    ]
    _priced(db_session, _product(db_session, "pharmonline", "Ametazon 30 qr", external_id="x"), 7.0)
    db_session.commit()
    before = _clusters(db_session)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before and _same_cluster(*rows)


def test_a_sibling_that_agrees_with_its_own_cluster_is_not_taken_from_it(db_session, monkeypatch):
    """Обмен не подходит, а двойник со своим кластером в цене согласен."""
    case = _crossed_pairs(db_session)
    case.client_lemon.name = "Septolete total limon və ağcaqayın № 16"
    case.competitor_lemon.name = "Septolete Total limon, kəndalaş N16"
    for row in (case.client_lemon, case.competitor_lemon):
        row.name_normalized = normalize_name(row.name)
    db_session.query(storage.PriceSnapshot).filter_by(product_id=case.client_lemon.id).update(
        {"price": 6.61}
    )
    db_session.commit()
    before = _clusters(db_session)
    journal = _MatcherLog()
    monkeypatch.setattr(matcher, "log", journal)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before
    assert journal.kept_strangers() == [
        ("sibling_agrees_with_its_cluster", case.competitor_lemon.id)
    ]


def test_passes_give_the_candidate_to_the_anchor_with_the_same_price(db_session):
    """Два товара клиента с одним названием; у конкурента один, с ценой второго.

    Якорем первым идёт товар с меньшим номером — свечи. Без оглядки на цену он
    и забирал таблетки конкурента.
    """
    suppositories = _priced(
        db_session,
        _product(db_session, "pharmonline", "Polioksidonium 12 mq № 10", external_id="supp"),
        27.6,
    )
    tablets = _priced(
        db_session,
        _product(db_session, "pharmonline", "Polioksidonium 12 mq  № 10", external_id="tabs"),
        19.2,
    )
    competitor = _priced(
        db_session, _product(db_session, "aptekonline", "Polioksidonium   12 mq  N10"), 19.2
    )
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(tablets, competitor)
    assert suppositories.canonical_id is None


def test_passes_prefer_the_candidate_with_the_anchor_price_among_equally_named(db_session):
    """У конкурента два одноимённых товара; пару получает тот, чья цена — цена якоря."""
    client = _priced(
        db_session, _product(db_session, "pharmonline", "Polioksidonium 12 mq № 10"), 19.2
    )
    other = _priced(
        db_session,
        _product(db_session, "aptekonline", "Polioksidonium   12 mq  N10", external_id="first"),
        27.6,
    )
    twin = _priced(
        db_session,
        _product(db_session, "aptekonline", "Polioksidonium  12 mq  N10", external_id="second"),
        19.2,
    )
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(client, twin)
    assert other.canonical_id is None


def test_the_stage_reseats_a_price_stranger_and_pairs_the_one_it_displaced(db_session):
    """Этап целиком: замена — его подготовительный шаг, проходы идут следом."""
    from src import main as main_mod

    case = _tablets_paired_with_suppositories(db_session)
    competitor_suppositories = _priced(
        db_session,
        _product(db_session, "aptekonline", "Polioksidonium  12 mq  N10", external_id="comp-supp"),
        27.6,
    )
    db_session.commit()

    summary = main_mod._run_matching_stage(db_session)
    db_session.commit()

    assert summary["reseated"] == 1
    assert _same_cluster(case.tablets, case.competitor)
    assert _same_cluster(case.suppositories, competitor_suppositories)
    assert len(_clusters(db_session)) == 2


def test_the_passes_alone_do_not_reseat(db_session):
    """`match_products` существующие пары не меняет: замена — отдельный шаг этапа."""
    case = _tablets_paired_with_suppositories(db_session)
    before = _clusters(db_session)

    matcher.match_products(db_session)
    db_session.commit()

    assert _clusters(db_session) == before and case.tablets.canonical_id is None


def test_a_member_swapped_earlier_in_the_call_is_seen_where_it_is_now(db_session):
    """Третий кластер ищет двойника по цене и находит товар, только что обменянный.

    Тот уже стоит в своей паре и в цене с ней согласен — забирать его нельзя.
    """
    case = _crossed_pairs(db_session)
    third = _cluster(db_session, "C")
    another_lemon = _priced(
        db_session,
        _product(
            db_session,
            "pharmonline",
            "Septolete total № 16 (limon)",
            external_id="client-lemon-2",
            canonical_id=third.id,
        ),
        5.27,
    )
    stranger = _priced(
        db_session,
        _product(
            db_session,
            "aptekonline",
            "Septolete Total N16",
            external_id="comp-third",
            canonical_id=third.id,
        ),
        9.99,
    )
    db_session.commit()

    replaced = matcher.replace_price_strangers(db_session, dry_run=True)
    assert replaced == matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert [item["swapped"] for item in replaced] == [True]
    assert _same_cluster(case.client_lemon, case.competitor_lemon)
    assert _same_cluster(another_lemon, stranger)


@pytest.mark.parametrize(
    "obstacle", ["the_other_cluster_is_manual", "a_cluster_with_two_rows_of_one_site"]
)
def test_crossed_pairs_stay_when_one_of_the_clusters_is_not_to_be_touched(
    db_session, monkeypatch, obstacle
):
    case = _crossed_pairs(db_session)
    if obstacle == "the_other_cluster_is_manual":
        case.second.is_manual = True
    else:
        # Вторая строка того же сайта, без цены: кластер уже неисправен, и ни
        # менять в нём товар, ни забирать из него нельзя.
        _product(
            db_session,
            "aptekonline",
            "Septolete Total N16",
            external_id="second-row",
            canonical_id=case.first.id,
        )
    db_session.commit()
    before = _clusters(db_session)
    journal = _MatcherLog()
    monkeypatch.setattr(matcher, "log", journal)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before
    assert journal.kept_strangers()
    assert {reason for reason, _ in journal.kept_strangers()} == {
        "sibling_cluster_not_to_be_touched"
    }


def test_a_sibling_does_not_join_a_cluster_that_mixes_pack_and_piece_prices(db_session):
    """У остальных цена одна, а фасовка названа только у одного: цена за единицу
    расходится в десять раз, и проходы такую пару не собрали бы."""
    match = _cluster(db_session, "Polioksidonium")
    rows = [
        _priced(db_session, _product(db_session, site, name, canonical_id=match.id), price)
        for site, name, price in (
            ("pharmonline", "Polioksidonium 12 mq", 27.6),
            ("aloe", "Polioksidonium 12 mq", 19.2),
            ("aptekonline", "Polioksidonium   12 mq  N10", 19.2),
        )
    ]
    twin = _priced(
        db_session,
        _product(db_session, "pharmonline", "Polioksidonium  12 mq", external_id="twin"),
        19.2,
    )
    db_session.commit()
    prices = storage.latest_snapshots_per_product(db_session, [twin.id, *(r.id for r in rows)])
    assert matcher._has_perunit_mismatch([twin, rows[2]], prices)
    assert not matcher._has_perunit_mismatch([twin, rows[1]], prices)
    assert not any(matcher._hard_conflict(twin, row) for row in rows[1:])
    before = _clusters(db_session)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before


def test_a_member_close_in_price_to_one_of_the_others_is_not_a_stranger(db_session):
    """Чужой расходится в цене с каждым из остальных, а не с кем-то одним."""
    match = _cluster(db_session, "Ametazon")
    rows = [
        _priced(db_session, _product(db_session, site, name, canonical_id=match.id), price)
        for site, name, price in (
            ("pharmonline", "Ametazon 30 qr (Krem)", 101.8),
            ("aptekonline", "Ametazon   30 q", 100.0),
            ("aloe", "Ametazon 30 q", 100.9),
        )
    ]
    sibling = _priced(
        db_session, _product(db_session, "pharmonline", "Ametazon 30 qr", external_id="x"), 100.45
    )
    db_session.commit()
    prices = storage.latest_snapshots_per_product(db_session, [sibling.id, *(r.id for r in rows)])
    price = {row.id: matcher._effective_price(prices, row) for row in (sibling, *rows)}
    assert matcher._price_apart(price[rows[0].id], price[rows[1].id])
    assert not matcher._price_apart(price[rows[0].id], price[rows[2].id])
    assert all(matcher._same_price(price[sibling.id], price[row.id]) for row in rows[1:])
    before = _clusters(db_session)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before


def test_a_sibling_seated_in_one_cluster_brings_the_next_cluster_to_it(db_session):
    """Два кластера с чужим по цене и один товар, подходящий обоим.

    Первый кластер получает товар; второй не забирает его, а переходит к нему:
    товар уже на месте, и его соседи подходят.
    """
    case = _tablets_paired_with_suppositories(db_session)
    second = _cluster(db_session, "second")
    stranger = _priced(
        db_session,
        _product(
            db_session,
            "pharmonline",
            "Polioksidonium 12 mq   № 10",
            external_id="stranger-2",
            canonical_id=second.id,
        ),
        30.0,
    )
    aloe = _priced(
        db_session,
        _product(db_session, "aloe", "Polioksidonium 12 mq 10 əd", canonical_id=second.id),
        19.2,
    )
    db_session.commit()

    replaced = matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert [(item["match_id"], item["new"], item["joined"]) for item in replaced] == [
        (case.match.id, case.tablets.id, []),
        (case.match.id, case.tablets.id, [aloe.id]),
    ]
    assert _same_cluster(case.tablets, case.competitor, aloe)
    assert stranger.canonical_id is None and case.suppositories.canonical_id is None
    assert len(_clusters(db_session)) == 1


def test_ringer_lactate_is_not_ringer(db_session):
    """Жалоба клиента 2026-10-12: «Ringer laktat (Hartman)» стоял в паре с «Ringer».

    Существующую пару распускает `revalidate_split`; лактат с лактатом сходятся.
    """
    wrong = _cluster(db_session, "Ringer")
    lactate = _priced(
        db_session,
        _product(
            db_session, "pharmonline", "Ringer laktat (Hartman) 500 ml", canonical_id=wrong.id
        ),
        2.41,
    )
    plain = _priced(
        db_session, _product(db_session, "aloe", "Ringer 500 ml", canonical_id=wrong.id), 1.84
    )
    other_lactate = _priced(
        db_session, _product(db_session, "aptekonline", "Ringer Laktat   500 ml"), 2.36
    )
    client_lactate = _priced(
        db_session,
        _product(db_session, "pharmonline", "Ringer laktat 500 ml (Məhlul)", external_id="rl"),
        2.36,
    )
    db_session.commit()

    matcher.match_products(db_session)
    actions = matcher.revalidate_split(db_session)
    db_session.commit()

    assert [action["action"] for action in actions] == ["dissolve"]
    assert lactate.canonical_id is None and plain.canonical_id is None
    assert matcher.is_rejected(db_session, lactate.id, plain.id)
    assert _same_cluster(client_lactate, other_lactate)


def _plain_client_row_with_the_garamycin_row(session):
    """«Selestoderm V» клиента (9,71) в паре с «…qaramisinlə» aloe (8,15); обычный
    aloe с той же ценой 9,71 стоит в своей паре с aptekonline."""
    wrong, right = _cluster(session, "wrong"), _cluster(session, "right")
    garamycin = _priced(
        session,
        _product(session, "aloe", "Selestoderm-V qaramisinlə məlhəm 30 qr.", canonical_id=wrong.id),
        8.15,
    )
    client = _priced(
        session,
        _product(session, "pharmonline", "Selestoderm V 30 qr (Məlhəm)", canonical_id=wrong.id),
        9.71,
    )
    plain = _priced(
        session,
        _product(
            session,
            "aloe",
            "Selestoderm-V məlhəm 30 qr.",
            external_id="aloe-plain",
            canonical_id=right.id,
        ),
        9.71,
    )
    third = _priced(
        session,
        _product(session, "aptekonline", "Selestoderm-V məlhəm  30 q", canonical_id=right.id),
        9.71,
    )
    session.commit()
    return SimpleNamespace(
        wrong=wrong, right=right, garamycin=garamycin, client=client, plain=plain, third=third
    )


def test_the_rest_joins_the_cluster_of_a_sibling_that_is_in_its_place(db_session):
    """Сосед с ценой клиента стоит в верной паре — туда и переходит товар клиента."""
    case = _plain_client_row_with_the_garamycin_row(db_session)
    wrong_id, right_id = case.wrong.id, case.right.id

    planned = matcher.replace_price_strangers(db_session, dry_run=True)
    assert not db_session.dirty and not db_session.deleted
    assert case.client.canonical_id == wrong_id
    replaced = matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert (
        replaced
        == planned
        == [
            {
                "match_id": right_id,
                "site": "aloe",
                "old": case.garamycin.id,
                "new": case.plain.id,
                "price": 9.71,
                "old_price": 8.15,
                "from_match": wrong_id,
                "swapped": False,
                "joined": [case.client.id],
                "left_alone": [case.garamycin.id],
            }
        ]
    )
    assert _same_cluster(case.client, case.plain, case.third)
    assert case.garamycin.canonical_id is None
    assert list(_clusters(db_session)) == [right_id]
    assert db_session.get(storage.Match, wrong_id) is None
    assert matcher.replace_price_strangers(db_session) == []


@pytest.mark.parametrize(
    "obstacle",
    [
        "the_site_is_already_there",
        "a_member_there_is_rejected",
        "a_member_there_costs_more",
        "a_member_there_is_further_by_name_than_the_sibling",
        "a_member_there_knows_less_of_the_country_than_the_sibling",
    ],
)
def test_the_rest_stays_when_it_does_not_fit_the_cluster_of_the_sibling(
    db_session, monkeypatch, obstacle
):
    from src import match_actions

    case = _plain_client_row_with_the_garamycin_row(db_session)
    if obstacle == "the_site_is_already_there":
        case.third.site = "pharmonline"
    elif obstacle == "a_member_there_is_rejected":
        match_actions.add_rejection(db_session, case.client.id, case.third.id, reason="op")
    elif obstacle == "a_member_there_costs_more":
        # Третий стоит на 0,7% дороже: сосед у себя не чужой (меньше процента),
        # но и «та же цена» для товара клиента — уже нет (больше полупроцента).
        db_session.query(storage.PriceSnapshot).filter_by(product_id=case.third.id).update(
            {"price": 9.78}
        )
    elif obstacle == "a_member_there_is_further_by_name_than_the_sibling":
        # Не дальше заменяемого, но дальше самого соседа: мерка — сосед.
        case.third.name = "Selestoderm-V Schering məlhəm  30 q"
        case.third.name_normalized = normalize_name(case.third.name)
        gap = matcher._name_gap
        assert (
            gap(case.client, case.plain)
            < gap(case.client, case.third)
            <= gap(case.client, case.garamycin)
        )
    else:
        _country(case.plain, "be")
        _country(case.client, "be")
    db_session.commit()
    before = _clusters(db_session)
    journal = _MatcherLog()
    monkeypatch.setattr(matcher, "log", journal)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before
    assert journal.kept_strangers() == [("sibling_agrees_with_its_cluster", case.garamycin.id)]


def test_the_price_compared_is_the_one_the_buyer_pays(db_session):
    """У соседа обычная цена 25, со скидкой 19,20 — как у конкурента."""
    case = _tablets_paired_with_suppositories(db_session)
    db_session.query(storage.PriceSnapshot).filter_by(product_id=case.tablets.id).update(
        {"price": 25.0, "discount_price": 19.2}
    )
    db_session.commit()

    replaced = matcher.replace_price_strangers(db_session)

    assert [(item["new"], item["price"]) for item in replaced] == [(case.tablets.id, 19.2)]


def _anchor_candidate_and_same_price_rival(session, *, rival_name, rival_kw=None):
    """Якорь (27,60) идёт раньше соперника (19,20); у конкурента один товар, 19,20."""
    anchor = _priced(
        session,
        _product(session, "pharmonline", "Polioksidonium 12 mq № 10", external_id="anchor"),
        27.6,
    )
    rival = _priced(
        session,
        _product(session, "pharmonline", rival_name, external_id="rival", **(rival_kw or {})),
        19.2,
    )
    candidate = _priced(
        session, _product(session, "aptekonline", "Polioksidonium   12 mq  N10"), 19.2
    )
    session.commit()
    return SimpleNamespace(anchor=anchor, rival=rival, candidate=candidate)


@pytest.mark.parametrize(
    "why", ["further_by_name", "cannot_take_it", "gone_from_the_site", "another_price_too"]
)
def test_passes_leave_the_candidate_to_the_anchor_when_the_rival_does_not_count(db_session, why):
    """Цена только выбирает между одинаково подходящими: соперник с ценой
    кандидата не в счёт, если он дальше по названию, сам взять кандидата не
    может или пропал с сайта; сосед с третьей ценой — тем более."""
    if why == "further_by_name":
        case = _anchor_candidate_and_same_price_rival(
            db_session, rival_name="Polioksidonium Petrovaks 12 mq № 10"
        )
        assert matcher._name_gap(case.rival, case.candidate) > matcher._name_gap(
            case.anchor, case.candidate
        )
    elif why == "cannot_take_it":
        case = _anchor_candidate_and_same_price_rival(
            db_session,
            rival_name="Polioksidonium 12 mq  № 10",
            rival_kw={"manufacturer_country_code": "ru", "country_resolution_status": "resolved"},
        )
        case.candidate.manufacturer_country_code = "tr"
        case.candidate.country_resolution_status = "resolved"
        db_session.commit()
    elif why == "another_price_too":
        case = _anchor_candidate_and_same_price_rival(
            db_session, rival_name="Polioksidonium 12 mq  № 10"
        )
        db_session.query(storage.PriceSnapshot).filter_by(product_id=case.rival.id).update(
            {"price": 30.0}
        )
        db_session.commit()
    else:
        case = _anchor_candidate_and_same_price_rival(
            db_session,
            rival_name="Polioksidonium 12 mq  № 10",
            rival_kw={"last_seen_at": utcnow() - timedelta(days=100)},
        )
        for i in range(2):  # свежий каталог сайта: строка именно пропала
            _product(db_session, "pharmonline", f"Fresh product {i} № 10", external_id=f"f{i}")
        db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(case.anchor, case.candidate)
    assert case.rival.canonical_id is None


def test_passes_count_a_same_price_rival_that_is_already_paired(db_session):
    """У конкурента две строки одного товара с одной ценой. Первую берёт товар
    клиента с той же ценой; вторая не достаётся его соседу с другой ценой."""
    rival = _priced(
        db_session,
        _product(db_session, "pharmonline", "Polioksidonium 12 mq  № 10", external_id="rival"),
        19.2,
    )
    anchor = _priced(
        db_session,
        _product(db_session, "pharmonline", "Polioksidonium 12 mq № 10", external_id="anchor"),
        27.6,
    )
    first, second = (
        _priced(
            db_session,
            _product(db_session, "aptekonline", "Polioksidonium   12 mq  N10", external_id=key),
            19.2,
        )
        for key in ("row-1", "row-2")
    )
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(rival, first)
    assert anchor.canonical_id is None and second.canonical_id is None


# ── Разбор 2026-10-13: контрпримеры, найденные на копии каталога ─────────────


def _country(product: storage.Product, code: str | None) -> None:
    product.manufacturer_country_code = code
    product.country_resolution_status = "resolved" if code else "unknown"


@pytest.mark.parametrize(
    "odd, rest, twin, replaced",
    [
        ("ru", None, "tr", False),  # у заменяемого страна подтверждена, у соседа другая
        ("ru", None, None, False),  # …или неизвестна
        (None, "ru", "tr", False),  # сосед конфликтует с остальными
        ("ru", "ru", "tr", False),
        ("ru", "ru", "ru", True),
        (None, None, "tr", True),  # сосед знает больше — можно
        (None, "ru", None, True),  # никто не знает меньше прежнего
        (None, "ru", "ru", True),
    ],
)
def test_the_price_never_outweighs_a_confirmed_country_in_the_step(
    db_session, odd, rest, twin, replaced
):
    case = _tablets_paired_with_suppositories(db_session)
    _country(case.suppositories, odd)
    _country(case.competitor, rest)
    _country(case.tablets, twin)
    db_session.commit()

    assert bool(matcher.replace_price_strangers(db_session)) is replaced


def test_passes_do_not_prefer_the_price_over_a_confirmed_country(db_session):
    """«Ringer 400 ml» на копии: aloe [ua] 1,752; клиент [ua] 1,54; клиент
    [страна неизвестна] 1,75. Пара — товары одной подтверждённой страны."""
    confirmed = _priced(
        db_session,
        _product(db_session, "pharmonline", "Ringer 400 ml ( Məhlul)", external_id="ua"),
        1.54,
    )
    unknown = _priced(
        db_session, _product(db_session, "pharmonline", "Ringer  400 ml", external_id="?"), 1.75
    )
    aloe = _priced(db_session, _product(db_session, "aloe", "Ringer 400 ml"), 1.752)
    _country(confirmed, "ua")
    _country(aloe, "ua")
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(confirmed, aloe) and unknown.canonical_id is None


def test_the_anchor_does_not_prefer_a_same_price_candidate_that_knows_less_of_the_country(
    db_session,
):
    """То же, когда якорем идёт товар конкурента: кандидат с его ценой, но без
    страны, не обходит кандидата с подтверждённой страной."""
    aloe = _priced(db_session, _product(db_session, "aloe", "Ringer 400 ml"), 1.752)
    confirmed = _priced(
        db_session,
        _product(db_session, "pharmonline", "Ringer 400 ml ( Məhlul)", external_id="ua"),
        1.54,
    )
    unknown = _priced(
        db_session, _product(db_session, "pharmonline", "Ringer  400 ml", external_id="?"), 1.75
    )
    _country(confirmed, "ua")
    _country(aloe, "ua")
    db_session.commit()

    assert matcher._closest_first(
        aloe,
        [unknown, confirmed],
        storage.latest_snapshots_per_product(db_session, [aloe.id, confirmed.id, unknown.id]),
    ) == [unknown, confirmed]  # порядок прежний: предпочтения по цене нет
    matcher.match_products(db_session)
    assert _same_cluster(confirmed, aloe)


def test_a_gone_rival_that_still_has_a_cluster_does_not_take_the_pair_from_a_live_row(
    db_session,
):
    """«Deprestin Plus» на копии: строку не видели с июня, она одна в ручном
    кластере с ценой конкурента; живой товар клиента стоит на копейки иначе."""
    manual = _cluster(db_session, "Deprestin Plus  N30", is_manual=True)
    _priced(
        db_session,
        _product(
            db_session,
            "pharmonline",
            "Deprestin plus  № 30",
            external_id="gone",
            canonical_id=manual.id,
            last_seen_at=utcnow() - timedelta(days=120),
        ),
        23.5,
    )
    client = _priced(
        db_session,
        _product(db_session, "pharmonline", "Deprestin Plus № 30", external_id="live"),
        23.1,
    )
    aloe = _priced(db_session, _product(db_session, "aloe", "Deprestin plus 30 əd"), 23.5)
    for i in range(3):  # свежий каталог сайта: строка именно пропала
        _product(db_session, "pharmonline", f"Fresh product {i} № 10", external_id=f"f{i}")
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(client, aloe)


@pytest.mark.parametrize("why", ["page_is_dead", "out_of_stock"])
def test_a_rival_that_cannot_be_bought_now_does_not_dispute_the_candidate(db_session, why):
    case = _anchor_candidate_and_same_price_rival(
        db_session, rival_name="Polioksidonium 12 mq  № 10"
    )
    if why == "page_is_dead":
        case.rival.url_dead_at = utcnow()
    else:
        case.rival.offer_availability_status = "out_of_stock"
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(case.anchor, case.candidate)
    assert case.rival.canonical_id is None


def test_an_anchor_with_the_candidate_price_is_not_denied_by_a_same_price_rival(db_session):
    """Два товара клиента с ценой кандидата: первый якорь его и берёт."""
    case = _anchor_candidate_and_same_price_rival(
        db_session, rival_name="Polioksidonium 12 mq  № 10"
    )
    db_session.query(storage.PriceSnapshot).filter_by(product_id=case.anchor.id).update(
        {"price": 19.2}
    )
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(case.anchor, case.candidate)


def test_the_price_breaks_ties_only_among_equally_close_and_equally_fresh(db_session):
    """Порядок кандидатов: свой кластер, близость названия, свежесть — и лишь
    потом цена."""
    anchor = _priced(
        db_session, _product(db_session, "aptekonline", "Polioksidonium   12 mq  N10"), 19.2
    )
    closer = _priced(
        db_session,
        _product(db_session, "pharmonline", "Polioksidonium 12 mq № 10", external_id="closer"),
        27.6,
    )
    same_price_but_further = _priced(
        db_session,
        _product(
            db_session, "pharmonline", "Polioksidonium Petrovaks 12 mq № 10", external_id="far"
        ),
        19.2,
    )
    same_price_but_not_seen_lately = _priced(
        db_session,
        _product(
            db_session,
            "pharmonline",
            "Polioksidonium 12 mq  № 10",
            external_id="stale",
            status="unknown",
        ),
        19.2,
    )
    db_session.commit()
    prices = storage.latest_snapshots_per_product(
        db_session,
        [anchor.id, closer.id, same_price_but_further.id, same_price_but_not_seen_lately.id],
    )

    assert matcher._closest_first(
        anchor, [same_price_but_further, same_price_but_not_seen_lately, closer], prices
    ) == [closer, same_price_but_not_seen_lately, same_price_but_further]


def test_a_gone_row_freed_by_the_step_does_not_start_a_new_pair(db_session):
    """Правило о пропавших строках действует и на строку, которую освободил шаг."""
    from src import main as main_mod

    old_pair, live_pair = _cluster(db_session, "old"), _cluster(db_session, "new")
    gone = _priced(
        db_session,
        _product(
            db_session,
            "pharmonline",
            "Foobarin 10 mq № 30",
            external_id="gone",
            canonical_id=old_pair.id,
            last_seen_at=utcnow() - timedelta(days=100),
        ),
        12.0,
    )
    right = _priced(
        db_session,
        _product(
            db_session,
            "aptekonline",
            "Foobarin 10 mq N30",
            external_id="right",
            canonical_id=old_pair.id,
        ),
        10.0,
    )
    client = _priced(
        db_session,
        _product(
            db_session,
            "pharmonline",
            "Foobarin 10 mq № 30",
            external_id="live",
            canonical_id=live_pair.id,
        ),
        10.0,
    )
    wrong = _priced(
        db_session,
        _product(
            db_session,
            "aptekonline",
            "Foobarin 10 mq  N30",
            external_id="wrong",
            canonical_id=live_pair.id,
        ),
        12.0,
    )
    for i in range(3):
        _product(db_session, "pharmonline", f"Fresh product {i} № 10", external_id=f"f{i}")
    db_session.commit()

    main_mod._run_matching_stage(db_session)
    db_session.commit()

    assert _same_cluster(client, right)
    assert gone.canonical_id is None and wrong.canonical_id is None


def test_the_cluster_is_renamed_when_its_name_was_the_name_of_the_one_who_left(db_session):
    """Название пары стоит в сравнении и в заголовке алерта."""
    match = _cluster(
        db_session,
        "Qlipsidan 50    50  mq  N30 (Glipsidan)",
        canonical_brand="Qlipsidan",
        canonical_dosage="50mq",
        canonical_pack_size="n30",
    )
    wrong = _priced(
        db_session,
        _product(
            db_session,
            "aptekonline",
            "Qlipsidan 50    50  mq  N30 (Glipsidan)",
            external_id="wrong",
            canonical_id=match.id,
        ),
        14.59,
    )
    client = _priced(
        db_session,
        _product(db_session, "pharmonline", "Qlipsidan M № 30 ( Tabletlər)", canonical_id=match.id),
        14.76,
    )
    right = _priced(
        db_session,
        _product(db_session, "aptekonline", "Qlipsidan M   N30 (Glipsidan)", external_id="right"),
        14.76,
    )
    db_session.commit()

    assert matcher.replace_price_strangers(db_session, dry_run=True)
    assert match.canonical_name == wrong.name
    assert matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert _same_cluster(client, right)
    first = min(client, right, key=lambda row: row.id)
    assert (match.canonical_name, match.canonical_dosage, match.canonical_pack_size) == (
        first.name,
        first.dosage,
        first.pack_size,
    )
    assert match.canonical_dosage != "50mq"


def test_the_cluster_keeps_a_name_that_one_of_its_members_carries(db_session):
    """Название носит товар, который в кластере есть (здесь — вошедший сосед, и
    он не первый по номеру): менять нечего."""
    case = _tablets_paired_with_suppositories(db_session)
    case.match.canonical_name = case.tablets.name
    db_session.commit()
    assert case.competitor.id < case.tablets.id

    assert matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert case.match.canonical_name == case.tablets.name


def test_a_name_the_one_who_left_carried_in_an_older_spelling_is_replaced_too(db_session):
    """Название кластера не обновляется вслед за товаром: сверяем с оставшимися."""
    case = _tablets_paired_with_suppositories(db_session)
    case.match.canonical_name = "Polioksidonium 12 mq № 10 (prejnee napisanie)"
    db_session.commit()

    assert matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert case.match.canonical_name == min(case.tablets, case.competitor, key=lambda p: p.id).name


def test_both_clusters_of_a_swap_are_named_after_their_own_members(db_session):
    case = _crossed_pairs(db_session)
    case.first.canonical_name = case.competitor_lemon.name  # уйдёт во второй
    case.second.canonical_name = case.competitor_plain.name  # уйдёт в первый
    db_session.commit()

    assert [item["swapped"] for item in matcher.replace_price_strangers(db_session)] == [True]
    db_session.commit()

    for match in (case.first, case.second):
        assert match.canonical_name in {product.name for product in match.products}


def test_the_cluster_a_sibling_moved_out_of_is_named_after_its_own_member(db_session):
    """Обычный товар конкурента стоял третьим в кластере двух лимонных и дал ему название."""
    case = _crossed_pairs(db_session)
    case.client_lemon.name = "Septolete total limon və ağcaqayın № 16"
    case.competitor_lemon.name = "Septolete Total limon, kəndalaş N16"
    for row in (case.client_lemon, case.competitor_lemon):
        row.name_normalized = normalize_name(row.name)
    _priced(
        db_session,
        _product(
            db_session,
            "aloe",
            "Septolete total limon və ağcaqayın 16 əd",
            canonical_id=case.second.id,
        ),
        5.27,
    )
    case.second.canonical_name = case.competitor_plain.name
    db_session.commit()

    assert matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert case.second.canonical_name in {product.name for product in case.second.products}


def test_costs_bound_to_the_cluster_follow_the_rows_that_join_another(db_session):
    """Закупочная цена и остаток привязаны к кластеру, а не к товару."""
    case = _plain_client_row_with_the_garamycin_row(db_session)
    db_session.add_all(
        [
            storage.SupplierPrice(
                canonical_id=case.wrong.id, supplier_name="s", purchase_price=5.0
            ),
            storage.StockLevel(canonical_id=case.wrong.id, qty=3, is_in_stock=True),
        ]
    )
    db_session.commit()
    right_id = case.right.id

    assert matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert [row.canonical_id for row in db_session.query(storage.SupplierPrice)] == [right_id]
    assert [row.canonical_id for row in db_session.query(storage.StockLevel)] == [right_id]


def test_costs_stay_with_their_cluster_when_two_rows_only_swap(db_session):
    case = _crossed_pairs(db_session)
    for match in (case.first, case.second):
        db_session.add(
            storage.SupplierPrice(
                canonical_id=match.id, supplier_name=match.canonical_name, purchase_price=5.0
            )
        )
    db_session.commit()

    assert [item["swapped"] for item in matcher.replace_price_strangers(db_session)] == [True]
    db_session.commit()

    assert {
        row.supplier_name: row.canonical_id for row in db_session.query(storage.SupplierPrice)
    } == {"A": case.first.id, "B": case.second.id}


@pytest.mark.parametrize("where", ["in_the_cluster", "next_to_the_sibling"])
def test_a_row_of_a_site_that_was_not_scraped_freezes_its_cluster(db_session, where):
    """«Не собран» — не «пропал»: о строке неизвестно ни цены, ни продаётся ли товар."""
    if where == "in_the_cluster":
        match = _cluster(db_session, "Ametazon")
        _priced(
            db_session,
            _product(db_session, "pharmonline", "Ametazon 30 qr (Krem)", canonical_id=match.id),
            7.78,
        )
        _priced(
            db_session,
            _product(db_session, "aptekonline", "Ametazon   30 q", canonical_id=match.id),
            7.0,
        )
        # Третий сайт не собирался: его строка в кластере — неизвестность.
        _priced(
            db_session,
            _product(
                db_session,
                "aloe",
                "Ametazon 30 q",
                canonical_id=match.id,
                last_seen_at=utcnow() - timedelta(days=100),
            ),
            7.0,
        )
        _priced(
            db_session,
            _product(db_session, "pharmonline", "Ametazon 30 qr", external_id="sibling"),
            7.0,
        )
    else:
        # Сосед стоит в паре со строкой несобранного сайта: пара не брошена.
        case = _crossed_pairs(db_session)
        case.client_lemon.last_seen_at = utcnow() - timedelta(days=100)
        case.client_lemon.name = "Septolete total limon və ağcaqayın № 16"
        case.client_lemon.name_normalized = normalize_name(case.client_lemon.name)
        case.client_lemon.site = "aloe"
    db_session.commit()
    before = _clusters(db_session)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before


@pytest.mark.parametrize("third", ["out_of_stock_at_an_old_price", "without_a_price"])
def test_a_member_that_is_not_on_sale_does_not_vote_on_the_price(db_session, third):
    """Третий член кластера не в наличии или без цены: о цене судят живые."""
    case = _tablets_paired_with_suppositories(db_session)
    extra = _product(
        db_session,
        "aloe",
        "Polioksidonium 12 mq 10 əd",
        canonical_id=case.match.id,
        status="out_of_stock" if third == "out_of_stock_at_an_old_price" else "in_stock",
    )
    if third == "out_of_stock_at_an_old_price":
        _priced(db_session, extra, 15.0)
    db_session.commit()

    assert [item["new"] for item in matcher.replace_price_strangers(db_session)] == [
        case.tablets.id
    ]
    assert _same_cluster(case.tablets, case.competitor, extra)


def test_the_sibling_must_suit_every_member_including_those_not_on_sale(db_session):
    """Оператор развёл соседа с членом кластера, которого сейчас нет в наличии."""
    from src import match_actions

    case = _tablets_paired_with_suppositories(db_session)
    extra = _priced(
        db_session,
        _product(
            db_session,
            "aloe",
            "Polioksidonium 12 mq 10 əd",
            canonical_id=case.match.id,
            status="out_of_stock",
        ),
        15.0,
    )
    match_actions.add_rejection(db_session, case.tablets.id, extra.id, reason="operator")
    db_session.commit()
    before = _clusters(db_session)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before


def test_the_sibling_must_be_no_further_by_name_from_every_member(db_session):
    """Сосед ближе заменяемого к одному члену кластера и дальше от другого."""
    match = _cluster(db_session, "Ametazon")
    rows = [
        _priced(db_session, _product(db_session, site, name, canonical_id=match.id), price)
        for site, name, price in (
            ("pharmonline", "Ametazon 30 qr", 7.78),
            ("aptekonline", "Ametazon   30 q", 7.0),
            ("aloe", "Ametazon Biofarma Derm 30 q", 7.0),
        )
    ]
    sibling = _priced(
        db_session,
        _product(db_session, "pharmonline", "Ametazon Biofarma 30 qr", external_id="x"),
        7.0,
    )
    db_session.commit()
    assert matcher._name_gap(rows[2], sibling) < matcher._name_gap(rows[2], rows[0])
    assert matcher._name_gap(rows[1], sibling) > matcher._name_gap(rows[1], rows[0])
    rules = matcher._PassRules(db_session, [*rows, sibling])
    assert not any(rules.incompatible(row, sibling, rows[1:]) for row in rows[1:])
    before = _clusters(db_session)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before


def test_a_sibling_at_home_among_some_of_its_cluster_is_not_a_stranger_there(
    db_session, monkeypatch
):
    """В своём кластере сосед заметно расходится в цене с одним членом, а с
    другим — меньше чем на процент: чужой там не он. Обмен и переход остальных
    не подходят — пара остаётся."""
    here, home = _cluster(db_session, "here"), _cluster(db_session, "home")
    _priced(
        db_session,
        _product(db_session, "pharmonline", "Septolete total № 16", canonical_id=here.id),
        6.61,
    )
    stranger = _priced(
        db_session,
        _product(
            db_session,
            "aptekonline",
            "Septolete Total N16 (limon)",
            external_id="lemon",
            canonical_id=here.id,
        ),
        5.27,
    )
    for site, name, price in (
        ("aptekonline", "Septolete Total N16", 6.61),
        ("pharmonline", "Septolete total  № 16", 6.66),
        ("aloe", "Septolete total 16 əd", 9.0),
    ):
        _priced(
            db_session,
            _product(db_session, site, name, external_id=f"home-{site}", canonical_id=home.id),
            price,
        )
    db_session.commit()
    before = _clusters(db_session)
    journal = _MatcherLog()
    monkeypatch.setattr(matcher, "log", journal)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before
    assert journal.kept_strangers() == [("sibling_agrees_with_its_cluster", stranger.id)]


def test_two_clusters_cannot_both_join_the_same_sibling_cluster(db_session):
    """Второй неверный кластер того же сайта видит, что место уже занято."""
    case = _plain_client_row_with_the_garamycin_row(db_session)
    again = _cluster(db_session, "again")
    second_garamycin = _priced(
        db_session,
        _product(
            db_session,
            "aloe",
            "Selestoderm-V qaramisinlə məlhəm 30 qr",
            external_id="aloe-garamycin-2",
            canonical_id=again.id,
        ),
        8.15,
    )
    second_client = _priced(
        db_session,
        _product(
            db_session,
            "pharmonline",
            "Selestoderm V 30 qr  (Məlhəm)",
            external_id="client-2",
            canonical_id=again.id,
        ),
        9.71,
    )
    db_session.commit()

    replaced = matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert [item["joined"] for item in replaced] == [[case.client.id]]
    assert _same_cluster(case.client, case.plain, case.third)
    assert _same_cluster(second_client, second_garamycin)
    assert all(
        len({site for site, _ in rows}) == len(rows) for rows in _clusters(db_session).values()
    )


def test_a_swap_needs_the_stranger_to_suit_every_member_there_including_those_not_on_sale(
    db_session,
):
    """В кластере соседа есть товар не в наличии, с которым оператор заменяемого
    развёл: обмена нет. Сосед и у себя чужой по цене — он переходит один."""
    from src import match_actions

    case = _crossed_pairs(db_session)
    off_sale = _priced(
        db_session,
        _product(
            db_session,
            "aloe",
            "Septolete total 16 əd (limon)",
            canonical_id=case.second.id,
            status="out_of_stock",
        ),
        5.27,
    )
    match_actions.add_rejection(db_session, case.competitor_lemon.id, off_sale.id, reason="op")
    db_session.commit()

    replaced = matcher.replace_price_strangers(db_session)
    db_session.commit()

    assert [(item["swapped"], item["new"]) for item in replaced] == [
        (False, case.competitor_plain.id)
    ]
    assert _same_cluster(case.client_plain, case.competitor_plain)
    assert case.competitor_lemon.canonical_id is None
    assert _same_cluster(case.client_lemon, off_sale)


def test_a_sibling_whose_price_once_agreed_with_its_cluster_is_not_taken_from_it(
    db_session, monkeypatch
):
    """Сосед расходится в цене со своим кластером сейчас, но раньше стоил так
    же: там он на месте, просто цена изменилась."""
    case = _crossed_pairs(db_session)
    case.client_lemon.name = "Septolete total limon və ağcaqayın № 16"
    case.competitor_lemon.name = "Septolete Total limon, kəndalaş N16"
    for row in (case.client_lemon, case.competitor_lemon):
        row.name_normalized = normalize_name(row.name)
    _priced(db_session, case.competitor_plain, 5.27, days_ago=30)
    db_session.commit()
    before = _clusters(db_session)
    journal = _MatcherLog()
    monkeypatch.setattr(matcher, "log", journal)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before
    # Второй кластер молчит по той же причине со своей стороны: его члены
    # раньше стоили одинаково.
    assert journal.kept_strangers() == [
        ("sibling_agrees_with_its_cluster", case.competitor_lemon.id),
        ("prices_agreed_before", case.client_lemon.id),
    ]


def test_a_fresh_row_of_a_site_that_was_not_scraped_is_judged_like_any_other(db_session):
    """«Не собран» — про строки, которых давно не видели. Свежую строку того же
    сайта (частичный сбор её видел) шаг считает живой, и кластер она не
    замораживает."""
    match = _cluster(db_session, "Polioksidonium")
    suppositories = _priced(
        db_session,
        _product(
            db_session,
            "pharmonline",
            "Polioksidonium 12 mq № 10",
            external_id="supp",
            canonical_id=match.id,
        ),
        27.6,
    )
    fresh_aloe = _priced(
        db_session,
        _product(db_session, "aloe", "Polioksidonium 12 mq 10 əd", canonical_id=match.id),
        19.2,
    )
    tablets = _priced(
        db_session,
        _product(db_session, "pharmonline", "Polioksidonium 12 mq  № 10", external_id="tabs"),
        19.2,
    )
    for i in range(3):  # у aloe видели одну строку из четырёх: сайт не собран
        _product(
            db_session,
            "aloe",
            f"Unseen product {i} 10 əd",
            external_id=f"unseen-{i}",
            last_seen_at=utcnow() - timedelta(days=100),
        )
    db_session.commit()

    assert matcher.replace_price_strangers(db_session)
    assert _same_cluster(tablets, fresh_aloe) and suppositories.canonical_id is None


@pytest.mark.parametrize("where", ["before_the_first_write", "after_the_writes"])
def test_a_failed_reseat_leaves_neither_the_database_nor_the_session_half_changed(
    db_session, monkeypatch, where
):
    """Шаг упал — этап идёт дальше с прежними парами. Точка сохранения
    возвращает базу, но не сессию: состав кластера, прочитанный шагом после его
    записей, остался бы «без ушедшего соседа». Проходы должны получить сессию,
    которая с базой согласна."""
    from sqlalchemy import select

    from src import main as main_mod

    case = _crossed_pairs(db_session)
    case.client_lemon.name = "Septolete total limon və ağcaqayın № 16"
    case.competitor_lemon.name = "Septolete Total limon, kəndalaş N16"
    for row in (case.client_lemon, case.competitor_lemon):
        row.name_normalized = normalize_name(row.name)
    _priced(
        db_session,
        _product(
            db_session,
            "aloe",
            "Septolete total limon və ağcaqayın 16 əd",
            canonical_id=case.second.id,
        ),
        5.27,
    )
    # Названия носят остающиеся члены: шаг записи кластеров не меняет, и откат
    # точки сохранения сам их состав в сессии не сбросит.
    case.first.canonical_name = case.client_plain.name
    case.second.canonical_name = case.client_lemon.name
    db_session.commit()
    before = _clusters(db_session)
    real = matcher.replace_price_strangers
    seen_by_the_passes: dict[int, set[int]] = {}

    def failing(session, **kwargs):
        if where == "after_the_writes":
            assert [item["new"] for item in real(session, **kwargs)] == [case.competitor_plain.id]
        raise ValueError("boom")

    def passes(session, **kwargs):
        in_the_database: dict[int, set[int]] = {case.first.id: set(), case.second.id: set()}
        for product_id, cluster_id in session.execute(
            select(storage.Product.id, storage.Product.canonical_id).where(
                storage.Product.canonical_id.is_not(None)
            )
        ):
            in_the_database[cluster_id].add(product_id)
        for match in (case.first, case.second):
            seen_by_the_passes[match.id] = {product.id for product in match.products}
        assert seen_by_the_passes == in_the_database
        return 0

    monkeypatch.setattr(matcher, "replace_price_strangers", failing)
    monkeypatch.setattr(matcher, "match_products", passes)

    summary = main_mod._run_matching_stage(db_session)
    db_session.commit()

    assert summary["reseated"] is None and seen_by_the_passes
    assert _clusters(db_session) == before


# ── Правила о самих товарах (`_PassRules.incompatible`) ──────────────────────
#
# Тело `blocked` перенесено в общий набор 2026-10-13. Эти три правила до
# переноса не закреплял ни один тест: без любого из них все тесты проходили.


@pytest.mark.parametrize(
    "client, competitor",
    [
        ("Kodelak Neo 100 ml", "Kodelak   100 ml"),  # модификатор — другая формула
        ("Ametazon 30 qr (Krem)", "Ametazon məlhəm   30 q"),  # крем и мазь
    ],
)
def test_passes_refuse_a_pair_the_shared_rules_forbid(db_session, client, competitor):
    ours = _product(db_session, "pharmonline", client)
    theirs = _product(db_session, "aptekonline", competitor)
    db_session.commit()
    rules = matcher._PassRules(db_session, [ours, theirs])

    assert rules.incompatible(ours, theirs, [ours])
    matcher.match_products(db_session)
    assert ours.canonical_id is None and theirs.canonical_id is None


def test_a_row_without_a_form_is_not_the_form_its_site_names_on_a_sibling(db_session):
    """У сайта два товара с одним названием: у одного форма названа, у другого
    нет. Значит, второй — не эта форма, и товару клиента с этой формой он не
    пара, хотя идёт в списке раньше."""
    unnamed = _product(
        db_session, "aptekonline", "Polioksidonium   12 mq  N10", external_id="no-form"
    )
    named = _product(
        db_session, "aptekonline", "Polioksidonium  12 mq  N10 (tabletlər)", external_id="tabs"
    )
    client = _product(db_session, "pharmonline", "Polioksidonium 12 mq № 10 (Tabletlər)")
    db_session.commit()

    matcher.match_products(db_session)

    assert _same_cluster(client, named) and unnamed.canonical_id is None


def test_the_sibling_is_judged_with_every_member_as_the_anchor(db_session):
    """Форму проходы сверяют с якорем. У первого члена кластера форма не
    названа, у второго — мазь: крем ему не пара, кто бы ни шёл первым."""
    match = _cluster(db_session, "Ametazon")
    unnamed = _priced(
        db_session, _product(db_session, "aloe", "Ametazon 30 q", canonical_id=match.id), 7.0
    )
    ointment = _priced(
        db_session,
        _product(db_session, "aptekonline", "Ametazon məlhəm   30 q", canonical_id=match.id),
        7.0,
    )
    _priced(
        db_session,
        _product(db_session, "pharmonline", "Ametazon 30 qr (Məlhəm)", canonical_id=match.id),
        9.0,
    )
    cream = _priced(
        db_session,
        _product(db_session, "pharmonline", "Ametazon 30 qr (Krem)", external_id="cream"),
        7.0,
    )
    db_session.commit()
    rules = matcher._PassRules(db_session, [unnamed, ointment, cream])
    assert not rules.incompatible(unnamed, cream, [unnamed, ointment])
    assert rules.incompatible(ointment, cream, [unnamed, ointment])
    before = _clusters(db_session)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before


def test_a_sibling_that_costs_as_a_member_of_its_cluster_now_off_sale_is_at_home(db_session):
    """Журнал цен спрашивается со всеми членами кластера соседа, не только с
    теми, кто в продаже: сосед стоит, как товар, которого сейчас нет в наличии."""
    case = _crossed_pairs(db_session)
    case.client_lemon.name = "Septolete total limon və ağcaqayın № 16"
    case.competitor_lemon.name = "Septolete Total limon, kəndalaş N16"
    for row in (case.client_lemon, case.competitor_lemon):
        row.name_normalized = normalize_name(row.name)
    # Страна лимонного товара клиента подтверждена: со своей стороны его кластер
    # соседа без страны не примет, и решает только кластер обычного товара.
    _country(case.client_lemon, "si")
    _priced(
        db_session,
        _product(
            db_session,
            "aloe",
            "Septolete total 16 əd",
            canonical_id=case.second.id,
            status="out_of_stock",
        ),
        6.61,
    )
    db_session.commit()
    before = _clusters(db_session)

    assert matcher.replace_price_strangers(db_session) == []
    assert _clusters(db_session) == before
