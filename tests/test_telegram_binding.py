"""Чат к аккаунту привязывает тот, кто вошёл в аккаунт, а не тот, кто знает адрес.

До 2026-10-09 бот принимал `/start <адрес>` и записывал чат любому активному
пользователю с таким адресом. Теперь код привязки выдаёт дашборд вошедшему
пользователю (`telegram_binding.issue_code`), а бот принимает только его
(`telegram_binding.bind_chat`). Здесь — прямые вызовы: что отвергается, что не
перезаписывается, что видит посторонний чат и что остаётся в базе и журнале.

Замки строк и одновременные запросы — `tests/test_telegram_binding_postgres.py`.
"""

from __future__ import annotations

import hashlib
import traceback
from datetime import datetime, timedelta

import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import api as api_module
from src import notifier, rate_limit, roi, storage, telegram_binding, telegram_bot, tenants
from src.storage import AlertEvent, TelegramBindAttempt, TelegramBindCode, TenantUser
from src.telegram_binding import BindOutcome

ADDRESS = "admin@client.example"
CHAT = "700200"
OTHER_CHAT = "700300"
START = datetime(2026, 10, 9, 12, 0, 0)
# По виду — код (22 знака алфавита ссылок), но никому не выдан.
UNKNOWN = "A" * 22
PLANTED = "P" * 22


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, span: timedelta) -> None:
        self.now += span


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(telegram_binding, "utcnow", clock)
    return clock


def _user(
    session,
    email: str,
    *,
    chat: str | None = None,
    active: bool = True,
    tenant_id: int | None = None,
) -> TenantUser:
    if tenant_id is None:
        tenant_id = tenants.get_or_create_default(session).id
    row = TenantUser(
        tenant_id=tenant_id,
        email=email,
        role="admin",
        is_active=active,
        created_at=START,
        telegram_chat_id=chat,
    )
    session.add(row)
    session.commit()
    return row


def _other_tenant(session) -> int:
    tenants.get_or_create_default(session)
    other = storage.Tenant(slug="other", name="Other", client_site="pharmonline")
    session.add(other)
    session.commit()
    assert other.id != 1
    return other.id


@pytest.fixture
def account(db_session) -> TenantUser:
    """Действующий пользователь без привязанного чата."""
    return _user(db_session, ADDRESS)


def _chat_of(session, user: TenantUser) -> str | None:
    """Что записано в базе, а не в объекте сессии."""
    return session.execute(
        select(TenantUser.telegram_chat_id).where(TenantUser.id == user.id)
    ).scalar_one()


def _pending_codes(session) -> int:
    return len(session.execute(select(TelegramBindCode.id)).all())


def _failures(session) -> dict[str, int]:
    rows = session.execute(select(TelegramBindAttempt.chat_id, TelegramBindAttempt.failures))
    return {chat: failures for chat, failures in rows}


def _plant_code(session, user: TenantUser, code: str, *, expires_at: datetime) -> None:
    """Код в базе мимо `issue_code` — для состояний, до которых выдача не доводит."""
    session.add(
        TelegramBindCode(
            user_id=user.id,
            code_hash=hashlib.sha256(code.encode()).hexdigest(),
            expires_at=expires_at,
            created_at=START,
        )
    )
    session.commit()


def _bot_session(db_session):
    """Новая сессия на каждое сообщение — как у бота (`run_polling`)."""
    return sessionmaker(db_session.get_bind(), expire_on_commit=False, autoflush=False)()


# ─── Код: один раз, недолго, хешем ───────────────────────────────────────────


def test_the_limits_stay_tight():
    """Тесты ниже считают от констант; сами константы держит этот."""
    assert telegram_binding.CODE_TTL <= timedelta(minutes=15)
    assert telegram_binding.MAX_FAILED_ATTEMPTS <= 10
    assert telegram_binding.ATTEMPT_WINDOW >= timedelta(minutes=10)


def test_a_code_from_the_dashboard_binds_the_chat(db_session, account, clock):
    issued = telegram_binding.issue_code(db_session, account.id)

    with _bot_session(db_session) as bot:
        assert telegram_binding.bind_chat(bot, CHAT, issued.code) is BindOutcome.BOUND

    assert _chat_of(db_session, account) == CHAT
    assert telegram_binding.user_for_chat(db_session, CHAT).id == account.id
    assert _pending_codes(db_session) == 0


