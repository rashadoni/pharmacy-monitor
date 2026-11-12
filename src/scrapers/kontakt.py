"""Kontakt Home (kontakt.az) — каталог из данных самого сайта, без браузера.

Сайт — витрина на React Router поверх Magento. У каждой страницы есть адрес
данных: ``/<путь>.data`` отдаёт то, из чего витрина рисует страницу, в формате
turbo-stream. Параметр ``_routes=routes/$`` оставляет в ответе только данные
самой страницы, без меню и настроек сайта (112 КБ вместо 348; замер 2026-11-12).

Категория бывает двух видов (поле ``type``):
  - ``CATEGORY`` — список товаров: ``products.items`` по 16 на страницу,
    ``products.total_count`` и ``page_info.total_pages``; страница — ``?p=N``;
  - ``SUBCATEGORY`` — витрина раздела: товаров нет, есть подразделы
    (``subcategoriesPage.subcategories[].categoryLink``).
Полный каталог — это все категории первого вида; ``discover_categories``
находит их, спускаясь от меню сайта.

Товар узнаётся по ``sku`` — артикулу Magento. Обычная цена и цена со скидкой
приходят раздельно (``regular_price`` / ``final_price``), наличие — явным
признаком ``stock_item.in_stock``.

С адреса дата-центра сайт отвечает проверкой Cloudflare: серверу нужен выход
через прокси (сайт в ``DECODO_SITES``).
"""

from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import quote, urlsplit

import structlog

from src.product_policy import OFFER_IN_STOCK, OFFER_OUT_OF_STOCK, offer_from_boolean
from src.scrapers.base import ScrapedProduct
from src.scrapers.http_catalog import (
    CategoryPass,
    HttpCatalogScraper,
    PageFetchError,
    price_or_none,
    sale_fields,
)

log = structlog.get_logger()

_ROUTE_KEY = "routes/$"
_PAGE_SIZE = 16
# Предохранители от бесконечного обхода: ни раздела на 400 страниц (6 400
# товаров), ни дерева на 3 000 категорий у сайта нет.
_MAX_PAGES = 400
_MAX_DISCOVERY_NODES = 3000


def decode_turbo_stream(text: str) -> Any:
    """Разобрать ответ ``.data`` (turbo-stream) в обычные словари и списки.

    Первая строка ответа — плоский JSON-массив. Значение под индексом 0 —
    корень; объект в массиве записан как ``{"_K": V}``, где K — индекс строки
    с именем поля, V — индекс значения; список — как список индексов.
    Отрицательный индекс — особое значение (нет значения, null, NaN…): здесь
    все они читаются как ``None``. Список, начинающийся со строки, — значение
    особого типа (дата, обещание, множество); сборщику они не нужны и тоже
    читаются как ``None``. Следующие строки ответа — отложенные значения, их
    страницы каталога не используют.
    """
    first_line = text.split("\n", 1)[0].strip()
    flat = json.loads(first_line)
    if not isinstance(flat, list) or not flat:
        raise ValueError("turbo-stream payload is not a non-empty array")

    cache: dict[int, Any] = {}

    def value_at(index: Any) -> Any:
        if not isinstance(index, int) or isinstance(index, bool):
            return None
        if index < 0 or index >= len(flat):
            return None
        if index in cache:
            return cache[index]
        raw = flat[index]
        if isinstance(raw, list):
            if raw and isinstance(raw[0], str):
                cache[index] = None
                return None
            items: list[Any] = []
            cache[index] = items
            items.extend(value_at(child) for child in raw)
            return items
        if isinstance(raw, dict):
            obj: dict[str, Any] = {}
            cache[index] = obj
            for key_ref, value_ref in raw.items():
                if not key_ref.startswith("_") or not key_ref[1:].isdigit():
                    continue
                key_index = int(key_ref[1:])
                key = flat[key_index] if key_index < len(flat) else None
                if isinstance(key, str):
                    obj[key] = value_at(value_ref)
            return obj
        cache[index] = raw
        return raw

    return value_at(0)


def category_path_from_link(link: str | None) -> str | None:
    """Путь категории из адреса на сайте: ``https://kontakt.az/a/b`` → ``a/b``."""
    if not link or not isinstance(link, str):
        return None
    parts = urlsplit(link.strip())
    if parts.netloc and parts.netloc.removeprefix("www.") != "kontakt.az":
        return None
    path = parts.path.strip("/")
    if path.startswith(("ru/", "en/")):
        path = path[3:]
    return path or None


