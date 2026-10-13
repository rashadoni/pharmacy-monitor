"""Сбой базы посреди сбора: прогон закрывается с причиной, остальное пишется.

Команда `run` пишет каждую категорию сразу после сбора — колбэком из
`BaseScraper.scrape`, который ловит исключение колбэка и идёт дальше, — а в
конце проходит по собранному ещё раз. После ошибки базы транзакция сессии
требует отката, и до этой правки его никто не делал:

* колбэк каждой следующей категории и финальный проход падали на той же сессии
  (`PendingRollbackError`; на PostgreSQL, если отказал запрос мимо flush, —
  `InFailedSqlTransaction`);
* обработчик сбоя падал на собственном `commit`, и строка прогона оставалась
  `running` без `finished_at` — пока часов через шесть её не закрывал вотчер
  очереди, задним числом и без причины.

Теперь недописанную пачку откатывает сама `persist_results`, а обработчики сбоя
`run`, `scrape` и `ai-crawl` перед записью итога откатывают сессию; поля,
выставленные на прогоне прямо перед исключением, переносятся через откат (кроме
случая, когда упал flush: тогда их уже сбросил сам SQLAlchemy).

Сбои здесь настоящие: отказывает сама база, посреди записи, а не код до
обращения к ней. Сценарии идут на SQLite и на PostgreSQL; три — только на
PostgreSQL (отложенное ограничение, обрыв соединения сервером, длина строки).
Прод — PostgreSQL, и после ошибки запроса он не выполняет в транзакции больше
ничего, а SQLite продолжает как ни в чём не бывало. Поэтому в CI вариант на
PostgreSQL обязан исполняться: его пропуск — ошибка.

Сбор идёт настоящим путём — `scrape_all` → `scrape_site` → `BaseScraper.scrape`
→ колбэк → `persist_results`; подменены только сам сайт и шаги после записи.
Движок — тот же, что у приложения (`create_engine_hiding_statement_data`):
причина сбоя ложится в `runs.error_message`, и данных запроса в ней быть не
должно.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import alerts, storage
from src import main as main_mod
from src._time import utcnow
from src.scrapers import ai_crawler
from src.scrapers import base as scraper_base
from src.scrapers.base import BaseScraper, ScrapedProduct, ScrapedPromo, ScrapeResult

# Запись итога в сессию, чью транзакцию уже отменил упавший flush, SQLAlchemy
# молча отбросил бы с этим предупреждением.
pytestmark = pytest.mark.filterwarnings(
    "error:Session's state has been changed on a non-active transaction"
)

# Сайт с колбэком по категориям. У aloe колбэка нет: он пишется целиком в конце
# сбора (см. `_persist_category` в `run_cmd`).
SITE = "aptekonline"


def _postgres_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql"):
        return url
    if os.environ.get("CI"):
        pytest.fail(
            "в CI сбой базы посреди сбора обязан проверяться на PostgreSQL, а "
            "DATABASE_URL — не PostgreSQL: прерванную транзакцию, из-за которой "
            "прогон оставался `running`, SQLite не воспроизводит."
        )
    pytest.skip("PostgreSQL DATABASE_URL is required")


@pytest.fixture(params=["sqlite", "postgresql"])
def db_session(request, monkeypatch):
    """Сессия на движке приложения; на PostgreSQL — в своей схеме на тест."""
    if request.param == "sqlite":
        engine = storage.create_engine_hiding_statement_data(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        storage.Base.metadata.create_all(engine)
        try:
            with sessionmaker(engine, expire_on_commit=False, autoflush=False)() as session:
                yield session
        finally:
            engine.dispose()
        return

    url = _postgres_url()
    # Замок сбора — один на всю базу, а не на схему: два прогона этих тестов на
    # одной базе мешали бы друг другу. На SQLite замка нет вовсе.
    monkeypatch.setattr(
        main_mod, "_hold_scrape_lock_until_command_exit", lambda factory, *, wait: True
    )
    # Своя схема на тест: общая база CI уже размечена миграциями.
    schema = f"persist_failure_{uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = storage.create_engine_hiding_statement_data(
        url,
        # lock_timeout: ожидание замка, которого быть не должно, роняет тест, а
        # не вешает его (замок сопоставления тоже один на базу).
        connect_args={"options": f"-csearch_path={schema} -clock_timeout=5000"},
        pool_pre_ping=True,  # как на проде, `storage.make_engine`
    )
    try:
        storage.Base.metadata.create_all(engine)
        with sessionmaker(engine, expire_on_commit=False, autoflush=False)() as session:
            yield session
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _is_postgresql(db_session) -> bool:
    return db_session.get_bind().dialect.name == "postgresql"


def _session_factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)


# ─── Сайт и прогон ───────────────────────────────────────────────────────────


def _scraped(ext_id: str, price: float, category: str, site: str = SITE) -> ScrapedProduct:
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


def _without_url(ext_id: str, price: float, category: str, site: str = SITE) -> ScrapedProduct:
    """Запись, которую база не примет никогда: `products.url` — NOT NULL."""
    entry = _scraped(ext_id, price, category, site)
    entry.url = None
    return entry


class _CatalogScraper(BaseScraper):
    """Сайт из заданных категорий; `scrape()` и вызов колбэка — настоящие."""

    site_name = SITE
    base_url = f"https://{SITE}.invalid"
    catalog: dict[str, list[ScrapedProduct]] = {}

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
            await asyncio.sleep(0)
            yield product
        self._set_route_status(
            category_slug,
            complete=True,
            raw_items=len(products),
            parsed_items=len(products),
            expected_items=len(products),
        )

    async def scrape_promos(self):
        return [ScrapedPromo(site=self.site_name, title="Promo")]


def _run_catalog(
    db_session,
    monkeypatch,
    catalog,
    *,
    site: str = SITE,
    expected_status: str = "ok",
    command_line: tuple[str, ...] = ("run", "--mode", "category"),
    revalidate_split=lambda session: [],
):
    """Команда сбора над сайтом из `catalog`; возвращает (прогон, итог команды).

    Подменены сам сайт и шаги после записи; запись сбора и обработчики сбоя —
    настоящие. `revalidate_split` — чем заменить перепроверку пар (шаг под
    замком сопоставления).
    """
    scraper = type(
        "_Site",
        (_CatalogScraper,),
        {"site_name": site, "base_url": f"https://{site}.invalid", "catalog": catalog},
    )
    monkeypatch.setitem(main_mod.SCRAPER_CLASSES, site, scraper)
    for name in ("PHARMONLINE_PUBLIC_API", "PHARMONLINE_LEGACY_ID_BRIDGE", "AI_FALLBACK_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "shadow")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    monkeypatch.setenv("SCRAPE_REPORT_EMAIL", "0")
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site_name, only_category_id=None: list(catalog),
    )
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: {site: 1})
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})
    monkeypatch.setattr(main_mod, "_smoke_test_per_site_coverage", lambda *args: None)
    monkeypatch.setattr(main_mod.matcher, "refresh_derived_fields", lambda session: 0)
    monkeypatch.setattr(main_mod.matcher, "relink_stale_members", lambda session: 0)
    monkeypatch.setattr(main_mod.matcher, "replace_price_strangers", lambda session: 0)
    monkeypatch.setattr(main_mod.matcher, "match_products", lambda session: 0)
    monkeypatch.setattr(main_mod.matcher, "revalidate_split", revalidate_split)
    monkeypatch.setattr(main_mod.matcher, "flag_suspected_mismatches", lambda session: 0)
    monkeypatch.setattr(alerts, "evaluate_rules", lambda *args: [])
    monkeypatch.setattr(
        main_mod.analyzer, "analyze", lambda *args: SimpleNamespace(run_started_at=utcnow())
    )
    monkeypatch.setattr(main_mod.reporter, "render_html", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "render_excel", lambda report: b"ok")
    monkeypatch.setattr(main_mod.reporter, "email_subject", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "excel_filename", lambda report: "ok.xlsx")

    # Общее соединение SQLite не должно нести в команду чужую транзакцию.
    db_session.commit()
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main_mod.cli, [*command_line, "--site", site])
    db_session.rollback()
    db_session.expire_all()
    run = db_session.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
    assert run is not None, result.output
    assert run.status == expected_status, (run.error_message, result.output, result.exception)
    assert (result.exit_code == 0) is (expected_status == "ok"), result.output
    return run, result


def _new_run(db_session, scope: str = "partial") -> storage.Run:
    run = storage.Run(status="ok", catalog_scope=scope, started_at=utcnow(), finished_at=utcnow())
    db_session.add(run)
    db_session.commit()
    return run


def _seen_earlier_at(db_session, ext_id: str, price: float) -> None:
    """Товар уже видели раньше по этой цене: у него есть и карточка, и наблюдение."""
    main_mod.persist_results(
        db_session,
        _new_run(db_session),
        [ScrapeResult(site=SITE, products=[_scraped(ext_id, price, "old")])],
    )


def _product(db_session, ext_id: str) -> storage.Product:
    return db_session.scalar(select(storage.Product).where(storage.Product.external_id == ext_id))


def _external_ids(db_session) -> list[str]:
    return sorted(db_session.scalars(select(storage.Product.external_id)))


def _snapshot_prices(db_session, run: storage.Run) -> list[float]:
    return sorted(
        db_session.scalars(
            select(storage.PriceSnapshot.price).where(storage.PriceSnapshot.run_id == run.id)
        )
    )


def _observed(db_session, run: storage.Run) -> list[str]:
    """Товары, у которых прогон оставил хотя бы одно наблюдение."""
    return sorted(
        db_session.scalars(
            select(storage.Product.external_id)
            .join(
                storage.OfferObservation,
                storage.OfferObservation.product_id == storage.Product.id,
            )
            .where(storage.OfferObservation.run_id == run.id)
            .distinct()
        )
    )


def _record_writes(monkeypatch, before_write=None) -> list[tuple[str, str]]:
    """Какие категории писал каждый вызов `persist_results` и чем он кончился.

    Запись настоящая. `before_write(writes)` вызывается перед каждой — так тест
    «чинит» базу между двумя записями.
    """
    writes: list[tuple[str, str]] = []
    real_persist_results = main_mod.persist_results

    def recording(session, run, results, **kwargs):
        if before_write is not None:
            before_write(writes)
        categories = " ".join(
            sorted({product.category for result in results for product in result.products})
        )
        try:
            count = real_persist_results(session, run, results, **kwargs)
        except Exception as error:
            writes.append((categories, type(error).__name__))
            raise
        writes.append((categories, "ok"))
        return count

    monkeypatch.setattr(main_mod, "persist_results", recording)
    return writes


def _record_log(monkeypatch, module, *methods: str) -> list[tuple[str, dict]]:
    """События, которые модуль записал в журнал этими методами (`error`, `exception`…).

    В `handled` — первая строка исключения, которое в этот момент
    обрабатывалось: его и печатает `log.exception`.
    """
    events: list[tuple[str, dict]] = []
    real_log = module.log

    class _Recording:
        def __getattr__(self, name):
            real = getattr(real_log, name)
            if name not in methods:
                return real

            def record(event_name, **fields):
                handled = str(sys.exc_info()[1]).splitlines()[0]
                events.append((event_name, {**fields, "handled": handled}))
                return real(event_name, **fields)

            return record

    monkeypatch.setattr(module, "log", _Recording())
    return events


def _dead_log_stream(monkeypatch, *methods: str) -> None:
    """Журнал `src.main`, у которого эти методы падают, как при оборванном выводе."""
    real_log = main_mod.log

    class _DeadStream:
        def __getattr__(self, name):
            if name not in methods:
                return getattr(real_log, name)

            def broken(event_name, **fields):
                raise BrokenPipeError(32, "Broken pipe")

            return broken

    monkeypatch.setattr(main_mod, "log", _DeadStream())


# ─── Запись категории ────────────────────────────────────────────────────────


# Отказ на втором наблюдении товара. Индекс проверяется при вставке — отказывает
# flush внутри `session.commit()`. Отложенное ограничение проверяется в конце
# транзакции — отказывает сам COMMIT, когда flush уже прошёл.
_REFUSALS = {
    "flush": (
        "CREATE UNIQUE INDEX refuse_second_observation ON offer_observations (product_id)",
        "DROP INDEX refuse_second_observation",
    ),
    "commit": (
        "ALTER TABLE offer_observations ADD CONSTRAINT refuse_second_observation "
        "UNIQUE (product_id) DEFERRABLE INITIALLY DEFERRED",
        "ALTER TABLE offer_observations DROP CONSTRAINT refuse_second_observation",
    ),
}


@pytest.mark.parametrize("refused_at", ["flush", "commit"])
def test_one_refused_category_write_does_not_cost_the_rest_of_the_run(
    db_session, monkeypatch, refused_at
):
    """База один раз отказала в записи — на коммите второй пачки `cat-b`, когда
    карточка товара и строка цены этой пачки уже были отправлены.

    Первая пачка `cat-b` к этому моменту закоммичена, до третьей колбэк не
    дошёл. Следующая категория пишется как обычно, финальный проход дописывает
    отказанную пачку и ту, что шла за ней, и прогон кончается `ok`. Раньше
    после такого отказа не писалось больше ничего, а прогон оставался `running`.
    """
    if refused_at == "commit" and not _is_postgresql(db_session):
        pytest.skip("отложенное ограничение уникальности — только PostgreSQL")
    refuse, recover = _REFUSALS[refused_at]
    monkeypatch.setattr(main_mod, "_PERSIST_CHUNK", 1)
    # У `b-seen` наблюдение уже есть, и товар не новый: до коммита пачки ничего
    # не падает.
    _seen_earlier_at(db_session, "b-seen", 10.0)
    db_session.execute(text(refuse))
    db_session.commit()

    def database_recovers_after_first_refusal(writes):
        if [outcome for _, outcome in writes] == ["ok", "IntegrityError"]:
            db_session.execute(text(recover))
            db_session.commit()

    writes = _record_writes(monkeypatch, before_write=database_recovers_after_first_refusal)
    # Строка `incremental_persist_failed` — единственный след отказанной записи
    # категории на проде.
    collector_errors = _record_log(monkeypatch, scraper_base, "error")
    catalog = {
        "cat-a": [_scraped("a", 5.0, "cat-a")],
        "cat-b": [
            _scraped("b-new", 6.0, "cat-b"),
            _scraped("b-seen", 8.0, "cat-b"),
            _scraped("b-later", 9.0, "cat-b"),
        ],
        "cat-c": [_scraped("c", 7.0, "cat-c")],
    }

    run, _ = _run_catalog(db_session, monkeypatch, catalog)

    assert writes == [
        ("cat-a", "ok"),
        ("cat-b", "IntegrityError"),
        ("cat-c", "ok"),
        ("cat-a cat-b cat-c", "ok"),
    ]
    assert [(name, fields["category"]) for name, fields in collector_errors] == [
        ("incremental_persist_failed", "cat-b")
    ]
    assert _observed(db_session, run) == ["a", "b-later", "b-new", "b-seen", "c"]
    assert _snapshot_prices(db_session, run) == [5.0, 6.0, 7.0, 8.0, 9.0], "каждая цена — один раз"
    assert _product(db_session, "b-seen").category == "cat-b"
    assert run.products_scraped == 5
    assert run.finished_at is not None
    assert (run.catalog_scope, run.full_catalog_sites, run.catalog_verified) == ("full", SITE, True)


def test_connection_lost_midway_costs_one_category_write_not_the_run(db_session, monkeypatch):
    """Сервер оборвал одно соединение — то, которым шла запись `cat-b`
    (`pg_terminate_backend`). Запись категории падает, следующая берёт новое
    соединение, финальный проход дописывает `cat-b`.

    Перезапуск PostgreSQL рвёт все соединения разом, включая то, на котором
    лежит замок сбора (здесь он подменён): этого тест не изображает.
    """
    if not _is_postgresql(db_session):
        pytest.skip("обрыв соединения сервером — только PostgreSQL")
    engine = db_session.get_bind()
    admin = create_engine(engine.url, isolation_level="AUTOCOMMIT")
    about_to_write: list[str] = []

    def server_drops_the_connection(conn, cursor, statement, parameters, context, executemany):
        if about_to_write != ["cat-b"]:
            return
        about_to_write.append("dropped")
        with admin.connect() as connection:
            connection.execute(
                text("SELECT pg_terminate_backend(:pid, 5000)"),
                {"pid": conn.connection.dbapi_connection.info.backend_pid},
            )

    # После первого запроса записи: транзакция уже открыта.
    event.listen(engine, "after_cursor_execute", server_drops_the_connection)

    def second_write_begins(writes):
        if writes == [("cat-a", "ok")]:
            about_to_write.append("cat-b")

    writes = _record_writes(monkeypatch, before_write=second_write_begins)
    catalog = {
        "cat-a": [_scraped("a", 5.0, "cat-a")],
        "cat-b": [_scraped("b", 6.0, "cat-b")],
        "cat-c": [_scraped("c", 7.0, "cat-c")],
    }
    try:
        run, _ = _run_catalog(db_session, monkeypatch, catalog)
    finally:
        event.remove(engine, "after_cursor_execute", server_drops_the_connection)
        admin.dispose()

    assert about_to_write == ["cat-b", "dropped"]
    assert writes == [
        ("cat-a", "ok"),
        ("cat-b", "OperationalError"),
        ("cat-c", "ok"),
        ("cat-a cat-b cat-c", "ok"),
    ]
    assert _observed(db_session, run) == ["a", "b", "c"]
    assert _snapshot_prices(db_session, run) == [5.0, 6.0, 7.0]


def test_run_that_cannot_be_written_ends_failed_not_running(db_session, monkeypatch):
    """Запись `cat-b` база не принимает никогда. Следующая категория всё равно
    записана, а прогон, упав на финальном проходе, получает `failed`, время
    окончания и причину — раньше он оставался `running`, а `c` терялся.

    Причина — отказ базы, как его назвала база, без данных запроса: её
    показывает дашборд, а текст команды уходит в журнал сервиса.
    """
    monkeypatch.setattr(main_mod, "_PERSIST_CHUNK", 1)
    writes = _record_writes(monkeypatch)
    catalog = {
        "cat-a": [_scraped("a", 5.0, "cat-a")],
        "cat-b": [_without_url("bad", 7777.25, "cat-b")],
        "cat-c": [_scraped("c", 7.0, "cat-c")],
    }

    run, result = _run_catalog(db_session, monkeypatch, catalog, expected_status="failed")

    assert writes == [
        ("cat-a", "ok"),
        ("cat-b", "IntegrityError"),
        ("cat-c", "ok"),
        ("cat-a cat-b cat-c", "IntegrityError"),
    ]
    assert run.finished_at is not None
    assert run.error_message.startswith("IntegrityError"), run.error_message
    assert "url" in run.error_message, "причина — отказ базы, а не сессия без отката"
    # Цена — такая, какой не бывает в отметке времени строки журнала.
    for shown in (run.error_message, result.output):
        assert "Product bad" not in shown and "7777.25" not in shown, shown
    assert isinstance(result.exception, SystemExit), "команда вышла сама, а не упала"
    assert _external_ids(db_session) == ["a", "c"]
    assert _observed(db_session, run) == ["a", "c"]


def test_refusal_in_the_final_write_of_a_site_without_callback_closes_the_run(
    db_session, monkeypatch
):
    """У aloe колбэка по категориям нет: сайт пишется целиком в конце сбора, и
    отказ базы приходит прямо в финальный проход. Прогон закрывается так же.

    Финальный проход останавливается на первой отказанной пачке: то, что шло
    за ней, не пишется.
    """
    monkeypatch.setattr(main_mod, "_PERSIST_CHUNK", 1)
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: 0)
    writes = _record_writes(monkeypatch)
    catalog = {
        "cat-a": [_scraped("a", 5.0, "cat-a", "aloe")],
        "cat-b": [_without_url("bad", 6.5, "cat-b", "aloe")],
        "cat-c": [_scraped("c", 7.0, "cat-c", "aloe")],
    }

    run, _ = _run_catalog(db_session, monkeypatch, catalog, site="aloe", expected_status="failed")

    assert writes == [("cat-a cat-b cat-c", "IntegrityError")]
    assert run.finished_at is not None
    assert run.error_message.startswith("IntegrityError"), run.error_message
    assert _external_ids(db_session) == ["a"]


def test_statement_refused_midway_leaves_nothing_half_written(db_session, monkeypatch):
    """Подпись акции длиннее колонки: PostgreSQL отказывает во вставке строки
    цены, когда карточка нового товара уже отправлена, и до отката не выполняет
    в этой транзакции ничего. Сама сессия при этом считает транзакцию рабочей —
    откатывать надо, не спрашивая её.

    Это тот сбой, после которого обработчик `run` падал на собственном commit с
    `InFailedSqlTransaction`. Товар из отказанной пачки в базе не остаётся — ни
    после колбэка, ни после финального прохода; подписи в причине нет.
    """
    if not _is_postgresql(db_session):
        pytest.skip("SQLite не проверяет длину строки")
    monkeypatch.setattr(main_mod, "_PERSIST_CHUNK", 1)
    writes = _record_writes(monkeypatch)
    too_long = _scraped("b", 6.0, "cat-b")
    too_long.promo_label = "LABEL" * 41
    catalog = {
        "cat-a": [_scraped("a", 5.0, "cat-a")],
        "cat-b": [too_long],
        "cat-c": [_scraped("c", 7.0, "cat-c")],
    }

    run, result = _run_catalog(db_session, monkeypatch, catalog, expected_status="failed")

    assert writes == [
        ("cat-a", "ok"),
        ("cat-b", "DataError"),
        ("cat-c", "ok"),
        ("cat-a cat-b cat-c", "DataError"),
    ]
    assert run.finished_at is not None
    assert run.error_message.startswith("DataError"), run.error_message
    assert "InFailedSqlTransaction" not in run.error_message
    for shown in (run.error_message, result.output):
        assert "LABEL" not in shown, shown
    assert _external_ids(db_session) == ["a", "c"]
    assert _observed(db_session, run) == ["a", "c"]


def test_write_broken_by_the_code_leaves_nothing_half_written(db_session, monkeypatch):
    """Запись упала не на базе, а в коде — на второй записи пачки, когда карточка
    первой уже изменена в сессии. Раньше обработчик сбоя сохранял эту полузапись
    вместе с итогом прогона: у товара менялись категория и время «видели», а
    наблюдения и цены за ними не было."""
    _seen_earlier_at(db_session, "old", 10.0)
    before = _product(db_session, "old")
    category_before, seen_before = before.category, before.last_seen_at
    broken = _scraped("broken", 6.0, "cat-b")
    broken.name = 123  # нормализация названия падает
    writes = _record_writes(monkeypatch)
    catalog = {
        "cat-a": [_scraped("a", 5.0, "cat-a")],
        "cat-b": [_scraped("old", 8.0, "cat-b"), broken],
    }

    run, _ = _run_catalog(db_session, monkeypatch, catalog, expected_status="failed")

    assert [outcome for _, outcome in writes] == ["ok", writes[1][1], writes[1][1]]
    assert writes[1][1] != "ok"
    assert run.finished_at is not None
    old = _product(db_session, "old")
    assert (old.category, old.last_seen_at) == (category_before, seen_before)
    assert _observed(db_session, run) == ["a"]
    assert _snapshot_prices(db_session, run) == [5.0]


def test_country_dictionary_survives_a_write_that_failed(db_session):
    """Словарь стран aloe пишется прямо перед финальным проходом. Откат упавшей
    записи его не забирает: словарь коммитится сам."""
    found = ScrapeResult(
        site="aloe",
        verified_country_mappings={
            "14": {
                "country_code": "gb",
                "country_raw": "Англия",
                "source_url": "https://aloe.invalid/p/1",
                "sample_count": 2,
            }
        },
        products=[_without_url("bad", 6.0, "cat-a", "aloe")],
    )
    run = _new_run(db_session)

    assert main_mod.persist_aloe_country_mappings(db_session, [found]) == 1
    with pytest.raises(Exception, match="url"):
        main_mod.persist_results(db_session, run, [found])

    assert main_mod.load_aloe_country_map(db_session)["14"]["country_code"] == "gb"


# ─── Сбой базы вне записи сбора ──────────────────────────────────────────────


def _unwritable_row_at_flush(session, *_):
    session.add(
        storage.Product(
            site="aloe", external_id="unwritable", url=None, name="x", name_normalized="x"
        )
    )
    session.flush()


def _refused_statement(session, *_):
    # Вторая строка с уже занятым первичным ключом. Запрос идёт мимо flush:
    # сессия после него считает транзакцию рабочей, PostgreSQL — прерванной.
    run_id = session.scalar(select(storage.Run.id).order_by(storage.Run.id.desc()))
    session.execute(text("INSERT INTO runs (id) VALUES (:id)"), {"id": run_id})


@pytest.mark.parametrize("failing_step", [_unwritable_row_at_flush, _refused_statement])
def test_database_failure_after_the_write_still_closes_the_run(
    db_session, monkeypatch, failing_step
):
    """На базе упал шаг после записи сбора (здесь — подтверждение цен). Прогон и
    заявка, по которой он запущен, закрываются как `failed` с настоящей
    причиной; уже сохранённое о прогоне остаётся."""
    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add(request)
    db_session.commit()
    monkeypatch.setattr(storage, "confirm_prices_of_latest_verified_runs", failing_step)
    catalog = {"cat-a": [_scraped("a", 5.0, "cat-a")]}

    run, _ = _run_catalog(
        db_session,
        monkeypatch,
        catalog,
        expected_status="failed",
        command_line=("run", "--mode", "category", "--request-id", str(request.id)),
    )

    assert run.finished_at is not None
    assert run.error_message.startswith("IntegrityError"), run.error_message
    assert run.products_scraped == 1
    assert _external_ids(db_session) == ["a"]
    db_session.refresh(request)
    assert (request.status, request.run_id) == ("failed", run.id)
    assert request.completed_at is not None


def _verdict_then_refused_statement(session, run):
    run.catalog_verification_reason = "reason set right before the failure"
    run.run_quality = {**(run.run_quality or {}), "set_before_failure": True}
    _refused_statement(session)


def test_fields_set_on_the_run_before_the_failure_survive_the_rollback(db_session, monkeypatch):
    """Шаг выставил на прогоне причину отказа и `run_quality`, а потом упал на
    базе запросом мимо flush. Откат перед записью итога эти поля стирает —
    обработчик переносит их через него, и в базе остаётся то, что о прогоне
    успели сказать. (После упавшего flush переносить нечего: объекты сессии
    SQLAlchemy сбрасывает сам, раньше обработчика.)"""
    monkeypatch.setattr(
        storage, "confirm_prices_of_latest_verified_runs", _verdict_then_refused_statement
    )
    catalog = {"cat-a": [_scraped("a", 5.0, "cat-a")]}

    run, _ = _run_catalog(db_session, monkeypatch, catalog, expected_status="failed")

    assert run.finished_at is not None
    assert run.error_message.startswith("IntegrityError"), run.error_message
    assert run.catalog_verification_reason == "reason set right before the failure"
    assert run.run_quality["set_before_failure"] is True
    assert run.run_quality["sites"], "прежнее содержимое на месте"


def _status_ok_then_broken_in_code(session, run):
    run.status = "ok"
    raise ValueError("failed after the status was set")


def test_status_set_before_the_failure_gives_way_to_the_outcome(db_session, monkeypatch):
    """Конец `run` ставит прогону `ok` и только потом коммитит. Сбой между ними
    оставляет на прогоне несохранённый `ok` — он переносится через откат, как
    любое поле, но итог ставится после него: упавший прогон не публикуется."""
    monkeypatch.setattr(
        storage, "confirm_prices_of_latest_verified_runs", _status_ok_then_broken_in_code
    )
    catalog = {"cat-a": [_scraped("a", 5.0, "cat-a")]}

    run, _ = _run_catalog(db_session, monkeypatch, catalog, expected_status="failed")

    assert run.finished_at is not None
    assert run.error_message == "ValueError: failed after the status was set"


def test_only_fields_changed_since_the_last_write_are_carried(db_session):
    """Через откат переносится то же, что записал бы flush, — не вся строка:
    иначе запись итога затирала бы колонки значениями, прочитанными раньше."""
    run = _new_run(db_session)
    assert main_mod._unsaved_column_values(run) == {}

    run.catalog_verification_reason = "why"
    run.status = "ok"  # то же, что в базе

    assert main_mod._unsaved_column_values(run) == {"catalog_verification_reason": "why"}


def _unwritable_field_then_broken_in_code(session, run):
    run.catalog_verification_reason = "set next to a field the database refuses"
    run.tenant_id = None  # NOT NULL
    raise ValueError("step failed")


@pytest.mark.parametrize("log_stream", ["alive", "dead"])
def test_carried_field_the_database_refuses_does_not_cost_the_outcome(
    db_session, monkeypatch, log_stream
):
    """Поле, выставленное на прогоне перед сбоем, база не принимает. Итог важнее
    поля: вторая попытка пишет его один — и тогда, когда строка журнала о ней
    сама падает на оборванном выводе."""
    monkeypatch.setattr(
        storage, "confirm_prices_of_latest_verified_runs", _unwritable_field_then_broken_in_code
    )
    retried: list[tuple[str, dict]] = []
    if log_stream == "alive":
        retried = _record_log(monkeypatch, main_mod, "warning")
    else:
        _dead_log_stream(monkeypatch, "warning")
    catalog = {"cat-a": [_scraped("a", 5.0, "cat-a")]}

    run, _ = _run_catalog(db_session, monkeypatch, catalog, expected_status="failed")

    assert run.finished_at is not None
    assert run.error_message == "ValueError: step failed"
    assert run.tenant_id == 1
    assert run.catalog_verification_reason != "set next to a field the database refuses"
    if log_stream == "alive":
        assert [
            fields["dropped_fields"]
            for name, fields in retried
            if name == "run_failure_write_retried"
        ] == [["catalog_verification_reason", "tenant_id"]]


def _half_done_then_broken_in_code(session, run):
    session.add(
        storage.Product(
            site=SITE,
            external_id="half-done",
            url="https://half.invalid/p",
            name="x",
            name_normalized="x",
        )
    )
    session.flush()
    raise ValueError("step broke halfway")


def test_unfinished_work_of_a_failed_step_is_not_saved_with_the_outcome(db_session, monkeypatch):
    """Шаг упал в коде, успев отправить в базу половину своей работы. Раньше
    commit итога сохранял эту половину вместе с `failed`; теперь итог пишется на
    чистой транзакции."""
    monkeypatch.setattr(
        storage, "confirm_prices_of_latest_verified_runs", _half_done_then_broken_in_code
    )
    catalog = {"cat-a": [_scraped("a", 5.0, "cat-a")]}

    run, _ = _run_catalog(db_session, monkeypatch, catalog, expected_status="failed")

    assert run.finished_at is not None
    assert run.error_message == "ValueError: step broke halfway"
    assert run.products_scraped == 1, "сохранённое до шага остаётся"
    assert _external_ids(db_session) == ["a"]


def test_dead_log_stream_does_not_leave_the_run_running(db_session, monkeypatch):
    """Журнал команды пишет в stdout. При оборванном выводе (ssh без терминала,
    отменённый workflow) строка о причине сбоя сама падает с `BrokenPipeError`.
    Она пишется до итога — и запись итога сорвать не должна."""

    def broken_step(session, run):
        raise ValueError("step failed")

    monkeypatch.setattr(storage, "confirm_prices_of_latest_verified_runs", broken_step)
    _dead_log_stream(monkeypatch, "exception")
    catalog = {"cat-a": [_scraped("a", 5.0, "cat-a")]}

    run, _ = _run_catalog(db_session, monkeypatch, catalog, expected_status="failed")

    assert run.finished_at is not None
    assert run.error_message == "ValueError: step failed"


# Кто держит сессионный замок сопоставления в этой базе: односложный ключ bigint
# лежит в classid (старшие 32 бита) и objid (младшие), objsubid = 1.
_MATCHING_LOCK_HOLDERS = text(
    """
    SELECT locks.pid
    FROM pg_locks AS locks
    WHERE locks.locktype = 'advisory'
      AND locks.granted
      AND locks.database = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND locks.classid::bigint = CASE
          WHEN hashtext(:key) < 0 THEN 4294967295::bigint
          ELSE 0::bigint
      END
      AND locks.objid::bigint = (hashtext(:key)::bigint & 4294967295::bigint)
      AND locks.objsubid = 1
    """
)


@pytest.mark.parametrize("failing_step", [_unwritable_row_at_flush, _refused_statement])
def test_database_failure_under_the_matching_lock_still_closes_the_run(
    db_session, monkeypatch, failing_step
):
    """На базе упал шаг под замком сопоставления (здесь — перепроверка пар).

    Замок лежит на отдельном соединении, и сессия до его снятия ходит только
    через него. Снятие замка на прерванной транзакции само падает, так что
    обработчик получает сессию с незавершённой транзакцией на этом соединении:
    откат её завершает, соединение замка закрывается, итог пишется через пул.
    """
    catalog = {"cat-a": [_scraped("a", 5.0, "cat-a")]}

    run, _ = _run_catalog(
        db_session, monkeypatch, catalog, expected_status="failed", revalidate_split=failing_step
    )

    assert run.finished_at is not None
    assert "identity revalidation failed: IntegrityError" in run.error_message, run.error_message
    assert run.products_scraped == 1
    if _is_postgresql(db_session):
        # Замка не держит никто. Прежний код этому не противоречил — замок
        # уходил, когда команда закрывала сессию; проверка стережёт только то,
        # что запись итога не оставила соединение замка открытым.
        holders = db_session.scalars(
            _MATCHING_LOCK_HOLDERS, {"key": main_mod.matcher.MATCH_MUTATION_ADVISORY_LOCK_KEY}
        ).all()
        assert holders == []


def test_reason_is_logged_even_when_the_outcome_cannot_be_written(db_session, monkeypatch):
    """База не принимает и запись итога: `failed` у прогона упирается в
    уникальный индекс. Записать итог некуда, прогон остаётся `running`, — но в
    журнале есть причина, по которой он упал, отдельной строкой — что итог не
    записан, а команда выходит с исходной ошибкой, а не с отказом записи итога."""
    db_session.add(storage.Run(status="failed", started_at=utcnow(), finished_at=utcnow()))
    db_session.commit()
    db_session.execute(text("CREATE UNIQUE INDEX refuse_second_failed_run ON runs (status)"))
    db_session.commit()
    logged = _record_log(monkeypatch, main_mod, "exception")
    catalog = {"cat-a": [_without_url("bad", 6.0, "cat-a")]}

    run, result = _run_catalog(db_session, monkeypatch, catalog, expected_status="running")

    assert [name for name, _ in logged] == ["run_failed", "run_failure_not_recorded"]
    assert "url" in logged[0][1]["handled"], logged
    # Свою ошибку команда печатает строкой `Error: …` — в ней причина падения
    # сбора (отказ по `products.url`), а не отказ записи итога (индекс `runs`).
    error_lines = [line for line in result.output.splitlines() if line.startswith("Error: ")]
    assert len(error_lines) == 1, result.output
    assert "url" in error_lines[0] and "refuse_second_failed_run" not in error_lines[0]
    assert isinstance(result.exception, SystemExit)
    assert run.finished_at is None


def test_outcome_is_written_even_when_the_rollback_itself_fails(db_session, monkeypatch):
    """Откат перед записью итога упал сам (так бывает на потерянном соединении:
    до сервера он не дошёл). Свою транзакцию сессия при этом закрыла — итог
    пишется на следующей. Строка журнала об этом падает тоже (вывод оборван) —
    и тоже ничего не срывает."""
    _dead_log_stream(monkeypatch, "warning")
    run = storage.Run(status="running", started_at=utcnow())
    db_session.add(run)
    db_session.commit()
    run_id = run.id
    real_rollback = db_session.rollback

    def rollback_that_fails():
        real_rollback()
        raise RuntimeError("rollback did not reach the server")

    db_session.rollback = rollback_that_fails

    def record_failure():
        run.status = "failed"
        run.error_message = "ValueError: boom"
        run.finished_at = utcnow()

    try:
        main_mod._write_failed_run_outcome(db_session, run, run_id, record_failure)
    finally:
        del db_session.rollback

    db_session.rollback()
    db_session.expire_all()
    stored = db_session.get(storage.Run, run_id)
    assert (stored.status, stored.error_message) == ("failed", "ValueError: boom")
    assert stored.finished_at is not None


def test_unwritable_outcome_with_a_dead_log_stream_still_leaves_the_handler(
    db_session, monkeypatch
):
    """Итог не записать, и строка журнала об этом падает сама: помощник всё
    равно возвращает управление — команда выйдет с причиной падения сбора."""
    _dead_log_stream(monkeypatch, "warning", "exception")
    run = _new_run(db_session)

    def record_that_the_database_refuses():
        raise RuntimeError("outcome refused")

    main_mod._write_failed_run_outcome(db_session, run, run.id, record_that_the_database_refuses)


def test_failed_rollback_does_not_replace_the_cause_of_a_failed_write(db_session, monkeypatch):
    """Запись сбора упала в коде, а соединение к этому моменту молча потеряно:
    откат недописанного сам падает. Наверх уходит причина сбоя записи, а не
    отказ отката, и сессия остаётся рабочей. Строка журнала об отказе отката
    падает тоже (вывод оборван) — причину она не подменяет."""
    if not _is_postgresql(db_session):
        pytest.skip("обрыв соединения сервером — только PostgreSQL")
    _dead_log_stream(monkeypatch, "warning")
    run = _new_run(db_session)
    admin = create_engine(db_session.get_bind().url, isolation_level="AUTOCOMMIT")

    def write_that_breaks_on_a_dead_connection(session, run, results, **kwargs):
        session.execute(select(storage.Run.id)).all()  # транзакция открыта
        pid = session.connection().connection.dbapi_connection.info.backend_pid
        with admin.connect() as connection:
            connection.execute(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid})
        raise ValueError("the write broke in code")

    monkeypatch.setattr(main_mod, "_persist_chunks", write_that_breaks_on_a_dead_connection)
    try:
        with pytest.raises(ValueError, match="the write broke in code"):
            main_mod.persist_results(db_session, run, [])
    finally:
        admin.dispose()

    assert db_session.scalar(select(storage.Run.id).where(storage.Run.id == run.id)) == run.id


# ─── Чего стоил незакрытый прогон соседнему сайту ─────────────────────────────


def test_one_sites_database_failure_does_not_fail_the_next_sites_run(db_session, monkeypatch):
    """Сбор aloe упал на базе, следом по расписанию идёт сбор aptekonline.

    Пока строка упавшего сбора висела `running`, проверенный сбор следующего
    сайта сам кончался `failed` — на пересчёте рекомендаций («ROI refresh failed
    for trusted epoch»), — а кэш рекомендаций удалялся: эпоха каталога смотрит на
    последнюю попытку сайта по номеру, и незакрытая попытка её закрывает.
    Прогон, закрытый как `failed`, соседу не мешает: пересчёт откладывается,
    сбор кончается `ok`, кэш остаётся как был.
    """
    sites = ("pharmonline", "aptekonline", "aloe")
    week_ago = utcnow() - timedelta(days=6, hours=23)
    for site in sites:
        db_session.add(
            storage.Run(
                status="ok",
                started_at=week_ago,
                finished_at=week_ago + timedelta(hours=1),
                catalog_scope="full",
                full_catalog_sites=site,
                catalog_verified=True,
                run_quality={
                    "full_catalog_verified": True,
                    "financially_eligible": True,
                    "sites": {site: {"status": "ok"}},
                },
                products_scraped=10,
            )
        )
    db_session.commit()
    for site in sites:
        db_session.add(
            storage.RoiActionsCache(
                tenant_id=1,
                client_site=site,
                payload=[{"type": "undercut"}],
                computed_at=week_ago + timedelta(hours=1),
                run_id=1,
            )
        )
    db_session.commit()
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: 0)
    # `--force`: гвард ритма решает по дню недели, тесту он не нужен.
    scheduled = ("run", "--mode", "category", "--force")

    broken, _ = _run_catalog(
        db_session,
        monkeypatch,
        {"cat-a": [_without_url("bad", 6.0, "cat-a", "aloe")]},
        site="aloe",
        expected_status="failed",
        command_line=scheduled,
    )
    following, _ = _run_catalog(
        db_session,
        monkeypatch,
        {"cat-a": [_scraped("a", 5.0, "cat-a")]},
        expected_status="ok",
        command_line=scheduled,
    )

    assert broken.finished_at is not None
    assert (following.full_catalog_sites, following.catalog_verified) == (SITE, True)
    cached = db_session.execute(
        select(storage.RoiActionsCache.client_site, storage.RoiActionsCache.payload)
    ).all()
    assert sorted(cached) == [(site, [{"type": "undercut"}]) for site in sorted(sites)]


# ─── Соседние команды: scrape (тики) и ai-crawl ──────────────────────────────


def test_scrape_command_that_cannot_be_written_ends_failed(db_session, monkeypatch):
    """Команда `scrape` (её зовут intraday-тики) пишет той же `persist_results`
    и после сбоя базы так же не могла записать итог."""
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: 0)
    catalog = {"cat-a": [_scraped("a", 5.0, "cat-a")], "cat-b": [_without_url("bad", 6.0, "cat-b")]}

    run, _ = _run_catalog(
        db_session, monkeypatch, catalog, expected_status="failed", command_line=("scrape",)
    )

    assert run.finished_at is not None
    assert "url" in run.error_message, "причина — отказ базы, а не сессия без отката"
    assert "PendingRollback" not in run.error_message


@pytest.mark.parametrize(
    ("failing_step", "refused_table"),
    [(_unwritable_row_at_flush, "products"), (_refused_statement, "runs")],
)
def test_scrape_command_closes_the_run_after_a_database_failure_outside_the_write(
    db_session, monkeypatch, failing_step, refused_table
):
    """Сбой базы в шаге `scrape` вне записи сбора (здесь — словарь стран, он
    пишется прямо перед ней): недописанное за собой шаг не откатывает, и итог
    записывает обработчик — откатив сессию."""
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", failing_step)
    catalog = {"cat-a": [_scraped("a", 5.0, "cat-a")]}

    run, _ = _run_catalog(
        db_session, monkeypatch, catalog, expected_status="failed", command_line=("scrape",)
    )

    assert run.finished_at is not None
    # `scrape` пишет причину без имени класса ошибки — узнаём её по таблице, в
    # записи которой база отказала.
    assert refused_table in run.error_message, run.error_message
    for secondary in ("PendingRollback", "InFailedSqlTransaction"):
        assert secondary not in run.error_message, "причина — отказ базы, а не сессия без отката"
    assert _external_ids(db_session) == [], "до записи сбора не дошло"


class _Crawler:
    """Запасной сборщик, нашедший одну запись."""

    found: ScrapedProduct

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def crawl(self, max_urls: int, dry_run: bool = False):
        return ScrapeResult(site=SITE, products=[type(self).found])


def _ai_crawl(db_session, monkeypatch, found: ScrapedProduct):
    crawler = type("_Found", (_Crawler,), {"found": found})
    monkeypatch.setitem(ai_crawler.AI_CRAWLER_BY_SITE, SITE, crawler)
    monkeypatch.delenv("AI_CRAWL_PROVIDER", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "not-used")
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    db_session.commit()

    result = CliRunner().invoke(main_mod.cli, ["ai-crawl", "--site", SITE])

    db_session.rollback()
    db_session.expire_all()
    run = db_session.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
    assert result.exit_code != 0
    assert run.status == "failed", (result.output, result.exception)
    assert run.finished_at is not None
    return run


def test_ai_crawl_command_that_cannot_be_written_ends_failed(db_session, monkeypatch):
    """`ai-crawl` пишет той же `persist_results`."""
    run = _ai_crawl(db_session, monkeypatch, _without_url("bad", 6.0, "cat-a"))

    assert run.error_message.startswith("IntegrityError"), run.error_message


@pytest.mark.parametrize("failing_step", [_unwritable_row_at_flush, _refused_statement])
def test_ai_crawl_handler_does_not_rely_on_the_write_cleaning_up(
    db_session, monkeypatch, failing_step
):
    """Другого обращения к базе, кроме записи сбора, у `ai-crawl` нет, а запись
    откатывает за собой сама. Обработчик на это не полагается: здесь на месте
    записи шаг, который падает на базе и транзакцию за собой не откатывает."""
    monkeypatch.setattr(main_mod, "persist_results", failing_step)

    run = _ai_crawl(db_session, monkeypatch, _scraped("a", 5.0, "cat-a"))

    assert run.error_message.startswith("IntegrityError"), run.error_message