def test_the_database_holds_the_hash_and_never_the_code(db_session, account, clock):
    issued = telegram_binding.issue_code(db_session, account.id)

    [stored] = db_session.execute(sqlalchemy.text("select * from telegram_bind_codes")).all()

    assert issued.code not in repr(tuple(stored))
    assert hashlib.sha256(issued.code.encode()).hexdigest() in tuple(stored)
    assert issued.expires_at == START + telegram_binding.CODE_TTL
    assert telegram_binding._CODE_SHAPE.fullmatch(issued.code)  # 128 случайных бит, не шесть цифр


def test_neither_the_code_nor_an_address_reaches_a_query(db_session, account, clock):
    """Текст ошибки базы несёт параметры запроса, а он попадает в журнал.

    Поэтому в параметрах нет ни кода, ни адреса, который человек написал боту.
    Код идёт в запрос хешем; адрес — и хешем не идёт (хеш адреса подбирается
    по словарю): что не похоже на код, таблицу кодов не трогает вовсе.
    """
    seen: list[tuple[str, str]] = []
    engine = db_session.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append((statement, repr(parameters)))

    sqlalchemy.event.listen(engine, "before_cursor_execute", record)
    try:
        issued = telegram_binding.issue_code(db_session, account.id)
        before_the_address = len(seen)
        telegram_bot.cmd_start(db_session, OTHER_CHAT, ADDRESS)
        for_the_address = seen[before_the_address:]
        telegram_bot.cmd_start(db_session, CHAT, issued.code)
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", record)

    assert _chat_of(db_session, account) == CHAT
    parameters = "\n".join(values for _, values in seen)
    assert issued.code not in parameters
    assert "@" not in parameters
    assert hashlib.sha256(ADDRESS.encode()).hexdigest() not in parameters
    assert for_the_address, "попытка с адресом должна была записать неудачу"
    assert not [statement for statement, _ in for_the_address if "telegram_bind_codes" in statement]


def test_an_unknown_code_is_refused(db_session, account, clock):
    telegram_binding.issue_code(db_session, account.id)

    assert telegram_binding.bind_chat(db_session, CHAT, UNKNOWN) is BindOutcome.REFUSED

    assert _chat_of(db_session, account) is None
    assert _pending_codes(db_session) == 1  # чужая попытка код не гасит


@pytest.mark.parametrize(
    "code",
    ["", "   ", "x" * 65, "two words", ADDRESS, "A" * 21, "A" * 23, "A" * 21 + "!"],
)
def test_what_is_not_a_code_is_refused(db_session, account, clock, code):
    telegram_binding.issue_code(db_session, account.id)

    assert telegram_binding.bind_chat(db_session, CHAT, code) is BindOutcome.REFUSED
    assert _chat_of(db_session, account) is None
    assert _failures(db_session) == {CHAT: 1}


def test_a_used_code_does_not_work_again(db_session, account, clock):
    issued = telegram_binding.issue_code(db_session, account.id)
    assert telegram_binding.bind_chat(db_session, CHAT, issued.code) is BindOutcome.BOUND

    # Из другого чата — привязка остаётся прежней.
    assert telegram_binding.bind_chat(db_session, OTHER_CHAT, issued.code) is BindOutcome.REFUSED
    assert _chat_of(db_session, account) == CHAT

    # И после «Отвязать»: код был одноразовым, а не «пока аккаунт занят».
    db_session.execute(sqlalchemy.update(TenantUser).values(telegram_chat_id=None))
    db_session.commit()
    assert telegram_binding.bind_chat(db_session, OTHER_CHAT, issued.code) is BindOutcome.REFUSED
    assert _chat_of(db_session, account) is None


@pytest.mark.parametrize(
    ("age", "outcome", "bound_to"),
    [
        (telegram_binding.CODE_TTL - timedelta(seconds=1), BindOutcome.BOUND, CHAT),
        (telegram_binding.CODE_TTL, BindOutcome.REFUSED, None),
        (timedelta(days=30), BindOutcome.REFUSED, None),
    ],
    ids=["second-before", "at-expiry", "long-after"],
)
def test_an_expired_code_is_refused(db_session, account, clock, age, outcome, bound_to):
    issued = telegram_binding.issue_code(db_session, account.id)
    clock.advance(age)

    assert telegram_binding.bind_chat(db_session, CHAT, issued.code) is outcome

    assert _chat_of(db_session, account) == bound_to
    assert _pending_codes(db_session) == 0  # истёкший код стёрт, удачный погашен


