"""Fail-closed coverage tests for the guarded Pharmonline public API source."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from src.scrapers.pharmonline_public_api import (
    PUBLIC_API_AVAILABILITY_SOURCE,
    PUBLIC_CATALOG_ROUTE,
    PharmonlinePublicAPIError,
    PharmonlinePublicAPIScraper,
    _canonical_product_url,
    _json_from_rendered_body,
    _same_origin_sitemap_url,
)


def _raw_product(index: int, *, count: int | None = 4) -> dict:
    return {
        "_id": f"PRODUCT{index:010d}",
        "path": f"product-{index}",
        "name": f"Product {index}",
        "i18n": {"az": {"name": f"Məhsul {index}"}},
        "category": ["category-1"],
        "minPrice": 12.0,
        "totalMinPrice": 9.0,
        "totalCount": count,
        "manufacturerCountryData": {"geocode": "de"},
        "barcode": "1234567890123",
    }


class _FakePublicAPIScraper(PharmonlinePublicAPIScraper):
    def __init__(
        self,
        pages: dict[int, dict],
        *,
        sitemap_urls: set[str],
        category_unavailable: bool = False,
    ):
        super().__init__()
        self.page_size = 2
        self.max_pages = 10
        self._pages = pages
        self._sitemap_urls = sitemap_urls
        self._category_unavailable = category_unavailable
        self.requests: list[str] = []

    async def _crawlbase_json(self, target_url: str):
        self.requests.append(target_url)
        if target_url.endswith("/api/categories?type=category"):
            if self._category_unavailable:
                raise PharmonlinePublicAPIError("crawlbase_target_status_cb_520_origin_520")
            return [{"_id": "category-1", "path": "vitaminler"}]
        page = int(parse_qs(urlsplit(target_url).query)["page"][0])
        return self._pages[page]

    async def _fetch_sitemap_product_urls(self) -> set[str]:
        return self._sitemap_urls


def _pages(*, second_page: list[dict] | None = None) -> dict[int, dict]:
    first = [_raw_product(1), _raw_product(2)]
    second = second_page if second_page is not None else [_raw_product(3)]
    return {
        1: {"data": first, "total": 3, "pages": 2},
        2: {"data": second, "total": 3, "pages": 2},
    }


@pytest.mark.asyncio
async def test_public_api_buffers_then_yields_verified_full_catalog():
    expected_urls = {f"https://pharmonline.az/product/product-{index}" for index in (1, 2, 3)}
    scraper = _FakePublicAPIScraper(_pages(), sitemap_urls=expected_urls)

    products = [product async for product in scraper.scrape_category(PUBLIC_CATALOG_ROUTE)]

    assert [product.external_id for product in products] == [
        "PRODUCT0000000001",
        "PRODUCT0000000002",
        "PRODUCT0000000003",
    ]
    assert all(product.identity_verified for product in products)
    assert all(product.category == "vitaminler" for product in products)
    assert all(
        product.availability_source == PUBLIC_API_AVAILABILITY_SOURCE for product in products
    )
    assert products[0].price == 12.0
    assert products[0].discount_price == 9.0
    assert products[0].is_on_sale is True
    assert products[0].offer_availability_status == "in_stock"
    assert products[0].offer_quantity == 4
    assert products[0].manufacturer_country_raw == "de"
    assert products[0].barcode == "1234567890123"
    status = scraper._route_statuses[PUBLIC_CATALOG_ROUTE]
    assert status.complete is True
    assert status.expected_pages == status.visited_pages == 2
    assert status.raw_items == status.parsed_items == status.expected_items == 3
    product_requests = [url for url in scraper.requests if "/api/products?" in url]
    assert [parse_qs(urlsplit(url).query)["page"][0] for url in product_requests] == ["1", "2"]
    assert all(parse_qs(urlsplit(url).query)["sortBy"][0] == "name_asc" for url in product_requests)


@pytest.mark.asyncio
async def test_public_api_rejects_duplicate_id_before_any_yield():
    expected_urls = {f"https://pharmonline.az/product/product-{index}" for index in (1, 2, 3)}
    scraper = _FakePublicAPIScraper(
        _pages(second_page=[_raw_product(2)]),
        sitemap_urls=expected_urls,
    )

    products = [product async for product in scraper.scrape_category(PUBLIC_CATALOG_ROUTE)]

    assert products == []
    status = scraper._route_statuses[PUBLIC_CATALOG_ROUTE]
    assert status.complete is False
    assert status.abort_reason == "products_duplicate_external_id"


@pytest.mark.asyncio
async def test_public_api_rejects_sitemap_mismatch_before_any_yield():
    scraper = _FakePublicAPIScraper(
        _pages(),
        sitemap_urls={"https://pharmonline.az/product/product-1"},
    )

    products = [product async for product in scraper.scrape_category(PUBLIC_CATALOG_ROUTE)]

    assert products == []
    status = scraper._route_statuses[PUBLIC_CATALOG_ROUTE]
    assert status.complete is False
    assert status.abort_reason == "products_sitemap_set_mismatch"


@pytest.mark.asyncio
async def test_public_api_keeps_identity_proof_when_category_map_is_temporarily_unavailable():
    expected_urls = {f"https://pharmonline.az/product/product-{index}" for index in (1, 2, 3)}
    scraper = _FakePublicAPIScraper(
        _pages(),
        sitemap_urls=expected_urls,
        category_unavailable=True,
    )

    products = [product async for product in scraper.scrape_category(PUBLIC_CATALOG_ROUTE)]

    assert len(products) == 3
    assert all(product.category is None for product in products)
    assert scraper._route_statuses[PUBLIC_CATALOG_ROUTE].complete is True


def test_product_mapper_marks_explicit_zero_stock_and_current_price():
    scraper = PharmonlinePublicAPIScraper()
    raw = _raw_product(7, count=0)
    raw["minPrice"] = 9.0
    raw["totalMinPrice"] = 9.0

    product = scraper._build_product(raw, {"category-1": "vitaminler"})

    assert product is not None
    assert product.offer_availability_status == "out_of_stock"
    assert product.offer_quantity == 0
    assert product.price == 9.0
    assert product.discount_price is None
    assert product.is_on_sale is False
    assert product.url == "https://pharmonline.az/product/product-7"


def test_sitemap_url_canonicalizes_localized_product_url():
    assert (
        _canonical_product_url(
            "https://pharmonline.az/ru/product/vitamin-c-1000", source_is_path=False
        )
        == "https://pharmonline.az/product/vitamin-c-1000"
    )
    assert _canonical_product_url("bad/path", source_is_path=True) is None


def test_sitemap_index_urls_must_stay_on_pharmonline_origin():
    assert _same_origin_sitemap_url("/sitemap-products-1.xml") == (
        "https://pharmonline.az/sitemap-products-1.xml"
    )
    assert _same_origin_sitemap_url("https://www.pharmonline.az/sitemap-products-1.xml") == (
        "https://pharmonline.az/sitemap-products-1.xml"
    )
    assert _same_origin_sitemap_url("//example.test/sitemap-products-1.xml") is None
    assert _same_origin_sitemap_url("https://pharmonline.az/sitemap-products-1.xml?x=1") is None


@pytest.mark.asyncio
async def test_sitemap_fetch_rejects_external_index_and_product_urls(monkeypatch):
    scraper = PharmonlinePublicAPIScraper()

    async def external_index(_url: str) -> str:
        return (
            "<sitemapindex><sitemap><loc>https://example.test/"
            "sitemap-products-1.xml</loc></sitemap></sitemapindex>"
        )

    monkeypatch.setattr(scraper, "_crawlbase_xml", external_index)
    with pytest.raises(PharmonlinePublicAPIError, match="product_sitemap_index_url_invalid"):
        await scraper._fetch_sitemap_product_urls()

    async def external_product(url: str) -> str:
        if url.endswith("/sitemap.xml"):
            return (
                "<sitemapindex><sitemap><loc>/sitemap-products-1.xml</loc></sitemap></sitemapindex>"
            )
        return "<urlset><url><loc>https://example.test/product/not-safe</loc></url></urlset>"

    monkeypatch.setattr(scraper, "_crawlbase_xml", external_product)
    with pytest.raises(PharmonlinePublicAPIError, match="product_sitemap_product_url_invalid"):
        await scraper._fetch_sitemap_product_urls()


def test_crawlbase_json_body_accepts_direct_and_pre_rendered_json():
    payload = {"data": [{"_id": "PRODUCT0000000001"}]}
    assert _json_from_rendered_body(payload) == payload
    assert _json_from_rendered_body(json.dumps(payload)) == payload
    assert _json_from_rendered_body(f"<html><pre>{json.dumps(payload)}</pre></html>") == payload
