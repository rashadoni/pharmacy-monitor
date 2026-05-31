"""Тесты нормализации имён, парсинга цены и извлечения дозировки/упаковки."""

import pytest

from src.normalize import (
    extract_dosage,
    extract_form,
    extract_pack_size,
    normalize_name,
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
