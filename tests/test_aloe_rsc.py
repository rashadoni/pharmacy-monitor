import json
from pathlib import Path

import pytest

from src.scrapers.aloe import (
    AloeScraper,
    _aloe_products_from_listing_html_with_stats,
    aloe_product_detail_signals,
    aloe_listing_page_info,
    aloe_products_from_listing_html,
)
from src.product_policy import OFFER_IN_STOCK, OFFER_OUT_OF_STOCK
from src.scrapers.base import ScrapedProduct


def _flight_html(payload: str) -> str:
    return f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>"


def _product_payload(
    *,
    product_id: int,
    name: str,
    slug: str,
    price: float = 10.0,
    old_price: float = 0.0,
) -> dict:
    return {
        "id": product_id,
        "code": str(product_id),
        "name": name,
        "slug": slug,
        "price": price,
        "old_price": old_price,
        "short_description": "<p>Test desc</p>",
        "brand": {"name": "AloeBrand"},
        "images": [
            {
                "media_manager": {
                    "media_file": "uploads/media/test.png",
                    "thumbnail_path": "uploads/thumbnails/test_thumb.png",
                }
            }
        ],
    }


def test_aloe_malformed_listing_item_closes_route_evidence() -> None:
    products, raw_items, parsed_items, item_failures = (
        _aloe_products_from_listing_html_with_stats(
            '<script>"data":{not-json}</script>',
            category_slug="medicine",
        )
    )

    assert products == []
    assert raw_items == 1
    assert parsed_items == 0
    assert item_failures == 1


def test_aloe_listing_page_info_from_next_flight() -> None:
    html = _flight_html(
        '15:[[["$","$L","26069",{"data":'
        + json.dumps(_product_payload(product_id=26069, name="Reloba 30 ed", slug="reloba-30-ed"))
        + '}]],false,["$","$L",null,{"currentPage":1,"lastPage":480}]]'
    )

    assert aloe_listing_page_info(html) == (1, 480)


def test_aloe_products_from_listing_html_parses_embedded_payload() -> None:
    payload = _product_payload(
        product_id=26069,
        name="Reloba 30 ed",
        slug="reloba-30-ed",
        price=37.7,
        old_price=38.1,
    )
    html = _flight_html(
        '15:[[["$","$L","26069",{"data":'
        + json.dumps(payload)
        + '}]],false,["$","$L",null,{"currentPage":1,"lastPage":480}]]'
    )

    products = aloe_products_from_listing_html(html, category_slug="dermanlar")

    assert len(products) == 1
    product = products[0]
    assert product.site == "aloe"
    assert product.external_id == "reloba-30-ed"
    assert product.url == "https://aloe.az/reloba-30-ed/"
    assert product.name == "Reloba 30 ed"
    assert product.brand == "AloeBrand"
    assert product.price == 38.1
    assert product.discount_price == 37.7
    assert product.is_on_sale is True
    assert product.image_url == "https://ecom.aloe.az/uploads/media/test.png"
    assert product.description == "Test desc"


def test_aloe_listing_preserves_country_id_as_unresolved_and_quantity() -> None:
    payload = _product_payload(product_id=1, name="One", slug="one")
    payload["manufacturer_country"] = 14
    payload["quantity"] = 0
    html = _flight_html(
        '15:[[["$","$L","1",{"data":'
        + json.dumps(payload)
        + '}]],false,["$","$L",null,{"currentPage":1,"lastPage":1}]]'
    )

    product = aloe_products_from_listing_html(html, category_slug="dermanlar")[0]

    assert product.manufacturer is None
    assert product.manufacturer_country_raw == "14"
    assert product.country_source == "aloe_api_country_id"
    assert product.offer_availability_status == OFFER_OUT_OF_STOCK
    assert product.offer_quantity == 0


def test_aloe_detail_signals_parse_rendered_country_and_stock() -> None:
    fixture = (Path(__file__).parent / "fixtures" / "aloe_product_rsc.html").read_text()

    assert aloe_product_detail_signals(fixture) == ("Италия", OFFER_IN_STOCK)


def test_aloe_detail_signals_parse_out_of_stock_flight_payload() -> None:
    html = r'children\":\"Ölkə\"}, children\":\"Сербия\" \\"inStock\\":false'
    country, status = aloe_product_detail_signals(html)

    assert country == "Сербия"
    assert status == OFFER_OUT_OF_STOCK


