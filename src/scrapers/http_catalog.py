"""Общая основа сборщиков, которым не нужен браузер (магазины электроники).

Kontakt Home, Irshad и Baku Electronics отдают каталог готовыми ответами —
страницу списка можно прочитать обычным HTTP-запросом. Браузер `BaseScraper`
здесь не поднимается вовсе: он нужен только там, где страницу надо исполнять.

Что берёт на себя этот класс:
  - клиент httpx с заголовками обычного браузера и паузой между запросами;
  - Decodo, если сайт перечислен в ``DECODO_SITES`` (те же переменные, что у
    aptekonline): один «липкий» порт на проход, смена порта после сбоя;
  - различение трёх исходов страницы — получена, сайт закрылся защитой
    (Cloudflare), страница не получена по сети;
  - цельный проход категории: неполный проход повторяется с начала, попытки
    между собой не склеиваются.

Подкласс реализует ``_collect_category`` (один проход одной категории) и
``discover_categories`` (какие категории составляют полный каталог).
"""

from __future__ import annotations

import asyncio
import os
import ssl
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx
import structlog

from src.scrapers.aptekonline import _decodo_httpx_proxy_for, _decodo_ports
from src.scrapers.base import (
    BaseScraper,
    CaptchaDetected,
    ScrapedProduct,
    SiteScrapeFatalError,
    fatal_proxy_reason,
)

log = structlog.get_logger()

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "az,ru;q=0.8,en;q=0.6",
}

# Тело ответа, которым Cloudflare заменяет страницу сайта. Статус при этом
# бывает и 403, и 503, а у «мягкой» проверки — 200. Слова «challenge-platform»
# в списке нет намеренно: Cloudflare дописывает скрипт с таким адресом и в
# обычные страницы сайта.
_PROTECTION_MARKERS = (
    "<title>Just a moment",
    "Attention Required! | Cloudflare",
    "Sorry, you have been blocked",
)

# Ответ прокси «доступ отклонён»: баланс или учётные данные. Повтор не поможет.
_PROXY_ACCOUNT_STATUSES = {402, 407}
# Временные ответы: сайт или выход прокси не справился, другой выход может.
_TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


