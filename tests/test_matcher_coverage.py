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
