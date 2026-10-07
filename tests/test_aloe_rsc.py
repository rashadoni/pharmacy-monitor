import json
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs
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
async def test_aloe_country_id_two_country_label_stays_unresolved(monkeypatch) -> None:
    """«Türkiyə-Almaniya» на карточке — две страны сразу: id остаётся числом.

    В словарь соответствий такой id не попадает, поэтому и страна товара
    остаётся неразрешённой, а не превращается в одну из двух наугад.
    """
    products = [
        ScrapedProduct(
            site="aloe",
            external_id=f"floramin-{index}",
            url=f"https://aloe.az/floramin-{index}/",
            name="Floramin",
            category="dermanlar",
            manufacturer_country_raw="7",
        )
        for index in (1, 2)
    ]

    async def fake_fetch(self, url: str) -> str:
        return '<span>Ölkə:</span><span>Türkiyə-Almaniya</span> "inStock":true'

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)

    await scraper._enrich_listing_country_ids(products)

    assert all(product.manufacturer_country_raw == "7" for product in products)
    assert all(product.country_source is None for product in products)
    assert scraper.verified_country_mappings == {}


@pytest.mark.asyncio
async def test_aloe_country_id_known_mapping_with_new_spelling_is_applied(
    monkeypatch,
) -> None:
    """Соответствие из таблицы применяется, когда словарь знает его название.

    Регрессия: id 117 лежал в aloe_country_mappings как «Аргентина», словарь
    этого написания не знал — соответствие считалось негодным, и товар в каждом
    сборе записывался со страной-числом.
    """
    product = ScrapedProduct(
        site="aloe",
        external_id="meloprid",
        url="https://aloe.az/meloprid-10-ed/",
        name="Meloprid 10 əd",
        category="dermanlar",
        manufacturer_country_raw="117",
    )

    async def fail_fetch(self, url: str) -> str:
        raise AssertionError("verified country ID must not refetch detail")

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fail_fetch)
    scraper = AloeScraper(
        rate_limit_sec=0,
        country_id_map={
            "117": {
                "country_code": "ar",
                "country_raw": "Аргентина",
                "source_url": "https://aloe.az/meloprid-10-ed/",
                "sample_count": 1,
                "version": 1,
            }
        },
    )

    await scraper._enrich_listing_country_ids([product])

    assert product.manufacturer_country_raw == "Аргентина"
    assert product.country_source == "aloe_country_id_verified_detail"


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


# ─── повтор загрузки листинга (httpx подменяется на MockTransport) ───────────


def _listing_page(product_id: int, *, current: int, last: int, country: int | None = None) -> str:
    payload = _product_payload(
        product_id=product_id, name=f"Item {product_id}", slug=f"item-{product_id}"
    )
    if country is not None:
        payload["manufacturer_country"] = country
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

    products = [p async for p in scraper.scrape_category("dermanlar")]

    assert [p.external_id for p in products] == ["item-1", "item-2"]
    assert requests == ["1", "2", "2"]
    status = scraper._route_statuses["dermanlar"]
    assert status.complete is True
    assert status.visited_pages == status.expected_pages == 2
    assert scraper.fetch_retries == 1


async def test_aloe_listing_gives_up_after_bounded_retries(monkeypatch) -> None:
    """Страница, не отдавшаяся и с повторами, по-прежнему роняет маршрут."""
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ReadTimeout("no answer", request=request)

    scraper = _scraper_with_transport(monkeypatch, handler)

    with pytest.raises(httpx.ReadTimeout):
        [p async for p in scraper.scrape_category("dermanlar")]

    assert requests == 3
    assert scraper.fetch_retries == 2
    assert "dermanlar" not in scraper._route_statuses


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
        [p async for p in scraper.scrape_category("dermanlar")]

    assert requests == 1
    assert scraper.fetch_retries == 0


async def test_aloe_detail_fetch_is_not_retried(monkeypatch) -> None:
    """Сбой карточки товара маршрут не роняет — повторять его незачем."""
    detail_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal detail_requests
        if request.url.path.startswith("/catalog/filters"):
            return httpx.Response(200, text=_listing_page(7, current=1, last=1, country=1549))
        detail_requests += 1
        raise httpx.ReadTimeout("no answer", request=request)

    scraper = _scraper_with_transport(monkeypatch, handler)

    products = [p async for p in scraper.scrape_category("dermanlar")]

    assert [p.external_id for p in products] == ["item-7"]
    assert detail_requests == 1
    assert scraper.fetch_retries == 0
    assert scraper._route_statuses["dermanlar"].complete is True


