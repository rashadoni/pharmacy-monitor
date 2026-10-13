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
(`pg_terminate_backend`). На SQLite замка нет, и проверять нечего; в CI эти
тесты обязаны исполняться — их пропуск там ошибка.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import click
import pytest
from sqlalchemy import create_engine, event, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from src import main as main_mod
from src import run_lock, storage
from src.scrapers.base import ScrapeMustStop
from tests.test_run_persist_db_failure import (
    SITE,
    _ai_crawl,
    _CatalogScraper,
    _Crawler,
    _external_ids,
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
    try:
        storage.Base.metadata.create_all(engine)
        with sessionmaker(engine, expire_on_commit=False, autoflush=False)() as session:
            yield SimpleNamespace(session=session, engine=engine, server=_Server(admin, database))
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
    (main_mod, "persist_aloe_country_mappings"),
    (main_mod, "_acquire_matcher_lock"),
    (main_mod, "_run_matching_stage"),
    (storage, "confirm_prices_of_latest_verified_runs"),
)


def _record_steps(monkeypatch, after: dict | None = None) -> list[str]:
    """Какие шаги после сбора прогон исполнил. Шаги настоящие; `after[имя]`
    вызывается, когда шаг `имя` закончил."""
    done: list[str] = []
    after = after or {}

    def recorded(owner, name):
        real = getattr(owner, name)

        def step(*args, **kwargs):
            result = real(*args, **kwargs)
            done.append(name)
            after.get(name, lambda: None)()
            return result

        return step

    for owner, name in _STEPS_AFTER_THE_SCRAPE:
        monkeypatch.setattr(owner, name, recorded(owner, name))
    return done


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

    steps = _record_steps(
        monkeypatch, after={name: count_holders for _, name in _STEPS_AFTER_THE_SCRAPE}
    )
    writes = _record_writes(monkeypatch)
    catalog = _three_categories(when_asked={"cat-b": count_holders})

    run, _ = _run_catalog(lock_db.session, monkeypatch, catalog)

    assert steps == [name for _, name in _STEPS_AFTER_THE_SCRAPE]
    assert holders_at_each_step == [1, 1, 1, 1, 1]
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
    about_to_write: list[str] = []

    def server_restarts(conn, cursor, statement, parameters, context, executemany):
        if about_to_write != ["cat-b"]:
            return
        about_to_write.append("every connection dropped")
        lock_db.server.drops_every_connection()

    def second_write_begins(writes):
        if writes == [("cat-a", "ok")]:
            about_to_write.append("cat-b")

    # После первого запроса записи: транзакция уже открыта.
    event.listen(lock_db.engine, "after_cursor_execute", server_restarts)
    writes = _record_writes(monkeypatch, before_write=second_write_begins)
    steps = _record_steps(monkeypatch)
    catalog = _three_categories()
    try:
        run, result = _run_catalog(lock_db.session, monkeypatch, catalog, expected_status="failed")
    finally:
        event.remove(lock_db.engine, "after_cursor_execute", server_restarts)

    assert about_to_write == ["cat-b", "every connection dropped"]
    assert writes == [("cat-a", "ok"), ("cat-b", "OperationalError")]
    assert catalog.asked == ["cat-a", "cat-b"], "после потери замка сайт не дособирается"
    assert steps == [], "ни словаря стран, ни сопоставления, ни подтверждения цен"
    assert run.error_message.startswith(_lost_lock_reason("after a failed category write"))
    assert run.finished_at is not None
    assert _external_ids(lock_db.session) == ["a"]
    assert isinstance(result.exception, SystemExit), "команда вышла сама, а не упала"
    assert "scrape lock lost" in result.output


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
    steps = _record_steps(monkeypatch)
    catalog = _three_categories(
        site, when_asked={"cat-b": lock_db.server.drops_the_lock_connection}
    )

    run, _ = _run_catalog(
        lock_db.session, monkeypatch, catalog, site=site, expected_status="failed"
    )

    assert run.error_message.startswith(_lost_lock_reason("before a category write"))
    assert catalog.asked == ["cat-a", "cat-b"]
    assert writes == ([("cat-a", "ok")] if site == SITE else [])
    assert steps == []
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
    steps = _record_steps(monkeypatch)
    catalog = _three_categories()

    run, _ = _run_catalog(lock_db.session, monkeypatch, catalog, expected_status="failed")

    assert run.error_message.startswith(_lost_lock_reason("after the scrape"))
    assert writes == [("cat-a", "ok"), ("cat-b", "ok"), ("cat-c", "ok")], "финального прохода нет"
    assert steps == []


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
    этап не начинается: без замка прогон не пишет финальный проход, не
    сопоставляет, не подтверждает цены и не публикуется как `ok`.

    Оборвано либо одно соединение с замком — тогда ни один запрос прогона не
    падает, и остановить его может только проверка, — либо все разом: вместе с
    ним уходит и замок сопоставления, а итог всё равно записан.
    """
    all_steps = [name for _, name in _STEPS_AFTER_THE_SCRAPE]
    steps = _record_steps(monkeypatch, after={lost_after: getattr(lock_db.server, dropped)})
    writes = _record_writes(monkeypatch)
    catalog = _Catalog({"cat-a": [_scraped("a", 5.0, "cat-a")]})

    run, _ = _run_catalog(lock_db.session, monkeypatch, catalog, expected_status="failed")

    assert run.error_message.startswith(reason), run.error_message
    assert steps == all_steps[: all_steps.index(lost_after) + 1], "следующий этап не начат"
    final_write = [("cat-a", "ok")] if lost_after != "persist_aloe_country_mappings" else []
    assert writes == [("cat-a", "ok"), *final_write]
    assert run.finished_at is not None
    for key in (
        run_lock.SCRAPE_ADVISORY_LOCK_KEY,
        main_mod.matcher.MATCH_MUTATION_ADVISORY_LOCK_KEY,
    ):
        assert lock_db.server.lock_holders(key) == [], "команда не оставила за собой замков"


# ─── Соседние команды ────────────────────────────────────────────────────────


def test_scrape_command_does_not_write_without_the_lock(lock_db, monkeypatch):
    """Команда `scrape` (её зовут intraday-тики) пишет один раз, в конце. Замок
    потерян за время сбора — записи нет, прогон `failed`."""

    async def promos_while_the_lock_goes(self):
        lock_db.server.drops_the_lock_connection()
        return []

    monkeypatch.setattr(_CatalogScraper, "scrape_promos", promos_while_the_lock_goes)
    steps = _record_steps(monkeypatch)
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
    assert steps == []
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


def test_shared_reader_lock_is_not_taken_for_the_producer_lock(lock_db):
    """Читатели каталога берут тот же ключ разделяемым замком
    (`try_shared_scrape_read_lock`) — это не замок сбора."""
    with lock_db.engine.connect() as reader:
        reader.scalar(
            text("SELECT pg_advisory_lock_shared(hashtext(:key))"),
            {"key": run_lock.SCRAPE_ADVISORY_LOCK_KEY},
        )
        reader.commit()

        assert lock_db.server.lock_holders() != []
        assert run_lock.connection_holds_exclusive_scrape_lock(reader) is False


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

    assert str(lost.value).startswith(
        "scrape lock lost (noticed later): the server no longer lists it for its connection; "
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
