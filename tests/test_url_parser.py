"""Тесты парсера URL → (site, slug)."""

from src.url_parser import parse_category_url, parse_urls_block


def test_pharmonline_basic():
    site, slug = parse_category_url("https://pharmonline.az/products?category=ushaq-qidasi")
    assert site == "pharmonline"
    assert slug == "ushaq-qidasi"


def test_pharmonline_with_www():
    site, slug = parse_category_url(
        "https://www.pharmonline.az/products?category=antiallergik-vasiteler"
    )
    assert site == "pharmonline"
    assert slug == "antiallergik-vasiteler"


def test_pharmonline_http():
    site, slug = parse_category_url("http://pharmonline.az/products?category=test")
    assert site == "pharmonline"
    assert slug == "test"


def test_aptekonline_numeric_id():
    site, slug = parse_category_url("https://aptekonline.az/products/252")
    assert site == "aptekonline"
    assert slug == "252"


def test_aptekonline_with_www_and_trailing():
    site, slug = parse_category_url("https://www.aptekonline.az/products/114/")
    assert site == "aptekonline"
    assert slug == "114"


def test_aloe_category_slug_url_encoded():
    """Турецкие символы в URL должны быть декодированы (uşaq, не u%C5%9Faq)."""
    site, slug = parse_category_url(
        "https://aloe.az/catalog/filters/?category_slug=u%C5%9Faq-qidas%C4%B1"
    )
    assert site == "aloe"
    assert slug == "uşaq-qidası"


def test_aloe_category_slug_plain():
    site, slug = parse_category_url(
        "https://aloe.az/catalog/filters/?category_slug=dermanlar"
    )
    assert site == "aloe"
    assert slug == "dermanlar"


def test_aloe_product_field_bestseller():
    site, slug = parse_category_url(
        "https://aloe.az/catalog/filters/?product_field=bestseller"
    )
    assert site == "aloe"
    # AloeScraper ожидает формат "product_field=VALUE" чтобы знать что это спец-срез
    assert slug == "product_field=bestseller"


def test_aloe_product_field_promo():
    site, slug = parse_category_url(
        "https://aloe.az/catalog/filters/?product_field=promo"
    )
    assert site == "aloe"
    assert slug == "product_field=promo"


def test_unknown_url():
    assert parse_category_url("https://google.com/anything") == (None, None)


def test_empty_string():
    assert parse_category_url("") == (None, None)


def test_whitespace_stripped():
    site, slug = parse_category_url("  https://pharmonline.az/products?category=test  \n")
    assert site == "pharmonline"
    assert slug == "test"


def test_parse_block_three_sites():
    text = """
    https://pharmonline.az/products?category=ushaq-qidasi
    https://aptekonline.az/products/252
    https://aloe.az/catalog/filters/?category_slug=u%C5%9Faq-qidas%C4%B1
    """
    found, unknown = parse_urls_block(text)
    assert found == {
        "pharmonline": "ushaq-qidasi",
        "aptekonline": "252",
        "aloe": "uşaq-qidası",
    }
    assert unknown == []


def test_parse_block_with_unknown_line():
    text = """
    https://pharmonline.az/products?category=test
    https://random-site.com/foo
    """
    found, unknown = parse_urls_block(text)
    assert found == {"pharmonline": "test"}
    assert unknown == ["https://random-site.com/foo"]


def test_parse_block_duplicate_site_last_wins():
    text = """
    https://pharmonline.az/products?category=first
    https://pharmonline.az/products?category=second
    """
    found, _ = parse_urls_block(text)
    assert found == {"pharmonline": "second"}
