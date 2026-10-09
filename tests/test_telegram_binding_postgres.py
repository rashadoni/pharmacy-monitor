"""Привязка Telegram под одновременными запросами — на настоящем PostgreSQL.

`tests/test_telegram_binding.py` идёт на SQLite, а он `FOR UPDATE` не знает и
пишет по одному. Здесь то, что держат замки строк в `src/telegram_binding.py`:

* код гасят под замком строки его пользователя — как и выдают: код, который в
  эту секунду заменяют новым, не срабатывает;
* два кода из одного чата одновременно не привязывают его к двум аккаунтам;
* один код из восьми чатов сразу привязывает один чат;
* два запроса кода сразу (у API два воркера, а кнопку нажимают дважды) не
  роняют друг друга и оставляют один код;
* сбой базы выходит без номера чата в тексте, и сессия после него годна.

Без PostgreSQL в `DATABASE_URL` тесты пропускаются; в CI пропуск — ошибка.
"""

from __future__ import annotations

import os
import threading
import traceback
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from src import telegram_binding
from src._time import utcnow
from src.storage import Base, TelegramBindCode, Tenant, TenantUser
from src.telegram_binding import BindOutcome


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql"):
        return url
    if os.environ.get("CI"):
        pytest.fail(
            "в CI тесты привязки Telegram под одновременными запросами обязаны "
            "исполняться, а DATABASE_URL — не PostgreSQL: SQLite строк не запирает."
        )
    pytest.skip("PostgreSQL DATABASE_URL is required")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Своя схема с таблицами проекта — одна на файл."""
    url = _database_url()
    name = f"telegram_binding_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{name}"'))
    # lock_timeout: ожидание замка, которого быть не должно, роняет тест, а не вешает.
    engine = create_engine(
        url,
        connect_args={"options": f"-csearch_path={name} -clock_timeout=10000"},
        pool_size=12,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        admin.dispose()


def _session(engine: Engine) -> Session:
    """Сессия как в `storage.make_session`."""
    return sessionmaker(engine, expire_on_commit=False, autoflush=False)()


def _new_user(engine: Engine) -> int:
    with _session(engine) as session:
        tenant = session.scalar(select(Tenant).limit(1))
        if tenant is None:
            tenant = Tenant(slug="default", name="Default", client_site="pharmonline")
            session.add(tenant)
            session.flush()
        user = TenantUser(
            tenant_id=tenant.id,
            email=f"{uuid.uuid4().hex[:8]}@client.example",
            role="admin",
            is_active=True,
            created_at=utcnow(),
        )
        session.add(user)
        session.commit()
        return user.id


@pytest.fixture
def account(engine) -> Iterator[int]:
    yield _new_user(engine)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE telegram_bind_codes, telegram_bind_attempts"))
        connection.execute(text("TRUNCATE audit_logs"))
        connection.execute(text("DELETE FROM tenant_users"))


def _chat_of(engine: Engine, user_id: int) -> str | None:
    with _session(engine) as session:
        return session.execute(
            select(TenantUser.telegram_chat_id).where(TenantUser.id == user_id)
        ).scalar_one()


def test_a_code_being_replaced_right_now_does_not_work(engine, account):
    """Гашение ждёт выдачу и после неё читает код заново.

    Соединение теста — выдача нового кода на полпути: строка пользователя уже
    заперта, прежний код ещё не стёрт. Гашение, которое не ждало бы или
    поверило бы прочитанному до ожидания, привязало бы чат по коду, который
    пользователь только что заменил.
    """
    with _session(engine) as session:
        code = telegram_binding.issue_code(session, account).code
    outcome: list[BindOutcome] = []

    def redeem() -> None:
        with _session(engine) as session:
            outcome.append(telegram_binding.bind_chat(session, "700500", code))

    with engine.connect() as issuing:
        issuing.execute(select(TenantUser.id).where(TenantUser.id == account).with_for_update())
        thread = threading.Thread(target=redeem)
        thread.start()
        thread.join(timeout=1.5)
        waited = thread.is_alive()
        issuing.execute(TelegramBindCode.__table__.delete())
        issuing.commit()
    thread.join(timeout=15)

    assert not thread.is_alive()
    assert waited, "гашение не ждало выдачи"
    assert outcome == [BindOutcome.REFUSED]
    assert _chat_of(engine, account) is None


def test_two_requests_from_one_chat_go_one_after_another(engine, account):
    """«Чат свободен» — обычное чтение, поэтому запросы одного чата идут по одному.

    Соединение теста — другой запрос того же чата: он начал раньше и привязывает
    чат к соседнему аккаунту. Без очереди этот запрос прочёл бы «чат свободен» и
    привязал его ко второму аккаунту тоже.
    """
    chat = "700400"
    neighbour = _new_user(engine)
    with _session(engine) as session:
        code = telegram_binding.issue_code(session, account).code
    outcome: list[BindOutcome] = []

    def redeem() -> None:
        with _session(engine) as session:
            outcome.append(telegram_binding.bind_chat(session, chat, code))

    with engine.connect() as earlier:
        earlier.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": telegram_binding.chat_lock_key(chat)},
        )
        thread = threading.Thread(target=redeem)
        thread.start()
        thread.join(timeout=1.5)
        waited = thread.is_alive()
        earlier.execute(
            update(TenantUser).where(TenantUser.id == neighbour).values(telegram_chat_id=chat)
        )
        earlier.commit()
    thread.join(timeout=15)

    assert not thread.is_alive()
    assert waited, "второй запрос чата не ждал первого"
    assert outcome == [BindOutcome.REFUSED]
    assert _chat_of(engine, account) is None
    assert _chat_of(engine, neighbour) == chat


def test_a_database_failure_names_no_chat_and_leaves_the_session_usable(engine, account):
    """Очередь чата занята дольше, чем запрос согласен ждать, — PostgreSQL отказывает.

    В тексте такой ошибки SQLAlchemy пишет параметры запроса — ключ замка с
    номером чата. А транзакция после ошибки оборвана: без отката любая
    следующая команда той же сессии падает.
    """
    chat = "700600"
    with engine.connect() as holder:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": telegram_binding.chat_lock_key(chat)},
        )
        with _session(engine) as session:
            session.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with (
                capture_logs() as logs,
                pytest.raises(telegram_binding.BindingStorageError) as raised,
            ):
                telegram_binding.bind_chat(session, chat, "A" * 22)

            assert chat not in "".join(traceback.format_exception(raised.value))
            assert str(raised.value) == "OperationalError in bind_chat, sqlstate 55P03"
            assert logs == [
                {
                    "event": "telegram_binding_storage_failed",
                    "log_level": "error",
                    "where": "bind_chat",
                    "error_type": "OperationalError",
                    "sqlstate": "55P03",
                }
            ]
            assert session.execute(text("SELECT 1")).scalar_one() == 1
        holder.rollback()


def test_one_code_binds_one_chat_when_many_arrive_at_once(engine, account):
    with _session(engine) as session:
        code = telegram_binding.issue_code(session, account).code
    chats = [f"7001{n:02d}" for n in range(8)]
    barrier = threading.Barrier(len(chats))
    outcomes: dict[str, object] = {}

    def redeem(chat: str) -> None:
        barrier.wait()
        try:
            with _session(engine) as session:
                outcomes[chat] = telegram_binding.bind_chat(session, chat, code)
        except Exception as exc:  # noqa: BLE001 — сбой запроса тоже результат
            outcomes[chat] = exc

    threads = [threading.Thread(target=redeem, args=(chat,)) for chat in chats]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    winners = [chat for chat, outcome in outcomes.items() if outcome is BindOutcome.BOUND]
    assert len(winners) == 1, outcomes
    assert [outcomes[chat] for chat in chats if chat not in winners] == [BindOutcome.REFUSED] * 7
    assert _chat_of(engine, account) == winners[0]


def test_two_requests_for_a_code_at_once_leave_one_code(engine, account):
    callers = 8
    barrier = threading.Barrier(callers)
    issued: list[object] = []

    def ask() -> None:
        barrier.wait()
        try:
            with _session(engine) as session:
                issued.append(telegram_binding.issue_code(session, account).code)
        except Exception as exc:  # noqa: BLE001 — сбой запроса тоже результат
            issued.append(exc)

    threads = [threading.Thread(target=ask) for _ in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(isinstance(code, str) for code in issued), issued
    assert len(set(issued)) == callers
    with _session(engine) as session:
        assert len(session.execute(select(TelegramBindCode.id)).all()) == 1
    # Действует ровно один из выданных — последний записанный.
    bound = []
    for number, code in enumerate(issued):
        with _session(engine) as session:
            if telegram_binding.bind_chat(session, f"7002{number:02d}", code) is BindOutcome.BOUND:
                bound.append(number)
    assert len(bound) == 1
    assert _chat_of(engine, account) == f"7002{bound[0]:02d}"