def test_a_new_code_replaces_the_previous_one(db_session, account, clock):
    first = telegram_binding.issue_code(db_session, account.id)
    second = telegram_binding.issue_code(db_session, account.id)

    assert first.code != second.code
    assert _pending_codes(db_session) == 1
    assert telegram_binding.bind_chat(db_session, OTHER_CHAT, first.code) is BindOutcome.REFUSED
    assert _chat_of(db_session, account) is None
    assert telegram_binding.bind_chat(db_session, CHAT, second.code) is BindOutcome.BOUND


def test_a_new_code_does_not_touch_the_code_of_another_user(db_session, account, clock):
    neighbour = _user(db_session, "viewer@client.example")
    theirs = telegram_binding.issue_code(db_session, neighbour.id)

    telegram_binding.issue_code(db_session, account.id)

    assert telegram_binding.bind_chat(db_session, OTHER_CHAT, theirs.code) is BindOutcome.BOUND
    assert _chat_of(db_session, neighbour) == OTHER_CHAT


def test_a_code_of_a_deactivated_user_is_refused(db_session, account, clock):
    issued = telegram_binding.issue_code(db_session, account.id)
    account.is_active = False
    db_session.commit()

    assert telegram_binding.bind_chat(db_session, CHAT, issued.code) is BindOutcome.REFUSED
    assert _chat_of(db_session, account) is None


def test_no_code_for_a_user_who_is_not_there(db_session, clock):
    gone = _user(db_session, "gone@client.example", active=False)

    with pytest.raises(LookupError):
        telegram_binding.issue_code(db_session, gone.id)
    with pytest.raises(LookupError):
        telegram_binding.issue_code(db_session, gone.id + 1000)
    assert _pending_codes(db_session) == 0


# ─── Существующая привязка не перезаписывается ───────────────────────────────


def test_a_bound_account_gets_no_code(db_session, clock):
    bound = _user(db_session, ADDRESS, chat=CHAT)

    with pytest.raises(telegram_binding.AlreadyBound):
        telegram_binding.issue_code(db_session, bound.id)

    assert _pending_codes(db_session) == 0


def test_a_bound_account_is_not_overwritten_even_by_a_valid_code(db_session, clock):
    """Выдача такого кода не допускает; если он всё же есть — привязка стоит."""
    bound = _user(db_session, ADDRESS, chat=CHAT)
    _plant_code(db_session, bound, PLANTED, expires_at=START + timedelta(minutes=5))

    assert telegram_binding.bind_chat(db_session, OTHER_CHAT, PLANTED) is BindOutcome.REFUSED

    assert _chat_of(db_session, bound) == CHAT
    assert telegram_binding.user_for_chat(db_session, OTHER_CHAT) is None


@pytest.mark.parametrize("same_tenant", [True, False], ids=["same-tenant", "another-tenant"])
def test_a_chat_of_another_account_is_not_taken(db_session, account, clock, same_tenant):
    tenant_id = account.tenant_id if same_tenant else _other_tenant(db_session)
    neighbour = _user(db_session, "viewer@client.example", chat=CHAT, tenant_id=tenant_id)
    issued = telegram_binding.issue_code(db_session, account.id)

    assert telegram_binding.bind_chat(db_session, CHAT, issued.code) is BindOutcome.REFUSED

    assert _chat_of(db_session, account) is None
    assert _chat_of(db_session, neighbour) == CHAT
    # Код цел: тот же человек привяжет им другой чат.
    assert telegram_binding.bind_chat(db_session, OTHER_CHAT, issued.code) is BindOutcome.BOUND


def test_binding_takes_the_chat_off_a_deactivated_account(db_session, account, clock):
    left = _user(db_session, "left@client.example", chat=CHAT, active=False)
    issued = telegram_binding.issue_code(db_session, account.id)

    assert telegram_binding.bind_chat(db_session, CHAT, issued.code) is BindOutcome.BOUND

    assert _chat_of(db_session, account) == CHAT
    assert _chat_of(db_session, left) is None


def test_a_chat_written_to_two_accounts_speaks_for_neither(db_session):
    """До такого доводит только ручная правка базы — отвечать наугад нельзя."""
    _user(db_session, ADDRESS, chat=CHAT)
    _user(db_session, "viewer@client.example", chat=CHAT)

    assert telegram_binding.user_for_chat(db_session, CHAT) is None
    assert telegram_bot.cmd_alerts(db_session, CHAT, "") == telegram_bot.NOT_BOUND_REPLY


