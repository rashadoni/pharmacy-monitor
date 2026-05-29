"""Тесты fuzzy-матчинга товаров между сайтами."""

from src import match_actions, matcher, storage
from src.matcher import (
    _has_conflicting_form,
    _has_conflicting_gender,
    _has_conflicting_series_number,
    _has_conflicting_strength_number,
    _has_conflicting_variant_atoms,
    _has_conflicting_variant_tokens,
    _has_extreme_length_disparity,
    _has_perunit_mismatch,
    _is_significant_variant_token,
    _pack_count,
    _strength_numbers,
    _variant_atoms,
)


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
    assert a.canonical_id != b.canonical_id or (a.canonical_id is None and b.canonical_id is None)


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


# ── Unit-тесты для guard-функций ─────────────────────────────────────────────


class TestHasConflictingSeriesNumber:
    def test_blocks_different_stages(self):
        assert _has_conflicting_series_number("nutrilon 1", "nutrilon 4") is True
        assert _has_conflicting_series_number("friso gold 1", "friso gold 3") is True
        assert _has_conflicting_series_number("huggies 4", "huggies 5") is True

    def test_allows_same_stage(self):
        assert _has_conflicting_series_number("nutrilon 1", "nutrilon 1") is False
        assert _has_conflicting_series_number("nan optipro 1", "nan 1") is False

    def test_allows_if_one_has_no_digit(self):
        # brand-only без уникальных токенов vs нумерованный → неполные данные, не блокируем
        # «friso gold» vs «friso gold 2»: unique в a = {} (friso и gold есть и в b) → не блокируем
        assert _has_conflicting_series_number("nutrilon", "nutrilon 1") is False
        assert _has_conflicting_series_number("friso gold", "friso gold 2") is False
        assert _has_conflicting_series_number("huggies", "huggies 4") is False

    def test_blocks_named_variant_vs_numbered_stage(self):
        # Именованная формула (описательное имя) ↔ нумерованная ступень → разные
        assert _has_conflicting_series_number("nutrilon pronutra", "nutrilon 4") is True
        assert _has_conflicting_series_number("nutrilon premium hipoallergik", "nutrilon 3") is True
        assert _has_conflicting_series_number("nutrilon premium antireflüks", "nutrilon 2") is True
        # «premium» — длинный уникальный токен → тоже блокируем (консервативно)
        assert _has_conflicting_series_number("nutrilon premium", "nutrilon 1") is True

    def test_allows_brand_only_no_tokens_vs_numbered(self):
        # Бренд-only БЕЗ уникальных токенов → неполные данные, не блокируем
        assert _has_conflicting_series_number("nutrilon", "nutrilon 1") is False
        assert _has_conflicting_series_number("huggies", "huggies 4") is False

    def test_ignores_multidigit_numbers(self):
        # «10mg», «N20», «500» — не серийные номера
        assert _has_conflicting_series_number("vitamin c 500mg", "vitamin c 1000mg") is False
        assert _has_conflicting_series_number("aspirin n10", "aspirin n20") is False

    def test_blocks_in_db_integration(self, db_session):
        """Nutrilon 1 и Nutrilon 4 в одном бакете — не должны матчиться."""
        a = _make_product(
            db_session,
            site="pharmonline",
            external_id="nl-1",
            name="Nutrilon 1 600g",
            name_normalized="nutrilon 1",
            brand="nutrilon",
            pack_size="600g",
        )
        b = _make_product(
            db_session,
            site="aptekonline",
            external_id="nl-4",
            name="Nutrilon 4 600g",
            name_normalized="nutrilon 4",
            brand="nutrilon",
            pack_size="600g",
        )
        db_session.commit()
        matcher.match_products(db_session)
        db_session.refresh(a)
        db_session.refresh(b)
        assert a.canonical_id != b.canonical_id or (
            a.canonical_id is None and b.canonical_id is None
        )


class TestHasConflictingForm:
    def test_blocks_drops_vs_spray(self):
        assert _has_conflicting_form("Otrivin drops 0.1%", "Otrivin spray 0.1%") is True

    def test_blocks_cream_vs_ointment(self):
        assert _has_conflicting_form("Bepanten krem 30g", "Bepanten məlhəm 30g") is True

    def test_allows_same_form(self):
        assert (
            _has_conflicting_form("Paracetamol tablet 500mg", "Paracetamol tab 500mg N20") is False
        )

    def test_allows_if_form_unknown(self):
        # Форма не распознана → не блокируем
        assert _has_conflicting_form("Lopril 10mg N20", "Lopril 10mg N30") is False

    def test_allows_if_one_form_unknown(self):
        assert _has_conflicting_form("Vitamin C", "Vitamin C tablet 500mg") is False

    def test_az_plural_and_sorma_canonicalize_to_group(self):
        """2026-05-29: AZ-плюралы и «sorma» канонизируются в свою группу, не
        дают ложный form-конфликт против канона. Гомеопатия «sorma tabletlər»
        (сублингвальные таблетки) ≡ «tabletlər» ≡ tablet → один товар."""
        # sorma tabletlər ≡ tabletlər (recall-edge: Afalaza/Anaferon/Divaza)
        assert (
            _has_conflicting_form(
                "Afalaza 6 mq N100 (sorma tabletlər)", "Afalaza 6 mq № 100 (tabletlər)"
            )
            is False
        )
        # AZ-плюрал капсул/ампул ≡ канон
        assert _has_conflicting_form("Drug N20 (kapsulalar)", "Drug № 20 (Kapsul)") is False
        assert _has_conflicting_form("Drug 2ml (ampulalar)", "Drug 2 ml (Ampoules)") is False
        # но РАЗНЫЕ группы по-прежнему конфликтуют
        assert _has_conflicting_form("Drug tabletlər", "Drug şərbət") is True  # tablet≠syrup


