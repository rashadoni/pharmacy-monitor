"""Скрейпер для aptekonline.az — backend JSON API (без Playwright).

Aptekonline.az ввёл reCAPTCHA на headless-браузеры в начале мая 2026 — Playwright
больше не пробивает категории. Зато их Angular фронт ходит за товарами в открытый
backend `GET /shop/productList` (Laravel-style pagination, 100 items/page), который
возвращает готовый JSON и не зависит от JS-выполнения у клиента.

Endpoint reverse-engineered из `https://www.aptekonline.az/assets/js/main.js?v=35`:

    GET https://www.aptekonline.az/shop/productList
    params:
        categoryId[]=<numeric_id>   # категория (тот же id что в `/products/{N}`)
        lang=az                      # язык
        page=<N>                     # пагинация
    headers:
        X-Requested-Width: XMLHttpRequest    (their typo, Width vs With)
        checkus: $2y$10$...                  (статичный bcrypt-токен из main.js)

Ответ (Laravel paginator):

    {
      "thumb_folder": "https://www.aptekonline.az/storage/ProductThumbs/",
      "current_page": 1, "last_page": 10, "total": 943, "per_page": 100,
      "data": [
        {"name": "...", "name_ru": "...", "price": 5.28, "discount_price": null,
         "url_id": "AMRIZOLE-N--N10-vagsupp", "thumb1": "thumb_54907_84.jpg",
         "olke": "Misir", "terkib": "Metronidazol Nistatin",
         "cashback_percent": null, "qaliq": 1, "status": 1, "vahid": "SVEC", ...},
        ...
      ]
    }

scrape_promos() остаётся на Playwright — главная страница без JS грузится нормально
из любого IP, reCAPTCHA не возникает.
"""

from __future__ import annotations

import os
from typing import AsyncIterator

import httpx
import structlog

from src.normalize import (
    extract_dosage,
    extract_pack_size,
    normalize_name,
)
from src.scrapers.base import BaseScraper, ScrapedProduct, ScrapedPromo

log = structlog.get_logger()


def _iproyal_httpx_proxy_for(site_name: str) -> str | None:
    """IPRoyal Residential proxy URL для httpx-клиента.

    Mirror логика из base._iproyal_proxy_for, возвращает один URL для httpx.
    IPRoyal — приоритетный provider (дешевле BD, без KYC).
    """
    username = os.getenv("IPROYAL_USERNAME")
    password = os.getenv("IPROYAL_PASSWORD")
    if not username or not password:
        return None
    sites_csv = os.getenv("IPROYAL_SITES", "")
    sites = {s.strip() for s in sites_csv.split(",") if s.strip()}
    if site_name not in sites:
        return None
    country = os.getenv("IPROYAL_COUNTRY", "").strip().lower()
    if country and "_country-" not in username:
        username = f"{username}_country-{country}"
    host = os.getenv("IPROYAL_HOST", "geo.iproyal.com:12321").strip()
    # IPRoyal residential MITMs HTTPS → use verify=False on httpx.AsyncClient.
    return f"http://{username}:{password}@{host}"


def _brightdata_httpx_proxy_for(site_name: str) -> str | None:
    """Bright Data Residential proxy URL для httpx-клиента.

    Mirror логика из base._brightdata_proxy_for, но возвращает один URL —
    httpx понимает строку проще чем Playwright-style dict. Bright Data
    residential pool — единственный реалистичный путь для прода: Hetzner IP
    забанен и в JSON API aptekonline (HTTP 403) тоже.

    Returns None если creds или site list missing → caller провалится на
    следующий провайдер в цепочке (Crawlbase → ScraperAPI → direct).
    """
    username = os.getenv("BRIGHTDATA_USERNAME")
    password = os.getenv("BRIGHTDATA_PASSWORD")
    if not username or not password:
        return None
    sites_csv = os.getenv("BRIGHTDATA_SITES", "")
    sites = {s.strip() for s in sites_csv.split(",") if s.strip()}
    if site_name not in sites:
        return None
    country = os.getenv("BRIGHTDATA_COUNTRY", "").strip().lower()
    if country and "-country-" not in username:
        username = f"{username}-country-{country}"
    host = os.getenv("BRIGHTDATA_HOST", "brd.superproxy.io:33335").strip()
    # Bright Data residential MITMs HTTPS — caller должен использовать verify=False.
    return f"http://{username}:{password}@{host}"