# ─── Число попыток ───────────────────────────────────────────────────────────


def test_a_chat_is_locked_out_after_too_many_failures(db_session, account, clock):
    issued = telegram_binding.issue_code(db_session, account.id)
    for attempt in range(telegram_binding.MAX_FAILED_ATTEMPTS):
        clock.advance(timedelta(seconds=1))
        assert telegram_binding.bind_chat(db_session, CHAT, f"guess-{attempt}") is (
            BindOutcome.REFUSED
        )

    # Верный код в блокировке не проверяется — и не гаснет.
    clock.advance(timedelta(seconds=1))
    assert telegram_binding.bind_chat(db_session, CHAT, issued.code) is BindOutcome.LOCKED
    assert _chat_of(db_session, account) is None
    assert _pending_codes(db_session) == 1
    # Попытки в блокировке её не продлевают.
    assert _failures(db_session) == {CHAT: telegram_binding.MAX_FAILED_ATTEMPTS}

    # Другой чат считается отдельно.
    assert telegram_binding.bind_chat(db_session, OTHER_CHAT, UNKNOWN) is BindOutcome.REFUSED


def test_failures_are_kept_between_the_sessions_of_the_bot(db_session, account, clock):
    """Бот открывает сессию на каждое сообщение: счёт живёт в базе, не в ней."""
    issued = telegram_binding.issue_code(db_session, account.id)
    for _ in range(telegram_binding.MAX_FAILED_ATTEMPTS):
        with _bot_session(db_session) as bot:
            assert telegram_binding.bind_chat(bot, CHAT, UNKNOWN) is BindOutcome.REFUSED

    with _bot_session(db_session) as bot:
        assert telegram_binding.bind_chat(bot, CHAT, issued.code) is BindOutcome.LOCKED
    assert _chat_of(db_session, account) is None


def test_the_lockout_ends_with_its_window(db_session, account, clock):
    for attempt in range(telegram_binding.MAX_FAILED_ATTEMPTS):
        telegram_binding.bind_chat(db_session, CHAT, f"guess-{attempt}")
    clock.advance(telegram_binding.ATTEMPT_WINDOW - timedelta(seconds=1))
    assert telegram_binding.bind_chat(db_session, CHAT, UNKNOWN) is BindOutcome.LOCKED

    clock.advance(timedelta(seconds=1))
    issued = telegram_binding.issue_code(db_session, account.id)
    assert telegram_binding.bind_chat(db_session, CHAT, issued.code) is BindOutcome.BOUND


def test_one_failure_short_of_the_limit_still_binds(db_session, account, clock):
    issued = telegram_binding.issue_code(db_session, account.id)
    for attempt in range(telegram_binding.MAX_FAILED_ATTEMPTS - 1):
        telegram_binding.bind_chat(db_session, CHAT, f"guess-{attempt}")

    assert telegram_binding.bind_chat(db_session, CHAT, issued.code) is BindOutcome.BOUND
    # Удача счёт стирает.
    assert _failures(db_session) == {}


def test_failures_of_a_past_window_are_forgotten(db_session, account, clock):
    telegram_binding.bind_chat(db_session, CHAT, UNKNOWN)
    telegram_binding.bind_chat(db_session, OTHER_CHAT, UNKNOWN)
    telegram_binding.bind_chat(db_session, OTHER_CHAT, UNKNOWN)
    assert _failures(db_session) == {CHAT: 1, OTHER_CHAT: 2}

    clock.advance(telegram_binding.ATTEMPT_WINDOW)
    telegram_binding.bind_chat(db_session, CHAT, UNKNOWN)

    # Строка другого чата стёрта, счёт этого начался заново.
    assert _failures(db_session) == {CHAT: 1}


# ─── Что отвечает бот ────────────────────────────────────────────────────────


def test_start_with_an_address_answers_the_same_whoever_exists(db_session, account, clock):
    """Адрес бот не ищет: ответ один, есть такой пользователь или нет."""
    for_existing = telegram_bot.cmd_start(db_session, CHAT, ADDRESS)
    for_missing = telegram_bot.cmd_start(db_session, OTHER_CHAT, "nobody@client.example")

    assert for_existing == for_missing == telegram_bot.BIND_REFUSED_REPLY
    assert "@" not in for_existing
    assert _chat_of(db_session, account) is None
    assert _failures(db_session) == {CHAT: 1, OTHER_CHAT: 1}