class TestHasExtremeLengthDisparity:
    def test_blocks_stub_vs_full(self):
        assert (
            _has_extreme_length_disparity("venatura", "venatura vitamin a palmitate retinol zinc")
            is True
        )
        assert (
            _has_extreme_length_disparity("bioderma", "bioderma atoderm intensive baume 200ml")
            is True
        )

    def test_allows_similar_length(self):
        assert _has_extreme_length_disparity("nutrilon 1", "nutrilon 1 comfort") is False
        assert _has_extreme_length_disparity("lopril h", "lopril h 10mg") is False

    def test_allows_short_pairs(self):
        # Оба имени короткие — longer < 3, пропускаем
        assert _has_extreme_length_disparity("lopril", "lopril h") is False
        assert _has_extreme_length_disparity("foo", "foo bar") is False

    def test_symmetry(self):
        # Порядок аргументов не важен
        a = "venatura"
        b = "venatura vitamin a palmitate retinol zinc"
        assert _has_extreme_length_disparity(a, b) == _has_extreme_length_disparity(b, a)

    # ── Stub-aware redesign (2026-05-29, Perplexity+Codex consensus) ──────────

    def test_allows_verbose_superset_with_brand_hint(self):
        """Verbose-vs-terse ОДНОГО товара НЕ блокируется когда есть общий
        значимый токен (cobanyastıgı=ромашка). Это главный fix — раньше
        блокировалось как disparity 3/13."""
        terse = "fitoton sampun cobanyastıgı"
        verbose = (
            "fitoton sampun cobanyastıgı ultra care d/norm sac ucun "
            "kosmetika herba flora azerbaycan"
        )
        assert _has_extreme_length_disparity(terse, verbose, brand_hint="Fitoton") is False

    def test_blocks_stub_even_with_brand_hint(self):
        """venatura остаётся заблокированным даже с brand_hint — после удаления
        бренда у короткого 0 значимых токенов → stub."""
        assert (
            _has_extreme_length_disparity(
                "venatura",
                "venatura vitamin a palmitate retinol zinc",
                brand_hint="venatura",
            )
            is True
        )

    def test_blocks_different_variants_via_noise_filter(self):
        """РАЗНЫЕ варианты (mineral vs chamomile) при verbose-длинном →
        блокируется: 'sampun' это noise, дифференциаторы (mineral/cobanyastıgı)
        НЕ пересекаются → нет общего значимого токена → stub-block.

        Это precision: mineral-шампунь не должен склеиться с ромашковым."""
        mineral = "fitoton sampun mineral"
        chamomile_verbose = (
            "fitoton sampun cobanyastıgı ultra care kosmetika herba flora azerbaycan"
        )
        assert (
            _has_extreme_length_disparity(mineral, chamomile_verbose, brand_hint="Fitoton") is True
        )

    def test_no_brand_hint_requires_two_overlap(self):
        """Без brand_hint требуется ≥2 общих значимых токена (защита от
        случая где единственное совпадение — сам бренд)."""
        # 1 общий значимый (kreatin) + бренд не указан → 1 < 2 → BLOCK
        assert (
            _has_extreme_length_disparity(
                "solgar kreatin", "solgar kreatin monohydrate powder extra strength 300"
            )
            is False  # 2 общих: solgar + kreatin (бренд не исключён без hint) → allow
        )
        # чистый stub без hint
        assert _has_extreme_length_disparity("solgar", "solgar omega 3 fish oil softgels") is True


# ─────────────────────────────────────────────────────────────────────────────


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


# ── Snowball / cluster-growth regression ─────────────────────────────────────


def test_snowball_no_site_conflict_in_existing_cluster(db_session):
    """_persist_match не должен добавлять продукт если его сайт уже занят в матче.

    Воспроизводит баг роста кластера: Nivea 150ml (brand=nivea, pack=150ml)
    — три сайта, у каждого 2 разных варианта. Первый прогон матчит по одному
    с каждого сайта. Второй прогон НЕ должен добавить второй вариант с того
    же сайта к существующему кластеру.
    """
    # Первый прогон: p1(aptk) + p3(pharm) матчатся → Match X
    # token_set_ratio("nivea deodorant", "nivea fresh deodorant") = 100 → матч
    p1 = _make_product(
        db_session,
        site="aptekonline",
        external_id="a1",
        name="Nivea deodorant 150ml",
        name_normalized="nivea deodorant",
        brand="nivea",
        dosage="150ml",
        pack_size="150ml",
    )
    p3 = _make_product(
        db_session,
        site="pharmonline",
        external_id="p1",
        name="Nivea fresh deodorant 150ml",
        name_normalized="nivea fresh deodorant",
        brand="nivea",
        dosage="150ml",
        pack_size="150ml",
    )
    matcher.match_products(db_session)
    db_session.refresh(p1)
    db_session.refresh(p3)
    first_cid = p1.canonical_id
    assert first_cid is not None, "первый прогон должен создать кластер"
    assert p3.canonical_id == first_cid

    # Второй прогон: добавляем p2(aptk) — тот же сайт что p1, но другой вариант.
    # Используем одинаковые имена чтобы p2+p4 гарантированно матчились между собой
    # (тест проверяет snowball-guard, не variant-guard).
    p2 = _make_product(
        db_session,
        site="aptekonline",
        external_id="a2",
        name="Nivea sport deodorant 150ml",
        name_normalized="nivea sport deodorant",
        brand="nivea",
        dosage="150ml",
        pack_size="150ml",
    )
    p4 = _make_product(
        db_session,
        site="pharmonline",
        external_id="p2",
        name="Nivea sport deodorant 150ml",
        name_normalized="nivea sport deodorant",
        brand="nivea",
        dosage="150ml",
        pack_size="150ml",
    )
    db_session.commit()

    matcher.match_products(db_session)
    db_session.refresh(p1)
    db_session.refresh(p2)
    db_session.refresh(p3)
    db_session.refresh(p4)

    # Старый кластер не должен был вырасти до 4 продуктов
    assert p2.canonical_id != first_cid, (
        "второй aptekonline-продукт НЕ должен попасть в кластер где aptekonline уже занят"
    )
    assert p4.canonical_id != first_cid, (
        "второй pharmonline-продукт НЕ должен попасть в кластер где pharmonline уже занят"
    )
    # p2 и p4 могут образовать собственный кластер — это нормально
    assert p2.canonical_id == p4.canonical_id


