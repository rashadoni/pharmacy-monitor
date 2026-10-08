"""Товар aloe узнаётся по номеру товара на сайте, а не по слагу.

Сайт aloe.az сам даёт один слаг нескольким разным товарам — другой завод,
страна, цена (в карте сайта 2026-10-08 так у 581 слага). Товар в базе искался по
паре (сайт, слаг): такие товары записывались одной строкой, а внутри одного
раздела второй товар сборщик отбрасывал. Адрес со слагом на сайте открывает тот
товар, у кого меньше номер, — даже если его уже нет в продаже.

Теперь идентификатор — номер товара (`id` в листинге, «Məhsul kodu» на странице),
адрес — `https://aloe.az/{номер}/#{слаг}`. Строки, записанные под слагом, запись
сбора переводит на номер сама, когда снова видит товар.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import main, match_actions, storage
from src.main import persist_results
from src.scrapers import aloe
from src.scrapers.aloe import (
    AloeScraper,
    aloe_product_number,
    aloe_product_number_from_detail_html,
    aloe_product_url,
    aloe_products_from_listing_html,
    aloe_slug_from_url,
)
from src.scrapers.base import RouteStatus, ScrapedProduct, ScrapeResult

_FIXTURES = Path(__file__).parent / "fixtures"
# Миграция включает правило на проде. До слова владельца она лежала в
# `migrations/pending/`, мимо цепочки Alembic.
_MIGRATION = next(
    path
    for folder in ("versions", "pending")
    if (
        path := Path(__file__).resolve().parents[1]
        / "migrations"
        / folder
        / "0024_aloe_product_numbers.py"
    ).exists()
)

# Живой пример 2026-10-08: один слаг, два завода, две страны, две цены.
_SLUG = "ceftriaxone-1-q"
_SINTEZ = {"id": 12058, "name": "Ceftriaxone 1 q", "slug": _SLUG, "price": 0.84}
_REYOUNG = {"id": 12224, "name": "Ceftriaxone 1 q", "slug": _SLUG, "price": 0.79}


def _migrate(connection, step: str) -> None:
    """Шаг миграции 0024 на этом соединении — тем же кодом, что и на проде."""
    spec = importlib.util.spec_from_file_location("aloe_product_numbers_migration", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, step)()


def _without_postgresql(reason: str) -> None:
    """Локально — пропуск. В CI пропуск молча убрал бы вариант с настоящей базой."""
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


@contextlib.contextmanager
def _postgresql_schema(*, switched_on: bool):
    """Своя схема на тест: общая база CI уже размечена миграциями."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        _without_postgresql("PostgreSQL DATABASE_URL is required")
    # `src.main` подгружает `.env`: тест не создаёт схемы в базе, которая не тестовая.
    if not (make_url(database_url).database or "").endswith("_test"):
        _without_postgresql("PostgreSQL DATABASE_URL must point at a *_test database")
    schema = f"aloe_number_{uuid4().hex[:12]}"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        storage.Base.metadata.create_all(engine)
        if switched_on:
            # Правило «по номеру» включает миграция: её предохранитель в базе.
            with engine.begin() as connection:
                _migrate(connection, "upgrade")
        with sessionmaker(engine, expire_on_commit=False, autoflush=False)() as session:
            yield session
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def db_session(request, db_session):
    """База с включённым правилом «по номеру» (в SQLite оно включено всегда)."""
    if request.param == "sqlite":
        yield db_session
        return
    with _postgresql_schema(switched_on=True) as session:
        yield session


@pytest.fixture
def switched_off_session():
    """PostgreSQL до миграции 0024: предохранителя нет — правило выключено."""
    with _postgresql_schema(switched_on=False) as session:
        yield session


def _listing(*items: dict, page: int = 1, last_page: int = 1) -> str:
    """Листинг aloe, как его отдаёт сайт: товары в потоке Next.js."""
    cards = ",".join(
        '["$","$L","%s",{"data":%s}]' % (item.get("id"), json.dumps({"code": "0", **item}))
        for item in items
    )
    payload = '15:[[%s],false,["$","$L",null,{"currentPage":%d,"lastPage":%d}]]' % (
        cards,
        page,
        last_page,
    )
    return f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>"


def _listed(*items: dict) -> list[ScrapedProduct]:
    return aloe_products_from_listing_html(_listing(*items), category_slug="dermanlar")


def _scraped(item: dict, *, country: str | None = None, brand: str | None = None) -> ScrapedProduct:
    """Товар из листинга; страна — уже названием, как после сверки с карточкой."""
    (product,) = _listed({**item, "brand": {"name": brand} if brand else None})
    product.manufacturer_country_raw = country
    product.country_source = "aloe_country_id_verified_detail" if country else None
    return product


async def _scan(monkeypatch, pages: dict[int, str]) -> list[ScrapedProduct]:
    """Обход раздела штатным сборщиком; сайт заменён заранее собранными страницами."""

    async def fetch(self, url: str) -> str:
        number = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
        return pages[number]

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fetch)
    scraper = AloeScraper(rate_limit_sec=0.001)
    return [product async for product in scraper.scrape_category("dermanlar")]


def _result(products: list[ScrapedProduct], *, complete: bool = True) -> ScrapeResult:
    """Результат сбора сайта: один раздел, пройденный целиком или нет."""
    return ScrapeResult(
        site="aloe",
        products=products,
        route_statuses={"dermanlar": RouteStatus(complete=complete)},
    )


def _write(db_session, products: list[ScrapedProduct], *, whole: bool = True) -> storage.Run:
    """Запись сбора: полного (он видит все товары сайта) или частичного (тик, раздел)."""
    run = storage.Run(status="running", catalog_scope="full" if whole else "partial")
    db_session.add(run)
    db_session.commit()
    written = persist_results(db_session, run, [_result(products)])
    # Счётчик — о собранном: товар, оставленный до полного сбора, тоже собран.
    assert written == len(products)
    db_session.commit()
    db_session.expire_all()
    return run


def _address(slug: str) -> str:
    """Адрес, который писал прежний сборщик."""
    return f"https://aloe.az/{slug}/"


def _legacy_row(
    db_session,
    slug: str,
    *,
    external_id: str | None = None,
    price: float | None = None,
    country: str | None = None,
    brand: str | None = None,
    name: str = "Ceftriaxone 1 q",
) -> storage.Product:
    """Строка, записанная до перехода на номер: идентификатор — слаг."""
    row = storage.Product(
        site="aloe",
        external_id=slug[:100] if external_id is None else external_id,
        url=_address(slug),
        name=name,
        name_normalized=name.lower(),
        brand_verified=brand,
        manufacturer_country_code=country,
        country_resolution_status="resolved" if country else "unknown",
    )
    db_session.add(row)
    db_session.flush()
    if price is not None:
        run = storage.Run(status="ok")
        db_session.add(run)
        db_session.flush()
        db_session.add(storage.PriceSnapshot(run_id=run.id, product_id=row.id, price=price))
    db_session.commit()
    return row


