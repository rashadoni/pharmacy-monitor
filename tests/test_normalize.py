"""Тесты нормализации имён, парсинга цены и извлечения дозировки/упаковки."""

import pytest

from src.normalize import (
    expand_dose_lists,
    extract_dosage,
    extract_form,
    extract_pack_size,
    extract_total_volume,
    fold_spelling,
    normalize_name,
    normalize_numbers,
    pack_unit_count,
    parse_price,
    strip_accents,
)


def test_strip_accents_decomposable_diacritics():
    """strip_accents: NFKD для декомпозируемых + явная AZ-карта для ə/ı.

    Fix 2026-05-29: ранее `ə` (U+0259 schwa) и `ı` (U+0131 dotless i) НЕ
    декомпозировались NFKD и оставались в выводе → «Şəkər»→«Səkər», ломая
    cross-site матч между сайтом с «ə» и сайтом с «e». Теперь явная карта
    переводит ə→e, ı→i (регистр сохраняется).
    """
    assert strip_accents("üçün") == "ucun"
    assert strip_accents("Şəkər") == "Seker"  # Ş→S, ə→e, к→k
    assert strip_accents("şərbət") == "serbet"  # ə→e полностью
    assert strip_accents("Günəş") == "Gunes"  # ü→u, ə→e, ş→s, регистр сохранён
    assert strip_accents("Ağız") == "Agiz"  # ğ→g, ı→i


def test_normalize_name_strips_dosage_and_pack():
    raw = "Paracetamol 500mg N20 tablet"
    assert "paracetamol" in normalize_name(raw)
    assert "500mg" not in normalize_name(raw)
    assert "n20" not in normalize_name(raw)


def test_normalize_name_idempotent():
    n1 = normalize_name("Aspirin Cardio 100 mg №30")
    n2 = normalize_name(n1)
    assert n1 == n2 or n2 in n1


def test_extract_dosage_simple():
    assert extract_dosage("Paracetamol 500mg") == "500mg"
    assert extract_dosage("Vitamin D 2000 IU") == "2000iu"
    assert extract_dosage("Solution 10ml") == "10ml"


def test_extract_dosage_compound():
    assert extract_dosage("Amoxicillin 250mg/5ml syrup") == "250mg/5ml"


def test_extract_dosage_returns_none():
    assert extract_dosage("Just a name") is None
    assert extract_dosage("") is None


def test_extract_pack_size():
    assert extract_pack_size("Paracetamol 500mg 30 tab") in ("30tab", "n30")
    assert extract_pack_size("Bottle 100ml") == "100ml"


def test_extract_pack_size_english_no_variant():
    """Регрессия (2026-05-29): английское «No. 28» (импортные бренды из
    India/Turkey/Hungary на pharmonline) раньше давало pack_size=None →
    матчер бакетил их с другими фасовками. Теперь ловится как n28.
    """
    assert extract_pack_size("Esom 40 mg No. 28 (Capsules) (Turkey)") == "n28"
    assert extract_pack_size("Evinol 400 mg No.30 (Capsules)") == "n30"
    assert extract_pack_size("Baralgin max 500 mg No 100 (Tablets)") == "n100"
    # bare N и № по-прежнему работают
    assert extract_pack_size("Aspirin N20") == "n20"
    assert extract_pack_size("Drug № 28") == "n28"
    # бренды с 'No' но без count-цифры → не ложный count (volume fallback / None)
    assert extract_pack_size("No-Spa forte 40mg 50ml") == "50ml"


def test_extract_pack_size_prefers_count_over_weight():
    """Регрессия false-match (2026-05-11): для diapers «Huggies-4 8-14kg N66»
    раньше возвращалось '14kg' (вес ребёнка), теперь должно вернуть n66
    (=кол-во в упаковке). Это разделяет разные упаковки одного бренда
    в matcher.py bucket_key.
    """
    # Diaper kg означает ВЕС РЕБЁНКА, не размер упаковки. N66 — реальный pack.
    assert extract_pack_size("Diapers Huggies - 4 8-14kg Elit Soft №54") == "n54"
    assert extract_pack_size("Huggies-4 Elit Soft uşaq bezi 8-14 kq N66") == "n66"
    assert extract_pack_size("Huggies-3 Elite Soft 5-9 kq 21 əd") == "n21"
    assert extract_pack_size("Huggies-5 Mega Elit Soft 12-22 kq 42 əd") == "n42"


def test_extract_pack_size_volume_fallback():
    """Если pack-count отсутствует — возвращаем volume/weight как раньше."""
    assert extract_pack_size("Nestle baby food 200gr") == "200g"
    assert extract_pack_size("Friso Gold 1 800g") == "800g"
    assert extract_pack_size("Bottle 100ml") == "100ml"
    assert extract_pack_size("just random text") is None