def build_product(item: dict, category_path: str) -> ScrapedProduct | None:
    """Товар из записи ``products.items``; ``None``, если его не опознать."""
    sku = str(item.get("sku") or "").strip()
    name = " ".join(str(item.get("name") or "").split())
    url_key = str(item.get("url_key") or "").strip().strip("/")
    if not sku or not name or not url_key:
        return None

    minimum = ((item.get("price_range") or {}).get("minimum_price")) or {}
    regular = price_or_none((minimum.get("regular_price") or {}).get("value"))
    final = price_or_none((minimum.get("final_price") or {}).get("value"))
    price, discount_price, discount_percent, on_sale = sale_fields(regular, final)

    availability, _ = offer_from_boolean((item.get("stock_item") or {}).get("in_stock"))
    availability_source = "kontakt_stock_item"
    if availability not in (OFFER_IN_STOCK, OFFER_OUT_OF_STOCK):
        stock_status = str(item.get("stock_status") or "").upper()
        if stock_status == "IN_STOCK":
            availability = OFFER_IN_STOCK
        elif stock_status == "OUT_OF_STOCK":
            availability = OFFER_OUT_OF_STOCK
        availability_source = "kontakt_stock_status"

    image_url = None
    gallery = item.get("media_gallery")
    if isinstance(gallery, list) and gallery and isinstance(gallery[0], dict):
        image_url = gallery[0].get("url") or None

    specs = []
    for attribute in item.get("general_attributes") or []:
        if isinstance(attribute, dict) and attribute.get("label") and attribute.get("value"):
            specs.append(f"{attribute['label']}: {attribute['value']}")

    return ScrapedProduct(
        site="kontakt",
        external_id=sku,
        url=f"https://kontakt.az/{url_key}",
        name=name,
        identity_verified=True,
        offer_availability_status=availability,
        availability_source=availability_source,
        category=category_path,
        image_url=image_url,
        description="; ".join(specs)[:1000] or None,
        price=price,
        discount_price=discount_price,
        discount_percent=discount_percent,
        is_on_sale=on_sale,
    )


class KontaktScraper(HttpCatalogScraper):
    site_name = "kontakt"
    base_url = "https://kontakt.az"
    # ``total_count: 0`` — явный ответ сайта «категория пуста».
    allow_verified_empty_categories = True

    async def _route_data(self, path: str, page: int = 1) -> dict:
        """Данные одной страницы сайта (уже разобранные)."""
        params: dict[str, str] = {"_routes": _ROUTE_KEY}
        if page > 1:
            params["p"] = str(page)
        response = await self.fetch(
            f"{self.base_url}/{quote(path.strip('/'), safe='/')}.data",
            params=params,
            headers={"Accept": "*/*"},
        )
        try:
            payload = decode_turbo_stream(response.text)
        except (ValueError, TypeError) as exc:
            raise PageFetchError(f"undecodable page data: {type(exc).__name__}") from exc
        data = (
            ((payload or {}).get(_ROUTE_KEY) or {}).get("data")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(data, dict):
            raise PageFetchError("page data has no route payload")
        return data

    async def _collect_category(self, category_slug: str, limit: int | None) -> CategoryPass:
        result = CategoryPass()
        seen: set[str] = set()
        page = 1
        while page <= _MAX_PAGES:
            try:
                data = await self._route_data(category_slug, page)
            except PageFetchError as exc:
                result.pages_skipped += 1
                result.abort_reason = f"page_{page}_failed: {exc.reason}"[:200]
                return result
            listing = data.get("products")
            if data.get("type") != "CATEGORY" or not isinstance(listing, dict):
                result.abort_reason = f"not_a_product_listing: {data.get('type')}"[:200]
                return result
            items = listing.get("items") or []
            if page == 1:
                total = listing.get("total_count")
                if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                    result.abort_reason = "invalid_total"
                    return result
                result.expected_items = total
                declared = (listing.get("page_info") or {}).get("total_pages")
                result.expected_pages = (
                    declared
                    if isinstance(declared, int) and declared >= 0
                    else math.ceil(total / _PAGE_SIZE)
                )
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

        if len(result.products) != result.expected_items:
            # Товар сдвинулся между страницами (повтор или пропуск) либо сайт
            # насчитал больше, чем показал.
            result.abort_reason = "item_count_mismatch"
            return result
        result.complete = True
        return result

    async def discover_categories(self) -> list[str]:
        """Все категории-списки сайта: обход от меню вниз по подразделам."""
        response = await self.fetch(f"{self.base_url}/_root.data", headers={"Accept": "*/*"})
        payload = decode_turbo_stream(response.text)
        menu = (((payload or {}).get("root") or {}).get("data") or {}).get("categories") or []

        queue: list[str] = []

        def take_menu(nodes: list) -> None:
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                path = str(node.get("url_path") or "").strip("/")
                if node.get("is_category") and path:
                    queue.append(path)
                else:
                    # Пункт меню без своей страницы — группа: берём её категории.
                    take_menu(node.get("children") or [])

        take_menu(menu)

        listings: list[str] = []
        visited: set[str] = set()
        while queue and len(visited) < _MAX_DISCOVERY_NODES:
            path = queue.pop(0)
            if path in visited:
                continue
            visited.add(path)
            try:
                data = await self._route_data(path)
            except PageFetchError as exc:
                log.warning("kontakt_category_probe_failed", category=path, error=exc.reason)
                continue
            if data.get("type") == "CATEGORY" and isinstance(data.get("products"), dict):
                listings.append(path)
                continue
            subcategories = (data.get("subcategoriesPage") or {}).get("subcategories") or []
            for sub in subcategories:
                child = category_path_from_link((sub or {}).get("categoryLink"))
                if child and child not in visited:
                    queue.append(child)

        # Вложенная категория-список рядом с родительской остаётся в обходе:
        # включает ли список родителя её товары, на сайте не проверено, а
        # повтор товара безвреден — пропуск нет.
        return listings
