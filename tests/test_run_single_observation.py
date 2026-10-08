"""Одна запись сбора — одно наблюдение в `offer_observations`.

Команда `run` отдаёт каждую запись в `persist_results` дважды: колбэк
`on_category` — сразу после категории, финальный проход — в конце целиком. До
2026-10-07 оба вызова добавляли строку наблюдения, и на каждую запись сбора в
`offer_observations` ложилось две: замер на проде — 3,1 млн лишних из 6,5 млн.
Снимки цен при этом не двоились (diff-only), поэтому снаружи это было не видно.

Финальный проход остался: он пишет то, что мимо колбэка прошло, и делает это в
порядке категорий. Повторной строки наблюдения он больше не добавляет.

Остальные тесты пайплайна подменяют `scrape_all`, и колбэк в них не вызывается
вовсе. Здесь сбор идёт настоящим путём — `scrape_all` → `scrape_site` →
`BaseScraper.scrape` → колбэк → `persist_results`, — подменён только сам сайт.
"""

from __future__ import annotations

import asyncio
import gc
import weakref
from types import SimpleNamespace

from click.testing import CliRunner
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from src import alerts, storage
from src import main as main_mod
from src._time import utcnow
from src.main import _ObservedEntries, persist_results
from src.scrapers.base import BaseScraper, ScrapedProduct, ScrapedPromo, ScrapeResult


def _scraped(ext_id: str, price: float, category: str, site: str = "aloe") -> ScrapedProduct:
    return ScrapedProduct(
        site=site,
        external_id=ext_id,
        url=f"https://{site}.invalid/p/{ext_id}",
        name=f"Product {ext_id}",
        price=price,
        category=category,
        manufacturer_country_raw="Latvia",
        country_source="fixture",
        offer_availability_status="in_stock",
        offer_quantity=1,
        availability_source="fixture",
    )


class _CatalogScraper(BaseScraper):
    """Сайт из заданных категорий; `scrape()` и вызов колбэка — настоящие."""

    site_name = "aloe"
    base_url = "https://aloe.invalid"
    catalog: dict[str, list[ScrapedProduct]] = {}
    # Категории, которые обрываются исключением после выдачи своих товаров.
    broken_after_yield: set[str] = set()

    def __init__(self, country_id_map=None):
        super().__init__()
        self.verified_country_mappings = {}
        self.fetch_retries = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def scrape_category(self, category_slug: str, limit: int | None = None):
        products = type(self).catalog[category_slug]
        for product in products:
            # Отдаём управление циклу: сайты одного прогона собираются вперемешку.
            await asyncio.sleep(0)
            yield product
        if category_slug in type(self).broken_after_yield:
            raise RuntimeError("listing page failed")
        self._set_route_status(
            category_slug,
            complete=True,
            raw_items=len(products),
            parsed_items=len(products),
            expected_items=len(products),
        )

    async def scrape_promos(self):
        return [ScrapedPromo(site=self.site_name, title="Promo")]


class _SecondSiteScraper(_CatalogScraper):
    """Второй сайт того же прогона — со своим каталогом."""

    site_name = "aptekonline"
    base_url = "https://aptekonline.invalid"
    catalog: dict[str, list[ScrapedProduct]] = {}
    broken_after_yield: set[str] = set()


def _session_factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)


