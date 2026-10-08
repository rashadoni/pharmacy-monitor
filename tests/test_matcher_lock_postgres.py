"""Замок сопоставления держится весь этап — на настоящих соединениях PostgreSQL.

Этап сопоставления (конец сбора, `rematch`) берёт сессионный advisory-замок и
под ним делает несколько транзакций. Сессионный замок принадлежит соединению, а
сессия, привязанная к движку, после каждого commit возвращает соединение в пул.
Пока замок брался через саму сессию, он жил ровно до тех пор, пока пул отдавал
ей то же соединение:

* соединение старше `pool_recycle` (на проде 30 минут) пул на очередной выдаче
  закрывает и открывает новое — замок пропадал после commit `match_products`, а
  `pg_advisory_unlock` в конце этапа уходил в соединение, которое замок не
  держит, и молча возвращал false. В журнале PostgreSQL прода это 12 строк
  `you don't own a lock of type ExclusiveLock` с 3 сентября по 7 октября 2026 —
  ровно те этапы, у которых тридцатая минута процесса пришлась между взятием
  замка и commit `match_products`;
* если тот же процесс между транзакциями брал из пула ещё одно соединение,
  сессия получала другое, и первый же шаг этапа вставал на транзакционный
  замок с тем же ключом — в очередь за собственным процессом.

Теперь замок берётся на выделенном соединении вне пула, и до снятия сессия
работает только через него. На SQLite замка нет — тесты пропускаются.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import pytest
import structlog
from sqlalchemy import Engine, create_engine, event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from src import main as main_mod
from src import matcher
from src.storage import Base, Match, Product

# Строки pg_locks с замком сопоставления: односложный ключ bigint лежит в
# classid (старшие 32 бита) и objid (младшие), objsubid = 1.
_LOCK_HOLDERS = text(
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
    ORDER BY locks.pid
    """
)
_KEY = {"key": matcher.MATCH_MUTATION_ADVISORY_LOCK_KEY}
# Пауза длиннее `pool_recycle` движка этапа: следующую выдачу пул начнёт с
# закрытия соединения.
_POOL_RECYCLE_SECONDS = 1
# Имя приложения у сеансов «процесса этапа» — своё на каждый запуск pytest: по нему
# их считают и убирают, не задевая чужие прогоны на том же сервере.
_STAGE_APPLICATION = f"matcher_lock_stage_{uuid.uuid4().hex[:8]}"
_OLDER_THAN_POOL_RECYCLE = 1.3


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql"):
        return url
    if os.environ.get("CI"):
        pytest.fail(
            "в CI тесты замка сопоставления обязаны исполняться, а DATABASE_URL — "
            "не PostgreSQL: на SQLite замка нет, и проверять нечего."
        )
    pytest.skip("PostgreSQL DATABASE_URL is required")


@pytest.fixture(scope="module")
def schema() -> Iterator[tuple[str, str]]:
    """Своя схема с таблицами проекта — одна на файл: `create_all` дороже самих тестов."""
    url = _database_url()
    name = f"matcher_lock_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{name}"'))
    # lock_timeout: ожидание замка, которого быть не должно, роняет тест, а не вешает.
    options = f"-csearch_path={name} -clock_timeout=5000"
    builder = create_engine(url, connect_args={"options": options})
    Base.metadata.create_all(builder)
    builder.dispose()
    try:
        yield url, options
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        admin.dispose()