def test_snowball_cluster_stays_max_one_per_site(db_session):
    """После N прогонов matcher'а ни один кластер не содержит 2+ продуктов с одного сайта.

    Используем одинаковое name_normalized для всех продуктов, чтобы матч
    не блокировался variant-guard'ом — тест проверяет именно snowball-guard.
    """
    variants = [
        ("aptekonline", "a1", "testbrand deodorant"),
        ("aptekonline", "a2", "testbrand deodorant"),
        ("aptekonline", "a3", "testbrand deodorant"),
        ("pharmonline", "p1", "testbrand deodorant"),
        ("pharmonline", "p2", "testbrand deodorant"),
        ("pharmonline", "p3", "testbrand deodorant"),
        ("aloe", "l1", "testbrand deodorant"),
    ]
    products = []
    for site, ext_id, name in variants:
        p = _make_product(
            db_session,
            site=site,
            external_id=ext_id,
            name=name,
            name_normalized=name,
            brand="testbrand",
            dosage="150ml",
            pack_size="150ml",
        )
        products.append(p)
    db_session.commit()

    # Прогоняем matcher 3 раза (имитация нескольких ночных запусков)
    for _ in range(3):
        matcher.match_products(db_session)
        db_session.expire_all()

    for p in products:
        db_session.refresh(p)

    from collections import defaultdict

    cid_to_sites: dict[int, list[str]] = defaultdict(list)
    for p in products:
        if p.canonical_id:
            cid_to_sites[p.canonical_id].append(p.site)

    for cid, sites in cid_to_sites.items():
        assert len(sites) == len(set(sites)), f"Кластер {cid} содержит дубли сайтов: {sites}"


# ── TestHasConflictingGender ──────────────────────────────────────────────────


class TestHasConflictingGender:
    def test_blocks_male_vs_female_az(self):
        # "oglanlar" (после strip breve: oğlanlar→oglanlar) vs "qız" (dotless-i)
        assert (
            _has_conflicting_gender(
                "usaq bezi huggies 5 oglanlar ucun",
                "huggies 5 ultra qız ucun",
            )
            is True
        )

    def test_blocks_female_vs_male_order(self):
        # Порядок аргументов не важен
        assert (
            _has_conflicting_gender(
                "huggies qızlar n64",
                "huggies oglanlar n64",
            )
            is True
        )

    def test_allows_same_gender_male(self):
        assert _has_conflicting_gender("huggies oglanlar n64", "huggies oglanlar n40") is False

    def test_allows_same_gender_female(self):
        assert _has_conflicting_gender("huggies qız n28", "huggies qız n56") is False

    def test_allows_no_gender_tokens(self):
        assert _has_conflicting_gender("paracetamol 500mg", "paracetamol 500mg n10") is False

    def test_allows_one_side_no_gender(self):
        # У одной стороны нет токена — не блокируем (неполные данные)
        assert _has_conflicting_gender("huggies n64", "huggies oglanlar n64") is False

    def test_blocks_in_db_integration(self, db_session):
        """Huggies для мальчиков и для девочек в одном бакете — не должны матчиться."""
        a = _make_product(
            db_session,
            site="pharmonline",
            external_id="hg-m",
            name="Huggies oğlanlar N64",
            name_normalized="usaq bezi huggies 5 oglanlar ucun",
            brand="huggies",
            pack_size="n64",
        )
        b = _make_product(
            db_session,
            site="aptekonline",
            external_id="hg-f",
            name="Huggies qızlar N64",
            name_normalized="huggies 5 qız ucun n64",
            brand="huggies",
            pack_size="n64",
        )
        db_session.commit()
        matcher.match_products(db_session)
        db_session.refresh(a)
        db_session.refresh(b)
        assert a.canonical_id != b.canonical_id or (
            a.canonical_id is None and b.canonical_id is None
        )


# ── TestHasConflictingVariantTokens ──────────────────────────────────────────


class TestHasConflictingVariantTokens:
    def test_blocks_different_cosmetic_variants(self):
        # splat aktiv vs splat lavandasept — оба имеют уникальный токен >= 4 символов
        assert (
            _has_conflicting_variant_tokens(
                "dis məcunu splat aktiv",
                "dis məcunu splat lavandasept",
            )
            is True
        )

    def test_blocks_different_product_lines(self):
        assert _has_conflicting_variant_tokens("bioderma atoderm", "bioderma sensibio") is True

    def test_allows_same_variant(self):
        assert _has_conflicting_variant_tokens("splat aktiv", "splat aktiv") is False

    def test_allows_one_side_no_unique(self):
        # «nivea deodorant» vs «nivea fresh deodorant» — только у второго уникальный «fresh»
        assert _has_conflicting_variant_tokens("nivea deodorant", "nivea fresh deodorant") is False

    def test_allows_short_unique_tokens_only(self):
        # Токены «b» и «c» — только буквы длиной 1 — не блокируем
        assert _has_conflicting_variant_tokens("vitamin b", "vitamin c") is False

    def test_allows_one_side_has_unique_other_does_not(self):
        # Только у одного имени есть уникальный токен — неполные данные, не блокируем
        # «nivea men deodorant» vs «nivea deodorant» — у B нет уникальных токенов
        assert (
            _has_conflicting_variant_tokens(
                "nivea men deodorant",
                "nivea deodorant",
            )
            is False
        )

    def test_hard_distinct_token_toxumu_blocks_one_sided(self):
        """2026-05-29: «toxumu» (семена) — hard-различитель. «Bağayarpağı»
        (подорожник-лист) ≠ «Bağayarpağı toxumu» (семена) — разные товары,
        блокируем даже без встречного уникального токена (в отличие от
        verbose-суффикса вроде «trihydrate»)."""
        assert _has_conflicting_variant_tokens("bağayarpağı", "bağayarpağı toxumu") is True
        # оба «toxumu» — не блокируем (одинаковый товар)
        assert _has_conflicting_variant_tokens("bağayarpağı toxumu", "bağayarpağı toxumu") is False
        # обычный verbose-суффикс (не hard) по-прежнему разрешён
        assert _has_conflicting_variant_tokens("amoxicillin", "amoxicillin trihydrate") is False

    def test_blocks_short_3char_variant_tokens(self):
        # «bal» (3 символа, мёд) vs «limon» — оба имеют уникальные токены → блокируем
        # Safeguard bal ≠ Safeguard Limon Fresh
        assert (
            _has_conflicting_variant_tokens(
                "safeguard bal",
                "safeguard limon fresh",
            )
            is True
        )

    def test_blocks_short_alphanumeric_codes(self):
        # «b12» (3 символа, буква+цифра) — значащий код витамина → блокируем
        # Venatura Methylfolate ≠ Venatura B12
        assert (
            _has_conflicting_variant_tokens(
                "venatura methylfolate odt",
                "venatura b12",
            )
            is True
        )
        # «d3» (2 символа, буква+цифра) — значащий код витамина → блокируем
        # Makson ≠ Makson D3
        assert _has_conflicting_variant_tokens("makson maxon", "makson d3") is True
        # «2x» (2 символа, буква+цифра) — значащий формульный код → блокируем
        assert (
            _has_conflicting_variant_tokens("amoksiklav 2x", "amoksiklav") is False
        )  # only one side
        assert _has_conflicting_variant_tokens("amoksiklav 2x", "amoksiklav forte") is True

    def test_symmetry(self):
        a = "dis məcunu splat aktiv"
        b = "dis məcunu splat lavandasept"
        assert _has_conflicting_variant_tokens(a, b) == _has_conflicting_variant_tokens(b, a)