def _run_aloe(
    db_session,
    monkeypatch,
    catalog,
    *,
    broken_after_yield=(),
    verified: bool = True,
    second_site_catalog=None,
    expected_status: str | None = None,
    command_line: tuple[str, ...] = ("run", "--mode", "category"),
) -> storage.Run:
    """Полный сбор aloe командой `run`; подменён только сайт и шаги после записи.

    `verified=False` — сбор, который не должен пройти проверку каталога.
    `second_site_catalog` — в том же прогоне собирается ещё и aptekonline.
    `expected_status` — чем прогон обязан кончиться, если не `ok` и не `degraded`.
    `command_line` — команда и её параметры; сайты подставляются сами.
    """
    catalogs = {"aloe": catalog}
    if second_site_catalog is not None:
        catalogs["aptekonline"] = second_site_catalog
        monkeypatch.setattr(_SecondSiteScraper, "catalog", second_site_catalog)
        monkeypatch.setitem(main_mod.SCRAPER_CLASSES, "aptekonline", _SecondSiteScraper)
    monkeypatch.setattr(_CatalogScraper, "catalog", catalog)
    monkeypatch.setattr(_CatalogScraper, "broken_after_yield", set(broken_after_yield))
    monkeypatch.setitem(main_mod.SCRAPER_CLASSES, "aloe", _CatalogScraper)
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "shadow")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    monkeypatch.setenv("SCRAPE_REPORT_EMAIL", "0")
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: list(catalogs[site]),
    )
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: dict.fromkeys(catalogs, 1))
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: None)
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})
    monkeypatch.setattr(main_mod, "_smoke_test_per_site_coverage", lambda *args: None)
    monkeypatch.setattr(main_mod.matcher, "refresh_derived_fields", lambda session: 0)
    monkeypatch.setattr(main_mod.matcher, "relink_stale_members", lambda session: 0)
    monkeypatch.setattr(main_mod.matcher, "match_products", lambda session: 0)
    monkeypatch.setattr(main_mod.matcher, "revalidate_split", lambda session: [])
    monkeypatch.setattr(main_mod.matcher, "flag_suspected_mismatches", lambda session: 0)
    monkeypatch.setattr(alerts, "evaluate_rules", lambda *args: [])
    monkeypatch.setattr(
        main_mod.analyzer, "analyze", lambda *args: SimpleNamespace(run_started_at=utcnow())
    )
    monkeypatch.setattr(main_mod.reporter, "render_html", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "render_excel", lambda report: b"ok")
    monkeypatch.setattr(main_mod.reporter, "email_subject", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "excel_filename", lambda report: "ok.xlsx")

    runner = CliRunner()
    with runner.isolated_filesystem():
        site_args = [arg for site in catalogs for arg in ("--site", site)]
        result = runner.invoke(main_mod.cli, [command_line[0], *site_args, *command_line[1:]])
    db_session.expire_all()
    run = db_session.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
    status = expected_status or ("ok" if verified else "degraded")
    assert (result.exit_code == 0) is (status == "ok"), result.output
    assert run.status == status, run.error_message
    if expected_status is None:
        assert bool(run.catalog_verified) is verified, run.error_message
    return run


def _observations(db_session, run: storage.Run) -> dict[str, int]:
    """Сколько наблюдений прогон оставил на каждый товар."""
    rows = db_session.execute(
        select(storage.Product.external_id, func.count(storage.OfferObservation.id))
        .join(storage.OfferObservation, storage.OfferObservation.product_id == storage.Product.id)
        .where(storage.OfferObservation.run_id == run.id)
        .group_by(storage.Product.external_id)
    ).all()
    return dict(rows)


def _product(db_session, ext_id: str) -> storage.Product:
    return db_session.scalar(select(storage.Product).where(storage.Product.external_id == ext_id))


def _new_run(db_session, scope: str = "partial") -> storage.Run:
    run = storage.Run(status="ok", catalog_scope=scope, started_at=utcnow())
    db_session.add(run)
    db_session.commit()
    return run


def _seen_earlier_at(db_session, ext_id: str, price: float) -> storage.PriceSnapshot:
    """Товар уже видели раньше по этой цене: полный сбор новой строки цены не запишет."""
    persist_results(
        db_session,
        _new_run(db_session),
        [ScrapeResult(site="aloe", products=[_scraped(ext_id, price, "old")])],
    )
    return db_session.scalar(
        select(storage.PriceSnapshot).order_by(storage.PriceSnapshot.id.desc())
    )


# ─── Пайплайн ────────────────────────────────────────────────────────────────