def test_start_without_a_code_binds_nothing(db_session, account, clock):
    reply = telegram_bot.cmd_start(db_session, CHAT, "")

    assert "/start <код>" in reply
    assert _chat_of(db_session, account) is None
    assert _failures(db_session) == {}  # приветствие — не попытка


def test_every_refusal_reads_the_same(db_session, account, clock):
    """Нет кода, не код, истёк, использован, чат занят, аккаунт занят — один текст."""
    replies = {}
    replies["unknown"] = telegram_bot.cmd_start(db_session, "1", UNKNOWN)
    replies["not a code"] = telegram_bot.cmd_start(db_session, "6", "no such code")

    used = telegram_binding.issue_code(db_session, account.id)
    assert telegram_bot.cmd_start(db_session, "2", used.code) == telegram_bot.BIND_OK_REPLY
    replies["used"] = telegram_bot.cmd_start(db_session, "3", used.code)

    second = _user(db_session, "second@client.example")
    expired = telegram_binding.issue_code(db_session, second.id)
    clock.advance(telegram_binding.CODE_TTL)
    replies["expired"] = telegram_bot.cmd_start(db_session, "4", expired.code)

    fresh = telegram_binding.issue_code(db_session, second.id)
    replies["chat taken"] = telegram_bot.cmd_start(db_session, "2", fresh.code)

    _plant_code(db_session, account, PLANTED, expires_at=clock.now + timedelta(minutes=5))
    replies["account taken"] = telegram_bot.cmd_start(db_session, "5", PLANTED)

    assert set(replies.values()) == {telegram_bot.BIND_REFUSED_REPLY}, replies
    assert _chat_of(db_session, account) == "2"
    assert _chat_of(db_session, second) is None


def test_a_locked_out_chat_is_told_to_wait(db_session, account, clock):
    for attempt in range(telegram_binding.MAX_FAILED_ATTEMPTS):
        telegram_bot.cmd_start(db_session, CHAT, f"guess-{attempt}")
    issued = telegram_binding.issue_code(db_session, account.id)

    reply = telegram_bot.cmd_start(db_session, CHAT, issued.code)

    assert "до 15 минут" in reply and reply != telegram_bot.BIND_REFUSED_REPLY
    assert _chat_of(db_session, account) is None


# ─── Команды с данными — только привязанному чату ────────────────────────────

# Отвечают и непривязанному чату. `/stop` действует только на свой чат.
OPEN_COMMANDS = {"/start", "/help", "/stop"}
DATA_COMMANDS = sorted(set(telegram_bot.COMMANDS) - OPEN_COMMANDS)


def _alert(session, title: str, *, tenant_id: int = 1) -> None:
    session.add(
        AlertEvent(
            rule_type="price_drop_pct",
            dedup_key=title,
            severity="critical",
            title=title,
            detail="x",
            tenant_id=tenant_id,
        )
    )
    session.commit()


def test_the_bot_has_no_other_open_commands():
    """Новая команда падает сюда: решить, отвечает ли она непривязанному чату."""
    assert OPEN_COMMANDS < set(telegram_bot.COMMANDS)
    assert DATA_COMMANDS == ["/alerts", "/status", "/today"]


@pytest.mark.parametrize("command", DATA_COMMANDS)
def test_a_data_command_refuses_a_chat_that_is_not_bound(db_session, account, command):
    _alert(db_session, "Цена упала на 25%: Noreva")

    reply = telegram_bot.COMMANDS[command](db_session, CHAT, "")

    assert reply == telegram_bot.NOT_BOUND_REPLY


@pytest.mark.parametrize("command", DATA_COMMANDS)
def test_a_data_command_refuses_the_chat_of_a_deactivated_user(db_session, command):
    _user(db_session, ADDRESS, chat=CHAT, active=False)
    _alert(db_session, "Цена упала на 25%: Noreva")

    assert telegram_bot.COMMANDS[command](db_session, CHAT, "") == telegram_bot.NOT_BOUND_REPLY


