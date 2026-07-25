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

import asyncio
import itertools
import os
from typing import AsyncIterator
from urllib.parse import quote

import httpx
import structlog

from src.normalize import (
    extract_dosage,
    extract_pack_size,
    normalize_name,
)
from src.scrapers.base import (
    BaseScraper,
    ScrapedProduct,
    ScrapedPromo,
    SiteScrapeFatalError,
    fatal_proxy_reason,
)

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


_DECODO_HOST_DEFAULT = "az.decodo.com"

# Транзиентно провалившуюся страницу (сеть/5xx/522 после ретраев) ПРОПУСКАЕМ и
# идём дальше — не обрываем всю категорию (Decodo residential даёт ~38% флайки на
# отдельный IP; одна сбойная страница не повод терять остальные). Жёсткий блок
# (403/401/451) ИЛИ исчерпание баланса прокси (402/407) → ретрай бесполезен,
# обрыв. NB 407 (proxy-auth / у Decodo кончился PAYG-баланс): без него 407 считался
# бы транзиентом → 5 ретраев × портов на каждой странице жгли бы остаток баланса
# вслепую, а run всё равно вернул бы run_ok с заниженным products. N провалов
# подряд → реальная проблема (провайдер лёг / систематический блок), тоже обрыв.
_HARD_BLOCK_STATUSES = {401, 402, 403, 407, 451}
_MAX_CONSECUTIVE_PAGE_FAILURES = 3


def _decodo_enabled(site_name: str) -> bool:
    """Decodo настроен для сайта (creds заданы И сайт в DECODO_SITES)."""
    if not (os.getenv("DECODO_USERNAME") and os.getenv("DECODO_PASSWORD")):
        return False
    sites = {s.strip() for s in os.getenv("DECODO_SITES", "").split(",") if s.strip()}
    return site_name in sites


def _decodo_ports(site_name: str) -> list[int]:
    """Порты Decodo для site — каждый порт = отдельная sticky AZ-сессия (свой IP).

    Цикл по портам = ретрай на разных IP: residential-пул Decodo даёт ~38%
    транзиентных 522 на отдельный IP, повтор на другом порту это лечит.
    DECODO_PORTS: диапазон "30001-30010" или CSV. Пусто если Decodo не настроен.
    """
    if not _decodo_enabled(site_name):
        return []
    raw = os.getenv("DECODO_PORTS", "30001-30010").strip()
    ports: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.strip().isdigit() and hi.strip().isdigit():
                ports.extend(range(int(lo), int(hi) + 1))
        elif part.isdigit():
            ports.append(int(part))
    return ports


def _decodo_page_attempts() -> int:
    """Сколько IP перебрать на одну страницу до отказа (деф. весь пул из 10)."""
    raw = os.getenv("DECODO_PAGE_ATTEMPTS", "10").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 10


def _decodo_retry_delay_seconds() -> float:
    """Пауза между сменами IP после транзиентного ответа прокси."""
    raw = os.getenv("DECODO_RETRY_DELAY_SECONDS", "0.5").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.5


def _category_attempts() -> int:
    """Число полных согласованных проходов категории перед отказом."""
    raw = os.getenv("APTEKONLINE_CATEGORY_ATTEMPTS", "3").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 3


