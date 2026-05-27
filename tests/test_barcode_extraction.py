"""Tests для barcode extraction helpers (Phase 2.2).

Покрываем `_extract_barcode_from_jsonld` (priority order, normalisation,
validation) и косвенно verify что JSON-LD parser работает с реальными
chunkами schema.org Product.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scrapers.ai_crawler import (  # noqa: E402
    _extract_barcode_from_jsonld,
    parse_jsonld_product,
)


# ─── _extract_barcode_from_jsonld ─────────────────────────────────────


def test_barcode_from_gtin13():
    """schema.org gtin13 = EAN-13, наш primary source."""
    assert _extract_barcode_from_jsonld({"gtin13": "4607017950021"}) == "4607017950021"


def test_barcode_priority_gtin13_over_gtin():
    """Если есть и gtin13, и gtin — gtin13 побеждает (более точный)."""
    jsonld = {"gtin13": "4607017950021", "gtin": "123"}
    assert _extract_barcode_from_jsonld(jsonld) == "4607017950021"


def test_barcode_falls_through_to_gtin_when_no_specific():
    assert _extract_barcode_from_jsonld({"gtin": "12345678"}) == "12345678"


def test_barcode_strips_whitespace_and_dashes():
    assert _extract_barcode_from_jsonld({"gtin13": "  4607-0179-50021  "}) == "4607017950021"


def test_barcode_rejects_short_string():
    """Less than 8 digits — not a valid barcode."""
    assert _extract_barcode_from_jsonld({"gtin13": "12345"}) is None


def test_barcode_rejects_non_numeric():
    assert _extract_barcode_from_jsonld({"gtin13": "ABC-DEFG-HIJK"}) is None


def test_barcode_rejects_too_long():
    """15+ digits is not a known GTIN format."""
    assert _extract_barcode_from_jsonld({"gtin13": "12345678901234567890"}) is None


def test_barcode_skips_empty_values():
    """Empty string, None, falsy — try next key, finally None."""
    assert _extract_barcode_from_jsonld({"gtin13": "", "gtin12": None, "gtin": "1234567890"}) == "1234567890"


def test_barcode_returns_none_for_empty_jsonld():
    assert _extract_barcode_from_jsonld({}) is None


def test_barcode_ignores_mpn():
    """`mpn` (manufacturer part number) is intentionally NOT used — could be
    not-a-barcode in pharma. We want to be conservative."""
    assert _extract_barcode_from_jsonld({"mpn": "4607017950021"}) is None


# ─── parse_jsonld_product end-to-end ──────────────────────────────────


def test_jsonld_product_with_gtin_in_html():
    """Real-world: aloe.az/pharmonline injects <script type=application/ld+json>.

    parse_jsonld_product requires `offers.availability` — that's how it
    distinguishes "this is THE product page" from related-item carousels.
    """
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type":"Product","name":"Aspirin 500mg","gtin13":"5712345678901",
     "offers":{"@type":"Offer","price":"3.45","priceCurrency":"AZN",
               "availability":"https://schema.org/InStock"}}
    </script>
    </head><body>...</body></html>
    """
    jsonld = parse_jsonld_product(html)
    assert jsonld is not None
    assert _extract_barcode_from_jsonld(jsonld) == "5712345678901"
