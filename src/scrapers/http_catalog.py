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
import re
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
        raw = raw.replace(" ", "").strip()
        if not raw:
            return None
        if "," in raw and "." in raw:
            # Оба знака: дробный — тот, что правее («1,199.99» и «1.199,99»).
            thousands = "," if raw.rfind(".") > raw.rfind(",") else "."
            raw = raw.replace(thousands, "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}([.,]\d{3})+", raw):
            # «1,199» и «1.199» — разделитель тысяч: трёх знаков после
            # дробного у цены в манатах не бывает.
            raw = raw.replace(",", "").replace(".", "")
        else:
            raw = raw.replace(",", ".")
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
    # Сайт закрылся от нас на какой-то из страниц этого прохода.
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
        """Следующий проход категории начать с другого выхода прокси.

        Считаем от выхода, которым шли сейчас, а не от начального: к этому
        времени ``fetch`` уже мог перейти на соседний, и шаг «плюс один от
        начального» вернул бы тот же самый выход.
        """
        ports = _decodo_ports(self.site_name)
        if not ports:
            return
        current = ports.index(self._active_port) if self._active_port in ports else -1
        self._active_port = ports[(current + 1) % len(ports)]

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
          - ``CaptchaDetected`` — сайт закрылся от нас: страница защиты или 403
            хотя бы на одном выходе, и ни один выход страницу не отдал;
          - ``PageFetchError`` — сеть, временный или неожиданный статус.

        Через прокси сбой лечится сменой выхода; напрямую выход один, поэтому
        временный сбой повторяется с паузой, а отказ защиты — нет: тот же адрес
        получит тот же отказ.
        """
        order = self._port_order()
        direct = order == [None]
        if direct:
            attempts = max(1, self.max_retries)
        else:
            # Выходов может быть меньше, чем нужно попыток на временный сбой
            # (один порт): тогда тот же выход пробуется повторно.
            attempts = min(_env_int("DECODO_PAGE_ATTEMPTS", 10), max(len(order), self.max_retries))
        last_reason = "no attempt was made"
        last_status: int | None = None
        refused_status: int | None = None
        refused_exits: set[int | None] = set()
        site_answered = False
        for attempt in range(attempts):
            port = None if direct else order[attempt % len(order)]
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
                # Только имя класса: в тексте ошибки прокси стоит его адрес.
                last_reason, last_status = f"proxy error: {type(exc).__name__}", None
                continue
            except (httpx.RequestError, ssl.SSLError) as exc:
                last_reason, last_status = f"network error: {type(exc).__name__}", None
                continue

            status = response.status_code
            if port is not None and status in _PROXY_ACCOUNT_STATUSES:
                raise SiteScrapeFatalError(f"proxy access rejected: HTTP {status}")
            protection = looks_like_protection_page(response.text)
            if status in accept_statuses and not protection:
                if port is not None:
                    self._active_port = port
                return response
            if protection or status == 403:
                # 403 без знакомой страницы — тоже отказ в доступе (у
                # Cloudflare их несколько видов): другой выход может пройти.
                refused_status = status
                refused_exits.add(port)
                last_reason, last_status = "access refused", status
                if len(refused_exits) >= len(order):
                    # Отказали все выходы (без прокси он один): тот же адрес
                    # получит тот же отказ.
                    break
                continue
            last_reason, last_status = f"HTTP {status}", status
            if status not in _TRANSIENT_STATUSES:
                # Постоянный ответ самого сайта (404, 410…): до сайта дошли, и
                # от смены выхода он не зависит.
                site_answered = True
                break
        if refused_status is not None and not site_answered:
            raise CaptchaDetected(
                f"{self.site_name}: access refused instead of content (HTTP {refused_status})"
            )
        raise PageFetchError(last_reason, status=last_status)

    # -- контракт категории --------------------------------------------------

    async def discover_categories(self) -> list[str]:
        """Категории, обход которых даёт полный каталог сайта.

        Список либо полон, либо его нет: раздел, который не удалось прочитать,
        — это ``PageFetchError``, а не список покороче. Проверка полноты
        прогона видит только запрошенные категории, и молча пропущенный раздел
        не числился бы пропущенным нигде.
        """
        raise NotImplementedError

    async def _collect_category(self, category_slug: str, limit: int | None) -> CategoryPass:
        """Один проход одной категории, от первой страницы до последней.

        Сбой страницы не бросается наружу, а записывается в итог прохода
        (``page_failed``): уже прочитанные страницы остаются в нём.
        """
        raise NotImplementedError

    @staticmethod
    def page_failed(result: CategoryPass, page: int, error: Exception) -> CategoryPass:
        """Записать в итог прохода, что страница не получена, и вернуть его."""
        result.pages_skipped += 1
        if isinstance(error, CaptchaDetected):
            result.blocked = True
            result.abort_reason = f"page_{page}_refused"
        else:
            reason = getattr(error, "reason", None) or type(error).__name__
            result.abort_reason = f"page_{page}_failed: {reason}"[:200]
        return result

    async def scrape_category(
        self, category_slug: str, limit: int | None = None
    ) -> AsyncIterator[ScrapedProduct]:
        """Отдать товары категории после цельного прохода.

        Неполный проход повторяется с начала — списки сайтов не снимок, товар
        между страницами может сдвинуться. Попытки не склеиваются: наружу
        уходит один проход, полный или лучший из неполных, со своим статусом.
        Лучший — тот, где товаров больше; при равенстве — последний.
        """
        best: CategoryPass | None = None
        for attempt in range(1, max(1, self.category_attempts) + 1):
            try:
                current = await self._collect_category(str(category_slug), limit)
            except CaptchaDetected as exc:
                current = self.page_failed(CategoryPass(), 1, exc)
            if best is None or current.complete or len(current.products) >= len(best.products):
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


def counted_pass_is_complete(result: CategoryPass) -> bool:
    """Проход сайта, который называет число товаров, сошёлся с этим числом.

    Условие то же, что у проверки полноты прогона
    (``main._route_incomplete_causes``): ни одной неразобранной записи, записей
    пришло ровно столько, сколько сайт назвал, и все они — разные товары.
    Иначе сборщик объявил бы полным проход, который прогон потом отвергнет, и
    не стал бы его повторять.
    """
    return (
        result.expected_items is not None
        and result.item_failures == 0
        and result.raw_items == result.expected_items
        and len(result.products) == result.expected_items
    )