def test_full_scan_records_one_observation_per_scraped_entry(db_session, monkeypatch):
    """Товар `both` стоит в двух категориях — это две записи сбора и два наблюдения.
    До правки было вдвое больше: шесть строк на три записи."""
    _seen_earlier_at(db_session, "both", 10.0)
    catalog = {
        "cat-a": [_scraped("both", 10.0, "cat-a"), _scraped("only-a", 5.0, "cat-a")],
        "cat-b": [_scraped("both", 10.0, "cat-b")],
    }

    run = _run_aloe(db_session, monkeypatch, catalog)

    assert _observations(db_session, run) == {"both": 2, "only-a": 1}
    assert run.products_scraped == 3, "счётчик прогона — записи сбора, как и раньше"
    assert run.products_per_site == {"aloe": 3}

    snapshots = db_session.scalars(
        select(storage.PriceSnapshot).where(storage.PriceSnapshot.run_id == run.id)
    ).all()
    assert [snap.price for snap in snapshots] == [5.0], "цена `both` прежняя — строки нет"
    promos = db_session.scalars(select(storage.Promo).where(storage.Promo.run_id == run.id)).all()
    assert [promo.title for promo in promos] == ["Promo"]
    assert _product(db_session, "both").category == "cat-b", "остаётся последняя категория"


def test_category_larger_than_one_write_batch_is_observed_once(db_session, monkeypatch):
    """Категория больше одной пачки записи: наблюдение по-прежнему одно на запись."""
    monkeypatch.setattr(main_mod, "_PERSIST_CHUNK", 2)
    catalog = {"cat-a": [_scraped(f"p{n}", 1.0 + n, "cat-a") for n in range(5)]}

    run = _run_aloe(db_session, monkeypatch, catalog)

    assert _observations(db_session, run) == {f"p{n}": 1 for n in range(5)}
    assert run.products_scraped == 5


def test_write_that_fails_midway_keeps_what_it_already_recorded(db_session, monkeypatch):
    """Запись категории упала на второй пачке, первая уже в базе вместе с
    наблюдением. Финальный проход добавляет наблюдение только второй: учёт
    ведётся по закоммиченным пачкам, а не по вызовам."""
    from src import normalize

    monkeypatch.setattr(main_mod, "_PERSIST_CHUNK", 1)
    catalog = {"cat-a": [_scraped("first", 5.0, "cat-a"), _scraped("second", 7.0, "cat-a")]}
    real_normalize_name = normalize.normalize_name
    failed_once: list[str] = []

    def flaky_normalize_name(name):
        if name == "Product second" and not failed_once:
            failed_once.append(name)
            raise RuntimeError("normalization failed")
        return real_normalize_name(name)

    monkeypatch.setattr(normalize, "normalize_name", flaky_normalize_name)

    run = _run_aloe(db_session, monkeypatch, catalog)

    assert failed_once == ["Product second"]
    assert _observations(db_session, run) == {"first": 1, "second": 1}
    assert run.products_scraped == 2


def test_final_pass_observes_the_category_whose_incremental_write_failed(db_session, monkeypatch):
    """Колбэк упал на `cat-b` до обращения к базе — наблюдения её записей добавляет
    финальный проход. Товар `both` уже учтён по `cat-a`, но запись из `cat-b` —
    отдельная, и своё наблюдение она получает."""
    catalog = {
        "cat-a": [_scraped("both", 10.0, "cat-a")],
        "cat-b": [_scraped("both", 10.0, "cat-b"), _scraped("only-b", 7.0, "cat-b")],
    }
    failed_once: list[str] = []

    def flaky_persist(session, run, results, **kwargs):
        categories = {product.category for result in results for product in result.products}
        if categories == {"cat-b"} and not failed_once:
            failed_once.append("cat-b")
            raise RuntimeError("category write failed")
        return persist_results(session, run, results, **kwargs)

    monkeypatch.setattr(main_mod, "persist_results", flaky_persist)

    run = _run_aloe(db_session, monkeypatch, catalog)

    assert failed_once == ["cat-b"]
    assert _observations(db_session, run) == {"both": 2, "only-b": 1}
    assert run.products_scraped == 3
    only_b = db_session.scalar(
        select(storage.PriceSnapshot).where(
            storage.PriceSnapshot.product_id == _product(db_session, "only-b").id
        )
    )
    assert (only_b.run_id, only_b.price) == (run.id, 7.0)


