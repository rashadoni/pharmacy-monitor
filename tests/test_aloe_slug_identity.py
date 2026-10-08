"""Товар aloe узнаётся по слагу целиком, а не по его первым ста знакам.

Сборщик писал в `products.external_id` обрезок `slug[:100]`, а товар в базе
ищется по паре (сайт, идентификатор). Два товара одной линейки с длинным
названием и разным объёмом в хвосте записались бы одной строкой: цена, название
и адрес — того, кто встретился последним. Адрес (`url`) при этом писался полный,
а страница товара (watchlist) брала идентификатор из адреса — тоже полный, то
есть тот же товар двумя путями получал два разных идентификатора.

Строки, уже записанные под обрезком, запись сбора переводит на полный слаг сама:
иначе товар получил бы вторую строку, а первая осталась бы с историей цен.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import storage
from src.main import persist_results
from src.scrapers import aloe
from src.scrapers.aloe import AloeScraper, aloe_external_id, aloe_products_from_listing_html
from src.scrapers.base import ScrapedProduct, ScrapeResult

_ID_COLUMN = storage.Product.__table__.c.external_id
# Столько знаков слага писал прежний сборщик. Число записано в базе строками,
# поэтому здесь оно своё, а не взятое из кода.
_CUT = 100

# Одна линейка, два объёма: слаги расходятся только после сотого знака.
_STEM = (
    "librederm-cerafavit-temizleyici-lipidleri-berpa-eden-krem-gel-korpeler-"
    "usaqlar-ve-boyukler-ucun-keramidli-ve-prebiotikli"
)
_SMALL = f"{_STEM}-250-ml"
_LARGE = f"{_STEM}-400-ml"
# Слаг шире колонки `products.external_id`.
_WIDE = "krem-" * 50 + "250-ml"


def _without_postgresql(reason: str) -> None:
    """Локально — пропуск. В CI пропуск молча убрал бы вариант с настоящей базой."""
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(params=["sqlite", "postgresql"])
def db_session(request, db_session):
    if request.param == "sqlite":
        yield db_session
        return
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        _without_postgresql("PostgreSQL DATABASE_URL is required")
    # `src.main` подгружает `.env`: тест не создаёт схемы в базе, которая не тестовая.
    if not (make_url(database_url).database or "").endswith("_test"):
        _without_postgresql("PostgreSQL DATABASE_URL must point at a *_test database")
    # Своя схема на тест: общая база CI уже размечена миграциями.
    schema = f"aloe_slug_id_{uuid4().hex[:12]}"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        storage.Base.metadata.create_all(engine)
        with sessionmaker(engine, expire_on_commit=False, autoflush=False)() as session:
            yield session
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def _listing(*items: tuple[str, str, float], page: int = 1, last_page: int = 1) -> str:
    """Листинг aloe, как его отдаёт сайт: товары в потоке Next.js."""
    cards = ",".join(
        '["$","$L","%d",{"data":%s}]'
        % (
            number,
            json.dumps(
                {"id": number, "code": str(number), "name": name, "slug": slug, "price": price}
            ),
        )
        for number, (slug, name, price) in enumerate(items, start=page * 100)
    )
    payload = '15:[[%s],false,["$","$L",null,{"currentPage":%d,"lastPage":%d}]]' % (
        cards,
        page,
        last_page,
    )
    return f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>"


def _listed(*items: tuple[str, str, float]) -> list[ScrapedProduct]:
    return aloe_products_from_listing_html(_listing(*items), category_slug="kosmetika")


async def _scan(monkeypatch, pages: dict[int, str]) -> list[ScrapedProduct]:
    """Обход раздела штатным сборщиком; сайт заменён заранее собранными страницами."""

    async def fetch(self, url: str) -> str:
        number = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
        return pages[number]

    monkeypatch.setattr(AloeScraper, "_fetch_listing_html", fetch)
    scraper = AloeScraper(rate_limit_sec=0.001)
    return [product async for product in scraper.scrape_category("kosmetika")]


def _write(db_session, products: list[ScrapedProduct]) -> storage.Run:
    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()
    written = persist_results(db_session, run, [ScrapeResult(site="aloe", products=products)])
    assert written == len(products)
    db_session.commit()
    db_session.expire_all()
    return run


def _product(
    external_id: str, url: str, *, site: str = "aloe", name: str = "Товар"
) -> storage.Product:
    return storage.Product(
        site=site, external_id=external_id, url=url, name=name, name_normalized=name.lower()
    )


def _address(slug: str) -> str:
    return f"https://aloe.az/{slug}/"


def _ids(db_session) -> dict[int, str]:
    """Номер строки → идентификатор, по всем товарам в базе."""
    return dict(db_session.execute(select(storage.Product.id, storage.Product.external_id)).all())


def _prices(db_session, external_id: str) -> list[float]:
    return list(
        db_session.scalars(
            select(storage.PriceSnapshot.price)
            .join(storage.Product, storage.Product.id == storage.PriceSnapshot.product_id)
            .where(storage.Product.site == "aloe", storage.Product.external_id == external_id)
            .order_by(storage.PriceSnapshot.id)
        )
    )


def _adoptions(logs: list[dict]) -> list[dict]:
    return [entry for entry in logs if entry["event"] == "aloe_cut_identifiers_adopted"]


# ─── Сборщик ────────────────────────────────────────────────────────────────


def test_fixture_slugs_differ_only_after_the_hundredth_character() -> None:
    assert aloe.ALOE_LEGACY_ID_CUT == _CUT
    assert len(_STEM) > _CUT
    assert _SMALL[:_CUT] == _LARGE[:_CUT] and _SMALL != _LARGE


@pytest.mark.parametrize("length", [1, 99, 100, 101, 158, _ID_COLUMN.type.length])
def test_identifier_is_the_whole_slug(length: int) -> None:
    slug = ("abcdefghi-" * 30)[:length]
    assert aloe_external_id(slug) == slug


def test_slug_wider_than_the_column_still_tells_products_apart() -> None:
    width = _ID_COLUMN.type.length
    assert aloe._EXTERNAL_ID_MAX == width
    stem = "krem-" * 50
    small, large = aloe_external_id(f"{stem}250-ml"), aloe_external_id(f"{stem}400-ml")

    assert small != large
    assert len(small) == len(large) == width
    assert small[:150] == stem[:150]
    # Правило записано в базе идентификаторами: сменить его — осиротить строки.
    assert aloe_external_id("a" * 201) == "a" * 187 + "~a92efd821093"


def test_listing_page_keeps_products_whose_slugs_share_the_first_hundred_characters() -> None:
    products = _listed((_SMALL, "Krem-gel 250 ml", 26.0), (_LARGE, "Krem-gel 400 ml", 38.5))

    assert [(p.external_id, p.url, p.price) for p in products] == [
        (_SMALL, _address(_SMALL), 26.0),
        (_LARGE, _address(_LARGE), 38.5),
    ]


async def test_scan_writes_such_products_as_two_rows_with_their_own_prices(
    db_session, monkeypatch
) -> None:
    first_scan = await _scan(
        monkeypatch,
        {
            1: _listing((_SMALL, "Krem-gel 250 ml", 26.0), page=1, last_page=2),
            2: _listing((_LARGE, "Krem-gel 400 ml", 38.5), page=2, last_page=2),
        },
    )
    _write(db_session, first_scan)

    stored = db_session.execute(
        select(storage.Product.external_id, storage.Product.name, storage.Product.url)
    ).all()
    assert sorted(stored) == [
        (_SMALL, "Krem-gel 250 ml", _address(_SMALL)),
        (_LARGE, "Krem-gel 400 ml", _address(_LARGE)),
    ]

    # Следующий сбор: подорожал только большой. У маленького цена прежняя —
    # новой записи о цене нет, а не «38.5 → 26 → 41».
    second_scan = await _scan(
        monkeypatch,
        {
            1: _listing((_SMALL, "Krem-gel 250 ml", 26.0), page=1, last_page=2),
            2: _listing((_LARGE, "Krem-gel 400 ml", 41.0), page=2, last_page=2),
        },
    )
    _write(db_session, second_scan)

    assert _prices(db_session, _SMALL) == [26.0]
    assert _prices(db_session, _LARGE) == [38.5, 41.0]
    assert db_session.scalar(select(func.count()).select_from(storage.Product)) == 2


class _Handle:
    def __init__(self, text_: str) -> None:
        self._text = text_

    async def inner_text(self) -> str:
        return self._text

    async def get_attribute(self, _name: str) -> None:
        return None


class _ProductPage:
    """Страница товара ровно в том объёме, который читает `scrape_product_page`."""

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    async def wait_for_selector(self, *_args, **_kwargs) -> None:
        return None

    async def query_selector(self, selector: str) -> _Handle | None:
        if selector.startswith("h1"):
            return _Handle("Krem-gel 250 ml")
        if "priceWrapper" in selector:
            return _Handle("26.00")
        return None

    async def content(self) -> str:
        return "<html></html>"

    async def close(self) -> None:
        return None


async def _opened(monkeypatch, url: str) -> ScrapedProduct:
    """Товар, прочитанный со своей страницы, — путь закреплённой ссылки (watchlist)."""

    async def new_page(self) -> _ProductPage:
        return _ProductPage()

    async def goto(self, page, url: str, check_captcha: bool = True) -> None:
        return None

    monkeypatch.setattr(AloeScraper, "new_page", new_page)
    monkeypatch.setattr(AloeScraper, "goto", goto)
    product = await AloeScraper(rate_limit_sec=0.001).scrape_product_page(url)
    assert product is not None
    return product


@pytest.mark.parametrize("slug", [_SMALL, _WIDE])
async def test_product_page_names_the_product_as_its_listing_does(monkeypatch, slug: str) -> None:
    # Иначе закреплённая ссылка завела бы товару вторую строку.
    (listed,) = _listed((slug, "Krem-gel 250 ml", 26.0))

    opened = await _opened(monkeypatch, listed.url)

    assert opened.external_id == listed.external_id
    assert len(opened.external_id) <= _ID_COLUMN.type.length


class _Card:
    """Карточка листинга в режиме браузера: у неё есть только название и цена."""

    def __init__(self, name: str) -> None:
        self._name = name

    async def query_selector(self, selector: str) -> _Handle | None:
        if "productName" in selector:
            return _Handle(self._name)
        if "priceWrapper" in selector:
            return _Handle("26.00")
        return None


@pytest.mark.parametrize("slug", [_SMALL, _WIDE])
async def test_browser_mode_names_the_product_as_its_listing_does(slug: str) -> None:
    (listed,) = _listed((slug, "Krem-gel 250 ml", 26.0))
    name = slug.replace("-", " ")
    assert aloe.aloe_slug(name) == slug

    card = await AloeScraper(rate_limit_sec=0.001)._parse_card(
        _Card(name), "kosmetika", "https://aloe.az/catalog/filters/?category_slug=kosmetika"
    )

    assert card is not None
    assert (card.external_id, card.url) == (listed.external_id, listed.url)


# ─── Строки, записанные под обрезком ────────────────────────────────────────


def test_stored_cut_row_is_found_again_and_keeps_its_history(db_session) -> None:
    cluster = storage.Match(canonical_name="Krem-gel 250 ml")
    earlier_run = storage.Run(status="ok")
    cut = _product(_SMALL[:_CUT], _address(_SMALL), name="Krem-gel 250 ml")
    wide = _product(_WIDE[:_CUT], _address(_WIDE))
    db_session.add_all([cluster, earlier_run, cut, wide])
    db_session.commit()
    cut.canonical_id = cluster.id
    db_session.add(
        storage.PriceSnapshot(
            run_id=earlier_run.id, product_id=cut.id, price=26.0, is_on_sale=False
        )
    )
    db_session.commit()
    rows = {cut.id: _SMALL, wide.id: aloe_external_id(_WIDE)}
    scan = ((_SMALL, "Krem-gel 250 ml", 26.0), (_WIDE, "Krem", 9.0))

    with capture_logs() as logs:
        _write(db_session, _listed(*scan))

    # Те же строки, вторых не появилось; цена прежняя — запись о ней осталась
    # одна, та, что была; место в кластере на месте.
    assert _ids(db_session) == rows
    assert db_session.get(storage.Product, cut.id).canonical_id == cluster.id
    assert db_session.execute(
        select(storage.PriceSnapshot.product_id, storage.PriceSnapshot.run_id).where(
            storage.PriceSnapshot.product_id == cut.id
        )
    ).all() == [(cut.id, earlier_run.id)]
    assert _adoptions(logs) == [
        {"event": "aloe_cut_identifiers_adopted", "log_level": "info", "products": 2}
    ]

    with capture_logs() as logs:
        _write(db_session, _listed(*scan))

    assert _ids(db_session) == rows
    assert not _adoptions(logs)


async def test_pinned_link_finds_the_stored_cut_row(db_session, monkeypatch) -> None:
    cut = _product(_SMALL[:_CUT], _address(_SMALL))
    db_session.add(cut)
    db_session.commit()

    # Закреплённая ссылка записана без косой черты в конце.
    _write(db_session, [await _opened(monkeypatch, f"https://aloe.az/{_SMALL}")])

    assert _ids(db_session) == {cut.id: _SMALL}


def test_cut_row_goes_to_the_product_whose_address_it_carries(db_session) -> None:
    # Обрезок у двух товаров общий; в строке записан адрес большого.
    stored = _product(_LARGE[:_CUT], _address(_LARGE), name="Krem-gel 400 ml")
    db_session.add(stored)
    db_session.commit()

    _write(
        db_session, _listed((_SMALL, "Krem-gel 250 ml", 26.0), (_LARGE, "Krem-gel 400 ml", 38.5))
    )

    ids = _ids(db_session)
    assert ids[stored.id] == _LARGE
    assert sorted(ids.values()) == sorted([_SMALL, _LARGE])
    assert _prices(db_session, _SMALL) == [26.0]
    assert _prices(db_session, _LARGE) == [38.5]


def test_rows_that_are_not_a_cut_of_the_scanned_product_stay_as_they_are(db_session) -> None:
    other_long = "e" * _CUT + "-50-ml"
    stored = [
        # Слаг ровно в сто знаков никогда не обрезался.
        _product("c" * _CUT, _address("c" * _CUT)),
        # Пара (сайт, идентификатор) уникальна: чужой сайт не в счёт.
        _product(_SMALL[:_CUT], f"https://aptekonline.az/product/{_SMALL}", site="aptekonline"),
        # Обрезок тот же, а адрес — не этого товара.
        _product(other_long[:_CUT], "https://aloe.az/catalog/filters/?category_slug=kosmetika"),
    ]
    db_session.add_all(stored)
    db_session.commit()
    before = _ids(db_session)

    with capture_logs() as logs:
        _write(
            db_session,
            _listed(
                ("c" * _CUT, "Ровно сто", 5.0),
                (_SMALL, "Krem-gel 250 ml", 26.0),
                (other_long, "Другой", 7.0),
            ),
        )

    after = _ids(db_session)
    assert {number: after[number] for number in before} == before
    assert sorted(after[number] for number in after.keys() - before.keys()) == sorted(
        [_SMALL, other_long]
    )
    assert not _adoptions(logs)


def test_cut_row_with_another_products_address_is_not_taken(db_session) -> None:
    # Обрезок общий с собранным товаром, но в строке записан адрес его соседа.
    neighbour = _product(_SMALL[:_CUT], _address(f"{_STEM}-750-ml"))
    db_session.add(neighbour)
    db_session.commit()

    _write(db_session, _listed((_SMALL, "Krem-gel 250 ml", 26.0)))

    ids = _ids(db_session)
    assert ids[neighbour.id] == _SMALL[:_CUT]
    assert sorted(ids.values()) == sorted([_SMALL[:_CUT], _SMALL])


@pytest.mark.parametrize(
    ("site", "external_id"),
    [
        # Товар другого сайта с таким же длинным слагом в адресе.
        ("aptekonline", _SMALL),
        # `ai-crawl` называет товар aloe по-своему (артикул), адрес — тот же.
        ("aloe", "82477"),
    ],
)
def test_cut_row_is_taken_only_by_the_product_named_after_its_slug(
    db_session, site: str, external_id: str
) -> None:
    cut = _product(_SMALL[:_CUT], _address(_SMALL))
    db_session.add(cut)
    db_session.commit()
    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()
    scraped = ScrapedProduct(
        site=site, external_id=external_id, url=_address(_SMALL), name="Krem-gel", price=26.0
    )

    persist_results(db_session, run, [ScrapeResult(site=site, products=[scraped])])
    db_session.commit()
    db_session.expire_all()

    stored = db_session.execute(
        select(storage.Product.site, storage.Product.external_id).order_by(storage.Product.id)
    ).all()
    assert stored == [("aloe", _SMALL[:_CUT]), (site, external_id)]


def test_cut_row_with_a_whole_slug_twin_is_left_alone(db_session) -> None:
    cut = _product(_SMALL[:_CUT], _address(_SMALL))
    twin = _product(_SMALL, _address(_SMALL))
    db_session.add_all([cut, twin])
    db_session.commit()
    before = _ids(db_session)

    _write(db_session, _listed((_SMALL, "Krem-gel 250 ml", 26.0)))

    assert _ids(db_session) == before
    assert _prices(db_session, _SMALL) == [26.0]
    assert _prices(db_session, _SMALL[:_CUT]) == []


def test_product_whose_whole_slug_equals_the_freed_cut_gets_its_own_row(db_session) -> None:
    # Строка записана под обрезком длинного слага, а на сайте есть и товар, чей
    # слаг целиком совпадает с этим обрезком. Раньше оба писались в неё.
    hundred = _SMALL[:_CUT]
    stored = _product(hundred, _address(_SMALL), name="Krem-gel 250 ml")
    db_session.add(stored)
    db_session.commit()

    _write(db_session, _listed((hundred, "Krem-gel", 12.0), (_SMALL, "Krem-gel 250 ml", 26.0)))

    ids = _ids(db_session)
    assert ids[stored.id] == _SMALL
    assert sorted(ids.values()) == sorted([hundred, _SMALL])
    assert _prices(db_session, hundred) == [12.0]
    assert _prices(db_session, _SMALL) == [26.0]


def test_record_without_an_address_is_still_refused_by_the_database(db_session) -> None:
    # Обязательное поле проверяет база: шаг перевода не должен упасть раньше неё
    # своей ошибкой и не должен такую запись пропустить.
    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()
    broken = ScrapedProduct(site="aloe", external_id=_SMALL, url=None, name="Krem-gel", price=1.0)

    with pytest.raises(IntegrityError):
        persist_results(db_session, run, [ScrapeResult(site="aloe", products=[broken])])
    db_session.rollback()


def test_scan_of_another_site_does_not_depend_on_the_aloe_module(db_session, monkeypatch) -> None:
    # На сервер попадали и неполные наборы файлов: запись aptekonline не должна
    # падать оттого, что рядом лежит прежний `scrapers/aloe.py`.
    monkeypatch.delattr(aloe, "aloe_slug_from_url")
    run = storage.Run(status="running")
    db_session.add(run)
    db_session.commit()
    scraped = ScrapedProduct(
        site="aptekonline",
        external_id="1001",
        url="https://aptekonline.az/product/1001",
        name="Aspirin",
        price=2.0,
    )

    assert persist_results(db_session, run, [ScrapeResult(site="aptekonline", products=[scraped])])
    assert list(_ids(db_session).values()) == ["1001"]
