"""Irshad (irshad.az) — каталог из списков товаров, без браузера.

Сайт отдаёт список раздела готовой вёрсткой: ``/az/list-products/<раздел>``
с параметром ``page=N`` возвращает только карточки (6 КБ на проводе против 580
у целой страницы; замер 2026-11-12), по девять на страницу. Числа товаров в
разделе сайт не называет: конец списка — это страница без ссылки «показать
ещё» (``id="loadMore"``); страница за концом списка приходит пустой.

Товар узнаётся по номеру товара на сайте — он стоит в классе карточки
(``product-108753_…``). Обычная цена — ``old-price``, действующая —
``new-price``; у товара без скидки есть только вторая. Наличие сайт пишет
меткой «Stokda var».

Сайт открывается с адреса дата-центра напрямую, прокси не нужен. В robots.txt
он просит паузу 30 секунд между запросами — она и стоит по умолчанию
(``IRSHAD_RATE_LIMIT_SEC`` меняет её).

Карточка с выбором цвета — это несколько товаров: у каждого цвета свой номер,
адрес, название и свой блок с ценой (``product__flex-right`` с
``data-selected-id``), на экране виден один. Сборщик берёт все.
"""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import quote

import structlog

from src.product_policy import OFFER_IN_STOCK, OFFER_OUT_OF_STOCK, OFFER_UNKNOWN
from src.scrapers.base import ScrapedProduct
from src.scrapers.http_catalog import (
    CategoryPass,
    HttpCatalogScraper,
    PageFetchError,
    price_or_none,
    sale_fields,
)

log = structlog.get_logger()

