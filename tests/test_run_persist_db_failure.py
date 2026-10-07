"""Сбой базы посреди сбора не должен стоить прогону всего остального.

Команда `run` пишет каждую категорию сразу после сбора — колбэком из
`BaseScraper.scrape`, который ловит исключение колбэка и идёт дальше. Сессия
после сбоя базы остаётся с транзакцией, требующей отката, и до 2026-10-07 его
никто не делал: колбэк каждой следующей категории и финальный проход падали на
той же сессии (`PendingRollbackError`, на PostgreSQL ещё и
`InFailedSqlTransaction`), обработчик ошибок `run` падал на записи итога так же,
и прогон оставался `running` без `finished_at` с товарами только до сбоя.

Теперь недописанную пачку откатывает сама `persist_results`, а обработчик
ошибок `run` откатывает сессию, если иначе итог не записать.

Сбои здесь настоящие: отказывает сама база, посреди записи, а не код до
обращения к ней. Каждый сценарий идёт на SQLite и на PostgreSQL. Прод —
PostgreSQL, и после ошибки запроса он не выполняет в транзакции больше ничего,
а SQLite продолжает как ни в чём не бывало; длину строки и отложенные
ограничения проверяет тоже только PostgreSQL.
"""

from __future__ import annotations

import os
import sys
from uuid import uuid4

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from src import main as main_mod
from src import storage
from src._time import utcnow
from src.scrapers import ai_crawler
from src.scrapers import base as scraper_base
from src.scrapers.base import ScrapeResult
from tests.test_run_single_observation import (
    _observations,
    _product,
    _run_aloe,
    _scraped,
    _seen_earlier_at,
    _session_factory,
)

# Запись итога в сессию, чью транзакцию уже отменил упавший flush, SQLAlchemy
# молча отбросил бы с этим предупреждением.
pytestmark = pytest.mark.filterwarnings(
    "error:Session's state has been changed on a non-active transaction"
)


@pytest.fixture(params=["sqlite", "postgresql"])
def db_session(request, db_session, monkeypatch):
    if request.param == "sqlite":
        yield db_session
        return
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL is required")
    # Замок сбора — один на всю базу, а не на схему: два прогона этих тестов на
    # одной базе мешали бы друг другу. На SQLite замка нет вовсе.
    monkeypatch.setattr(
        main_mod, "_hold_scrape_lock_until_command_exit", lambda factory, *, wait: True
    )
    # Своя схема на тест: общая база CI уже размечена миграциями.
    schema = f"persist_failure_{uuid4().hex[:12]}"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_pre_ping=True,  # как на проде, `storage.make_engine`
    )
    try:
        storage.Base.metadata.create_all(engine)
        with sessionmaker(engine, expire_on_commit=False, autoflush=False)() as session:
            yield session
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def _is_postgresql(db_session) -> bool:
    return db_session.get_bind().dialect.name == "postgresql"


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


def _record_log(monkeypatch, module, method: str) -> list[tuple[str, dict]]:
    """События, которые модуль записал в журнал этим методом (`error`, `exception`).

    В `handled` — первая строка исключения, которое в этот момент
    обрабатывалось: его и печатает `log.exception`.
    """
    events: list[tuple[str, dict]] = []
    real_log = module.log

    class _Recording:
        def __getattr__(self, name):
            real = getattr(real_log, name)
            if name != method:
                return real

            def record(event, **fields):
                handled = str(sys.exc_info()[1]).splitlines()[0]
                events.append((event, {**fields, "handled": handled}))
                return real(event, **fields)

            return record

    monkeypatch.setattr(module, "log", _Recording())
    return events


def _without_url(ext_id: str, price: float, category: str):
    """Запись, которую база не примет никогда: `products.url` — NOT NULL."""
    entry = _scraped(ext_id, price, category)
    entry.url = None
    return entry


def _snapshot_prices(db_session, run: storage.Run) -> list[float]:
    return sorted(
        db_session.scalars(
            select(storage.PriceSnapshot.price).where(storage.PriceSnapshot.run_id == run.id)
        )
    )


