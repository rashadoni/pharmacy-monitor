"""Task #33 — aloe URL slug generation.

Verified via Playwright MCP click-navigation на 3 reference products
(2026-05-28):
  'Vitamin B1 5% 1 ml 10 əd.'        → vitamin-b1-5-1-ml-10-ed
  'Diampa-M 12,5 mq/1000 mq 28 əd'   → diampa-m-125-mq1000-mq-28-ed
  'Şüşə və əmzik fırçası'             → suse-ve-emzik-fircasi
"""
import pytest

from src.scrapers.aloe import aloe_slug


@pytest.mark.parametrize("name,expected", [
    # Reference cases verified via real Playwright clicks 2026-05-28
    ("Vitamin B1 5% 1 ml 10 əd.", "vitamin-b1-5-1-ml-10-ed"),
    ("Diampa-M 12,5 mq/1000 mq 28 əd", "diampa-m-125-mq1000-mq-28-ed"),
    ("Şüşə və əmzik fırçası", "suse-ve-emzik-fircasi"),

    # Edge cases
    ("", ""),
    ("   ", ""),
    ("Already-lowercase-slug", "already-lowercase-slug"),

    # Azeri-specific chars (all 8)
    ("ə Ə İ ı ö Ö ü Ü", "e-e-i-i-o-o-u-u"),
    ("ş Ş ç Ç ğ Ğ", "s-s-c-c-g-g"),

    # Numbers preserved, commas dropped (12,5 → 125 not 12-5)
    ("Test 12,5 mq", "test-125-mq"),

    # Punctuation removed WITHOUT space-replacement (matches aloe behavior:
    # `12,5` → `125`, not `12-5`). So adjacent words without space → merged.
    ("Foo,,, Bar///Baz", "foo-barbaz"),

    # Trailing/leading whitespace stripped
    ("  Vitamin  ", "vitamin"),

    # Period at end of "əd." should be removed
    ("Aspirin 100mg №30 əd.", "aspirin-100mg-№30-ed"),

    # Multiple spaces collapsed
    ("A    B    C", "a-b-c"),

    # Hyphen in product name preserved (Diampa-M)
    ("Diampa-M", "diampa-m"),
])
def test_aloe_slug(name: str, expected: str) -> None:
    assert aloe_slug(name) == expected


def test_aloe_slug_is_url_safe() -> None:
    """Slug should be safe for URL path component (no spaces, no /, etc.)."""
    import string
    samples = [
        "Vitamin B1 5%",
        "Şüşə və əmzik fırçası",
        "Diampa-M 12,5 mq/1000 mq 28 əd",
    ]
    safe_chars = set(string.ascii_lowercase + string.digits + "-")
    for s in samples:
        slug = aloe_slug(s)
        unsafe = set(slug) - safe_chars
        # Allow Unicode like № which appears in actual product names
        # but flag truly URL-unsafe chars like spaces, slashes
        forbidden = {" ", "/", "?", "#", "&", "=", "%", "+"}
        offenders = unsafe & forbidden
        assert not offenders, f"Unsafe chars in slug '{slug}' for '{s}': {offenders}"
