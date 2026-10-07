"""Скрейпер для aloe.az — Next.js, динамический рендер.

Структура (по результатам live-probe 2026-04-28, обновлено 2026-05-28):
- Categories:    https://aloe.az/catalog/filters/?product_field=bestseller
                 https://aloe.az/catalog/filters/?category_slug={slug}
- Product page:  https://aloe.az/{slug}/   ← БЕЗ /product/ префикса!
- Card:          `[class*="productCardWrapper"]`  (это <button>, не <a>!)
- Card name:     `[class*="productName"]`
- Card brand:    `[class*="brand"]`  (внутри карточки <a>)
- Card price:    `[class*="price"]:not([class*="old"])`  → 'priceWrapper > price'
- Card image:    `[class*="productImg"] img`  (alt содержит имя)
- Detail title:  `h1[class*="style_title"]`
- Detail price:  `[class*="priceWrapper"] [class*="style_price"]` → e.g. "4.03"

URL synth (Task #33 fix, 2026-05-28):
Карточка не имеет href, но клик-навигация через Playwright MCP подтвердила
паттерн: slug = lowercase + Azeri-to-ASCII + remove punctuation + space→dash.

  'Vitamin B1 5% 1 ml 10 əd.'        → vitamin-b1-5-1-ml-10-ed
  'Diampa-M 12,5 mq/1000 mq 28 əd'   → diampa-m-125-mq1000-mq-28-ed
  'Şüşə və əmzik fırçası'             → suse-ve-emzik-fircasi

Если product page по synth-slug не существует — будет 404, но это лучше чем
гарантированно неправильный listing-URL (предыдущее поведение для всех 1809 aloe products).
"""

from __future__ import annotations

import html
import json
import os
import re
from typing import AsyncIterator

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.normalize import (
    extract_dosage,
    extract_pack_size,
    normalize_name,
    parse_price,
)
from src.scrapers.base import BaseScraper, ScrapedProduct, ScrapedPromo

log = structlog.get_logger()


# ─── Azeri → ASCII transliteration (Task #33, 2026-05-28) ────────────────────
# Aloe.az генерирует URL slug из product name по этим правилам.
# Verified via Playwright MCP click-navigation на 3 products.
_AZERI_TO_ASCII = str.maketrans(
    {
        "ə": "e",
        "Ə": "e",
        "ı": "i",
        "İ": "i",  # dotless + dotted I
        "ö": "o",
        "Ö": "o",
        "ü": "u",
        "Ü": "u",
        "ş": "s",
        "Ş": "s",
        "ç": "c",
        "Ç": "c",
        "ğ": "g",
        "Ğ": "g",
    }
)

# Punctuation которая удаляется БЕЗ замены (12,5 → 125, не 12-5).
_PUNCT_REMOVE_RE = re.compile(r"[,/.%()\[\]'\"!?:;«»“”]+")
_NEXT_FLIGHT_CHUNK_RE = re.compile(
    r"self\.__next_f\.push\(\[1,\"((?:\\.|[^\"\\])*)\"\]\)</script>",
    re.DOTALL,
)
_ALOE_DATA_MARKER_RE = re.compile(r'"data":\{')
_ALOE_HTTP_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "az-AZ,az;q=0.9,ru-RU;q=0.8,ru;q=0.7,en-US;q=0.6,en;q=0.5",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}


def aloe_slug(name: str) -> str:
    """Convert product name → URL slug as aloe.az generates it.

    Returns empty string for empty input (callers should fallback to listing URL).

    Order matters: translate FIRST (handles `İ` → `i` before `.lower()` produces
    combining-dot `i̇`), THEN lowercase, THEN strip punctuation.
    """
    if not name:
        return ""
    s = name.strip()
    s = s.translate(_AZERI_TO_ASCII)  # İ→i, Ə→e BEFORE lower() screws up İ
    s = s.lower()
    s = _PUNCT_REMOVE_RE.sub("", s)
    # Whitespace runs → single dash, then collapse multiple dashes
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    return s


def _decode_next_flight(html_text: str) -> str:
    """Return decoded Next.js flight payload chunks embedded in Aloe listing HTML."""
    chunks: list[str] = []
    for match in _NEXT_FLIGHT_CHUNK_RE.finditer(html_text):
        raw = match.group(1)
        try:
            chunks.append(json.loads(f'"{raw}"'))
        except json.JSONDecodeError:
            log.debug("aloe_next_chunk_decode_failed")
    return "\n".join(chunks)


