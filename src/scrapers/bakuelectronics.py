"""Baku Electronics (bakuelectronics.az) — каталог из данных Next.js.

Сайт собран на Next.js: то, из чего рисуется страница каталога, лежит в самой
странице (``<script id="__NEXT_DATA__">``) и отдельно — по адресу
``/_next/data/<buildId>/az/catalog/<раздел>.json`` (12 КБ на проводе против
~50 у страницы; замер 2026-11-12). ``buildId`` меняется с каждой выкладкой
сайта: сборщик читает его с главной и перечитывает, получив 404.

В ответе ``pageProps.products.products``: ``items`` по 18 на страницу,
``total``, ``page``, ``size``; страница — ``?page=N``.

Раздел верхнего уровня (``/catalog/<раздел>``) отдаёт товары всех своих
подразделов: на разделе tv-audio-video список раздела и объединение 19 его
конечных подразделов совпали товар в товар (371 и 371; замер 2026-11-12).
Поэтому полный каталог — это разделы верхнего уровня. Исключение — раздел, у
которого своя страница сломана на самом сайте (telefonlar-qadcetler отвечал
502): ``discover_categories`` проверяет каждый раздел и вместо нерабочего берёт
его подразделы.

Дерево разделов в вёрстке главной не лежит: браузер получает меню отдельным
запросом к API сайта (``/megamenu`` на служебном имени, заголовок ``lang``), и
ссылки ``catalog/<раздел>/<подраздел>`` стоят в нём на всех уровнях (замер
через прокси 2026-11-13: 770 ссылок, 447 разных разделов, 22 верхних). Часть
ссылок меню ведёт на тот же раздел с фильтром (``?brand=apple``) — фильтр
отбрасывается.

Товар узнаётся по ``product_code`` — коду товара на сайте (им же кончается
адрес страницы товара). Обычная цена — ``price``, цена со скидкой —
``discounted_price``, остаток — числом (``quantity``).

С адреса дата-центра сайт отвечает блокировкой Cloudflare: серверу нужен выход
через прокси (сайт в ``DECODO_SITES``).
"""

from __future__ import annotations

import json
import math
import re
from urllib.parse import quote

import structlog

from src.product_policy import offer_from_quantity
from src.scrapers.base import CaptchaDetected, ScrapedProduct
from src.scrapers.http_catalog import (
    CategoryPass,
    HttpCatalogScraper,
    PageFetchError,
    counted_pass_is_complete,
    price_or_none,
    sale_fields,
)

log = structlog.get_logger()

_MAX_PAGES = 600
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_MENU_CATALOG_LINK_RE = re.compile(r"(?:^|/)catalog/([^?#]+)")
# Страницы под /catalog/, которые не разделы каталога.
_NOT_CATEGORIES = {"muqayise-et"}


def build_product(item: dict, category_slug: str) -> ScrapedProduct | None:
    """Товар из записи ``items``; ``None``, если его не опознать."""
    code = item.get("product_code")
    name = " ".join(str(item.get("name") or "").split())
    slug = str(item.get("slug") or "").strip().strip("/")
    if code in (None, "") or isinstance(code, bool) or not name or not slug:
        return None

    regular = price_or_none(item.get("price"))
    final = price_or_none(item.get("discounted_price"))
    price, discount_price, discount_percent, on_sale = sale_fields(regular, final)
    availability, quantity = offer_from_quantity(item.get("quantity"))

    return ScrapedProduct(
        site="bakuelectronics",
        external_id=str(code).strip(),
        url=f"https://www.bakuelectronics.az/mehsul/{slug}",
        name=name,
        identity_verified=True,
        offer_availability_status=availability,
        offer_quantity=quantity,
        availability_source="bakuelectronics_quantity",
        category=category_slug,
        image_url=item.get("image") or None,
        price=price,
        discount_price=discount_price,
        discount_percent=discount_percent,
        is_on_sale=on_sale,
    )


def listing_from_payload(payload: object) -> dict | None:
    """Блок списка товаров из данных страницы каталога, если он там есть."""
    if not isinstance(payload, dict):
        return None
    page_props = payload.get("pageProps")
    if not isinstance(page_props, dict):
        page_props = (payload.get("props") or {}).get("pageProps")
    if not isinstance(page_props, dict):
        return None
    listing = (page_props.get("products") or {}).get("products")
    if not isinstance(listing, dict) or not isinstance(listing.get("items"), list):
        return None
    return listing