def test_parse_price_simple():
    assert parse_price("24.50 AZN") == 24.50
    assert parse_price("24,50 ₼") == 24.50
    assert parse_price("12") == 12.0


def test_parse_price_thousand_separator():
    assert parse_price("1.234,56") == 1234.56
    assert parse_price("1,234.56") == 1234.56


def test_parse_price_invalid():
    assert parse_price(None) is None
    assert parse_price("") is None
    assert parse_price("foo") is None


def test_parse_price_multi_price_contamination():
    """Bug: aptekonline's Angular template renders <del>22 AZN</del>317 AZN
    when there's a discount. inner_text concatenates the two prices without
    separator -> "22 AZN317 AZN". parse_price must return only the first
    token (22), never the joined string (22317)."""
    # Discounted product, original price was integer
    assert parse_price("22 AZN317 AZN") == 22.0
    # Discounted product, original had a decimal
    assert parse_price("22.31 AZN17 AZN") == 22.31
    # Newline between the prices (browser inner_text variant)
    assert parse_price("22.31 AZN\n17 AZN") == 22.31
    # Common HTML inner_text shape: "<del>5.28 AZN</del> 4.62 AZN"
    assert parse_price("5.28 AZN 4.62 AZN") == 5.28


# ── pack_unit_count (per-unit price comparison, 2026-05-29) ──────────────────


@pytest.mark.parametrize(
    "pack_size,name,expected",
    [
        ("n10", None, (10, "high")),
        ("N50", None, (50, "high")),
        (None, "Tibbi maska N50", (50, "high")),
        (None, "Maska No 10", (10, "high")),
        ("n10x2", None, (20, "high")),  # multiplier
        ("10 ədəd", None, (10, "high")),
        ("10 шт", None, (10, "high")),
        # ── COUNT не путается с объёмом/весом/дозой ──
        ("50ml", None, (1, "low")),
        ("300q", None, (1, "low")),  # 300 грамм по-азербайджански
        ("500mg", None, (1, "low")),
        ("200 ml", None, (1, "low")),
        (None, "Пластырь 10x20 sm", (1, "low")),
        (None, "Bandage 10×20 cm", (1, "low")),
        (None, "Sirop 100 ml", (1, "low")),
        # ── одиночный товар без маркера ──
        (None, "Thiogamma turbo 50 ml", (1, "low")),
        (None, None, (1, "low")),
        ("", "", (1, "low")),
        # ── N-count + объём в названии: count берётся, ml игнорируется ──
        ("n20", "Drug 0.9% 200 ml", (20, "high")),
    ],
)
def test_pack_unit_count(pack_size, name, expected):
    assert pack_unit_count(pack_size, name) == expected


def test_pack_unit_count_pack_size_priority_over_name():
    """pack_size проверяется первым; если там count — его и берём."""
    assert pack_unit_count("n30", "Aspirin N10 (старое имя)") == (30, "high")


# ── «X ağacı» = растение, не категория/форма (2026-05-29) ─────────────────────


def test_tree_plant_bigram_not_stripped():
    """«Çay ağacı» (чайное дерево) и «Şam ağacı» (сосна) НЕ схлопываются в
    «agaci» — иначе ложный матч чайное-дерево ↔ сосна (разные растения)."""
    cay = normalize_name("Çay ağacı yağı 20 ml")
    sam = normalize_name("Şam ağacı yağı 20 ml")
    assert "cayagaci" in cay
    assert "samagaci" in sam
    assert cay != sam  # различимы → не матчатся


def test_tea_tree_distinct_from_herbal_tea_category():
    # «Çay ağacı» (дерево) сохраняет çay; обычный «çay»-префикс (категория) — нет
    assert "cayagaci" in normalize_name("Çay ağacı efir yağı 10 ml")


# ── yağı (масло) = форма «oil», oil ≠ solution (2026-05-29) ───────────────────


def test_oil_form_distinct_from_solution():
    assert extract_form("Qliserin yağı 50 ml") == "oil"
    assert extract_form("Qliserin 50 ml (Məhlul)") == "solution"


def test_oil_oil_same_form():
    # масло ↔ масло (Çaytikanı обоих сайтов) остаётся одной формой → матч сохраняется
    assert extract_form("Çaytikanı yağı 100 ml") == "oil"
    assert extract_form("Lavanda yağı 10 ml") == "oil"