class TestIsSignificantVariantToken:
    def test_long_tokens_significant(self):
        assert _is_significant_variant_token("aktiv") is True
        assert _is_significant_variant_token("methylfolate") is True
        assert _is_significant_variant_token("bal") is True  # 3 chars

    def test_short_alpha_only_not_significant(self):
        assert _is_significant_variant_token("b") is False  # 1 char
        assert _is_significant_variant_token("ml") is False  # 2 chars, only alpha
        assert _is_significant_variant_token("sr") is False  # 2 chars, only alpha

    def test_short_digit_only_not_significant(self):
        assert _is_significant_variant_token("50") is False  # only digits
        assert _is_significant_variant_token("10") is False

    def test_short_alphanumeric_significant(self):
        # Код витамина/формулы — буква + цифра
        assert _is_significant_variant_token("b12") is True  # 3 chars, letter+digit
        assert _is_significant_variant_token("d3") is True  # 2 chars, letter+digit
        assert _is_significant_variant_token("2x") is True  # 2 chars, digit+letter
        assert _is_significant_variant_token("c3") is True  # 2 chars

    def test_blocks_in_db_integration(self, db_session):
        """Splat Aktiv и Splat Lavandasept в одном бакете — не должны матчиться."""
        a = _make_product(
            db_session,
            site="pharmonline",
            external_id="sp-1",
            name="Splat Aktiv 75ml",
            name_normalized="dis məcunu splat aktiv",
            brand="splat",
            pack_size="75ml",
        )
        b = _make_product(
            db_session,
            site="aptekonline",
            external_id="sp-2",
            name="Splat Lavandasept 75ml",
            name_normalized="dis məcunu splat lavandasept",
            brand="splat",
            pack_size="75ml",
        )
        db_session.commit()
        matcher.match_products(db_session)
        db_session.refresh(a)
        db_session.refresh(b)
        assert a.canonical_id != b.canonical_id or (
            a.canonical_id is None and b.canonical_id is None
        )


# ── _pack_count ──────────────────────────────────────────────────────────────


class TestPackCount:
    def test_n10(self):
        assert _pack_count("n10") == 10.0

    def test_n1(self):
        assert _pack_count("n1") == 1.0

    def test_empty(self):
        assert _pack_count("") == 1.0

    def test_n30(self):
        assert _pack_count("n30") == 30.0

    def test_ml_volume(self):
        assert _pack_count("50ml") == 50.0


# ── _has_perunit_mismatch ────────────────────────────────────────────────────


class _FakeSnap:
    """Лёгкий мок PriceSnapshot — только поле price."""

    def __init__(self, price: float):
        self.price = price


class _FakeProduct:
    """Лёгкий мок Product — только поля id, site, pack_size."""

    def __init__(self, pid: int, site: str, pack_size: str):
        self.id = pid
        self.site = site
        self.pack_size = pack_size


class TestTwoCharAlphaVariantCodes:
    """2-буквенные all-alpha фармкоды (SK/QK/GK) блокируют ложные матчи."""

    def test_sk_vs_qk_blocked(self):
        # Akriderm SK (məlhəm) ≠ Akriderm QK (krem) — разные формулы
        assert _has_conflicting_variant_tokens("akriderm sk", "akriderm qk") is True

    def test_sk_vs_gk_blocked(self):
        assert _has_conflicting_variant_tokens("akriderm sk", "akriderm gk") is True

    def test_qk_vs_gk_blocked(self):
        assert _has_conflicting_variant_tokens("akriderm qk", "akriderm gk") is True

    def test_same_code_allowed(self):
        # Оба SK — один и тот же вариант → матч разрешён
        assert _has_conflicting_variant_tokens("akriderm sk", "akriderm sk") is False

    def test_only_one_side_has_code_allowed(self):
        # Только у одного сайта есть суффикс — неполные данные, не блокируем
        assert _has_conflicting_variant_tokens("akriderm sk", "akriderm") is False
        assert _has_conflicting_variant_tokens("akriderm", "akriderm qk") is False

    def test_symmetry(self):
        assert _has_conflicting_variant_tokens(
            "akriderm sk", "akriderm qk"
        ) == _has_conflicting_variant_tokens("akriderm qk", "akriderm sk")

    def test_short_alpha_single_char_not_affected(self):
        # Одиночные буквы (b, c) не блокируют — слишком короткие
        assert _has_conflicting_variant_tokens("vitamin b", "vitamin c") is False