def _scraperapi_httpx_proxy_for(site_name: str) -> str | None:
    """ScraperAPI proxy URL для httpx-клиента (если настроен в env).

    Mirror логика из base._scraperapi_proxy_for, но возвращает один URL —
    httpx понимает строку проще чем Playwright-style dict.

    Дополнительный env:
      SCRAPER_API_PREMIUM_SITES  CSV сайтов, которым нужен premium=true
                                 (residential-пул, +10 кредитов/запрос).
                                 Требуется для гео-стран, доступных ТОЛЬКО
                                 через residential-тариф — напр. Azerbaijan
                                 для aptekonline (без premium ScraperAPI
                                 отдаёт 403 "country requires premium tier").
                                 Флаг точечный, чтобы не навешивать платный
                                 premium на дешёвые datacenter-фоллбэки.
    """
    key = os.getenv("SCRAPER_API_KEY")
    if not key:
        return None
    sites_csv = os.getenv("SCRAPER_API_SITES", "")
    sites = {s.strip() for s in sites_csv.split(",") if s.strip()}
    if site_name not in sites:
        return None
    # ScraperAPI proxy-mode: флаги в username через точку, напр.
    # "scraperapi.country_code=az.premium=true". country_code бесплатен,
    # premium=true стоит +10 кредитов (residential).
    flags: list[str] = []
    # NB: SCRAPER_API_COUNTRY — ГЛОБАЛЬНЫЙ (не per-site, в отличие от premium
    # ниже). Он применится КО ВСЕМ сайтам в SCRAPER_API_SITES. Сейчас это
    # безвредно (только aptekonline реально ходит через ScraperAPI; pharmonline
    # там лишь deep-fallback за IPRoyal DDP), но если задашь country под один
    # сайт — он молча затронет и второй. Захочешь scope per-site — заведи
    # SCRAPER_API_COUNTRY_SITES по образцу premium ниже.
    country = os.getenv("SCRAPER_API_COUNTRY", "").strip()
    if country:
        flags.append(f"country_code={country}")
    premium_csv = os.getenv("SCRAPER_API_PREMIUM_SITES", "")
    premium_sites = {s.strip() for s in premium_csv.split(",") if s.strip()}
    if site_name in premium_sites:
        flags.append("premium=true")
    user = ".".join(["scraperapi", *flags])
    # ScraperAPI proxy MITMs HTTPS — caller должен использовать verify=False
    # на httpx.AsyncClient. ВНИМАНИЕ: возвращаемый URL содержит SCRAPER_API_KEY
    # как пароль прокси — НЕ логировать его verbatim (call-site логирует только
    # provider+country, не URL).
    return f"http://{user}:{key}@proxy-server.scraperapi.com:8001"


def _crawlbase_httpx_proxy_for(site_name: str) -> str | None:
    """Crawlbase Smart Proxy URL для httpx (если настроен).

    Используем **Normal Token** (CRAWLBASE_NORMAL_TOKEN) — aptekonline backend
    отдаёт чистый JSON без JS, дешевле чем JS-токен. Если есть только JS-токен
    тоже сойдёт, но overkill для JSON-эндпоинта.
    """
    token = os.getenv("CRAWLBASE_NORMAL_TOKEN") or os.getenv("CRAWLBASE_JS_TOKEN")
    if not token:
        return None
    sites_csv = os.getenv("CRAWLBASE_SITES") or os.getenv("SCRAPER_API_SITES", "")
    sites = {s.strip() for s in sites_csv.split(",") if s.strip()}
    if site_name not in sites:
        return None
    # USER_TOKEN — username, пароль пустой
    return f"http://{token}:@smartproxy.crawlbase.com:8012"