def _rows(db_session) -> dict[str, int]:
    """Идентификатор → номер строки, по всем товарам aloe в базе."""
    return dict(
        db_session.execute(
            select(storage.Product.external_id, storage.Product.id).where(
                storage.Product.site == "aloe"
            )
        ).all()
    )


def _prices(db_session, row_id: int) -> list[float]:
    return list(
        db_session.scalars(
            select(storage.PriceSnapshot.price)
            .where(storage.PriceSnapshot.product_id == row_id)
            .order_by(storage.PriceSnapshot.id)
        )
    )


def _adoptions(logs: list[dict]) -> list[dict]:
    return [entry for entry in logs if entry["event"] == "aloe_product_numbers_adopted"]


# ─── Сборщик ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "number"),
    [
        (12058, "12058"),
        ("12058", "12058"),
        (" 12058 ", "12058"),
        (0, None),
        (-5, None),
        (True, None),
        (None, None),
        ("", None),
        ("0", None),
        ("12a", None),
        ("ceftriaxone-1-q", None),
        ("１２", None),
    ],
)
def test_product_number_is_a_positive_site_number(value: object, number: str | None) -> None:
    assert aloe_product_number(value) == number


def test_listing_names_two_products_of_one_slug_by_their_numbers() -> None:
    products = _listed(_SINTEZ, _REYOUNG)

    assert [(p.external_id, p.url, p.price) for p in products] == [
        ("12058", "https://aloe.az/12058/#ceftriaxone-1-q", 0.84),
        ("12224", "https://aloe.az/12224/#ceftriaxone-1-q", 0.79),
    ]
    # Номер прочитан у сайта: только такой товар запись сбора переводит на номер.
    assert all(p.identity_verified for p in products)


def test_listing_keeps_one_product_listed_twice_on_a_page() -> None:
    products = _listed(_SINTEZ, _SINTEZ)

    assert [p.external_id for p in products] == ["12058"]


async def test_scan_yields_both_products_of_a_slug_met_on_different_pages(monkeypatch) -> None:
    products = await _scan(
        monkeypatch,
        {1: _listing(_SINTEZ, last_page=2), 2: _listing(_REYOUNG, page=2, last_page=2)},
    )

    assert [(p.external_id, p.price) for p in products] == [("12058", 0.84), ("12224", 0.79)]


@pytest.mark.parametrize("slug", ["", "0", None])
def test_product_without_a_slug_is_named_by_its_number(slug: str | None) -> None:
    """Такому товару сайт пишет слаг «0» и сам ведёт на адрес с номером."""
    (product,) = _listed({"id": 30781, "name": "Aykutop 30 əd", "slug": slug, "price": 12.0})

    assert (product.external_id, product.url) == ("30781", "https://aloe.az/30781/")


@pytest.mark.parametrize("number", [0, None, "abc"])
def test_product_without_a_number_is_a_parse_failure(number: object) -> None:
    html = _listing({"id": number, "name": "Ceftriaxone 1 q", "slug": _SLUG, "price": 0.84})

    products, raw, parsed, failures = aloe._aloe_products_from_listing_html_with_stats(
        html, category_slug="dermanlar"
    )

    assert (products, raw, parsed, failures) == ([], 1, 0, 1)


def test_product_address_fits_the_column() -> None:
    width = storage.Product.__table__.c.url.type.length
    assert aloe._URL_MAX == width
    fits = "a" * (width - len("https://aloe.az/12058/#"))

    assert aloe_product_url("https://aloe.az", "12058", fits) == f"https://aloe.az/12058/#{fits}"
    # Слаг, с которым адрес не поместился бы, опускаем: номер открывает товар и без него.
    assert aloe_product_url("https://aloe.az", "12058", fits + "a") == "https://aloe.az/12058/"


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://aloe.az/12058/#ceftriaxone-1-q", "ceftriaxone-1-q"),
        ("https://aloe.az/ceftriaxone-1-q/", "ceftriaxone-1-q"),
        ("https://aloe.az/ceftriaxone-1-q", "ceftriaxone-1-q"),
        ("https://aloe.az/ru/ceftriaxone-1-q/?utm=1", "ceftriaxone-1-q"),
        ("https://aloe.az/12058/", ""),
        ("https://aloe.az/0/", ""),
        ("", ""),
        # После «#» в адресе со слагом — якорь страницы, а не слаг.
        ("https://aloe.az/ceftriaxone-1-q/#reviews", "ceftriaxone-1-q"),
        ("https://aloe.az/ceftriaxone-1-q/#:~:text=1%20q", "ceftriaxone-1-q"),
        ("https://aloe.az/ru/12058/#ceftriaxone-1-q", "ceftriaxone-1-q"),
    ],
)
def test_slug_is_read_from_both_address_forms(url: str, slug: str) -> None:
    assert aloe_slug_from_url(url) == slug


@pytest.mark.parametrize(
    ("fixture", "number"),
    [("aloe_product_rsc.html", "12019"), ("aloe_product_no_country.html", "34227")],
)
def test_product_page_tells_its_number(fixture: str, number: str) -> None:
    html = (_FIXTURES / fixture).read_text(encoding="utf-8")

    assert aloe_product_number_from_detail_html(html) == number


def test_page_without_a_product_tells_no_number() -> None:
    """Сайт отвечает 200 и на номер, которого нет: такую страницу не пишем."""
    listing = (_FIXTURES / "aloe_bestseller.html").read_text(encoding="utf-8")

    assert aloe_product_number_from_detail_html(listing) is None
    assert aloe_product_number_from_detail_html('"productId":12058 … "productId":12224') is None


class _Page:
    """Страница браузера, которая отдаёт заранее сохранённый HTML."""

    def __init__(self, html: str, title: str = "Ceftriaxone 1 q") -> None:
        self._html, self._title = html, title

    async def wait_for_load_state(self, *args, **kwargs) -> None:
        return None

    async def wait_for_selector(self, *args, **kwargs) -> None:
        return None

    async def query_selector(self, *args, **kwargs) -> None:
        return None

    async def title(self) -> str:
        return f"{self._title} | Aloe.az"

    async def content(self) -> str:
        return self._html

    async def close(self) -> None:
        return None


def _browser(monkeypatch, pages: dict[str, str]) -> AloeScraper:
    scraper = AloeScraper(rate_limit_sec=0.001)
    opened: list[str] = []

    async def new_page():
        return _Page("")

    async def goto(page, url: str) -> None:
        opened.append(url)
        page._html = pages[url]

    monkeypatch.setattr(scraper, "new_page", new_page)
    monkeypatch.setattr(scraper, "goto", goto)
    scraper.opened = opened  # type: ignore[attr-defined]
    return scraper