def _decodo_httpx_proxy_for(site_name: str, port: int) -> str | None:
    """Decodo residential proxy URL для одного порта (sticky AZ-сессия).

    Гео (Азербайджан) закодировано в host (az.decodo.com), НЕ в username —
    country-флаг не нужен. Пароль URL-кодируется (может содержать '='/спецсимволы).
    ВНИМАНИЕ: URL несёт DECODO_PASSWORD — не логировать verbatim.
    """
    user = os.getenv("DECODO_USERNAME")
    pwd = os.getenv("DECODO_PASSWORD")
    if not user or not pwd:
        return None
    sites = {s.strip() for s in os.getenv("DECODO_SITES", "").split(",") if s.strip()}
    if site_name not in sites:
        return None
    host = os.getenv("DECODO_HOST", _DECODO_HOST_DEFAULT).strip()
    return f"http://{user}:{quote(pwd, safe='')}@{host}:{port}"


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

    from src.product_policy import offer_from_quantity

    country_raw = str(item.get("olke") or "").strip() or None
    availability_status, offer_quantity = offer_from_quantity(item.get("qaliq"))

    # API не отдаёт brand отдельно. Для категории передаём тот же slug что был
    # передан скраперу — это совместимо со старыми данными в БД.
    return ScrapedProduct(
        site="aptekonline",
        external_id=str(url_id),
        url=f"{base_url}/product/{url_id}",
        name=name,
        brand=None,
        manufacturer=None,
        manufacturer_country_raw=country_raw,
        country_source="aptek_api_olke",
        offer_availability_status=availability_status,
        offer_quantity=offer_quantity,
        availability_source="aptek_api_qaliq",
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
        """Буферизуем и при необходимости повторяем всю категорию.

        Laravel paginator не является snapshot: добавление/удаление товара во
        время обхода может изменить ``total`` и сдвинуть страницы. Кроме того,
        отдельный AZ residential exit иногда отвечает 522. Частичный проход
        нельзя отдавать наружу (incremental persist сохранит его как будто он
        полный), поэтому публикуем товары только после согласованной попытки.
        """
        best_products: list[ScrapedProduct] = []
        best_status = None
        attempts = _category_attempts()
        for attempt in range(1, attempts + 1):
            products = [
                product
                async for product in self._scrape_category_once(
                    category_slug,
                    limit=limit,
                    max_pages=max_pages,
                )
            ]
            status = self._route_statuses.get(str(category_slug))
            if status is not None and status.complete:
                if attempt > 1:
                    log.info(
                        "aptekonline_category_retry_recovered",
                        category=category_slug,
                        attempt=attempt,
                        products=len(products),
                    )
                for product in products:
                    yield product
                return

            if len(products) > len(best_products) or best_status is None:
                best_products = products
                best_status = status
            if (
                status is None
                or status.abort_reason
                in {
                    "invalid_category",
                    "invalid_last_page",
                    "invalid_total",
                    "requested_limit_reached",
                }
                or (status.abort_reason or "").startswith("hard_block_")
            ):
                break
            if attempt < attempts:
                log.warning(
                    "aptekonline_category_retrying",
                    category=category_slug,
                    attempt=attempt,
                    max_attempts=attempts,
                    reason=status.abort_reason or "incomplete_route",
                    pages_skipped=status.pages_skipped,
                )

        if best_status is not None:
            self._route_statuses[str(category_slug)] = best_status
        for product in best_products:
            yield product

    async def _scrape_category_once(
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
            self._set_route_status(category_slug, complete=False, abort_reason="invalid_category")
            return

        seen: set[str] = set()
        yielded = 0
        params_base = [("categoryId[]", category_id), ("lang", "az")]

        # Proxy resolution: Decodo (AZ residential) → IPRoyal → Bright Data →
        # Crawlbase → ScraperAPI → direct. Decodo первым: единственный рабочий
        # азербайджанский residential-пул (aptek принимает только AZ-IP).
        decodo_ports = _decodo_ports("aptekonline")
        port_cycle = itertools.cycle(decodo_ports) if decodo_ports else None
        persistent_client: httpx.AsyncClient | None = None

        if port_cycle is not None:
            proxied_via = "decodo"
            log.info(
                "aptekonline_using_decodo",
                host=os.getenv("DECODO_HOST", _DECODO_HOST_DEFAULT),
                ports=len(decodo_ports),
                attempts_per_page=_decodo_page_attempts(),
            )
        else:
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
                client_kwargs["verify"] = False  # MITM HTTPS на этих прокси
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
            persistent_client = httpx.AsyncClient(**client_kwargs)

        async def _fetch_page(req_params: list) -> httpx.Response | None:
            """GET страницы. Decodo — ретрай по портам (порт = другой AZ-IP;
            ~38% IP дают транзиентный 522). Прочие — один persistent client."""
            if port_cycle is not None:
                last_resp: httpx.Response | None = None
                for _ in range(_decodo_page_attempts()):
                    purl = _decodo_httpx_proxy_for("aptekonline", next(port_cycle))
                    try:
                        async with httpx.AsyncClient(
                            headers=_API_HEADERS,
                            timeout=httpx.Timeout(60.0),
                            proxy=purl,
                            verify=False,
                        ) as c:
                            r = await c.get(_API_PRODUCT_LIST, params=req_params)
                        if r.status_code == 200:
                            return r
                        last_resp = r
                        if r.status_code in {402, 407}:
                            raise SiteScrapeFatalError(
                                f"Decodo proxy access rejected: HTTP {r.status_code}"
                            )
                        # Жёсткий блок (403/401/451 или баланс 402/407): ретрай по
                        # ДРУГИМ портам бесполезен (это не флайки IP) — возвращаем
                        # сразу, не жжём оставшиеся попытки/баланс прокси.
                        if r.status_code in _HARD_BLOCK_STATUSES:
                            return r
                        delay = _decodo_retry_delay_seconds()
                        if delay:
                            await asyncio.sleep(delay)
                    except httpx.ProxyError as exc:
                        reason = fatal_proxy_reason(exc)
                        if reason is not None:
                            raise SiteScrapeFatalError(reason) from exc
                        delay = _decodo_retry_delay_seconds()
                        if delay:
                            await asyncio.sleep(delay)
                        continue
                    except httpx.RequestError:
                        delay = _decodo_retry_delay_seconds()
                        if delay:
                            await asyncio.sleep(delay)
                        continue
                return last_resp
            try:
                response = await persistent_client.get(_API_PRODUCT_LIST, params=req_params)
                if proxied_via and response.status_code in {402, 407}:
                    raise SiteScrapeFatalError(
                        f"{proxied_via} proxy access rejected: HTTP {response.status_code}"
                    )
                return response
            except httpx.ProxyError as exc:
                reason = fatal_proxy_reason(exc)
                if reason is not None:
                    raise SiteScrapeFatalError(reason) from exc
                return None
            except httpx.RequestError:
                return None

        pages_skipped = 0
        consecutive_failures = 0
        visited_pages = 0
        expected_pages: int | None = None
        expected_items: int | None = None
        raw_items = 0
        parsed_items = 0
        item_failures = 0
        abort_reason: str | None = None
        try:
            for page_num in range(1, max_pages + 1):
                if limit is not None and yielded >= limit:
                    abort_reason = "requested_limit_reached"
                    return
                params = list(params_base) + [("page", str(page_num))]
                resp = await _fetch_page(params)
                status = None if resp is None else resp.status_code
                if status != 200:
                    if status in _HARD_BLOCK_STATUSES:
                        log.warning(
                            "aptekonline_api_blocked",
                            category=category_slug,
                            page=page_num,
                            status=status,
                            provider=proxied_via,
                        )
                        abort_reason = f"hard_block_{status}"
                        return
                    # Транзиент (сеть/5xx/522): пропускаем страницу, идём дальше.
                    pages_skipped += 1
                    consecutive_failures += 1
                    log.warning(
                        "aptekonline_page_skipped",
                        category=category_slug,
                        page=page_num,
                        status=status,
                        provider=proxied_via,
                        consecutive=consecutive_failures,
                    )
                    if consecutive_failures >= _MAX_CONSECUTIVE_PAGE_FAILURES:
                        log.warning(
                            "aptekonline_category_aborted",
                            category=category_slug,
                            page=page_num,
                            consecutive=consecutive_failures,
                            provider=proxied_via,
                        )
                        abort_reason = "consecutive_page_failures"
                        return
                    continue
                try:
                    payload = resp.json()
                except ValueError as e:
                    pages_skipped += 1
                    consecutive_failures += 1
                    log.warning(
                        "aptekonline_api_invalid_json",
                        category=category_slug,
                        page=page_num,
                        error=str(e),
                        consecutive=consecutive_failures,
                    )
                    if consecutive_failures >= _MAX_CONSECUTIVE_PAGE_FAILURES:
                        abort_reason = "consecutive_invalid_json"
                        return
                    continue
                consecutive_failures = 0
                visited_pages += 1

                items = payload.get("data") or []
                raw_items += len(items)
                thumb_folder = payload.get("thumb_folder") or ""
                raw_last_page = payload.get("last_page")
                if raw_last_page not in (None, ""):
                    try:
                        expected_pages = int(raw_last_page)
                    except (TypeError, ValueError):
                        abort_reason = "invalid_last_page"
                        return
                raw_total = payload.get("total")
                if raw_total not in (None, ""):
                    try:
                        page_expected_items = int(raw_total)
                    except (TypeError, ValueError):
                        abort_reason = "invalid_total"
                        return
                    if expected_items is None:
                        expected_items = page_expected_items
                    elif expected_items != page_expected_items:
                        abort_reason = "total_changed_during_pagination"
                        return

                if page_num == 1:
                    log.info(
                        "aptekonline_api_loaded",
                        category=category_slug,
                        total=payload.get("total"),
                        last_page=payload.get("last_page"),
                        per_page=payload.get("per_page"),
                    )

                if not items:
                    if expected_pages is not None and page_num < expected_pages:
                        abort_reason = "empty_page_before_last"
                    break

                for item in items:
                    if limit is not None and yielded >= limit:
                        abort_reason = "requested_limit_reached"
                        return
                    product = _build_product_from_api(
                        item, category_slug, thumb_folder, self.base_url
                    )
                    if not product:
                        item_failures += 1
                        continue
                    if product.external_id in seen:
                        continue
                    seen.add(product.external_id)
                    parsed_items += 1
                    yielded += 1
                    yield product

                # Pagination: stop at last page or when next_page_url отсутствует
                last_page = payload.get("last_page")
                if last_page and page_num >= int(last_page):
                    break
                if not payload.get("next_page_url"):
                    break
            if pages_skipped:
                log.warning(
                    "aptekonline_category_incomplete",
                    category=category_slug,
                    pages_skipped=pages_skipped,
                    provider=proxied_via,
                )
        finally:
            complete = (
                abort_reason is None
                and pages_skipped == 0
                and item_failures == 0
                and (expected_pages is None or visited_pages >= expected_pages)
                and (
                    expected_items is None
                    or (raw_items == expected_items and parsed_items == expected_items)
                )
            )
            self._set_route_status(
                category_slug,
                complete=complete,
                pages_skipped=pages_skipped,
                abort_reason=abort_reason,
                expected_pages=expected_pages,
                visited_pages=visited_pages,
                raw_items=raw_items,
                parsed_items=parsed_items,
                item_failures=item_failures,
                expected_items=expected_items,
            )
            if persistent_client is not None:
                await persistent_client.aclose()

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
        except SiteScrapeFatalError:
            raise
        except Exception as e:
            reason = fatal_proxy_reason(e)
            if reason is not None:
                raise SiteScrapeFatalError(reason) from e
            log.warning("aptekonline_promos_browser_failed", error=str(e))
            return []
        try:
            try:
                await self.goto(page, self.base_url)
            except SiteScrapeFatalError:
                raise
            except Exception as e:
                reason = fatal_proxy_reason(e)
                if reason is not None:
                    raise SiteScrapeFatalError(reason) from e
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