class TestPerunitMismatch:
    def _product(self, site: str, pack_size: str, pid: int) -> _FakeProduct:
        return _FakeProduct(pid, site, pack_size)

    def test_blocks_perunit_vs_perpack(self):
        """Thiogamma aloe 8.90/n10 vs pharmonline 89.00/n10 → ratio=10 → BLOCK."""
        aloe = self._product("aloe", "n10", 1)
        pharm = self._product("pharmonline", "n10", 2)
        prices = {1: _FakeSnap(8.9), 2: _FakeSnap(89.0)}
        assert _has_perunit_mismatch([aloe, pharm], prices) is True

    def test_allows_genuine_price_diff(self):
        """Ketotifen aloe 0.95 vs pharmonline 2.29, ratio=2.4 → OK."""
        aloe = self._product("aloe", "n30", 1)
        pharm = self._product("pharmonline", "n30", 2)
        prices = {1: _FakeSnap(0.95), 2: _FakeSnap(2.29)}
        assert _has_perunit_mismatch([aloe, pharm], prices) is False

    def test_allows_same_price(self):
        aloe = self._product("aloe", "n10", 1)
        pharm = self._product("pharmonline", "n10", 2)
        prices = {1: _FakeSnap(30.83), 2: _FakeSnap(30.83)}
        assert _has_perunit_mismatch([aloe, pharm], prices) is False

    def test_no_price_data_no_block(self):
        """Без данных о цене — не блокируем."""
        aloe = self._product("aloe", "n10", 1)
        pharm = self._product("pharmonline", "n10", 2)
        assert _has_perunit_mismatch([aloe, pharm], {}) is False

    def test_blocks_in_db_integration(self, db_session):
        """Интеграционный тест: aloe 8.90 vs pharmonline 89.00 не матчатся."""
        import datetime

        run = storage.Run(tenant_id=1, started_at=datetime.datetime.utcnow(), status="ok")
        db_session.add(run)
        db_session.flush()

        a = _make_product(
            db_session,
            site="aloe",
            external_id="th-aloe",
            name="Thiogamma Turbo 50 ml, 10 əd",
            name_normalized="thiogamma turbo",
            brand="thiogamma",
            pack_size="n10",
        )
        b = _make_product(
            db_session,
            site="pharmonline",
            external_id="th-pharm",
            name="Thiogamma turbo 50 ml N10 (Solution)",
            name_normalized="thiogamma turbo",
            brand="thiogamma",
            pack_size="n10",
        )
        db_session.add(
            storage.PriceSnapshot(
                run_id=run.id,
                product_id=a.id,
                price=8.9,
                is_on_sale=False,
                captured_at=datetime.datetime.utcnow(),
            )
        )
        db_session.add(
            storage.PriceSnapshot(
                run_id=run.id,
                product_id=b.id,
                price=89.0,
                is_on_sale=False,
                captured_at=datetime.datetime.utcnow(),
            )
        )
        db_session.commit()

        matcher.match_products(db_session)
        db_session.refresh(a)
        db_session.refresh(b)
        assert a.canonical_id is None or a.canonical_id != b.canonical_id


class TestSiblingFormCheck:
    """Sibling-form check: если у p нет формы, но на его сайте в том же bucket'е
    уже есть продукт с явной формой q → матч запрещён.

    Реальный кейс: aptk 11201 «Ukraferon 1000000 BV N10» (форма неизвестна) vs
    pharm 4119 «Ukraferon 1000000 IU N10 (Suppositories)» (suppository).
    На aptekonline в том же bucket'е есть aptk 10761 «…(rektal şamlar)»
    → aptk 11201 — не суппозиторий → матч с фарм-суппозиторием ЗАПРЕЩЁН.
    """

    def test_sibling_blocks_nasal_from_matching_suppository(self, db_session):
        """aptk без формы + aptk-sibling suppository → не матчится с pharm suppository."""
        import datetime

        run = storage.Run(tenant_id=1, started_at=datetime.datetime.utcnow(), status="ok")
        db_session.add(run)
        db_session.flush()

        # aptekonline: один суппозиторий с явной формой, один без формы (=другая форма)
        aptk_suppository = _make_product(
            db_session,
            site="aptekonline",
            external_id="ukr-aptk-supp",
            name="Ukraferon 1000000 BV N10 (rektal şamlar)",
            name_normalized="ukraferon",
            brand="ukraferon",
            dosage="1000000bv",
            pack_size="n10",
        )
        aptk_no_form = _make_product(
            db_session,
            site="aptekonline",
            external_id="ukr-aptk-nasal",
            name="Ukraferon 1000000 BV N10",
            name_normalized="ukraferon",
            brand="ukraferon",
            dosage="1000000bv",
            pack_size="n10",
        )
        # pharmonline: суппозиторий
        pharm_suppository = _make_product(
            db_session,
            site="pharmonline",
            external_id="ukr-pharm-supp",
            name="Ukraferon 1000000 IU N10 (Suppositories)",
            name_normalized="ukraferon",
            brand="ukraferon",
            dosage="1000000iu",
            pack_size="n10",
        )
        db_session.commit()

        matcher.match_products(db_session)
        db_session.refresh(aptk_suppository)
        db_session.refresh(aptk_no_form)
        db_session.refresh(pharm_suppository)

        # aptk_suppository должен матчиться с pharm_suppository (оба суппозитории)
        assert aptk_suppository.canonical_id is not None
        assert aptk_suppository.canonical_id == pharm_suppository.canonical_id

        # aptk_no_form НЕ должен матчиться с pharm_suppository
        assert aptk_no_form.canonical_id is None or (
            aptk_no_form.canonical_id != pharm_suppository.canonical_id
        )

    def test_no_block_when_no_sibling(self, db_session):
        """Если на сайте нет sibling с явной формой — матч разрешён (нет данных → не блокируем)."""
        import datetime

        run = storage.Run(tenant_id=1, started_at=datetime.datetime.utcnow(), status="ok")
        db_session.add(run)
        db_session.flush()

        aptk_no_form = _make_product(
            db_session,
            site="aptekonline",
            external_id="dr-aptk-1",
            name="Drotaverinum 40mg N20",
            name_normalized="drotaverinum",
            brand="drotaverinum",
            dosage="40mg",
            pack_size="n20",
        )
        pharm_tablet = _make_product(
            db_session,
            site="pharmonline",
            external_id="dr-pharm-1",
            name="Drotaverinum 40mg N20 (Tablets)",
            name_normalized="drotaverinum",
            brand="drotaverinum",
            dosage="40mg",
            pack_size="n20",
        )
        db_session.commit()

        matcher.match_products(db_session)
        db_session.refresh(aptk_no_form)
        db_session.refresh(pharm_tablet)

        # aptk не имеет sibling с формой tablet → матч РАЗРЕШЁН
        assert aptk_no_form.canonical_id is not None
        assert aptk_no_form.canonical_id == pharm_tablet.canonical_id