@pytest.mark.parametrize(
    ("pinned", "written"),
    [
        # Адрес со слагом открывает один из товаров слага: какой — говорит страница.
        ("https://aloe.az/ceftriaxone-1-q/", "https://aloe.az/12224/#ceftriaxone-1-q"),
        ("https://aloe.az/12224/#ceftriaxone-1-q", "https://aloe.az/12224/#ceftriaxone-1-q"),
        ("https://aloe.az/12224/", "https://aloe.az/12224/"),
    ],
)
async def test_product_page_is_named_by_the_number_it_shows(
    monkeypatch, pinned: str, written: str
) -> None:
    scraper = _browser(monkeypatch, {pinned: '"productId":12224,"inStock":true'})

    product = await scraper.scrape_product_page(pinned)

    assert product is not None
    assert (product.external_id, product.url, product.identity_verified) == ("12224", written, True)


async def test_page_pinned_by_number_keeps_the_slug_in_its_address(monkeypatch) -> None:
    """Иначе строка потеряла бы слаг и перестала бы находиться по адресу с сайта."""
    pinned = "https://aloe.az/12019/"
    html = (_FIXTURES / "aloe_product_rsc.html").read_text(encoding="utf-8")
    scraper = _browser(monkeypatch, {pinned: html})

    product = await scraper.scrape_product_page(pinned)

    assert product is not None
    assert (product.external_id, product.url) == (
        "12019",
        "https://aloe.az/12019/#nimesil-100-q-30-ed",
    )


async def test_product_page_without_a_number_is_not_written(monkeypatch) -> None:
    pinned = "https://aloe.az/99999999/"
    scraper = _browser(monkeypatch, {pinned: "<h1>Aloe+</h1>"})

    with capture_logs() as logs:
        product = await scraper.scrape_product_page(pinned)

    assert product is None
    assert [entry["event"] for entry in logs] == ["aloe_product_page_without_number"]


async def test_browser_mode_names_products_by_number_too(monkeypatch) -> None:
    """В вёрстке карточки номера нет: режим браузера читает тот же поток страницы."""
    base = "https://aloe.az/catalog/filters/?category_slug=dermanlar"
    scraper = _browser(
        monkeypatch,
        {base: _listing(_SINTEZ, _REYOUNG), f"{base}&page=2": _listing(page=2)},
    )
    monkeypatch.setenv("ALOE_SCRAPER_MODE", "playwright")

    products = [product async for product in scraper.scrape_category("dermanlar")]

    assert [(p.external_id, p.url) for p in products] == [
        ("12058", "https://aloe.az/12058/#ceftriaxone-1-q"),
        ("12224", "https://aloe.az/12224/#ceftriaxone-1-q"),
    ]
    assert scraper.opened == [base, f"{base}&page=2"]  # type: ignore[attr-defined]
    status = scraper._route_statuses["dermanlar"]
    assert (status.complete, status.raw_items, status.item_failures) == (True, 2, 0)


async def test_country_is_read_from_the_page_of_that_very_product(monkeypatch) -> None:
    """Раньше страну id сверяли по адресу со слагом — он открывает другой товар."""
    requested: list[str] = []

    async def fetch(self, url: str) -> str:
        requested.append(url)
        return '<span>Ölkə:</span><span>Çin</span> "inStock":true'

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fetch)
    scraper = AloeScraper(rate_limit_sec=0.001)
    (product,) = _listed({**_REYOUNG, "manufacturer_country": 87})

    await scraper._enrich_listing_country_ids([product])

    assert requested == ["https://aloe.az/12224/#ceftriaxone-1-q"]
    assert product.manufacturer_country_raw == "Çin"


# ─── Запись сбора ────────────────────────────────────────────────────────────


async def test_scan_writes_two_products_of_one_slug_as_two_rows(monkeypatch, db_session) -> None:
    products = await _scan(monkeypatch, {1: _listing(_SINTEZ, _REYOUNG)})

    _write(db_session, products)

    rows = _rows(db_session)
    assert set(rows) == {"12058", "12224"}
    assert _prices(db_session, rows["12058"]) == [0.84]
    assert _prices(db_session, rows["12224"]) == [0.79]


def test_row_written_under_the_slug_gets_the_number_in_place(db_session) -> None:
    row = _legacy_row(db_session, _SLUG, price=0.84)

    with capture_logs() as logs:
        _write(db_session, [_scraped(_SINTEZ)])

    assert _rows(db_session) == {"12058": row.id}
    stored = db_session.get(storage.Product, row.id)
    assert stored.url == "https://aloe.az/12058/#ceftriaxone-1-q"
    # Цена прежняя — история цен продолжается, новой записи нет.
    assert _prices(db_session, row.id) == [0.84]
    assert [
        (e["products"], e["shared_slugs"], e["left_for_other_country"]) for e in _adoptions(logs)
    ] == [(1, 0, 0)]


def test_cluster_place_and_price_history_stay_with_the_row(db_session) -> None:
    row = _legacy_row(db_session, _SLUG, price=0.9)
    match = storage.Match(canonical_name="Ceftriaxone 1 q")
    db_session.add(match)
    db_session.flush()
    row.canonical_id = match.id
    db_session.commit()

    _write(db_session, [_scraped(_SINTEZ)])

    stored = db_session.get(storage.Product, row.id)
    assert (stored.external_id, stored.canonical_id) == ("12058", match.id)
    assert _prices(db_session, row.id) == [0.9, 0.84]


def test_second_scan_changes_nothing(db_session) -> None:
    row = _legacy_row(db_session, _SLUG, price=0.84)
    _write(db_session, [_scraped(_SINTEZ), _scraped(_REYOUNG)])
    before = _rows(db_session)
    snapshots = db_session.scalar(select(func.count(storage.PriceSnapshot.id)))

    with capture_logs() as logs:
        _write(db_session, [_scraped(_SINTEZ), _scraped(_REYOUNG)])

    assert _rows(db_session) == before and before["12058"] == row.id
    assert db_session.scalar(select(func.count(storage.PriceSnapshot.id))) == snapshots
    assert _adoptions(logs) == []


def test_row_under_a_cut_slug_goes_to_the_product_whose_address_it_holds(db_session) -> None:
    """Прежний сборщик писал первые сто знаков слага: обрезок общий у двух товаров."""
    stem = "librederm-cerafavit-" * 5 + "krem-gel"
    small, large = f"{stem}-250-ml", f"{stem}-400-ml"
    assert small[:100] == large[:100]
    row = _legacy_row(db_session, large, price=38.5)

    _write(
        db_session,
        [
            _scraped({"id": 501, "name": "Krem-gel 250 ml", "slug": small, "price": 26.0}),
            _scraped({"id": 502, "name": "Krem-gel 400 ml", "slug": large, "price": 38.5}),
        ],
    )

    rows = _rows(db_session)
    assert rows["502"] == row.id and rows["501"] != row.id
    assert _prices(db_session, rows["502"]) == [38.5]
    assert _prices(db_session, rows["501"]) == [26.0]


def test_row_under_the_full_slug_is_found_as_well(db_session) -> None:
    """Строка могла быть записана и под слагом целиком (PR #52)."""
    slug = "librederm-cerafavit-" * 5 + "krem-gel-250-ml"
    row = _legacy_row(db_session, slug, external_id=slug, price=26.0)

    _write(db_session, [_scraped({"id": 501, "name": "Krem-gel", "slug": slug, "price": 26.0})])

    assert _rows(db_session) == {"501": row.id}