@pytest.fixture
def engines(schema) -> Iterator[tuple[Callable[..., Engine], Engine]]:
    """Фабрика движков «процесса этапа» и движок постороннего процесса.

    Параметры пула — как в `storage.make_engine`; `pool_recycle` задаёт тест:
    у настоящего движка он не бывает меньше 30 секунд.
    """
    url, options = schema
    created: list[Engine] = []

    def stage_engine(*, pool_recycle: int = 1800, pool_pre_ping: bool = True) -> Engine:
        engine = create_engine(
            url,
            # По имени приложения сеансы «процесса этапа» отличимы от чужих в той же базе.
            connect_args={"options": f"{options} -capplication_name={_STAGE_APPLICATION}"},
            pool_size=5,
            max_overflow=5,
            pool_timeout=10,
            pool_recycle=pool_recycle,
            pool_pre_ping=pool_pre_ping,
            pool_use_lifo=True,
        )
        created.append(engine)
        return engine

    outsider = create_engine(url, connect_args={"options": options})
    try:
        yield stage_engine, outsider
    finally:
        for engine in created:
            engine.dispose()
        with outsider.begin() as connection:
            # Соединение замка — вне пула, dispose его не закрывает: упавший между
            # взятием и снятием тест оставил бы замок всем следующим файлам.
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND application_name = :application"
                ),
                {"application": _STAGE_APPLICATION},
            )
            connection.execute(text("TRUNCATE products, matches CASCADE"))
        outsider.dispose()


def _session(bind) -> Session:
    """Сессия как в `storage.make_session`."""
    return sessionmaker(bind, expire_on_commit=False, autoflush=False)()


def _lock_holders(outsider: Engine) -> list[int]:
    with outsider.connect() as connection:
        return list(connection.scalars(_LOCK_HOLDERS, _KEY))


def _outsider_can_take_the_lock(outsider: Engine) -> bool:
    """Так замок пробует второй `rematch`; взятое сразу отпускаем."""
    with outsider.connect() as connection:
        taken = bool(connection.scalar(text("SELECT pg_try_advisory_lock(hashtext(:key))"), _KEY))
        if taken:
            connection.scalar(text("SELECT pg_advisory_unlock(hashtext(:key))"), _KEY)
        connection.commit()
        return taken


def _waiting_for_the_lock(outsider: Engine) -> int:
    with outsider.connect() as connection:
        return connection.scalar(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            )
        )


def _backend_pid(session: Session) -> int:
    return session.scalar(text("SELECT pg_backend_pid()"))


def _settles(outsider: Engine, query: str, expected: int, **params) -> bool:
    """Сервер завершает сеанс чуть позже, чем клиент закрыл сокет, — ждём до пяти секунд."""
    deadline = time.monotonic() + 5
    with outsider.connect() as connection:
        while time.monotonic() < deadline:
            value = connection.scalar(text(query), params)
            connection.commit()
            if value == expected:
                return True
            time.sleep(0.02)
    return False


def _wait_until_backend_is_gone(outsider: Engine, pid: int) -> None:
    gone = _settles(outsider, "SELECT count(*) FROM pg_stat_activity WHERE pid = :pid", 0, pid=pid)
    assert gone, f"соединение {pid}, державшее замок, осталось открытым"


def _product(session: Session, site: str, tag: str) -> Product:
    product = Product(
        tenant_id=1,
        site=site,
        external_id=tag,
        url=f"https://{site}.example/product/{tag}",
        name="Aspirin Kardio 100 mq 30 tablet",
        name_normalized="",
    )
    session.add(product)
    session.flush()
    return product


def _products_seen_by(outsider: Engine) -> int:
    with outsider.connect() as connection:
        return connection.scalar(select(func.count()).select_from(Product))


def test_lock_survives_a_connection_the_pool_recycles_between_commits(engines):
    """Тридцатая минута процесса посреди этапа: замок остаётся, сессия — на его соединении."""
    make_stage_engine, outsider = engines
    engine = make_stage_engine(pool_recycle=_POOL_RECYCLE_SECONDS)

    with _session(engine) as witness, _session(engine) as session:
        assert matcher.acquire_match_mutation_lock(session, wait=True)
        holders = _lock_holders(outsider)
        assert len(holders) == 1

        # Свидетель — обычная сессия того же пула: по ней видно, что пул в этом
        # прогоне действительно пересоздаёт соединение, а не тест прошёл впустую.
        # Соединение он берёт после замка: то, что лежало в пуле, забрал замок.
        witness_pid = _backend_pid(witness)
        witness.commit()

        session.execute(text("SELECT 1"))
        session.commit()
        time.sleep(_OLDER_THAN_POOL_RECYCLE)

        assert _backend_pid(witness) != witness_pid
        assert _backend_pid(session) == holders[0]
        assert _lock_holders(outsider) == holders
        assert not _outsider_can_take_the_lock(outsider)
        # Шаг этапа берёт транзакционный замок с тем же ключом и не ждёт сам себя.
        matcher.acquire_match_mutation_xact_lock(session)
        session.commit()

        with structlog.testing.capture_logs() as logs:
            matcher.release_match_mutation_lock(session)
        assert logs == []
        assert _lock_holders(outsider) == []
        assert session.get_bind() is engine
        _wait_until_backend_is_gone(outsider, holders[0])


