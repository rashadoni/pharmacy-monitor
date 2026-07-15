"""Unit tests for the AI crawler — focuses on the Next.js RSC JSON-LD parser.

Strategy: load fixture HTML files, call parse_next_rsc_jsonld() directly. No
browser, no network, fully deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.scrapers import ai_crawler
from src.scrapers.ai_crawler import (
    AICrawlerScraper,
    _build_product_from_jsonld,
    _decode_rsc_chunks,
    parse_next_rsc_jsonld,
)
from src.scrapers.base import SiteScrapeFatalError

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOTS = Path(__file__).parent.parent / "data" / "snapshots"


def test_rsc_jsonld_extracts_real_aloe_product() -> None:
    """A real product page from aloe.az yields the embedded schema.org Product."""
    html = (FIXTURES / "aloe_product_rsc.html").read_text(encoding="utf-8")

    result = parse_next_rsc_jsonld(html)

    assert result is not None
    assert result["@type"] == "Product"
    assert result["name"] == "Nimesil 100 q 30 əd"
    assert result["sku"] == "12019"
    assert result["brand"]["name"] == "Berlin chemie"
    assert result["offers"]["price"] == "13.43"
    assert result["offers"]["priceCurrency"] == "AZN"
    assert result["offers"]["availability"] == "https://schema.org/InStock"


def test_rsc_jsonld_to_scraped_product() -> None:
    """The extracted JSON-LD round-trips through _build_product_from_jsonld."""
    html = (FIXTURES / "aloe_product_rsc.html").read_text(encoding="utf-8")

    jsonld = parse_next_rsc_jsonld(html)
    assert jsonld is not None
    product = _build_product_from_jsonld("aloe.az", "https://aloe.az/nimesil-100-q-30-ed/", jsonld)

    assert product is not None
    assert product.site == "aloe.az"
    assert product.external_id == "12019"
    assert product.name == "Nimesil 100 q 30 əd"
    assert product.brand == "Berlin chemie"
    assert product.price == 13.43
    assert product.image_url is not None
    assert product.image_url.startswith("https://")


def test_rsc_jsonld_returns_none_on_filter_page() -> None:
    """Listing/filter pages have no single Product with offers.availability."""
    snapshot = SNAPSHOTS / "aloe.az_product_field-bestseller.html"
    if not snapshot.exists():
        # Snapshot ships alongside the repo; skip cleanly if it was pruned.
        import pytest

        pytest.skip(f"snapshot missing: {snapshot}")
    html = snapshot.read_text(encoding="utf-8")

    assert parse_next_rsc_jsonld(html) is None


def test_rsc_jsonld_returns_none_on_html_without_rsc() -> None:
    """Plain HTML (e.g. server-rendered Bootstrap) has no chunks → None."""
    html = "<html><body><h1>No RSC here</h1></body></html>"
    assert parse_next_rsc_jsonld(html) is None


def test_rsc_jsonld_handles_malformed_chunks() -> None:
    """Truncated / non-JSON chunks are skipped without raising."""
    html = (
        "<script>self.__next_f.push([1, "
        '"truncated string with no closing quote'  # missing closing "
        "</script>"
        "<script>self.__next_f.push([1, []])</script>"  # not a string
        '<script>self.__next_f.push([1, "valid but unrelated"])</script>'
    )
    # No Product schema in any chunk → None, but importantly: no exception.
    assert parse_next_rsc_jsonld(html) is None


def test_decode_rsc_chunks_joins_in_document_order() -> None:
    """_decode_rsc_chunks concatenates chunks in the order they appear."""
    html = (
        '<script>self.__next_f.push([1, "alpha"])</script>'
        '<script>self.__next_f.push([1, "beta"])</script>'
        '<script>self.__next_f.push([1, "gamma"])</script>'
    )
    assert _decode_rsc_chunks(html) == "alphabetagamma"


def test_decode_rsc_chunks_handles_escapes() -> None:
    """JSON string escapes (\\\", \\n, \\u) are decoded properly."""
    # The raw HTML representation of a chunk containing a quote and a newline.
    html = '<script>self.__next_f.push([1, "with \\"quote\\" and\\nnewline"])</script>'
    assert _decode_rsc_chunks(html) == 'with "quote" and\nnewline'


@pytest.mark.asyncio
async def test_ai_crawl_proxy_fatal_stops_after_first_url(monkeypatch):
    class TestCrawler(AICrawlerScraper):
        site_name = "test"
        base_url = "https://test.invalid"

    class FakePage:
        async def close(self):
            return None

    crawler = TestCrawler()
    calls = []

    async def new_page():
        return FakePage()

    async def discover(*args, **kwargs):
        return [
            "https://test.invalid/one",
            "https://test.invalid/two",
            "https://test.invalid/three",
        ]

    async def fatal_goto(page, url, check_captcha=True):
        calls.append(url)
        raise RuntimeError(
            "proxy http://user:top-secret@az.decodo.com:30001 rejected HTTP 407"
        )

    monkeypatch.setattr(crawler, "new_page", new_page)
    monkeypatch.setattr(crawler, "goto", fatal_goto)
    monkeypatch.setattr(ai_crawler, "fetch_sitemap_urls", discover)

    with pytest.raises(SiteScrapeFatalError) as exc_info:
        await crawler.crawl(max_urls=3, dry_run=True)

    assert calls == ["https://test.invalid/one"]
    assert str(exc_info.value) == "proxy access rejected: HTTP 407"
    assert "top-secret" not in str(exc_info.value)