_API_PRODUCT_LIST = "https://www.aptekonline.az/shop/productList"

# checkus — bcrypt-захэшированный API-ключ, статичный для всех клиентов
# (не привязан к user_id). Скопирован из main.js?v=35 (зафиксирован 2026-05-07).
# Если сайт его ротейтит — переопредели через env APTEKONLINE_CHECKUS без
# редеплоя кода. Default остаётся как fallback для совместимости.
_DEFAULT_CHECKUS = "$2y$10$heJNZP6TbdT.DmpZlHp87u1NY7RqdXXV5Ht/rBbvjSsEIqgi42/ku"


def _resolve_checkus_token() -> str:
    """Resolve aptekonline `checkus` API token.

    - APTEKONLINE_CHECKUS unset → use _DEFAULT_CHECKUS (back-compat).
    - APTEKONLINE_CHECKUS set & non-empty → use env value (rotation path).
    - APTEKONLINE_CHECKUS set & empty → RuntimeError (fail-fast, no silent fallback).
    """
    env_val = os.getenv("APTEKONLINE_CHECKUS")
    if env_val is None:
        return _DEFAULT_CHECKUS
    stripped = env_val.strip()
    if not stripped:
        raise RuntimeError(
            "APTEKONLINE_CHECKUS is set but empty — refusing to start aptekonline "
            "scraper. Either unset the env var to use the bundled default token, "
            "or provide a valid bcrypt-style token from main.js."
        )
    return stripped


# Заголовки скопированы 1-в-1 из main.js — иначе backend возвращает HTML-редирект
# на страницу логина вместо JSON.
_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "X-Requested-Width": "XMLHttpRequest",
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "az-AZ,az;q=0.9,ru-RU;q=0.8,ru;q=0.7,en-US;q=0.6,en;q=0.5",
    "checkus": _resolve_checkus_token(),
}


def _build_product_from_api(
    item: dict, category_slug: str, thumb_folder: str, base_url: str
) -> ScrapedProduct | None:
    """Маппинг JSON-айтема → ScrapedProduct. None если нет нужных полей."""
    url_id = item.get("url_id")
    name = (item.get("name") or "").strip()
    if not url_id or not name:
        return None

    try:
        price = float(item["price"]) if item.get("price") not in (None, "") else None
    except (TypeError, ValueError):
        price = None
    try:
        discount_price = (
            float(item["discount_price"]) if item.get("discount_price") not in (None, "") else None
        )
    except (TypeError, ValueError):
        discount_price = None

    is_on_sale = discount_price is not None and price is not None and 0 < discount_price < price
    discount_percent = None
    if is_on_sale:
        discount_percent = round((1 - discount_price / price) * 100, 1)

    image_url = None
    thumb1 = item.get("thumb1")
    if thumb1 and thumb_folder:
        image_url = f"{thumb_folder}{thumb1}"

    cashback_pct = item.get("cashback_percent")
    try:
        cashback_pct_num = float(cashback_pct) if cashback_pct not in (None, "") else None
    except (TypeError, ValueError):
        cashback_pct_num = None
    promo_label = (
        f"{cashback_pct_num:g}% kəşbək" if cashback_pct_num and cashback_pct_num > 0 else None
    )

    # API не отдаёт brand отдельно. Для категории передаём тот же slug что был
    # передан скраперу — это совместимо со старыми данными в БД.
    return ScrapedProduct(
        site="aptekonline",
        external_id=str(url_id),
        url=f"{base_url}/product/{url_id}",
        name=name,
        brand=None,
        manufacturer=(item.get("olke") or None),
        category=str(category_slug),
        dosage=extract_dosage(name),
        pack_size=extract_pack_size(name),
        image_url=image_url,
        description=(item.get("terkib") or None),
        price=price,
        discount_price=discount_price if is_on_sale else None,
        discount_percent=discount_percent,
        is_on_sale=is_on_sale,
        promo_label=promo_label,
    )


