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
502 в браузере 2026-11-12 и 301 через прокси 2026-11-13):
``discover_categories`` проверяет каждый раздел и вместо нерабочего берёт его
подразделы.

Дерево разделов в вёрстке главной не лежит: браузер получает меню отдельным
запросом к API сайта (``/megamenu`` на служебном имени, заголовок ``lang``), и
ссылки ``catalog/<раздел>/<подраздел>`` стоят в нём на всех уровнях (замер
через прокси 2026-11-13: 776 ссылок, из них 770 на каталог, 447 разных путей).
Особенности меню, на которые рассчитан разбор:

- ссылка на раздел с фильтром (``?brand=apple``) — фильтр отбрасывается;
- у промежуточного уровня своей ссылки может не быть, есть только ссылки
  глубже (так у 18 узлов) — дерево строится из начал всех путей;
- у ссылки бывает потерян родитель: ``catalog/itler-ve-pishikler-ucun`` при
  ``catalog/ev-heyvanlari-ucun/itler-ve-pishikler-ucun/...``. По одному имени
  это не решается (одинаковые имена стоят в меню под разными родителями):
  обход спрашивает сайт и убирает такой путь из верхних разделов, только если
  его список совпал со списком одноимённого подраздела;
- часть путей уже закодирована (``turizm-v%C9%99-kempinq``) — путь хранится
  раскодированным и кодируется один раз, при запросе;
- баннеры дают полные адреса сайта; ссылки на товары и чужие адреса —
  не разделы.

Меню — единственный источник дерева: раздел, которого в меню нет, обход не
увидит и пропущенным не назовёт. Сверить меню не с чем.

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
from urllib.parse import quote, unquote, urlsplit

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
# Страницы под /catalog/, которые не разделы каталога.
_NOT_CATEGORIES = {"muqayise-et"}
_OWN_DOMAIN = "bakuelectronics.az"
_LANGUAGE_PREFIXES = {"az", "ru", "en"}