@pytest.mark.parametrize("first", ["sintez", "reyoung"])
def test_shared_row_goes_to_the_product_of_its_country(db_session, first: str) -> None:
    """Страна — часть того, какой это товар: порядок в листинге ничего не решает."""
    row = _legacy_row(db_session, _SLUG, price=0.84, country="cn")
    sintez = _scraped(_SINTEZ, country="Rusiya")
    reyoung = _scraped(_REYOUNG, country="Çin")

    with capture_logs() as logs:
        _write(db_session, [sintez, reyoung] if first == "sintez" else [reyoung, sintez])

    rows = _rows(db_session)
    assert rows["12224"] == row.id and rows["12058"] != row.id
    # В строке была цена другого товара: наследник записал свою.
    assert _prices(db_session, row.id) == [0.84, 0.79]
    assert _prices(db_session, rows["12058"]) == [0.84]
    stored = db_session.get(storage.Product, row.id)
    assert (stored.manufacturer_country_code, stored.country_resolution_status) == (
        "cn",
        "resolved",
    )
    assert [(e["products"], e["shared_slugs"]) for e in _adoptions(logs)] == [(1, 1)]


@pytest.mark.parametrize(
    ("row", "sintez", "reyoung", "heir"),
    [
        pytest.param(
            {"country": "ru", "brand": "Reyoung", "price": 0.79},
            {"country": "Rusiya", "brand": "Sintez"},
            {"country": "Rusiya", "brand": "Reyoung"},
            "12224",
            id="same_country_brand_decides",
        ),
        pytest.param(
            {"country": "ru", "brand": "reyoung ", "price": 0.84},
            {"country": "Rusiya", "brand": "Sintez"},
            {"country": "Rusiya", "brand": "Reyoung"},
            "12224",
            id="brand_outweighs_price_and_ignores_case",
        ),
        pytest.param(
            {"country": "ru", "price": 0.79},
            {"country": "Rusiya"},
            {"country": "Rusiya"},
            "12224",
            id="same_country_no_brand_last_price_decides",
        ),
        pytest.param(
            {"country": "ru", "price": 5.0},
            {"country": "Rusiya"},
            {"country": "Rusiya"},
            "12058",
            id="nothing_tells_them_apart_lower_number",
        ),
        pytest.param(
            {"price": 0.79},
            {"country": "Rusiya"},
            {"country": "Çin"},
            "12224",
            id="row_without_country_last_price_decides",
        ),
        pytest.param(
            {"country": "ru", "brand": "Reyoung", "price": 0.79},
            {"country": "Rusiya"},
            {"country": "87", "brand": "Reyoung"},
            "12058",
            id="country_outweighs_brand_and_price",
        ),
    ],
)
def test_shared_row_heir(db_session, row: dict, sintez: dict, reyoung: dict, heir: str) -> None:
    stored = _legacy_row(db_session, _SLUG, **row)

    # Больший номер первым: «кто раньше в листинге» наследника не определяет.
    _write(db_session, [_scraped(_REYOUNG, **reyoung), _scraped(_SINTEZ, **sintez)])

    rows = _rows(db_session)
    assert set(rows) == {"12058", "12224"}
    assert rows[heir] == stored.id


def test_heirs_are_chosen_across_persist_chunks(db_session) -> None:
    """Претенденты на одну строку стоят в листинге далеко друг от друга.

    Страна у обоих та же, что у строки: решить может только сравнение их между
    собой, а оно возможно, пока оба видны в одном проходе.
    """
    row = _legacy_row(db_session, _SLUG, price=0.79, country="ru", brand="Reyoung")
    filler = [
        _scraped({"id": 40000 + n, "name": f"Filler {n}", "slug": f"filler-{n}", "price": 1.0})
        for n in range(main._PERSIST_CHUNK + 1)
    ]

    _write(
        db_session,
        [
            _scraped(_SINTEZ, country="Rusiya", brand="Sintez"),
            *filler,
            _scraped(_REYOUNG, country="Rusiya", brand="Reyoung"),
        ],
    )

    rows = _rows(db_session)
    assert rows["12224"] == row.id and rows["12058"] != row.id
    assert len(rows) == len(filler) + 2


@pytest.mark.parametrize("whole", [True, False], ids=["full_scan", "partial_scan"])
def test_product_of_another_country_does_not_take_the_row_even_alone(db_session, whole) -> None:
    """Сбор видит одного из двух товаров слага — не того, что в строке."""
    row = _legacy_row(db_session, _SLUG, price=0.84, country="ru")
    seen_at = db_session.get(storage.Product, row.id).last_seen_at

    with capture_logs() as logs:
        _write(db_session, [_scraped(_REYOUNG, country="Çin")], whole=whole)

    rows = _rows(db_session)
    assert rows[_SLUG] == row.id and rows["12224"] != row.id
    untouched = db_session.get(storage.Product, row.id)
    assert (untouched.url, untouched.last_seen_at) == (_address(_SLUG), seen_at)
    assert _prices(db_session, row.id) == [0.84]
    assert [(e["products"], e["left_for_other_country"]) for e in _adoptions(logs)] == [(0, 1)]

    # Свой товар приходит следующим сбором и получает строку.
    _write(db_session, [_scraped(_SINTEZ, country="Rusiya")], whole=whole)

    assert _rows(db_session) == {"12058": row.id, "12224": rows["12224"]}


@pytest.mark.parametrize("country", [None, "87", "Специфарма"])
def test_product_with_no_named_country_takes_the_row(db_session, country: str | None) -> None:
    """Номер страны без названия и подпись, которая не страна, — не возражение."""
    row = _legacy_row(db_session, _SLUG, price=0.84, country="ru")

    _write(db_session, [_scraped(_SINTEZ, country=country)])

    assert _rows(db_session) == {"12058": row.id}


def test_renamed_product_keeps_its_row(db_session) -> None:
    """Слаг сайт выводит из названия: под номером товар переживает переименование."""
    _write(db_session, [_scraped(_SINTEZ)])
    before = _rows(db_session)

    _write(
        db_session,
        [_scraped({**_SINTEZ, "name": "Seftriakson 1 q", "slug": "seftriakson-1-q"})],
    )

    assert _rows(db_session) == before
    stored = db_session.get(storage.Product, before["12058"])
    assert (stored.name, stored.url) == (
        "Seftriakson 1 q",
        "https://aloe.az/12058/#seftriakson-1-q",
    )