def test_lock_stays_with_the_session_when_the_pool_hands_out_another_connection(engines):
    """Тот же процесс взял из пула ещё одно соединение — шаг этапа не встаёт за своим замком."""
    make_stage_engine, outsider = engines
    engine = make_stage_engine()

    with _session(engine) as session:
        # Соединение, которым сессия пользовалась до этапа, лежит в пуле первым на выдачу.
        session.execute(text("SELECT 1"))
        session.commit()
        assert matcher.acquire_match_mutation_lock(session, wait=True)
        holders = _lock_holders(outsider)
        session.execute(text("SELECT 1"))
        session.commit()

        # Так соединение берут вторая Session() и try_shared_scrape_read_lock.
        with engine.connect() as neighbour:
            neighbour.execute(text("SELECT 1"))
            matcher.acquire_match_mutation_xact_lock(session)
            assert _backend_pid(session) == holders[0]
            session.commit()

        matcher.release_match_mutation_lock(session)
        assert _lock_holders(outsider) == []


def test_edit_queued_behind_the_stage_runs_only_after_its_last_step(engines, monkeypatch):
    """Настоящий этап: правка, ждущая замок, не проходит между его шагами.

    Правка пары (`match_actions`) берёт транзакционный замок с тем же ключом и
    всё время этапа стоит в очереди. Когда пул закрывал соединение с замком,
    сервер отдавал замок ей — после commit `match_products`, до `revalidate_split`,
    который дальше работал с тем, что прочитал до неё.
    """
    make_stage_engine, outsider = engines
    engine = make_stage_engine(pool_recycle=_POOL_RECYCLE_SECONDS)
    with _session(outsider) as seed:
        _product(seed, "pharmonline", "client")
        _product(seed, "aloe", "competitor")
        seed.commit()

    order: list[str] = []
    real_revalidate = matcher.revalidate_split
    real_flag = matcher.flag_suspected_mismatches

    def revalidate_after_the_pool_recycled(session, **kwargs):
        # match_products только что сделал commit; первый запрос revalidate —
        # первая выдача соединения после него.
        time.sleep(_OLDER_THAN_POOL_RECYCLE)
        result = real_revalidate(session, **kwargs)
        order.append("revalidate_split")
        return result

    def flag_after_revalidate(session):
        result = real_flag(session)
        order.append("flag_suspected_mismatches")
        return result

    monkeypatch.setattr(matcher, "revalidate_split", revalidate_after_the_pool_recycled)
    monkeypatch.setattr(matcher, "flag_suspected_mismatches", flag_after_revalidate)

    def manual_edit() -> None:
        try:
            with outsider.begin() as connection:
                # Правка ждёт весь этап; общий короткий lock_timeout здесь мешал бы.
                connection.execute(text("SET LOCAL lock_timeout = '60s'"))
                connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), _KEY)
                # Отметка — до commit: этап не продолжит раньше, чем она поставлена.
                order.append("manual edit")
        except Exception as failed:
            order.append(f"manual edit failed: {failed!r}")

    with _session(engine) as session, structlog.testing.capture_logs() as logs:
        # Как в конце сбора: сессия к этапу уже поработала и зафиксировала своё.
        session.execute(text("SELECT 1"))
        session.commit()
        assert main_mod._acquire_matcher_lock(session, wait=True)
        edit = threading.Thread(target=manual_edit, daemon=True)
        try:
            edit.start()
            deadline = time.monotonic() + 5
            while not _waiting_for_the_lock(outsider) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert _waiting_for_the_lock(outsider) == 1, "правка не встала на замок"
            summary = main_mod._run_matching_stage(session)
        finally:
            main_mod._release_matcher_lock(session)
        # revalidate ничего не разбил и свой транзакционный замок ещё держит —
        # его отпускает commit вызывающего, как в конце сбора.
        session.commit()
        edit.join(10)
        assert not edit.is_alive(), "правка не прошла после снятия замка"

    assert order == ["revalidate_split", "flag_suspected_mismatches", "manual edit"]
    assert _lock_holders(outsider) == []
    assert not [entry for entry in logs if entry["event"].startswith("matcher_lock_")]
    # Этап при этом сделал свою работу — через то же соединение.
    assert summary["clusters"] == 1
    with _session(outsider) as check:
        assert check.scalar(select(func.count()).select_from(Match)) == 1
        assert {p.canonical_id for p in check.scalars(select(Product))} != {None}


