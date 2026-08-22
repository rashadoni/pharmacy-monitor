"""Fail-closed coverage tests for the guarded Pharmonline public API source."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest

from src.scrapers.pharmonline_public_api import (
    PUBLIC_API_AVAILABILITY_SOURCE,
    PUBLIC_CATALOG_ROUTE,
    PharmonlinePublicAPIError,
    PharmonlinePublicAPIScraper,
    _canonical_product_url,
    _configured_decodo_ports,
    _json_from_rendered_body,
    _same_origin_sitemap_url,
)
from src.scrapers.base import SiteScrapeFatalError


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
        self.crawlbase_sessions: list[tuple[str, str | None]] = []

    async def _crawlbase_json(
        self,
        target_url: str,
        *,
        crawlbase_session: str | None = None,
    ):
        self.requests.append(target_url)
        self.crawlbase_sessions.append((target_url, crawlbase_session))
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


def _three_pages() -> dict[int, dict]:
    return {
        1: {"data": [_raw_product(1), _raw_product(2)], "total": 5, "pages": 3},
        2: {"data": [_raw_product(3), _raw_product(4)], "total": 5, "pages": 3},
        3: {"data": [_raw_product(5)], "total": 5, "pages": 3},
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
async def test_public_api_catalog_does_not_require_strict_zip_runtime(monkeypatch):
    """Keep the recovery adapter usable on the existing production Python."""
    import src.scrapers.pharmonline_public_api as public_api_module

    native_zip = zip

    def legacy_zip(*iterables):
        return native_zip(*iterables)

    monkeypatch.setattr(public_api_module, "zip", legacy_zip, raising=False)
    expected_urls = {f"https://pharmonline.az/product/product-{index}" for index in (1, 2, 3)}
    scraper = _FakePublicAPIScraper(_pages(), sitemap_urls=expected_urls)

    products = [product async for product in scraper.scrape_category(PUBLIC_CATALOG_ROUTE)]

    assert [product.external_id for product in products] == [
        "PRODUCT0000000001",
        "PRODUCT0000000002",
        "PRODUCT0000000003",
    ]


@pytest.mark.asyncio
async def test_public_api_uses_a_fresh_sticky_session_for_each_bounded_page_group(
    monkeypatch,
):
    expected_urls = {f"https://pharmonline.az/product/product-{index}" for index in range(1, 6)}
    scraper = _FakePublicAPIScraper(_three_pages(), sitemap_urls=expected_urls)
    monkeypatch.setattr(
        "src.scrapers.pharmonline_public_api._CATALOG_SESSION_PAGE_SPAN",
        2,
    )

    products = [product async for product in scraper.scrape_category(PUBLIC_CATALOG_ROUTE)]

    assert len(products) == 5
    product_sessions = [
        (parse_qs(urlsplit(target_url).query)["page"][0], session)
        for target_url, session in scraper.crawlbase_sessions
        if "/api/products?" in target_url
    ]
    assert len(product_sessions) == 4
    assert [page for page, _ in product_sessions] == ["1", "2", "1", "3"]
    assert all(session is not None and len(session) == 32 for _, session in product_sessions)
    assert product_sessions[0][1] == product_sessions[1][1]
    assert product_sessions[1][1] != product_sessions[2][1]
    assert product_sessions[2][1] == product_sessions[3][1]


@pytest.mark.asyncio
async def test_public_api_rejects_a_chunk_when_its_page_one_anchor_changes(monkeypatch):
    expected_urls = {f"https://pharmonline.az/product/product-{index}" for index in range(1, 6)}

    class _ChangingAnchorScraper(_FakePublicAPIScraper):
        first_product_session: str | None = None

        async def _crawlbase_json(
            self,
            target_url: str,
            *,
            crawlbase_session: str | None = None,
        ):
            payload = await super()._crawlbase_json(
                target_url,
                crawlbase_session=crawlbase_session,
            )
            if "/api/products?" not in target_url:
                return payload
            page = int(parse_qs(urlsplit(target_url).query)["page"][0])
            if page != 1:
                return payload
            if self.first_product_session is None:
                self.first_product_session = crawlbase_session
                return payload
            if crawlbase_session != self.first_product_session:
                return {
                    "data": [_raw_product(90), _raw_product(91)],
                    "total": 5,
                    "pages": 3,
                }
            return payload

    scraper = _ChangingAnchorScraper(_three_pages(), sitemap_urls=expected_urls)
    monkeypatch.setattr(
        "src.scrapers.pharmonline_public_api._CATALOG_SESSION_PAGE_SPAN",
        2,
    )

    products = [product async for product in scraper.scrape_category(PUBLIC_CATALOG_ROUTE)]

    assert products == []
    status = scraper._route_statuses[PUBLIC_CATALOG_ROUTE]
    assert status.complete is False
    assert status.abort_reason == "products_chunk_anchor_changed"


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


def test_decodo_ports_require_pharmonline_and_cap_large_ranges(monkeypatch):
    monkeypatch.setenv("DECODO_SITES", "pharmonline,aptekonline")
    monkeypatch.setenv("DECODO_PORTS", "30001-30003,30002")
    assert _configured_decodo_ports() == (30001, 30002, 30003)

    monkeypatch.setenv("DECODO_SITES", "aptekonline")
    with pytest.raises(SiteScrapeFatalError, match="not configured"):
        _configured_decodo_ports()

    monkeypatch.setenv("DECODO_SITES", "pharmonline")
    monkeypatch.setenv("DECODO_PORTS", "1-65")
    with pytest.raises(SiteScrapeFatalError, match="too many"):
        _configured_decodo_ports()


@pytest.mark.asyncio
async def test_decodo_transport_uses_isolated_logical_contexts(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_TRANSPORT", "decodo")
    monkeypatch.setenv("DECODO_USERNAME", "user")
    monkeypatch.setenv("DECODO_PASSWORD", "pass")
    monkeypatch.setenv("DECODO_SITES", "pharmonline")
    monkeypatch.setenv("DECODO_PORTS", "30001,30002")

    scraper = PharmonlinePublicAPIScraper()
    await scraper.__aenter__()
    try:
        first_chunk = scraper._catalog_session_for_page(1)
        second_chunk = scraper._catalog_session_for_page(11)
        sitemap = scraper._sitemap_session()

        assert first_chunk.startswith("decodo-catalog-0-")
        assert second_chunk.startswith("decodo-catalog-1-")
        assert sitemap.startswith("decodo-sitemap-")
        assert scraper._decodo_port_for_context(first_chunk) == 30001
        assert scraper._decodo_port_for_context(first_chunk) == 30001
        assert scraper._decodo_port_for_context(second_chunk) == 30002
        # The pool can be smaller than the number of contexts; reuse still
        # receives a new client/cookie jar and anchors remain mandatory.
        assert scraper._decodo_port_for_context(sitemap) == 30001
        first_proxy = urlsplit(scraper._decodo_proxy_url_for_context(first_chunk, 30001))
        assert first_proxy.hostname == "az.decodo.com"
        assert first_proxy.port == 30001
        assert first_proxy.username == "user"

        first_client = scraper._decodo_client_for_context(first_chunk, 30001)
        assert scraper._decodo_client_for_context(first_chunk, 30001) is first_client
        assert scraper._decodo_client_for_context(sitemap, 30001) is not first_client
    finally:
        await scraper.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_decodo_backconnect_uses_a_named_sticky_session_per_context(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_TRANSPORT", "decodo")
    monkeypatch.setenv("PHARMONLINE_DECODO_BACKCONNECT_STICKY", "1")
    monkeypatch.setenv("DECODO_USERNAME", "proxy-user")
    monkeypatch.setenv("DECODO_PASSWORD", "pass")
    monkeypatch.setenv("DECODO_SITES", "pharmonline")
    monkeypatch.setenv("DECODO_HOST", "not-used.example")
    monkeypatch.setenv("DECODO_PORTS", "not-a-port")

    scraper = PharmonlinePublicAPIScraper()
    await scraper.__aenter__()
    try:
        first_context = scraper._catalog_session_for_page(1)
        second_context = scraper._catalog_session_for_page(11)
        assert scraper._decodo_port_for_context(first_context) == 7000
        assert scraper._decodo_port_for_context(second_context) == 7000

        first_proxy = urlsplit(scraper._decodo_proxy_url_for_context(first_context, 7000))
        second_proxy = urlsplit(scraper._decodo_proxy_url_for_context(second_context, 7000))
        first_username = first_proxy.username or ""
        second_username = second_proxy.username or ""

        assert first_proxy.hostname == second_proxy.hostname == "gate.decodo.com"
        assert first_proxy.port == second_proxy.port == 7000
        assert first_username.startswith("user-proxy-user-country-az-session-")
        assert first_username.endswith("-sessionduration-30")
        assert first_username != second_username
        first_session = first_username.removeprefix(
            "user-proxy-user-country-az-session-"
        ).removesuffix("-sessionduration-30")
        assert len(first_session) == 24
        assert all(character in "0123456789abcdef" for character in first_session)
    finally:
        await scraper.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_decodo_backconnect_rejects_unknown_flag_value(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_TRANSPORT", "decodo")
    monkeypatch.setenv("PHARMONLINE_DECODO_BACKCONNECT_STICKY", "sometimes")
    monkeypatch.setenv("DECODO_USERNAME", "proxy-user")
    monkeypatch.setenv("DECODO_PASSWORD", "pass")
    monkeypatch.setenv("DECODO_SITES", "pharmonline")

    with pytest.raises(SiteScrapeFatalError, match="backconnect mode is invalid"):
        await PharmonlinePublicAPIScraper().__aenter__()


@pytest.mark.asyncio
async def test_scraperapi_transport_uses_isolated_named_sticky_sessions(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_TRANSPORT", "scraperapi")
    monkeypatch.setenv("SCRAPER_API_KEY", "proxy:key@value")
    monkeypatch.setenv("SCRAPER_API_SITES", "pharmonline")

    scraper = PharmonlinePublicAPIScraper()
    await scraper.__aenter__()
    try:
        first_context = scraper._catalog_session_for_page(1)
        second_context = scraper._catalog_session_for_page(11)
        sitemap_context = scraper._sitemap_session()

        assert first_context.startswith("scraperapi-catalog-0-")
        assert second_context.startswith("scraperapi-catalog-1-")
        assert sitemap_context.startswith("scraperapi-sitemap-")

        first_proxy = urlsplit(scraper._scraperapi_proxy_url_for_context(first_context))
        second_proxy = urlsplit(scraper._scraperapi_proxy_url_for_context(second_context))
        assert first_proxy.hostname == second_proxy.hostname == "proxy-server.scraperapi.com"
        assert first_proxy.port == second_proxy.port == 8001
        first_username = unquote(first_proxy.username or "")
        second_username = unquote(second_proxy.username or "")
        prefix = "scraperapi.session_number="
        assert first_username.startswith(prefix)
        assert second_username.startswith(prefix)
        assert first_username.removeprefix(prefix).isdigit()
        assert second_username.removeprefix(prefix).isdigit()
        assert first_username != second_username
        assert unquote(first_proxy.password or "") == "proxy:key@value"

        first_client = scraper._scraperapi_client_for_context(first_context)
        assert scraper._scraperapi_client_for_context(first_context) is first_client
        assert scraper._scraperapi_client_for_context(sitemap_context) is not first_client
    finally:
        await scraper.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_scraperapi_transport_rejects_premium_with_sticky_sessions(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_TRANSPORT", "scraperapi")
    monkeypatch.setenv("SCRAPER_API_KEY", "proxy-key")
    monkeypatch.setenv("SCRAPER_API_SITES", "pharmonline")
    monkeypatch.setenv("SCRAPER_API_PREMIUM_SITES", "pharmonline")

    with pytest.raises(SiteScrapeFatalError, match="premium mode cannot be combined"):
        await PharmonlinePublicAPIScraper().__aenter__()


@pytest.mark.asyncio
async def test_scraperapi_transport_rejects_unproven_global_country_targeting(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_TRANSPORT", "scraperapi")
    monkeypatch.setenv("SCRAPER_API_KEY", "proxy-key")
    monkeypatch.setenv("SCRAPER_API_SITES", "pharmonline")
    monkeypatch.setenv("SCRAPER_API_COUNTRY", "az")

    with pytest.raises(SiteScrapeFatalError, match="country targeting is not enabled"):
        await PharmonlinePublicAPIScraper().__aenter__()


@pytest.mark.asyncio
async def test_scraperapi_transport_requires_explicit_pharmonline_scope(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_TRANSPORT", "scraperapi")
    monkeypatch.setenv("SCRAPER_API_KEY", "proxy-key")
    monkeypatch.setenv("SCRAPER_API_SITES", "aptekonline")

    with pytest.raises(SiteScrapeFatalError, match="not configured for Pharmonline"):
        await PharmonlinePublicAPIScraper().__aenter__()


@pytest.mark.asyncio
async def test_decodo_direct_request_keeps_one_context_on_retry(monkeypatch):
    scraper = PharmonlinePublicAPIScraper()
    scraper._decodo_ports = (30001,)
    scraper._decodo_session_ports = {"decodo-catalog-test": 30001}

    class _Response:
        status_code = 200
        headers = {"cache-control": "public, max-age=60"}
        text = '{"data": []}'

    class _Client:
        calls = 0

        async def get(self, _url: str, *, headers: dict[str, str]) -> _Response:
            self.calls += 1
            assert headers == {"accept": "application/json"}
            if self.calls == 1:
                raise httpx.ReadTimeout("transient")
            return _Response()

    async def no_delay(_seconds: float) -> None:
        return None

    client = _Client()
    scraper._decodo_clients = {"decodo-catalog-test": client}
    monkeypatch.setattr("src.scrapers.pharmonline_public_api.asyncio.sleep", no_delay)

    assert (
        await scraper._decodo_body(
            "https://pharmonline.az/api/products?lng=az&page=1",
            accept="application/json",
            crawlbase_session="decodo-catalog-test",
        )
        == '{"data": []}'
    )
    assert client.calls == 2
    assert scraper._decodo_session_ports == {"decodo-catalog-test": 30001}
    assert scraper._origin_context_evidence() == "/api/products:1"


@pytest.mark.asyncio
async def test_decodo_direct_request_retries_a_closed_remote_connection(monkeypatch):
    scraper = PharmonlinePublicAPIScraper()
    scraper._decodo_ports = (30001,)
    scraper._decodo_session_ports = {"decodo-catalog-test": 30001}

    class _Response:
        status_code = 200
        headers = {"cache-control": "public, max-age=60"}
        text = '{"data": []}'

    class _Client:
        calls = 0

        async def get(self, _url: str, *, headers: dict[str, str]) -> _Response:
            self.calls += 1
            assert headers == {"accept": "application/json"}
            if self.calls == 1:
                raise httpx.RemoteProtocolError("peer closed connection")
            return _Response()

    async def no_delay(_seconds: float) -> None:
        return None

    client = _Client()
    scraper._decodo_clients = {"decodo-catalog-test": client}
    monkeypatch.setattr("src.scrapers.pharmonline_public_api.asyncio.sleep", no_delay)

    assert (
        await scraper._decodo_body(
            "https://pharmonline.az/api/products?lng=az&page=2",
            accept="application/json",
            crawlbase_session="decodo-catalog-test",
        )
        == '{"data": []}'
    )
    assert client.calls == 2
    assert scraper._decodo_session_ports == {"decodo-catalog-test": 30001}


@pytest.mark.asyncio
async def test_scraperapi_direct_request_retries_in_the_same_sticky_context(monkeypatch):
    scraper = PharmonlinePublicAPIScraper()

    class _Response:
        status_code = 200
        headers = {"cache-control": "public, max-age=60"}
        text = '{"data": []}'

    class _Client:
        calls = 0

        async def get(self, _url: str, *, headers: dict[str, str]) -> _Response:
            self.calls += 1
            assert headers == {"accept": "application/json"}
            if self.calls == 1:
                raise httpx.RemoteProtocolError("peer closed connection")
            return _Response()

    async def no_delay(_seconds: float) -> None:
        return None

    client = _Client()
    scraper._scraperapi_clients = {"scraperapi-catalog-test": client}
    monkeypatch.setattr("src.scrapers.pharmonline_public_api.asyncio.sleep", no_delay)

    assert (
        await scraper._scraperapi_body(
            "https://pharmonline.az/api/products?lng=az&page=1",
            accept="application/json",
            crawlbase_session="scraperapi-catalog-test",
        )
        == '{"data": []}'
    )
    assert client.calls == 2
    assert scraper._origin_context_evidence() == "/api/products:1"


@pytest.mark.asyncio
async def test_scraperapi_retries_a_transient_gateway_status_in_the_same_sticky_context(
    monkeypatch,
):
    scraper = PharmonlinePublicAPIScraper()

    class _Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.headers = {"cache-control": "public, max-age=60"}
            self.text = '{"data": []}'

    class _Client:
        calls = 0

        async def get(self, _url: str, *, headers: dict[str, str]) -> _Response:
            self.calls += 1
            assert headers == {"accept": "application/json"}
            return _Response(499 if self.calls == 1 else 200)

    async def no_delay(_seconds: float) -> None:
        return None

    client = _Client()
    scraper._scraperapi_clients = {"scraperapi-catalog-test": client}
    monkeypatch.setattr("src.scrapers.pharmonline_public_api.asyncio.sleep", no_delay)

    assert (
        await scraper._scraperapi_body(
            "https://pharmonline.az/api/products?lng=az&page=1",
            accept="application/json",
            crawlbase_session="scraperapi-catalog-test",
        )
        == '{"data": []}'
    )
    assert client.calls == 2
    assert scraper._origin_context_evidence() == "/api/products:1"


@pytest.mark.asyncio
async def test_source_json_selects_decodo_only_when_explicit(monkeypatch):
    scraper = PharmonlinePublicAPIScraper()
    scraper._public_api_transport = "decodo"
    calls: list[tuple[str, str, str | None]] = []

    async def decodo_body(
        target_url: str,
        *,
        accept: str,
        crawlbase_session: str | None = None,
    ) -> str:
        calls.append((target_url, accept, crawlbase_session))
        return '{"data": []}'

    monkeypatch.setattr(scraper, "_decodo_body", decodo_body)
    assert await scraper._source_json(
        "https://pharmonline.az/api/products?lng=az&page=1",
        crawlbase_session="decodo-catalog-test",
    ) == {"data": []}
    assert calls == [
        (
            "https://pharmonline.az/api/products?lng=az&page=1",
            "application/json",
            "decodo-catalog-test",
        )
    ]


@pytest.mark.asyncio
async def test_source_json_selects_scraperapi_only_when_explicit(monkeypatch):
    scraper = PharmonlinePublicAPIScraper()
    scraper._public_api_transport = "scraperapi"
    calls: list[tuple[str, str, str | None]] = []

    async def scraperapi_body(
        target_url: str,
        *,
        accept: str,
        crawlbase_session: str | None = None,
    ) -> str:
        calls.append((target_url, accept, crawlbase_session))
        return '{"data": []}'

    monkeypatch.setattr(scraper, "_scraperapi_body", scraperapi_body)
    assert await scraper._source_json(
        "https://pharmonline.az/api/products?lng=az&page=1",
        crawlbase_session="scraperapi-catalog-test",
    ) == {"data": []}
    assert calls == [
        (
            "https://pharmonline.az/api/products?lng=az&page=1",
            "application/json",
            "scraperapi-catalog-test",
        )
    ]


@pytest.mark.asyncio
async def test_source_xml_selects_scraperapi_only_when_explicit(monkeypatch):
    scraper = PharmonlinePublicAPIScraper()
    scraper._public_api_transport = "scraperapi"
    calls: list[tuple[str, str, str | None]] = []

    async def scraperapi_body(
        target_url: str,
        *,
        accept: str,
        crawlbase_session: str | None = None,
    ) -> str:
        calls.append((target_url, accept, crawlbase_session))
        return "<urlset />"

    monkeypatch.setattr(scraper, "_scraperapi_body", scraperapi_body)
    assert (
        await scraper._source_xml(
            "https://pharmonline.az/sitemap.xml",
            crawlbase_session="scraperapi-sitemap-test",
        )
        == "<urlset />"
    )
    assert calls == [
        (
            "https://pharmonline.az/sitemap.xml",
            "application/xml,text/xml;q=0.9,*/*;q=0.8",
            "scraperapi-sitemap-test",
        )
    ]


@pytest.mark.asyncio
async def test_scraperapi_exit_closes_and_forgets_context_clients():
    scraper = PharmonlinePublicAPIScraper()

    class _Client:
        close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    first_client = _Client()
    second_client = _Client()
    scraper._scraperapi_clients = {
        "scraperapi-catalog-test": first_client,
        "scraperapi-sitemap-test": second_client,
    }
    scraper._scraperapi_session_numbers = {
        "scraperapi-catalog-test": 1,
        "scraperapi-sitemap-test": 2,
    }

    await scraper.__aexit__(None, None, None)

    assert first_client.close_calls == second_client.close_calls == 1
    assert scraper._scraperapi_clients == {}
    assert scraper._scraperapi_session_numbers == {}


@pytest.mark.asyncio
async def test_decodo_direct_request_rejects_exhausted_proxy_without_url_leakage():
    scraper = PharmonlinePublicAPIScraper()
    scraper._decodo_ports = (30001,)
    scraper._decodo_session_ports = {"decodo-catalog-test": 30001}

    class _Response:
        status_code = 407
        headers: dict[str, str] = {}
        text = "proxy rejected"

    class _Client:
        async def get(self, _url: str, *, headers: dict[str, str]) -> _Response:
            return _Response()

    scraper._decodo_clients = {"decodo-catalog-test": _Client()}
    with pytest.raises(SiteScrapeFatalError, match="HTTP 407") as exc_info:
        await scraper._decodo_body(
            "https://pharmonline.az/api/products?lng=az&page=1",
            accept="application/json",
            crawlbase_session="decodo-catalog-test",
        )
    assert "@" not in str(exc_info.value)


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

    async def external_index(_url: str, *, crawlbase_session: str | None = None) -> str:
        return (
            "<sitemapindex><sitemap><loc>https://example.test/"
            "sitemap-products-1.xml</loc></sitemap></sitemapindex>"
        )

    monkeypatch.setattr(scraper, "_crawlbase_xml", external_index)
    with pytest.raises(PharmonlinePublicAPIError, match="product_sitemap_index_url_invalid"):
        await scraper._fetch_sitemap_product_urls()

    async def external_product(url: str, *, crawlbase_session: str | None = None) -> str:
        if url.endswith("/sitemap.xml"):
            return (
                "<sitemapindex><sitemap><loc>/sitemap-products-1.xml</loc></sitemap></sitemapindex>"
            )
        return "<urlset><url><loc>https://example.test/product/not-safe</loc></url></urlset>"

    monkeypatch.setattr(scraper, "_crawlbase_xml", external_product)
    with pytest.raises(PharmonlinePublicAPIError, match="product_sitemap_product_url_invalid"):
        await scraper._fetch_sitemap_product_urls()


@pytest.mark.asyncio
async def test_crawlbase_requests_keep_one_sticky_session_and_origin_header_evidence():
    scraper = PharmonlinePublicAPIScraper()
    scraper._crawlbase_token = "test-token"
    scraper._crawlbase_session = "a" * 32

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "cb_status": "200",
                "original_status": "200",
                "body": {"data": []},
                "original_headers": {
                    "Cache-Control": "public, max-age=60",
                    "CF-Cache-Status": "HIT",
                },
            }

    class _Client:
        params: dict | None = None

        async def get(self, _url: str, *, params: dict) -> _Response:
            self.params = params
            return _Response()

    client = _Client()
    scraper._client = client

    assert await scraper._crawlbase_body(
        "https://pharmonline.az/api/products?lng=az&page=1",
        accept="application/json",
    ) == {"data": []}
    assert client.params is not None
    assert client.params["cookies_session"] == "a" * 32
    assert client.params["get_headers"] == "true"
    assert scraper._origin_context_evidence() == "/api/products:1"


@pytest.mark.asyncio
async def test_crawlbase_retries_one_timeout_in_the_same_explicit_chunk_session(monkeypatch):
    scraper = PharmonlinePublicAPIScraper()
    scraper._crawlbase_token = "test-token"
    scraper._crawlbase_session = "b" * 32

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"cb_status": "200", "original_status": "200", "body": {"data": []}}

    class _Client:
        calls = 0
        sessions: list[str] = []

        async def get(self, _url: str, *, params: dict) -> _Response:
            self.calls += 1
            self.sessions.append(params["cookies_session"])
            if self.calls == 1:
                raise httpx.ReadTimeout("transient")
            return _Response()

    async def no_delay(_seconds: float) -> None:
        return None

    client = _Client()
    scraper._client = client
    monkeypatch.setattr("src.scrapers.pharmonline_public_api.asyncio.sleep", no_delay)

    assert await scraper._crawlbase_body(
        "https://pharmonline.az/api/products?lng=az&page=52",
        accept="application/json",
        crawlbase_session="c" * 32,
    ) == {"data": []}
    assert client.calls == 2
    assert client.sessions == ["c" * 32, "c" * 32]


@pytest.mark.asyncio
async def test_sitemap_fetch_deduplicates_repeated_canonical_product_urls(monkeypatch):
    scraper = PharmonlinePublicAPIScraper()
    scraper._crawlbase_session = "a" * 32
    sitemap_sessions: list[str | None] = []

    async def duplicated_product_urls(url: str, *, crawlbase_session: str | None = None) -> str:
        sitemap_sessions.append(crawlbase_session)
        if url.endswith("/sitemap.xml"):
            return (
                "<sitemapindex>"
                "<sitemap><loc>/sitemap-products-1.xml</loc></sitemap>"
                "<sitemap><loc>/sitemap-products-2.xml</loc></sitemap>"
                "</sitemapindex>"
            )
        if url.endswith("sitemap-products-1.xml"):
            return (
                "<urlset>"
                "<url><loc>/product/one</loc></url>"
                "<url><loc>/product/two</loc></url>"
                "</urlset>"
            )
        return (
            "<urlset>"
            "<url><loc>https://pharmonline.az/product/two</loc></url>"
            "<url><loc>/product/three</loc></url>"
            "</urlset>"
        )

    monkeypatch.setattr(scraper, "_crawlbase_xml", duplicated_product_urls)

    assert await scraper._fetch_sitemap_product_urls() == {
        "https://pharmonline.az/product/one",
        "https://pharmonline.az/product/two",
        "https://pharmonline.az/product/three",
    }
    assert len(sitemap_sessions) == 3
    assert all(session is not None and len(session) == 32 for session in sitemap_sessions)
    assert len(set(sitemap_sessions)) == 1
    assert sitemap_sessions[0] != scraper._crawlbase_session


def test_crawlbase_json_body_accepts_direct_and_pre_rendered_json():
    payload = {"data": [{"_id": "PRODUCT0000000001"}]}
    assert _json_from_rendered_body(payload) == payload
    assert _json_from_rendered_body(json.dumps(payload)) == payload
    assert _json_from_rendered_body(f"<html><pre>{json.dumps(payload)}</pre></html>") == payload