@pytest.mark.asyncio
async def test_aloe_rsc_scrape_category_uses_last_page(monkeypatch) -> None:
    pages = {
        1: _flight_html(
            '15:[[["$","$L","1",{"data":'
            + json.dumps(_product_payload(product_id=1, name="One", slug="one"))
            + '}]],false,["$","$L",null,{"currentPage":1,"lastPage":3}]]'
        ),
        2: _flight_html(
            '15:[[["$","$L","2",{"data":'
            + json.dumps(_product_payload(product_id=2, name="Two", slug="two"))
            + '}]],false,["$","$L",null,{"currentPage":2,"lastPage":3}]]'
        ),
        3: _flight_html(
            '15:[[["$","$L","3",{"data":'
            + json.dumps(_product_payload(product_id=3, name="Three", slug="three"))
            + '}]],false,["$","$L",null,{"currentPage":3,"lastPage":3}]]'
        ),
    }
    requested: list[str] = []

    async def fake_fetch(self, url: str) -> str:
        requested.append(url)
        if "page=2" in url:
            return pages[2]
        if "page=3" in url:
            return pages[3]
        return pages[1]

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)

    products = [p async for p in scraper.scrape_category("dermanlar")]

    assert [p.external_id for p in products] == ["one", "two", "three"]
    assert len(requested) == 3
    assert requested[0] == "https://aloe.az/catalog/filters/?category_slug=dermanlar"
    assert requested[1].endswith("&page=2")
    assert requested[2].endswith("&page=3")


@pytest.mark.asyncio
async def test_aloe_country_id_is_resolved_and_cached_from_detail(monkeypatch) -> None:
    product = ScrapedProduct(
        site="aloe",
        external_id="ornafer",
        url="https://aloe.az/ornafer/",
        name="Ornafer",
        category="dermanlar",
        manufacturer_country_raw="14",
    )

    async def fake_fetch(self, url: str) -> str:
        return '<span>Ölkə:</span><span>Англия</span> "inStock":true'

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)

    await scraper._enrich_listing_country_ids([product])

    assert product.manufacturer_country_raw == "Англия"
    assert product.country_source == "aloe_country_id_verified_detail"
    assert scraper.verified_country_mappings["14"]["country_code"] == "gb"


@pytest.mark.asyncio
async def test_aloe_country_id_uses_durable_map_without_detail_fetch(monkeypatch) -> None:
    product = ScrapedProduct(
        site="aloe",
        external_id="ornafer",
        url="https://aloe.az/ornafer/",
        name="Ornafer",
        category="dermanlar",
        manufacturer_country_raw="14",
    )

    async def fail_fetch(self, url: str) -> str:
        raise AssertionError("verified country ID must not refetch detail")

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fail_fetch)
    scraper = AloeScraper(
        rate_limit_sec=0,
        country_id_map={
            "14": {
                "country_code": "gb",
                "country_raw": "Англия",
                "source_url": "https://aloe.az/ornafer/",
                "sample_count": 2,
                "version": 1,
            }
        },
    )

    await scraper._enrich_listing_country_ids([product])

    assert product.manufacturer_country_raw == "Англия"
    assert scraper.verified_country_mappings == {}


@pytest.mark.asyncio
async def test_aloe_country_id_requires_two_samples_when_group_has_two(
    monkeypatch,
) -> None:
    products = [
        ScrapedProduct(
            site="aloe",
            external_id=f"ornafer-{index}",
            url=f"https://aloe.az/ornafer-{index}/",
            name="Ornafer",
            category="dermanlar",
            manufacturer_country_raw="14",
        )
        for index in (1, 2)
    ]

    async def flaky_fetch(self, url: str) -> str:
        if url.endswith("-2/"):
            raise ValueError("detail unavailable")
        return '<span>Ölkə:</span><span>Англия</span> "inStock":true'

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", flaky_fetch)
    scraper = AloeScraper(rate_limit_sec=0)

    await scraper._enrich_listing_country_ids(products)

    assert all(product.manufacturer_country_raw == "14" for product in products)
    assert scraper.verified_country_mappings == {}
    assert scraper._route_statuses["dermanlar"].complete is False
    assert "unresolved" in (scraper._route_statuses["dermanlar"].abort_reason or "")