async def test_aloe_listing_retry_is_logged(monkeypatch) -> None:
    """Повтор гасит сбой, но не прячет его: в логе остаётся след."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, text=_listing_page(1, current=1, last=1))

    scraper = _scraper_with_transport(monkeypatch, handler)

    with capture_logs() as seen:
        html_text = await scraper._fetch_listing_page("https://aloe.az/catalog/filters/?page=1")

    assert "item-1" in html_text
    retries = [row for row in seen if row["event"] == "aloe_fetch_retry"]
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1
    assert retries[0]["url"].endswith("?page=1")
    assert retries[0]["error"].startswith("ConnectTimeout")


# ─── неразрешимый id страны запоминается на один сбор ────────────────────────


_FIXTURES = Path(__file__).parent / "fixtures"


def _country_page(
    page: int, country_id: str, *, size: int = 2, tag: str = "p"
) -> list[ScrapedProduct]:
    """Товары одной страницы листинга с одним и тем же числовым id страны."""
    return [
        ScrapedProduct(
            site="aloe",
            external_id=f"{tag}{page}-{index}",
            url=f"https://aloe.az/{tag}{page}-{index}/",
            name=f"Item {page}-{index}",
            category="dermanlar",
            manufacturer_country_raw=country_id,
            country_source="aloe_api_country_id",
        )
        for index in range(1, size + 1)
    ]


def _detail_card(country: str | None) -> str:
    """Карточка товара: подпись страны (если есть) и остаток, как на aloe.az."""
    label = f"<span>Ölkə:</span><span>{country}</span> " if country else ""
    return label + '"inStock":true'


def _unresolved_rows(seen: list[dict]) -> list[dict]:
    return [row for row in seen if row["event"] == "aloe_country_id_unresolved"]


@pytest.mark.parametrize(
    "labels",
    [
        pytest.param(("Türkiyə-Almaniya", "Türkiyə-Almaniya"), id="two_countries_in_label"),
        pytest.param(("Специфарма", "Специфарма"), id="label_is_not_a_country"),
        pytest.param((None, None), id="card_has_no_country"),
        pytest.param(("Англия", "Германия"), id="samples_disagree"),
        pytest.param(("Англия", "НВ"), id="one_sample_of_two"),
        pytest.param(("НВ", None), id="not_a_country_and_no_label"),
    ],
)
async def test_aloe_unresolvable_country_id_is_sampled_once_per_run(
    monkeypatch, labels: tuple[str | None, str | None]
) -> None:
    """Карточки прочитаны, страна не определилась — на второй странице их не открываем."""
    requested: list[str] = []

    async def fake_fetch(self, url: str) -> str:
        requested.append(url)
        return _detail_card(labels[0] if url.endswith("-1/") else labels[1])

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)
    pages = [_country_page(page, "7") for page in (1, 2, 3)]

    with capture_logs() as seen:
        for page in pages:
            await scraper._enrich_listing_country_ids(page)

    assert requested == ["https://aloe.az/p1-1/", "https://aloe.az/p1-2/"]
    # Страновая политика не ослаблена: id остался числом, источник прежний.
    for product in (product for page in pages for product in page):
        assert product.manufacturer_country_raw == "7"
        assert product.country_source == "aloe_api_country_id"
    # Отказ не попадает ни в то, что main.py пишет в aloe_country_mappings,
    # ни в рабочий словарь соответствий.
    assert scraper.verified_country_mappings == {}
    assert scraper.country_id_map == {}
    unresolved = _unresolved_rows(seen)
    assert len(unresolved) == 1
    assert unresolved[0]["country_id"] == "7"
    assert unresolved[0]["retry"] is False
    assert unresolved[0]["unread_details"] == 0
    # В единственной строке на сбор видно, что именно написано на карточках.
    assert unresolved[0]["labels"] == sorted({label for label in labels if label})
    assert unresolved[0]["unlabeled_details"] == sum(label is None for label in labels)


def test_aloe_real_card_without_country_still_carries_stock() -> None:
    """Опора правила «это не карточка»: настоящая карточка без страны остаток несёт.

    Фикстура — карточка товара с id страны 0, снятая с aloe.az 2026-10-07;
    листинг того же сайта не даёт ни страны, ни остатка.
    """
    card = (_FIXTURES / "aloe_product_no_country.html").read_text()
    listing = (_FIXTURES / "aloe_bestseller.html").read_text()

    assert aloe_product_detail_signals(card) == (None, OFFER_IN_STOCK)
    assert aloe_product_detail_signals(listing) == (None, "unknown")


async def test_aloe_real_card_without_country_closes_id_for_the_run(monkeypatch) -> None:
    """id 0 на живой карточке: страны нет, карточка прочитана — второй раз не открываем."""
    card = (_FIXTURES / "aloe_product_no_country.html").read_text()
    requested: list[str] = []

    async def fake_fetch(self, url: str) -> str:
        requested.append(url)
        return card

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)

    with capture_logs() as seen:
        for page in (1, 2):
            await scraper._enrich_listing_country_ids(_country_page(page, "0", size=1))

    assert requested == ["https://aloe.az/p1-1/"]
    assert [row["retry"] for row in _unresolved_rows(seen)] == [False]
    assert not [row for row in seen if row["event"] == "aloe_country_detail_failed"]


async def test_aloe_labelled_card_without_stock_marker_counts_as_read(monkeypatch) -> None:
    """Подпись страны есть, остатка нет — карточка прочитана: «не карточка» только без обоих."""
    requested: list[str] = []

    async def fake_fetch(self, url: str) -> str:
        requested.append(url)
        return "<span>Ölkə:</span><span>Türkiyə-Almaniya</span>"

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)

    with capture_logs() as seen:
        for page in (1, 2):
            await scraper._enrich_listing_country_ids(_country_page(page, "7", size=1))

    assert requested == ["https://aloe.az/p1-1/"]
    assert [row["retry"] for row in _unresolved_rows(seen)] == [False]


@pytest.mark.parametrize(
    "blip", ["read_timeout", "timeout_without_text", "http_404", "http_503", "not_a_card"]
)
async def test_aloe_country_id_is_rechecked_after_unread_detail(monkeypatch, blip: str) -> None:
    """Непрочитанная карточка — не отказ сайта: на следующей странице проверяем заново."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/p1-2/":
            if blip == "read_timeout":
                raise httpx.ReadTimeout("no answer", request=request)
            if blip == "timeout_without_text":
                # У настоящих таймаутов httpx текст ошибки часто пустой.
                raise httpx.ReadTimeout("", request=request)
            if blip == "not_a_card":
                # Настоящая страница aloe.az, но не карточка: листинг.
                listing = (_FIXTURES / "aloe_bestseller.html").read_text()
                return httpx.Response(200, text=listing)
            return httpx.Response(int(blip.removeprefix("http_")))
        return httpx.Response(200, text=_detail_card("Англия"))

    scraper = _scraper_with_transport(monkeypatch, handler)
    first, second = _country_page(1, "14"), _country_page(2, "14")

    await scraper._enrich_listing_country_ids(first)

    assert all(product.manufacturer_country_raw == "14" for product in first)
    assert scraper.verified_country_mappings == {}

    await scraper._enrich_listing_country_ids(second)

    assert requested == ["/p1-1/", "/p1-2/", "/p2-1/", "/p2-2/"]
    for product in second:
        assert product.manufacturer_country_raw == "Англия"
        assert product.country_source == "aloe_country_id_verified_detail"
    assert scraper.verified_country_mappings["14"]["country_code"] == "gb"
    assert scraper.verified_country_mappings["14"]["sample_count"] == 2
    # Сбой карточки по-прежнему не повторяется и в счётчик повторов не идёт.
    assert scraper.fetch_retries == 0


