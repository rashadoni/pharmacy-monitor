import json
from pathlib import Path

import httpx
import pytest
from tenacity import wait_none

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
    assert "dermanlar" not in scraper._route_statuses


@pytest.mark.asyncio
async def test_aloe_unknown_country_id_does_not_poison_complete_listing_route(
    monkeypatch,
) -> None:
    """Regression for production run #487."""
    payload = _product_payload(product_id=487, name="Unknown country", slug="unknown-country")
    payload["manufacturer_country"] = 1549
    page = _flight_html(
        '15:[[["$","$L","487",{"data":'
        + json.dumps(payload)
        + '}]],false,["$","$L",null,{"currentPage":1,"lastPage":1}]]'
    )

    async def fake_fetch(self, url: str) -> str:
        if "/catalog/filters/" in url:
            return page
        return '"inStock":true'

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)

    products = [p async for p in scraper.scrape_category("usaq-dunyasi")]

    assert len(products) == 1
    assert products[0].manufacturer_country_raw == "1549"
    assert products[0].country_source == "aloe_api_country_id"
    status = scraper._route_statuses["usaq-dunyasi"]
    assert status.complete is True
    assert status.abort_reason is None
    assert status.visited_pages == status.expected_pages == 1


# ─── повтор загрузки страницы (httpx подменяется на MockTransport) ───────────


def _listing_page(product_id: int, *, current: int, last: int) -> str:
    payload = _product_payload(
        product_id=product_id, name=f"Item {product_id}", slug=f"item-{product_id}"
    )
    return _flight_html(
        f'15:[[["$","$L","{product_id}",{{"data":'
        + json.dumps(payload)
        + f'}}]],false,["$","$L",null,{{"currentPage":{current},"lastPage":{last}}}]]'
    )


def _scraper_with_transport(monkeypatch, handler) -> AloeScraper:
    """Скрейпер, чьи запросы идут в `handler`, а паузы между повторами нулевые."""
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    monkeypatch.setattr("src.scrapers.aloe.wait_exponential", lambda **_: wait_none())
    return AloeScraper(rate_limit_sec=0.001, max_retries=3)


@pytest.mark.parametrize("blip", ["read_error", "remote_closed", "http_503"])
async def test_aloe_listing_survives_one_transient_failure(monkeypatch, blip: str) -> None:
    """Одиночный сбой сети не должен стоить проверки всего каталога (прогон #952)."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        requests.append(page)
        if page == "2" and requests.count("2") == 1:
            if blip == "read_error":
                raise httpx.ReadError("connection reset", request=request)
            if blip == "remote_closed":
                raise httpx.RemoteProtocolError("server disconnected", request=request)
            return httpx.Response(int(blip.removeprefix("http_")))
        return httpx.Response(200, text=_listing_page(int(page), current=int(page), last=2))

    scraper = _scraper_with_transport(monkeypatch, handler)

    products = [p async for p in scraper.scrape_category("kosmetika")]

    assert [p.external_id for p in products] == ["item-1", "item-2"]
    assert requests == ["1", "2", "2"]
    status = scraper._route_statuses["kosmetika"]
    assert status.complete is True
    assert status.visited_pages == status.expected_pages == 2


async def test_aloe_listing_gives_up_after_bounded_retries(monkeypatch) -> None:
    """Страница, не отдавшаяся и с повторами, по-прежнему роняет маршрут."""
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ReadTimeout("no answer", request=request)

    scraper = _scraper_with_transport(monkeypatch, handler)

    with pytest.raises(httpx.ReadTimeout):
        [p async for p in scraper.scrape_category("kosmetika")]

    assert requests == 3
    assert "kosmetika" not in scraper._route_statuses


@pytest.mark.parametrize("status", [403, 404, 429])
async def test_aloe_listing_does_not_retry_a_refusal(monkeypatch, status: int) -> None:
    """Любой 4xx — ответ сайта, а не помеха: один запрос и честный отказ."""
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status)

    scraper = _scraper_with_transport(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError):
        [p async for p in scraper.scrape_category("kosmetika")]

    assert requests == 1


async def test_aloe_listing_retry_is_logged(monkeypatch) -> None:
    """Повтор гасит сбой, но не прячет его: в логе остаётся след."""
    seen: list[dict] = []
    monkeypatch.setattr(
        "src.scrapers.aloe.log.warning", lambda event, **kw: seen.append({"event": event, **kw})
    )
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, text=_listing_page(1, current=1, last=1))

    scraper = _scraper_with_transport(monkeypatch, handler)

    html_text = await scraper._fetch_listing_html("https://aloe.az/catalog/filters/?page=1")

    assert "item-1" in html_text
    retries = [row for row in seen if row["event"] == "aloe_fetch_retry"]
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1
    assert retries[0]["url"].endswith("?page=1")
    assert retries[0]["error"].startswith("ConnectTimeout")