class BakuElectronicsScraper(HttpCatalogScraper):
    site_name = "bakuelectronics"
    base_url = "https://www.bakuelectronics.az"
    # Откуда витрина берёт меню разделов. Имя служебное и может смениться с
    # выкладкой сайта: тогда обход разделов отказывает, а не возвращает пусто.
    menu_url = "https://5vyra9x.bakuelectronics.az/megamenu"
    # ``total: 0`` — явный ответ сайта «в разделе нет товаров».
    allow_verified_empty_categories = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._build_id: str | None = None

    async def _load_home(self) -> str:
        response = await self.fetch(f"{self.base_url}/")
        html = response.text
        match = _NEXT_DATA_RE.search(html)
        if not match:
            raise PageFetchError("home page has no page data")
        try:
            build_id = json.loads(match.group(1)).get("buildId")
        except (ValueError, AttributeError) as exc:
            raise PageFetchError("home page data is not readable") from exc
        if not build_id or not isinstance(build_id, str):
            raise PageFetchError("home page data has no build id")
        self._build_id = build_id
        return html

    async def _menu_sections(self) -> list[str]:
        """Разделы каталога из меню сайта, в порядке меню, без повторов."""
        response = await self.fetch(
            self.menu_url, headers={"Accept": "application/json", "lang": "az"}
        )
        try:
            menu = response.json()
        except ValueError as exc:
            raise PageFetchError("catalog menu is not readable") from exc

        sections: list[str] = []
        seen: set[str] = set()

        def walk(node: object) -> None:
            if isinstance(node, dict):
                link = node.get("link")
                if isinstance(link, str):
                    match = _MENU_CATALOG_LINK_RE.search(link)
                    slug = match.group(1).strip("/") if match else ""
                    if slug and slug.split("/")[0] not in _NOT_CATEGORIES and slug not in seen:
                        seen.add(slug)
                        sections.append(slug)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(menu)
        return sections

    async def _listing_page(self, category_slug: str, page: int) -> dict:
        """Одна страница списка раздела; ``PageFetchError``, если списка нет."""
        slug = quote(category_slug.strip("/"), safe="/")
        params = {"page": str(page)} if page > 1 else None
        for refreshed in (False, True):
            if self._build_id is None:
                await self._load_home()
            response = await self.fetch(
                f"{self.base_url}/_next/data/{self._build_id}/az/catalog/{slug}.json",
                params=params,
                headers={"Accept": "application/json", "x-nextjs-data": "1"},
                accept_statuses=(200, 404),
            )
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if response.status_code == 404:
                if isinstance(payload, dict) and payload.get("notFound"):
                    raise PageFetchError("category does not exist", status=404)
                if not refreshed:
                    # Сайт выложили заново: прежний buildId больше не отвечает.
                    self._build_id = None
                    continue
                raise PageFetchError("page data is not available", status=404)
            listing = listing_from_payload(payload)
            if listing is None:
                raise PageFetchError("response has no product listing")
            return listing
        raise PageFetchError("page data is not available")

    async def _collect_category(self, category_slug: str, limit: int | None) -> CategoryPass:
        result = CategoryPass()
        seen: set[str] = set()
        page = 1
        while page <= _MAX_PAGES:
            try:
                listing = await self._listing_page(category_slug, page)
            except (PageFetchError, CaptchaDetected) as exc:
                return self.page_failed(result, page, exc)
            items = listing["items"]
            if page == 1:
                total = listing.get("total")
                size = listing.get("size")
                if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                    result.abort_reason = "invalid_total"
                    return result
                if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                    result.abort_reason = "invalid_page_size"
                    return result
                result.expected_items = total
                result.expected_pages = math.ceil(total / size)
            result.visited_pages = page
            if not items and page <= (result.expected_pages or 0):
                result.abort_reason = "empty_page_before_last"
                return result
            for item in items:
                result.raw_items += 1
                product = build_product(item, category_slug) if isinstance(item, dict) else None
                if product is None:
                    result.item_failures += 1
                    continue
                if product.external_id in seen:
                    continue
                seen.add(product.external_id)
                result.products.append(product)
                if limit is not None and len(result.products) >= limit:
                    result.abort_reason = "requested_limit_reached"
                    return result
            if page >= (result.expected_pages or 0):
                break
            page += 1
        else:
            result.abort_reason = "page_limit_reached"
            return result

        if not counted_pass_is_complete(result):
            result.abort_reason = (
                "item_parse_failures" if result.item_failures else "item_count_mismatch"
            )
            return result
        result.complete = True
        return result

    async def discover_categories(self) -> list[str]:
        """Разделы, обход которых даёт полный каталог.

        Берутся разделы верхнего уровня из меню сайта. Раздел, чья
        собственная страница списка не отвечает, заменяется своими
        подразделами — и так вниз по дереву ссылок меню.
        """
        links = await self._menu_sections()

        roots: list[str] = []
        for slug in links:
            root = slug.split("/")[0]
            if root not in roots:
                roots.append(root)

        def children_of(parent: str) -> list[str]:
            depth = parent.count("/") + 2
            return [
                slug
                for slug in links
                if slug.startswith(f"{parent}/") and slug.count("/") + 1 == depth
            ]

        if not roots:
            raise PageFetchError("catalog menu has no sections")

        routes: list[str] = []
        queue = list(roots)
        while queue:
            slug = queue.pop(0)
            try:
                await self._listing_page(slug, 1)
            except PageFetchError as exc:
                children = children_of(slug)
                log.warning(
                    "bakuelectronics_section_listing_unavailable",
                    category=slug,
                    error=exc.reason,
                    replaced_by_children=len(children),
                )
                if not children:
                    # Заменить нечем: список без этого раздела был бы неполным
                    # каталогом, который выглядит полным.
                    raise PageFetchError(
                        f"section {slug} is not readable and has no subsections"
                    ) from exc
                queue.extend(children)
                continue
            routes.append(slug)
        return routes