async def test_aloe_page_that_is_not_a_card_does_not_close_the_id(monkeypatch) -> None:
    """Ответ 200 без страны и без остатка — не карточка: id не закрывается и на одном образце."""
    listing = (_FIXTURES / "aloe_bestseller.html").read_text()
    requested: list[str] = []

    async def fake_fetch(self, url: str) -> str:
        requested.append(url)
        return listing if url.endswith("/p1-1/") else _detail_card("Türkiyə-Almaniya")

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)

    with capture_logs() as seen:
        for page in (1, 2, 3):
            await scraper._enrich_listing_country_ids(_country_page(page, "7", size=1))

    assert requested == ["https://aloe.az/p1-1/", "https://aloe.az/p2-1/"]
    assert [row["retry"] for row in _unresolved_rows(seen)] == [True, False]
    failed = [row for row in seen if row["event"] == "aloe_country_detail_failed"]
    assert [row["url"] for row in failed] == ["https://aloe.az/p1-1/"]


async def test_aloe_unresolvable_label_with_unread_sample_waits_for_full_read(
    monkeypatch,
) -> None:
    """Одна карточка прочитана с негодной подписью, вторая упала — id ещё не закрыт."""
    requested: list[str] = []

    async def fake_fetch(self, url: str) -> str:
        requested.append(url)
        if url.endswith("/p1-2/"):
            raise httpx.ConnectError("connection refused")
        return _detail_card("Türkiyə-Almaniya")

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)

    with capture_logs() as seen:
        for page in (1, 2, 3):
            await scraper._enrich_listing_country_ids(_country_page(page, "7"))

    assert requested == [
        "https://aloe.az/p1-1/",
        "https://aloe.az/p1-2/",
        "https://aloe.az/p2-1/",
        "https://aloe.az/p2-2/",
    ]
    assert [row["retry"] for row in _unresolved_rows(seen)] == [True, False]
    assert scraper.verified_country_mappings == {}


