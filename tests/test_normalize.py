"""Тесты нормализации имён, парсинга цены и извлечения дозировки/упаковки."""

from src.normalize import (
    extract_dosage,
    extract_pack_size,
    normalize_name,
    parse_price,
    strip_accents,
)


def test_strip_accents_decomposable_diacritics():
    """strip_accents использует NFKD — работает только на пре-композированных символах.

    `ç`, `ü`, `Ş` декомпозируются → диакритика убирается.
    `ə` (U+0259, Latin schwa) — атомарный символ, НЕ декомпозируется. Это известно
    и ожидаемо; для матчинга мы полагаемся на нормализацию name через token-set ratio.
    """
    assert strip_accents("üçün") == "ucun"
    assert strip_accents("Şəkər") == "Səkər"  # ş→S, ə остаётся, к не имеет диакритики


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
