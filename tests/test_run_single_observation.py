"""Одна запись сбора — одно наблюдение в `offer_observations`.

Команда `run` пишет товары дважды: колбэк `on_category` — сразу после каждой
категории, финальный проход — в конце. До 2026-10-07 финальный проход писал всё
заново, и на каждую запись сбора в `offer_observations` ложилось две строки:
замер на проде — 3,1 млн лишних из 6,5 млн. Снимки цен при этом не двоились
(diff-only), поэтому снаружи это было не видно.

Остальные тесты пайплайна подменяют `scrape_all`, и колбэк в них не вызывается
вовсе. Здесь сбор идёт настоящим путём — `scrape_all` → `scrape_site` →
`BaseScraper.scrape` → колбэк → `persist_results`, — подменён только сам сайт.
"""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from src import alerts, storage
from src import main as main_mod
from src._time import utcnow
from src.main import _PersistedEntries, _split_persisted, persist_results
from src.scrapers.base import BaseScraper, ScrapedProduct, ScrapedPromo, ScrapeResult


def _scraped(ext_id: str, price: float, category: str) -> ScrapedProduct:
    return ScrapedProduct(
        site="aloe",
        external_id=ext_id,
        url=f"https://aloe.invalid/p/{ext_id}",
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
        return [ScrapedPromo(site="aloe", title="Promo")]


def _session_factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)


def _run_aloe(db_session, monkeypatch, catalog, *, broken_after_yield=()) -> storage.Run:
    """Полный сбор aloe командой `run`; подменён только сайт и шаги после записи."""
    monkeypatch.setattr(_CatalogScraper, "catalog", catalog)
    monkeypatch.setattr(_CatalogScraper, "broken_after_yield", set(broken_after_yield))
    monkeypatch.setitem(main_mod.SCRAPER_CLASSES, "aloe", _CatalogScraper)
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "shadow")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    monkeypatch.setenv("SCRAPE_REPORT_EMAIL", "0")
    monkeypatch.delenv("AI_FALLBACK_ENABLED", raising=False)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: list(catalog),
    )
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: {"aloe": 1})
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
        runner.invoke(main_mod.cli, ["run", "--site", "aloe", "--mode", "category"])
    db_session.expire_all()
    return db_session.scalar(select(storage.Run).order_by(storage.Run.id.desc()))


def _observations(db_session, run: storage.Run) -> dict[str, int]:
    """Сколько наблюдений прогон оставил на каждый товар."""
    rows = db_session.execute(
        select(storage.Product.external_id, func.count(storage.OfferObservation.id))
        .join(storage.OfferObservation, storage.OfferObservation.product_id == storage.Product.id)
        .where(storage.OfferObservation.run_id == run.id)
        .group_by(storage.Product.external_id)
    ).all()
    return dict(rows)


def _seen_earlier_at(db_session, ext_id: str, price: float) -> storage.PriceSnapshot:
    """Товар уже видели раньше по этой цене: полный сбор новой строки цены не запишет."""
    tick = storage.Run(status="ok", catalog_scope="partial", started_at=utcnow())
    db_session.add(tick)
    db_session.commit()
    persist_results(
        db_session, tick, [ScrapeResult(site="aloe", products=[_scraped(ext_id, price, "old")])]
    )
    return db_session.scalar(
        select(storage.PriceSnapshot).order_by(storage.PriceSnapshot.id.desc())
    )


# ─── Пайплайн ────────────────────────────────────────────────────────────────


def test_full_scan_records_one_observation_per_scraped_entry(db_session, monkeypatch):
    """Товар `both` стоит в двух категориях — это две записи сбора и два наблюдения.
    До правки было вдвое больше: шесть строк на три записи."""
    seen_before = _seen_earlier_at(db_session, "both", 10.0)
    catalog = {
        "cat-a": [_scraped("both", 10.0, "cat-a"), _scraped("only-a", 5.0, "cat-a")],
        "cat-b": [_scraped("both", 10.0, "cat-b")],
    }

    run = _run_aloe(db_session, monkeypatch, catalog)

    assert (run.status, run.catalog_verified) == ("ok", True), run.error_message
    assert _observations(db_session, run) == {"both": 2, "only-a": 1}
    assert run.products_scraped == 3, "счётчик прогона — записи сбора, как и раньше"
    assert run.products_per_site == {"aloe": 3}

    snapshots = db_session.scalars(
        select(storage.PriceSnapshot).where(storage.PriceSnapshot.run_id == run.id)
    ).all()
    assert [snap.price for snap in snapshots] == [5.0], "цена `both` прежняя — строки нет"
    promos = db_session.scalars(select(storage.Promo).where(storage.Promo.run_id == run.id)).all()
    assert [promo.title for promo in promos] == ["Promo"], "промо пишет только финальный проход"

    # Категории идут по порядку, последняя и остаётся у товара — как при повторной
    # записи в конце.
    both = db_session.scalar(select(storage.Product).where(storage.Product.external_id == "both"))
    assert both.category == "cat-b"

    # Наблюдение не раньше записи цены, с которой товар сравнили: по этому
    # условию проверенный сбор находит цену, которую он подтверждает.
    observed_at = db_session.scalars(
        select(storage.OfferObservation.observed_at).where(
            storage.OfferObservation.run_id == run.id,
            storage.OfferObservation.product_id == both.id,
        )
    ).all()
    assert min(observed_at) >= seen_before.captured_at