def test_category_that_broke_midway_is_written_in_category_order(db_session, monkeypatch):
    """Раздел отдал товары и оборвался: колбэк для него не вызывается, товары
    доходят до базы только финальным проходом. Так на проде регулярно обрывается
    общий раздел aloe `dermanlar`, который идёт раньше точных подразделов.

    Финальный проход идёт по порядку разделов, поэтому у товара из обоих остаётся
    категория позднего, точного — а не оборвавшегося общего, хоть тот и записан
    последним по времени.
    """
    catalog = {
        "broad": [_scraped("both", 10.0, "broad"), _scraped("only-broad", 5.0, "broad")],
        "exact": [_scraped("both", 10.0, "exact")],
    }

    run = _run_aloe(db_session, monkeypatch, catalog, broken_after_yield={"broad"}, verified=False)

    assert _observations(db_session, run) == {"both": 2, "only-broad": 1}
    assert run.products_scraped == 3
    assert _product(db_session, "both").category == "exact"


def test_products_added_by_the_fallback_crawler_are_observed_once(db_session, monkeypatch):
    """Запасной сборщик дописывает товары в результат сайта после всех категорий —
    мимо колбэка."""

    class _Fallback:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def crawl(self, max_urls: int):
            return ScrapeResult(site="aloe", products=[_scraped("from-fallback", 4.0, "cat-a")])

    monkeypatch.setitem(main_mod.AI_CRAWLER_BY_SITE, "aloe", _Fallback)
    monkeypatch.setattr(main_mod, "_should_trigger_ai_fallback", lambda *args, **kwargs: True)
    catalog = {"cat-a": [_scraped("only-a", 5.0, "cat-a")]}

    run = _run_aloe(db_session, monkeypatch, catalog)

    assert _observations(db_session, run) == {"only-a": 1, "from-fallback": 1}
    assert run.products_scraped == 2


def test_two_sites_in_one_run_share_the_tracking(db_session, monkeypatch):
    """Сайты одного прогона собираются одновременно и пишут через один колбэк с
    общим учётом: записи одного сайта не должны сойти за учтённые записи другого."""
    aloe = {"cat-a": [_scraped("a1", 5.0, "cat-a"), _scraped("a2", 6.0, "cat-a")]}
    aptekonline = {
        "cat-x": [_scraped("x1", 7.0, "cat-x", "aptekonline")],
        "cat-y": [
            _scraped("x1", 7.0, "cat-y", "aptekonline"),
            _scraped("x2", 8.0, "cat-y", "aptekonline"),
        ],
    }

    run = _run_aloe(db_session, monkeypatch, aloe, second_site_catalog=aptekonline)

    assert _observations(db_session, run) == {"a1": 1, "a2": 1, "x1": 2, "x2": 1}
    assert run.products_scraped == 5
    assert run.products_per_site == {"aloe": 2, "aptekonline": 3}


def test_scan_without_incremental_writes_is_unchanged(db_session, monkeypatch):
    """Путь без колбэка (public_api, legacy bridge): всё пишет финальный проход,
    по одному наблюдению на запись."""
    catalog = {"cat-a": [_scraped("only-a", 5.0, "cat-a")], "cat-b": [_scraped("b", 7.0, "cat-b")]}
    calls: list[int] = []

    async def scrape_without_callback(slugs_by_site, limit, **kwargs):
        calls.append(1)
        return [await main_mod.scrape_site("aloe", slugs_by_site["aloe"], limit)]

    monkeypatch.setattr(main_mod, "scrape_all", scrape_without_callback)

    run = _run_aloe(db_session, monkeypatch, catalog)

    assert calls == [1]
    assert _observations(db_session, run) == {"only-a": 1, "b": 1}
    assert run.products_scraped == 2