def test_a_bound_chat_gets_its_answer(db_session):
    _user(db_session, ADDRESS, chat=CHAT)
    _alert(db_session, "Цена упала на 25%: Noreva")

    assert "Noreva" in telegram_bot.COMMANDS["/alerts"](db_session, CHAT, "")
    assert "Status" in telegram_bot.COMMANDS["/status"](db_session, CHAT, "")


def test_alerts_are_those_of_the_user_tenant(db_session):
    _user(db_session, ADDRESS, chat=CHAT)
    _alert(db_session, "Свой алерт")
    _alert(db_session, "Алерт другого тенанта", tenant_id=2)

    reply = telegram_bot.cmd_alerts(db_session, CHAT, "")

    assert "Свой алерт" in reply
    assert "другого тенанта" not in reply


def test_today_reads_the_recommendations_of_the_user_tenant(db_session, monkeypatch):
    other = _other_tenant(db_session)
    _user(db_session, ADDRESS, chat=CHAT, tenant_id=other)
    asked: list[int] = []

    def cached(session, client_site, *, tenant_id):
        asked.append(tenant_id)
        return None

    monkeypatch.setattr(roi, "get_cached_action_items", cached)

    telegram_bot.cmd_today(db_session, CHAT, "")

    assert asked == [other]


# ─── /stop: чат отвязывает себя сам ──────────────────────────────────────────


def test_stop_unbinds_the_calling_chat_and_no_other(db_session, clock):
    mine = _user(db_session, ADDRESS, chat=CHAT)
    neighbour = _user(db_session, "viewer@client.example", chat=OTHER_CHAT)

    with _bot_session(db_session) as bot:
        assert telegram_bot.cmd_stop(bot, CHAT, "") == telegram_bot.UNBOUND_REPLY

    assert _chat_of(db_session, mine) is None
    assert _chat_of(db_session, neighbour) == OTHER_CHAT
    # Чужой чат в команде не назвать: она видит только тот, из которого пришла.
    assert telegram_bot.cmd_stop(db_session, CHAT, OTHER_CHAT) == telegram_bot.NOT_BOUND_REPLY
    assert telegram_bot.cmd_stop(db_session, "999", OTHER_CHAT) == telegram_bot.NOT_BOUND_REPLY
    assert _chat_of(db_session, neighbour) == OTHER_CHAT


def test_stop_frees_a_chat_left_on_a_deactivated_account(db_session, clock):
    left = _user(db_session, "left@client.example", chat=CHAT, active=False)

    assert telegram_bot.cmd_stop(db_session, CHAT, "") == telegram_bot.UNBOUND_REPLY
    assert _chat_of(db_session, left) is None


def test_a_chat_bound_with_somebody_elses_code_frees_itself(db_session, account, clock):
    """Чужая ссылка `t.me/<бот>?start=<код>`: чат привязан не к своему аккаунту.

    Свой код чат после этого не примет (чат занят), а «Отвязать» в дашборде
    есть только у хозяина чужого аккаунта.
    """
    recipient = _user(db_session, "viewer@client.example")
    theirs = telegram_binding.issue_code(db_session, account.id)
    assert telegram_binding.bind_chat(db_session, CHAT, theirs.code) is BindOutcome.BOUND
    own = telegram_binding.issue_code(db_session, recipient.id)
    assert telegram_binding.bind_chat(db_session, CHAT, own.code) is BindOutcome.REFUSED

    assert telegram_bot.cmd_stop(db_session, CHAT, "") == telegram_bot.UNBOUND_REPLY

    assert telegram_binding.bind_chat(db_session, CHAT, own.code) is BindOutcome.BOUND
    assert _chat_of(db_session, account) is None
    assert _chat_of(db_session, recipient) == CHAT


def test_binding_and_unbinding_from_the_chat_are_audited(db_session, account, clock):
    """Привязку делает не запрос к дашборду — без этой записи её следа нет."""
    issued = telegram_binding.issue_code(db_session, account.id)
    telegram_binding.bind_chat(db_session, CHAT, UNKNOWN)
    telegram_binding.bind_chat(db_session, CHAT, issued.code)
    telegram_binding.unbind_chat(db_session, CHAT)
    telegram_binding.unbind_chat(db_session, CHAT)

    rows = db_session.execute(sqlalchemy.text("select * from audit_logs order by id")).all()

    assert [(row.action, row.resource, row.actor_user_id, row.tenant_id) for row in rows] == [
        ("BIND", "telegram-chat", account.id, account.tenant_id),
        ("UNBIND", "telegram-chat", account.id, account.tenant_id),
    ]
    for secret in (CHAT, issued.code, "@"):
        assert secret not in repr([tuple(row) for row in rows])