# ──────────────────────────────────────────────────────────────────────────────
# Coverage gap closure (2026-05-28): _norm_units, finders, flag_suspected_mismatches
# ──────────────────────────────────────────────────────────────────────────────


def test_norm_units_mq_to_mg():
    """mq (азербайджанский milliqram) → mg."""
    assert matcher._norm_units("500mq") == "500mg"


def test_norm_units_mkg_to_mcg():
    """mkg → mcg."""
    assert matcher._norm_units("100mkg") == "100mcg"


def test_norm_units_q_to_g():
    """цифра+q → цифра+g (gram)."""
    assert matcher._norm_units("5q") == "5g"
    assert matcher._norm_units("0.5q") == "0.5g"


def test_norm_units_iu_aliases():
    """bv/me/ie → iu (international units)."""
    assert matcher._norm_units("500000bv") == "500000iu"
    assert matcher._norm_units("100me") == "100iu"
    assert matcher._norm_units("200ie") == "200iu"


def test_norm_units_does_not_replace_non_unit_chars():
    """`me` без digit-prefix не должен меняться (brand-name 'meridia')."""
    out = matcher._norm_units("meridia")
    assert "iu" not in out  # 'meridia' остаётся как было


def test_pack_count_simple():
    assert matcher._pack_count("n10") == 10.0
    assert matcher._pack_count("n30") == 30.0
    assert matcher._pack_count("") == 1.0
    assert matcher._pack_count("nothing-numeric") == 1.0


def test_pack_count_decimal():
    """Decimal number support: '1.5' → 1.5."""
    assert matcher._pack_count("1.5g") == 1.5


def test_find_matched_groups_returns_only_with_products(db_session):
    """Match без products в кластере не возвращается."""
    m1 = storage.Match(canonical_name="Empty", confidence=1.0)
    m2 = storage.Match(canonical_name="HasProducts", confidence=1.0)
    db_session.add_all([m1, m2])
    db_session.flush()
    p = _make_product(db_session, site="pharmonline", external_id="hp1", name="Has")
    p.canonical_id = m2.id  # _make_product не принимает canonical_id напрямую
    db_session.commit()
    out = matcher.find_matched_groups(db_session)
    names = [g["name"] for g in out]
    assert "HasProducts" in names
    assert "Empty" not in names


def test_find_matched_groups_payload_shape(db_session):
    """Payload содержит canonical_id, name, brand, dosage, pack_size, products, is_manual."""
    m = storage.Match(
        canonical_name="X",
        canonical_brand="X-brand",
        canonical_dosage="500mg",
        canonical_pack_size="n10",
        confidence=1.0,
        is_manual=True,
    )
    db_session.add(m)
    db_session.flush()
    p = _make_product(db_session, site="pharmonline", external_id="x1", name="X")
    p.canonical_id = m.id
    db_session.commit()
    out = matcher.find_matched_groups(db_session)
    g = out[0]
    assert g["canonical_id"] == m.id
    assert g["name"] == "X"
    assert g["brand"] == "X-brand"
    assert g["dosage"] == "500mg"
    assert g["pack_size"] == "n10"
    assert len(g["products"]) == 1
    assert g["is_manual"] is True


