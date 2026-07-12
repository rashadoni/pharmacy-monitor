"""BaseScraper — общий интерфейс и инфраструктура для всех 3 сайтов.

Robustness features (W10):
  - Proxy support via HTTP_PROXY env (residential proxy plan recommended)
  - Anti-detection: stealth patches, large UA pool, realistic viewport
  - Captcha detection: log + skip if bot-wall encountered
  - Smart retry: exponential backoff with jitter, separate handling for 429/5xx
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.scrapers.anti_detection import (
    USER_AGENTS_EXTENDED,
    apply_stealth,
    random_user_agent,
    random_viewport,
)
from src.scrapers.captcha import detect_captcha

log = structlog.get_logger()

# Backwards-compat alias (used by tests / external callers)
USER_AGENTS = tuple(USER_AGENTS_EXTENDED)


class CaptchaDetected(Exception):
    """Raised when a page is blocked by captcha / bot-wall.

    Caught at the scrape_category level — the page is skipped and logged,
    but doesn't kill the overall run.
    """


class SiteScrapeFatalError(RuntimeError):
    """Account/provider failure that makes every remaining site item impossible.

    Unlike a transient page failure, retrying the next category cannot recover
    from an exhausted proxy balance or rejected proxy credentials.
    """


def fatal_proxy_status(exc: BaseException) -> int | None:
    """Extract only account-level proxy statuses without leaking proxy URLs."""
    message = str(exc)
    match = re.search(
        r"(?:HTTP\s+|status(?:_code)?[=: ]+|^)(402|407)\b", message, re.I
    )
    if match:
        return int(match.group(1))
    if re.search(
        r"ERR_(?:PROXY_AUTH_REQUESTED|INVALID_AUTH_CREDENTIALS)|"
        r"Proxy Authentication Required",
        message,
        re.I,
    ):
        return 407
    return None


def fatal_proxy_reason(error: BaseException) -> str | None:
    """Return a sanitized site-fatal reason for account/config proxy failures."""
    status = fatal_proxy_status(error)
    if status is not None:
        return f"proxy access rejected: HTTP {status}"
    error_type = type(error).__name__
    # websockets.InvalidProxyStatus / InvalidProxyMessage describe a proxy
    # response, not a malformed local configuration. Residential exits can
    # transiently return 5xx; these must reach the retry loop. Account-level
    # 402/407 was already caught above and remains fail-closed.
    if error_type in {"InvalidProxyStatus", "InvalidProxyMessage"}:
        return None
    diagnostic = f"{error_type}: {error}"
    if error_type in {"InvalidProxy", "UnsupportedProxy"} or re.search(
        r"\b(?:InvalidProxy|UnsupportedProxy)\b|ERR_NO_SUPPORTED_PROXIES",
        diagnostic,
        re.I,
    ):
        return "proxy configuration rejected"
    return None


def site_fatal_error_message(error: BaseException) -> str:
    """Return bounded diagnostics without proxy endpoints or credentials."""
    reason = fatal_proxy_reason(error)
    if reason is not None:
        return reason
    message = f"{type(error).__name__}: {error}"
    message = re.sub(r"https?://\S+", "[redacted-proxy-url]", message, flags=re.I)
    return message[:500]


def site_fatal_result(site: str, item_keys: list[str], error: BaseException) -> ScrapeResult:
    """Build bounded fail-closed telemetry when a site cannot start at all."""
    message = site_fatal_error_message(error)
    return ScrapeResult(
        site=site,
        errors=[f"site_fatal: {message}"],
        site_fatal=True,
        items_expected=len(item_keys),
        items_failed=len(item_keys),
        item_results={
            str(key): {
                "status": "skipped",
                "products": 0,
                "error": message,
                "error_kind": "site_fatal",
            }
            for key in item_keys
        },
    )


def _redact_proxy(url: str) -> str:
    """Hide credentials in proxy URL for logging. http://user:pass@host:8080 → http://***@host:8080"""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    auth, host = rest.rsplit("@", 1)
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


