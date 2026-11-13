"""Irshad (irshad.az) — каталог из списков товаров, без браузера.

Сайт отдаёт список раздела готовой вёрсткой: ``/az/list-products/<раздел>``
с параметром ``page=N`` возвращает только карточки (6 КБ на проводе против 580
у целой страницы; замер 2026-11-12), по девять на страницу. Числа товаров в
разделе сайт не называет. Конец списка сборщик подтверждает двумя признаками
сразу: на странице нет ссылки «показать ещё» (``id="loadMore"``) **и** следующая
за ней страница приходит пустой. Один признак без другого — неполный проход:
пропавшая из вёрстки ссылка иначе превращала бы каждый раздел в «полный» раздел
из одной страницы.

Список запрашивается в порядке номеров товаров по возрастанию (``sort=id``).
По умолчанию сайт отдаёт новинки первыми (``-id``), и каждый товар, добавленный
посреди обхода, сдвигал все следующие страницы: первый полный сбор 2026-11-13
оборвался так в четырёх больших разделах из четырнадцати, и повтор прохода не
помог. При возрастании новый товар встаёт в конец и страниц не сдвигает.

Карточка, пришедшая второй раз, проход не обрывает: товар с несколькими цветами
сайт ставит в список не один раз, на местах разных своих цветов, и до правки
2026-11-13 раздел мелкой техники обрывался на 16-й странице при каждой попытке
(``listing_shifted_between_pages``). Повторная карточка разбирается снова —
уже взятые товары отсеиваются, новый цвет берётся. Обрыв — только страница с
тем же набором карточек, что уже читали. Не виден один случай сдвига: товар, снятый с сайта посреди обхода, сдвигает список вверх
без повтора, и один из следующих товаров может не попасть ни на одну страницу.
Без числа товаров от сайта этого не обнаружить.

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
from src.scrapers.base import CaptchaDetected, ScrapedProduct
from src.scrapers.http_catalog import (
    CategoryPass,
    HttpCatalogScraper,
    PageFetchError,
    price_or_none,
    sale_fields,
)

log = structlog.get_logger()

_MAX_PAGES = 1500
# Порядок списка, в котором добавленный посреди обхода товар не сдвигает
# страницы: по номеру товара, старые первыми.
_STABLE_SORT = "id"
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
        new_match = _NEW_PRICE_RE.search(block_html)
        old_match = _OLD_PRICE_RE.search(block_html)
        current = price_or_none(_text(new_match[1])) if new_match else None
        old = price_or_none(_text(old_match[1])) if old_match else None
        if old_match and current is None:
            # Есть зачёркнутая цена, а действующей нет или она не прочиталась:
            # выдать зачёркнутую за действующую хуже, чем не выдать товар.
            # Товар вовсе без цены («по запросу») отказом не считается — у
            # него цена пуста, как и на других сайтах.
            failures += 1
            continue
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
            params={"page": str(page), "sort": _STABLE_SORT},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "text/html, */*"},
        )
        return response.text

    async def _collect_category(self, category_slug: str, limit: int | None) -> CategoryPass:
        result = CategoryPass()
        seen: set[str] = set()
        seen_cards: set[str] = set()
        seen_pages: set[tuple[str, ...]] = set()
        page = 1
        # Позвал ли сайт на эту страницу ссылкой «показать ещё» с предыдущей.
        announced = True
        # Страница-подтверждение за последней разрешённой запрашивается всё равно.
        while page <= _MAX_PAGES or not announced:
            try:
                page_html = await self._list_page(category_slug, page)
            except (PageFetchError, CaptchaDetected) as exc:
                return self.page_failed(result, page, exc)
            cards = split_cards(page_html)
            if cards or page == 1:
                # Пустая страница-подтверждение страницей списка не считается.
                result.visited_pages = page
            if not cards:
                if page == 1:
                    # Раздела нет или вёрстка сменилась.
                    result.abort_reason = "empty_listing"
                    return result
                if announced:
                    # Сайт сам позвал на эту страницу, а она пуста: обрыв.
                    result.abort_reason = "empty_page_before_last"
                    return result
                break  # конец списка подтверждён пустой страницей за ним
            if not announced:
                # Ссылки «показать ещё» не было, а товары дальше есть: признак
                # конца списка пропал из вёрстки, верить ему больше нельзя.
                result.abort_reason = "load_more_marker_missing"
                return result
            page_cards = tuple(product_id for product_id, _ in cards)
            if page_cards in seen_pages:
                # Та же страница второй раз: листание сломано.
                result.abort_reason = "listing_page_repeated"
                return result
            seen_pages.add(page_cards)
            repeated = [product_id for product_id in page_cards if product_id in seen_cards]
            if repeated:
                # Карточка, которую уже видели, — не обрыв. Товар с несколькими
                # цветами сайт ставит в список не один раз — на местах разных
                # своих цветов (замер 2026-11-13: карточка 65715 — на 7-й
                # странице раздела, на месте цвета 38401, и на 16-й). Такая
                # карточка разбирается снова: товары, взятые с первой встречи,
                # отсеет `seen`, а цвет, которого в первой не было, не
                # потеряется. При порядке по возрастанию номера повтор не
                # прячет другой товар и при сдвиге списка: вставленный посреди
                # товар сдвигает следующие вниз, на ещё не читанные страницы.
                log.info(
                    "irshad_repeated_cards",
                    category=category_slug,
                    page=page,
                    cards=repeated,
                )
            seen_cards.update(page_cards)
            for product_id, card_html in cards:
                products, failures = build_products(product_id, card_html, category_slug)
                if failures:
                    # Раздел из-за такой карточки неполон; без номера карточки
                    # причину потом не найти — список длиной в сотни страниц.
                    log.warning(
                        "irshad_card_not_parsed",
                        category=category_slug,
                        page=page,
                        card=product_id,
                        offers_failed=failures,
                    )
                result.raw_items += failures
                result.item_failures += failures
                for product in products:
                    if product.external_id in seen:
                        # Цвет, у которого есть и своя карточка: тот же товар.
                        continue
                    seen.add(product.external_id)
                    result.raw_items += 1
                    result.products.append(product)
                    if limit is not None and len(result.products) >= limit:
                        result.abort_reason = "requested_limit_reached"
                        return result
            more = _LOAD_MORE_RE.search(page_html)
            if more:
                next_page = int(more.group(1))
                if next_page <= page:
                    result.abort_reason = "pagination_does_not_advance"
                    return result
                page, announced = next_page, True
            else:
                # Последняя страница по словам сайта. Проверяем: следующая
                # обязана прийти пустой.
                page, announced = page + 1, False
        else:
            result.abort_reason = "page_limit_reached"
            return result

        if result.item_failures:
            result.abort_reason = "item_parse_failures"
            return result
        result.complete = True
        return result

    async def discover_categories(self) -> list[str]:
        """Разделы первого уровня из меню главной страницы."""
        response = await self.fetch(f"{self.base_url}/az")
        links = set(_HOME_LINK_RE.findall(response.text))
        parents = {link.split("/")[0] for link in links if "/" in link}
        roots = sorted(parents - _NOT_CATALOG_ROOTS)
        if not roots:
            raise PageFetchError("home page has no catalog sections")
        return roots
