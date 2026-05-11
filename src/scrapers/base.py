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


def _redact_proxy(url: str) -> str:
    """Hide credentials in proxy URL for logging. http://user:pass@host:8080 → http://***@host:8080"""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    auth, host = rest.rsplit("@", 1)
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


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
    username = (
        f"scraperapi.country_code={country}" if country else "scraperapi"
    )
    return {
        "server": "http://proxy-server.scraperapi.com:8001",
        "username": username,
        "password": key,
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
        self._playwright = await async_playwright().start()

        # Proxy resolution order (first match wins):
        #   1. ScraperAPI per-site config (SCRAPER_API_KEY + SCRAPER_API_SITES)
        #   2. Generic HTTP_PROXY / SCRAPE_PROXY env (single proxy for everything)
        # ScraperAPI takes precedence so aloe (which works direct from Hetzner)
        # doesn't burn ScraperAPI credits unnecessarily.
        launch_args: dict = {"headless": self.headless}
        scraperapi_cfg = _scraperapi_proxy_for(self.site_name)
        if scraperapi_cfg:
            launch_args["proxy"] = scraperapi_cfg
            log.info(
                "scrape_using_scraperapi",
                site=self.site_name,
                country=os.getenv("SCRAPER_API_COUNTRY", "default"),
            )
        else:
            proxy_url = os.getenv("HTTP_PROXY") or os.getenv("SCRAPE_PROXY")
            if proxy_url:
                launch_args["proxy"] = {"server": proxy_url}
                log.info("scrape_using_proxy", proxy=_redact_proxy(proxy_url))

        self._browser = await self._playwright.chromium.launch(**launch_args)
        # ScraperAPI's proxy MITMs HTTPS with a self-signed cert — Chromium
        # blocks with ERR_CERT_AUTHORITY_INVALID unless we explicitly accept it.
        # Only relax the check when we actually went through ScraperAPI; direct
        # / generic-proxy contexts keep strict TLS validation.
        ignore_https_errors = scraperapi_cfg is not None
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
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _throttle(self) -> None:
        """Не более 1 запроса в `rate_limit_sec`. Безопасно для concurrent-доступа."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            since = now - self._last_request_at
            if since < self.rate_limit_sec:
                jitter = random.uniform(0, 0.5)
                await asyncio.sleep(self.rate_limit_sec - since + jitter)
            self._last_request_at = asyncio.get_event_loop().time()

    async def goto(self, page: Page, url: str, check_captcha: bool = True) -> None:
        """Navigate with rate-limit, exponential retry, and captcha detection.

        Raises:
            CaptchaDetected: if a bot-wall is detected after navigation. Caller
                may catch this to skip the page without killing the whole run.
        """
        await self._throttle()
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

    async def scrape_urls(self, urls: list[str]) -> list[ScrapedProduct]:
        """Watchlist-режим: посетить список URL и собрать продукты."""
        out: list[ScrapedProduct] = []
        for url in urls:
            try:
                product = await self.scrape_product_page(url)
                if product:
                    out.append(product)
            except Exception as e:
                log.warning("scrape_url_failed", url=url, error=str(e))
        return out

    async def scrape(
        self, category_slugs: list[str], limit_per_category: int | None = None
    ) -> ScrapeResult:
        """Точка входа: парсит все запрошенные категории + промо.

        Tracking metrics added (W10): captcha hits, retry counts, blocked categories.
        """
        result = ScrapeResult(site=self.site_name)
        captcha_hits = 0
        category_failures = 0

        for slug in category_slugs:
            if slug is None:
                continue
            try:
                count = 0
                async for product in self.scrape_category(slug, limit=limit_per_category):
                    result.products.append(product)
                    count += 1
                log.info(
                    "category_scraped",
                    site=self.site_name,
                    category=slug,
                    products=count,
                )
            except CaptchaDetected as e:
                captcha_hits += 1
                msg = f"category={slug}: captcha — {e}"
                log.warning("category_captcha_blocked", site=self.site_name, slug=slug, error=str(e))
                result.errors.append(msg)
            except Exception as e:
                category_failures += 1
                msg = f"category={slug}: {type(e).__name__}: {e}"
                log.error("category_failed", site=self.site_name, error=msg)
                result.errors.append(msg)

        try:
            result.promos = await self.scrape_promos()
        except Exception as e:
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
                metrics.scrape_failures_total.labels(
                    site=self.site_name, reason="exception"
                ).inc(category_failures)
        except Exception:
            pass
        return result
