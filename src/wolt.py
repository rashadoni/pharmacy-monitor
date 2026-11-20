"""Аптеки на Wolt: сбор каталога и сравнение цен между ними.

Отдельный раздел аптечного дашборда (решение владельца 2026-11-19). Аптеки
Wolt — не сайты профиля (``site_profile``): их товары лежат в своих таблицах
(``wolt_items``, ``wolt_scrapes``), и ничто из основного пути — доверенные
цены, эпоха каталога, сопоставление pharmonline, алерты, health — их не
читает. Раздел только показывает, у какой из аптек Wolt один и тот же товар
дешевле. Утреннее письмо (с 2026-11-20) берёт эти же строки готовыми —
``comparison`` — разделом и вторым файлом (``notifications._wolt_report``,
``wolt_export``) и ничего в таблицы Wolt не пишет.

Источник — открытый API, которым пользуется сама страница заведения на
wolt.com (robots.txt Wolt ничего не запрещает, вход не нужен, прокси не
нужен):

* ``/assortment`` — дерево разделов заведения;
* ``/assortment/categories/slug/<раздел>`` — товары раздела страницами по 50,
  следующая страница — ``page_token`` из ``metadata.next_page_token``.

Цена в API — в гяпиках (``300`` — 3,00 AZN). Штрихкодов Wolt у аптек не
отдаёт, поэтому одинаковые товары узнаются по названию — теми же правилами,
что у сопоставления сайтов (``matcher``), но строже: только одинаковое
нормализованное название, доза и фасовка, и ни одна проверка «это разные
товары» не сработала.

Сбор аптеки — всё или ничего: страница, не пришедшая после повторов, роняет
сбор этой аптеки, и её каталог в базе остаётся прежним. Каталог, вдвое
меньший прошлого полного, тоже не записывается: сломанный ответ не должен
превратить полкаталога в «пропавшие».
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src import matcher, site_profile, storage
from src._time import utcnow
from src.normalize import (
    extract_dosage,
    extract_form,
    extract_pack_size,
    fold_spelling,
    normalize_name,
)

log = structlog.get_logger()

# Заведения Wolt, которые сравниваются: слаг из адреса wolt.com → подпись.
# Порядок — порядок колонок на странице.
VENUES: dict[str, str] = {
    "aptek5": "Buta Aptek — Kral",
    "aloe-aptek-ag-sheher": "Aloe Aptek — Ağ Şəhər",
    "kanon-tarqovaya": "Kanon Aptek — Torgovaya",
}

API_BASE = "https://consumer-api.wolt.com/consumer-api/consumer-assortment/v1/venues/slug/"
VENUE_PAGE = "https://wolt.com/az/aze/baku/venue/"
LANGUAGE = "az"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
# Пауза между запросами: полный сбор трёх аптек — около 300 запросов.
REQUEST_DELAY_SEC = 0.7
ATTEMPTS = 3
RETRY_DELAY_SEC = 5.0
# Страниц в одном разделе больше этого — что-то не так с подачей страниц.
MAX_PAGES_PER_CATEGORY = 200
# Когда идёт ежедневный сбор — час по Баку (UTC+4, без перехода на летнее
# время). Страница называет его клиенту; tests/test_wolt.py сверяет с таймером.
DAILY_SYNC_UTC_HOUR = 6
BAKU_UTC_OFFSET_HOURS = 4
# Каталог меньше этой доли прошлого полного не записывается.
MIN_SHARE_OF_PREVIOUS = 0.5
# Цены одного «товара» в разных аптеках расходятся больше чем во столько раз —
# почти всегда разная фасовка или разный товар под похожим названием (замер
# 2026-11-19: «Viaqra 100 mq N4» 9,43 и 68,03). Такая строка не показывается,
# а считается сомнительной.
MAX_PRICE_RATIO = 3.0
# «Həb», «həblər», «həbi» — таблетки по-азербайджански. Общий словарь форм
# (normalize._FORMS) этого слова не знает, а аптеки Wolt пишут его часто;
# добавлять его туда — значит менять сопоставление сайтов, поэтому только здесь.
_TABLET_WORD_RE = re.compile(r"(?<![a-zəöüğışç])h[əe]b(?:l[əe]r|i)?(?![a-zəöüğışç])", re.I)


class WoltScrapeError(RuntimeError):
    """Сбор аптеки неполон — её каталог в базе не меняется."""


@dataclass(frozen=True)
class WoltOffer:
    item_id: str
    name: str
    price: float
    original_price: float | None
    in_stock: bool


def venue_url(venue: str) -> str:
    return VENUE_PAGE + venue


def item_url(venue: str, item_id: str) -> str:
    # Wolt сам переводит этот адрес на страницу товара.
    return f"{venue_url(venue)}/itemid-{item_id}"


def _money(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value <= 0:
        return None
    return round(value / 100, 2)


def parse_item(raw: dict) -> WoltOffer | None:
    """Товар из ответа API; без номера, названия или цены — None."""
    item_id = raw.get("id")
    name = raw.get("name")
    price = _money(raw.get("price"))
    if not isinstance(item_id, str) or not item_id.strip():
        return None
    if not isinstance(name, str) or not name.strip() or price is None:
        return None
    original = _money(raw.get("original_price"))
    balance = raw.get("purchasable_balance")
    in_stock = not raw.get("disabled_info") and not (
        isinstance(balance, int) and not isinstance(balance, bool) and balance <= 0
    )
    return WoltOffer(
        item_id=item_id.strip(),
        name=" ".join(name.split())[:500],
        price=price,
        original_price=original if original is not None and original > price else None,
        in_stock=in_stock,
    )


def _leaf_category_slugs(categories: Iterable[dict]) -> list[str]:
    slugs: list[str] = []
    for category in categories:
        children = category.get("subcategories") or []
        if children:
            slugs.extend(_leaf_category_slugs(children))
            continue
        slug = category.get("slug")
        if isinstance(slug, str) and slug:
            slugs.append(slug)
    # Раздел может стоять в дереве дважды — читаем один раз.
    return list(dict.fromkeys(slugs))


def _get_json(
    client: httpx.Client,
    url: str,
    sleep: Callable[[float], None],
    params: dict[str, str] | None = None,
) -> dict:
    last: str = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = client.get(url, params={"language": LANGUAGE, **(params or {})})
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    return data
                last = "not_an_object"
            else:
                last = f"http_{response.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            last = type(exc).__name__
        if attempt < ATTEMPTS:
            sleep(RETRY_DELAY_SEC * attempt)
    raise WoltScrapeError(f"request_failed:{last}")


def fetch_venue(
    venue: str,
    *,
    client: httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
) -> list[WoltOffer]:
    """Весь каталог аптеки; неполный проход — WoltScrapeError."""
    base = API_BASE + quote(venue, safe="-")
    tree = _get_json(client, f"{base}/assortment", sleep)
    slugs = _leaf_category_slugs(tree.get("categories") or [])
    if not slugs:
        raise WoltScrapeError("no_categories")
    offers: dict[str, WoltOffer] = {}
    for slug in slugs:
        token: str | None = None
        seen_tokens: set[str] = set()
        for _page in range(MAX_PAGES_PER_CATEGORY):
            sleep(REQUEST_DELAY_SEC)
            url = f"{base}/assortment/categories/slug/{quote(slug, safe='-')}"
            # Номер страницы Wolt, а не учётные данные; параметром, а не строкой.
            page = _get_json(client, url, sleep, {"page_token": token} if token else None)
            items = page.get("items")
            if not isinstance(items, list):
                raise WoltScrapeError("items_missing")
            for raw in items:
                if isinstance(raw, dict) and (offer := parse_item(raw)) is not None:
                    offers.setdefault(offer.item_id, offer)
            metadata = page.get("metadata")
            token = metadata.get("next_page_token") if isinstance(metadata, dict) else None
            if not token:
                break
            if not isinstance(token, str) or token in seen_tokens:
                # Тот же токен второй раз — страницы пошли по кругу.
                raise WoltScrapeError("page_token_invalid")
            seen_tokens.add(token)
        else:
            raise WoltScrapeError("too_many_pages")
    if not offers:
        raise WoltScrapeError("empty_catalog")
    return list(offers.values())


def _previous_full_count(session: Session, venue: str) -> int | None:
    return session.scalar(
        select(storage.WoltScrape.items)
        .where(storage.WoltScrape.venue == venue, storage.WoltScrape.status == "ok")
        .order_by(storage.WoltScrape.id.desc())
        .limit(1)
    )


def store_catalog(
    session: Session,
    venue: str,
    offers: list[WoltOffer],
    *,
    now: datetime | None = None,
    allow_shrink: bool = False,
) -> dict[str, int]:
    """Записать полный каталог аптеки. Commit — за вызывающим.

    ``allow_shrink`` — человек проверил, что аптека и правда убрала больше
    половины товаров (RUNBOOK «Аптеки на Wolt»).
    """
    now = now or utcnow()
    previous = _previous_full_count(session, venue)
    if not allow_shrink and previous and len(offers) < previous * MIN_SHARE_OF_PREVIOUS:
        raise WoltScrapeError(f"catalog_shrank:{len(offers)}<{previous}*{MIN_SHARE_OF_PREVIOUS}")
    existing = {
        row.item_id: row
        for row in session.scalars(select(storage.WoltItem).where(storage.WoltItem.venue == venue))
    }
    counts = {"new": 0, "price_changed": 0, "gone": 0}
    seen: set[str] = set()

    def remember(offer: WoltOffer) -> None:
        # Журнал цен: строка на каждое изменение цены, скидки или наличия.
        session.add(
            storage.WoltPriceChange(
                venue=venue,
                item_id=offer.item_id,
                price=offer.price,
                original_price=offer.original_price,
                in_stock=offer.in_stock,
                at=now,
            )
        )

    for offer in offers:
        seen.add(offer.item_id)
        row = existing.get(offer.item_id)
        if row is None:
            remember(offer)
            session.add(
                storage.WoltItem(
                    venue=venue,
                    item_id=offer.item_id,
                    name=offer.name,
                    price=offer.price,
                    original_price=offer.original_price,
                    in_stock=offer.in_stock,
                    in_catalog=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            counts["new"] += 1
            continue
        if (row.price, row.original_price, row.in_stock) != (
            offer.price,
            offer.original_price,
            offer.in_stock,
        ):
            remember(offer)
        if row.price != offer.price:
            row.price = offer.price
            row.price_changed_at = now
            counts["price_changed"] += 1
        row.name = offer.name
        row.original_price = offer.original_price
        row.in_stock = offer.in_stock
        row.in_catalog = True
        row.last_seen_at = now
    for item_id, row in existing.items():
        if item_id not in seen and row.in_catalog:
            row.in_catalog = False
            counts["gone"] += 1
    return counts


# Попытка дольше этого — процесс убит (таймаут юнита, перезагрузка) и
# закончить её некому. Юнит даёт сбору 30 минут.
INTERRUPTED_AFTER = timedelta(hours=2)


def _close_interrupted_attempts(session: Session, venue: str) -> None:
    """Попытки, которые убитый процесс оставил «running», — неудачные."""
    cutoff = utcnow() - INTERRUPTED_AFTER
    stale = list(
        session.scalars(
            select(storage.WoltScrape).where(
                storage.WoltScrape.venue == venue,
                storage.WoltScrape.status == "running",
                storage.WoltScrape.started_at < cutoff,
            )
        )
    )
    for attempt in stale:
        attempt.status = "failed"
        attempt.error = "interrupted"
        attempt.finished_at = utcnow()
    if stale:
        session.commit()
        log.warning("wolt_interrupted_attempts_closed", venue=venue, count=len(stale))


def sync_venue(
    session: Session,
    venue: str,
    *,
    client: httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
    allow_shrink: bool = False,
) -> storage.WoltScrape:
    """Собрать и записать одну аптеку; исход — строка ``wolt_scrapes``.

    Любой сбой — сеть, неполный каталог, отказ базы на записи — оставляет
    прежний каталог аптеки и строку попытки со статусом failed. Исключение
    наружу не летит: следующая аптека собирается своим чередом.
    """
    if venue not in VENUES:
        raise ValueError(f"unknown Wolt venue: {venue!r}")
    _close_interrupted_attempts(session, venue)
    attempt = storage.WoltScrape(venue=venue, started_at=utcnow(), status="running")
    session.add(attempt)
    session.commit()
    attempt_id = attempt.id
    try:
        offers = fetch_venue(venue, client=client, sleep=sleep)
        counts = store_catalog(session, venue, offers, allow_shrink=allow_shrink)
        # Запись каталога уходит в базу только здесь (сессии без autoflush):
        # её сбой — такой же сбой сбора, как сетевой.
        attempt.status = "ok"
        attempt.items = len(offers)
        attempt.finished_at = utcnow()
        session.commit()
    except Exception as exc:
        session.rollback()
        # Текст ошибки базы может нести данные строки — пишем только класс.
        reason = str(exc) if isinstance(exc, WoltScrapeError) else type(exc).__name__
        failed = session.get(storage.WoltScrape, attempt_id)
        failed.status = "failed"
        failed.items = None
        failed.error = reason[:500]
        failed.finished_at = utcnow()
        session.commit()
        log.warning("wolt_venue_failed", venue=venue, reason=failed.error)
        return failed
    log.info("wolt_venue_synced", venue=venue, items=len(offers), **counts)
    return attempt


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30.0,
        follow_redirects=False,
    )


# ─── Сравнение ───────────────────────────────────────────────────────────────


def _form(raw_name: str) -> str | None:
    form = extract_form(raw_name)
    if form is None and _TABLET_WORD_RE.search(raw_name or ""):
        return "tablet"
    return form


def _conflicting_form(raw_a: str, raw_b: str) -> bool:
    form_a, form_b = _form(raw_a), _form(raw_b)
    if form_a is None or form_b is None or form_a == form_b:
        return False
    return frozenset((form_a, form_b)) not in matcher._COMPATIBLE_FORMS


def _different_products(a: storage.Product, b: storage.Product) -> bool:
    """Те же проверки «это разные товары», что у сопоставления сайтов."""
    an, bn = a.name_normalized or "", b.name_normalized or ""
    ar, br = a.name or "", b.name or ""
    return (
        _conflicting_form(ar, br)
        or matcher._hard_conflict(a, b)
        or matcher._pairwise_spec_conflict(a, b)
        or matcher._has_conflicting_form(ar, br)
        or matcher._has_conflicting_route(ar, br)
        or matcher._has_conflicting_modifier(an, bn)
        or matcher._has_conflicting_series_number(an, bn)
        or matcher._has_conflicting_variant_tokens(an, bn)
        or matcher._has_conflicting_gender(an, bn)
    )


def _as_product(row: storage.WoltItem) -> storage.Product:
    # Объект без сессии: только чтобы проверки матчера прочли название.
    normalized = normalize_name(row.name)
    return storage.Product(
        site=row.venue,
        external_id=row.item_id,
        name=row.name,
        name_normalized=normalized,
        dosage=extract_dosage(row.name),
        pack_size=extract_pack_size(row.name),
    )


def group_items(
    rows: Iterable[storage.WoltItem], *, optional: frozenset[str] = frozenset()
) -> list[dict[str, storage.WoltItem]]:
    """Один и тот же товар в разных аптеках: {аптека: строка}, минимум две.

    Ключ — свёрнутое нормализованное название, доза и фасовка. У аптеки,
    где под одним ключом лежат два товара, место в группе пустое: какой из
    них тот самый, по названию не решить. Группа, где хоть одна пара
    признана разными товарами, не показывается вовсе.

    ``optional`` — колонки, которые пристёгиваются к группе остальных, но
    сами её не ломают (владелец): спорная строка такой колонки выпадает из
    группы, а группа аптек Wolt остаётся какой была бы без неё.
    """
    buckets: dict[tuple, dict[str, list[tuple[storage.WoltItem, storage.Product]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        product = _as_product(row)
        if not product.name_normalized:
            continue
        key = (fold_spelling(product.name_normalized), product.dosage, product.pack_size)
        buckets[key][row.venue].append((row, product))
    groups: list[dict[str, storage.WoltItem]] = []
    for by_venue in buckets.values():
        picked = {venue: found[0] for venue, found in by_venue.items() if len(found) == 1}
        core = {venue: pair for venue, pair in picked.items() if venue not in optional}
        if any(
            _different_products(a[1], b[1]) for a, b in itertools.combinations(core.values(), 2)
        ):
            continue
        group = dict(core)
        for venue, pair in picked.items():
            if venue in optional and not any(
                _different_products(pair[1], other[1]) for other in group.values()
            ):
                group[venue] = pair
        if len(group) < 2:
            continue
        groups.append({venue: pair[0] for venue, pair in group.items()})
    return groups


# ─── Владелец: сайт клиента первой колонкой ─────────────────────────────────
# Решение владельца 2026-11-20: рядом с аптеками Wolt — его собственные цены,
# первой колонкой. На Wolt своего заведения у клиента нет, поэтому цена — с
# его сайта, из ночного сбора (`products` и `price_snapshots`, только чтение):
# действующая (со скидкой, если она есть) по тому же правилу, что основная
# страница сравнения (`comparison_rows`): в эпоху доверенного каталога —
# только проверенные цены. Товары — те, что сайт показывал последние
# OWNER_FRESH_DAYS дней, без мёртвых ссылок, изолированных и архивных
# карточек; наличие — как пишет сайт. Основной путь таблицы Wolt по-прежнему
# не читает (кроме утреннего письма — готовыми строками `comparison`, с
# 2026-11-20); зависимость односторонняя.
OWNER = site_profile.CLIENT_SITE
OWNER_TITLE = site_profile.SITE_TITLES[OWNER]
OWNER_URL = "https://pharmonline.az"
OWNER_FRESH_DAYS = 3
# Колонки страницы: владелец, потом аптеки Wolt.
COLUMNS: tuple[str, ...] = (OWNER, *VENUES)


def _owner_items(session: Session) -> tuple[list[storage.WoltItem], dict[str, str]]:
    """Товары сайта клиента в виде строк Wolt (без сессии) и их адреса."""
    from src import product_policy
    from src.comparison_rows import _trusted_snapshot_lineage_available

    cutoff = utcnow() - timedelta(days=OWNER_FRESH_DAYS)
    products = list(
        session.execute(
            select(
                storage.Product.id,
                storage.Product.name,
                storage.Product.url,
                storage.Product.offer_availability_status,
            ).where(
                storage.Product.site == OWNER,
                storage.Product.tenant_id == 1,
                storage.Product.last_seen_at >= cutoff,
                storage.Product.url_dead_at.is_(None),
                product_policy.source_identity_eligible_sql(storage.Product),
            )
        )
    )
    prices = storage.latest_prices_per_product(
        session,
        [row.id for row in products],
        financially_eligible_only=_trusted_snapshot_lineage_available(session),
    )
    rows: list[storage.WoltItem] = []
    urls: dict[str, str] = {}
    for product in products:
        regular, discount, _on_sale = prices.get(product.id, (None, None, False))
        price = discount or regular
        if not price or price <= 0 or not product.name:
            continue
        regular = regular if discount and regular else None
        item_id = str(product.id)
        rows.append(
            storage.WoltItem(
                venue=OWNER,
                item_id=item_id,
                name=" ".join(product.name.split()),
                price=round(float(price), 2),
                original_price=round(float(regular), 2) if regular and regular > price else None,
                in_stock=product.offer_availability_status != product_policy.OFFER_OUT_OF_STOCK,
                in_catalog=True,
            )
        )
        urls[item_id] = product.url
    return rows, urls


# Строки пересчитываются, только когда сменились данные: сопоставление ~20 тыс.
# товаров идёт секунды, а страница спрашивает его на каждый ввод. Свой у
# каждого процесса API. Пока один запрос пересобирает, остальные получают
# прошлый результат, а не ждут.
_rows_cache: dict[tuple, tuple[list[dict], dict[str, int], list[dict]]] = {}
_last_rows: tuple[list[dict], dict[str, int], list[dict]] | None = None
_cache_lock = threading.Lock()
_build_lock = threading.Lock()


def _cache_key(session: Session) -> tuple:
    wolt = tuple(
        session.execute(
            select(storage.WoltScrape.venue, func.max(storage.WoltScrape.id))
            .where(storage.WoltScrape.status == "ok")
            .group_by(storage.WoltScrape.venue)
            .order_by(storage.WoltScrape.venue)
        ).all()
    )
    # Цены владельца: последняя записанная цена (сбор пишет их пачками по ходу
    # прогона) и конец последнего прогона (меняет доверенную эпоху); срок
    # свежести товаров — сутки.
    owner = (
        session.scalar(select(func.max(storage.PriceSnapshot.id))),
        session.scalar(select(func.max(storage.Run.finished_at))),
        utcnow().date(),
    )
    return wolt, owner


def _pair_summary(groups: list[dict[str, storage.WoltItem]]) -> list[dict]:
    """Каждая пара аптек: сколько общих товаров и у кого дешевле.

    Считаются только товары, которые есть в наличии у обеих аптек пары, —
    то же, что сравнивает таблица. Это ответ на вопрос «почему разных цен так
    мало»: у пары может быть тысяча общих товаров и ни одной разной цены.
    """
    summary = []
    for a, b in itertools.combinations(COLUMNS, 2):
        common = same = a_cheaper = b_cheaper = 0
        for group in groups:
            ia, ib = group.get(a), group.get(b)
            if ia is None or ib is None or not (ia.in_stock and ib.in_stock):
                continue
            common += 1
            if ia.price == ib.price:
                same += 1
            elif ia.price < ib.price:
                a_cheaper += 1
            else:
                b_cheaper += 1
        summary.append(
            {
                "a": a,
                "b": b,
                "common": common,
                "same_price": same,
                "a_cheaper": a_cheaper,
                "b_cheaper": b_cheaper,
            }
        )
    return summary


def _price_range(group: dict[str, storage.WoltItem]) -> tuple[float, float]:
    # Сравниваются цены того, что можно купить; товар «нет в наличии»
    # показывается, но в разницу и «дешевле всех» не идёт.
    prices = [item.price for item in group.values() if item.in_stock]
    if len(prices) < 2:
        low = min(prices or [item.price for item in group.values()])
        return low, low
    return min(prices), max(prices)


def _build_rows(session: Session) -> tuple[list[dict], dict[str, int], list[dict]]:
    owner_items, owner_urls = _owner_items(session)
    wolt_items = list(
        session.scalars(
            select(storage.WoltItem).where(
                storage.WoltItem.in_catalog.is_(True),
                storage.WoltItem.venue.in_(list(VENUES)),
            )
        )
    )
    totals = {
        "rows": 0,
        "differ": 0,
        "all_venues": 0,
        "suspicious": 0,
        "owner_items": len(owner_items),
    }
    rows: list[dict] = []
    shown_groups: list[dict[str, storage.WoltItem]] = []
    for group in group_items(owner_items + wolt_items, optional=frozenset({OWNER})):
        low, high = _price_range(group)
        if high > low * MAX_PRICE_RATIO and OWNER in group:
            # Цена владельца втрое от остальных — его карточка, скорее всего,
            # не тот товар; строку аптек Wolt она не прячет.
            group = {venue: item for venue, item in group.items() if venue != OWNER}
            if len(group) < 2:
                totals["suspicious"] += 1
                continue
            low, high = _price_range(group)
        if high > low * MAX_PRICE_RATIO:
            totals["suspicious"] += 1
            continue
        shown_groups.append(group)
        totals["rows"] += 1
        totals["differ"] += low != high
        totals["all_venues"] += len(group) == len(COLUMNS)
        title = min(group.values(), key=lambda item: (len(item.name), item.venue)).name
        rows.append(
            {
                "name": title,
                "prices": {
                    venue: {
                        "price": item.price,
                        "original_price": item.original_price,
                        "in_stock": item.in_stock,
                        "name": item.name,
                        "url": owner_urls[item.item_id]
                        if venue == OWNER
                        else item_url(venue, item.item_id),
                    }
                    for venue, item in group.items()
                },
                "min_price": low,
                "max_price": high,
                "spread_pct": round((high - low) / low * 100, 1) if low else 0.0,
                "cheapest": sorted(
                    v for v, item in group.items() if item.in_stock and item.price == low
                )
                if low != high
                else [],
                "_search": " ".join(
                    part
                    for item in group.values()
                    for part in (
                        fold_spelling(normalize_name(item.name)),
                        fold_spelling(item.name.lower()),
                    )
                ),
            }
        )
    rows.sort(key=lambda r: (-r["spread_pct"], r["name"].lower()))
    return rows, totals, _pair_summary(shown_groups)


def comparison(
    session: Session, *, search: str | None = None, only_diff: bool = False, wait: bool = False
) -> dict:
    """Строки сравнения аптек Wolt, самые большие разницы — первыми.

    `wait` — не отдавать прошлую сборку, пока другой запрос пересобирает, а
    дождаться своей: так берёт строки утреннее письмо (кнопка тестового письма
    идёт через процесс API, где кэш общий со страницей).
    """
    global _last_rows
    key = _cache_key(session)
    with _cache_lock:
        cached = _rows_cache.get(key)
        stale = _last_rows
    if cached is None:
        # Строит один запрос; остальные получают прошлый результат, а ждут
        # только тогда, когда прошлого ещё нет (первый запрос процесса).
        if _build_lock.acquire(blocking=stale is None or wait):
            try:
                with _cache_lock:
                    cached = _rows_cache.get(key)
                if cached is None:
                    cached = _build_rows(session)
                    with _cache_lock:
                        _rows_cache.clear()
                        _rows_cache[key] = cached
                        _last_rows = cached
            finally:
                _build_lock.release()
        else:
            cached = stale
    rows, totals, pairs = cached
    needle = ""
    if search and search.strip():
        # normalize_name выбрасывает дозу и форму («100 mq» → пусто): тогда
        # ищем по свёрнутому тексту как есть.
        needle = fold_spelling(normalize_name(search)) or fold_spelling(search.lower())
    shown = [
        {k: v for k, v in row.items() if k != "_search"}
        for row in rows
        if not (only_diff and row["min_price"] == row["max_price"])
        and (not needle or needle in row["_search"])
    ]
    venues = venue_status(session)
    # На карточке владельца — столько товаров, сколько пошло в сравнение.
    venues[0]["items"] = totals["owner_items"]
    return {
        "venues": venues,
        "rows": shown,
        "totals": {k: v for k, v in totals.items() if k != "owner_items"},
        "pairs": pairs,
        "daily_sync_baku": f"{(DAILY_SYNC_UTC_HOUR + BAKU_UTC_OFFSET_HOURS) % 24:02d}:00",
    }


def _owner_status(session: Session) -> dict:
    cutoff = utcnow() - timedelta(days=OWNER_FRESH_DAYS)
    fresh = (
        storage.Product.site == OWNER,
        storage.Product.tenant_id == 1,
        storage.Product.last_seen_at >= cutoff,
    )
    items = session.scalar(select(func.count()).select_from(storage.Product).where(*fresh))
    last_seen = session.scalar(select(func.max(storage.Product.last_seen_at)).where(*fresh))
    return {
        "venue": OWNER,
        "title": OWNER_TITLE,
        "url": OWNER_URL,
        "items": items or 0,
        "last_status": "ok" if last_seen else None,
        "last_ok_at": last_seen.isoformat() if last_seen else None,
        "is_owner": True,
    }


def venue_status(session: Session) -> list[dict]:
    status = [_owner_status(session)]
    for venue, title in VENUES.items():
        last = session.scalar(
            select(storage.WoltScrape)
            .where(storage.WoltScrape.venue == venue, storage.WoltScrape.finished_at.is_not(None))
            .order_by(storage.WoltScrape.id.desc())
            .limit(1)
        )
        last_ok = session.scalar(
            select(func.max(storage.WoltScrape.finished_at)).where(
                storage.WoltScrape.venue == venue, storage.WoltScrape.status == "ok"
            )
        )
        items = session.scalar(
            select(func.count())
            .select_from(storage.WoltItem)
            .where(storage.WoltItem.venue == venue, storage.WoltItem.in_catalog.is_(True))
        )
        status.append(
            {
                "venue": venue,
                "title": title,
                "url": venue_url(venue),
                "items": items or 0,
                "last_status": last.status if last else None,
                "last_ok_at": last_ok.isoformat() if last_ok else None,
                "is_owner": False,
            }
        )
    return status
