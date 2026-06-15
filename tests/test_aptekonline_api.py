"""Tests для AptekonlineScraper после миграции с Playwright на JSON API.

Проверяем:
  - _build_product_from_api маппит реальный API-ответ в ScrapedProduct
    с правильными полями (price/discount/image/dosage/etc).
  - scrape_category итерирует постранично, мокая HTTP-вызовы фикстурой.
  - Edge cases: пустой data, отсутствие url_id/name, malformed price.

Никаких сетевых запросов или браузера — все ответы мокаются.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from src.scrapers.aptekonline import (
    AptekonlineScraper,
    _DEFAULT_CHECKUS,
    _brightdata_httpx_proxy_for,
    _build_product_from_api,
    _decodo_enabled,
    _decodo_httpx_proxy_for,
    _decodo_page_attempts,
    _decodo_ports,
    _iproyal_httpx_proxy_for,
    _resolve_checkus_token,
    _scraperapi_httpx_proxy_for,
)


def _mock_httpx_client(payload: dict | list[dict] | None = None, status: int = 200):
    """Контекстный менеджер: подменяет httpx.AsyncClient на mock-транспорт.

    payload может быть:
      - dict — один и тот же ответ для каждого запроса
      - list[dict] — последовательность ответов (по 1 на запрос, остаток повторяется)
      - None — Response(status, b"") без JSON
    """
    payloads = payload if isinstance(payload, list) else [payload]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        idx = min(call_count["n"], len(payloads) - 1)
        call_count["n"] += 1
        body = payloads[idx]
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)

    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    return patch.object(httpx.AsyncClient, "__init__", patched_init), call_count


def _mock_httpx_status_seq(statuses: list[int], payload: dict | None):
    """Mock-транспорт с последовательностью статусов: i-й запрос → statuses[i]
    (последний повторяется), json=payload только для 200.

    Для Decodo-ретрая каждый attempt создаёт НОВЫЙ клиент с proxy=, поэтому
    proxy/mounts вычищаем — они конфликтуют с подменным transport.
    """
    idx = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = idx["n"]
        idx["n"] += 1
        st = statuses[min(i, len(statuses) - 1)]
        if st == 200:
            return httpx.Response(200, json=payload)
        return httpx.Response(st)

    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs.pop("mounts", None)
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    return patch.object(httpx.AsyncClient, "__init__", patched_init), idx


FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture() -> dict:
    return json.loads((FIXTURES / "aptekonline_api_response.json").read_text(encoding="utf-8"))


# ─── _build_product_from_api ────────────────────────────────────────────────


def test_build_product_basic_fields():
    payload = _load_fixture()
    item = payload["data"][0]
    thumb = payload["thumb_folder"]

    product = _build_product_from_api(item, "114", thumb, "https://www.aptekonline.az")

    assert product is not None
    assert product.site == "aptekonline"
    assert product.external_id == item["url_id"]
    assert product.url == f"https://www.aptekonline.az/product/{item['url_id']}"
    assert product.name == item["name"]
    assert product.price == item["price"]
    assert product.image_url and product.image_url.startswith(thumb)
    assert product.image_url.endswith(item["thumb1"])
    assert product.category == "114"
    assert product.manufacturer == item["olke"]
    assert product.description == item["terkib"]


def test_build_product_no_discount_when_discount_price_is_null():
    item = {
        "url_id": "X-1",
        "name": "Test",
        "price": 5.28,
        "discount_price": None,
        "thumb1": None,
        "olke": None,
        "terkib": None,
        "cashback_percent": None,
    }
    product = _build_product_from_api(item, "1", "", "https://x.az")
    assert product is not None
    assert product.is_on_sale is False
    assert product.discount_price is None
    assert product.discount_percent is None


def test_build_product_handles_active_discount():
    item = {
        "url_id": "X-2",
        "name": "Test discounted",
        "price": 10.0,
        "discount_price": 7.5,
        "thumb1": "x.jpg",
        "olke": "Türkiye",
        "terkib": "Param",
        "cashback_percent": None,
    }
    product = _build_product_from_api(item, "1", "https://thumb.x/", "https://x.az")
    assert product is not None
    assert product.is_on_sale is True
    assert product.discount_price == 7.5
    assert product.price == 10.0
    assert product.discount_percent == 25.0


def test_build_product_skips_invalid_discount_price_higher_than_price():
    """API иногда отдаёт discount_price >= price (мусор) — игнорируем."""
    item = {
        "url_id": "X-3",
        "name": "X",
        "price": 5.0,
        "discount_price": 10.0,
        "thumb1": None,
        "olke": None,
        "terkib": None,
        "cashback_percent": None,
    }
    product = _build_product_from_api(item, "1", "", "https://x.az")
    assert product is not None
    assert product.is_on_sale is False
    assert product.discount_price is None


def test_build_product_returns_none_without_url_id_or_name():
    assert _build_product_from_api({"name": "n"}, "1", "", "https://x.az") is None
    assert _build_product_from_api({"url_id": "u"}, "1", "", "https://x.az") is None
    assert _build_product_from_api({"url_id": "u", "name": ""}, "1", "", "https://x.az") is None


def test_build_product_promo_label_from_cashback_percent():
    item = {
        "url_id": "X-4",
        "name": "X",
        "price": 10.0,
        "discount_price": None,
        "thumb1": None,
        "olke": None,
        "terkib": None,
        "cashback_percent": 5,
    }
    product = _build_product_from_api(item, "1", "", "https://x.az")
    assert product is not None
    assert product.promo_label == "5% kəşbək"


def test_build_product_handles_string_prices():
    """Иногда API отдаёт цены как строки — должны парситься."""
    item = {
        "url_id": "X-5",
        "name": "X",
        "price": "10.50",
        "discount_price": "8.25",
        "thumb1": None,
        "olke": None,
        "terkib": None,
        "cashback_percent": None,
    }
    product = _build_product_from_api(item, "1", "", "https://x.az")
    assert product is not None
    assert product.price == 10.50
    assert product.discount_price == 8.25


# ─── scrape_category (httpx подменяется на MockTransport) ────────────────────


@pytest.mark.asyncio
async def test_scrape_category_yields_products_from_api():
    payload = _load_fixture()
    # last_page=1 в фикстуре чтобы scrape остановился сразу после первой страницы
    payload["last_page"] = 1
    payload["next_page_url"] = None
    patcher, _ = _mock_httpx_client(payload)
    with patcher:
        scraper = AptekonlineScraper()
        products = [p async for p in scraper.scrape_category("114")]
    assert len(products) == len(payload["data"])
    assert all(p.site == "aptekonline" for p in products)
    assert all(p.external_id for p in products)


@pytest.mark.asyncio
async def test_scrape_category_skips_non_numeric_slug():
    """Старые .slug категории (текстовые) не подходят для API — silently skip."""
    patcher, calls = _mock_httpx_client({"data": [], "last_page": 1, "next_page_url": None})
    with patcher:
        scraper = AptekonlineScraper()
        products = [p async for p in scraper.scrape_category("baby-food")]
    assert products == []
    assert calls["n"] == 0  # ни одного запроса


@pytest.mark.asyncio
async def test_scrape_category_respects_limit():
    payload = _load_fixture()
    payload["last_page"] = 1
    payload["next_page_url"] = None
    patcher, _ = _mock_httpx_client(payload)
    with patcher:
        scraper = AptekonlineScraper()
        products = [p async for p in scraper.scrape_category("114", limit=2)]
    assert len(products) == 2


@pytest.mark.asyncio
async def test_scrape_category_handles_http_error_gracefully():
    patcher, _ = _mock_httpx_client(None, status=500)
    with patcher:
        scraper = AptekonlineScraper()
        products = [p async for p in scraper.scrape_category("114")]
    assert products == []


@pytest.mark.asyncio
async def test_scrape_category_stops_on_empty_data():
    patcher, _ = _mock_httpx_client({"data": [], "current_page": 1, "last_page": 1})
    with patcher:
        scraper = AptekonlineScraper()
        products = [p async for p in scraper.scrape_category("114")]
    assert products == []


# ─────────────────────────────────────────────────────────────────────
# Phase 0.1 — APTEKONLINE_CHECKUS env resolution (rotation path).
# Default остаётся как fallback; задаваемая через env строка имеет приоритет;
# empty string → fail-fast (защита от .env-опечатки).
# ─────────────────────────────────────────────────────────────────────


def test_checkus_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("APTEKONLINE_CHECKUS", raising=False)
    assert _resolve_checkus_token() == _DEFAULT_CHECKUS


def test_checkus_uses_env_override(monkeypatch):
    monkeypatch.setenv("APTEKONLINE_CHECKUS", "$2y$10$ROTATED_TOKEN_FROM_ENV")
    assert _resolve_checkus_token() == "$2y$10$ROTATED_TOKEN_FROM_ENV"


def test_checkus_strips_whitespace_from_env(monkeypatch):
    monkeypatch.setenv("APTEKONLINE_CHECKUS", "  $2y$10$WITH_WS  \n")
    assert _resolve_checkus_token() == "$2y$10$WITH_WS"


def test_checkus_fails_fast_on_empty_env(monkeypatch):
    monkeypatch.setenv("APTEKONLINE_CHECKUS", "")
    with pytest.raises(RuntimeError, match="set but empty"):
        _resolve_checkus_token()


def test_checkus_fails_fast_on_whitespace_only_env(monkeypatch):
    monkeypatch.setenv("APTEKONLINE_CHECKUS", "   \n\t  ")
    with pytest.raises(RuntimeError, match="set but empty"):
        _resolve_checkus_token()


# ─────────────────────────────────────────────────────────────────────
# Phase 1.2 — Bright Data httpx proxy resolution for aptekonline.
# ─────────────────────────────────────────────────────────────────────


def _clear_brightdata(mp):
    for k in (
        "BRIGHTDATA_USERNAME",
        "BRIGHTDATA_PASSWORD",
        "BRIGHTDATA_SITES",
        "BRIGHTDATA_HOST",
        "BRIGHTDATA_COUNTRY",
    ):
        mp.delenv(k, raising=False)


def test_brightdata_httpx_proxy_none_without_creds(monkeypatch):
    _clear_brightdata(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_SITES", "aptekonline")
    assert _brightdata_httpx_proxy_for("aptekonline") is None


def test_brightdata_httpx_proxy_none_when_site_excluded(monkeypatch):
    _clear_brightdata(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_USERNAME", "u")
    monkeypatch.setenv("BRIGHTDATA_PASSWORD", "p")
    monkeypatch.setenv("BRIGHTDATA_SITES", "pharmonline")
    assert _brightdata_httpx_proxy_for("aptekonline") is None


def test_brightdata_httpx_proxy_returns_url_with_default_host(monkeypatch):
    _clear_brightdata(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_USERNAME", "brd-customer-hl_X-zone-Y")
    monkeypatch.setenv("BRIGHTDATA_PASSWORD", "s3cr3t")
    monkeypatch.setenv("BRIGHTDATA_SITES", "aptekonline")
    url = _brightdata_httpx_proxy_for("aptekonline")
    assert url == "http://brd-customer-hl_X-zone-Y:s3cr3t@brd.superproxy.io:33335"


def test_brightdata_httpx_proxy_appends_country(monkeypatch):
    _clear_brightdata(monkeypatch)
    monkeypatch.setenv("BRIGHTDATA_USERNAME", "brd-customer-hl_X-zone-Y")
    monkeypatch.setenv("BRIGHTDATA_PASSWORD", "p")
    monkeypatch.setenv("BRIGHTDATA_SITES", "aptekonline")
    monkeypatch.setenv("BRIGHTDATA_COUNTRY", "tr")
    url = _brightdata_httpx_proxy_for("aptekonline")
    assert url == "http://brd-customer-hl_X-zone-Y-country-tr:p@brd.superproxy.io:33335"


# ─── IPRoyal httpx (Phase 1.2b) ──────────────────────────────────────────────


def _clear_iproyal(mp):
    for k in (
        "IPROYAL_USERNAME",
        "IPROYAL_PASSWORD",
        "IPROYAL_SITES",
        "IPROYAL_HOST",
        "IPROYAL_COUNTRY",
    ):
        mp.delenv(k, raising=False)


def test_iproyal_httpx_proxy_none_without_creds(monkeypatch):
    _clear_iproyal(monkeypatch)
    monkeypatch.setenv("IPROYAL_SITES", "aptekonline")
    assert _iproyal_httpx_proxy_for("aptekonline") is None


def test_iproyal_httpx_proxy_none_when_site_excluded(monkeypatch):
    _clear_iproyal(monkeypatch)
    monkeypatch.setenv("IPROYAL_USERNAME", "u")
    monkeypatch.setenv("IPROYAL_PASSWORD", "p")
    monkeypatch.setenv("IPROYAL_SITES", "pharmonline")
    assert _iproyal_httpx_proxy_for("aptekonline") is None


def test_iproyal_httpx_proxy_returns_url(monkeypatch):
    _clear_iproyal(monkeypatch)
    monkeypatch.setenv("IPROYAL_USERNAME", "myuser")
    monkeypatch.setenv("IPROYAL_PASSWORD", "s3cr3t")
    monkeypatch.setenv("IPROYAL_SITES", "aptekonline")
    url = _iproyal_httpx_proxy_for("aptekonline")
    assert url == "http://myuser:s3cr3t@geo.iproyal.com:12321"


def test_iproyal_httpx_proxy_with_country(monkeypatch):
    _clear_iproyal(monkeypatch)
    monkeypatch.setenv("IPROYAL_USERNAME", "myuser")
    monkeypatch.setenv("IPROYAL_PASSWORD", "p")
    monkeypatch.setenv("IPROYAL_SITES", "aptekonline")
    monkeypatch.setenv("IPROYAL_COUNTRY", "az")
    url = _iproyal_httpx_proxy_for("aptekonline")
    assert url == "http://myuser_country-az:p@geo.iproyal.com:12321"


# ScraperAPI httpx proxy resolution + premium flag (Azerbaijan residential).


def _clear_scraperapi(mp):
    for k in (
        "SCRAPER_API_KEY",
        "SCRAPER_API_SITES",
        "SCRAPER_API_COUNTRY",
        "SCRAPER_API_PREMIUM_SITES",
    ):
        mp.delenv(k, raising=False)


def test_scraperapi_httpx_proxy_none_without_key(monkeypatch):
    _clear_scraperapi(monkeypatch)
    monkeypatch.setenv("SCRAPER_API_SITES", "aptekonline")
    assert _scraperapi_httpx_proxy_for("aptekonline") is None


def test_scraperapi_httpx_proxy_none_when_site_excluded(monkeypatch):
    _clear_scraperapi(monkeypatch)
    monkeypatch.setenv("SCRAPER_API_KEY", "k3y")
    monkeypatch.setenv("SCRAPER_API_SITES", "pharmonline")
    assert _scraperapi_httpx_proxy_for("aptekonline") is None


def test_scraperapi_httpx_proxy_plain_without_country_or_premium(monkeypatch):
    _clear_scraperapi(monkeypatch)
    monkeypatch.setenv("SCRAPER_API_KEY", "k3y")
    monkeypatch.setenv("SCRAPER_API_SITES", "aptekonline")
    url = _scraperapi_httpx_proxy_for("aptekonline")
    assert url == "http://scraperapi:k3y@proxy-server.scraperapi.com:8001"


def test_scraperapi_httpx_proxy_country_only(monkeypatch):
    _clear_scraperapi(monkeypatch)
    monkeypatch.setenv("SCRAPER_API_KEY", "k3y")
    monkeypatch.setenv("SCRAPER_API_SITES", "aptekonline")
    monkeypatch.setenv("SCRAPER_API_COUNTRY", "az")
    url = _scraperapi_httpx_proxy_for("aptekonline")
    assert url == "http://scraperapi.country_code=az:k3y@proxy-server.scraperapi.com:8001"


def test_scraperapi_httpx_proxy_premium_only(monkeypatch):
    _clear_scraperapi(monkeypatch)
    monkeypatch.setenv("SCRAPER_API_KEY", "k3y")
    monkeypatch.setenv("SCRAPER_API_SITES", "aptekonline")
    monkeypatch.setenv("SCRAPER_API_PREMIUM_SITES", "aptekonline")
    url = _scraperapi_httpx_proxy_for("aptekonline")
    assert url == "http://scraperapi.premium=true:k3y@proxy-server.scraperapi.com:8001"


def test_scraperapi_httpx_proxy_country_and_premium_for_aptek(monkeypatch):
    # Боевая конфигурация aptekonline: AZ residential через premium-тариф.
    _clear_scraperapi(monkeypatch)
    monkeypatch.setenv("SCRAPER_API_KEY", "k3y")
    monkeypatch.setenv("SCRAPER_API_SITES", "pharmonline,aptekonline")
    monkeypatch.setenv("SCRAPER_API_COUNTRY", "az")
    monkeypatch.setenv("SCRAPER_API_PREMIUM_SITES", "aptekonline")
    url = _scraperapi_httpx_proxy_for("aptekonline")
    assert url == (
        "http://scraperapi.country_code=az.premium=true:k3y"
        "@proxy-server.scraperapi.com:8001"
    )


def test_scraperapi_httpx_proxy_premium_not_applied_to_other_site(monkeypatch):
    # premium точечный: pharmonline-фоллбэк не должен получить платный premium.
    _clear_scraperapi(monkeypatch)
    monkeypatch.setenv("SCRAPER_API_KEY", "k3y")
    monkeypatch.setenv("SCRAPER_API_SITES", "pharmonline,aptekonline")
    monkeypatch.setenv("SCRAPER_API_COUNTRY", "az")
    monkeypatch.setenv("SCRAPER_API_PREMIUM_SITES", "aptekonline")
    url = _scraperapi_httpx_proxy_for("pharmonline")
    assert url == "http://scraperapi.country_code=az:k3y@proxy-server.scraperapi.com:8001"


def test_scraperapi_httpx_proxy_premium_list_present_but_bare_result(monkeypatch):
    # premium задан для aptek, но запрашиваем другой сайт без country →
    # должен получиться голый "scraperapi" (premium-список не протёк).
    _clear_scraperapi(monkeypatch)
    monkeypatch.setenv("SCRAPER_API_KEY", "k3y")
    monkeypatch.setenv("SCRAPER_API_SITES", "pharmonline,aptekonline")
    monkeypatch.setenv("SCRAPER_API_PREMIUM_SITES", "aptekonline")
    url = _scraperapi_httpx_proxy_for("pharmonline")
    assert url == "http://scraperapi:k3y@proxy-server.scraperapi.com:8001"


def test_scraperapi_httpx_proxy_none_without_key_even_with_premium(monkeypatch):
    # Нет ключа → None ДО любой flag-логики, даже если premium настроен.
    _clear_scraperapi(monkeypatch)
    monkeypatch.setenv("SCRAPER_API_SITES", "aptekonline")
    monkeypatch.setenv("SCRAPER_API_COUNTRY", "az")
    monkeypatch.setenv("SCRAPER_API_PREMIUM_SITES", "aptekonline")
    assert _scraperapi_httpx_proxy_for("aptekonline") is None


# Decodo residential AZ proxy: per-port retry pool for aptekonline.


def _clear_decodo(mp):
    for k in (
        "DECODO_USERNAME",
        "DECODO_PASSWORD",
        "DECODO_SITES",
        "DECODO_HOST",
        "DECODO_PORTS",
        "DECODO_PAGE_ATTEMPTS",
    ):
        mp.delenv(k, raising=False)


def test_decodo_disabled_without_creds(monkeypatch):
    _clear_decodo(monkeypatch)
    monkeypatch.setenv("DECODO_SITES", "aptekonline")
    assert _decodo_enabled("aptekonline") is False
    assert _decodo_ports("aptekonline") == []


def test_decodo_disabled_when_site_excluded(monkeypatch):
    _clear_decodo(monkeypatch)
    monkeypatch.setenv("DECODO_USERNAME", "u")
    monkeypatch.setenv("DECODO_PASSWORD", "p")
    monkeypatch.setenv("DECODO_SITES", "pharmonline")
    assert _decodo_enabled("aptekonline") is False
    assert _decodo_ports("aptekonline") == []
    assert _decodo_httpx_proxy_for("aptekonline", 30001) is None


def test_decodo_ports_default_range(monkeypatch):
    _clear_decodo(monkeypatch)
    monkeypatch.setenv("DECODO_USERNAME", "u")
    monkeypatch.setenv("DECODO_PASSWORD", "p")
    monkeypatch.setenv("DECODO_SITES", "aptekonline")
    # default DECODO_PORTS = "30001-30010"
    assert _decodo_ports("aptekonline") == list(range(30001, 30011))


def test_decodo_ports_explicit_range_and_csv(monkeypatch):
    _clear_decodo(monkeypatch)
    monkeypatch.setenv("DECODO_USERNAME", "u")
    monkeypatch.setenv("DECODO_PASSWORD", "p")
    monkeypatch.setenv("DECODO_SITES", "aptekonline")
    monkeypatch.setenv("DECODO_PORTS", "30001-30003")
    assert _decodo_ports("aptekonline") == [30001, 30002, 30003]
    monkeypatch.setenv("DECODO_PORTS", "30001, 30005, 30009")
    assert _decodo_ports("aptekonline") == [30001, 30005, 30009]


def test_decodo_proxy_url_encodes_password(monkeypatch):
    # Пароль с '=' должен быть URL-кодирован (%3D), иначе ломает userinfo.
    _clear_decodo(monkeypatch)
    monkeypatch.setenv("DECODO_USERNAME", "spw25z9lwn")
    monkeypatch.setenv("DECODO_PASSWORD", "85Yo=yePfQ")
    monkeypatch.setenv("DECODO_SITES", "aptekonline")
    url = _decodo_httpx_proxy_for("aptekonline", 30001)
    assert url == "http://spw25z9lwn:85Yo%3DyePfQ@az.decodo.com:30001"


def test_decodo_proxy_custom_host_and_port(monkeypatch):
    _clear_decodo(monkeypatch)
    monkeypatch.setenv("DECODO_USERNAME", "u")
    monkeypatch.setenv("DECODO_PASSWORD", "p")
    monkeypatch.setenv("DECODO_SITES", "aptekonline")
    monkeypatch.setenv("DECODO_HOST", "tr.decodo.com")
    assert _decodo_httpx_proxy_for("aptekonline", 7000) == "http://u:p@tr.decodo.com:7000"


def test_decodo_page_attempts_default_and_override(monkeypatch):
    _clear_decodo(monkeypatch)
    assert _decodo_page_attempts() == 5
    monkeypatch.setenv("DECODO_PAGE_ATTEMPTS", "3")
    assert _decodo_page_attempts() == 3
    # invalid / zero → safe default
    monkeypatch.setenv("DECODO_PAGE_ATTEMPTS", "0")
    assert _decodo_page_attempts() == 5
    monkeypatch.setenv("DECODO_PAGE_ATTEMPTS", "abc")
    assert _decodo_page_attempts() == 5


def _enable_decodo(monkeypatch):
    _clear_decodo(monkeypatch)
    monkeypatch.setenv("DECODO_USERNAME", "u")
    monkeypatch.setenv("DECODO_PASSWORD", "p")
    monkeypatch.setenv("DECODO_SITES", "aptekonline")
    monkeypatch.setenv("DECODO_PAGE_ATTEMPTS", "5")


@pytest.mark.asyncio
async def test_scrape_category_decodo_retries_past_transient_522(monkeypatch):
    """Decodo: 2 первых IP дают 522, 3-й — 200. Ретрай по портам должен пробить."""
    _enable_decodo(monkeypatch)
    payload = _load_fixture()
    payload["last_page"] = 1
    payload["next_page_url"] = None
    patcher, idx = _mock_httpx_status_seq([522, 522, 200], payload)
    with patcher:
        scraper = AptekonlineScraper()
        products = [p async for p in scraper.scrape_category("114")]
    assert len(products) == len(payload["data"])  # пробились на 3-м порту
    assert idx["n"] == 3  # два 522 отретраены, затем 200


@pytest.mark.asyncio
async def test_scrape_category_decodo_skips_then_aborts_on_persistent_522(monkeypatch):
    """Постоянный 522: страница пропускается (skip), после 3 подряд — обрыв
    категории (не бесконечный перебор всех max_pages)."""
    _enable_decodo(monkeypatch)
    patcher, idx = _mock_httpx_status_seq([522], None)  # всегда 522
    with patcher:
        scraper = AptekonlineScraper()
        products = [p async for p in scraper.scrape_category("114")]
    assert products == []
    # 3 пропущенных страницы × 5 attempts = 15 запросов, затем обрыв
    assert idx["n"] == 15


@pytest.mark.asyncio
async def test_scrape_category_decodo_aborts_on_403_hard_block(monkeypatch):
    """403 — жёсткий блок: ретрай по портам исчерпывается, категория обрывается
    сразу (НЕ skip-continue, в отличие от транзиентного 522)."""
    _enable_decodo(monkeypatch)
    patcher, idx = _mock_httpx_status_seq([403], None)
    with patcher:
        scraper = AptekonlineScraper()
        products = [p async for p in scraper.scrape_category("114")]
    assert products == []
    # _fetch_page ретраит 403 по 5 портам, вернёт 403 → hard-block → обрыв
    assert idx["n"] == 5