def aloe_listing_page_info(html_text: str) -> tuple[int | None, int | None]:
    """Extract (currentPage, lastPage) from Aloe listing HTML."""
    text = _decode_next_flight(html_text) or html.unescape(html_text)
    current_matches = re.findall(r'"currentPage"\s*:\s*(\d+)', text)
    last_matches = re.findall(r'"lastPage"\s*:\s*(\d+)', text)
    current = int(current_matches[-1]) if current_matches else None
    last = int(last_matches[-1]) if last_matches else None
    return current, last


def _is_transient_fetch_error(exc: BaseException) -> bool:
    """Сбой, который имеет смысл повторить: обрыв или таймаут сети либо 5xx.

    Любой 4xx — это ответ сайта (блок, нет страницы, 429 «помедленнее»), а не
    помеха: быстрый повтор ничего не изменит и только утроит запросы. Ошибки
    прокси и протокола на нашей стороне тоже не повторяем — они не проходят сами.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(
        exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
    )


def _media_url(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    return f"https://ecom.aloe.az/{path.lstrip('/')}"


def _strip_html_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def aloe_product_detail_signals(html_text: str) -> tuple[str | None, str]:
    """Extract explicit manufacturing country and stock from an Aloe detail page.

    Aloe embeds the same data twice (rendered HTML and escaped Next flight
    payload).  The patterns are intentionally anchored to the country label and
    boolean ``inStock`` value so translation dictionaries do not become data.
    """
    from src.product_policy import OFFER_IN_STOCK, OFFER_OUT_OF_STOCK, OFFER_UNKNOWN

    country: str | None = None
    html_match = re.search(
        r"(?:Ölkə|Страна|Country)(?:<!--.*?-->)?\s*:\s*</span>\s*"
        r"<span[^>]*>([^<]+)</span>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if html_match:
        country = html.unescape(html_match.group(1)).strip() or None
    if country is None:
        decoded = re.sub(r'\\+"', '"', html_text)
        flight_match = re.search(
            r'children"\s*:\s*(?:\[)?"(?:Ölkə|Страна|Country)".{0,300}?'
            r'children"\s*:\s*"([^"\\]+)',
            decoded,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if flight_match:
            country = html.unescape(flight_match.group(1)).strip() or None

    decoded = re.sub(r'\\+"', '"', html_text)
    stock_match = re.search(r'"inStock"\s*:\s*(true|false)', decoded)
    status = OFFER_UNKNOWN
    if stock_match:
        status = OFFER_IN_STOCK if stock_match.group(1) == "true" else OFFER_OUT_OF_STOCK
    return country, status


def _is_aloe_product_payload(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    return all(k in obj for k in ("id", "code", "name", "slug", "price")) and isinstance(
        obj.get("name"), str
    )


def _aloe_product_from_payload(
    obj: dict, *, category_slug: str, base_url: str
) -> ScrapedProduct | None:
    name = str(obj.get("name") or "").strip()
    slug = str(obj.get("slug") or "").strip()
    if not name or not slug:
        return None

    price = parse_price(str(obj.get("price"))) if obj.get("price") is not None else None
    old_price = (
        parse_price(str(obj.get("old_price"))) if obj.get("old_price") is not None else None
    )
    is_on_sale = old_price is not None and price is not None and old_price > price
    discount_percent = None
    if is_on_sale and old_price:
        discount_percent = round((1 - price / old_price) * 100, 1)

    brand_payload = obj.get("brand") if isinstance(obj.get("brand"), dict) else {}
    brand = (brand_payload.get("name") or "").strip() or None

    image_url = None
    images = obj.get("images")
    if isinstance(images, list):
        for image in images:
            if not isinstance(image, dict):
                continue
            media = image.get("media_manager")
            if isinstance(media, dict):
                image_url = _media_url(media.get("media_file") or media.get("thumbnail_path"))
            if image_url:
                break

    promo_label = None
    if obj.get("promo"):
        promo_label = "promo"

    from src.product_policy import offer_from_quantity

    country_raw = (
        str(obj.get("manufacturer_country")).strip()
        if obj.get("manufacturer_country") not in (None, "")
        else None
    )
    availability_status, offer_quantity = offer_from_quantity(obj.get("quantity"))

    return ScrapedProduct(
        site="aloe",
        external_id=slug[:100],
        url=f"{base_url}/{slug}/",
        name=name,
        brand=brand,
        manufacturer=None,
        manufacturer_country_raw=country_raw,
        country_source="aloe_api_country_id" if country_raw else None,
        offer_availability_status=availability_status,
        offer_quantity=offer_quantity,
        availability_source="aloe_api_quantity",
        category=category_slug,
        dosage=extract_dosage(name),
        pack_size=extract_pack_size(name),
        image_url=image_url,
        description=_strip_html_text(obj.get("short_description")),
        price=old_price if is_on_sale else price,
        discount_price=price if is_on_sale else None,
        discount_percent=discount_percent,
        is_on_sale=is_on_sale,
        promo_label=promo_label,
    )


def aloe_products_from_listing_html(
    html_text: str, *, category_slug: str, base_url: str = "https://aloe.az"
) -> list[ScrapedProduct]:
    """Parse product payloads embedded in Aloe Next.js listing HTML.

    Aloe listing pages expose the authoritative product objects in the Next
    flight stream. Parsing that stream avoids the old Playwright DOM limit that
    stopped at 100 pages while Aloe currently reports much larger `lastPage`
    values for broad categories.
    """
    products, _raw_items, _parsed_items, _item_failures = (
        _aloe_products_from_listing_html_with_stats(
            html_text, category_slug=category_slug, base_url=base_url
        )
    )
    return products


def _aloe_products_from_listing_html_with_stats(
    html_text: str,
    *,
    category_slug: str,
    base_url: str = "https://aloe.az",
) -> tuple[list[ScrapedProduct], int, int, int]:
    """Parse listing payloads and expose item-level completeness evidence."""
    text = _decode_next_flight(html_text) or html.unescape(html_text)
    decoder = json.JSONDecoder()
    products: list[ScrapedProduct] = []
    seen: set[str] = set()
    raw_items = 0
    parsed_items = 0
    item_failures = 0

    for match in _ALOE_DATA_MARKER_RE.finditer(text):
        raw_items += 1
        start = match.end() - 1
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            item_failures += 1
            continue
        if not _is_aloe_product_payload(obj):
            item_failures += 1
            continue
        product = _aloe_product_from_payload(obj, category_slug=category_slug, base_url=base_url)
        if product is None:
            item_failures += 1
            continue
        parsed_items += 1
        if product.external_id in seen:
            continue
        seen.add(product.external_id)
        products.append(product)

    return products, raw_items, parsed_items, item_failures


class AloeScraper(BaseScraper):
    site_name = "aloe"
    base_url = "https://aloe.az"

    def __init__(
        self,
        *args,
        country_id_map: dict[str, dict[str, object]] | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.country_id_map = country_id_map or {}
        self.verified_country_mappings: dict[str, dict[str, object]] = {}
        self.fetch_retries = 0

    async def _enrich_listing_country_ids(
        self, products: list[ScrapedProduct]
    ) -> None:
        """Resolve Aloe numeric country IDs from durable map or detail pages."""
        from src.product_policy import COUNTRY_RESOLVED, country_resolution

        groups: dict[str, list[ScrapedProduct]] = {}
        for product in products:
            raw = product.manufacturer_country_raw
            if raw is not None and str(raw).isdigit():
                groups.setdefault(str(raw), []).append(product)

        for country_id, group in groups.items():
            mapping = self.country_id_map.get(country_id)
            if mapping is None:
                samples: list[tuple[str, str, str]] = []
                for product in group[:2]:
                    try:
                        html_text = await self._fetch_listing_html(product.url)
                        raw, _availability = aloe_product_detail_signals(html_text)
                    except (httpx.HTTPError, ValueError) as exc:
                        log.warning(
                            "aloe_country_detail_failed",
                            country_id=country_id,
                            url=product.url,
                            error=str(exc),
                        )
                        continue
                    code, status = country_resolution(raw)
                    if code is not None and status == COUNTRY_RESOLVED and raw:
                        samples.append((code, raw, product.url))
                codes = {code for code, _raw, _url in samples}
                required_samples = min(2, len(group))
                if len(samples) < required_samples or len(codes) != 1:
                    # Country enrichment is optional product metadata, not
                    # evidence that listing pagination or card parsing lost
                    # catalog rows. Keep the numeric ID unresolved so policy
                    # checks cannot mistake it for a verified country.
                    log.warning(
                        "aloe_country_id_unresolved",
                        country_id=country_id,
                        category=group[0].category or "unknown",
                        samples=len(samples),
                        required_samples=required_samples,
                        distinct_codes=len(codes),
                    )
                    continue
                code, country_raw, source_url = samples[0]
                mapping = {
                    "country_code": code,
                    "country_raw": country_raw,
                    "source_url": source_url,
                    "sample_count": len(samples),
                }
                self.country_id_map[country_id] = mapping
                self.verified_country_mappings[country_id] = mapping

            country_raw = str(mapping.get("country_raw") or "").strip()
            code, status = country_resolution(country_raw)
            if not country_raw or code is None or status != COUNTRY_RESOLVED:
                log.warning(
                    "aloe_country_id_invalid_mapping",
                    country_id=country_id,
                    category=group[0].category or "unknown",
                    country_raw=country_raw,
                    resolution_status=status,
                )
                continue
            for product in group:
                product.manufacturer_country_raw = country_raw
                product.country_source = "aloe_country_id_verified_detail"

    async def scrape_category(
        self, category_slug: str, limit: int | None = None, max_pages: int = 100
    ) -> AsyncIterator[ScrapedProduct]:
        mode = os.getenv("ALOE_SCRAPER_MODE", "rsc").strip().lower()
        if mode not in ("playwright", "browser"):
            async for product in self._scrape_category_rsc(category_slug, limit=limit):
                yield product
            return

        async for product in self._scrape_category_playwright(
            category_slug, limit=limit, max_pages=max_pages
        ):
            yield product

    async def _scrape_category_rsc(
        self, category_slug: str, limit: int | None = None
    ) -> AsyncIterator[ScrapedProduct]:
        if not category_slug:
            return
        if "=" in category_slug:
            base_url = f"{self.base_url}/catalog/filters/?{category_slug}"
        elif category_slug in ("bestseller", "new", "promo", "seasonal_product_now_is_the_time"):
            base_url = f"{self.base_url}/catalog/filters/?product_field={category_slug}"
        else:
            base_url = f"{self.base_url}/catalog/filters/?category_slug={category_slug}"

        first_html = await self._fetch_listing_page(base_url)
        current_page, last_page = aloe_listing_page_info(first_html)
        if not last_page:
            last_page = 1
        max_pages = int(os.getenv("ALOE_RSC_MAX_PAGES", "1000"))
        last_page = min(last_page, max_pages)

        yielded = 0
        seen_external_ids: set[str] = set()
        visited_pages = 0
        raw_items = 0
        parsed_items = 0
        item_failures = 0
        for page_num in range(1, last_page + 1):
            html_text = first_html if page_num == 1 else await self._fetch_listing_page(
                f"{base_url}&page={page_num}"
            )
            products, page_raw, page_parsed, page_failures = (
                _aloe_products_from_listing_html_with_stats(
                html_text, category_slug=category_slug, base_url=self.base_url
                )
            )
            raw_items += page_raw
            parsed_items += page_parsed
            item_failures += page_failures
            await self._enrich_listing_country_ids(products)
            visited_pages += 1
            log.info(
                "aloe_rsc_page_parsed",
                category=category_slug,
                page=page_num,
                current_page=current_page if page_num == 1 else page_num,
                last_page=last_page,
                products=len(products),
            )
            if page_num > 1 and not products:
                log.info("aloe_rsc_pagination_done", category=category_slug, page=page_num)
                self._set_route_status(
                    category_slug,
                    complete=False,
                    abort_reason="empty_page_before_last",
                    expected_pages=last_page,
                    visited_pages=visited_pages,
                    raw_items=raw_items,
                    parsed_items=parsed_items,
                    item_failures=item_failures,
                )
                break
            for product in products:
                if product.external_id in seen_external_ids:
                    continue
                seen_external_ids.add(product.external_id)
                yielded += 1
                yield product
                if limit and yielded >= limit:
                    self._set_route_status(
                        category_slug,
                        complete=False,
                        abort_reason="requested_limit_reached",
                        expected_pages=last_page,
                        visited_pages=visited_pages,
                        raw_items=raw_items,
                        parsed_items=parsed_items,
                        item_failures=item_failures,
                    )
                    return
        if str(category_slug) not in self._route_statuses:
            self._set_route_status(
                category_slug,
                complete=visited_pages == last_page and item_failures == 0,
                abort_reason=(
                    "item_parse_failures"
                    if item_failures
                    else None if visited_pages == last_page else "pagination_incomplete"
                ),
                expected_pages=last_page,
                visited_pages=visited_pages,
                raw_items=raw_items,
                parsed_items=parsed_items,
                item_failures=item_failures,
            )

    async def _fetch_listing_page(self, url: str) -> str:
        """Страница листинга с ограниченным повтором при сбое сети.

        Один не отданный листинг рушит проверку ВСЕГО каталога (маршрут неполон
        → catalog_verified=false), а полный сбор — это сотни страниц. За
        сентябрь–октябрь 2026 три полных сбора из 30 пропали из-за единственного
        ReadError/ReadTimeout. Страница, не отдавшаяся и с повторами, по-прежнему
        роняет маршрут.

        Только листинги: сбой карточки товара (определение страны) маршрут и так
        не роняет, повтор там ничего не даёт проверке и лишь растягивает прогон.
        """
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=2, min=2, max=60),
            retry=retry_if_exception(_is_transient_fetch_error),
            before_sleep=self._note_fetch_retry,
            reraise=True,
        )
        return await retrying(self._fetch_listing_html, url)

    def _note_fetch_retry(self, retry_state: RetryCallState) -> None:
        # Повтор гасит сбой, но прятать его не должен: счётчик уходит в
        # Run.run_quality, иначе деградация сайта пропадёт из виду до дня, когда
        # повторов перестанет хватать.
        self.fetch_retries += 1
        outcome = retry_state.outcome
        error = outcome.exception() if outcome is not None else None
        log.warning(
            "aloe_fetch_retry",
            url=retry_state.args[0] if retry_state.args else None,
            attempt=retry_state.attempt_number,
            error=f"{type(error).__name__}: {error}"[:200],
        )

    async def _fetch_listing_html(self, url: str) -> str:
        await self._throttle()
        timeout = float(os.getenv("ALOE_HTTP_TIMEOUT_SEC", str(self.timeout_sec)))
        async with httpx.AsyncClient(
            headers=_ALOE_HTTP_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def _scrape_category_playwright(
        self, category_slug: str, limit: int | None = None, max_pages: int = 100
    ) -> AsyncIterator[ScrapedProduct]:
        """Aloe пагинация через ?page=N (12 товаров на страницу).

        max_pages=100 — safety net. Реально цикл обрывается через
        pagination_done когда страница 0 новых уникальных.
        """
        if not category_slug:
            return
        if "=" in category_slug:
            base_url = f"{self.base_url}/catalog/filters/?{category_slug}"
        elif category_slug in ("bestseller", "new", "promo", "seasonal_product_now_is_the_time"):
            base_url = f"{self.base_url}/catalog/filters/?product_field={category_slug}"
        else:
            base_url = f"{self.base_url}/catalog/filters/?category_slug={category_slug}"

        seen_external_ids: set[str] = set()
        yielded = 0
        total_cards_found = 0
        total_card_failures = 0
        total_cards_parsed = 0

        for page_num in range(1, max_pages + 1):
            url = base_url if page_num == 1 else f"{base_url}&page={page_num}"
            page = await self.new_page()
            page_yielded = 0
            page_dropped_dup = 0
            page_dropped_empty = 0
            try:
                await self.goto(page, url)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    log.debug("aloe_networkidle_timeout", url=url)
                await self._scroll_until_stable(page, max_iterations=15)

                cards = await page.query_selector_all('[class*="productCardWrapper"]')
                total_cards_found += len(cards)
                log.info(
                    "aloe_cards_found",
                    category=category_slug,
                    page=page_num,
                    count=len(cards),
                )

                for card in cards:
                    if limit and yielded >= limit:
                        self._set_route_status(
                            category_slug,
                            complete=False,
                            abort_reason="requested_limit_reached",
                            visited_pages=page_num,
                            raw_items=total_cards_found,
                            parsed_items=total_cards_parsed,
                            item_failures=total_card_failures,
                        )
                        return
                    try:
                        product = await self._parse_card(card, category_slug, url)
                        if not product:
                            page_dropped_empty += 1
                            total_card_failures += 1
                            continue
                        total_cards_parsed += 1
                        if product.external_id in seen_external_ids:
                            page_dropped_dup += 1
                            continue
                        seen_external_ids.add(product.external_id)
                        page_yielded += 1
                        yielded += 1
                        yield product
                    except Exception as e:
                        total_card_failures += 1
                        log.warning("aloe_card_parse_failed", error=str(e))
                if page_dropped_empty or page_dropped_dup:
                    log.info(
                        "aloe_page_drops",
                        page=page_num,
                        dropped_empty=page_dropped_empty,
                        dropped_dup=page_dropped_dup,
                    )
            finally:
                await page.close()

            if page_yielded == 0:
                log.info(
                    "aloe_pagination_done",
                    category=category_slug,
                    pages=page_num,
                    total_cards_found=total_cards_found,
                    total_yielded=yielded,
                    total_card_failures=total_card_failures,
                )
                self._set_route_status(
                    category_slug,
                    complete=(
                        total_card_failures == 0 and page_dropped_dup == 0
                    ),
                    pages_skipped=total_card_failures,
                    abort_reason=(
                        "duplicate_only_page"
                        if page_dropped_dup
                        else None
                        if total_card_failures == 0
                        else "card_parse_failures"
                    ),
                    visited_pages=page_num,
                    raw_items=total_cards_found,
                    parsed_items=total_cards_parsed,
                    item_failures=total_card_failures,
                )
                break
        if str(category_slug) not in self._route_statuses:
            self._set_route_status(
                category_slug,
                complete=False,
                abort_reason="max_pages_reached",
                expected_pages=max_pages,
                visited_pages=max_pages,
                raw_items=total_cards_found,
                parsed_items=total_cards_parsed,
                item_failures=total_card_failures,
            )

    async def _scroll_until_stable(self, page, max_iterations: int = 20) -> None:
        prev_count = -1
        same_streak = 0
        for _ in range(max_iterations):
            count = await page.evaluate(
                "document.querySelectorAll('[class*=\"productCardWrapper\"]').length"
            )
            if count == prev_count:
                same_streak += 1
                if same_streak >= 2:
                    break
            else:
                same_streak = 0
            prev_count = count
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

    async def _parse_card(self, card, category: str, listing_url: str) -> ScrapedProduct | None:
        # имя — первое предпочтение productName, затем alt у изображения
        name_handle = await card.query_selector('[class*="productName"]')
        name = (await name_handle.inner_text()).strip() if name_handle else ""
        if not name:
            img = await card.query_selector("img")
            if img:
                name = (await img.get_attribute("alt") or "").strip()
        if not name:
            return None

        # бренд — внутри карточки есть <a class="style_brand_*">
        brand_handle = await card.query_selector('[class*="brand"]')
        brand = (await brand_handle.inner_text()).strip() if brand_handle else None

        # цена
        price_handle = await card.query_selector('[class*="priceWrapper"] [class*="style_price"]')
        if not price_handle:
            price_handle = await card.query_selector('[class*="price"]:not([class*="old"])')
        price_text = await price_handle.inner_text() if price_handle else None
        price = parse_price(price_text)

        old_price_handle = await card.query_selector('[class*="oldPriceText"], [class*="oldPrice"]')
        old_price_text = await old_price_handle.inner_text() if old_price_handle else None
        old_price = parse_price(old_price_text)

        is_on_sale = old_price is not None and price is not None and old_price > price
        discount_percent = None
        if is_on_sale and old_price:
            discount_percent = round((1 - price / old_price) * 100, 1)  # type: ignore[operator]

        img_handle = await card.query_selector('[class*="productImg"] img')
        image_url = await img_handle.get_attribute("src") if img_handle else None

        promo_handle = await card.query_selector(
            '[class*="properties"], [class*="badge"], [class*="discount"]'
        )
        promo_text = (await promo_handle.inner_text()).strip() if promo_handle else None
        promo_label = promo_text if promo_text and len(promo_text) < 50 else None

        # external_id: aloe не даёт стабильного id в карточке, генерируем из имени.
        # Task #33 (2026-05-28): используем aloe_slug, идентичен URL slug → стабильность
        # внешнего id между runs + точное соответствие detail page URL.
        slug = aloe_slug(name)
        external_id = slug[:100] if slug else name.lower().replace(" ", "-")[:100]

        # URL synthesis (Task #33 fix): aloe.az/{slug}/ — verified via Playwright MCP
        # click-navigation. Fallback на listing_url если slugification failed
        # (empty slug, edge case с не-Azeri/Latin/Cyrillic chars).
        product_url = f"{self.base_url}/{slug}/" if slug else listing_url

        return ScrapedProduct(
            site=self.site_name,
            external_id=external_id,
            url=product_url,
            name=name,
            brand=brand,
            category=category,
            dosage=extract_dosage(name),
            pack_size=extract_pack_size(name),
            image_url=image_url,
            price=old_price if is_on_sale else price,
            discount_price=price if is_on_sale else None,
            discount_percent=discount_percent,
            is_on_sale=is_on_sale,
            promo_label=promo_label,
        )

    async def scrape_product_page(self, url: str) -> ScrapedProduct | None:
        """Watchlist-режим: страница товара по адресу https://aloe.az/{slug}/."""
        page = await self.new_page()
        try:
            await self.goto(page, url)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            try:
                await page.wait_for_selector('h1, [class*="style_title"]', timeout=20000)
            except Exception:
                log.warning("aloe_product_page_timeout", url=url)
                return None

            name_handle = await page.query_selector('h1[class*="style_title"], h1')
            name = (await name_handle.inner_text()).strip() if name_handle else ""
            if not name:
                title = await page.title()
                name = title.split("|")[0].strip() if title else ""
            if not name:
                return None

            price_handle = await page.query_selector(
                '[class*="priceWrapper"] [class*="style_price"]'
            )
            price = parse_price(await price_handle.inner_text()) if price_handle else None

            old_price_handle = await page.query_selector('[class*="oldPrice"], [class*="OldPrice"]')
            old_price = (
                parse_price(await old_price_handle.inner_text()) if old_price_handle else None
            )

            is_on_sale = old_price is not None and price is not None and old_price > price
            discount_percent = None
            if is_on_sale and old_price:
                discount_percent = round((1 - price / old_price) * 100, 1)  # type: ignore[operator]

            # бренд может быть указан рядом с именем
            brand_handle = await page.query_selector(
                '[class*="brand"]:not([class*="brand-"]), [class*="Brand"]'
            )
            brand = (await brand_handle.inner_text()).strip() if brand_handle else None

            img_handle = await page.query_selector(
                '[class*="productImg"] img, main img, [class*="gallery"] img'
            )
            image_url = await img_handle.get_attribute("src") if img_handle else None

            external_id = url.rstrip("/").split("/")[-1]

            # Phase 2.2 — try to extract barcode from JSON-LD injected post-
            # hydration. Next.js apps put <script type="application/ld+json">
            # with schema.org Product (including gtin*) into the DOM.
            barcode: str | None = None
            try:
                from src.scrapers.ai_crawler import (
                    _extract_barcode_from_jsonld,
                    parse_jsonld_product,
                )

                html = await page.content()
                jsonld = parse_jsonld_product(html)
                if jsonld:
                    barcode = _extract_barcode_from_jsonld(jsonld)
            except Exception as exc:
                log.debug("aloe_barcode_extract_failed", url=url, error=str(exc))

            country_raw, availability_status = aloe_product_detail_signals(html)

            return ScrapedProduct(
                site=self.site_name,
                external_id=external_id,
                url=url,
                name=name,
                brand=brand,
                manufacturer_country_raw=country_raw,
                country_source="aloe_detail_country_label" if country_raw else None,
                offer_availability_status=availability_status,
                availability_source="aloe_detail_in_stock",
                dosage=extract_dosage(name),
                pack_size=extract_pack_size(name),
                image_url=image_url,
                price=old_price if is_on_sale else price,
                discount_price=price if is_on_sale else None,
                discount_percent=discount_percent,
                is_on_sale=is_on_sale,
                barcode=barcode,
            )
        finally:
            await page.close()

    async def scrape_promos(self) -> list[ScrapedPromo]:
        page = await self.new_page()
        try:
            await self.goto(page, f"{self.base_url}/promos/")
            await page.wait_for_timeout(3000)
            promos: list[ScrapedPromo] = []
            cards = await page.query_selector_all(
                '[class*="banner"], [class*="Banner"], [class*="promoCard"], [class*="PromoCard"]'
            )
            for card in cards[:15]:
                try:
                    text = (await card.inner_text()).strip()
                    if not text or len(text) > 500:
                        continue
                    img = await card.query_selector("img")
                    img_url = await img.get_attribute("src") if img else None
                    link = await card.query_selector("a")
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
            await page.close()


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
