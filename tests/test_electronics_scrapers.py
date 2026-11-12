"""Сборщики магазинов электроники: Kontakt Home, Irshad, Baku Electronics.

Сети и браузера нет: сайт изображает `httpx.MockTransport`. Формы ответов сняты
с живых сайтов 2026-11-12 — карточки Irshad лежат фикстурой как есть
(`fixtures/irshad_list_page.html`), записи Kontakt и Baku Electronics повторяют
поля настоящих ответов.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from src.scrapers import http_catalog
from src.scrapers.bakuelectronics import BakuElectronicsScraper
from src.scrapers.bakuelectronics import build_product as build_be_product
from src.scrapers.base import CaptchaDetected, SiteScrapeFatalError
from src.scrapers.electronics import CLIENT_SITE, ELECTRONICS_SCRAPERS
from src.scrapers.http_catalog import (
    PageFetchError,
    looks_like_protection_page,
    price_or_none,
    sale_fields,
)
from src.scrapers.irshad import IrshadScraper, build_products, split_cards
from src.scrapers.kontakt import (
    KontaktScraper,
    category_path_from_link,
    decode_turbo_stream,
)
from src.scrapers.kontakt import build_product as build_kontakt_product

FIXTURES = Path(__file__).parent / "fixtures"

CLOUDFLARE_CHALLENGE = "<html><head><title>Just a moment...</title></head><body></body></html>"
CLOUDFLARE_BLOCK = (
    "<html><head><title>Attention Required! | Cloudflare</title></head>"
    "<body>Sorry, you have been blocked</body></html>"
)


# -- помощники ------------------------------------------------------------------


def serve(scraper, handler):
    """Подменить сеть сборщика: каждый запрос отвечает `handler`.

    Возвращает список полученных запросов. Клиент строится тем же методом, что
    в работе, поэтому заголовки сборщика и адрес прокси доходят до `handler`
    (адрес прокси — в `request.extensions["pm_proxy"]`).
    """
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def build_client(proxy_url):
        def with_proxy(request: httpx.Request) -> httpx.Response:
            request.extensions["pm_proxy"] = proxy_url
            return recording(request)

        return httpx.AsyncClient(
            headers=dict(scraper.request_headers),
            transport=httpx.MockTransport(with_proxy),
            follow_redirects=True,
        )

    scraper._build_client = build_client
    return seen


def fast(cls, **kwargs):
    """Сборщик без пауз между запросами."""
    return cls(rate_limit_sec=0, **kwargs)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(http_catalog.asyncio, "sleep", instant)


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    for name in ("DECODO_USERNAME", "DECODO_PASSWORD", "DECODO_SITES", "DECODO_PORTS"):
        monkeypatch.delenv(name, raising=False)


def turbo_encode(value) -> str:
    """Записать значение так, как его отдаёт `.data` (turbo-stream)."""
    flat: list = []

    def put(item) -> int:
        if item is None:
            return -5
        index = len(flat)
        flat.append(None)
        if isinstance(item, dict):
            encoded = {}
            for key, child in item.items():
                key_index = len(flat)
                flat.append(key)
                encoded[f"_{key_index}"] = put(child)
            flat[index] = encoded
        elif isinstance(item, list):
            flat[index] = [put(child) for child in item]
        else:
            flat[index] = item
        return index

    put(value)
    return json.dumps(flat, ensure_ascii=False) + "\n"


async def collect(scraper, slug, limit=None):
    return [product async for product in scraper.scrape_category(slug, limit=limit)]


# -- общая основа ---------------------------------------------------------------


def test_registry_names_the_client_and_both_competitors():
    assert CLIENT_SITE == "kontakt"
    assert set(ELECTRONICS_SCRAPERS) == {"kontakt", "irshad", "bakuelectronics"}
    for site, cls in ELECTRONICS_SCRAPERS.items():
        assert cls.site_name == site


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (949.99, 949.99),
        ("949.99 AZN", 949.99),
        ("1 199,50 ₼", 1199.5),
        (0, None),
        (-5, None),
        ("", None),
        (None, None),
        (True, None),
        ("pulsuz", None),
        (float("nan"), None),
    ],
)
def test_price_or_none(raw, expected):
    assert price_or_none(raw) == expected


def test_sale_fields_only_call_a_lower_price_a_sale():
    assert sale_fields(100.0, 80.0) == (100.0, 80.0, 20.0, True)
    assert sale_fields(100.0, 100.0) == (100.0, None, None, False)
    assert sale_fields(100.0, 120.0) == (100.0, None, None, False)
    assert sale_fields(100.0, None) == (100.0, None, None, False)
    assert sale_fields(None, 80.0) == (80.0, None, None, False)


def test_protection_page_is_recognised_and_an_ordinary_page_is_not():
    assert looks_like_protection_page(CLOUDFLARE_CHALLENGE)
    assert looks_like_protection_page(CLOUDFLARE_BLOCK)
    ordinary = (
        "<html><head><title>Televizorlar</title></head><body>"
        '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></body></html>'
    )
    assert not looks_like_protection_page(ordinary)


async def test_no_browser_is_started(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("the HTTP scrapers must not start a browser")

    monkeypatch.setattr("src.scrapers.base.async_playwright", forbidden)
    for cls in ELECTRONICS_SCRAPERS.values():
        async with fast(cls) as scraper:
            assert scraper._browser is None


async def test_fetch_retries_a_transient_answer_and_counts_it():
    scraper = fast(IrshadScraper)
    answers = iter([httpx.Response(502), httpx.Response(200, text="ok")])
    seen = serve(scraper, lambda _request: next(answers))
    response = await scraper.fetch("https://irshad.az/az")
    assert response.text == "ok"
    assert len(seen) == 2
    assert scraper.fetch_retries == 1


async def test_fetch_does_not_retry_a_permanent_refusal():
    scraper = fast(IrshadScraper)
    seen = serve(scraper, lambda _request: httpx.Response(404, text="yoxdur"))
    with pytest.raises(PageFetchError) as error:
        await scraper.fetch("https://irshad.az/az/yoxdur")
    assert error.value.status == 404
    assert len(seen) == 1


async def test_fetch_reports_a_protection_page_as_such():
    scraper = fast(KontaktScraper)
    serve(scraper, lambda _request: httpx.Response(403, text=CLOUDFLARE_CHALLENGE))
    with pytest.raises(CaptchaDetected):
        await scraper.fetch("https://kontakt.az/x.data")


async def test_a_protection_page_with_status_200_is_not_taken_for_content():
    scraper = fast(KontaktScraper)
    serve(scraper, lambda _request: httpx.Response(200, text=CLOUDFLARE_CHALLENGE))
    with pytest.raises(CaptchaDetected):
        await scraper.fetch("https://kontakt.az/x.data")


async def test_fetch_gives_up_on_network_errors_without_calling_it_protection():
    scraper = fast(IrshadScraper)

    def broken(request):
        raise httpx.ConnectError("no route", request=request)

    serve(scraper, broken)
    with pytest.raises(PageFetchError) as error:
        await scraper.fetch("https://irshad.az/az")
    assert "network error" in error.value.reason


def _decodo(monkeypatch, sites="kontakt,bakuelectronics", ports="30001-30003"):
    monkeypatch.setenv("DECODO_USERNAME", "user")
    monkeypatch.setenv("DECODO_PASSWORD", "p=ss")
    monkeypatch.setenv("DECODO_SITES", sites)
    monkeypatch.setenv("DECODO_PORTS", ports)


async def test_proxy_is_used_only_for_the_sites_it_is_configured_for(monkeypatch):
    _decodo(monkeypatch)
    kontakt = fast(KontaktScraper)
    seen = serve(kontakt, lambda _request: httpx.Response(200, text="ok"))
    await kontakt.fetch("https://kontakt.az/x.data")
    assert ":30001" in seen[0].extensions["pm_proxy"]

    irshad = fast(IrshadScraper)
    seen = serve(irshad, lambda _request: httpx.Response(200, text="ok"))
    await irshad.fetch("https://irshad.az/az")
    assert seen[0].extensions["pm_proxy"] is None


async def test_a_blocked_exit_is_replaced_and_the_working_one_is_kept(monkeypatch):
    _decodo(monkeypatch)
    scraper = fast(KontaktScraper)

    def by_exit(request):
        if ":30001" in request.extensions["pm_proxy"]:
            return httpx.Response(403, text=CLOUDFLARE_CHALLENGE)
        return httpx.Response(200, text="ok")

    seen = serve(scraper, by_exit)
    await scraper.fetch("https://kontakt.az/a.data")
    await scraper.fetch("https://kontakt.az/b.data")
    exits = [request.extensions["pm_proxy"].rsplit(":", 1)[1] for request in seen]
    assert exits == ["30001", "30002", "30002"]


async def test_a_503_challenge_is_protection_not_a_server_fault():
    scraper = fast(KontaktScraper)
    serve(scraper, lambda _request: httpx.Response(503, text=CLOUDFLARE_CHALLENGE))
    with pytest.raises(CaptchaDetected):
        await scraper.fetch("https://kontakt.az/x.data")


async def test_a_refusal_without_proxy_is_not_asked_again():
    # Тот же адрес получит тот же отказ: шесть запросов на раздел ни к чему.
    scraper = fast(IrshadScraper)
    seen = serve(scraper, lambda _request: httpx.Response(403, text=CLOUDFLARE_BLOCK))
    with pytest.raises(CaptchaDetected):
        await scraper.fetch("https://irshad.az/az")
    assert len(seen) == 1


async def test_a_bare_403_through_proxy_tries_other_exits_and_is_called_a_refusal(monkeypatch):
    _decodo(monkeypatch)
    scraper = fast(KontaktScraper)
    seen = serve(scraper, lambda _request: httpx.Response(403, text="error code: 1020"))
    with pytest.raises(CaptchaDetected):
        await scraper.fetch("https://kontakt.az/a.data")
    exits = [request.extensions["pm_proxy"].rsplit(":", 1)[1] for request in seen]
    assert exits == ["30001", "30002", "30003"]


async def test_a_refusal_on_one_exit_decides_even_if_the_last_exit_only_timed_out(monkeypatch):
    _decodo(monkeypatch)
    scraper = fast(KontaktScraper)

    def by_exit(request):
        if ":30003" in request.extensions["pm_proxy"]:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(403, text=CLOUDFLARE_CHALLENGE)

    serve(scraper, by_exit)
    with pytest.raises(CaptchaDetected):
        await scraper.fetch("https://kontakt.az/a.data")


async def test_a_single_exit_still_retries_a_transient_answer(monkeypatch):
    _decodo(monkeypatch, ports="30001")
    scraper = fast(KontaktScraper)
    answers = iter([httpx.Response(502), httpx.Response(200, text="ok")])
    seen = serve(scraper, lambda _request: next(answers))
    assert (await scraper.fetch("https://kontakt.az/a.data")).text == "ok"
    assert len(seen) == 2


async def test_the_next_category_pass_starts_from_an_exit_not_used_yet(monkeypatch):
    _decodo(monkeypatch)
    scraper = fast(KontaktScraper)

    def by_exit(request):
        if ":30001" in request.extensions["pm_proxy"]:
            return httpx.Response(403, text=CLOUDFLARE_CHALLENGE)
        return httpx.Response(200, text="ok")

    seen = serve(scraper, by_exit)
    await scraper.fetch("https://kontakt.az/a.data")  # 30001 закрыт, перешли на 30002
    scraper._rotate_session()
    await scraper.fetch("https://kontakt.az/b.data")
    exits = [request.extensions["pm_proxy"].rsplit(":", 1)[1] for request in seen]
    assert exits == ["30001", "30002", "30003"]


async def test_a_proxy_that_refuses_the_tunnel_stops_the_site(monkeypatch):
    # Так отказ выглядит на деле: прокси отвечает 407 на CONNECT, и до сайта
    # запрос не доходит — httpx бросает ProxyError.
    _decodo(monkeypatch)
    scraper = fast(KontaktScraper)

    def refused(request):
        raise httpx.ProxyError("407 Proxy Authentication Required", request=request)

    seen = serve(scraper, refused)
    with pytest.raises(SiteScrapeFatalError):
        await scraper.fetch("https://kontakt.az/a.data")
    assert len(seen) == 1


@pytest.mark.parametrize("status", [402, 407])
async def test_a_rejected_proxy_account_stops_the_site(monkeypatch, status):
    _decodo(monkeypatch)
    scraper = fast(KontaktScraper)
    seen = serve(scraper, lambda _request: httpx.Response(status))
    with pytest.raises(SiteScrapeFatalError):
        await scraper.fetch("https://kontakt.az/a.data")
    assert len(seen) == 1


@pytest.mark.parametrize("error_class", [httpx.ProxyError, httpx.ConnectError])
async def test_the_proxy_password_never_reaches_an_error_message(monkeypatch, error_class):
    _decodo(monkeypatch)
    scraper = fast(KontaktScraper)

    def broken(request):
        # Как у настоящих ошибок прокси: адрес с паролем стоит в самом тексте.
        proxy_url = request.extensions["pm_proxy"]
        assert "p%3Dss" in proxy_url
        raise error_class(f"cannot reach {proxy_url}", request=request)

    serve(scraper, broken)
    with pytest.raises(PageFetchError) as error:
        await scraper.fetch("https://kontakt.az/a.data")
    for text in (str(error.value), error.value.reason, repr(error.value.args)):
        assert "p=ss" not in text and "p%3Dss" not in text and "decodo" not in text


# -- Kontakt Home ---------------------------------------------------------------

# Начало настоящего ответа kontakt.az/<категория>.data (2026-11-12), обрезанное
# до полей, по которым видно устройство формата.
REAL_TURBO_HEAD = (
    '[{"_1":2},"root",{"_3":4},"data",{"_5":6,"_7":8,"_13":-5,"_14":15},'
    '"locale","az","store",{"_9":6,"_10":11,"_12":11},"code","currency","AZN",'
    '"baseCurrency","countdownItem","brands",[16,17],"Samsung","Apple"]'
)


def test_turbo_stream_decodes_the_real_layout():
    assert decode_turbo_stream(REAL_TURBO_HEAD) == {
        "root": {
            "data": {
                "locale": "az",
                "store": {"code": "az", "currency": "AZN", "baseCurrency": "AZN"},
                "countdownItem": None,
                "brands": ["Samsung", "Apple"],
            }
        }
    }


def test_turbo_stream_round_trip_and_special_values():
    value = {"a": [1, 2.5, "x", None, True], "b": {"c": [], "d": {}}, "e": "ə"}
    assert decode_turbo_stream(turbo_encode(value)) == value
    # Значение особого типа (дата) и отложенные строки ответа сборщику не нужны.
    assert decode_turbo_stream('[{"_1":2},"when",["D",1731400000000]]\nP3:[1]\n') == {"when": None}
    with pytest.raises(ValueError):
        decode_turbo_stream("<html>not data</html>")


def kontakt_item(sku="TM-DG-TVA-1118-TV-1654", regular=6299.99, final=5049.99, in_stock=True):
    return {
        "id": 133312,
        "uid": "MTMzMzEy",
        "sku": sku,
        "name": "Televizor JVC LT100NQ7145A (WEBOS) ",
        "url_key": f"televizor-{sku.lower()}",
        "type_id": "simple",
        "stock_status": "IN_STOCK" if in_stock else "OUT_OF_STOCK",
        "stock_item": {"in_stock": in_stock, "min_sale_qty": 1},
        "media_gallery": [{"url": "https://api.kontakt.az/media/catalog/product/t/m/x_1.png"}],
        "price_range": {
            "minimum_price": {
                "regular_price": {"value": regular, "currency": "AZN"},
                "final_price": {"value": final, "currency": "AZN"},
                "discount": {"amount_off": 1250, "percent_off": 20},
            }
        },
        "general_attributes": [
            {"label": "Ekran icazəsi", "value": "4K UHD (3840 × 2160)"},
            {"label": "Ekran ölçüsü", "value": '100"'},
        ],
    }


def kontakt_listing(items, total, page=1, pages=1, children=()):
    return {
        "routes/$": {
            "data": {
                "type": "CATEGORY",
                "category": {
                    "id": 8649,
                    "name": "Televizorlar",
                    "children": [{"url_path": child} for child in children],
                },
                "products": {
                    "total_count": total,
                    "page_info": {"current_page": page, "page_size": 16, "total_pages": pages},
                    "items": items,
                },
            }
        }
    }


def kontakt_section(links):
    return {
        "routes/$": {
            "data": {
                "type": "SUBCATEGORY",
                "subcategoriesPage": {
                    "subcategories": [
                        {"categoryId": n, "categoryTitle": link, "categoryLink": link}
                        for n, link in enumerate(links)
                    ],
                    "products_list": [],
                },
            }
        }
    }


def test_kontakt_product_fields():
    product = build_kontakt_product(kontakt_item(), "tv-audio-ve-video/televizorlar")
    assert product.site == "kontakt"
    assert product.external_id == "TM-DG-TVA-1118-TV-1654"
    assert product.identity_verified is True
    assert product.name == "Televizor JVC LT100NQ7145A (WEBOS)"
    assert product.url == "https://kontakt.az/televizor-tm-dg-tva-1118-tv-1654"
    assert (product.price, product.discount_price, product.discount_percent) == (
        6299.99,
        5049.99,
        19.8,
    )
    assert product.is_on_sale is True
    assert product.offer_availability_status == "in_stock"
    assert product.category == "tv-audio-ve-video/televizorlar"
    assert "Ekran ölçüsü" in product.description


def test_kontakt_product_without_a_sale_and_out_of_stock():
    product = build_kontakt_product(kontakt_item(regular=500, final=500, in_stock=False), "c")
    assert (product.price, product.discount_price, product.is_on_sale) == (500.0, None, False)
    assert product.offer_availability_status == "out_of_stock"


def test_kontakt_availability_falls_back_to_the_status_word():
    item = kontakt_item()
    item["stock_item"] = None
    assert build_kontakt_product(item, "c").offer_availability_status == "in_stock"
    item["stock_status"] = "SOMETHING_NEW"
    assert build_kontakt_product(item, "c").offer_availability_status == "unknown"


@pytest.mark.parametrize("missing", ["sku", "name", "url_key"])
def test_kontakt_item_without_identity_is_not_a_product(missing):
    item = kontakt_item()
    item[missing] = ""
    assert build_kontakt_product(item, "c") is None


@pytest.mark.parametrize(
    ("link", "path"),
    [
        ("https://kontakt.az/tv-audio-ve-video/televizorlar", "tv-audio-ve-video/televizorlar"),
        ("https://kontakt.az/ru/telefoniya/", "telefoniya"),
        ("/gaming", "gaming"),
        ("https://example.com/gaming", None),
        ("", None),
        (None, None),
    ],
)
def test_kontakt_category_path_from_link(link, path):
    assert category_path_from_link(link) == path


async def test_kontakt_category_is_read_page_by_page_and_verified_by_count():
    scraper = fast(KontaktScraper)
    pages = {
        1: [kontakt_item(sku=f"SKU-{n}") for n in range(16)],
        2: [kontakt_item(sku=f"SKU-{n}") for n in range(16, 20)],
    }

    def site(request):
        page = int(parse_qs(urlsplit(str(request.url)).query).get("p", ["1"])[0])
        return httpx.Response(200, text=turbo_encode(kontakt_listing(pages[page], 20, page, 2)))

    seen = serve(scraper, site)
    products = await collect(scraper, "tv-audio-ve-video/televizorlar")

    assert [p.external_id for p in products] == [f"SKU-{n}" for n in range(20)]
    first, second = (urlsplit(str(request.url)) for request in seen)
    assert first.path == "/tv-audio-ve-video/televizorlar.data"
    assert parse_qs(first.query) == {"_routes": ["routes/$"]}
    assert parse_qs(second.query) == {"_routes": ["routes/$"], "p": ["2"]}
    status = scraper._route_statuses["tv-audio-ve-video/televizorlar"]
    assert status.complete and status.abort_reason is None
    assert (status.expected_items, status.raw_items, status.parsed_items) == (20, 20, 20)
    assert (status.expected_pages, status.visited_pages) == (2, 2)


async def test_kontakt_shifted_listing_is_retried_whole_and_never_merged():
    scraper = fast(KontaktScraper)
    passes = {"n": 0}

    def site(request):
        page = int(parse_qs(urlsplit(str(request.url)).query).get("p", ["1"])[0])
        if page == 1:
            passes["n"] += 1
        if passes["n"] == 1:
            # Первый проход: товар SKU-15 пришёл на обеих страницах, SKU-16 пропал.
            items = (
                [kontakt_item(sku=f"SKU-{n}") for n in range(16)]
                if page == 1
                else [kontakt_item(sku="SKU-15"), kontakt_item(sku="SKU-17")]
            )
        else:
            items = (
                [kontakt_item(sku=f"NEW-{n}") for n in range(16)]
                if page == 1
                else [kontakt_item(sku="NEW-16"), kontakt_item(sku="NEW-17")]
            )
        return httpx.Response(200, text=turbo_encode(kontakt_listing(items, 18, page, 2)))

    serve(scraper, site)
    products = await collect(scraper, "c")
    assert [p.external_id for p in products] == [f"NEW-{n}" for n in range(18)]
    assert scraper._route_statuses["c"].complete


async def test_kontakt_listing_that_never_adds_up_is_reported_incomplete():
    scraper = fast(KontaktScraper)
    serve(
        scraper,
        lambda _request: httpx.Response(
            200, text=turbo_encode(kontakt_listing([kontakt_item(sku="ONLY")], 3))
        ),
    )
    products = await collect(scraper, "c")
    status = scraper._route_statuses["c"]
    assert [p.external_id for p in products] == ["ONLY"]
    assert not status.complete
    assert status.abort_reason == "item_count_mismatch"
    assert (status.expected_items, status.parsed_items) == (3, 1)


async def test_kontakt_section_page_is_not_mistaken_for_an_empty_listing():
    scraper = fast(KontaktScraper)
    serve(
        scraper,
        lambda _request: httpx.Response(
            200, text=turbo_encode(kontakt_section(["https://kontakt.az/a/b"]))
        ),
    )
    assert await collect(scraper, "a") == []
    status = scraper._route_statuses["a"]
    assert not status.complete
    assert status.abort_reason.startswith("not_a_product_listing")
    assert status.expected_items is None


async def test_kontakt_explicitly_empty_category_counts_as_verified():
    scraper = fast(KontaktScraper)
    serve(
        scraper,
        # Сайт может назвать «1 страницу» и у пустой категории.
        lambda _request: httpx.Response(200, text=turbo_encode(kontakt_listing([], 0, 1, 1))),
    )
    result = await scraper.scrape(["bos"])
    assert result.item_results["bos"] == {"status": "ok", "products": 0, "verified_empty": True}
    assert result.route_statuses["bos"].complete


@pytest.mark.parametrize(
    ("skus", "total", "reason", "counts"),
    [
        # Повтор товара: записей больше, чем назвал сайт.
        (["A", "A", "B"], 2, "item_count_mismatch", (3, 2, 0)),
        # Запись без артикула: товаров меньше, чем записей.
        (["A", ""], 2, "item_parse_failures", (2, 1, 1)),
    ],
)
async def test_kontakt_pass_the_run_would_reject_is_not_called_complete(
    skus, total, reason, counts
):
    scraper = fast(KontaktScraper)
    items = [kontakt_item(sku=sku) for sku in skus]
    seen = serve(
        scraper,
        lambda _request: httpx.Response(200, text=turbo_encode(kontakt_listing(items, total))),
    )
    await collect(scraper, "c")
    status = scraper._route_statuses["c"]
    assert not status.complete
    assert status.abort_reason == reason
    assert (status.raw_items, status.parsed_items, status.item_failures) == counts
    # Неполный проход повторён, а не отдан с первого раза.
    assert len(seen) == KontaktScraper.category_attempts


async def test_kontakt_empty_page_before_the_last_is_a_break():
    scraper = fast(KontaktScraper)

    def site(request):
        page = int(parse_qs(urlsplit(str(request.url)).query).get("p", ["1"])[0])
        items = [kontakt_item(sku=f"SKU-{n}") for n in range(16)] if page == 1 else []
        return httpx.Response(200, text=turbo_encode(kontakt_listing(items, 20, page, 2)))

    serve(scraper, site)
    products = await collect(scraper, "c")
    status = scraper._route_statuses["c"]
    assert len(products) == 16
    assert not status.complete and status.abort_reason == "empty_page_before_last"


async def test_kontakt_protection_midway_keeps_the_pages_already_read():
    scraper = fast(KontaktScraper)

    def site(request):
        if "p=2" in str(request.url):
            return httpx.Response(403, text=CLOUDFLARE_CHALLENGE)
        items = [kontakt_item(sku=f"SKU-{n}") for n in range(16)]
        return httpx.Response(200, text=turbo_encode(kontakt_listing(items, 20, 1, 2)))

    serve(scraper, site)
    result = await scraper.scrape(["c"])
    assert len(result.products) == 16
    status = result.route_statuses["c"]
    assert not status.complete and status.abort_reason == "page_2_refused"


async def test_the_better_of_two_incomplete_passes_is_the_one_given_out():
    scraper = fast(KontaktScraper)
    passes = {"n": 0}

    def site(request):
        page = int(parse_qs(urlsplit(str(request.url)).query).get("p", ["1"])[0])
        if page == 1:
            passes["n"] += 1
        if passes["n"] == 1 and page == 1:
            items = [kontakt_item(sku=f"FIRST-{n}") for n in range(16)]
            return httpx.Response(200, text=turbo_encode(kontakt_listing(items, 20, 1, 2)))
        # Вторая страница первого прохода и весь второй проход не отвечают.
        return httpx.Response(404)

    serve(scraper, site)
    products = await collect(scraper, "c")
    assert [p.external_id for p in products] == [f"FIRST-{n}" for n in range(16)]
    assert scraper._route_statuses["c"].abort_reason.startswith("page_2_failed")


async def test_kontakt_page_failure_marks_the_route_and_keeps_what_was_read():
    scraper = fast(KontaktScraper)

    def site(request):
        page = int(parse_qs(urlsplit(str(request.url)).query).get("p", ["1"])[0])
        if page == 2:
            return httpx.Response(404)
        items = [kontakt_item(sku=f"SKU-{n}") for n in range(16)]
        return httpx.Response(200, text=turbo_encode(kontakt_listing(items, 20, 1, 2)))

    serve(scraper, site)
    products = await collect(scraper, "c")
    status = scraper._route_statuses["c"]
    assert len(products) == 16
    assert not status.complete and status.pages_skipped == 1
    assert status.abort_reason.startswith("page_2_failed")


async def test_kontakt_limit_stops_early_and_says_so():
    scraper = fast(KontaktScraper)
    items = [kontakt_item(sku=f"SKU-{n}") for n in range(16)]
    seen = serve(
        scraper,
        lambda _request: httpx.Response(200, text=turbo_encode(kontakt_listing(items, 160, 1, 10))),
    )
    products = await collect(scraper, "c", limit=5)
    assert len(products) == 5
    assert len(seen) == 1
    assert scraper._route_statuses["c"].abort_reason == "requested_limit_reached"


async def test_kontakt_behind_protection_is_a_blocked_category_not_an_empty_one():
    scraper = fast(KontaktScraper)
    serve(scraper, lambda _request: httpx.Response(403, text=CLOUDFLARE_CHALLENGE))
    result = await scraper.scrape(["tv-audio-ve-video/televizorlar"])
    assert result.products == []
    assert result.item_results["tv-audio-ve-video/televizorlar"]["error_kind"] == "captcha"
    assert not result.route_statuses["tv-audio-ve-video/televizorlar"].complete


async def test_kontakt_discovers_listings_by_walking_down_from_the_menu():
    scraper = fast(KontaktScraper)
    menu = {
        "root": {
            "data": {
                "categories": [
                    {"url_path": "telefoniya", "is_category": True, "children": []},
                    # Пункт-группа без своей страницы: берутся его категории.
                    {
                        "url_path": "",
                        "is_category": False,
                        "children": [
                            {"url_path": "gaming", "is_category": True, "children": []},
                            {"url_path": "brand/sony", "is_category": False, "children": []},
                        ],
                    },
                ]
            }
        },
        "home": {"data": {}},
    }
    pages = {
        "/_root.data": menu,
        "/telefoniya.data": kontakt_section(
            [
                "https://kontakt.az/telefoniya/smartfonlar",
                "https://kontakt.az/telefoniya/aksesuarlar",
                "https://example.com/elsewhere",
            ]
        ),
        "/telefoniya/smartfonlar.data": kontakt_listing([kontakt_item()], 1),
        "/telefoniya/aksesuarlar.data": kontakt_section(
            ["https://kontakt.az/telefoniya/aksesuarlar/cexollar"]
        ),
        "/telefoniya/aksesuarlar/cexollar.data": kontakt_listing([], 0, 1, 0),
        # Категория-список со вложенной категорией: вложенная тоже в обходе.
        "/gaming.data": kontakt_listing([kontakt_item()], 1, children=["gaming/konsollar"]),
        "/gaming/konsollar.data": kontakt_listing([kontakt_item()], 1),
    }

    def site(request):
        path = urlsplit(str(request.url)).path
        if path not in pages:
            return httpx.Response(404)
        return httpx.Response(200, text=turbo_encode(pages[path]))

    seen = serve(scraper, site)
    # Обход в ширину: сначала пункты меню, потом найденные под ними подразделы.
    assert await scraper.discover_categories() == [
        "gaming",
        "telefoniya/smartfonlar",
        "gaming/konsollar",
        "telefoniya/aksesuarlar/cexollar",
    ]
    assert not any("brand" in str(request.url) for request in seen)


async def test_kontakt_discovery_with_an_unread_section_is_an_error_not_a_shorter_list():
    scraper = fast(KontaktScraper)
    menu = {
        "root": {
            "data": {
                "categories": [
                    {"url_path": "telefoniya", "is_category": True, "children": []},
                    {"url_path": "tv", "is_category": True, "children": []},
                ]
            }
        }
    }

    def site(request):
        path = urlsplit(str(request.url)).path
        if path == "/_root.data":
            return httpx.Response(200, text=turbo_encode(menu))
        if path == "/tv.data":
            return httpx.Response(200, text=turbo_encode(kontakt_listing([kontakt_item()], 1)))
        return httpx.Response(502)

    serve(scraper, site)
    with pytest.raises(PageFetchError) as error:
        await scraper.discover_categories()
    assert "1 sections were not read" in error.value.reason


# -- Baku Electronics -----------------------------------------------------------


def be_item(code=235291, price=629.99, discounted=549.99, quantity=31):
    return {
        "id": 137123,
        "product_code": code,
        "name": "Smartfon Samsung Galaxy A08 6GB/128GB Silver (A085)",
        "slug": f"telefon-samsung-a085-6128gb-silver-a08-{code}",
        "price": price,
        "discount": "80",
        "discounted_price": discounted,
        "perMonth": {"month": 18, "price": 32.24},
        "quantity": quantity,
        "image": "https://media.bakuelectronics.az/media/inventImages/x.webp",
        "is_online": False,
        "pre_order": False,
    }


def be_page(items, total, page=1, size=18):
    return {
        "pageProps": {
            "products": {
                "pageName": "Smartfonlar",
                "products": {"items": items, "total": total, "page": page, "size": size},
            }
        },
        "__N_SSP": True,
    }


def be_home(build_id="BUILD-1", links=()):
    anchors = "".join(f'<a href="/catalog/{link}">x</a>' for link in links)
    data = json.dumps({"props": {"pageProps": {}}, "page": "/", "buildId": build_id})
    return (
        f"<html><body>{anchors}"
        f'<script id="__NEXT_DATA__" type="application/json">{data}</script></body></html>'
    )


def test_bakuelectronics_product_fields():
    product = build_be_product(be_item(), "telefonlar-qadcetler/smartfonlar-mobil-telefonlar")
    assert product.site == "bakuelectronics"
    assert product.external_id == "235291"
    assert product.identity_verified is True
    assert product.url == (
        "https://www.bakuelectronics.az/mehsul/telefon-samsung-a085-6128gb-silver-a08-235291"
    )
    assert (product.price, product.discount_price, product.discount_percent) == (
        629.99,
        549.99,
        12.7,
    )
    assert product.is_on_sale is True
    assert (product.offer_availability_status, product.offer_quantity) == ("in_stock", 31.0)


def test_bakuelectronics_no_sale_no_stock_and_no_identity():
    product = build_be_product(be_item(discounted=629.99, quantity=0), "c")
    assert (product.price, product.discount_price, product.is_on_sale) == (629.99, None, False)
    assert product.offer_availability_status == "out_of_stock"
    assert build_be_product(be_item(quantity=None), "c").offer_availability_status == "unknown"
    for field in ("product_code", "name", "slug"):
        item = be_item()
        item[field] = None
        assert build_be_product(item, "c") is None


async def test_bakuelectronics_category_is_read_page_by_page_and_verified_by_count():
    scraper = fast(BakuElectronicsScraper)

    def site(request):
        url = urlsplit(str(request.url))
        if url.path == "/":
            return httpx.Response(200, text=be_home())
        assert url.path == "/_next/data/BUILD-1/az/catalog/tv-audio-video.json"
        page = int(parse_qs(url.query).get("page", ["1"])[0])
        codes = range(18) if page == 1 else range(18, 25)
        return httpx.Response(200, json=be_page([be_item(code=c) for c in codes], 25, page))

    seen = serve(scraper, site)
    products = await collect(scraper, "tv-audio-video")
    assert [p.external_id for p in products] == [str(c) for c in range(25)]
    assert [urlsplit(str(r.url)).path for r in seen].count("/") == 1
    status = scraper._route_statuses["tv-audio-video"]
    assert status.complete
    assert (status.expected_items, status.raw_items, status.parsed_items) == (25, 25, 25)
    assert (status.expected_pages, status.visited_pages) == (2, 2)


async def test_bakuelectronics_rereads_the_build_id_after_the_site_is_redeployed():
    scraper = fast(BakuElectronicsScraper)
    builds = iter(["OLD", "NEW"])

    def site(request):
        path = urlsplit(str(request.url)).path
        if path == "/":
            return httpx.Response(200, text=be_home(build_id=next(builds)))
        if "/OLD/" in path:
            return httpx.Response(404, text="<html>404</html>")
        return httpx.Response(200, json=be_page([be_item()], 1))

    seen = serve(scraper, site)
    products = await collect(scraper, "c")
    assert len(products) == 1 and scraper._route_statuses["c"].complete
    assert [urlsplit(str(r.url)).path for r in seen] == [
        "/",
        "/_next/data/OLD/az/catalog/c.json",
        "/",
        "/_next/data/NEW/az/catalog/c.json",
    ]


async def test_bakuelectronics_missing_category_and_broken_section_are_not_empty_listings():
    scraper = fast(BakuElectronicsScraper)

    def site(request):
        path = urlsplit(str(request.url)).path
        if path == "/":
            return httpx.Response(200, text=be_home())
        if path.endswith("/yoxdur.json"):
            return httpx.Response(404, json={"notFound": True})
        # Так сайт отвечал на telefonlar-qadcetler: 200 и страница ошибки.
        return httpx.Response(200, text="<!DOCTYPE html><html><body>502</body></html>")

    serve(scraper, site)
    for slug, reason in (
        ("yoxdur", "category does not exist"),
        ("telefonlar-qadcetler", "response has no product listing"),
    ):
        assert await collect(scraper, slug) == []
        status = scraper._route_statuses[slug]
        assert not status.complete
        assert reason in status.abort_reason
        assert status.expected_items is None


async def test_bakuelectronics_empty_section_is_verified_empty():
    scraper = fast(BakuElectronicsScraper)

    def site(request):
        if urlsplit(str(request.url)).path == "/":
            return httpx.Response(200, text=be_home())
        return httpx.Response(200, json=be_page([], 0))

    serve(scraper, site)
    result = await scraper.scrape(["bos"])
    assert result.item_results["bos"] == {"status": "ok", "products": 0, "verified_empty": True}


async def test_bakuelectronics_discovery_replaces_a_broken_section_with_its_children():
    scraper = fast(BakuElectronicsScraper)
    links = [
        "muqayise-et",
        "telefonlar-qadcetler/smartfonlar-mobil-telefonlar",
        "telefonlar-qadcetler/qulaqliqlar",
        "telefonlar-qadcetler/qulaqliqlar/simsiz-qulaqliqlar",
        "tv-audio-video",
        "tv-audio-video/televizorlar",
    ]

    def site(request):
        path = urlsplit(str(request.url)).path
        if path == "/":
            return httpx.Response(200, text=be_home(links=links))
        if path.endswith("/catalog/telefonlar-qadcetler.json"):
            return httpx.Response(200, text="<html>502</html>")
        return httpx.Response(200, json=be_page([be_item()], 1))

    serve(scraper, site)
    assert await scraper.discover_categories() == [
        "tv-audio-video",
        "telefonlar-qadcetler/smartfonlar-mobil-telefonlar",
        "telefonlar-qadcetler/qulaqliqlar",
    ]


@pytest.mark.parametrize(
    ("codes", "total", "reason", "counts"),
    [
        ([1, 1, 2], 2, "item_count_mismatch", (3, 2, 0)),
        ([1, None], 2, "item_parse_failures", (2, 1, 1)),
        ([1], 3, "item_count_mismatch", (1, 1, 0)),
    ],
)
async def test_bakuelectronics_pass_the_run_would_reject_is_not_called_complete(
    codes, total, reason, counts
):
    scraper = fast(BakuElectronicsScraper)

    def site(request):
        if urlsplit(str(request.url)).path == "/":
            return httpx.Response(200, text=be_home())
        return httpx.Response(200, json=be_page([be_item(code=c) for c in codes], total))

    serve(scraper, site)
    await collect(scraper, "c")
    status = scraper._route_statuses["c"]
    assert not status.complete
    assert status.abort_reason == reason
    assert (status.raw_items, status.parsed_items, status.item_failures) == counts


async def test_bakuelectronics_empty_page_before_the_last_is_a_break():
    scraper = fast(BakuElectronicsScraper)

    def site(request):
        url = urlsplit(str(request.url))
        if url.path == "/":
            return httpx.Response(200, text=be_home())
        page = int(parse_qs(url.query).get("page", ["1"])[0])
        items = [be_item(code=c) for c in range(18)] if page == 1 else []
        return httpx.Response(200, json=be_page(items, 25, page))

    serve(scraper, site)
    products = await collect(scraper, "c")
    status = scraper._route_statuses["c"]
    assert len(products) == 18
    assert not status.complete and status.abort_reason == "empty_page_before_last"


async def test_bakuelectronics_limit_stops_early_and_says_so():
    scraper = fast(BakuElectronicsScraper)

    def site(request):
        if urlsplit(str(request.url)).path == "/":
            return httpx.Response(200, text=be_home())
        return httpx.Response(200, json=be_page([be_item(code=c) for c in range(18)], 180))

    seen = serve(scraper, site)
    products = await collect(scraper, "c", limit=5)
    assert len(products) == 5
    assert len(seen) == 2  # главная и одна страница списка
    assert scraper._route_statuses["c"].abort_reason == "requested_limit_reached"


async def test_bakuelectronics_discovery_fails_when_a_section_cannot_be_replaced():
    scraper = fast(BakuElectronicsScraper)

    def site(request):
        path = urlsplit(str(request.url)).path
        if path == "/":
            return httpx.Response(200, text=be_home(links=["tv-audio-video", "qizdiricilar"]))
        if path.endswith("/qizdiricilar.json"):
            return httpx.Response(502)
        return httpx.Response(200, json=be_page([be_item()], 1))

    serve(scraper, site)
    with pytest.raises(PageFetchError) as error:
        await scraper.discover_categories()
    assert "qizdiricilar" in error.value.reason


async def test_bakuelectronics_block_page_is_a_blocked_category():
    scraper = fast(BakuElectronicsScraper)
    serve(scraper, lambda _request: httpx.Response(403, text=CLOUDFLARE_BLOCK))
    result = await scraper.scrape(["tv-audio-video"])
    assert result.products == []
    assert result.item_results["tv-audio-video"]["error_kind"] == "captcha"


# -- Irshad ---------------------------------------------------------------------

IRSHAD_PAGE = (FIXTURES / "irshad_list_page.html").read_text(encoding="utf-8")
IRSHAD_LAST_PAGE = IRSHAD_PAGE[: IRSHAD_PAGE.index('<div id="loadMoreBlock"')] + "</div></div>"
IRSHAD_PAST_THE_END = '<div class="pjax-container"><div id="productGridItems"></div></div>'


def test_irshad_fixture_is_the_page_the_scraper_expects():
    assert 'id="loadMore"' in IRSHAD_PAGE and 'data-page="3"' in IRSHAD_PAGE
    assert 'id="loadMore"' not in IRSHAD_LAST_PAGE
    assert [product_id for product_id, _ in split_cards(IRSHAD_PAGE)] == [
        "108753",
        "108751",
        "85759",
    ]


def test_irshad_plain_card_is_one_product():
    product_id, card = split_cards(IRSHAD_PAGE)[0]
    products, failures = build_products(product_id, card, "kondisioner")
    assert failures == 0
    (product,) = products
    assert product.site == "irshad"
    assert product.external_id == "108753"
    assert product.identity_verified is True
    assert product.name == "Indesit IRFPB 180/181"
    assert product.url == "https://irshad.az/az/mehsullar/indesit-irfpb-180-181"
    assert (product.price, product.discount_price, product.discount_percent) == (
        1199.99,
        949.99,
        20.8,
    )
    assert product.is_on_sale is True
    assert product.offer_availability_status == "in_stock"
    assert product.description == "Kondisioner"
    assert product.image_url.startswith("https://storage.irshad.az/products/")


def test_irshad_colour_card_is_one_product_per_colour_each_with_its_own_price():
    product_id, card = split_cards(IRSHAD_PAGE)[2]
    products, failures = build_products(product_id, card, "kondisioner")
    assert failures == 0
    assert [(p.external_id, p.name, p.price, p.discount_price, p.url) for p in products] == [
        (
            "85759",
            "TAUBE TB12R410JP",
            739.99,
            599.99,
            "https://irshad.az/az/mehsullar/taube-tb12r410jp",
        ),
        (
            "108719",
            "TAUBE TB09R410JPBK Black",
            899.99,
            599.99,
            "https://irshad.az/az/mehsullar/taube-tb09r410jpbk-black",
        ),
    ]


def test_irshad_card_without_a_sale_has_one_price():
    product_id, card = split_cards(IRSHAD_PAGE)[0]
    card = card.replace('<span class="old-price">1199.99 AZN</span>', "")
    (product,), _ = build_products(product_id, card, "c")
    assert (product.price, product.discount_price, product.is_on_sale) == (949.99, None, False)


def test_irshad_stock_label_decides_availability():
    product_id, card = split_cards(IRSHAD_PAGE)[0]
    assert "Stokda var" in card
    (unknown,), _ = build_products(product_id, card.replace("Stokda var", "Tezliklə"), "c")
    assert unknown.offer_availability_status == "unknown"
    (gone,), _ = build_products(product_id, card.replace("Stokda var", "Stokda yoxdur"), "c")
    assert gone.offer_availability_status == "out_of_stock"


def test_irshad_offer_nobody_can_name_is_a_failure_not_a_product():
    product_id, card = split_cards(IRSHAD_PAGE)[2]
    card = card.replace('value="108719"', 'value=""')
    products, failures = build_products(product_id, card, "c")
    assert [p.external_id for p in products] == ["85759"]
    assert failures == 1


async def test_irshad_category_follows_load_more_until_it_disappears():
    scraper = fast(IrshadScraper)
    second = IRSHAD_LAST_PAGE.replace("product-108753_", "product-200001_").replace(
        'data-selected-id="108753"', 'data-selected-id="200001"'
    )
    second = second.replace("product-108751_", "product-200002_").replace(
        'data-selected-id="108751"', 'data-selected-id="200002"'
    )
    second = second.replace("product-85759_", "product-200003_").replace(
        'data-selected-id="85759"', 'data-selected-id="200003"'
    )
    second = second.replace('value="85759"', 'value="200003"').replace(
        'value="108719"', 'value="200004"'
    )
    second = second.replace('data-selected-id="108719"', 'data-selected-id="200004"')
    first = IRSHAD_PAGE.replace('data-page="3"', 'data-page="2"')

    def site(request):
        url = urlsplit(str(request.url))
        assert url.path == "/az/list-products/boyuk-meiset-texnikasi/iqlim/kondisioner"
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        page = parse_qs(url.query)["page"][0]
        return httpx.Response(200, text={"1": first, "2": second}.get(page, IRSHAD_PAST_THE_END))

    seen = serve(scraper, site)
    products = await collect(scraper, "boyuk-meiset-texnikasi/iqlim/kondisioner")
    assert [p.external_id for p in products] == [
        "108753",
        "108751",
        "85759",
        "108719",
        "200001",
        "200002",
        "200003",
        "200004",
    ]
    # Третий запрос — страница за концом списка: конец подтверждён, а не принят на веру.
    assert [parse_qs(urlsplit(str(r.url)).query)["page"][0] for r in seen] == ["1", "2", "3"]
    status = scraper._route_statuses["boyuk-meiset-texnikasi/iqlim/kondisioner"]
    assert status.complete and status.abort_reason is None
    assert status.expected_items is None
    assert (status.visited_pages, status.raw_items, status.parsed_items) == (3, 8, 8)
    assert status.item_failures == 0 and status.pages_skipped == 0


def irshad_site(pages: dict[str, str]):
    """Сайт Irshad из страниц по номерам; за последней — пустая."""

    def site(request):
        page = parse_qs(urlsplit(str(request.url)).query)["page"][0]
        return httpx.Response(200, text=pages.get(page, IRSHAD_PAST_THE_END))

    return site


async def test_irshad_product_seen_twice_is_kept_once_and_the_pass_stays_complete():
    # Цвет, у которого есть и своя карточка, или список, сдвинувшийся на новый
    # товар: повтор ничего не теряет и проход не портит.
    scraper = fast(IrshadScraper)
    first = IRSHAD_PAGE.replace('data-page="3"', 'data-page="2"')
    serve(scraper, irshad_site({"1": first, "2": IRSHAD_LAST_PAGE}))
    products = await collect(scraper, "c")
    status = scraper._route_statuses["c"]
    assert [p.external_id for p in products] == ["108753", "108751", "85759", "108719"]
    assert status.complete
    assert (status.raw_items, status.parsed_items) == (4, 4)


async def test_irshad_end_of_list_is_not_believed_when_the_marker_vanished():
    # Вёрстка без ссылки «показать ещё», а товары на следующей странице есть:
    # раньше такой раздел считался полным разделом из одной страницы.
    scraper = fast(IrshadScraper)
    seen = serve(scraper, irshad_site({"1": IRSHAD_LAST_PAGE, "2": IRSHAD_LAST_PAGE}))
    products = await collect(scraper, "c")
    status = scraper._route_statuses["c"]
    assert len(products) == 4
    assert not status.complete
    assert status.abort_reason == "load_more_marker_missing"
    assert len(seen) == 2 * IrshadScraper.category_attempts


async def test_irshad_empty_page_the_site_itself_pointed_to_is_a_break():
    scraper = fast(IrshadScraper)
    first = IRSHAD_PAGE.replace('data-page="3"', 'data-page="2"')
    serve(scraper, irshad_site({"1": first}))
    await collect(scraper, "c")
    status = scraper._route_statuses["c"]
    assert not status.complete
    assert status.abort_reason == "empty_page_before_last"


async def test_irshad_unreadable_offer_makes_the_pass_incomplete():
    scraper = fast(IrshadScraper)
    broken = IRSHAD_LAST_PAGE.replace('value="108719"', 'value=""')
    serve(scraper, irshad_site({"1": broken}))
    products = await collect(scraper, "c")
    status = scraper._route_statuses["c"]
    assert [p.external_id for p in products] == ["108753", "108751", "85759"]
    assert not status.complete
    assert status.abort_reason == "item_parse_failures"
    assert (status.item_failures, status.raw_items, status.parsed_items) == (1, 4, 3)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # Действующая цена не прочиталась — зачёркнутую за неё выдавать нельзя.
        ('<p class="new-price">', '<p class="new-price">1,199.99 AZN</p><p class="x">'),
        # Зачёркнутая цена записана так, что не читается.
        ('<span class="old-price">1199.99 AZN</span>', '<span class="old-price">min</span>'),
    ],
)
def test_irshad_price_that_cannot_be_read_is_a_failure_not_a_guess(old, new):
    product_id, card = split_cards(IRSHAD_PAGE)[0]
    assert old in card
    products, failures = build_products(product_id, card.replace(old, new, 1), "c")
    assert (products, failures) == ([], 1)


def test_irshad_struck_out_price_alone_is_never_the_current_price():
    product_id, card = split_cards(IRSHAD_PAGE)[0]
    start = card.index('<p class="new-price">')
    without_current = card[:start] + card[card.index("</p>", start) + 4 :]
    assert 'class="old-price"' in without_current
    assert build_products(product_id, without_current, "c") == ([], 1)


async def test_irshad_page_that_fails_midway_keeps_the_pages_already_read():
    scraper = fast(IrshadScraper)
    first = IRSHAD_PAGE.replace('data-page="3"', 'data-page="2"')

    def site(request):
        if "page=2" in str(request.url):
            return httpx.Response(403, text=CLOUDFLARE_BLOCK)
        return httpx.Response(200, text=first)

    serve(scraper, site)
    result = await scraper.scrape(["c"])
    status = result.route_statuses["c"]
    assert len(result.products) == 4
    assert not status.complete
    assert status.abort_reason == "page_2_refused" and status.pages_skipped == 1


async def test_irshad_empty_first_page_is_not_a_verified_empty_category():
    scraper = fast(IrshadScraper)
    serve(scraper, lambda _request: httpx.Response(200, text=IRSHAD_PAST_THE_END))
    result = await scraper.scrape(["yoxdur"])
    assert result.item_results["yoxdur"]["status"] == "empty"
    assert result.route_statuses["yoxdur"].abort_reason == "empty_listing"
    assert not result.route_statuses["yoxdur"].complete


async def test_irshad_pagination_that_does_not_advance_is_stopped():
    scraper = fast(IrshadScraper)
    stuck = IRSHAD_PAGE.replace('data-page="3"', 'data-page="1"')
    seen = serve(scraper, lambda _request: httpx.Response(200, text=stuck))
    await collect(scraper, "c")
    assert scraper._route_statuses["c"].abort_reason == "pagination_does_not_advance"
    assert len(seen) == IrshadScraper.category_attempts


def test_irshad_waits_as_long_as_the_site_asks_unless_told_otherwise(monkeypatch):
    monkeypatch.delenv("IRSHAD_RATE_LIMIT_SEC", raising=False)
    assert IrshadScraper().rate_limit_sec == 30.0
    monkeypatch.setenv("IRSHAD_RATE_LIMIT_SEC", "12")
    assert IrshadScraper().rate_limit_sec == 12.0
    assert IrshadScraper(rate_limit_sec=3).rate_limit_sec == 3
    monkeypatch.delenv("KONTAKT_RATE_LIMIT_SEC", raising=False)
    assert KontaktScraper().rate_limit_sec == 2.0


async def test_irshad_discovers_first_level_sections_and_skips_non_catalog_ones():
    scraper = fast(IrshadScraper)
    home = "".join(
        f'<a href="https://irshad.az/az/{path}">x</a>'
        for path in (
            "tv-ve-audio",
            "tv-ve-audio/televizorlar",
            "foto-texnika/fotoaparatlar/fujifilmm",
            "outlet/telefonlar",
            "mehsullar/indesit-irfpb-180-181",
            "brend/samsung",
            "kampaniyalar",
            "magazalar",
        )
    )
    serve(scraper, lambda _request: httpx.Response(200, text=home))
    assert await scraper.discover_categories() == ["foto-texnika", "tv-ve-audio"]


async def test_irshad_discovery_without_sections_is_an_error_not_an_empty_catalog():
    scraper = fast(IrshadScraper)
    serve(scraper, lambda _request: httpx.Response(200, text="<html><body></body></html>"))
    with pytest.raises(PageFetchError):
        await scraper.discover_categories()