def test_newer_of_two_rows_under_one_slug_gets_the_number(db_session) -> None:
    """Обрезок и слаг целиком — две строки одного товара; живая та, что видели позже."""
    slug = "librederm-cerafavit-" * 5 + "krem-gel-250-ml"
    cut = _legacy_row(db_session, slug, price=20.0)
    full = _legacy_row(db_session, slug, external_id=slug, price=26.0)
    cut.last_seen_at = full.last_seen_at - timedelta(days=30)
    db_session.commit()

    product = {"id": 501, "name": "Krem-gel", "slug": slug, "price": 26.0}
    with capture_logs() as logs:
        _write(db_session, [_scraped(product)])

    assert _rows(db_session) == {"501": full.id, slug[:100]: cut.id}
    # Вторая строка осталась без товара, а не «для товара другой страны».
    assert [(e["products"], e["left_for_other_country"]) for e in _adoptions(logs)] == [(1, 0)]

    # У товара уже есть строка под номером: вторая строка слага ему не нужна.
    with capture_logs() as logs:
        _write(db_session, [_scraped(product)])

    assert _rows(db_session) == {"501": full.id, slug[:100]: cut.id}
    assert _adoptions(logs) == []


def test_products_not_named_by_the_site_are_left_alone(db_session) -> None:
    """Товар, собранный не из листинга (номер не прочитан), строку не переводит."""
    row = _legacy_row(db_session, _SLUG, price=0.84)
    by_hand = ScrapedProduct(
        site="aloe", external_id="12058", url="https://aloe.az/12058/#ceftriaxone-1-q", name="X"
    )
    other_site = ScrapedProduct(
        site="aptekonline",
        external_id="12224",
        url="https://aloe.az/12224/#ceftriaxone-1-q",
        name="X",
        identity_verified=True,
    )
    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()

    with capture_logs() as logs:
        persist_results(
            db_session,
            run,
            [
                ScrapeResult(site="aloe", products=[by_hand]),
                ScrapeResult(site="aptekonline", products=[other_site]),
            ],
        )

    assert _adoptions(logs) == []
    assert db_session.get(storage.Product, row.id).external_id == _SLUG


def test_adoption_asks_the_database_in_bounded_batches(db_session, monkeypatch) -> None:
    """Первый сбор после выкладки переводит весь каталог: тысячи строк одним списком не ищем."""
    monkeypatch.setattr(main, "_ALOE_ADOPT_QUERY_CHUNK", 2)
    items = [
        {"id": 700 + n, "name": f"Tovar {n}", "slug": f"tovar-{n}", "price": 1.0} for n in range(5)
    ]
    rows = [_legacy_row(db_session, item["slug"], name=item["name"]) for item in items]

    _write(db_session, [_scraped(item) for item in items])

    assert _rows(db_session) == {str(item["id"]): row.id for item, row in zip(items, rows)}


def test_number_and_address_are_written_together(db_session) -> None:
    """Запись сбора коммитит пачками: сбой между ними не оставляет номер при адресе со слагом."""
    row = _legacy_row(db_session, _SLUG, price=0.84)

    waiting = main._adopt_aloe_product_numbers(db_session, [_scraped(_SINTEZ)], whole_catalog=True)
    db_session.commit()
    db_session.expire_all()

    assert waiting == set()
    stored = db_session.get(storage.Product, row.id)
    assert (stored.external_id, stored.url) == ("12058", "https://aloe.az/12058/#ceftriaxone-1-q")


def test_discounted_price_tells_the_heir(db_session) -> None:
    """У товара со скидкой в записи о цене две цены — сравниваются обе."""
    row = _legacy_row(db_session, _SLUG, country="ru")
    run = storage.Run(status="ok")
    db_session.add(run)
    db_session.flush()
    db_session.add(
        storage.PriceSnapshot(
            run_id=run.id, product_id=row.id, price=0.9, discount_price=0.79, is_on_sale=True
        )
    )
    db_session.commit()
    plain = _scraped({**_SINTEZ, "price": 0.9}, country="Rusiya")
    on_sale = _scraped({**_REYOUNG, "price": 0.79, "old_price": 0.9}, country="Rusiya")
    assert (on_sale.price, on_sale.discount_price) == (0.9, 0.79)

    _write(db_session, [plain, on_sale])

    assert _rows(db_session)["12224"] == row.id


def test_label_the_site_corrected_is_not_another_country(db_session) -> None:
    """Страна, которую сбор уже видел у строки и держит на проверке, — тот же товар."""
    row = _legacy_row(db_session, _SLUG, price=0.84, country="ua")
    row.country_candidate_code = "ie"
    row.country_candidate_seen_count = 1
    row.country_resolution_status = "ambiguous"
    db_session.commit()

    _write(db_session, [_scraped(_SINTEZ, country="Ирландия")])

    assert _rows(db_session) == {"12058": row.id}
    # А страна, которой у строки не было и на проверке, — чужая.
    _legacy_row(db_session, "enterol-250-mq-10-ed", price=16.0, country="ua")
    _write(
        db_session,
        [
            _scraped(
                {"id": 12242, "name": "Enterol", "slug": "enterol-250-mq-10-ed", "price": 16.0},
                country="Ирландия",
            )
        ],
    )

    assert {"12242", "enterol-250-mq-10-ed"} <= set(_rows(db_session))


# ─── Частичный сбор: остальных товаров слага он не видит ─────────────────────


@pytest.mark.parametrize(
    ("row", "product"),
    [
        pytest.param(
            {"country": "ru", "brand": "Sintez", "price": 0.84},
            {"country": "Rusiya", "brand": "Sintez"},
            id="country_brand_and_price_are_the_ones_in_the_row",
        ),
        pytest.param(
            {"country": "ru", "price": 0.84}, {"country": "Rusiya"}, id="row_without_brand"
        ),
        pytest.param(
            {"country": "ru", "brand": "Sintez"},
            {"country": "Rusiya", "brand": "sintez"},
            id="row_without_price",
        ),
        pytest.param({"price": 0.84}, {"country": "Rusiya"}, id="row_without_country"),
        pytest.param({}, {"country": "Rusiya", "brand": "Sintez"}, id="row_knows_nothing"),
    ],
)
def test_partial_scan_moves_a_row_to_the_product_written_in_it(db_session, row, product) -> None:
    stored = _legacy_row(db_session, _SLUG, **row)

    _write(db_session, [_scraped(_SINTEZ, **product)], whole=False)

    assert _rows(db_session) == {"12058": stored.id}