def _external_ids(db_session) -> list[str]:
    return sorted(db_session.scalars(select(storage.Product.external_id)))


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
    отказанную пачку и ту, что шла за ней, и у каждой записи сбора ровно одно
    наблюдение: у закоммиченной — от колбэка, у остальных — от финального
    прохода. Запись считается учтённой только после коммита своей пачки; отметь
    её раньше — и `b-seen` осталась бы без наблюдения вовсе.
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

    run = _run_aloe(db_session, monkeypatch, catalog)

    assert writes == [
        ("cat-a", "ok"),
        ("cat-b", "IntegrityError"),
        ("cat-c", "ok"),
        ("cat-a cat-b cat-c", "ok"),
    ]
    assert [(event, fields["category"]) for event, fields in collector_errors] == [
        ("incremental_persist_failed", "cat-b")
    ]
    assert _observations(db_session, run) == {
        "a": 1,
        "b-new": 1,
        "b-seen": 1,
        "b-later": 1,
        "c": 1,
    }
    assert _snapshot_prices(db_session, run) == [5.0, 6.0, 7.0, 8.0, 9.0], "каждая цена — один раз"
    assert _product(db_session, "b-seen").category == "cat-b"
    assert run.products_scraped == 5
    assert (run.catalog_scope, run.full_catalog_sites) == ("full", "aloe")


def test_connection_lost_midway_costs_one_category_write_not_the_run(db_session, monkeypatch):
    """Сервер оборвал соединение посреди записи `cat-b` — так рвётся связь при
    перезапуске PostgreSQL. Запись категории падает, следующая берёт новое
    соединение, финальный проход дописывает `cat-b`."""
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
        run = _run_aloe(db_session, monkeypatch, catalog)
    finally:
        admin.dispose()

    assert about_to_write == ["cat-b", "dropped"]
    assert writes == [
        ("cat-a", "ok"),
        ("cat-b", "OperationalError"),
        ("cat-c", "ok"),
        ("cat-a cat-b cat-c", "ok"),
    ]
    assert _observations(db_session, run) == {"a": 1, "b": 1, "c": 1}
    assert _snapshot_prices(db_session, run) == [5.0, 6.0, 7.0]


def test_run_that_cannot_be_written_ends_failed_not_running(db_session, monkeypatch):
    """Запись `cat-b` база не принимает никогда. Следующая категория всё равно
    записана, а прогон, упав на финальном проходе, получает `failed` и время
    окончания — раньше он оставался `running`, а `c` терялся."""
    writes = _record_writes(monkeypatch)
    catalog = {
        "cat-a": [_scraped("a", 5.0, "cat-a")],
        "cat-b": [_without_url("bad", 6.0, "cat-b")],
        "cat-c": [_scraped("c", 7.0, "cat-c")],
    }

    run = _run_aloe(db_session, monkeypatch, catalog, expected_status="failed")

    assert writes == [
        ("cat-a", "ok"),
        ("cat-b", "IntegrityError"),
        ("cat-c", "ok"),
        ("cat-a cat-b cat-c", "IntegrityError"),
    ]
    assert run.finished_at is not None
    assert run.error_message.startswith("IntegrityError"), run.error_message
    assert _external_ids(db_session) == ["a", "c"]
    assert _observations(db_session, run) == {"a": 1, "c": 1}


def test_statement_refused_midway_leaves_nothing_half_written(db_session, monkeypatch):
    """Подпись акции длиннее колонки: PostgreSQL отказывает во вставке строки
    цены, когда карточка нового товара уже отправлена, и до отката не выполняет
    в этой транзакции ничего. Сама сессия при этом считает транзакцию рабочей —
    откатывать надо, не спрашивая её.

    Товар из отказанной пачки в базе не остаётся — ни после колбэка, ни после
    финального прохода.
    """
    if not _is_postgresql(db_session):
        pytest.skip("SQLite не проверяет длину строки")
    writes = _record_writes(monkeypatch)
    too_long = _scraped("b", 6.0, "cat-b")
    too_long.promo_label = "x" * 201
    catalog = {
        "cat-a": [_scraped("a", 5.0, "cat-a")],
        "cat-b": [too_long],
        "cat-c": [_scraped("c", 7.0, "cat-c")],
    }

    run = _run_aloe(db_session, monkeypatch, catalog, expected_status="failed")

    assert writes == [
        ("cat-a", "ok"),
        ("cat-b", "DataError"),
        ("cat-c", "ok"),
        ("cat-a cat-b cat-c", "DataError"),
    ]
    assert run.finished_at is not None
    assert run.error_message.startswith("DataError"), run.error_message
    assert _external_ids(db_session) == ["a", "c"]
    assert _observations(db_session, run) == {"a": 1, "c": 1}