class AptekonlineScraper(BaseScraper):
    site_name = "aptekonline"
    base_url = "https://www.aptekonline.az"

    async def scrape_category(
        self, category_slug: str, limit: int | None = None, max_pages: int = 50
    ) -> AsyncIterator[ScrapedProduct]:
        """Стримим продукты одной категории через JSON API.

        category_slug — строковый числовой ID (например "114"), как в БД сейчас.
        max_pages — safety net против бесконечной пагинации.
        """
        if not category_slug:
            return
        category_id = str(category_slug).strip()
        if not category_id.isdigit():
            log.warning("aptekonline_skip_non_numeric_category", category=category_slug)
            return

        seen: set[str] = set()
        yielded = 0
        params_base = [("categoryId[]", category_id), ("lang", "az")]

        # Proxy resolution: IPRoyal → Bright Data → Crawlbase → ScraperAPI → direct.
        # IPRoyal первым — дешевле ($1.75/GB vs BD $8/GB) и без KYC.
        proxy_url = _iproyal_httpx_proxy_for("aptekonline")
        proxied_via = "iproyal" if proxy_url else None
        if proxy_url is None:
            proxy_url = _brightdata_httpx_proxy_for("aptekonline")
            proxied_via = "brightdata" if proxy_url else None
        if proxy_url is None:
            proxy_url = _crawlbase_httpx_proxy_for("aptekonline")
            proxied_via = "crawlbase" if proxy_url else None
        if proxy_url is None:
            proxy_url = _scraperapi_httpx_proxy_for("aptekonline")
            proxied_via = "scraperapi" if proxy_url else None

        client_kwargs: dict = {
            "headers": _API_HEADERS,
            "timeout": httpx.Timeout(90.0 if proxy_url else 30.0),
        }
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
            client_kwargs["verify"] = False  # MITM HTTPS на любом из этих прокси
            _country_env_by_provider = {
                "iproyal": "IPROYAL_COUNTRY",
                "brightdata": "BRIGHTDATA_COUNTRY",
                "scraperapi": "SCRAPER_API_COUNTRY",
            }
            log.info(
                f"aptekonline_using_{proxied_via}",
                country=os.getenv(
                    _country_env_by_provider.get(proxied_via, "SCRAPER_API_COUNTRY"),
                    "default",
                ),
            )
        async with httpx.AsyncClient(**client_kwargs) as client:
            for page_num in range(1, max_pages + 1):
                if limit is not None and yielded >= limit:
                    return
                params = list(params_base) + [("page", str(page_num))]
                try:
                    resp = await client.get(_API_PRODUCT_LIST, params=params)
                except (httpx.RequestError, httpx.TimeoutException) as e:
                    log.warning(
                        "aptekonline_api_request_failed",
                        category=category_slug,
                        page=page_num,
                        error=str(e),
                    )
                    return
                if resp.status_code != 200:
                    log.warning(
                        "aptekonline_api_status",
                        category=category_slug,
                        page=page_num,
                        status=resp.status_code,
                    )
                    return
                try:
                    payload = resp.json()
                except ValueError as e:
                    log.warning(
                        "aptekonline_api_invalid_json",
                        category=category_slug,
                        page=page_num,
                        error=str(e),
                    )
                    return

                items = payload.get("data") or []
                thumb_folder = payload.get("thumb_folder") or ""

                if page_num == 1:
                    log.info(
                        "aptekonline_api_loaded",
                        category=category_slug,
                        total=payload.get("total"),
                        last_page=payload.get("last_page"),
                        per_page=payload.get("per_page"),
                    )

                if not items:
                    break

                for item in items:
                    if limit is not None and yielded >= limit:
                        return
                    product = _build_product_from_api(
                        item, category_slug, thumb_folder, self.base_url
                    )
                    if not product:
                        continue
                    if product.external_id in seen:
                        continue
                    seen.add(product.external_id)
                    yielded += 1
                    yield product

                # Pagination: stop at last page or when next_page_url отсутствует
                last_page = payload.get("last_page")
                if last_page and page_num >= int(last_page):
                    break
                if not payload.get("next_page_url"):
                    break

    async def scrape_product_page(self, url: str) -> ScrapedProduct | None:
        """Watchlist-режим: один товар.

        Pre-API мы заходили на /product/{slug} через Playwright. Сейчас slug
        совпадает с url_id в API ответе, поэтому достаточно поискать его через
        productList по всем категориям — но это N запросов. Проще парсить
        страницу товара через тот же API endpoint c параметром keywords:
        backend ищет по name_ru/name_az. Возвращаем первый match.
        """
        slug = url.rstrip("/").split("/")[-1]
        if not slug:
            return None

        # Конвертируем slug "AMRIZOLE-N--N10-vagsupp" → keyword query
        # Слэшированный slug сам по себе не годится для search — берём первое слово.
        keyword = slug.replace("--", " ").replace("-", " ").strip()
        if not keyword:
            return None

        params = [("lang", "az"), ("keywords", keyword[:100])]
        try:
            async with httpx.AsyncClient(
                headers=_API_HEADERS, timeout=httpx.Timeout(30.0)
            ) as client:
                resp = await client.get(_API_PRODUCT_LIST, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as e:
            log.warning("aptekonline_product_page_api_failed", url=url, error=str(e))
            return None

        thumb_folder = payload.get("thumb_folder") or ""
        for item in payload.get("data") or []:
            if str(item.get("url_id") or "").lower() == slug.lower():
                return _build_product_from_api(item, "watchlist", thumb_folder, self.base_url)

        log.info("aptekonline_product_page_not_found", url=url, slug=slug, keyword=keyword)
        return None

    async def scrape_promos(self) -> list[ScrapedPromo]:
        """Промо/баннеры — оставляем на Playwright (главная не имеет reCAPTCHA на public banners).

        Если IP-бан или reCAPTCHA сработает — возвращаем пустой список,
        не валим весь скрейп.
        """
        try:
            page = await self.new_page()
        except Exception as e:
            log.warning("aptekonline_promos_browser_failed", error=str(e))
            return []
        try:
            try:
                await self.goto(page, self.base_url)
            except Exception as e:
                log.warning("aptekonline_promos_goto_failed", error=str(e))
                return []
            promos: list[ScrapedPromo] = []
            banners = await page.query_selector_all(
                '[class*="banner"], swiper-slide, .owl-item, .carousel-item'
            )
            for banner in banners[:10]:
                try:
                    text = (await banner.inner_text()).strip()
                    if not text or len(text) > 500:
                        continue
                    img = await banner.query_selector("img")
                    img_url = await img.get_attribute("src") if img else None
                    link = await banner.query_selector("a")
                    href = await link.get_attribute("href") if link else None
                    promos.append(
                        ScrapedPromo(
                            site=self.site_name,
                            title=text[:200],
                            image_url=img_url,
                            landing_url=href,
                        )
                    )
                except Exception:
                    continue
            return promos
        finally:
            try:
                await page.close()
            except Exception:
                pass


def to_db_dict(p: ScrapedProduct) -> dict:
    return {
        "site": p.site,
        "external_id": p.external_id,
        "url": p.url,
        "name": p.name,
        "name_normalized": normalize_name(p.name),
        "brand": p.brand,
        "manufacturer": p.manufacturer,
        "category": p.category,
        "dosage": p.dosage,
        "pack_size": p.pack_size,
        "image_url": p.image_url,
        "description": p.description,
    }