@pytest.mark.parametrize("ending", ["commit", "rollback", "close"])
def test_release_leaves_the_open_transaction_to_its_owner(engines, ending):
    """Снятие замка не фиксирует и не откатывает работу сессии — это решает вызывающий."""
    make_stage_engine, outsider = engines
    engine = make_stage_engine()

    with _session(engine) as session:
        assert matcher.acquire_match_mutation_lock(session, wait=True)
        holders = _lock_holders(outsider)
        _product(session, "aloe", "written-under-the-lock")

        matcher.release_match_mutation_lock(session)

        # Замок снят сразу, а транзакция жива и по-прежнему не видна снаружи.
        assert _lock_holders(outsider) == []
        assert _products_seen_by(outsider) == 0
        # Точка сохранения внутри неё — ещё не конец транзакции.
        with session.begin_nested():
            _product(session, "aloe", "written-in-a-savepoint")
        assert _backend_pid(session) == holders[0]
        getattr(session, ending)()

        assert _products_seen_by(outsider) == (2 if ending == "commit" else 0)
        # Соединение замка закрыто вместе с транзакцией, дальше сессия ходит через пул.
        _wait_until_backend_is_gone(outsider, holders[0])
        assert session.get_bind() is engine
        assert _backend_pid(session) != holders[0]


def test_pool_never_gets_the_closed_lock_connection_back(engines):
    """Соединение замка закрывается, а не возвращается в пул закрытым.

    Замок забирает из пула то соединение, которым сессия пользовалась до этапа.
    Вернись оно в пул после закрытия, следующий запрос сессии получил бы мёртвое
    соединение; на проде это скрыл бы `pool_pre_ping`, здесь он выключен.
    """
    make_stage_engine, outsider = engines
    engine = make_stage_engine(pool_pre_ping=False)

    with _session(engine) as session:
        before = _backend_pid(session)
        session.commit()
        assert matcher.acquire_match_mutation_lock(session, wait=True)
        assert _lock_holders(outsider) == [before]
        matcher.release_match_mutation_lock(session)

        assert _backend_pid(session) != before
        session.commit()
    assert engine.pool.checkedout() == 0


def test_busy_lock_is_refused_and_leaves_the_session_on_the_pool(engines, recwarn):
    """Замок занят: отказ, сессия остаётся на пуле, лишнего соединения не остаётся."""
    make_stage_engine, outsider = engines
    engine = make_stage_engine()

    with outsider.connect() as holder, _session(engine) as session:
        assert holder.scalar(text("SELECT pg_try_advisory_lock(hashtext(:key))"), _KEY)
        holder.commit()
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        holder.commit()

        assert matcher.acquire_match_mutation_lock(session, wait=False) is False

        assert session.get_bind() is engine
        assert session.info == {}
        assert _lock_holders(outsider) == [holder_pid]
        # Отказавшая попытка не оставила своего сеанса: других соединений этот
        # «процесс» не открывал, значит на сервере их не должно быть вовсе.
        assert engine.pool.checkedout() == 0
        assert _settles(
            outsider,
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = :application",
            0,
            application=_STAGE_APPLICATION,
        )
        # И закрыто оно явно, а не сборщиком мусора за брошенным объектом.
        assert not [w for w in recwarn if issubclass(w.category, ResourceWarning)]

        holder.scalar(text("SELECT pg_advisory_unlock(hashtext(:key))"), _KEY)
        holder.commit()