def test_extract_pack_size_concentration_shadow():
    """workflow audit 2026-05-31: the '5ml' in '200mq/5ml' is concentration, not the
    bottle volume — must not shadow the trailing total volume (15ml/30ml)."""
    from src.normalize import extract_pack_size, extract_total_volume

    assert extract_pack_size("Azoksin 200mq/5ml 15ml") == "15ml"
    assert extract_total_volume("Azoksin 200mq/5ml 15ml") == "15ml"
    assert extract_total_volume("Azoksin 200mq/5ml 30ml") == "30ml"
    assert extract_total_volume("Azoksin 200mq/5ml") is None
    assert extract_total_volume("Lavanda yağı 100 ml") == "100ml"


# ── Запись чисел, доз и пояснений (2026-10-07) ───────────────────────────────
# Один товар сайты записывают по-разному; до этих правок такие товары либо
# расходились по разным бакетам матчера, либо выглядели как разные варианты.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Xalatan 0,005% 2,5 ml", "Xalatan 0.005% 2.5 ml"),  # дробь через запятую
        ("Depovit D3 50.000 TV 15 ml", "Depovit D3 50000 TV 15 ml"),  # тысячи
        ("Desol D3+ 50,000 5 ml", "Desol D3+ 50000 5 ml"),
        ("Viferon - 2.500.000 IU 10 əd.", "Viferon - 2500000 IU 10 əd."),
        ("Mezim forte 3.500 ED N20", "Mezim forte 3500 ED N20"),
        ("Sinaflan 0,025% 15 q", "Sinaflan 0.025% 15 q"),  # ведущий ноль — всегда дробь
        ("Aspirin 1.125 q N10", "Aspirin 1.125 q N10"),  # не единица активности — дробь
        ("Ozempik 0.25, 0.5 mq/doza", "Ozempik 0.25, 0.5 mq/doza"),  # перечисление
    ],
)
def test_normalize_numbers(raw, expected):
    assert normalize_numbers(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Koros plus (40+10mq) № 30", "40 mq/10 mq"),
        ("Lukastin plus (10+5)mq № 30", "10 mq/5 mq"),
        ("Uperio 97/103 mq 28 əd.", "97 mq/103 mq"),
        ("Ekvapress 10/5/ 1.5 mq № 28", "10 mq/5 mq/1.5 mq"),
        ("Diampa M 5 mq+500 mq № 28", "5 mq/500 mq"),
        ("Ozempik 0.25, 0.5 mq/doza", "0.25 mq/0.5 mq"),
        ("Ozempik 0.25 mq, 0.5 mq/doza", "0.25 mq/0.5 mq/doza"),
    ],
)
def test_expand_dose_lists(raw, expected):
    assert expected in expand_dose_lists(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "Femoston 2/10 № 28",  # единицы нет — не доза
        "Solgar Koenzym Q-10, 100 mq 30 əd",  # «10» — часть названия
        "Solgar Omega-3-6-9, 1300 mq 120 əd",
        "İnsulin iynəsi 32G 0.23/ 0.25 x 6 mm N100",
    ],
)
def test_expand_dose_lists_leaves_other_numbers_alone(raw):
    assert expand_dose_lists(raw) == raw


@pytest.mark.parametrize(
    ("name", "dosage", "pack"),
    [
        ("Ozempik 1.0 mq/doza 3.0 ml № 1 (Şpris - qələm)", "1mq/doza", "n1"),
        ("Ozempik 0,25 mq/0,5 mq 1,5 ml", "0.25mq/0.5mq", "1.5ml"),  # было «5ml»
        ("Amikacin sulfat 0,50 q 1 əd", "0.5q", "n1"),
        ("Euthyrox 100 mkq 100 əd.", "100mkq", "n100"),  # mkq = микрограмм
        ("Kardosal Kombo plus 20/5/12,5 mq №28", "20mq/5mq/12.5mq", "n28"),
        ("Soliqamma 10.000 İU 25 mq № 30", "10000iu", "n30"),
        ("Numis med bədən losyonu PH 5.5 200 ml", "200ml", "200ml"),  # было «5.5200ml»
        ("Pulmares 0.25 mq/ml 2 ml №20 (Flakon)", "0.25mq/ml", "n20"),
        ("Azoksin 200mq/5ml 15ml", "200mq/5ml", "15ml"),
    ],
)
def test_dosage_and_pack_notation(name, dosage, pack):
    assert extract_dosage(name) == dosage
    assert extract_pack_size(name) == pack