class PageFetchError(Exception):
    """Страница не получена: сеть, временный ответ или неожиданный статус."""

    def __init__(self, reason: str, *, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def looks_like_protection_page(text: str) -> bool:
    """Ответ — страница защиты Cloudflare, а не страница сайта."""
    head = text[:6000]
    return any(marker in head for marker in _PROTECTION_MARKERS)


def _env_float(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def price_or_none(raw: object) -> float | None:
    """Цена из ответа сайта: положительное число или ничего.

    Ноль и отрицательное значение ценой не считаются: так сайты помечают
    «цена по запросу» и снятый товар, а ноль в журнале цен дал бы ложное
    падение на сто процентов.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        raw = raw.replace("\xa0", " ").replace("AZN", "").replace("₼", "")
        raw = raw.replace(" ", "").replace(",", ".").strip()
        if not raw:
            return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0:  # NaN или не цена
        return None
    return round(value, 2)


def sale_fields(
    regular: float | None, final: float | None
) -> tuple[float | None, float | None, float | None, bool]:
    """Привести пару «обычная цена / цена со скидкой» к полям ScrapedProduct.

    Возвращает (price, discount_price, discount_percent, is_on_sale). Скидкой
    считается только цена строго ниже обычной; если сайт назвал одну цену —
    она и есть обычная.
    """
    if regular is None:
        return final, None, None, False
    if final is None or final >= regular:
        return regular, None, None, False
    percent = round((1 - final / regular) * 100, 1)
    return regular, final, percent, True


@dataclass
class CategoryPass:
    """Итог одного прохода категории — до того, как товары уйдут наружу."""

    products: list[ScrapedProduct] = field(default_factory=list)
    complete: bool = False
    abort_reason: str | None = None
    expected_items: int | None = None
    expected_pages: int | None = None
    visited_pages: int = 0
    pages_skipped: int = 0
    raw_items: int = 0
    item_failures: int = 0
    # Сайт закрылся защитой: повторять категорию тем же путём бессмысленно.
    blocked: bool = False


class HttpCatalogScraper(BaseScraper):
    """Сборщик каталога обычными HTTP-запросами, без браузера."""

    # Пауза между запросами по умолчанию; переопределяется переменной
    # ``<SITE>_RATE_LIMIT_SEC`` (например ``IRSHAD_RATE_LIMIT_SEC``).
    default_rate_limit_sec: float = 2.0
    # Сколько раз пройти категорию целиком, прежде чем отдать неполный проход.
    category_attempts: int = 2
    request_headers: dict[str, str] = BROWSER_HEADERS

    def __init__(
        self,
        rate_limit_sec: float | None = None,
        timeout_sec: int | None = None,
        max_retries: int | None = None,
        headless: bool | None = None,
    ):
        if rate_limit_sec is None:
            rate_limit_sec = _env_float(f"{self.site_name.upper()}_RATE_LIMIT_SEC")
        if rate_limit_sec is None:
            rate_limit_sec = self.default_rate_limit_sec
        super().__init__(
            # Ноль у родителя значит «взять общий по умолчанию»; явный ноль
            # (тесты) передаём как очень малую паузу.
            rate_limit_sec=rate_limit_sec or 1e-9,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            headless=headless,
        )
        self._clients: dict[int | None, httpx.AsyncClient] = {}
        self._decodo_port_offset = 0
        self._active_port: int | None = None
        self.fetch_retries = 0

    # -- жизненный цикл: браузер не нужен ------------------------------------

    async def __aenter__(self) -> HttpCatalogScraper:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close_clients()

    async def _close_clients(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            try:
                await client.aclose()
            except Exception:
                pass

    # -- HTTP ------------------------------------------------------------------

    def _build_client(self, proxy_url: str | None) -> httpx.AsyncClient:
        kwargs: dict = {
            "headers": dict(self.request_headers),
            "timeout": httpx.Timeout(60.0 if proxy_url else float(self.timeout_sec)),
            "follow_redirects": True,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.AsyncClient(**kwargs)

    def _client_for(self, port: int | None) -> httpx.AsyncClient:
        client = self._clients.get(port)
        if client is None:
            proxy_url = _decodo_httpx_proxy_for(self.site_name, port) if port is not None else None
            client = self._build_client(proxy_url)
            self._clients[port] = client
        return client

    def _port_order(self) -> list[int | None]:
        """Порты Decodo в порядке попыток; ``[None]`` — прямой доступ."""
        ports = _decodo_ports(self.site_name)
        if not ports:
            return [None]
        if self._active_port not in ports:
            self._active_port = ports[self._decodo_port_offset % len(ports)]
        start = ports.index(self._active_port)
        return [*ports[start:], *ports[:start]]

    def _rotate_session(self) -> None:
        """Следующий проход категории начать с другого выхода прокси."""
        ports = _decodo_ports(self.site_name)
        if ports:
            self._decodo_port_offset = (self._decodo_port_offset + 1) % len(ports)
            self._active_port = ports[self._decodo_port_offset]

    async def fetch(
        self,
        url: str,
        *,
        params: dict | list | None = None,
        headers: dict[str, str] | None = None,
        accept_statuses: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        """Получить страницу или сказать, почему её нет.

        Возвращает ответ со статусом из ``accept_statuses``. Иначе:
          - ``SiteScrapeFatalError`` — прокси отклонил доступ (402/407);
          - ``CaptchaDetected`` — сайт ответил страницей защиты на всех выходах;
          - ``PageFetchError`` — сеть, временный или неожиданный статус.
        """
        order = self._port_order()
        direct = order == [None]
        attempts = (
            max(1, self.max_retries)
            if direct
            else min(len(order), _env_int("DECODO_PAGE_ATTEMPTS", 10))
        )
        last_reason = "no attempt was made"
        last_status: int | None = None
        blocked = False
        for attempt in range(attempts):
            port = None if direct else order[attempt]
            if attempt:
                self.fetch_retries += 1
                await asyncio.sleep(min(8.0, 0.5 * 2**attempt) if direct else 0.5)
            await self._throttle()
            try:
                response = await self._client_for(port).get(url, params=params, headers=headers)
            except httpx.ProxyError as exc:
                reason = fatal_proxy_reason(exc)
                if reason is not None:
                    raise SiteScrapeFatalError(reason) from exc
                last_reason, last_status = f"proxy error: {type(exc).__name__}", None
                continue
            except (httpx.RequestError, ssl.SSLError) as exc:
                last_reason, last_status = f"network error: {type(exc).__name__}", None
                continue

            status = response.status_code
            if port is not None and status in _PROXY_ACCOUNT_STATUSES:
                raise SiteScrapeFatalError(f"proxy access rejected: HTTP {status}")
            if status in accept_statuses:
                if looks_like_protection_page(response.text):
                    blocked, last_reason, last_status = True, "protection page", status
                    continue
                if port is not None:
                    self._active_port = port
                return response
            if status in (403, 503) and looks_like_protection_page(response.text):
                blocked, last_reason, last_status = True, "protection page", status
                continue
            last_reason, last_status = f"HTTP {status}", status
            if status not in _TRANSIENT_STATUSES:
                # Постоянный отказ сайта (404, 410…) от смены выхода не зависит.
                break
        if blocked and last_reason == "protection page":
            raise CaptchaDetected(
                f"{self.site_name}: protection page instead of content (HTTP {last_status})"
            )
        raise PageFetchError(last_reason, status=last_status)

    # -- контракт категории --------------------------------------------------

    async def discover_categories(self) -> list[str]:
        """Категории, обход которых даёт полный каталог сайта."""
        raise NotImplementedError

    async def _collect_category(self, category_slug: str, limit: int | None) -> CategoryPass:
        """Один проход одной категории, от первой страницы до последней."""
        raise NotImplementedError

    async def scrape_category(
        self, category_slug: str, limit: int | None = None
    ) -> AsyncIterator[ScrapedProduct]:
        """Отдать товары категории после цельного прохода.

        Неполный проход повторяется с начала — списки сайтов не снимок, товар
        между страницами может сдвинуться. Попытки не склеиваются: наружу
        уходит один проход, полный или лучший из неполных, со своим статусом.
        """
        best: CategoryPass | None = None
        for attempt in range(1, max(1, self.category_attempts) + 1):
            try:
                current = await self._collect_category(str(category_slug), limit)
            except CaptchaDetected:
                current = CategoryPass(abort_reason="protection_page", blocked=True)
            if best is None or len(current.products) > len(best.products) or current.complete:
                best = current
            if current.complete or current.abort_reason == "requested_limit_reached":
                break
            if attempt < self.category_attempts:
                self.fetch_retries += 1
                self._rotate_session()
                log.warning(
                    "category_pass_retrying",
                    site=self.site_name,
                    category=category_slug,
                    attempt=attempt,
                    reason=current.abort_reason or "incomplete",
                )
        assert best is not None
        self._set_route_status(
            str(category_slug),
            complete=best.complete,
            pages_skipped=best.pages_skipped,
            abort_reason=best.abort_reason,
            expected_pages=best.expected_pages,
            visited_pages=best.visited_pages,
            raw_items=best.raw_items,
            parsed_items=len(best.products),
            item_failures=best.item_failures,
            expected_items=best.expected_items,
        )
        if best.blocked and not best.products:
            raise CaptchaDetected(
                f"{self.site_name}: category {category_slug} is behind protection"
            )
        for product in best.products:
            yield product