async def test_aloe_silent_card_next_to_resolved_one_does_not_close_the_id(monkeypatch) -> None:
    """Одна карточка назвала страну, на другой подписи нет: молчание — не возражение."""
    requested: list[str] = []

    async def fake_fetch(self, url: str) -> str:
        requested.append(url)
        return _detail_card(None if url.endswith("/p1-2/") else "Англия")

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)
    first, second = _country_page(1, "14"), _country_page(2, "14")

    with capture_logs() as seen:
        await scraper._enrich_listing_country_ids(first)
        await scraper._enrich_listing_country_ids(second)

    assert len(requested) == 4
    assert all(product.manufacturer_country_raw == "14" for product in first)
    assert all(product.manufacturer_country_raw == "Англия" for product in second)
    assert scraper.verified_country_mappings["14"]["sample_count"] == 2
    unresolved = _unresolved_rows(seen)
    assert len(unresolved) == 1
    assert unresolved[0]["retry"] is True
    assert unresolved[0]["unread_details"] == 0
    assert unresolved[0]["unlabeled_details"] == 1


async def test_aloe_country_id_memory_is_per_id(monkeypatch) -> None:
    """Закрытый или сбойный id не мешает остальным id той же страницы."""
    cards = {"tr": "Türkiyə-Almaniya", "uk": "Англия", "de": "Германия"}
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        slug = request.url.path.strip("/")
        requested.append(slug)
        label = cards.get(slug[:2])
        if label is None:
            return httpx.Response(404)
        return httpx.Response(200, text=_detail_card(label))

    scraper = _scraper_with_transport(monkeypatch, handler)
    first = (
        _country_page(1, "7", tag="tr")
        + _country_page(1, "60", tag="gone", size=1)
        + _country_page(1, "14", tag="uk")
    )
    second = (
        _country_page(2, "7", tag="tr")
        + _country_page(2, "60", tag="gone", size=1)
        + _country_page(2, "14", tag="uk")
        + _country_page(2, "15", tag="de")
        + _country_page(2, "61", tag="lost", size=1)
    )

    with capture_logs() as seen:
        await scraper._enrich_listing_country_ids(first)
        await scraper._enrich_listing_country_ids(second)

    first_page = ["tr1-1", "tr1-2", "gone1-1", "uk1-1", "uk1-2"]
    # Вторая страница: 7 закрыт, 14 берётся из словаря, 60 перепроверяется.
    second_page = ["gone2-1", "de2-1", "de2-2", "lost2-1"]
    assert requested == first_page + second_page
    seen_countries: dict[str, set[str | None]] = {}
    for product in first + second:
        tag = product.external_id.rstrip("0123456789-")
        seen_countries.setdefault(tag, set()).add(product.manufacturer_country_raw)
    assert seen_countries == {
        "tr": {"7"},
        "gone": {"60"},
        # На первой странице 14 разрешился сразу, поэтому числа не осталось.
        "uk": {"Англия"},
        "de": {"Германия"},
        "lost": {"61"},
    }
    assert sorted(scraper.verified_country_mappings) == ["14", "15"]
    assert [(row["country_id"], row["retry"]) for row in _unresolved_rows(seen)] == [
        ("7", False),
        ("60", True),
        ("61", True),
    ]