def test_final_pass_writes_the_category_whose_incremental_write_failed(db_session, monkeypatch):
    """Колбэк упал на `cat-b` — её записи обязан дописать финальный проход, включая
    товар, который уже записан из другой категории: это отдельная запись сбора."""
    catalog = {
        "cat-a": [_scraped("both", 10.0, "cat-a")],
        "cat-b": [_scraped("both", 10.0, "cat-b"), _scraped("only-b", 7.0, "cat-b")],
    }
    failed_once: list[str] = []

    def flaky_persist(session, run, results):
        categories = {product.category for result in results for product in result.products}
        if categories == {"cat-b"} and not failed_once:
            failed_once.append("cat-b")
            raise RuntimeError("database went away")
        return persist_results(session, run, results)

    monkeypatch.setattr(main_mod, "persist_results", flaky_persist)

    run = _run_aloe(db_session, monkeypatch, catalog)

    assert failed_once == ["cat-b"]
    assert _observations(db_session, run) == {"both": 2, "only-b": 1}
    assert run.products_scraped == 3
    only_b = db_session.scalar(
        select(storage.PriceSnapshot)
        .join(storage.Product, storage.Product.id == storage.PriceSnapshot.product_id)
        .where(storage.Product.external_id == "only-b")
    )
    assert (only_b.run_id, only_b.price) == (run.id, 7.0)


def test_final_pass_writes_products_of_a_category_that_broke_midway(db_session, monkeypatch):
    """Категория отдала товары и оборвалась: колбэк для неё не вызывается, товары
    остаются в результате сайта и доходят до базы только финальным проходом."""
    catalog = {
        "cat-a": [_scraped("only-a", 5.0, "cat-a")],
        "cat-b": [_scraped("only-b", 7.0, "cat-b")],
    }

    run = _run_aloe(db_session, monkeypatch, catalog, broken_after_yield={"cat-b"})

    assert run.status == "degraded"
    assert _observations(db_session, run) == {"only-a": 1, "only-b": 1}
    assert run.products_scraped == 2


def test_scan_without_incremental_writes_is_unchanged(db_session, monkeypatch):
    """Путь без колбэка (public_api, legacy bridge, подменённый сборщик): всё пишет
    финальный проход, по одному наблюдению на запись."""
    catalog = {"cat-a": [_scraped("only-a", 5.0, "cat-a")], "cat-b": [_scraped("b", 7.0, "cat-b")]}
    calls: list[int] = []

    async def scrape_without_callback(slugs_by_site, limit, **kwargs):
        calls.append(1)
        return [await main_mod.scrape_site("aloe", slugs_by_site["aloe"], limit)]

    monkeypatch.setattr(main_mod, "scrape_all", scrape_without_callback)

    run = _run_aloe(db_session, monkeypatch, catalog)

    assert calls == [1]
    assert (run.status, run.catalog_verified) == ("ok", True), run.error_message
    assert _observations(db_session, run) == {"only-a": 1, "b": 1}
    assert run.products_scraped == 2


# ─── Отбор остатка ───────────────────────────────────────────────────────────


def test_entries_are_told_apart_by_object_not_by_product():
    """Две записи одного товара равны как данные, но это разные наблюдения."""
    written = _scraped("both", 10.0, "cat")
    same_product_again = _scraped("both", 10.0, "cat")
    assert written == same_product_again
    persisted = _PersistedEntries()
    persisted.add([written])
    promo = ScrapedPromo(site="aloe", title="Promo")
    result = ScrapeResult(site="aloe", products=[written, same_product_again], promos=[promo])

    pending, already_persisted = _split_persisted([result], persisted)

    assert already_persisted == 1
    assert len(pending) == 1
    assert pending[0].products[0] is same_product_again
    assert pending[0].promos == [promo]
    assert result.products == [written, same_product_again], "исходный результат не тронут"


def test_site_with_nothing_left_still_hands_its_promos_to_the_final_pass():
    product = _scraped("only", 10.0, "cat")
    persisted = _PersistedEntries()
    persisted.add([product])
    promo = ScrapedPromo(site="aloe", title="Promo")

    pending, already_persisted = _split_persisted(
        [ScrapeResult(site="aloe", products=[product], promos=[promo])], persisted
    )

    assert already_persisted == 1
    assert (pending[0].products, pending[0].promos) == ([], [promo])