def test_verified_scan_still_confirms_the_price_it_saw_unchanged(db_session, monkeypatch):
    """Подтверждение цен ищет запись по моменту последнего наблюдения товара в
    прогоне. Раньше последним было наблюдение финального прохода; теперь оно
    одно на запись сбора и делается вызовом колбэка."""
    same = _seen_earlier_at(db_session, "same", 10.0)
    changed = _seen_earlier_at(db_session, "changed", 10.0)
    catalog = {
        "cat-a": [_scraped("same", 10.0, "cat-a"), _scraped("changed", 8.0, "cat-a")],
        "cat-b": [_scraped("same", 10.0, "cat-b"), _scraped("new", 3.0, "cat-b")],
    }

    run = _run_aloe(db_session, monkeypatch, catalog)

    assert _observations(db_session, run) == {"same": 2, "changed": 1, "new": 1}
    rows = [
        (snap.run_id, snap.price, snap.confirmed_run_id)
        for snap in db_session.scalars(
            select(storage.PriceSnapshot).order_by(storage.PriceSnapshot.id)
        )
    ]
    assert rows == [
        (same.run_id, 10.0, run.id),  # цена прежняя — подтверждена проверенным сбором
        (changed.run_id, 10.0, None),  # сбор увидел другую цену — прежнюю не подтверждает
        (run.id, 8.0, None),
        (run.id, 3.0, None),
    ]
    ids = dict(db_session.execute(select(storage.Product.external_id, storage.Product.id)).all())
    trusted = storage.latest_prices_per_product(
        db_session, list(ids.values()), financially_eligible_only=True
    )
    assert {ext_id: trusted[pid][0] for ext_id, pid in ids.items()} == {
        "same": 10.0,
        "changed": 8.0,
        "new": 3.0,
    }


# ─── `persist_results` ───────────────────────────────────────────────────────


def _observation_count(db_session, run: storage.Run) -> int:
    return db_session.scalar(
        select(func.count(storage.OfferObservation.id)).where(
            storage.OfferObservation.run_id == run.id
        )
    )


def test_repeated_entry_is_observed_once_but_its_product_is_still_updated(db_session):
    """Повторный проход по уже учтённой записи: строки наблюдения нет, а карточка
    товара обновляется как обычно — на этом держится порядок категорий."""
    run = _new_run(db_session, "full")
    entry = _scraped("one", 10.0, "first")
    observed = _ObservedEntries()
    persist_results(
        db_session, run, [ScrapeResult(site="aloe", products=[entry])], observed=observed
    )
    first_pass = _product(db_session, "one")
    stamped_by_first_pass = (first_pass.availability_observed_at, first_pass.last_seen_at)

    entry.category = "second"
    count = persist_results(
        db_session, run, [ScrapeResult(site="aloe", products=[entry])], observed=observed
    )

    assert count == 1, "счётчик считает записи сбора, а не строки наблюдений"
    assert _observation_count(db_session, run) == 1
    product = _product(db_session, "one")
    assert product.category == "second"
    assert product.availability_observed_at > stamped_by_first_pass[0]
    assert product.last_seen_at > stamped_by_first_pass[1]


def test_entries_are_told_apart_by_object_not_by_product(db_session):
    """Две записи одного товара равны как данные, но это разные наблюдения."""
    run = _new_run(db_session, "full")
    written = _scraped("both", 10.0, "cat")
    same_product_again = _scraped("both", 10.0, "cat")
    assert written == same_product_again
    observed = _ObservedEntries()
    persist_results(
        db_session, run, [ScrapeResult(site="aloe", products=[written])], observed=observed
    )

    persist_results(
        db_session,
        run,
        [ScrapeResult(site="aloe", products=[written, same_product_again])],
        observed=observed,
    )

    assert _observation_count(db_session, run) == 2


def test_without_tracking_every_call_records_an_observation(db_session):
    """Частичные тики, `scrape` и `ai-crawl` учёт не передают: один вызов на
    прогон, наблюдение на каждую запись."""
    run = _new_run(db_session)
    entry = _scraped("one", 10.0, "cat")

    persist_results(db_session, run, [ScrapeResult(site="aloe", products=[entry])])
    persist_results(db_session, run, [ScrapeResult(site="aloe", products=[entry])])

    assert _observation_count(db_session, run) == 2


def test_tracked_entries_are_kept_alive():
    """Учёт идёт по адресу объекта. Освобождённый адрес Python отдаёт следующему
    объекту, и без ссылки чужая запись сошла бы за уже учтённую."""
    entry = _scraped("one", 10.0, "cat")
    still_alive = weakref.ref(entry)
    observed = _ObservedEntries()
    observed.add([entry])

    del entry
    gc.collect()

    assert still_alive() is not None
    assert still_alive() in observed
    assert _scraped("one", 10.0, "cat") not in observed