async def test_aloe_unreadable_country_detail_is_retried_but_logged_once(monkeypatch) -> None:
    """Карточка 404 на каждой странице: запрос повторяется, итог по id в логе один."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(404)

    scraper = _scraper_with_transport(monkeypatch, handler)

    with capture_logs() as seen:
        for page in (1, 2, 3):
            await scraper._enrich_listing_country_ids(_country_page(page, "60", size=1))

    assert requested == ["/p1-1/", "/p2-1/", "/p3-1/"]
    assert [row["retry"] for row in _unresolved_rows(seen)] == [True]
    failed = [row for row in seen if row["event"] == "aloe_country_detail_failed"]
    assert [row["url"] for row in failed] == [f"https://aloe.az/p{page}-1/" for page in (1, 2, 3)]


async def test_aloe_resolvable_country_id_is_sampled_once_and_applied(monkeypatch) -> None:
    """Разрешимый id — как раньше: две карточки на первой странице, дальше из словаря."""
    requested: list[str] = []

    async def fake_fetch(self, url: str) -> str:
        requested.append(url)
        return _detail_card("Англия")

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)
    scraper = AloeScraper(rate_limit_sec=0)
    pages = [_country_page(page, "14") for page in (1, 2)]

    with capture_logs() as seen:
        for page in pages:
            await scraper._enrich_listing_country_ids(page)

    assert requested == ["https://aloe.az/p1-1/", "https://aloe.az/p1-2/"]
    for product in (product for page in pages for product in page):
        assert product.manufacturer_country_raw == "Англия"
        assert product.country_source == "aloe_country_id_verified_detail"
    assert scraper.verified_country_mappings["14"] == {
        "country_code": "gb",
        "country_raw": "Англия",
        "source_url": "https://aloe.az/p1-1/",
        "sample_count": 2,
    }
    assert _unresolved_rows(seen) == []


async def test_aloe_unresolvable_country_id_is_rechecked_by_next_run(monkeypatch) -> None:
    """Отказ живёт один сбор: сайт исправил подпись — следующий сбор её видит."""
    label = "Türkiyə-Almaniya"
    requested: list[str] = []

    async def fake_fetch(self, url: str) -> str:
        requested.append(url)
        return _detail_card(label)

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fake_fetch)

    first_run = AloeScraper(rate_limit_sec=0)
    await first_run._enrich_listing_country_ids(_country_page(1, "7"))
    assert first_run.verified_country_mappings == {}

    label = "Türkiyə"
    # Следующий сбор получает словарь из aloe_country_mappings, а туда уходит
    # только verified_country_mappings прошлого сбора — то есть ничего.
    next_run = AloeScraper(
        rate_limit_sec=0, country_id_map=dict(first_run.verified_country_mappings)
    )
    products = _country_page(1, "7")
    await next_run._enrich_listing_country_ids(products)

    assert len(requested) == 4
    assert all(product.manufacturer_country_raw == "Türkiyə" for product in products)
    assert next_run.verified_country_mappings["7"]["country_code"] == "tr"


async def test_aloe_unresolvable_country_id_costs_one_detail_per_scrape(monkeypatch) -> None:
    """Сквозь scrape_category: id 7 на двух страницах и в двух разделах — одна карточка."""
    detail_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/catalog/filters"):
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, text=_listing_page(page, current=page, last=2, country=7))
        detail_requests.append(request.url.path)
        return httpx.Response(200, text=_detail_card("Türkiyə-Almaniya"))

    scraper = _scraper_with_transport(monkeypatch, handler)

    products = [p async for p in scraper.scrape_category("dermanlar")]
    products += [p async for p in scraper.scrape_category("kosmetika")]

    assert detail_requests == ["/item-1/"]
    assert [p.external_id for p in products] == ["item-1", "item-2", "item-1", "item-2"]
    assert all(p.manufacturer_country_raw == "7" for p in products)
    assert all(p.country_source == "aloe_api_country_id" for p in products)
    assert scraper.verified_country_mappings == {}
    assert scraper._route_statuses["dermanlar"].complete is True
    assert scraper._route_statuses["kosmetika"].complete is True