@pytest.mark.parametrize(
    ("row", "product"),
    [
        pytest.param(
            {"country": "ru", "brand": "Reyoung", "price": 0.79},
            {"country": "Rusiya", "brand": "Sintez"},
            id="same_country_other_brand_other_price",
        ),
        # У двух товаров слага цена бывает одинаковой, а бренд — общим: одного
        # совпадения мало.
        pytest.param(
            {"country": "ru", "brand": "Reyoung", "price": 0.84},
            {"country": "Rusiya", "brand": "Sintez"},
            id="same_price_other_brand",
        ),
        pytest.param(
            {"country": "ru", "brand": "Sintez", "price": 0.79},
            {"country": "Rusiya", "brand": "Sintez"},
            id="same_brand_other_price",
        ),
        pytest.param(
            {"country": "ru", "brand": "Sintez", "price": 0.84},
            {"country": "Rusiya"},
            id="brand_of_the_row_is_not_confirmed",
        ),
        pytest.param(
            {"country": "ru", "price": 0.79}, {"country": "Rusiya"}, id="no_brand_other_price"
        ),
        pytest.param(
            {"country": "ru", "brand": "Sintez", "price": 0.84},
            {"country": "87", "brand": "Sintez"},
            id="country_of_the_row_is_not_confirmed",
        ),
    ],
)
def test_partial_scan_leaves_a_doubtful_product_for_the_full_scan(db_session, row, product) -> None:
    """Вторая строка отняла бы у товара историю и место в паре: лучше пропустить тик."""
    stored = _legacy_row(db_session, _SLUG, **row)
    before = (stored.external_id, stored.url, stored.last_seen_at)
    snapshots = db_session.scalar(select(func.count(storage.PriceSnapshot.id)))

    with capture_logs() as logs:
        _write(db_session, [_scraped(_SINTEZ, **product)], whole=False)

    assert _rows(db_session) == {_SLUG: stored.id}
    untouched = db_session.get(storage.Product, stored.id)
    assert (untouched.external_id, untouched.url, untouched.last_seen_at) == before
    assert db_session.scalar(select(func.count(storage.PriceSnapshot.id))) == snapshots
    assert db_session.scalar(select(func.count(storage.OfferObservation.id))) == 0
    assert [(e["products"], e["left_for_full_scan"], e["waiting"]) for e in _adoptions(logs)] == [
        (0, 1, ["12058"])
    ]


def test_partial_scan_gives_the_other_product_its_row_once_the_shared_row_is_taken(
    db_session,
) -> None:
    """Ждать нечего: строку слага уже получил товар, который в ней записан."""
    row = _legacy_row(db_session, _SLUG, price=0.79, country="ru", brand="Reyoung")

    _write(
        db_session,
        [
            _scraped(_SINTEZ, country="Rusiya", brand="Sintez"),
            _scraped(_REYOUNG, country="Rusiya", brand="Reyoung"),
        ],
        whole=False,
    )

    rows = _rows(db_session)
    assert rows["12224"] == row.id and rows["12058"] != row.id


def test_product_listed_in_two_sections_gets_one_price_record(db_session) -> None:
    """aloe пишется целиком в конце сбора: оба вхождения товара стоят в одной пачке."""
    _write(db_session, [_scraped(_SINTEZ)])
    row_id = _rows(db_session)["12058"]
    dearer = {**_SINTEZ, "price": 0.9}

    _write(db_session, [_scraped(dearer), _scraped(dearer)])

    assert _prices(db_session, row_id) == [0.84, 0.9]


def test_full_scan_settles_what_a_partial_scan_left(db_session) -> None:
    """Раздел «хиты» показывает один товар слага; в строке записан другой — той же страны."""
    row = _legacy_row(db_session, _SLUG, price=0.79, country="ru", brand="Reyoung")
    sintez = _scraped(_SINTEZ, country="Rusiya", brand="Sintez")
    reyoung = _scraped(_REYOUNG, country="Rusiya", brand="Reyoung")

    _write(db_session, [sintez], whole=False)
    assert _rows(db_session) == {_SLUG: row.id}

    _write(db_session, [sintez, reyoung])

    rows = _rows(db_session)
    assert rows["12224"] == row.id and rows["12058"] != row.id
    assert _prices(db_session, row.id) == [0.79]
    assert _prices(db_session, rows["12058"]) == [0.84]


def test_full_scan_with_an_unfinished_section_decides_like_a_partial_one(db_session) -> None:
    """Раздел не пройден — часть претендентов не видна: сравнивать их между собой рано."""
    row = _legacy_row(db_session, _SLUG, price=0.79, country="ru", brand="Reyoung")
    run = storage.Run(status="running", catalog_scope="full")
    db_session.add(run)
    db_session.commit()

    persist_results(
        db_session,
        run,
        [_result([_scraped(_SINTEZ, country="Rusiya", brand="Sintez")], complete=False)],
    )

    assert _rows(db_session) == {_SLUG: row.id}


async def test_run_decides_heirs_over_the_whole_site_not_section_by_section(
    monkeypatch, db_session
) -> None:
    """Команда `run` пишет другие сайты по разделам; aloe — целиком в конце сбора.

    Раздел «хиты» идёт первым и показывает один товар слага. Записанный сразу, он
    забрал бы строку, в которой записан другой товар той же страны.
    """
    from tests.test_run_failure_semantics import _patch_verified_aloe_pipeline

    row = _legacy_row(db_session, _SLUG, price=0.79, brand="Reyoung")
    pages = {
        "product_field=bestseller": _listing({**_SINTEZ, "brand": {"name": "Sintez"}}),
        "category_slug=dermanlar": _listing(
            {**_SINTEZ, "brand": {"name": "Sintez"}}, {**_REYOUNG, "brand": {"name": "Reyoung"}}
        ),
    }
    scrape_all, persist = main.scrape_all, main.persist_results
    persisted: list[list[str]] = []

    def recording_persist(session, run, results):
        persisted.append([p.external_id for r in results for p in r.products])
        return persist(session, run, results)

    async def fetch(self, url: str) -> str:
        return pages[url.split("?", 1)[1]]

    async def no_browser(self):
        return self

    async def no_promos(self):
        return []

    _patch_verified_aloe_pipeline(db_session, monkeypatch)
    monkeypatch.setattr(main, "scrape_all", scrape_all)
    monkeypatch.setattr(main, "persist_results", recording_persist)
    monkeypatch.setattr(
        main.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: ["product_field=bestseller", "dermanlar"],
    )
    monkeypatch.setattr(AloeScraper, "_open_browser", no_browser)
    monkeypatch.setattr(AloeScraper, "scrape_promos", no_promos)
    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fetch)
    monkeypatch.setattr(AloeScraper, "rate_limit_sec", 0.001, raising=False)

    result = await asyncio.to_thread(
        CliRunner().invoke,
        main.cli,
        ["run", "--site", "aloe", "--mode", "category", "--no-alerts", "--force"],
    )

    assert result.exit_code == 0, result.output
    assert persisted == [["12058", "12058", "12224"]]
    db_session.expire_all()
    rows = _rows(db_session)
    assert rows["12224"] == row.id and rows["12058"] != row.id


# ─── Включение правила: миграция 0024, а не выкладка кода ────────────────────


def test_before_the_migration_products_are_written_under_the_slug_as_before(
    switched_off_session,
) -> None:
    """Код выложен, миграция ещё не применена: ни одна строка не переводится."""
    session = switched_off_session
    row = _legacy_row(session, _SLUG, price=0.84)
    long_slug = "librederm-cerafavit-" * 5 + "krem-gel-250-ml"

    with capture_logs() as logs:
        _write(
            session,
            [
                _scraped(_SINTEZ),
                # Второй товар слага прежний сборщик отбрасывал, товар без слага не писал.
                _scraped(_REYOUNG),
                _scraped({"id": 30781, "name": "Aykutop 30 əd", "slug": "0", "price": 12.0}),
                _scraped({"id": 501, "name": "Krem-gel", "slug": long_slug, "price": 26.0}),
            ],
        )

    rows = _rows(session)
    assert set(rows) == {_SLUG, long_slug[:100]} and rows[_SLUG] == row.id
    assert session.get(storage.Product, row.id).url == _address(_SLUG)
    assert session.get(storage.Product, rows[long_slug[:100]]).url == _address(long_slug)
    assert _prices(session, row.id) == [0.84]
    assert _adoptions(logs) == []