def _iproyal_proxy_for(site_name: str) -> dict | None:
    """Return Playwright proxy config for IPRoyal Residential when site enabled.

    Phase 1.2b (2026-05-27) — IPRoyal added as a cheaper alternative to Bright
    Data: $1.75/GB vs $8/GB, no KYC. Used when BD compliance restrictions block
    target (e.g. pharmonline.az where BD Web Unlocker returns 502).

    Env vars:
      IPROYAL_USERNAME    Dashboard username (just account name, not zone)
      IPROYAL_PASSWORD    Account password
      IPROYAL_SITES       CSV of site_names that should route through it
                          (e.g. "pharmonline" — leave aptekonline on BD)
      IPROYAL_HOST        Override endpoint (default geo.iproyal.com:12321)
      IPROYAL_COUNTRY     Optional ISO-2 (e.g. "tr", "az"). IPRoyal supports
                          `username_country-tr_session-...` suffix syntax;
                          when set, appended as `_country-XX` to username.

    Returns None when creds or site list missing → caller falls through to the
    next provider in chain (Bright Data → Crawlbase → ScraperAPI → direct).
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
    # IPRoyal username suffix syntax: `<user>_country-XX_session-<id>`.
    # We add only country; sticky sessions are not needed for our diff-only
    # daily scrape (rotation per request is fine).
    if country and "_country-" not in username:
        username = f"{username}_country-{country}"
    host = os.getenv("IPROYAL_HOST", "geo.iproyal.com:12321").strip()
    return {
        "server": f"http://{host}",
        "username": username,
        "password": password,
    }


def _decodo_proxy_for(site_name: str) -> dict | None:
    """Playwright proxy config for Decodo residential (AZ pool) when site enabled.

    Для Playwright-сайтов (aloe). Гео (Азербайджан) закодировано в host
    (az.decodo.com), ОДИН sticky-порт (первый из DECODO_PORTS) на сессию браузера.
    Playwright сам кодирует username/password в Proxy-Authorization — URL-encoding
    здесь НЕ нужен (в отличие от httpx-пути, где пароль с '=' квотится вручную).
    None если creds/сайт не настроены → следующий провайдер в цепочке.
    """
    username = os.getenv("DECODO_USERNAME")
    password = os.getenv("DECODO_PASSWORD")
    if not username or not password:
        return None
    sites = {s.strip() for s in os.getenv("DECODO_SITES", "").split(",") if s.strip()}
    if site_name not in sites:
        return None
    host = os.getenv("DECODO_HOST", "az.decodo.com").strip()
    ports = os.getenv("DECODO_PORTS", "30001-30010").strip()
    first = ports.split(",")[0].split("-")[0].strip()
    port = first if first.isdigit() else "30001"
    return {
        "server": f"http://{host}:{port}",
        "username": username,
        "password": password,
    }


def _brightdata_proxy_for(site_name: str) -> dict | None:
    """Return Playwright proxy config for Bright Data Residential when site is enabled.

    Phase 1.2 (2026-05-27). Bright Data is the primary residential proxy provider —
    Hetzner IPs got banned by pharmonline + aptekonline since ~2026-04-29, and
    ScraperAPI default pool stable returned 0 products. Residential rotating IPs
    bypass both bans without per-request fingerprint juggling.

    Env vars:
      BRIGHTDATA_USERNAME       Full username from Access Parameters, e.g.
                                `brd-customer-hl_XXXXXX-zone-pharmacy-monitor`
      BRIGHTDATA_PASSWORD       Zone password
      BRIGHTDATA_SITES          CSV of site_names that should route through it
                                (e.g. "pharmonline,aptekonline" — leave aloe direct)
      BRIGHTDATA_HOST           Override endpoint (default brd.superproxy.io:33335)
      BRIGHTDATA_COUNTRY        Optional ISO-2 (e.g. "tr"). Bright Data supports
                                appending `-country-XX` to the zone username for
                                country-specific routing without per-request config.

    Returns None when creds or site list missing — caller falls through to the
    next provider in chain (Crawlbase → ScraperAPI → HTTP_PROXY → direct).
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
    # If country provided AND username doesn't already encode one, append it.
    # Bright Data username syntax: `brd-customer-X-zone-Y[-country-tr]`.
    if country and "-country-" not in username:
        username = f"{username}-country-{country}"
    host = os.getenv("BRIGHTDATA_HOST", "brd.superproxy.io:33335").strip()
    return {
        "server": f"http://{host}",
        "username": username,
        "password": password,
    }