def test_total_volume_keeps_the_fraction():
    assert extract_total_volume("Ksalatan 0,005% 2.5 ml") == "2.5ml"
    assert extract_total_volume("Ozempik 1.0 mq/doza 3.0 ml № 1") == "3ml"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # число силы перед «N20» раньше съедалось вместе с фасовкой
        ("Kreon 10000  N20", "kreon 10000"),
        ("Kreon 10000 №20 (Kapsula)", "kreon 10000"),
        ("Aspirin 500 N20", "aspirin 500"),
        ("Xalatan 0,005% 2,5 ml", "xalatan 0.005%"),  # было «0 005%»
        # пояснения в скобках: форма, путь введения, страна, тара
        ("Orniksil № 30 (Tabletlər) (Latviya)", "orniksil"),
        ("Pulmares  0.25 mq/ml 2 ml N20 (inhalyasiya üçün suspenziya)", "pulmares"),
        ("Neladeks  5 q (göz məlhəmi) Neladex", "neladeks neladex"),
        ("Kombilak № 30 (Kapsulalar) (San Marino)", "kombilak"),
        ("Ozempik  1 mq/doza  N1 (məhlullu şpris-qələm)", "ozempik"),
        ("Koros plus (40+10mq) № 30  ( Tabletlər)", "koros plus"),
        ("Beklomil  100 mkq/doza  200 doza (Beclomil)", "beklomil 200 beclomil"),
        # …но не всё, что в скобках: вкус, аудитория, бренд — часть товара
        ("Doktor Qorlo (bal) № 36 (Pastillər)", "doktor qorlo bal"),
        ("Tenoten N40 (uşaqlar üçün)", "tenoten usaqlar ucun"),
        ("Şi yağı 20 ml (Medoil)", "si medoil"),
        # вне скобок «Şpris» — товар, а не тара
        ("Şpris 5 ml", "spris"),
    ],
)
def test_normalize_name_coverage_cases(name, expected):
    assert normalize_name(name) == expected


@pytest.mark.parametrize(
    ("name", "form"),
    [
        ("Kreon 10000 №20 (Kapsula)", "capsule"),
        ("Montel 4 mq №28 (Saşe)", "sachet"),
        ("Neladeks 5 q (göz məlhəmi)", "ointment"),
        ("Dream Drops 30 ml (Damla)", "drops"),
        # тара и нестрогие пояснения форму не задают
        ("Pulmares 2 ml №20 (Flakon)", None),
        ("Duspatalin 200 mq № 30 (Həb)", None),
        ("Almaqel A 170 ml (Suspenziya)", None),
        # …и не заслоняют настоящую форму, названную рядом
        ("Tad 600 mq № 10 (Flakon) məhlul", "solution"),
    ],
)
def test_extract_form_descriptors(name, form):
    assert extract_form(name) == form


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Kreon", "Creon"),
        ("Atiqen", "Atigen"),
        ("Aziraq", "Azirag-"),
        ("Orniksil", "Ornicsil"),
        ("Veqovi", "Wegovy"),
        ("Maksideks", "Maxidex"),
        ("Sefazolin", "Cefazolin"),
        ("Yodomarin", "İodomarin"),
        ("Диос-Вен", "Dios-Ven"),
    ],
)
def test_fold_spelling_joins_transliterations(a, b):
    assert fold_spelling(a) == fold_spelling(b)


def test_fold_spelling_keeps_letter_codes_apart():
    assert fold_spelling("Vitamin C") != fold_spelling("Vitamin K")
    assert fold_spelling("Akriderm SK") != fold_spelling("Akriderm GK")


@pytest.mark.parametrize(
    ("name", "pack"),
    [
        # пробел между числами — не разделитель тысяч
        ("Uşaq qidası NAN - 2 400 qr", "400q"),
        ("Sebamed günəş kremi SPF 30 150 ml", "150ml"),
        ("Angins 10 100 ml", "100ml"),
    ],
)
def test_pack_size_does_not_glue_neighbouring_numbers(name, pack):
    assert extract_pack_size(name) == pack


def test_every_route_word_stripped_from_the_name_has_a_class():
    """Слово, вырезаемое из имени как путь введения, матчер обязан сверять сам."""
    from src.matcher import _has_conflicting_route
    from src.normalize import ROUTE_CLASSES

    assert normalize_name("Dermasol 100 ml (xarici məhlul)") == normalize_name(
        "Dermasol 100 ml (daxili məhlul)"
    )
    assert _has_conflicting_route("Dermasol (xarici məhlul)", "Dermasol (daxili məhlul)")
    assert _has_conflicting_route("Lidosan (dəri üçün sprey)", "Lidosan (oral sprey)")
    assert {"vag", "deri", "xarici", "daxili"} <= set(ROUTE_CLASSES)