def test_work_pending_before_the_lock_is_committed_not_lost(engines):
    """Открытая транзакция сессии фиксируется: перенести её на соединение замка нельзя."""
    make_stage_engine, outsider = engines
    engine = make_stage_engine()

    with _session(engine) as session:
        _product(session, "aloe", "written-before-the-lock")
        assert _products_seen_by(outsider) == 0

        assert matcher.acquire_match_mutation_lock(session, wait=True)

        assert _products_seen_by(outsider) == 1
        matcher.acquire_match_mutation_xact_lock(session)
        session.commit()
        matcher.release_match_mutation_lock(session)
    assert _lock_holders(outsider) == []


def test_refused_acquire_has_already_committed_the_open_transaction(engines):
    """Фиксация идёт до попытки взять замок: при отказе работа сессии уже в базе."""
    make_stage_engine, outsider = engines
    engine = make_stage_engine()

    with outsider.connect() as holder, _session(engine) as session:
        assert holder.scalar(text("SELECT pg_try_advisory_lock(hashtext(:key))"), _KEY)
        holder.commit()
        _product(session, "aloe", "written-before-the-refusal")

        assert matcher.acquire_match_mutation_lock(session, wait=False) is False

        assert _products_seen_by(outsider) == 1
        assert not session.in_transaction()
        holder.scalar(text("SELECT pg_advisory_unlock(hashtext(:key))"), _KEY)
        holder.commit()


def test_session_bound_to_a_connection_keeps_the_lock_on_it(engines):
    """Сессию, которую вызывающий сам посадил на соединение, функция не перепривязывает."""
    make_stage_engine, outsider = engines
    engine = make_stage_engine()

    with engine.connect() as connection, _session(connection) as session:
        assert matcher.acquire_match_mutation_lock(session, wait=False)
        assert session.get_bind() is connection
        assert _lock_holders(outsider) == [_backend_pid(session)]
        session.commit()
        assert not _outsider_can_take_the_lock(outsider)

        with structlog.testing.capture_logs() as logs:
            matcher.release_match_mutation_lock(session)
        session.commit()
        assert logs == []
        assert _lock_holders(outsider) == []


@pytest.mark.parametrize("ending", ["rollback", "close"])
def test_lock_leaves_with_the_transaction_when_the_query_could_not_release_it(engines, ending):
    """Транзакция этапа оборвана ошибкой SQL: снять замок запросом нельзя, но он не остаётся.

    Соединение замка закрывается, как только сессия закончит на нём транзакцию:
    в `rematch` и в `run` это выход из `with Session()` (обработчик сбоя `run`
    на оборванной транзакции падает сам и до rollback не доходит).
    """
    make_stage_engine, outsider = engines
    engine = make_stage_engine()

    with _session(engine) as session:
        assert main_mod._acquire_matcher_lock(session, wait=True)
        holders = _lock_holders(outsider)
        with pytest.raises(DBAPIError):
            session.execute(text("SELECT * FROM no_such_table_in_this_schema"))

        with structlog.testing.capture_logs() as logs:
            main_mod._release_matcher_lock(session)
        assert [entry["event"] for entry in logs] == ["matcher_lock_release_failed"]
        assert session.get_bind() is engine

        getattr(session, ending)()
        _wait_until_backend_is_gone(outsider, holders[0])
        assert _lock_holders(outsider) == []
        assert _outsider_can_take_the_lock(outsider)