def test_before_the_migration_a_shared_row_stays_with_the_last_section_first_product(
    switched_off_session,
) -> None:
    """Прежний сборщик писал по разделам: строка оставалась за первым товаром последнего."""
    session = switched_off_session
    hit, sintez, reyoung = _scraped(_SINTEZ), _scraped(_SINTEZ), _scraped(_REYOUNG)
    hit.category = "product_field=bestseller"

    _write(session, [hit, reyoung, sintez])

    (row_id,) = _rows(session).values()
    assert set(_rows(session)) == {_SLUG}
    assert (_prices(session, row_id), session.get(storage.Product, row_id).category) == (
        [0.79],
        "dermanlar",
    )


def test_rule_off_refuses_to_write_under_the_slug_over_numbered_rows(switched_off_session) -> None:
    """Предохранитель потерян, а строки уже под номером: запись под слагом — второй каталог."""
    session = switched_off_session
    numbered = _numbered_row(session, "12058", _SLUG)
    run = storage.Run(status="running")
    session.add(run)
    session.commit()

    with pytest.raises(RuntimeError, match="уже записаны под номером"):
        persist_results(session, run, [_result([_scraped(_SINTEZ), _scraped(_REYOUNG)])])
    session.rollback()

    assert _rows(session) == {"12058": numbered.id}
    # После отката у слага есть строка под слагом: запись идёт, как до правки.
    under_slug = _legacy_row(session, _SLUG, price=0.84)
    persist_results(session, run, [_result([_scraped(_SINTEZ)])])
    session.commit()
    assert _rows(session) == {"12058": numbered.id, _SLUG: under_slug.id}


def test_downgrade_waits_for_a_running_scrape(db_session) -> None:
    """Сбор, начатый при включённом правиле, не должен заканчиваться без него."""
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("замок сбора — advisory lock PostgreSQL")
    row = _legacy_row(db_session, _SLUG, price=0.84)
    _write(db_session, [_scraped(_SINTEZ)])
    with db_session.get_bind().connect() as scrape:
        scrape.exec_driver_sql("SELECT pg_advisory_lock(hashtext('pharmacy_monitor_scrape'))")
        with pytest.raises(RuntimeError, match="the scrape lock is held"):
            _downgrade(db_session)
        db_session.rollback()
        scrape.exec_driver_sql("SELECT pg_advisory_unlock(hashtext('pharmacy_monitor_scrape'))")

    assert main._aloe_number_identity_active(db_session) is True
    assert _rows(db_session) == {"12058": row.id}
    _downgrade(db_session)
    assert _rows(db_session) == {_SLUG: row.id}


def test_guard_works_whatever_the_search_path_of_the_writer(db_session) -> None:
    """Скрипт восстановления ставит пустой search_path: функция находит таблицу сама."""
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("предохранитель — триггер PostgreSQL")
    _write(db_session, [_scraped(_SINTEZ)])
    schema = db_session.scalar(text("select current_schema()"))
    insert = (
        f"insert into {schema}.products (tenant_id, site, external_id, url, name,"
        " name_normalized, country_resolution_status, country_candidate_seen_count,"
        " offer_availability_status, first_seen_at, last_seen_at)"
        " values (1, 'aloe', :id, :url, 'x', 'x', 'unknown', 0, 'unknown', now(), now())"
    )
    db_session.execute(text("set local search_path = ''"))

    db_session.execute(text(insert), {"id": "novinka", "url": "https://aloe.az/novinka/"})
    with pytest.raises(IntegrityError, match="already stored under its site number"):
        db_session.execute(text(insert), {"id": _SLUG, "url": f"https://aloe.az/ru/{_SLUG}?x=1"})
    db_session.rollback()


async def test_automatic_ai_fallback_skips_aloe(monkeypatch) -> None:
    """Запасной сборщик называет товар aloe по-своему — завёл бы ему вторую строку."""
    started: list[str] = []

    class Crawler:
        def __init__(self) -> None:
            started.append("ai")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def crawl(self, max_urls: int):
            return ScrapeResult(site="aloe")

    async def no_browser(self):
        return self

    async def no_promos(self):
        return []

    async def fetch(self, url: str) -> str:
        return _listing()

    monkeypatch.setenv(main.AI_FALLBACK_ENABLED_ENV, "1")
    monkeypatch.setitem(main.AI_CRAWLER_BY_SITE, "aloe", Crawler)
    monkeypatch.setitem(main.AI_CRAWLER_BY_SITE, "aptekonline", Crawler)
    monkeypatch.setattr(AloeScraper, "_open_browser", no_browser)
    monkeypatch.setattr(AloeScraper, "scrape_promos", no_promos)
    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fetch)
    monkeypatch.setitem(main.SCRAPER_CLASSES, "aptekonline", AloeScraper)

    await main.scrape_site("aloe", ["dermanlar"], None, ai_fallback_baseline=1000)
    assert started == []
    # Тот же пустой сбор другого сайта запасной сборщик запускает.
    await main.scrape_site("aptekonline", ["dermanlar"], None, ai_fallback_baseline=1000)
    assert started == ["ai"]


def test_migration_switches_the_rule_on_and_downgrade_switches_it_off(
    switched_off_session,
) -> None:
    session = switched_off_session
    assert main._aloe_number_identity_active(session) is False
    row = _legacy_row(session, _SLUG, price=0.84)

    for _ in range(2):  # повторный запуск шага ничего не ломает
        _migrate(session.connection(), "upgrade")
        session.commit()
    assert main._aloe_number_identity_active(session) is True
    _write(session, [_scraped(_SINTEZ)])
    assert _rows(session) == {"12058": row.id}

    for _ in range(2):
        _downgrade(session)
    assert main._aloe_number_identity_active(session) is False
    assert _rows(session) == {_SLUG: row.id}


def test_guard_stops_slug_keyed_code_from_writing_a_second_catalogue(db_session) -> None:
    """Код, который не знает о номере, после перевода строк не находит товар и вставляет его снова."""
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("предохранитель — триггер PostgreSQL")
    row = _legacy_row(db_session, _SLUG, price=0.84)
    _write(db_session, [_scraped(_SINTEZ), _scraped(_REYOUNG)])
    before = _rows(db_session)
    assert before["12058"] == row.id

    # Так пишет код до правки: идентификатор — слаг, о номере он не знает.
    def as_old_code(slug: str, price: float) -> ScrapeResult:
        return ScrapeResult(
            site="aloe",
            products=[
                ScrapedProduct(
                    site="aloe", external_id=slug, url=_address(slug), name=slug, price=price
                )
            ],
        )

    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()
    with pytest.raises(IntegrityError, match="already stored under its site number"):
        persist_results(db_session, run, [as_old_code(_SLUG, 0.84)])
    db_session.rollback()

    assert _rows(db_session) == before
    # Товар, которого под номером нет, такой код пишет как раньше.
    persist_results(db_session, run, [as_old_code("novinka", 1.0)])
    db_session.commit()
    assert set(_rows(db_session)) == {*before, "novinka"}