def section_from_menu_link(link: str) -> str | None:
    """Путь раздела из ссылки меню; ``None``, если ссылка не на раздел каталога.

    Разделом считается только путь ``catalog/...`` самого сайта: слово
    ``catalog`` внутри другого пути, в строке запроса или на чужом адресе —
    не раздел.
    """
    try:
        parts = urlsplit(link.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if host and host != _OWN_DOMAIN and not host.endswith(f".{_OWN_DOMAIN}"):
        return None
    segments = [unquote(segment).strip() for segment in parts.path.split("/")]
    segments = [segment for segment in segments if segment]
    if segments[:1] and segments[0] in _LANGUAGE_PREFIXES:
        segments = segments[1:]
    if len(segments) < 2 or segments[0] != "catalog":
        return None
    if segments[1] in _NOT_CATEGORIES:
        return None
    return "/".join(segments[1:])


def _name_from_slug(slug: str, code: str) -> str:
    """Название для карточки без названия: адрес страницы без кода товара."""
    words = [word for word in slug.split("-") if word]
    if words and words[-1] == code:
        words = words[:-1]
    return " ".join(words)


def build_product(item: dict, category_slug: str) -> ScrapedProduct | None:
    """Товар из записи ``items``; ``None``, если его не опознать."""
    code = item.get("product_code")
    name = " ".join(str(item.get("name") or "").split())
    slug = str(item.get("slug") or "").strip().strip("/")
    if code in (None, "") or isinstance(code, bool) or not slug:
        return None
    if not name:
        # На сайте встречается карточка без названия (код, цена и адрес есть,
        # `name: null` — замер 2026-11-13, раздел посуды). Это товар сайта, а не
        # сбой разбора: отвергнуть запись значило бы каждую ночь объявлять
        # неполным весь каталог из-за одной битой карточки. Название берётся из
        # адреса страницы — другого у сайта нет.
        name = _name_from_slug(slug, str(code).strip())
        if not name:
            return None
        log.warning("bakuelectronics_item_without_name", product_code=str(code), slug=slug[:120])

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

    async def _menu_sections(self) -> tuple[list[str], dict[str, list[str]]]:
        """Дерево разделов из меню сайта и ссылки, похожие на подразделы.

        Дерево — каждый путь и все его начала, в порядке меню, без повторов.
        Уровень, на который в меню нет собственной ссылки, но есть ссылки
        глубже, в дерево входит: иначе замена нерабочего раздела подразделами
        теряла бы целые ветки.

        Второе значение — пути из одного сегмента без собственных подпутей,
        чьё имя стоит глубже в другом пути, и эти глубокие пути. Кто из них
        верхний раздел, а кто ссылка с потерянным родителем, по меню не
        решить: это решает ``discover_categories``, спросив сайт.
        """
        response = await self.fetch(
            self.menu_url, headers={"Accept": "application/json", "lang": "az"}
        )
        try:
            menu = response.json()
        except ValueError as exc:
            raise PageFetchError("catalog menu is not readable") from exc
        if not isinstance(menu, list):
            raise PageFetchError("catalog menu is not a list of groups")

        paths: list[str] = []
        not_sections: list[str] = []
        empty_groups: list[str] = []
        section_links = 0

        def walk(node: object) -> None:
            nonlocal section_links
            if isinstance(node, dict):
                link = node.get("link")
                if isinstance(link, str) and link.strip():
                    section = section_from_menu_link(link)
                    if section is None:
                        not_sections.append(link)
                    else:
                        section_links += 1
                        if section not in paths:
                            paths.append(section)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        for group in menu:
            before = section_links
            walk(group)
            if section_links == before and isinstance(group, dict):
                empty_groups.append(str(group.get("slug") or group.get("title") or "?"))

        sections: list[str] = []
        for path in paths:
            segments = path.split("/")
            for depth in range(1, len(segments) + 1):
                node = "/".join(segments[:depth])
                if node not in sections:
                    sections.append(node)

        # «x» при «a/x/...» и без своих «x/...»: возможно, потерян родитель.
        own_branches = {path.split("/")[0] for path in paths if "/" in path}
        namesakes: dict[str, list[str]] = {}
        for path in paths:
            if "/" in path or path in own_branches:
                continue
            twins = [node for node in sections if "/" in node and node.rsplit("/", 1)[1] == path]
            if twins:
                namesakes[path] = twins

        # Меню сверить не с чем: что пропущено, видно только здесь.
        log.info(
            "bakuelectronics_menu_read",
            groups=len(menu),
            sections=len(sections),
            top_level=len({section.split("/")[0] for section in sections}),
            groups_without_sections=empty_groups,
            links_not_to_sections=len(not_sections),
            links_not_to_sections_sample=not_sections[:10],
            top_level_named_like_a_subsection=sorted(namesakes),
        )
        return sections, namesakes

    async def _same_listing(self, first: str, second: str) -> bool:
        """Отдаёт ли сайт по двум путям один и тот же список товаров.

        Сравниваются число товаров и коды на первой странице. Не прочиталось
        хоть одно — «не тот же»: лишний раздел даёт повтор товаров, а
        выброшенный — потерю.
        """
        try:
            one = await self._listing_page(first, 1)
            two = await self._listing_page(second, 1)
        except PageFetchError:
            return False

        def codes(listing: dict) -> list[object]:
            return [item.get("product_code") for item in listing["items"] if isinstance(item, dict)]

        total = one.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
            return False
        # Пустая первая страница при названном числе товаров — сломанный
        # ответ, а не совпадение.
        return total == two.get("total") and bool(codes(one)) and codes(one) == codes(two)

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
        подразделами — и так вниз по дереву меню.

        Полнота — относительно меню: раздел, которого в меню нет, сюда не
        попадёт, и отказа не будет. Что в меню не разобрано, пишет в журнал
        ``bakuelectronics_menu_read``.
        """
        links, namesakes = await self._menu_sections()

        # Ссылка с потерянным родителем — не верхний раздел, а второй адрес
        # подраздела: оставить её значило бы читать те же товары дважды.
        # Убирается, только когда сайт сам подтвердил, что список тот же.
        for name, twins in namesakes.items():
            for twin in twins:
                if await self._same_listing(name, twin):
                    log.warning(
                        "bakuelectronics_menu_link_is_a_subsection",
                        link=name,
                        same_listing_as=twin,
                    )
                    links.remove(name)
                    break

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
