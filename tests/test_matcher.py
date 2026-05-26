"""Тесты fuzzy-матчинга товаров между сайтами."""

import pytest

from src import match_actions, matcher, storage
from src.matcher import (
    _has_conflicting_form,
    _has_conflicting_gender,
    _has_conflicting_modifier,
    _has_conflicting_series_number,
    _has_conflicting_variant_tokens,
    _has_extreme_length_disparity,
    _has_perunit_mismatch,
    _is_significant_variant_token,
    _pack_count,
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
            db_session, site="pharmonline", external_id="nl-1",
            name="Nutrilon 1 600g", name_normalized="nutrilon 1",
            brand="nutrilon", pack_size="600g",
        )
        b = _make_product(
            db_session, site="aptekonline", external_id="nl-4",
            name="Nutrilon 4 600g", name_normalized="nutrilon 4",
            brand="nutrilon", pack_size="600g",
        )
        db_session.commit()
        matcher.match_products(db_session)
        db_session.refresh(a); db_session.refresh(b)
        assert a.canonical_id != b.canonical_id or (
            a.canonical_id is None and b.canonical_id is None
        )


class TestHasConflictingForm:
    def test_blocks_drops_vs_spray(self):
        assert _has_conflicting_form("Otrivin drops 0.1%", "Otrivin spray 0.1%") is True

    def test_blocks_cream_vs_ointment(self):
        assert _has_conflicting_form("Bepanten krem 30g", "Bepanten məlhəm 30g") is True

    def test_allows_same_form(self):
        assert _has_conflicting_form("Paracetamol tablet 500mg", "Paracetamol tab 500mg N20") is False

    def test_allows_if_form_unknown(self):
        # Форма не распознана → не блокируем
        assert _has_conflicting_form("Lopril 10mg N20", "Lopril 10mg N30") is False

    def test_allows_if_one_form_unknown(self):
        assert _has_conflicting_form("Vitamin C", "Vitamin C tablet 500mg") is False


class TestHasExtremeLengthDisparity:
    def test_blocks_stub_vs_full(self):
        assert _has_extreme_length_disparity("venatura", "venatura vitamin a palmitate retinol zinc") is True
        assert _has_extreme_length_disparity("bioderma", "bioderma atoderm intensive baume 200ml") is True

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
    p1 = _make_product(db_session, site="aptekonline", external_id="a1",
                       name="Nivea deodorant 150ml",
                       name_normalized="nivea deodorant",
                       brand="nivea", dosage="150ml", pack_size="150ml")
    p3 = _make_product(db_session, site="pharmonline", external_id="p1",
                       name="Nivea fresh deodorant 150ml",
                       name_normalized="nivea fresh deodorant",
                       brand="nivea", dosage="150ml", pack_size="150ml")
    matcher.match_products(db_session)
    db_session.refresh(p1)
    db_session.refresh(p3)
    first_cid = p1.canonical_id
    assert first_cid is not None, "первый прогон должен создать кластер"
    assert p3.canonical_id == first_cid

    # Второй прогон: добавляем p2(aptk) — тот же сайт что p1, но другой вариант.
    # Используем одинаковые имена чтобы p2+p4 гарантированно матчились между собой
    # (тест проверяет snowball-guard, не variant-guard).
    p2 = _make_product(db_session, site="aptekonline", external_id="a2",
                       name="Nivea sport deodorant 150ml",
                       name_normalized="nivea sport deodorant",
                       brand="nivea", dosage="150ml", pack_size="150ml")
    p4 = _make_product(db_session, site="pharmonline", external_id="p2",
                       name="Nivea sport deodorant 150ml",
                       name_normalized="nivea sport deodorant",
                       brand="nivea", dosage="150ml", pack_size="150ml")
    db_session.commit()

    matcher.match_products(db_session)
    db_session.refresh(p1)
    db_session.refresh(p2)
    db_session.refresh(p3)
    db_session.refresh(p4)

    # Старый кластер не должен был вырасти до 4 продуктов
    assert p2.canonical_id != first_cid, \
        "второй aptekonline-продукт НЕ должен попасть в кластер где aptekonline уже занят"
    assert p4.canonical_id != first_cid, \
        "второй pharmonline-продукт НЕ должен попасть в кластер где pharmonline уже занят"
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
        ("aloe",        "l1", "testbrand deodorant"),
    ]
    products = []
    for site, ext_id, name in variants:
        p = _make_product(db_session, site=site, external_id=ext_id,
                          name=name, name_normalized=name,
                          brand="testbrand", dosage="150ml", pack_size="150ml")
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
        assert len(sites) == len(set(sites)), \
            f"Кластер {cid} содержит дубли сайтов: {sites}"