def test_lock_leaves_with_the_transaction_after_an_interrupt_mid_query(engines):
    """Ctrl-C посреди запроса этапа: SQLAlchemy бросает соединение вне пула открытым.

    После исключения-выхода он считает соединение потерянным и отпускает его;
    пуловое при этом закрывается, а отсоединённое остаётся жить на сервере
    вместе с замком — пока его не подберёт сборщик мусора. Закрываем сами.
    """
    make_stage_engine, outsider = engines
    engine = make_stage_engine()

    @event.listens_for(engine, "do_execute")
    def interrupt(cursor, statement, parameters, context):
        if "interrupted" in statement:
            raise KeyboardInterrupt

    with _session(engine) as session:
        assert main_mod._acquire_matcher_lock(session, wait=True)
        holders = _lock_holders(outsider)
        # Ссылка держит объект соединения живым: сборщик мусора тест не выручит.
        raw = session.info[matcher._LOCK_CONNECTION_INFO_KEY].raw
        with pytest.raises(KeyboardInterrupt):
            session.execute(text("SELECT 'interrupted'"))
        # Откатили и пошли дальше: сессия уже на соединении из пула, и шаг встал бы
        # на транзакционный замок за брошенным соединением — он отказывает сразу.
        session.rollback()
        with pytest.raises(RuntimeError, match="no longer on the connection"):
            matcher.acquire_match_mutation_xact_lock(session)
        main_mod._release_matcher_lock(session)

        session.close()
        assert raw.closed
        _wait_until_backend_is_gone(outsider, holders[0])
        assert _lock_holders(outsider) == []
    assert engine.pool.checkedout() == 0


def test_interrupt_while_waiting_for_the_lock_leaves_no_connection_behind(engines):
    """Ctrl-C во время ожидания замка: соединение, на котором его ждали, закрыто."""
    make_stage_engine, outsider = engines
    engine = make_stage_engine()

    @event.listens_for(engine, "do_execute")
    def interrupt(cursor, statement, parameters, context):
        if "pg_advisory_lock" in statement:
            raise KeyboardInterrupt

    with _session(engine) as session:
        # excinfo держит кадры стека с брошенным соединением: сборщик мусора не выручит.
        with pytest.raises(KeyboardInterrupt) as excinfo:
            matcher.acquire_match_mutation_lock(session, wait=True)
        assert excinfo.traceback
        assert session.get_bind() is engine
        assert session.info == {}
        assert _settles(
            outsider,
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = :application",
            0,
            application=_STAGE_APPLICATION,
        )


def test_step_refuses_to_continue_after_the_lock_connection_died(engines):
    """Соединение замка оборвано: шаг этапа не продолжает на соединении из пула без замка."""
    make_stage_engine, outsider = engines
    engine = make_stage_engine()

    with _session(engine) as session:
        assert matcher.acquire_match_mutation_lock(session, wait=True)
        holders = _lock_holders(outsider)
        matcher.acquire_match_mutation_xact_lock(session)
        session.commit()

        with outsider.connect() as connection:
            connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": holders[0]})
            connection.commit()
        with pytest.raises(DBAPIError):
            session.execute(text("SELECT 1"))
        # После rollback SQLAlchemy переподключает сессию через пул — уже без замка.
        session.rollback()
        assert _lock_holders(outsider) == []

        with pytest.raises(RuntimeError, match="no longer on the connection"):
            matcher.acquire_match_mutation_xact_lock(session)
        with structlog.testing.capture_logs() as logs:
            matcher.release_match_mutation_lock(session)
        assert [entry["event"] for entry in logs] == ["matcher_lock_not_held_at_release"]
        session.rollback()
        assert session.get_bind() is engine
    assert engine.pool.checkedout() == 0


def test_second_acquire_on_the_same_session_is_refused(engines):
    """Повторное взятие той же сессией — ошибка вызывающего, а не второй счётчик замка."""
    make_stage_engine, outsider = engines
    engine = make_stage_engine()

    with _session(engine) as session:
        assert matcher.acquire_match_mutation_lock(session, wait=True)
        with pytest.raises(RuntimeError, match="already holds"):
            matcher.acquire_match_mutation_lock(session, wait=True)
        matcher.release_match_mutation_lock(session)
    assert _lock_holders(outsider) == []