# ─── Поиск товара по адресу (ручная привязка, закреплённые ссылки) ───────────


def _numbered_row(db_session, number: str, slug: str, *, site: str = "aloe", tenant_id: int = 1):
    row = storage.Product(
        tenant_id=tenant_id,
        site=site,
        external_id=number,
        url=f"https://aloe.az/{number}/#{slug}",
        name=slug,
        name_normalized=slug,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _found(db_session, url: str, **kwargs) -> list[str]:
    return [
        p.external_id for p in match_actions.aloe_products_at_address(db_session, url, **kwargs)
    ]


def test_number_address_names_one_product_and_slug_address_all_of_the_slug(db_session) -> None:
    _numbered_row(db_session, "12058", _SLUG)
    _numbered_row(db_session, "12224", _SLUG)
    _numbered_row(db_session, "15153", f"pan-{_SLUG}")
    _numbered_row(db_session, "27224", f"{_SLUG}-1-ed")
    _numbered_row(db_session, "12224", _SLUG, site="aptekonline")
    _numbered_row(db_session, "99001", _SLUG, tenant_id=2)

    for address in (
        "https://aloe.az/12224/",
        "https://aloe.az/12224",
        "https://aloe.az/ru/12224/?x=1",
    ):
        assert _found(db_session, address) == ["12224"]
    assert _found(db_session, "https://aloe.az/12224/#any-other-slug") == ["12224"]
    assert _found(db_session, f"https://aloe.az/{_SLUG}/") == ["12058", "12224"]
    assert _found(db_session, f"https://aloe.az/ru/{_SLUG}?utm=1") == ["12058", "12224"]
    assert _found(db_session, f"https://aloe.az/{_SLUG}/", tenant_id=2) == ["99001"]
    # Номера 1205 в каталоге нет, хотя эти цифры есть в адресе товара 12058.
    assert _found(db_session, "https://aloe.az/1205/") == []
    for nothing in ("", "https://aloe.az/", "https://aloe.az/0/", "https://aloe.az/no-such-slug/"):
        assert _found(db_session, nothing) == []


@pytest.mark.parametrize("wildcard", ["ceftriaxone-_-q", "ceftriaxone-%", "%"])
def test_signs_in_a_slug_address_are_not_wildcards(db_session, wildcard: str) -> None:
    _numbered_row(db_session, "12058", _SLUG)

    assert _found(db_session, f"https://aloe.az/{wildcard}/") == []


def test_slug_address_finds_a_row_the_scrape_has_not_moved_to_a_number_yet(db_session) -> None:
    stem = "librederm-cerafavit-" * 5 + "krem-gel"
    small, large = f"{stem}-250-ml", f"{stem}-400-ml"
    _legacy_row(db_session, small)
    _legacy_row(db_session, _SLUG)

    assert _found(db_session, _address(small)) == [small[:100]]
    assert _found(db_session, _address(_SLUG)) == [_SLUG]
    # Обрезок слага общий, а адрес в строке — другого товара.
    assert _found(db_session, _address(large)) == []


# ─── Откат: миграция возвращает идентификаторы ───────────────────────────────


def _downgrade(db_session) -> None:
    db_session.commit()
    _migrate(db_session.connection(), "downgrade")
    db_session.commit()
    db_session.expire_all()


def test_downgrade_gives_rows_their_slug_identifiers_back(db_session) -> None:
    """Прежний код ищет товар по слагу: после отката он должен найти ту же строку."""
    long_slug = "librederm-cerafavit-" * 5 + "krem-gel-250-ml"
    shared = _legacy_row(db_session, _SLUG, price=0.84, country="ru")
    long_row = _legacy_row(db_session, long_slug, price=26.0)
    never_adopted = _legacy_row(db_session, "oxolin-025-10-q", price=1.2)
    _write(
        db_session,
        [
            _scraped(_SINTEZ, country="Rusiya"),
            _scraped(_REYOUNG, country="Çin"),
            _scraped({"id": 501, "name": "Krem-gel", "slug": long_slug, "price": 26.0}),
            _scraped({"id": 30781, "name": "Aykutop 30 əd", "slug": "0", "price": 12.0}),
        ],
    )
    split_off = _rows(db_session)["12224"]

    _downgrade(db_session)

    assert _rows(db_session) == {
        _SLUG: shared.id,
        long_slug[:100]: long_row.id,
        "oxolin-025-10-q": never_adopted.id,
        # Второй товар слага и товар без слага остаются под номером: прежний
        # код их не видит.
        "12224": split_off,
        "30781": _rows(db_session)["30781"],
    }
    assert db_session.get(storage.Product, shared.id).url == _address(_SLUG)
    assert db_session.get(storage.Product, long_row.id).url == _address(long_slug)
    assert (
        db_session.get(storage.Product, split_off).url == "https://aloe.az/12224/#ceftriaxone-1-q"
    )

    restored = _rows(db_session)
    _write(db_session, [_scraped(_SINTEZ, country="Rusiya"), _scraped(_REYOUNG, country="Çin")])

    rows = _rows(db_session)
    if db_session.get_bind().dialect.name == "postgresql":
        # Откат снял предохранитель — правило выключено: тот же код снова пишет
        # под слагом, без выкладки прежнего.
        assert rows == restored
        assert _prices(db_session, shared.id) == [0.84]
    else:
        # В SQLite правило включено всегда: строки переводятся снова.
        assert rows["12058"] == shared.id and rows["12224"] == split_off


def test_downgrade_leaves_other_sites_and_unnumbered_rows_alone(db_session) -> None:
    # У чужого сайта адрес той же формы, что у aloe: «номер, затем слаг».
    other = storage.Product(
        site="aptekonline",
        external_id="252",
        url="https://www.aptekonline.az/252/#vitamin-c",
        name="X",
        name_normalized="x",
    )
    slugged = _legacy_row(db_session, _SLUG)
    # Номер в идентификаторе, но адрес не «адрес с номером»: не наша строка.
    odd = storage.Product(
        site="aloe",
        external_id="777",
        url="https://aloe.az/something-else/#slug",
        name="Y",
        name_normalized="y",
    )
    db_session.add_all([other, odd])
    db_session.commit()

    _downgrade(db_session)

    assert db_session.get(storage.Product, other.id).external_id == "252"
    assert db_session.get(storage.Product, slugged.id).external_id == _SLUG
    assert db_session.get(storage.Product, odd.id).external_id == "777"