# ── TestHasConflictingGender ──────────────────────────────────────────────────

class TestHasConflictingGender:
    def test_blocks_male_vs_female_az(self):
        # "oglanlar" (после strip breve: oğlanlar→oglanlar) vs "qız" (dotless-i)
        assert _has_conflicting_gender(
            "usaq bezi huggies 5 oglanlar ucun",
            "huggies 5 ultra qız ucun",
        ) is True

    def test_blocks_female_vs_male_order(self):
        # Порядок аргументов не важен
        assert _has_conflicting_gender(
            "huggies qızlar n64",
            "huggies oglanlar n64",
        ) is True

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
            db_session, site="pharmonline", external_id="hg-m",
            name="Huggies oğlanlar N64",
            name_normalized="usaq bezi huggies 5 oglanlar ucun",
            brand="huggies", pack_size="n64",
        )
        b = _make_product(
            db_session, site="aptekonline", external_id="hg-f",
            name="Huggies qızlar N64",
            name_normalized="huggies 5 qız ucun n64",
            brand="huggies", pack_size="n64",
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
        assert _has_conflicting_variant_tokens(
            "dis məcunu splat aktiv",
            "dis məcunu splat lavandasept",
        ) is True

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
        assert _has_conflicting_variant_tokens(
            "nivea men deodorant",
            "nivea deodorant",
        ) is False

    def test_blocks_short_3char_variant_tokens(self):
        # «bal» (3 символа, мёд) vs «limon» — оба имеют уникальные токены → блокируем
        # Safeguard bal ≠ Safeguard Limon Fresh
        assert _has_conflicting_variant_tokens(
            "safeguard bal",
            "safeguard limon fresh",
        ) is True

    def test_blocks_short_alphanumeric_codes(self):
        # «b12» (3 символа, буква+цифра) — значащий код витамина → блокируем
        # Venatura Methylfolate ≠ Venatura B12
        assert _has_conflicting_variant_tokens(
            "venatura methylfolate odt",
            "venatura b12",
        ) is True
        # «d3» (2 символа, буква+цифра) — значащий код витамина → блокируем
        # Makson ≠ Makson D3
        assert _has_conflicting_variant_tokens("makson maxon", "makson d3") is True
        # «2x» (2 символа, буква+цифра) — значащий формульный код → блокируем
        assert _has_conflicting_variant_tokens("amoksiklav 2x", "amoksiklav") is False  # only one side
        assert _has_conflicting_variant_tokens("amoksiklav 2x", "amoksiklav forte") is True

    def test_symmetry(self):
        a = "dis məcunu splat aktiv"
        b = "dis məcunu splat lavandasept"
        assert _has_conflicting_variant_tokens(a, b) == _has_conflicting_variant_tokens(b, a)


class TestIsSignificantVariantToken:
    def test_long_tokens_significant(self):
        assert _is_significant_variant_token("aktiv") is True
        assert _is_significant_variant_token("methylfolate") is True
        assert _is_significant_variant_token("bal") is True   # 3 chars

    def test_short_alpha_only_not_significant(self):
        assert _is_significant_variant_token("b") is False    # 1 char
        assert _is_significant_variant_token("ml") is False   # 2 chars, only alpha
        assert _is_significant_variant_token("sr") is False   # 2 chars, only alpha

    def test_short_digit_only_not_significant(self):
        assert _is_significant_variant_token("50") is False   # only digits
        assert _is_significant_variant_token("10") is False

    def test_short_alphanumeric_significant(self):
        # Код витамина/формулы — буква + цифра
        assert _is_significant_variant_token("b12") is True   # 3 chars, letter+digit
        assert _is_significant_variant_token("d3") is True    # 2 chars, letter+digit
        assert _is_significant_variant_token("2x") is True    # 2 chars, digit+letter
        assert _is_significant_variant_token("c3") is True    # 2 chars

    def test_blocks_in_db_integration(self, db_session):
        """Splat Aktiv и Splat Lavandasept в одном бакете — не должны матчиться."""
        a = _make_product(
            db_session, site="pharmonline", external_id="sp-1",
            name="Splat Aktiv 75ml",
            name_normalized="dis məcunu splat aktiv",
            brand="splat", pack_size="75ml",
        )
        b = _make_product(
            db_session, site="aptekonline", external_id="sp-2",
            name="Splat Lavandasept 75ml",
            name_normalized="dis məcunu splat lavandasept",
            brand="splat", pack_size="75ml",
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
        run = storage.Run(
            tenant_id=1, started_at=datetime.datetime.utcnow(), status="ok"
        )
        db_session.add(run)
        db_session.flush()

        a = _make_product(
            db_session, site="aloe", external_id="th-aloe",
            name="Thiogamma Turbo 50 ml, 10 əd",
            name_normalized="thiogamma turbo",
            brand="thiogamma", pack_size="n10",
        )
        b = _make_product(
            db_session, site="pharmonline", external_id="th-pharm",
            name="Thiogamma turbo 50 ml N10 (Solution)",
            name_normalized="thiogamma turbo",
            brand="thiogamma", pack_size="n10",
        )
        db_session.add(storage.PriceSnapshot(
            run_id=run.id, product_id=a.id,
            price=8.9, is_on_sale=False,
            captured_at=datetime.datetime.utcnow(),
        ))
        db_session.add(storage.PriceSnapshot(
            run_id=run.id, product_id=b.id,
            price=89.0, is_on_sale=False,
            captured_at=datetime.datetime.utcnow(),
        ))
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
        run = storage.Run(
            tenant_id=1, started_at=datetime.datetime.utcnow(), status="ok"
        )
        db_session.add(run)
        db_session.flush()

        # aptekonline: один суппозиторий с явной формой, один без формы (=другая форма)
        aptk_suppository = _make_product(
            db_session, site="aptekonline", external_id="ukr-aptk-supp",
            name="Ukraferon 1000000 BV N10 (rektal şamlar)",
            name_normalized="ukraferon",
            brand="ukraferon", dosage="1000000bv", pack_size="n10",
        )
        aptk_no_form = _make_product(
            db_session, site="aptekonline", external_id="ukr-aptk-nasal",
            name="Ukraferon 1000000 BV N10",
            name_normalized="ukraferon",
            brand="ukraferon", dosage="1000000bv", pack_size="n10",
        )
        # pharmonline: суппозиторий
        pharm_suppository = _make_product(
            db_session, site="pharmonline", external_id="ukr-pharm-supp",
            name="Ukraferon 1000000 IU N10 (Suppositories)",
            name_normalized="ukraferon",
            brand="ukraferon", dosage="1000000iu", pack_size="n10",
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
        run = storage.Run(
            tenant_id=1, started_at=datetime.datetime.utcnow(), status="ok"
        )
        db_session.add(run)
        db_session.flush()

        aptk_no_form = _make_product(
            db_session, site="aptekonline", external_id="dr-aptk-1",
            name="Drotaverinum 40mg N20",
            name_normalized="drotaverinum",
            brand="drotaverinum", dosage="40mg", pack_size="n20",
        )
        pharm_tablet = _make_product(
            db_session, site="pharmonline", external_id="dr-pharm-1",
            name="Drotaverinum 40mg N20 (Tablets)",
            name_normalized="drotaverinum",
            brand="drotaverinum", dosage="40mg", pack_size="n20",
        )
        db_session.commit()

        matcher.match_products(db_session)
        db_session.refresh(aptk_no_form)
        db_session.refresh(pharm_tablet)

        # aptk не имеет sibling с формой tablet → матч РАЗРЕШЁН
        assert aptk_no_form.canonical_id is not None
        assert aptk_no_form.canonical_id == pharm_tablet.canonical_id
