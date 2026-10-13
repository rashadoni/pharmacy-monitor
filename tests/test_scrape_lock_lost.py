"""Сбор, потерявший замок сбора, останавливается — на настоящем замке PostgreSQL.

Замок сбора — сессионный advisory-замок на отдельном соединении, которое команда
держит до выхода (`main._hold_scrape_lock_until_command_exit`). Если сервер
оборвал соединения процесса (перезапуск PostgreSQL мимо systemd, восстановление
после сбоя), замок свободен, а процесс об этом не узнаёт: рабочая сессия берёт
из пула новое соединение и пишет дальше. До правки, после которой запись сбора
сама откатывает недописанное, обрыв посреди записи такой сбор губил; после неё
он доходил до конца — через запись, сопоставление и публикацию — уже без
замка, рядом с тиком, сбором другого сайта или `rematch`.

Теперь команда перед каждой записью сбора и перед каждым этапом после неё
спрашивает сервер, числится ли замок за её соединением, и без замка кончается
`failed` с причиной.

Замок здесь настоящий — в `tests/test_run_persist_db_failure.py` он подменён, — а
он один на базу, не на схему: у каждого теста своя база, иначе два прогона этих
тестов на одном сервере ждали бы друг друга. Соединения обрывает сам сервер
(`pg_terminate_backend`); перезапуск, после которого сервер какое-то время не
принимает новых, изображает запрет соединений с базой теста — сам сервер тесты
не перезапускают, он общий. На SQLite замка нет, и проверять нечего; в CI эти
тесты обязаны исполняться — их пропуск там ошибка.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import click
import pytest
from sqlalchemy import create_engine, event, make_url, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from src import main as main_mod
from src import run_lock, storage
from src._time import utcnow
from src.scrapers.base import ScrapeMustStop
from tests.test_run_persist_db_failure import (
    SITE,
    _ai_crawl,
    _CatalogScraper,
    _Crawler,
    _dead_log_stream,
    _external_ids,
    _record_log,
    _record_writes,
    _run_catalog,
    _scraped,
)

pytestmark = pytest.mark.filterwarnings(
    "error:Session's state has been changed on a non-active transaction"
)

# Сеансы, держащие замок в базе `:database`: односложный ключ bigint лежит в
# classid (старшие 32 бита) и objid (младшие), objsubid = 1.
_LOCK_HOLDERS = text(
    """
    SELECT locks.pid
    FROM pg_locks AS locks
    WHERE locks.locktype = 'advisory'
      AND locks.granted
      AND locks.database = (SELECT oid FROM pg_database WHERE datname = :database)
      AND locks.classid::bigint = CASE
          WHEN hashtext(:key) < 0 THEN 4294967295::bigint
          ELSE 0::bigint
      END
      AND locks.objid::bigint = (hashtext(:key)::bigint & 4294967295::bigint)
      AND locks.objsubid = 1
    ORDER BY locks.pid
    """
)


def _postgres_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql"):
        return url
    if os.environ.get("CI"):
        pytest.fail(
            "в CI потеря замка сбора обязана проверяться, а DATABASE_URL — не "
            "PostgreSQL: на SQLite замка нет, и проверять нечего."
        )
    pytest.skip("PostgreSQL DATABASE_URL is required")


class _Server:
    """Сервер глазами администратора: кто держит замки базы теста, оборвать сеансы."""

    def __init__(self, admin, database: str) -> None:
        self._admin = admin
        self._database = database

    def lock_holders(self, key: str = run_lock.SCRAPE_ADVISORY_LOCK_KEY) -> list[int]:
        with self._admin.connect() as connection:
            return list(connection.scalars(_LOCK_HOLDERS, {"database": self._database, "key": key}))

    def drops_the_lock_connection(self) -> None:
        """Оборвано одно соединение — то, на котором лежит замок сбора. Ни один
        запрос процесса при этом не падает."""
        (holder,) = self.lock_holders()
        with self._admin.connect() as connection:
            connection.execute(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": holder})
        assert self.lock_holders() == []

    def drops_connection(self, backend_pid: int) -> None:
        with self._admin.connect() as connection:
            connection.execute(
                text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": backend_pid}
            )

    def drops_every_connection(self) -> None:
        """Оборваны все соединения с базой разом — как при перезапуске PostgreSQL."""
        with self._admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity "
                    "WHERE datname = :database AND pid <> pg_backend_pid()"
                ),
                {"database": self._database},
            )
        assert self.lock_holders() == []

    def stops_accepting_connections(self) -> None:
        """Перезапуск, как его видит процесс: соединения оборваны, а новых
        сервер какое-то время не принимает."""
        with self._admin.connect() as connection:
            connection.execute(text(f'ALTER DATABASE "{self._database}" ALLOW_CONNECTIONS false'))
        self.drops_every_connection()

    def accepts_connections_again(self) -> None:
        with self._admin.connect() as connection:
            connection.execute(text(f'ALTER DATABASE "{self._database}" ALLOW_CONNECTIONS true'))

    def has_no_lock_holders_soon(self) -> bool:
        """Закрытый клиентом сеанс сервер убирает не мгновенно."""
        deadline = time.monotonic() + 5
        while self.lock_holders() and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.lock_holders() == []


# Настоящее ожидание: тесты ниже подменяют его, чтобы не спать.
_REAL_WAIT_FOR_THE_DATABASE = main_mod._wait_for_the_database


@pytest.fixture(autouse=True)
def outcome_write_never_waits(monkeypatch):
    """Запись итога ждёт только сервер, который недоступен; где сервер отвечает,
    пауза (до минуты) значила бы, что его приняли за недоступный. Тесты простоя
    ставят своё ожидание поверх. Проверяется после теста: исключение отсюда
    запись итога проглотила бы сама."""
    waited: list[float] = []
    monkeypatch.setattr(main_mod, "_wait_for_the_database", waited.append)
    yield
    assert waited == [], f"запись итога ждала сервер: {waited} с"


@pytest.fixture
def lock_db():
    """Своя база на тест, движок приложения и настоящий замок сбора."""
    admin_url = make_url(_postgres_url())
    database = f"scrape_lock_{uuid4().hex[:12]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    engine = storage.create_engine_hiding_statement_data(
        admin_url.set(database=database),
        # lock_timeout: ожидание замка, которого быть не должно, роняет тест, а
        # не вешает его.
        connect_args={"options": "-clock_timeout=5000"},
        pool_pre_ping=True,  # как на проде, `storage.make_engine`
    )
    server = _Server(admin, database)
    try:
        storage.Base.metadata.create_all(engine)
        with sessionmaker(engine, expire_on_commit=False, autoflush=False)() as session:
            yield SimpleNamespace(session=session, engine=engine, server=server)
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        admin.dispose()


class _Catalog(dict):
    """Каталог сайта, который помнит, какие категории сборщик у него спросил."""

    def __init__(self, *args, when_asked=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.asked: list[str] = []
        self._when_asked = when_asked or {}

    def __getitem__(self, category):
        self.asked.append(category)
        self._when_asked.get(category, lambda: None)()
        return super().__getitem__(category)


def _three_categories(site: str = SITE, **kwargs) -> _Catalog:
    return _Catalog(
        {
            "cat-a": [_scraped("a", 5.0, "cat-a", site)],
            "cat-b": [_scraped("b", 6.0, "cat-b", site)],
            "cat-c": [_scraped("c", 7.0, "cat-c", site)],
        },
        **kwargs,
    )


# Шаги `run` после сбора, в порядке исполнения. Между ними стоят проверки замка.
_STEPS_AFTER_THE_SCRAPE = (
    "persist_aloe_country_mappings",
    "final write",
    "_smoke_test_per_site_coverage",
    "_acquire_matcher_lock",
    "_run_matching_stage",
    "confirm_prices_of_latest_verified_runs",
)


class _Steps:
    """Какие шаги после сбора прогон исполнил. Шаги настоящие (кроме проверки
    покрытия: её `_run_catalog` подменяет, сюда она приходит аргументом
    `smoke_test`); `after[имя]` вызывается, когда шаг `имя` закончил."""

    def __init__(self, monkeypatch, after: dict | None = None) -> None:
        self.done: list[str] = []
        self._after = after or {}
        for owner, name in (
            (main_mod, "persist_aloe_country_mappings"),
            (main_mod, "_acquire_matcher_lock"),
            (main_mod, "_run_matching_stage"),
            (storage, "confirm_prices_of_latest_verified_runs"),
        ):
            monkeypatch.setattr(owner, name, self._recorded(name, getattr(owner, name)))
        # Финальный проход — тот вызов записи, которому `run` говорит, верит ли
        # он собранному каталогу aloe; запись категории такого аргумента не несёт.
        write = main_mod.persist_results
        monkeypatch.setattr(
            main_mod,
            "persist_results",
            self._recorded(
                "final write",
                write,
                only_if=lambda kwargs: "aloe_whole_catalog" in kwargs,
                otherwise=write,
            ),
        )
        self.smoke_test = self._recorded("_smoke_test_per_site_coverage", lambda *args: None)

    def _recorded(self, name, real, only_if=lambda kwargs: True, otherwise=None):
        def step(*args, **kwargs):
            if not only_if(kwargs):
                return otherwise(*args, **kwargs)
            result = real(*args, **kwargs)
            self.done.append(name)
            self._after.get(name, lambda: None)()
            return result

        return step


@contextmanager
def _in_the_middle_of_writing_cat_b(lock_db, monkeypatch, happens):
    """Запись категорий, у которой `happens(сеанс)` случается посреди записи
    `cat-b` — после её первого запроса, когда транзакция уже открыта; `сеанс` —
    номер сеанса сервера, которым идёт запись. Отдаёт список записей
    (`_record_writes`)."""
    about_to_write: list[str] = []

    def in_the_middle(conn, cursor, statement, parameters, context, executemany):
        if about_to_write == ["cat-b"]:
            about_to_write.append("happened")
            happens(conn.connection.dbapi_connection.info.backend_pid)

    def second_write_begins(writes):
        if writes == [("cat-a", "ok")]:
            about_to_write.append("cat-b")

    event.listen(lock_db.engine, "after_cursor_execute", in_the_middle)
    try:
        yield _record_writes(monkeypatch, before_write=second_write_begins)
    finally:
        event.remove(lock_db.engine, "after_cursor_execute", in_the_middle)
    assert about_to_write == ["cat-b", "happened"]


def _lost_lock_reason(noticed: str) -> str:
    return f"ScrapeLockLost: scrape lock lost (noticed {noticed}): "


# ─── Прогон с замком ─────────────────────────────────────────────────────────


def test_run_holds_the_lock_to_the_end_and_is_published(lock_db, monkeypatch):
    """Контроль: замок настоящий, никто его не обрывал. Прогон проходит все
    проверки, кончается `ok`, держит замок на каждом шаге и снимает его на
    выходе."""
    holders_at_each_step: list[int] = []

    def count_holders():
        holders_at_each_step.append(len(lock_db.server.lock_holders()))

    steps = _Steps(monkeypatch, after=dict.fromkeys(_STEPS_AFTER_THE_SCRAPE, count_holders))
    writes = _record_writes(monkeypatch)
    catalog = _three_categories(when_asked={"cat-b": count_holders})

    run, _ = _run_catalog(lock_db.session, monkeypatch, catalog, smoke_test=steps.smoke_test)

    assert steps.done == list(_STEPS_AFTER_THE_SCRAPE)
    assert holders_at_each_step == [1] * (1 + len(_STEPS_AFTER_THE_SCRAPE))
    assert [outcome for _, outcome in writes] == ["ok", "ok", "ok", "ok"]
    assert (run.catalog_scope, run.catalog_verified, run.error_message) == ("full", True, None)
    assert lock_db.server.lock_holders() == [], "замок снят на выходе из команды"


def test_run_stops_when_the_server_drops_every_connection_mid_write(lock_db, monkeypatch):
    """Сервер оборвал все соединения процесса посреди записи `cat-b` — вместе с
    тем, на котором лежал замок сбора.

    Запись откатывает недописанное, и следующая взяла бы из пула новое
    соединение как ни в чём не бывало. Прогон на это не идёт: `cat-c` не
    собирается, финального прохода и сопоставления нет, итог — `failed` с
    причиной. До проверки замка такой прогон кончался `ok`.
    """
    steps = _Steps(monkeypatch)
    catalog = _three_categories()

    with _in_the_middle_of_writing_cat_b(
        lock_db, monkeypatch, lambda writer: lock_db.server.drops_every_connection()
    ) as writes:
        run, result = _run_catalog(
            lock_db.session,
            monkeypatch,
            catalog,
            expected_status="failed",
            smoke_test=steps.smoke_test,
        )

    assert writes == [("cat-a", "ok"), ("cat-b", "OperationalError")]
    assert catalog.asked == ["cat-a", "cat-b"], "после потери замка сайт не дособирается"
    assert steps.done == [], "ни словаря стран, ни сопоставления, ни подтверждения цен"
    assert run.error_message.startswith(_lost_lock_reason("after a failed category write"))
    assert run.finished_at is not None
    assert _external_ids(lock_db.session) == ["a"]
    assert isinstance(result.exception, SystemExit), "команда вышла сама, а не упала"
    assert "scrape lock lost" in result.output


def test_run_survives_losing_the_writing_connection_while_the_lock_holds(lock_db, monkeypatch):
    """Обратный случай: оборвано соединение, которым шла запись `cat-b`, а
    соединение с замком цело. Замок держится — прогон не останавливается:
    запись категории потеряна одна, финальный проход её дописывает, итог `ok`."""
    holders_after_the_drop: list[int] = []

    def writer_is_dropped(writer):
        lock_db.server.drops_connection(writer)
        holders_after_the_drop.append(len(lock_db.server.lock_holders()))

    steps = _Steps(monkeypatch)
    catalog = _three_categories()

    with _in_the_middle_of_writing_cat_b(lock_db, monkeypatch, writer_is_dropped) as writes:
        run, _ = _run_catalog(lock_db.session, monkeypatch, catalog, smoke_test=steps.smoke_test)

    assert holders_after_the_drop == [1]
    assert writes == [
        ("cat-a", "ok"),
        ("cat-b", "OperationalError"),
        ("cat-c", "ok"),
        ("cat-a cat-b cat-c", "ok"),
    ]
    assert steps.done == list(_STEPS_AFTER_THE_SCRAPE)
    assert run.error_message is None
    assert _external_ids(lock_db.session) == ["a", "b", "c"]


@pytest.mark.parametrize("site", [SITE, "aloe"])
def test_run_stops_at_the_next_category_when_only_the_lock_connection_is_dropped(
    lock_db, monkeypatch, site
):
    """Оборвано одно соединение — с замком; запись `cat-a` уже прошла, и ни один
    запрос прогона не упал. Замеченное на границе следующей категории
    останавливает сбор сразу: `cat-b` не пишется, `cat-c` не собирается.

    У aloe записи по категориям нет (сайт пишется целиком в конце), но граница
    категории проверяется и у него: сбор без замка не идёт до конца сайта.
    """
    writes = _record_writes(monkeypatch)
    steps = _Steps(monkeypatch)
    catalog = _three_categories(
        site, when_asked={"cat-b": lock_db.server.drops_the_lock_connection}
    )

    run, _ = _run_catalog(
        lock_db.session,
        monkeypatch,
        catalog,
        site=site,
        expected_status="failed",
        smoke_test=steps.smoke_test,
    )

    assert run.error_message.startswith(_lost_lock_reason("before a category write"))
    assert catalog.asked == ["cat-a", "cat-b"]
    assert writes == ([("cat-a", "ok")] if site == SITE else [])
    assert steps.done == []
    assert _external_ids(lock_db.session) == (["a"] if site == SITE else [])


def test_run_does_not_write_what_it_collected_after_the_lock_was_lost(lock_db, monkeypatch):
    """Соединение с замком оборвано, когда категории уже собраны и записаны
    (сайт отдаёт акции). Дальше прогон не пишет ничего: ни словарь стран, ни
    финальный проход."""

    async def promos_while_the_lock_goes(self):
        lock_db.server.drops_the_lock_connection()
        return []

    monkeypatch.setattr(_CatalogScraper, "scrape_promos", promos_while_the_lock_goes)
    writes = _record_writes(monkeypatch)
    steps = _Steps(monkeypatch)
    catalog = _three_categories()

    run, _ = _run_catalog(
        lock_db.session,
        monkeypatch,
        catalog,
        expected_status="failed",
        smoke_test=steps.smoke_test,
    )

    assert run.error_message.startswith(_lost_lock_reason("after the scrape"))
    assert writes == [("cat-a", "ok"), ("cat-b", "ok"), ("cat-c", "ok")], "финального прохода нет"
    assert steps.done == []


def test_watchlist_run_is_checked_as_soon_as_its_scrape_ends(lock_db, monkeypatch):
    """У прогона по закреплённым ссылкам колбэка по категориям нет: первая
    проверка замка — сразу после сбора, раньше любого шага по его итогам."""

    async def scrape_while_the_lock_goes(urls_by_site):
        lock_db.server.drops_the_lock_connection()
        return [main_mod.ScrapeResult(site=SITE, products=[_scraped("a", 5.0, "cat-a")])]

    monkeypatch.setattr(
        main_mod, "collect_watchlist_urls", lambda session: {SITE: [f"https://{SITE}.invalid/p/a"]}
    )
    monkeypatch.setattr(main_mod, "scrape_watchlist_all", scrape_while_the_lock_goes)
    steps = _Steps(monkeypatch)

    run, _ = _run_catalog(
        lock_db.session,
        monkeypatch,
        _Catalog(),
        expected_status="failed",
        command_line=("run", "--mode", "watchlist"),
        smoke_test=steps.smoke_test,
    )

    assert run.error_message.startswith(_lost_lock_reason("after the scrape"))
    assert steps.done == []
    assert _external_ids(lock_db.session) == []


def test_public_api_run_is_checked_as_soon_as_its_scrape_ends(lock_db, monkeypatch):
    """Плановый сбор pharmonline (`public_api`) собирает каталог одним куском,
    без колбэка по категориям, и до записи сверяет его. Первая проверка замка —
    сразу после сбора: раньше сверки и раньше любой записи по её итогам."""
    from src.scrapers.pharmonline_public_api import PUBLIC_CATALOG_ROUTE

    async def promos_while_the_lock_goes(self):
        lock_db.server.drops_the_lock_connection()
        return []

    monkeypatch.setattr(_CatalogScraper, "scrape_promos", promos_while_the_lock_goes)
    monkeypatch.delenv("PHARMONLINE_USE_DDP", raising=False)
    monkeypatch.setattr(main_mod, "_pharmonline_public_api_enabled", lambda: True)
    monkeypatch.setattr(
        main_mod, "_pharmonline_scraper_class", lambda: main_mod.SCRAPER_CLASSES["pharmonline"]
    )
    verified: list[str] = []
    monkeypatch.setattr(
        main_mod, "classify_run_quality", lambda *args, **kwargs: verified.append("quality")
    )
    steps = _Steps(monkeypatch)
    catalog = _Catalog({PUBLIC_CATALOG_ROUTE: [_scraped("a", 5.0, "all", "pharmonline")]})

    run, _ = _run_catalog(
        lock_db.session,
        monkeypatch,
        catalog,
        site="pharmonline",
        expected_status="failed",
        command_line=("run", "--mode", "public_api"),
        smoke_test=steps.smoke_test,
    )

    assert catalog.asked == [PUBLIC_CATALOG_ROUTE], "сбор прошёл путём public_api"
    assert run.error_message.startswith(_lost_lock_reason("after the scrape"))
    assert verified == [], "до сверки собранного дело не дошло"
    assert steps.done == []
    assert _external_ids(lock_db.session) == []


_ONLY_THE_LOCK = "drops_the_lock_connection"
_EVERY_CONNECTION = "drops_every_connection"


@pytest.mark.parametrize(
    ("lost_after", "dropped", "reason"),
    [
        (
            "persist_aloe_country_mappings",
            _ONLY_THE_LOCK,
            _lost_lock_reason("before the final write"),
        ),
        (
            "persist_aloe_country_mappings",
            _EVERY_CONNECTION,
            _lost_lock_reason("before the final write"),
        ),
        # Следом за финальным проходом идёт проверка покрытия сайта — она пишет
        # события в журнал алертов.
        ("final write", _ONLY_THE_LOCK, _lost_lock_reason("after the final write")),
        ("final write", _EVERY_CONNECTION, _lost_lock_reason("after the final write")),
        ("_acquire_matcher_lock", _ONLY_THE_LOCK, _lost_lock_reason("before matching")),
        ("_acquire_matcher_lock", _EVERY_CONNECTION, _lost_lock_reason("before matching")),
        ("_run_matching_stage", _ONLY_THE_LOCK, _lost_lock_reason("before alerts")),
        ("_run_matching_stage", _EVERY_CONNECTION, _lost_lock_reason("before alerts")),
        (
            "confirm_prices_of_latest_verified_runs",
            _ONLY_THE_LOCK,
            _lost_lock_reason("before publication"),
        ),
        # Шаг оставил открытую транзакцию: её commit падает на оборванном
        # соединении раньше, чем дело доходит до проверки замка. Итог тот же.
        ("confirm_prices_of_latest_verified_runs", _EVERY_CONNECTION, "OperationalError: "),
    ],
)
def test_run_stops_at_the_first_stage_after_the_lock_was_lost(
    lock_db, monkeypatch, lost_after, dropped, reason
):
    """Замок потерян после записи сбора — на любом из этапов за ней. Следующий
    этап не начинается: без замка прогон не пишет финальный проход, не пишет
    события проверки покрытия, не сопоставляет, не подтверждает цены и не
    публикуется как `ok` — ни на миг: к обработчику сбоя он приходит `running`.

    Оборвано либо одно соединение с замком — тогда ни один запрос прогона не
    падает, и остановить его может только проверка, — либо все разом: вместе с
    ним уходит и замок сопоставления, а итог всё равно записан.
    """
    all_steps = list(_STEPS_AFTER_THE_SCRAPE)
    steps = _Steps(monkeypatch, after={lost_after: getattr(lock_db.server, dropped)})
    writes = _record_writes(monkeypatch)
    stored_status_when_the_run_failed: list[str] = []
    write_outcome = main_mod._write_failed_run_outcome

    def outcome_of_a_run_stored_as(session, run, run_id, record):
        with lock_db.engine.connect() as observer:
            stored_status_when_the_run_failed.append(
                observer.scalar(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id})
            )
        write_outcome(session, run, run_id, record)

    monkeypatch.setattr(main_mod, "_write_failed_run_outcome", outcome_of_a_run_stored_as)
    catalog = _Catalog({"cat-a": [_scraped("a", 5.0, "cat-a")]})

    run, _ = _run_catalog(
        lock_db.session,
        monkeypatch,
        catalog,
        expected_status="failed",
        smoke_test=steps.smoke_test,
    )

    assert run.error_message.startswith(reason), run.error_message
    # Замок сопоставления берётся сразу за проверкой покрытия, проверка замка
    # сбора — уже под ним; остальные шаги отделены проверкой каждый.
    assert steps.done == all_steps[: all_steps.index(lost_after) + 1], "следующий этап не начат"
    final_write = [("cat-a", "ok")] if "final write" in steps.done else []
    assert writes == [("cat-a", "ok"), *final_write]
    assert stored_status_when_the_run_failed == ["running"], "`ok` прогон не получал"
    assert run.finished_at is not None
    for key in (
        run_lock.SCRAPE_ADVISORY_LOCK_KEY,
        main_mod.matcher.MATCH_MUTATION_ADVISORY_LOCK_KEY,
    ):
        assert lock_db.server.lock_holders(key) == [], "команда не оставила за собой замков"


def test_dashboard_request_is_not_told_ok_by_a_run_that_lost_the_lock(lock_db, monkeypatch):
    """Сбор по кнопке из дашборда (с пределом товаров) отдаёт заявке итог сразу
    после записи, не дожидаясь сопоставления. Замок потерян за время записи —
    заявка `ok` не получает ни на миг: к обработчику сбоя она приходит
    `running`, а кончается `failed` вместе с прогоном."""
    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    lock_db.session.add(request)
    lock_db.session.commit()
    steps = _Steps(monkeypatch, after={"final write": lock_db.server.drops_the_lock_connection})
    stored_request_when_the_run_failed: list[str] = []
    write_outcome = main_mod._write_failed_run_outcome

    def outcome_of_a_request_stored_as(session, run, run_id, record):
        with lock_db.engine.connect() as observer:
            stored_request_when_the_run_failed.append(
                observer.scalar(
                    text("SELECT status FROM scrape_requests WHERE id = :id"), {"id": request.id}
                )
            )
        write_outcome(session, run, run_id, record)

    monkeypatch.setattr(main_mod, "_write_failed_run_outcome", outcome_of_a_request_stored_as)
    catalog = _Catalog({"cat-a": [_scraped("a", 5.0, "cat-a")]})

    run, _ = _run_catalog(
        lock_db.session,
        monkeypatch,
        catalog,
        expected_status="failed",
        command_line=("run", "--mode", "category", "--limit", "5", "--request-id", str(request.id)),
        smoke_test=steps.smoke_test,
    )

    assert run.error_message.startswith(_lost_lock_reason("after the final write"))
    assert stored_request_when_the_run_failed == ["running"]
    lock_db.session.refresh(request)
    assert (request.status, request.run_id) == ("failed", run.id)


def test_dead_log_stream_does_not_hide_the_lost_lock(lock_db, monkeypatch):
    """Строки журнала о потере замка падают сами (вывод оборван — ssh без
    терминала, отменённый workflow). Причина прогона — всё равно потеря замка,
    а не `BrokenPipeError` строки, которая о ней сообщала."""
    # Оборвано только соединение замка сбора: на прочих строках журнала этот
    # путь не спотыкается.
    steps = _Steps(
        monkeypatch, after={"_acquire_matcher_lock": lock_db.server.drops_the_lock_connection}
    )
    _dead_log_stream(monkeypatch, "warning", "error")
    catalog = _Catalog({"cat-a": [_scraped("a", 5.0, "cat-a")]})

    run, _ = _run_catalog(
        lock_db.session,
        monkeypatch,
        catalog,
        expected_status="failed",
        smoke_test=steps.smoke_test,
    )

    assert run.error_message.startswith(_lost_lock_reason("before matching")), run.error_message
    assert "_run_matching_stage" not in steps.done


# ─── Сервер не принимает соединения ──────────────────────────────────────────


def test_run_is_closed_as_failed_once_the_restarted_server_accepts_connections(
    lock_db, monkeypatch
):
    """Перезапуск PostgreSQL, как он выглядит на деле: соединения оборваны
    посреди записи `cat-b`, и новых сервер какое-то время не принимает.

    О потере замка прогон узнаёт за миллисекунды — раньше, чем сервер вернулся,
    — и итог ему записать некуда. Запись итога ждёт сервер и пишет `failed` с
    причиной, когда тот отвечает; без ожидания прогон оставался бы `running`.
    """
    waited: list[float] = []

    def server_comes_back(seconds):
        waited.append(seconds)
        lock_db.server.accepts_connections_again()

    monkeypatch.setattr(main_mod, "_wait_for_the_database", server_comes_back)
    logged = _record_log(monkeypatch, main_mod, "warning", "exception")
    steps = _Steps(monkeypatch)
    catalog = _three_categories()

    with _in_the_middle_of_writing_cat_b(
        lock_db, monkeypatch, lambda writer: lock_db.server.stops_accepting_connections()
    ) as writes:
        run, _ = _run_catalog(
            lock_db.session,
            monkeypatch,
            catalog,
            expected_status="failed",
            smoke_test=steps.smoke_test,
        )

    assert waited == [main_mod._RUN_OUTCOME_WAITS_FOR_DATABASE[0]]
    assert [
        (fields["seconds"], fields["error"])
        for name, fields in logged
        if name == "run_failure_write_waits_for_database"
    ] == [(waited[0], "OperationalError")]
    assert "run_failure_not_recorded" not in [name for name, _ in logged], "итог записан"
    assert writes == [("cat-a", "ok"), ("cat-b", "OperationalError")]
    assert run.error_message.startswith(_lost_lock_reason("after a failed category write"))
    assert run.finished_at is not None
    assert steps.done == []


def test_run_stays_running_when_the_server_does_not_come_back_in_time(lock_db, monkeypatch):
    """Сервер не вернулся за всё время ожидания (восстановление после сбоя идёт
    дольше минуты). Ждать дальше запись итога не будет: прогон остаётся
    `running`, в журнале — причина его остановки и строка о том, что итог не
    записан, команда выходит с причиной остановки. Закроет такую строку вотчер
    очереди."""
    waited: list[float] = []

    def waits_in_vain(seconds):
        waited.append(seconds)
        assert len(waited) <= 2 * len(main_mod._RUN_OUTCOME_WAITS_FOR_DATABASE), "ждёт без предела"

    monkeypatch.setattr(main_mod, "_wait_for_the_database", waits_in_vain)
    write_outcome = main_mod._write_failed_run_outcome

    def write_outcome_then_the_server_returns(*args):
        try:
            write_outcome(*args)
        finally:
            lock_db.server.accepts_connections_again()

    monkeypatch.setattr(
        main_mod, "_write_failed_run_outcome", write_outcome_then_the_server_returns
    )
    logged = _record_log(monkeypatch, main_mod, "exception")
    catalog = _three_categories()

    with _in_the_middle_of_writing_cat_b(
        lock_db, monkeypatch, lambda writer: lock_db.server.stops_accepting_connections()
    ):
        run, result = _run_catalog(lock_db.session, monkeypatch, catalog, expected_status="running")

    assert waited == list(main_mod._RUN_OUTCOME_WAITS_FOR_DATABASE)
    assert [name for name, _ in logged] == ["run_failed", "run_failure_not_recorded"]
    assert logged[0][1]["handled"].startswith("scrape lock lost (noticed after a failed category")
    assert run.finished_at is None
    assert "scrape lock lost" in result.output
    assert isinstance(result.exception, SystemExit)


_LAST_WAIT = len(main_mod._RUN_OUTCOME_WAITS_FOR_DATABASE)


@pytest.mark.parametrize("comes_back_on_wait", [2, _LAST_WAIT])
def test_fields_set_before_the_failure_survive_the_wait_for_the_server(
    lock_db, monkeypatch, comes_back_on_wait
):
    """Недоступный сервер в записи не отказывал: пока запись итога его ждёт, поля,
    выставленные на прогоне перед сбоем, не отбрасываются — в отличие от записи,
    которую база отклонила. Сервер, вернувшийся за последнюю паузу, тоже успел:
    после неё есть ещё одна попытка."""
    session = lock_db.session
    run = storage.Run(status="running", started_at=utcnow())
    session.add(run)
    session.commit()
    run_id = run.id
    run.catalog_verification_reason = "set right before the failure"
    lock_db.server.stops_accepting_connections()
    waited: list[float] = []

    def server_comes_back(seconds):
        waited.append(seconds)
        if len(waited) == comes_back_on_wait:
            lock_db.server.accepts_connections_again()

    monkeypatch.setattr(main_mod, "_wait_for_the_database", server_comes_back)

    def record_failure():
        run.status = "failed"
        run.error_message = "ValueError: boom"
        run.finished_at = utcnow()

    main_mod._write_failed_run_outcome(session, run, run_id, record_failure)

    session.rollback()
    session.expire_all()
    stored = session.get(storage.Run, run_id)
    assert waited == list(main_mod._RUN_OUTCOME_WAITS_FOR_DATABASE[:comes_back_on_wait])
    assert (stored.status, stored.error_message) == ("failed", "ValueError: boom")
    assert stored.catalog_verification_reason == "set right before the failure"


@pytest.mark.parametrize("comes_back_on_wait", [1, _LAST_WAIT])
def test_outcome_alone_also_waits_for_the_server(lock_db, monkeypatch, comes_back_on_wait):
    """База отклонила поле, выставленное на прогоне перед сбоем, — и тут же
    пропала. Вторая попытка, уже без поля, ждёт сервер так же, как первая, — все
    паузы, и после последней пишет итог."""
    session = lock_db.session
    run = storage.Run(status="running", started_at=utcnow())
    session.add(run)
    session.commit()
    run_id = run.id
    run.tenant_id = None  # NOT NULL: первую запись база не примет
    waited: list[float] = []

    def server_comes_back(seconds):
        waited.append(seconds)
        if len(waited) == comes_back_on_wait:
            lock_db.server.accepts_connections_again()

    monkeypatch.setattr(main_mod, "_wait_for_the_database", server_comes_back)
    real_log = main_mod.log

    class _ServerGoesAwayAfterTheRefusal:
        def __getattr__(self, name):
            def line(event_name, **fields):
                if event_name == "run_failure_write_retried":
                    lock_db.server.stops_accepting_connections()
                return getattr(real_log, name)(event_name, **fields)

            return line

    monkeypatch.setattr(main_mod, "log", _ServerGoesAwayAfterTheRefusal())

    def record_failure():
        run.status = "failed"
        run.error_message = "ValueError: boom"
        run.finished_at = utcnow()

    main_mod._write_failed_run_outcome(session, run, run_id, record_failure)

    session.rollback()
    session.expire_all()
    stored = session.get(storage.Run, run_id)
    assert waited == list(main_mod._RUN_OUTCOME_WAITS_FOR_DATABASE[:comes_back_on_wait])
    assert (stored.status, stored.tenant_id) == ("failed", 1)


def test_waiting_for_the_server_is_a_real_pause(monkeypatch):
    """Остальные тесты ожидание подменяют. Без настоящей паузы все попытки
    записи итога уложились бы в миллисекунды — раньше, чем сервер вернётся."""
    slept: list[float] = []
    monkeypatch.setattr(main_mod.time, "sleep", slept.append)

    _REAL_WAIT_FOR_THE_DATABASE(4)

    assert slept == [4]
    assert sum(main_mod._RUN_OUTCOME_WAITS_FOR_DATABASE) == 60, "всего ждёт минуту"


@pytest.mark.parametrize(
    ("error", "may_come_back"),
    [
        # Сервер недоступен, занят или оборвал соединение.
        (sa_exc.OperationalError("SELECT 1", {}, Exception("server closed the connection")), True),
        (sa_exc.InterfaceError("SELECT 1", {}, Exception("connection already closed")), True),
        (sa_exc.TimeoutError("QueuePool limit reached"), True),
        (sa_exc.DBAPIError("SELECT 1", {}, Exception("gone"), connection_invalidated=True), True),
        # Сервер ответил отказом в самой записи — ждать нечего.
        (sa_exc.IntegrityError("INSERT", {}, Exception("null value in column")), False),
        (sa_exc.DataError("INSERT", {}, Exception("value too long")), False),
        (sa_exc.PendingRollbackError("rollback first"), False),
        (ValueError("broken in code"), False),
    ],
)
def test_only_an_unreachable_server_is_waited_for(error, may_come_back):
    assert main_mod._database_may_come_back(error) is may_come_back


# ─── Соседние команды ────────────────────────────────────────────────────────


def test_scrape_command_does_not_write_without_the_lock(lock_db, monkeypatch):
    """Команда `scrape` (её зовут intraday-тики) пишет один раз, в конце. Замок
    потерян за время сбора — записи нет, прогон `failed`."""

    async def promos_while_the_lock_goes(self):
        lock_db.server.drops_the_lock_connection()
        return []

    monkeypatch.setattr(_CatalogScraper, "scrape_promos", promos_while_the_lock_goes)
    steps = _Steps(monkeypatch)
    catalog = _three_categories()

    run, _ = _run_catalog(
        lock_db.session,
        monkeypatch,
        catalog,
        expected_status="failed",
        command_line=("scrape",),
    )

    # `scrape` пишет причину без имени класса ошибки.
    assert run.error_message.startswith("scrape lock lost (noticed before the write): ")
    assert steps.done == [], "словарь стран не записан"
    assert _external_ids(lock_db.session) == []


def test_ai_crawl_command_does_not_write_without_the_lock(lock_db, monkeypatch):
    """`ai-crawl` пишет один раз, в конце обхода, — и без замка не пишет."""

    async def crawl_while_the_lock_goes(self, max_urls: int, dry_run: bool = False):
        lock_db.server.drops_the_lock_connection()
        return main_mod.ScrapeResult(site=SITE, products=[type(self).found])

    monkeypatch.setattr(_Crawler, "crawl", crawl_while_the_lock_goes)

    run = _ai_crawl(lock_db.session, monkeypatch, _scraped("a", 5.0, "cat-a"))

    assert run.error_message.startswith(_lost_lock_reason("before the write"))
    assert _external_ids(lock_db.session) == []


# ─── Сама проверка ───────────────────────────────────────────────────────────


def test_lock_is_asked_of_the_server_not_of_the_connection_object(lock_db):
    """Держит ли соединение замок, отвечает сервер — по своему списку замков.

    Спрашивать «отвечает ли соединение» нельзя: после обрыва SQLAlchemy на
    следующем запросе молча открывает вместо него новое, и оно отвечает — только
    замка за ним нет.
    """
    lock_key = {"key": run_lock.SCRAPE_ADVISORY_LOCK_KEY}
    with lock_db.engine.connect() as holder, lock_db.engine.connect() as bystander:
        assert run_lock.connection_holds_exclusive_scrape_lock(holder) is False
        holder.scalar(text("SELECT pg_advisory_lock(hashtext(:key))"), lock_key)
        holder.commit()

        assert run_lock.connection_holds_exclusive_scrape_lock(holder) is True
        assert run_lock.connection_holds_exclusive_scrape_lock(bystander) is False
        assert not holder.in_transaction(), "соединение замка не висит в открытой транзакции"

        lock_db.server.drops_the_lock_connection()

        with pytest.raises(OperationalError):
            run_lock.connection_holds_exclusive_scrape_lock(holder)
        holder.rollback()
        assert holder.scalar(text("SELECT 1")) == 1, "объект соединения снова отвечает"
        holder.commit()
        assert run_lock.connection_holds_exclusive_scrape_lock(holder) is False


@pytest.mark.parametrize(
    "look_alike",
    [
        # Тот же ключ, но замок разделяемый: так его берут читатели каталога
        # (`try_shared_scrape_read_lock`).
        "pg_advisory_lock_shared(hashtext(:key))",
        # Другой ключ — замок сопоставления.
        "pg_advisory_lock(hashtext('pharmacy_monitor_matcher'))",
        # Та же младшая половина ключа при другой старшей.
        "pg_advisory_lock(hashtext(:key)::bigint & 4294967295)",
        # Те же две половины, но ключ из двух чисел, а не из одного.
        "pg_advisory_lock((hashtext(:key)::bigint >> 32)::integer, hashtext(:key))",
    ],
)
def test_another_advisory_lock_of_the_session_is_not_taken_for_the_scrape_lock(lock_db, look_alike):
    """Сеанс держит advisory-замок, похожий на замок сбора, — но не его."""
    with lock_db.engine.connect() as connection:
        connection.execute(text(f"SELECT {look_alike}"), {"key": run_lock.SCRAPE_ADVISORY_LOCK_KEY})
        connection.commit()
        held_by_the_session = connection.scalar(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            )
        )
        connection.commit()

        assert held_by_the_session == 1
        assert run_lock.connection_holds_exclusive_scrape_lock(connection) is False


def test_lost_lock_stays_lost(lock_db, monkeypatch):
    """Замок потерян, и его тут же взял другой процесс. Команда, потерявшая
    замок, заново его не берёт и после первого отказа сервер больше не
    спрашивает: каждая следующая проверка отказывает сразу, с той же причиной.
    Выход команды замок нового владельца не трогает."""
    factory = sessionmaker(lock_db.engine, expire_on_commit=False, autoflush=False)
    asked: list[bool | str] = []
    real_question = main_mod.connection_holds_exclusive_scrape_lock

    def question(connection):
        try:
            answer = real_question(connection)
        except Exception as error:
            asked.append(type(error).__name__)
            raise
        asked.append(answer)
        return answer

    monkeypatch.setattr(main_mod, "connection_holds_exclusive_scrape_lock", question)
    other = create_engine(lock_db.engine.url)
    try:
        with other.connect() as next_scrape:
            with click.Context(click.Command("probe")):
                assert main_mod._hold_scrape_lock_until_command_exit(factory, wait=False) is True
                main_mod._fail_if_scrape_lock_lost(noticed="while it is held")
                main_mod._fail_if_scrape_lock_lost(noticed="while it is still held")
                assert asked == [True, True], "пока замок держится, сервер спрашивают каждый раз"

                lock_db.server.drops_the_lock_connection()
                took_it = next_scrape.scalar(
                    text("SELECT pg_try_advisory_lock(hashtext(:key))"),
                    {"key": run_lock.SCRAPE_ADVISORY_LOCK_KEY},
                )
                next_scrape.commit()
                assert took_it is True, "замок свободен: рядом стартует другой сбор"
                (new_owner,) = lock_db.server.lock_holders()

                for noticed in ("first", "again"):
                    with pytest.raises(main_mod.ScrapeLockLost) as lost:
                        main_mod._fail_if_scrape_lock_lost(noticed=noticed)
                    assert str(lost.value).startswith(
                        f"scrape lock lost (noticed {noticed}): "
                        "the check on its connection failed (OperationalError); "
                    )
                assert asked == [True, True, "OperationalError"], "второй раз не спрашивали"
            # Команда вышла — замок по-прежнему за новым владельцем.
            assert lock_db.server.lock_holders() == [new_owner]
    finally:
        other.dispose()


def test_lock_the_server_no_longer_lists_is_lost_even_on_a_live_connection(lock_db):
    """Соединение замка отвечает, а замка за ним сервер не числит (так выглядит
    соединение, которое SQLAlchemy молча открыл взамен оборванного). Это тоже
    потеря — со своей причиной."""
    factory = sessionmaker(lock_db.engine, expire_on_commit=False, autoflush=False)
    with click.Context(click.Command("probe")) as context:
        assert main_mod._hold_scrape_lock_until_command_exit(factory, wait=False) is True
        held = main_mod._SCRAPE_LOCKS_BY_COMMAND[context]
        held.connection.scalar(
            text("SELECT pg_advisory_unlock(hashtext(:key))"),
            {"key": run_lock.SCRAPE_ADVISORY_LOCK_KEY},
        )
        held.connection.commit()

        with pytest.raises(main_mod.ScrapeLockLost) as lost:
            main_mod._fail_if_scrape_lock_lost(noticed="later")

    assert str(lost.value) == (
        "scrape lock lost (noticed later): the server no longer lists it for its connection; "
        "another scrape, tick or rematch may have run while it was free, so the run stops here"
    )


def test_command_forgets_its_lock_on_exit(lock_db):
    """Замок числится за командой, пока она идёт: после выхода проверять нечего,
    а следующая команда того же процесса берёт свой."""
    factory = sessionmaker(lock_db.engine, expire_on_commit=False, autoflush=False)
    with click.Context(click.Command("first")) as first:
        assert main_mod._hold_scrape_lock_until_command_exit(factory, wait=False) is True
        assert first in main_mod._SCRAPE_LOCKS_BY_COMMAND
        assert len(lock_db.server.lock_holders()) == 1

    assert first not in main_mod._SCRAPE_LOCKS_BY_COMMAND
    assert lock_db.server.lock_holders() == []
    with click.Context(click.Command("second")):
        assert main_mod._hold_scrape_lock_until_command_exit(factory, wait=False) is True
        main_mod._fail_if_scrape_lock_lost(noticed="in the second command")
    assert lock_db.server.lock_holders() == []


def test_lock_whose_check_failed_on_a_live_connection_is_not_left_in_the_pool(lock_db, monkeypatch):
    """Проверка упала, а сеанс замка жив (сервер отменил её запрос): замок
    считается потерянным, хотя он ещё за сеансом, а транзакция проверки
    прервана — снять замок запросом уже нельзя. На выходе команда закрывает
    сеанс совсем: иначе замок остался бы на соединении в пуле до конца процесса,
    и следующий сбор не стартовал бы."""
    factory = sessionmaker(lock_db.engine, expire_on_commit=False, autoflush=False)

    def refused_by_the_server(connection):
        connection.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(main_mod, "connection_holds_exclusive_scrape_lock", refused_by_the_server)
    warned = _record_log(monkeypatch, main_mod, "warning")
    with click.Context(click.Command("probe")):
        assert main_mod._hold_scrape_lock_until_command_exit(factory, wait=False) is True
        with pytest.raises(main_mod.ScrapeLockLost, match="the check on its connection failed"):
            main_mod._fail_if_scrape_lock_lost(noticed="in the middle")
        assert len(lock_db.server.lock_holders()) == 1, "сеанс жив, замок ещё за ним"

    # Снять замок запросом команда и не пыталась: строки об отказе снятия нет.
    assert [name for name, _ in warned] == ["scrape_lock_check_failed"]
    assert lock_db.server.has_no_lock_holders_soon()
    with click.Context(click.Command("next scrape")):
        assert main_mod._hold_scrape_lock_until_command_exit(factory, wait=False) is True


def test_lock_that_could_not_be_released_is_not_left_in_the_pool(lock_db, monkeypatch):
    """Снять замок на выходе не удалось (транзакция на его соединении прервана).
    Соединение в пул не возвращается: сеанс закрывается, замок уходит с ним, а
    отказ ложится в журнал строкой `scrape_lock_release_failed`."""
    factory = sessionmaker(lock_db.engine, expire_on_commit=False, autoflush=False)
    warned = _record_log(monkeypatch, main_mod, "warning")
    with click.Context(click.Command("probe")) as context:
        assert main_mod._hold_scrape_lock_until_command_exit(factory, wait=False) is True
        lock_connection = main_mod._SCRAPE_LOCKS_BY_COMMAND[context].connection
        with pytest.raises(Exception, match="DivisionByZero"):
            lock_connection.execute(text("SELECT 1 / 0"))

    assert [name for name, _ in warned] == ["scrape_lock_release_failed"]
    assert lock_db.server.has_no_lock_holders_soon()


def test_nothing_is_checked_without_a_real_lock():
    """На SQLite замка нет, вне команды его некому держать: проверка молчит."""
    main_mod._fail_if_scrape_lock_lost(noticed="outside any command")
    with click.Context(click.Command("probe")):
        main_mod._fail_if_scrape_lock_lost(noticed="in a command that took no lock")


# ─── Сборщик ─────────────────────────────────────────────────────────────────


def test_callback_can_stop_the_scrape_while_its_ordinary_failure_cannot():
    """Обычная ошибка колбэка стоит одной записи категории, сбор идёт дальше.
    `ScrapeMustStop` останавливает его сразу: писать дальше вызывающему нельзя."""
    catalog = {"cat-a": [_scraped("a", 5.0, "cat-a")], "cat-b": [_scraped("b", 6.0, "cat-b")]}
    scraper = type("_Site", (_CatalogScraper,), {"catalog": catalog})
    called: list[str] = []

    def failing(error):
        def on_category(site, slug, products):
            called.append(slug)
            raise error

        return on_category

    result = asyncio.run(scraper().scrape(list(catalog), on_category=failing(ValueError("no"))))
    assert called == ["cat-a", "cat-b"]
    assert len(result.products) == 2

    called.clear()
    with pytest.raises(ScrapeMustStop, match="stop now"):
        asyncio.run(
            scraper().scrape(list(catalog), on_category=failing(ScrapeMustStop("stop now")))
        )
    assert called == ["cat-a"]