def _scraperapi_proxy_for(site_name: str) -> dict | None:
    """Return Playwright proxy config for ScraperAPI when this site is configured.

    Reads two env vars:
      SCRAPER_API_KEY            ScraperAPI account key
      SCRAPER_API_SITES          CSV of site_names that should route through it
                                 (e.g. "pharmonline,aptekonline" — leave aloe direct)
      SCRAPER_API_COUNTRY        Optional ISO-2 code (e.g. "tr") for geotargeted IPs

    Returns None when the key is missing or the site is not in the list, so the
    caller falls through to HTTP_PROXY / direct connection.
    """
    key = os.getenv("SCRAPER_API_KEY")
    if not key:
        return None
    sites_csv = os.getenv("SCRAPER_API_SITES", "")
    sites = {s.strip() for s in sites_csv.split(",") if s.strip()}
    if site_name not in sites:
        return None
    country = os.getenv("SCRAPER_API_COUNTRY", "").strip()
    # ScraperAPI accepts feature flags as suffixes on the username — e.g.
    # "scraperapi.country_code=tr". When no country is set, plain "scraperapi"
    # uses their default datacenter pool.
    #
    # NB: premium=true (residential) support intentionally lives ONLY in the
    # httpx sibling _scraperapi_httpx_proxy_for in aptekonline.py — aptekonline
    # is the only site needing AZ residential and it scrapes via httpx, never
    # Playwright. The omission here is deliberate, not an oversight; add it if a
    # Playwright-routed site ever needs a residential pool.
    username = f"scraperapi.country_code={country}" if country else "scraperapi"
    return {
        "server": "http://proxy-server.scraperapi.com:8001",
        "username": username,
        "password": key,
    }


def _crawlbase_proxy_for(site_name: str) -> dict | None:
    """Return Playwright proxy config for Crawlbase Smart Proxy.

    Reads env vars:
      CRAWLBASE_JS_TOKEN     Browser-Enabled API Token (для JS-rendering)
      CRAWLBASE_SITES        CSV of site_names to route (default: same as
                             SCRAPER_API_SITES для backwards compat)

    Crawlbase smart proxy: USER_TOKEN — username, password пустой.
    Endpoint: smartproxy.crawlbase.com:8012. С JS-токеном автоматически
    рендерит JavaScript на их стороне (через Headless Chrome в их облаке).

    Возвращает None если токен не задан или site не в CRAWLBASE_SITES — caller
    провалится дальше по цепочке резолва (ScraperAPI / HTTP_PROXY / direct).
    """
    token = os.getenv("CRAWLBASE_JS_TOKEN")
    if not token:
        return None
    sites_csv = os.getenv("CRAWLBASE_SITES") or os.getenv("SCRAPER_API_SITES", "")
    sites = {s.strip() for s in sites_csv.split(",") if s.strip()}
    if site_name not in sites:
        return None
    return {
        "server": "http://smartproxy.crawlbase.com:8012",
        "username": token,
        "password": "",
    }