_MAX_PAGES = 1500
_CARD_OPEN_RE = re.compile(r'<div class="product (?:first )?product-(\d+)_[0-9a-f-]+[^"]*">')
_NAME_LINK_RE = re.compile(
    r'<a\s+href="(https://irshad\.az/az/mehsullar/[^"]+)"[^>]*'
    r'class="product__name product-link"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TYPE_RE = re.compile(r'class="product__type">(.*?)</div>', re.DOTALL)
_OLD_PRICE_RE = re.compile(r'class="old-price">(.*?)</span>', re.DOTALL)
_NEW_PRICE_RE = re.compile(r'class="new-price">(.*?)</p>', re.DOTALL)
_LABEL_RE = re.compile(r'class="product__label[^"]*">(.*?)</div>', re.DOTALL)
_IMAGE_RE = re.compile(r'class="product__img".*?<img\s+src="([^"]+)"', re.DOTALL)
_LOAD_MORE_RE = re.compile(r'id="loadMore"[^>]*data-page="(\d+)"')
_HOME_LINK_RE = re.compile(r'href="https://irshad\.az/az/([a-z0-9-]+(?:/[a-z0-9-]+)*)"')
_TAG_RE = re.compile(r"<[^>]+>")

# Разделы первого уровня, которые повторяют товары каталога (уценка, акции)
# или каталогом не являются.
_NOT_CATALOG_ROOTS = {
    "outlet",
    "kampaniyalar",
    "brend",
    "mehsullar",
    "sebet",
    "muqayise",
    "beyendiklerim",
    "giris",
    "magazalar",
    "sirket",
    "musteri-ucun",
    "sexsi-kabinet",
    "irshad-youtube",
    "mehsul-icmali",
}


def _text(fragment: str | None) -> str:
    if not fragment:
        return ""
    return " ".join(html_lib.unescape(_TAG_RE.sub(" ", fragment)).split())


def split_cards(page_html: str) -> list[tuple[str, str]]:
    """Разрезать вёрстку списка на карточки: ``[(номер товара, вёрстка)]``."""
    opens = list(_CARD_OPEN_RE.finditer(page_html))
    cards = []
    for index, match in enumerate(opens):
        end = opens[index + 1].start() if index + 1 < len(opens) else len(page_html)
        cards.append((match.group(1), page_html[match.start() : end]))
    return cards


_OFFER_BLOCK_RE = re.compile(r'<div class="product__flex-right[^"]*"\s+data-selected-id="(\d+)"')
_COLOR_INPUT_RE = re.compile(r'<input\s+type="radio"[^>]*>', re.DOTALL)
_ATTR_RE = re.compile(r'([a-zA-Z-]+)="([^"]*)"')


def _availability(block_html: str) -> str:
    for label in _LABEL_RE.findall(block_html):
        lowered = _text(label).casefold()
        if lowered.startswith("stokda var"):
            return OFFER_IN_STOCK
        if "stokda yoxdur" in lowered or "bitib" in lowered:
            return OFFER_OUT_OF_STOCK
    return OFFER_UNKNOWN


def build_products(
    product_id: str, card_html: str, category_slug: str
) -> tuple[list[ScrapedProduct], int]:
    """Товары одной карточки и число предложений, которые не удалось опознать.

    У обычной карточки товар один. У карточки с выбором цвета их столько,
    сколько цветов: название и адрес цвета стоят у его переключателя, цена и
    наличие — в блоке предложения с тем же номером.
    """
    # Кто есть кто: номер товара -> (адрес, название, картинка).
    identities: dict[str, tuple[str, str, str | None]] = {}
    link = _NAME_LINK_RE.search(card_html)
    image = _IMAGE_RE.search(card_html)
    if link:
        identities[product_id] = (
            html_lib.unescape(link.group(1)).strip(),
            _text(link.group(2)),
            html_lib.unescape(image.group(1)) if image else None,
        )
    for raw_input in _COLOR_INPUT_RE.findall(card_html):
        attrs = dict(_ATTR_RE.findall(raw_input))
        variant_id = attrs.get("value", "")
        url = html_lib.unescape(attrs.get("data-url", "")).strip()
        title = _text(attrs.get("data-title"))
        if variant_id.isdigit() and url and title:
            identities[variant_id] = (url, title, attrs.get("data-img-src") or None)

    # Блоки предложений: у каждого своя цена и своё наличие.
    opens = list(_OFFER_BLOCK_RE.finditer(card_html))
    blocks = [
        (
            match.group(1),
            card_html[
                match.start() : opens[i + 1].start() if i + 1 < len(opens) else len(card_html)
            ],
        )
        for i, match in enumerate(opens)
    ] or [(product_id, card_html)]

    product_type = _text((_TYPE_RE.search(card_html) or [None, None])[1])
    products: list[ScrapedProduct] = []
    failures = 0
    for offer_id, block_html in blocks:
        identity = identities.get(offer_id)
        if identity is None or not identity[0] or not identity[1]:
            failures += 1
            continue
        url, name, image_url = identity
        current = price_or_none(_text((_NEW_PRICE_RE.search(block_html) or [None, None])[1]))
        old = price_or_none(_text((_OLD_PRICE_RE.search(block_html) or [None, None])[1]))
        # У товара без скидки сайт пишет одну цену — в «new-price».
        price, discount_price, discount_percent, on_sale = sale_fields(old or current, current)
        products.append(
            ScrapedProduct(
                site="irshad",
                external_id=offer_id,
                url=url,
                name=name,
                identity_verified=True,
                offer_availability_status=_availability(block_html),
                availability_source="irshad_stock_label",
                category=category_slug,
                image_url=image_url,
                description=product_type or None,
                price=price,
                discount_price=discount_price,
                discount_percent=discount_percent,
                is_on_sale=on_sale,
            )
        )
    return products, failures


class IrshadScraper(HttpCatalogScraper):
    site_name = "irshad"
    base_url = "https://irshad.az"
    # robots.txt сайта: Crawl-delay: 30.
    default_rate_limit_sec = 30.0

    async def _list_page(self, category_slug: str, page: int) -> str:
        response = await self.fetch(
            f"{self.base_url}/az/list-products/{quote(category_slug.strip('/'), safe='/')}",
            params={"page": str(page)},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "text/html, */*"},
        )
        return response.text

    async def _collect_category(self, category_slug: str, limit: int | None) -> CategoryPass:
        result = CategoryPass()
        seen: set[str] = set()
        page = 1
        while page <= _MAX_PAGES:
            try:
                page_html = await self._list_page(category_slug, page)
            except PageFetchError as exc:
                result.pages_skipped += 1
                result.abort_reason = f"page_{page}_failed: {exc.reason}"[:200]
                return result
            result.visited_pages = page
            cards = split_cards(page_html)
            if not cards:
                # Пустая первая страница — раздела нет или вёрстка сменилась;
                # пустая страница, на которую сайт сам позвал, — обрыв списка.
                result.abort_reason = "empty_listing" if page == 1 else "empty_page_before_last"
                return result
            for product_id, card_html in cards:
                products, failures = build_products(product_id, card_html, category_slug)
                result.raw_items += len(products) + failures
                result.item_failures += failures
                for product in products:
                    if product.external_id in seen:
                        continue
                    seen.add(product.external_id)
                    result.products.append(product)
                    if limit is not None and len(result.products) >= limit:
                        result.abort_reason = "requested_limit_reached"
                        return result
            more = _LOAD_MORE_RE.search(page_html)
            if not more:
                break
            next_page = int(more.group(1))
            if next_page <= page:
                result.abort_reason = "pagination_does_not_advance"
                return result
            page = next_page
        else:
            result.abort_reason = "page_limit_reached"
            return result

        if result.item_failures:
            result.abort_reason = "item_parse_failures"
            return result
        if len(result.products) != result.raw_items:
            # Один товар пришёл дважды: список сдвинулся между страницами, и
            # какой-то другой товар мог не прийти вовсе.
            result.abort_reason = "listing_shifted_between_pages"
            return result
        result.complete = True
        return result

    async def discover_categories(self) -> list[str]:
        """Разделы первого уровня из меню главной страницы."""
        response = await self.fetch(f"{self.base_url}/az")
        links = set(_HOME_LINK_RE.findall(response.text))
        parents = {link.split("/")[0] for link in links if "/" in link}
        return sorted(parents - _NOT_CATALOG_ROOTS)