@pytest.fixture
def replies(monkeypatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        notifier,
        "send_telegram_message",
        lambda chat_id, text, **kwargs: sent.append((str(chat_id), text)) or True,
    )
    return sent


def _incoming(text: str, *, chat: str | None = CHAT) -> dict:
    message: dict = {"text": text, "chat": {} if chat is None else {"id": int(chat)}}
    return {"update_id": 1, "message": message}


def test_a_message_to_the_bot_binds_through_the_code(db_session, account, clock, replies):
    issued = telegram_binding.issue_code(db_session, account.id)

    for text in ("/alerts", f"/start {ADDRESS}", f"/start {issued.code}", "/alerts", "/stop"):
        with _bot_session(db_session) as bot:
            telegram_bot.handle_update(bot, _incoming(text))
    with _bot_session(db_session) as bot:
        telegram_bot.handle_update(bot, _incoming("/alerts"))

    assert [chat for chat, _ in replies] == [CHAT] * 6
    assert [text for _, text in replies] == [
        telegram_bot.NOT_BOUND_REPLY,
        telegram_bot.BIND_REFUSED_REPLY,
        telegram_bot.BIND_OK_REPLY,
        "🔔 Алертов ещё не было.",
        telegram_bot.UNBOUND_REPLY,
        telegram_bot.NOT_BOUND_REPLY,
    ]
    assert _chat_of(db_session, account) is None


def test_a_message_without_a_chat_is_not_answered(db_session, account, clock, replies):
    issued = telegram_binding.issue_code(db_session, account.id)

    telegram_bot.handle_update(db_session, _incoming(f"/start {issued.code}", chat=None))

    assert replies == []
    assert _chat_of(db_session, account) is None
    assert _pending_codes(db_session) == 1


# ─── Журнал ──────────────────────────────────────────────────────────────────


def test_the_log_names_the_user_by_id_and_nothing_else(db_session, account, clock):
    with capture_logs() as logs:
        issued = telegram_binding.issue_code(db_session, account.id)
        telegram_bot.cmd_start(db_session, OTHER_CHAT, ADDRESS)
        telegram_bot.cmd_start(db_session, CHAT, issued.code)
        for attempt in range(telegram_binding.MAX_FAILED_ATTEMPTS):
            telegram_bot.cmd_start(db_session, OTHER_CHAT, f"guess-{attempt}")
        telegram_bot.cmd_stop(db_session, CHAT, "")

    written = repr(logs)
    for secret in ("@", CHAT, OTHER_CHAT, issued.code, "guess"):
        assert secret not in written, logs
    assert [(entry["event"], entry.get("user_id")) for entry in logs] == [
        ("telegram_bind_code_issued", account.id),
        ("telegram_bind_refused", None),
        ("telegram_bound", account.id),
        *[("telegram_bind_refused", None)] * (telegram_binding.MAX_FAILED_ATTEMPTS - 1),
        ("telegram_bind_locked", None),
        ("telegram_unbound", account.id),
    ]


@pytest.mark.parametrize(
    ("broken_table", "call"),
    [
        ("telegram_bind_attempts", lambda session: telegram_binding.bind_chat),
        ("tenant_users", lambda session: telegram_binding.user_for_chat),
        ("tenant_users", lambda session: telegram_binding.unbind_chat),
    ],
    ids=["bind", "whose-chat", "unbind"],
)
def test_a_database_failure_leaves_the_chat_out_of_the_error(
    db_session, account, clock, broken_table, call
):
    """Обработчик бота пишет текст ошибки в журнал, а SQLAlchemy кладёт в него
    параметры запроса — номер чата. Наружу ошибка выходит без них."""
    issued = telegram_binding.issue_code(db_session, account.id)
    db_session.execute(sqlalchemy.text(f"alter table {broken_table} rename to gone"))
    db_session.commit()
    function = call(db_session)
    arguments = (CHAT, issued.code) if function is telegram_binding.bind_chat else (CHAT,)
    # Посылка правила: сама ошибка базы номер чата несёт. Перестанет — тест
    # скажет об этом, а не пройдёт вхолостую.
    with pytest.raises(sqlalchemy.exc.SQLAlchemyError) as raw:
        function.__wrapped__(db_session, *arguments)
    db_session.rollback()
    assert CHAT in str(raw.value)

    with pytest.raises(telegram_binding.BindingStorageError) as raised:
        function(db_session, *arguments)

    shown = "".join(traceback.format_exception(raised.value))
    for secret in (CHAT, issued.code, "parameters", "SELECT"):
        assert secret not in shown, shown
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert str(raised.value) == f"OperationalError in {function.__name__}"
    # Сессия после сбоя годна: бот отвечает человеку и берётся за следующее сообщение.
    assert db_session.execute(sqlalchemy.text("select 1")).scalar_one() == 1


