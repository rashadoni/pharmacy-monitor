import json

import pytest

from src.scrapers.aloe import (
    AloeScraper,
    aloe_listing_page_info,
    aloe_products_from_listing_html,
)


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
async def test_aloe_rsc_rejects_http_200_error_shell(monkeypatch) -> None:
    async def fake_fetch(self, url: str) -> str:
        return _flight_html('e:E{"digest":"1038857154"}')

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)

    with pytest.raises(
        RuntimeError,
        match="contains neither pagination nor products",
    ):
        _ = [p async for p in scraper.scrape_category("dermanlar")]