@dataclass
class ScrapedProduct:
    """Единый формат товара, возвращаемый любым скрейпером."""

    site: str
    external_id: str
    url: str
    name: str
    brand: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    dosage: str | None = None
    pack_size: str | None = None
    image_url: str | None = None
    description: str | None = None
    price: float | None = None
    discount_price: float | None = None
    discount_percent: float | None = None
    is_on_sale: bool = False
    promo_label: str | None = None
    # Phase 2.1 — canonical barcode (EAN/GTIN/UPC unified). Digits-only string.
    barcode: str | None = None


@dataclass
class ScrapedPromo:
    site: str
    title: str
    description: str | None = None
    image_url: str | None = None
    landing_url: str | None = None
    valid_until: str | None = None
    raw_data: dict = field(default_factory=dict)


@dataclass
class ScrapeResult:
    site: str
    products: list[ScrapedProduct] = field(default_factory=list)
    promos: list[ScrapedPromo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Account/provider failure means this site's result is untrustworthy even
    # when earlier categories/URLs produced partial data.
    site_fatal: bool = False
    # Quality telemetry for the unit of work requested from this site. In
    # category mode the unit is a category slug; in watchlist mode it is a URL.
    # `item_results` is persisted on Run.run_quality so a degraded run can name
    # the exact category/URL that failed instead of looking generically "ok".
    items_expected: int = 0
    items_completed: int = 0
    items_failed: int = 0
    item_results: dict[str, dict] = field(default_factory=dict)


class BaseScraper(ABC):
    """Контракт для скрейперов конкретных сайтов.

    Подкласс должен реализовать `scrape_category` (и опционально `scrape_promos`).
    BaseScraper берёт на себя браузер, rate-limit, retry и сбор результатов.
    """

    site_name: str  # переопределить в подклассе
    base_url: str  # переопределить в подклассе

    def __init__(
        self,
        rate_limit_sec: float | None = None,
        timeout_sec: int | None = None,
        max_retries: int | None = None,
        headless: bool | None = None,
    ):
        self.rate_limit_sec = rate_limit_sec or float(os.getenv("SCRAPE_RATE_LIMIT_SEC", "2"))
        self.timeout_sec = timeout_sec or int(os.getenv("SCRAPE_TIMEOUT_SEC", "30"))
        self.max_retries = max_retries or int(os.getenv("SCRAPE_MAX_RETRIES", "3"))
        env_headless = os.getenv("SCRAPE_HEADLESS", "true").lower() in ("1", "true", "yes")
        self.headless = headless if headless is not None else env_headless
        self._last_request_at = 0.0
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> BaseScraper:
        try:
            return await self._open_browser()
        except Exception as exc:
            await self._close_resources()
            reason = fatal_proxy_reason(exc)
            if reason is not None:
                raise SiteScrapeFatalError(reason) from exc
            raise

    async def _open_browser(self) -> BaseScraper:
        self._playwright = await async_playwright().start()

        # Proxy resolution order (first match wins):
        #   1. IPRoyal residential (IPROYAL_USERNAME/PASSWORD + IPROYAL_SITES)
        #      — cheapest ($1.75/GB), no KYC
        #   2. Bright Data residential (BRIGHTDATA_USERNAME/PASSWORD + SITES)
        #      — $8/GB ISP or Web Unlocker $1.50/CPM, KYC required for some targets
        #   3. Crawlbase Smart Proxy (CRAWLBASE_JS_TOKEN + CRAWLBASE_SITES)
        #   4. ScraperAPI per-site config (SCRAPER_API_KEY + SCRAPER_API_SITES)
        #   5. Generic HTTP_PROXY / SCRAPE_PROXY env (single proxy for everything)
        # Provider-specific configs take precedence so aloe (works direct from
        # Hetzner) не burn'ит платные credits.
        launch_args: dict = {"headless": self.headless}
        proxied_via = None
        decodo_cfg = _decodo_proxy_for(self.site_name)
        iproyal_cfg = _iproyal_proxy_for(self.site_name)
        if decodo_cfg:
            launch_args["proxy"] = decodo_cfg
            proxied_via = "decodo"
            log.info(
                "scrape_using_decodo",
                site=self.site_name,
                host=os.getenv("DECODO_HOST", "az.decodo.com"),
            )
        elif iproyal_cfg:
            launch_args["proxy"] = iproyal_cfg
            proxied_via = "iproyal"
            log.info(
                "scrape_using_iproyal",
                site=self.site_name,
                country=os.getenv("IPROYAL_COUNTRY", "default"),
            )
        elif brightdata_cfg := _brightdata_proxy_for(self.site_name):
            launch_args["proxy"] = brightdata_cfg
            proxied_via = "brightdata"
            log.info(
                "scrape_using_brightdata",
                site=self.site_name,
                country=os.getenv("BRIGHTDATA_COUNTRY", "default"),
            )
        else:
            crawlbase_cfg = _crawlbase_proxy_for(self.site_name)
            if crawlbase_cfg:
                launch_args["proxy"] = crawlbase_cfg
                proxied_via = "crawlbase"
                log.info("scrape_using_crawlbase", site=self.site_name)
            else:
                scraperapi_cfg = _scraperapi_proxy_for(self.site_name)
                if scraperapi_cfg:
                    launch_args["proxy"] = scraperapi_cfg
                    proxied_via = "scraperapi"
                    log.info(
                        "scrape_using_scraperapi",
                        site=self.site_name,
                        country=os.getenv("SCRAPER_API_COUNTRY", "default"),
                    )
                else:
                    proxy_url = os.getenv("HTTP_PROXY") or os.getenv("SCRAPE_PROXY")
                    if proxy_url:
                        launch_args["proxy"] = {"server": proxy_url}
                        proxied_via = "generic"
                        log.info("scrape_using_proxy", proxy=_redact_proxy(proxy_url))

        self._browser = await self._playwright.chromium.launch(**launch_args)
        # IPRoyal + Bright Data + Crawlbase + ScraperAPI прокси MITM'ят HTTPS
        # self-signed серт → Chromium блокирует с ERR_CERT_AUTHORITY_INVALID
        # без явного relaxation. Только если реально через managed proxy идёт
        # — direct и generic-proxy остаются strict (там не должно быть MITM).
        ignore_https_errors = proxied_via in (
            "iproyal",
            "brightdata",
            "crawlbase",
            "scraperapi",
            "decodo",
        )
        self._context = await self._browser.new_context(
            user_agent=random_user_agent(),
            viewport=random_viewport(),
            locale="az-AZ",
            # Realistic prefs that real users have
            timezone_id="Asia/Baku",
            color_scheme="light",
            extra_http_headers={
                "Accept-Language": "az-AZ,az;q=0.9,ru-RU;q=0.8,ru;q=0.7,en-US;q=0.6,en;q=0.5",
            },
            ignore_https_errors=ignore_https_errors,
        )
        self._context.set_default_timeout(self.timeout_sec * 1000)
        # Stealth patches (anti-bot detection)
        await apply_stealth(self._context)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close_resources()

    async def _close_resources(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    async def _throttle(self) -> None:
        """Не более 1 запроса в `rate_limit_sec`. Безопасно для concurrent-доступа."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            since = now - self._last_request_at
            if since < self.rate_limit_sec:
                jitter = random.uniform(0, 0.5)
                await asyncio.sleep(self.rate_limit_sec - since + jitter)
            self._last_request_at = asyncio.get_event_loop().time()

    def _crawlbase_api_token_for_site(self) -> str | None:
        """Если site в CRAWLBASE_SITES и CRAWLBASE_JS_TOKEN задан — вернуть токен.

        Эта функция включает «pre-fetch HTML через Crawling API» режим: в goto()
        вместо page.goto() мы тянем HTML через api.crawlbase.com и подаём его
        через page.set_content. Используется когда:
        - Direct connection не работает (CF блочит Hetzner/Azure IP)
        - Smart Proxy mode не работает (CF блочит Crawlbase proxy IP)
        - Crawling API имеет другую инфраструктуру и проходит CF
        """
        token = os.getenv("CRAWLBASE_JS_TOKEN")
        if not token:
            return None
        sites_csv = os.getenv("CRAWLBASE_API_SITES") or os.getenv("CRAWLBASE_SITES", "")
        sites = {s.strip() for s in sites_csv.split(",") if s.strip()}
        return token if self.site_name in sites else None

    async def goto(self, page: Page, url: str, check_captcha: bool = True) -> None:
        """Navigate with rate-limit, exponential retry, and captcha detection.

        Если активирован Crawlbase Crawling API mode (CRAWLBASE_JS_TOKEN +
        CRAWLBASE_API_SITES contains self.site_name), вместо реального page.goto
        мы тянем HTML через api.crawlbase.com (с JS-рендером на их стороне) и
        подаём в page.set_content. Существующий код парсит DOM как обычно.

        Raises:
            CaptchaDetected: if a bot-wall is detected after navigation. Caller
                may catch this to skip the page without killing the whole run.
        """
        await self._throttle()

        cb_token = self._crawlbase_api_token_for_site()
        if cb_token:
            # Pre-fetch HTML through Crawling API
            await self._goto_via_crawlbase_api(page, url, cb_token)
        else:
            # Native page.goto with retries
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.max_retries),
                wait=wait_exponential(multiplier=2, min=2, max=60),
                retry=retry_if_exception_type((RuntimeError, TimeoutError)),
                reraise=True,
            ):
                with attempt:
                    response = await page.goto(url, wait_until="domcontentloaded")
                    # Treat 429 as rate-limit signal — wait longer than exponential
                    if response and response.status == 429:
                        log.warning("rate_limited", url=url, status=429)
                        await asyncio.sleep(60 + random.uniform(0, 30))
                        raise RuntimeError(f"HTTP 429 for {url}")
                    if response and response.status >= 500:
                        raise RuntimeError(f"HTTP {response.status} for {url}")
                    if response and response.status == 403:
                        log.warning("forbidden", url=url, status=403)
                        raise RuntimeError(f"HTTP 403 for {url}")

        if check_captcha:
            captcha = await detect_captcha(page)
            if captcha:
                raise CaptchaDetected(f"{captcha} on {url}")

    async def _goto_via_crawlbase_api(self, page: Page, url: str, token: str) -> None:
        """Fetch URL через api.crawlbase.com → подать HTML в page.set_content.

        Crawlbase JS Token включает headless Chrome рендер у них на стороне,
        отдаёт уже post-render HTML. Lazy-loaded cards гарантированно в DOM.
        """
        import httpx
        from urllib.parse import quote

        api_url = f"https://api.crawlbase.com/?token={token}&url={quote(url, safe='')}"
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=2, min=2, max=60),
            retry=retry_if_exception_type((RuntimeError, TimeoutError, httpx.HTTPError)),
            reraise=True,
        ):
            with attempt:
                # Crawlbase JS-rendering обычно занимает 3-10с на page
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.get(api_url)
                if resp.status_code == 200:
                    # Подаём HTML — Playwright парсит как обычно
                    await page.set_content(resp.text, wait_until="domcontentloaded")
                    return
                if resp.status_code in (429, 503):
                    raise RuntimeError(f"Crawlbase HTTP {resp.status_code} for {url}")
                if resp.status_code == 520:
                    # 520 — Crawlbase не смог обойти CF на target site
                    log.warning("crawlbase_target_unreachable", url=url, status=520)
                    raise RuntimeError(f"Crawlbase 520 (target CF challenge) for {url}")
                raise RuntimeError(f"Crawlbase HTTP {resp.status_code} for {url}")

    async def new_page(self) -> Page:
        assert self._context is not None
        return await self._context.new_page()

    @abstractmethod
    async def scrape_category(
        self, category_slug: str, limit: int | None = None
    ) -> AsyncIterator[ScrapedProduct]:
        """Дать поток товаров для одной категории. Подкласс реализует."""
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]

    async def scrape_product_page(self, url: str) -> ScrapedProduct | None:
        """Зайти на страницу одного товара по URL и извлечь данные.

        Используется в watchlist-режиме (когда мы парсим конкретные SKU, а не категорию).
        По умолчанию — логирует warning. Подкласс должен переопределить.
        """
        log.warning("scrape_product_page_not_implemented", site=self.site_name, url=url)
        return None

    async def search(self, query: str, limit: int = 5) -> list[ScrapedProduct]:
        """Поиск товара через site search. По умолчанию пусто.

        Используется как fallback если URL в watchlist не зафиксирован.
        Подклассы могут переопределить когда мы узнаем эндпоинты поиска каждого сайта.
        """
        log.warning("search_not_implemented", site=self.site_name)
        return []

    async def scrape_promos(self) -> list[ScrapedPromo]:
        """Опционально — собрать промо/баннеры с главной. По умолчанию — ничего."""
        return []

    async def scrape_urls(
        self, urls: list[str], on_result=None, on_abort=None
    ) -> list[ScrapedProduct]:
        """Watchlist-режим: посетить список URL и собрать продукты."""
        out: list[ScrapedProduct] = []
        for item_index, url in enumerate(urls):
            try:
                product = await self.scrape_product_page(url)
                if product:
                    out.append(product)
                    if on_result is not None:
                        on_result(url, product, None)
                elif on_result is not None:
                    on_result(url, None, "product_not_found")
            except SiteScrapeFatalError as exc:
                fatal = SiteScrapeFatalError(site_fatal_error_message(exc))
                if on_abort is not None:
                    on_abort(url, urls[item_index + 1 :], fatal)
                raise fatal from exc
            except Exception as e:
                reason = fatal_proxy_reason(e)
                if reason is not None:
                    fatal = SiteScrapeFatalError(reason)
                    if on_abort is not None:
                        on_abort(url, urls[item_index + 1 :], fatal)
                    raise fatal from e
                log.warning("scrape_url_failed", url=url, error=str(e))
                if on_result is not None:
                    on_result(url, None, f"{type(e).__name__}: {e}")
        return out

    async def scrape(
        self,
        category_slugs: list[str],
        limit_per_category: int | None = None,
        on_category=None,
    ) -> ScrapeResult:
        """Точка входа: парсит все запрошенные категории + промо.

        Tracking metrics added (W10): captcha hits, retry counts, blocked categories.

        on_category(site, slug, products): опциональный callback, вызываемый ПОСЛЕ
        каждой успешной категории — для ИНКРЕМЕНТАЛЬНОГО persist'а. Без него persist
        идёт только в конце прогона (`run_cmd`), и медленный/оборванный/зависший
        прогон (pharmonline DDP ~7ч) теряет ВСЁ собранное. Callback сохраняет каждую
        категорию сразу. Ошибка callback'а НЕ валит скрейп (логируется и идём дальше).
        """
        requested_slugs = [slug for slug in category_slugs if slug is not None]
        result = ScrapeResult(site=self.site_name, items_expected=len(requested_slugs))
        captcha_hits = 0
        category_failures = 0
        site_aborted = False

        def abort_site(
            error: BaseException, item_index: int, current_products: int
        ) -> None:
            nonlocal site_aborted
            site_aborted = True
            message = site_fatal_error_message(error)
            remaining = requested_slugs[item_index:]
            result.site_fatal = True
            result.errors.append(f"site_fatal: {message}")
            result.items_failed += len(remaining)
            for remaining_index, remaining_slug in enumerate(remaining):
                result.item_results[str(remaining_slug)] = {
                    "status": "failed" if remaining_index == 0 else "skipped",
                    "products": current_products if remaining_index == 0 else 0,
                    "error": message,
                    "error_kind": "site_fatal",
                }
            log.error(
                "site_scrape_aborted",
                site=self.site_name,
                category=requested_slugs[item_index],
                remaining=len(remaining),
                error=message,
            )

        for item_index, slug in enumerate(requested_slugs):
            try:
                count = 0
                cat_products = []
                async for product in self.scrape_category(slug, limit=limit_per_category):
                    result.products.append(product)
                    cat_products.append(product)
                    count += 1
                log.info(
                    "category_scraped",
                    site=self.site_name,
                    category=slug,
                    products=count,
                )
                if count == 0:
                    msg = f"category={slug}: empty result"
                    result.errors.append(msg)
                    result.items_failed += 1
                    result.item_results[str(slug)] = {
                        "status": "empty",
                        "products": 0,
                        "error": "category returned zero products",
                        "error_kind": "empty",
                    }
                else:
                    result.items_completed += 1
                    result.item_results[str(slug)] = {
                        "status": "ok",
                        "products": count,
                    }
                if on_category is not None and cat_products:
                    try:
                        on_category(self.site_name, slug, cat_products)
                    except Exception as e:
                        log.error(
                            "incremental_persist_failed",
                            site=self.site_name,
                            category=slug,
                            error=f"{type(e).__name__}: {e}",
                        )
            except SiteScrapeFatalError as e:
                abort_site(e, item_index, count)
                break
            except CaptchaDetected as e:
                captcha_hits += 1
                msg = f"category={slug}: captcha — {e}"
                log.warning(
                    "category_captcha_blocked", site=self.site_name, slug=slug, error=str(e)
                )
                result.errors.append(msg)
                result.items_failed += 1
                result.item_results[str(slug)] = {
                    "status": "failed",
                    "products": 0,
                    "error": str(e)[:500],
                    "error_kind": "captcha",
                }
            except Exception as e:
                fatal_reason = fatal_proxy_reason(e)
                if fatal_reason is not None:
                    abort_site(
                        SiteScrapeFatalError(fatal_reason),
                        item_index,
                        count,
                    )
                    break
                category_failures += 1
                msg = f"category={slug}: {type(e).__name__}: {e}"
                log.error("category_failed", site=self.site_name, error=msg)
                result.errors.append(msg)
                result.items_failed += 1
                result.item_results[str(slug)] = {
                    "status": "failed",
                    "products": 0,
                    "error": f"{type(e).__name__}: {e}"[:500],
                    "error_kind": "exception",
                }

        if not site_aborted:
            try:
                result.promos = await self.scrape_promos()
            except SiteScrapeFatalError as e:
                message = site_fatal_error_message(e)
                result.site_fatal = True
                result.errors.append(f"site_fatal: promos: {message}")
                log.error(
                    "site_scrape_promos_aborted",
                    site=self.site_name,
                    error=message,
                )
            except Exception as e:
                reason = fatal_proxy_reason(e)
                if reason is not None:
                    message = reason
                    result.site_fatal = True
                    result.errors.append(f"site_fatal: promos: {message}")
                    log.error(
                        "site_scrape_promos_aborted",
                        site=self.site_name,
                        error=message,
                    )
                else:
                    msg = f"promos: {type(e).__name__}: {e}"
                    log.error("promos_failed", site=self.site_name, error=msg)
                    result.errors.append(msg)

        log.info(
            "site_scrape_summary",
            site=self.site_name,
            products=len(result.products),
            categories=len(category_slugs),
            captcha_hits=captcha_hits,
            failures=category_failures,
        )
        # Prometheus metrics
        try:
            from src.observability import metrics

            metrics.scrape_products_total.labels(site=self.site_name).inc(len(result.products))
            if captcha_hits:
                metrics.captcha_hits_total.labels(site=self.site_name).inc(captcha_hits)
            if category_failures:
                metrics.scrape_failures_total.labels(site=self.site_name, reason="exception").inc(
                    category_failures
                )
        except Exception:
            pass
        return result