def test_the_bot_answers_a_database_failure_without_its_text(db_session, account, replies):
    db_session.execute(sqlalchemy.text("alter table tenant_users rename to gone"))
    db_session.commit()

    with capture_logs() as logs:
        telegram_bot.handle_update(db_session, _incoming("/alerts"))

    assert replies == [(CHAT, "❌ Ошибка: BindingStorageError")]
    assert [entry["event"] for entry in logs] == ["telegram_command_failed"]
    assert CHAT not in repr(logs), logs


# ─── Дашборд: код выдаётся вошедшему ─────────────────────────────────────────

CODE_URL = "/api/v1/dash/me/notifications/telegram/code"


@pytest.fixture
def client(monkeypatch, db_session) -> TestClient:
    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: Session)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    # Счёт запросов — в памяти процесса и с нуля: в CI рядом стоит Redis, и
    # счётчик в нём переживал бы тест, а id пользователя в каждой базе тот же.
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: None)
    rate_limit._reset_memory_for_tests()
    return TestClient(api_module.app)


def _sign_in(client: TestClient, user: TenantUser) -> None:
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    client.cookies.set(
        api_module.COOKIE_NAME,
        api_module._make_jwt(user.id, user.tenant_id, user.email),
    )


@pytest.fixture
def signed_in(client, account) -> TenantUser:
    _sign_in(client, account)
    return account


def test_the_dashboard_issues_a_code_to_the_signed_in_user(client, signed_in, db_session):
    response = client.post(CODE_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_in_sec"] == telegram_binding.CODE_TTL.total_seconds()
    assert response.headers["cache-control"] == "no-store"
    assert telegram_binding.bind_chat(db_session, CHAT, body["code"]) is BindOutcome.BOUND
    assert _chat_of(db_session, signed_in) == CHAT


def test_the_dashboard_code_is_for_the_caller_only(client, signed_in, db_session):
    """Код получает тот, кто вошёл: чужой аккаунт в запросе не назвать."""
    neighbour = _user(db_session, "viewer@client.example")

    body = client.post(CODE_URL, json={"user_id": neighbour.id, "email": neighbour.email}).json()

    assert telegram_binding.bind_chat(db_session, CHAT, body["code"]) is BindOutcome.BOUND
    assert _chat_of(db_session, signed_in) == CHAT
    assert _chat_of(db_session, neighbour) is None


def test_the_dashboard_gives_no_code_without_sign_in(client, account, db_session):
    response = client.post(CODE_URL)

    assert response.status_code == 401
    assert _pending_codes(db_session) == 0


def test_the_dashboard_gives_no_code_when_sign_in_is_switched_off(
    client, account, db_session, monkeypatch
):
    """`PHARMACY_AUTH_DISABLED`: «вошедший» — любой, кто открыл дашборд."""
    monkeypatch.setattr(api_module, "_AUTH_DISABLED", True)

    response = client.post(CODE_URL)

    assert response.status_code == 403
    assert _pending_codes(db_session) == 0


def test_the_dashboard_gives_no_code_to_a_bound_account(client, signed_in, db_session):
    signed_in.telegram_chat_id = CHAT
    db_session.commit()

    response = client.post(CODE_URL)

    assert response.status_code == 409
    assert _pending_codes(db_session) == 0
    assert _chat_of(db_session, signed_in) == CHAT


def test_the_dashboard_limits_how_often_one_user_asks_for_a_code(client, signed_in, db_session):
    statuses = [client.post(CODE_URL).status_code for _ in range(6)]

    assert statuses == [200] * 5 + [429]
    # Счёт — на пользователя: сосед свой код получает.
    _sign_in(client, _user(db_session, "viewer@client.example"))
    assert client.post(CODE_URL).status_code == 200