def test_write_broken_by_the_code_leaves_nothing_half_written(db_session, monkeypatch):
    """Запись упала не на базе, а в коде — на второй записи пачки, когда карточка
    первой уже изменена в сессии. Раньше обработчик ошибок сохранял эту
    полузапись вместе с итогом прогона: у товара менялись категория и время
    «видели», а наблюдения и цены за ними не было."""
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

    run = _run_aloe(db_session, monkeypatch, catalog, expected_status="failed")

    assert writes == [
        ("cat-a", "ok"),
        ("cat-b", "AttributeError"),
        ("cat-a cat-b", "AttributeError"),
    ]
    old = _product(db_session, "old")
    assert (old.category, old.last_seen_at) == (category_before, seen_before)
    assert _observations(db_session, run) == {"a": 1}
    assert _snapshot_prices(db_session, run) == [5.0]


def test_scrape_command_that_cannot_be_written_ends_failed(db_session, monkeypatch):
    """Команда `scrape` (её зовут intraday-тики) пишет той же `persist_results`
    и после сбоя базы так же не могла записать итог."""
    catalog = {"cat-a": [_scraped("a", 5.0, "cat-a")], "cat-b": [_without_url("bad", 6.0, "cat-b")]}

    run = _run_aloe(
        db_session, monkeypatch, catalog, expected_status="failed", command_line=("scrape",)
    )

    assert run.finished_at is not None
    assert "url" in run.error_message, "причина — отказ базы, а не сессия без отката"
    assert "PendingRollback" not in run.error_message


def test_ai_crawl_command_that_cannot_be_written_ends_failed(db_session, monkeypatch):
    """`ai-crawl` пишет той же `persist_results`."""

    class _Crawler:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def crawl(self, max_urls: int, dry_run: bool = False):
            return ScrapeResult(site="aloe", products=[_without_url("bad", 6.0, "cat-a")])

    monkeypatch.setitem(ai_crawler.AI_CRAWLER_BY_SITE, "aloe", _Crawler)
    monkeypatch.delenv("AI_CRAWL_PROVIDER", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "not-used")
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))

    result = CliRunner().invoke(main_mod.cli, ["ai-crawl", "--site", "aloe"])

    run = db_session.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
    assert result.exit_code != 0
    assert run.status == "failed", result.output
    assert run.finished_at is not None
    assert run.error_message.startswith("IntegrityError"), run.error_message


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
        products=[_without_url("bad", 6.0, "cat-a")],
    )
    run = storage.Run(status="running", started_at=utcnow())
    db_session.add(run)
    db_session.commit()

    assert main_mod.persist_aloe_country_mappings(db_session, [found]) == 1
    with pytest.raises(Exception, match="url"):
        main_mod.persist_results(db_session, run, [found])

    assert main_mod.load_aloe_country_map(db_session)["14"]["country_code"] == "gb"


# ─── Шаг после записи ────────────────────────────────────────────────────────


def _unwritable_row_at_flush(session, run):
    session.add(
        storage.Product(
            site="aloe", external_id="unwritable", url=None, name="x", name_normalized="x"
        )
    )
    session.flush()


def _refused_statement(session, run):
    # Вторая строка с тем же первичным ключом. Запрос идёт мимо flush: сессия
    # после него считает транзакцию рабочей, PostgreSQL — прерванной.
    session.execute(text("INSERT INTO runs (id) VALUES (:id)"), {"id": run.id})


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

    run = _run_aloe(
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


def test_reason_is_logged_even_when_the_outcome_cannot_be_written(db_session, monkeypatch):
    """База не принимает и запись итога: `failed` у прогона упирается в
    уникальный индекс. Записать итог некуда, прогон остаётся `running`, — но в
    журнале есть причина, по которой он упал, а не отказ записи итога."""
    db_session.add(storage.Run(status="failed", started_at=utcnow(), finished_at=utcnow()))
    db_session.commit()
    db_session.execute(text("CREATE UNIQUE INDEX refuse_second_failed_run ON runs (status)"))
    db_session.commit()
    logged = _record_log(monkeypatch, main_mod, "exception")
    catalog = {"cat-a": [_without_url("bad", 6.0, "cat-a")]}

    run = _run_aloe(db_session, monkeypatch, catalog, expected_status="running")

    assert [event for event, _ in logged] == ["run_failed"]
    assert "url" in logged[0][1]["handled"], logged
    assert run.finished_at is None