def test_find_unmatched_groups_by_site(db_session):
    """Продукты без canonical_id группируются по сайту."""
    _make_product(db_session, site="pharmonline", external_id="u1", name="A")
    _make_product(db_session, site="pharmonline", external_id="u2", name="B")
    _make_product(db_session, site="aloe", external_id="u3", name="C")
    # Matched product — не возвращается
    m = storage.Match(canonical_name="M", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    p = _make_product(db_session, site="aloe", external_id="u4", name="D")
    p.canonical_id = m.id
    db_session.commit()
    by_site = matcher.find_unmatched(db_session)
    assert len(by_site["pharmonline"]) == 2
    assert len(by_site["aloe"]) == 1  # только C, D исключён (matched)


def test_normalize_for_matching_returns_string():
    """`normalize_for_matching` — backwards-compat helper."""
    out = matcher.normalize_for_matching("Some Product 500mg N10")
    assert isinstance(out, str)
    assert len(out) > 0


# === flag_suspected_mismatches ============================================


def _add_match_with_prices(db_session, *, name, prices_by_site, is_manual=False):
    """Хелпер: создаёт Match + Products + PriceSnapshots."""
    import datetime

    m = storage.Match(
        canonical_name=name,
        confidence=1.0,
        is_manual=is_manual,
    )
    db_session.add(m)
    db_session.flush()
    run = storage.Run(started_at=datetime.datetime.utcnow(), status="ok")
    db_session.add(run)
    db_session.flush()
    for site, price in prices_by_site.items():
        p = _make_product(
            db_session,
            site=site,
            external_id=f"{site}-{name}",
            name=name,
        )
        p.canonical_id = m.id
        db_session.add(storage.PriceSnapshot(run_id=run.id, product_id=p.id, price=price))
    db_session.commit()
    return m


def test_flag_suspected_mismatches_flags_spread_above_50pct(db_session):
    """Match с разлётом цен ≥50% → needs_review=True."""
    m = _add_match_with_prices(
        db_session,
        name="Big spread",
        prices_by_site={"pharmonline": 10.0, "aloe": 20.0},  # 2× spread
    )
    changed = matcher.flag_suspected_mismatches(db_session)
    assert changed == 1
    db_session.refresh(m)
    assert m.needs_review is True


def test_flag_suspected_mismatches_keeps_close_prices(db_session):
    """Match с близкими ценами → needs_review=False."""
    m = _add_match_with_prices(
        db_session,
        name="Close",
        prices_by_site={"pharmonline": 10.0, "aloe": 11.0},  # 10% spread
    )
    matcher.flag_suspected_mismatches(db_session)
    db_session.refresh(m)
    assert m.needs_review is False


def test_flag_suspected_mismatches_skips_manual(db_session):
    """is_manual=True match'и не флагируются — даже с большим spread."""
    m = _add_match_with_prices(
        db_session,
        name="Manual spread",
        is_manual=True,
        prices_by_site={"pharmonline": 10.0, "aloe": 100.0},  # 10× spread!
    )
    matcher.flag_suspected_mismatches(db_session)
    db_session.refresh(m)
    assert m.needs_review is False


def test_flag_suspected_mismatches_unflags_when_prices_align(db_session):
    """Existing needs_review=True flag сбрасывается если spread упал ниже порога."""
    m = _add_match_with_prices(
        db_session,
        name="Was spread",
        prices_by_site={"pharmonline": 10.0, "aloe": 11.0},  # уже close
    )
    m.needs_review = True  # был flagged, симулируем
    db_session.commit()
    changed = matcher.flag_suspected_mismatches(db_session)
    assert changed == 1
    db_session.refresh(m)
    assert m.needs_review is False


def test_flag_suspected_mismatches_zero_changes_returns_zero(db_session):
    """Если ни один флаг не изменился → 0 (no commit overhead)."""
    _add_match_with_prices(
        db_session,
        name="Already stable",
        prices_by_site={"pharmonline": 10.0, "aloe": 10.5},  # close
    )
    # Первый вызов установит false (или уже false) — второй меняет 0
    matcher.flag_suspected_mismatches(db_session)
    second = matcher.flag_suspected_mismatches(db_session)
    assert second == 0


def test_flag_suspected_mismatches_skips_singleton_clusters(db_session):
    """Match с одним продуктом (< 2) — пропускается."""
    m = _add_match_with_prices(
        db_session,
        name="Solo",
        prices_by_site={"pharmonline": 10.0},  # single site
    )
    matcher.flag_suspected_mismatches(db_session)
    db_session.refresh(m)
    assert m.needs_review is False


def test_flag_suspected_mismatches_empty_db_returns_zero(db_session):
    """Нет matches → 0, не падает."""
    assert matcher.flag_suspected_mismatches(db_session) == 0


# ── Variant-atoms (2026-05-29): буква/серийная цифра из RAW ───────────────────
# Валидировано на прод-дампе (57142 товара, scripts/scan_variant_conflicts.py):
# ловит 7 настоящих wrong-match (Lorinden C/A, Vitamin A/C, ASferon C/S,
# Solgar C/E, Normoqlip M/2, Güzgü M/S), 0 ложных. Сводит общий 2+ счёт 4876→4873.


class TestVariantAtoms:
    def test_extracts_single_variant_letter(self):
        assert _variant_atoms("Lorinden C məlhəm 15 q") == frozenset({"c"})
        assert _variant_atoms("Vitamin A № 10") == frozenset({"a"})
        assert _variant_atoms("Normoqlip M № 30 (Tabletlər)") == frozenset({"m"})

    def test_extracts_series_digit(self):
        assert _variant_atoms("Normoqlip 2  N30") == frozenset({"2"})
        assert _variant_atoms("Nutrilon 1") == frozenset({"1"})

    def test_excludes_unit_letters_q_g_l(self):
        # q=грамм(AZ), g=грамм, l=литр — единицы, НЕ варианты
        assert _variant_atoms("Heparin 25 q") == frozenset()
        assert _variant_atoms("Heparin 25 g") == frozenset()

    def test_excludes_age_weight_volume_pack(self):
        assert _variant_atoms("Gerber sıyıq 6 aylıq alma 180 q") == frozenset()
        assert _variant_atoms("Mikrazim 10000 N20") == frozenset()  # 10000 = 2+-значное
        # range «2-5» убран, pack «N94» убран, серийная «1» сохранена
        assert _variant_atoms("Pampers 1 New Baby 2-5 kq N94") == frozenset({"1"})

    def test_excludes_multidigit_strength(self):
        # «25000 ED» → unit-strip; «50 000» split-число → 2+-значные не атомы
        assert _variant_atoms("Mikrazim 25000 ED № 20") == frozenset()
        # D-3 → буква d + серийная 3 (на ОБОИХ сайтах одинаково → не конфликт)
        assert _variant_atoms("D-3 Ferol 50 000 BV 15 ml") == frozenset({"d", "3"})

    def test_extracts_single_digit_pharma_mass(self):
        # «2 mq» (Normoqlip 2mg глимепирид) → атом {2}; дробные/многозначные — нет
        assert _variant_atoms("Normoqlip 2 mq № 30") == frozenset({"2"})
        assert _variant_atoms("Concor 5 mg N30") == frozenset({"5"})
        assert _variant_atoms("Amlodipin 2.5 mg N30") == frozenset()  # дробное → не атом
        assert _variant_atoms("Paracetamol 500 mg N20") == frozenset()  # многозначное


class TestHasConflictingVariantAtoms:
    def test_blocks_letter_variants(self):
        assert _has_conflicting_variant_atoms("Lorinden C məlhəm 15 q", "Lorinden A 15 qr") is True
        assert _has_conflicting_variant_atoms("Vitamin A № 10", "Vitamin C 100 mq N10") is True
        assert _has_conflicting_variant_atoms("ASferon C  N30", "Asferon S № 30") is True

    def test_blocks_cross_type_letter_vs_digit(self):
        # Normoqlip M (буква) ≠ Normoqlip 2 (цифра) — кросс-тип, главный кейс
        assert _has_conflicting_variant_atoms("Normoqlip M № 30", "Normoqlip 2  N30") is True

    def test_blocks_size_variant(self):
        assert _has_conflicting_variant_atoms("güzgü spekulum ölçüsü M", "Güzgü (S)") is True

    def test_blocks_single_digit_dosage_variant(self):
        # Normoqlip 2 mq (2mg, юнит у pharm) ≠ Normoqlip 4 N30 (голая 4 у aptek):
        # single-mass атом «2» против серийной «4» → кросс-форматный конфликт дозы.
        assert _has_conflicting_variant_atoms("Normoqlip 2 mq № 30", "Normoqlip 4  N30") is True
        # та же доза в разном формате → НЕ блок
        assert _has_conflicting_variant_atoms("Normoqlip 2 mq № 30", "Normoqlip 2  N30") is False

    def test_allows_same_atoms(self):
        assert _has_conflicting_variant_atoms("Nutrilon 1", "Nutrilon 1") is False
        assert _has_conflicting_variant_atoms("Heparin 25 g", "Heparin 25 q") is False

    def test_allows_one_sided_atom_incomplete_data(self):
        # «Vitamin C 1» vs «Vitamin C» — superset, неполные данные, НЕ блокируем
        assert _has_conflicting_variant_atoms("Vitamin C 1", "Vitamin C") is False

    def test_allows_no_atoms(self):
        assert (
            _has_conflicting_variant_atoms("Paracetamol 500 mg N20", "Paracetamol 500 mq N10")
            is False
        )


# ── Strength-number conflict (Mikrazim 25000 ED ≠ 10000) ─────────────────────


class TestStrengthNumber:
    def test_blocks_different_enzyme_strength(self):
        assert (
            _has_conflicting_strength_number(
                "Mikrazim 25000 ED № 20 (Kapsulalar)", "Mikrazim  10000  N20"
            )
            is True
        )

    def test_allows_same_strength_diff_notation(self):
        # 50000 IU == 50 000 BV (thousand-space normalize; обе = единицы)
        assert (
            _has_conflicting_strength_number("D3 Ferol 50000 IU 15 ml", "D3 Ferol 50 000 BV 15 ml")
            is False
        )

    def test_ed_enzyme_units_not_eaten_as_pack(self):
        # «25000 ED» (enzyme) НЕ стрипается как «əd»=штук → сила извлекается
        assert _strength_numbers("Mikrazim 25000 ED № 20") == frozenset({"25000"})

    def test_ignores_sub_1000_volume_dose(self):
        assert _strength_numbers("Çaytikanı yağı 100 ml") == frozenset()  # 100 < 1000
        assert _strength_numbers("Aspirin 500 mg") == frozenset()  # 500 < 1000

    def test_one_sided_strength_not_blocked(self):
        # одна сторона со силой, другая без → неполные данные, не блокируем
        assert _has_conflicting_strength_number("Mikrazim 25000 ED", "Mikrazim") is False


# ── Ambiguity-suppression: генерик ↔ несколько брендов (2026-05-29) ───────────


def test_ambiguous_generic_suppressed(db_session):
    """Генерик («Çaytikanı yağı»), матчащийся к ≥2 РАЗНЫМ брендам (Altay,
    Mirrolla) — неоднозначен → не матчим (какой «тот же товар» неизвестно)."""
    s = db_session
    g = _make_product(
        s,
        site="aptekonline",
        external_id="cg",
        name="Çaytikanı yağı 100 ml",
        name_normalized="caytikani yagi",
        brand="caytikani",
        pack_size="100ml",
    )
    _make_product(
        s,
        site="pharmonline",
        external_id="ca",
        name="Çaytikanı yağı Altay 100 ml",
        name_normalized="caytikani yagi altay",
        brand="caytikani",
        pack_size="100ml",
    )
    _make_product(
        s,
        site="pharmonline",
        external_id="cm",
        name="Çaytikanı yağı Mirrolla 100 ml",
        name_normalized="caytikani yagi mirrolla",
        brand="caytikani",
        pack_size="100ml",
    )
    s.commit()
    matcher.match_products(s)
    s.refresh(g)
    assert g.canonical_id is None  # неоднозначен → подавлен


def test_single_brand_generic_still_matches(db_session):
    """Генерик + ОДИН бренд (один уникальный токен у всех кандидатов) → НЕ
    ambiguous → матчится (легит verbose-vs-terse сохранён)."""
    s = db_session
    g = _make_product(
        s,
        site="aptekonline",
        external_id="bg",
        name="Biyan 100 ml",
        name_normalized="biyan",
        brand="biyan",
        pack_size="100ml",
    )
    _make_product(
        s,
        site="pharmonline",
        external_id="ba",
        name="Biyan Altay 100 ml",
        name_normalized="biyan altay",
        brand="biyan",
        pack_size="100ml",
    )
    _make_product(
        s,
        site="aloe",
        external_id="ba2",
        name="Biyan Altay 100 ml",
        name_normalized="biyan altay",
        brand="biyan",
        pack_size="100ml",
    )
    s.commit()
    matcher.match_products(s)
    s.refresh(g)
    assert g.canonical_id is not None  # один бренд → не ambiguous → матч


def test_generic_with_exact_twin_not_suppressed(db_session):
    """Генерик с ТОЧНЫМ двойником (равный набор значащих токенов) на другом сайте
    НЕ подавляется, даже если рядом brand-сиблинги (Codex HIGH fix): у него есть
    определённый матч → cross-brand-неоднозначность к нему не относится."""
    s = db_session
    g1 = _make_product(
        s,
        site="aptekonline",
        external_id="cg1",
        name="Çaytikanı yağı 100 ml",
        name_normalized="caytikani yagi",
        brand="caytikani",
        pack_size="100ml",
    )
    g2 = _make_product(
        s,
        site="pharmonline",
        external_id="cg2",
        name="Çaytikanı yağı 100 ml",
        name_normalized="caytikani yagi",
        brand="caytikani",
        pack_size="100ml",
    )
    _make_product(
        s,
        site="aloe",
        external_id="cma",
        name="Çaytikanı yağı Mirrolla 100 ml",
        name_normalized="caytikani yagi mirrolla",
        brand="caytikani",
        pack_size="100ml",
    )
    s.commit()
    matcher.match_products(s)
    s.refresh(g1)
    s.refresh(g2)
    assert g1.canonical_id is not None
    assert g1.canonical_id == g2.canonical_id  # два генерика склеились, не подавлены
