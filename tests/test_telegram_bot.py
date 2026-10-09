"""Тесты Telegram-бота: маршрутизация команд, рендер сводок и журнал бота.

Журнал — в конце файла: обработчики бота не пишут в него ничего из того, что
человек написал боту, и ничего из текста ошибки. Почему — docs/RUNBOOK.md
«Адреса в журнал не пишутся».
"""

import logging
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src._time import utcnow

from src import logging_setup, notifications, notifier, roi, storage, telegram_bot, tenants
from src.storage import AlertEvent, Match, PriceSnapshot, Product, Run


def _add_run(s, started_at=None, products_scraped=10):
    started_at = started_at or utcnow()
    r = Run(
        started_at=started_at,
        finished_at=started_at,
        status="ok",
        products_scraped=products_scraped,
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {
                "pharmonline": {"status": "ok"},
                "aptekonline": {"status": "ok"},
                "aloe": {"status": "ok"},
            },
        },
    )
    s.add(r)
    s.flush()
    return r


def test_cmd_start_includes_chat_id(db_session):
    out = telegram_bot.cmd_start(db_session, "12345", "")
    assert "12345" in out
    assert "/help" in out


def test_cmd_help_lists_commands(db_session):
    out = telegram_bot.cmd_help(db_session, "12345", "")
    for cmd in ("/start", "/today", "/alerts", "/status"):
        assert cmd in out


def test_cmd_today_no_data(db_session):
    out = telegram_bot.cmd_today(db_session, "12345", "")
    assert "временно недоступны" in out.lower()


def test_cmd_today_with_data(db_session):
    """Если есть Match с конкурентом дешевле клиента — должно быть в топ-3."""
    m = Match(canonical_name="Foo", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    p_client = Product(
        site="pharmonline",
        external_id="ph",
        url="x",
        name="Foo",
        name_normalized="foo",
        canonical_id=m.id,
    )
    p_comp = Product(
        site="aloe",
        external_id="al",
        url="x",
        name="Foo",
        name_normalized="foo",
        canonical_id=m.id,
    )
    db_session.add_all([p_client, p_comp])
    db_session.flush()
    run = _add_run(db_session)
    db_session.add_all(
        [
            PriceSnapshot(run_id=run.id, product_id=p_client.id, price=10.0),
            PriceSnapshot(run_id=run.id, product_id=p_comp.id, price=7.0),
        ]
    )
    db_session.commit()
    actions = roi.compute_actions(db_session, client_site="pharmonline")
    roi.cache_actions(
        db_session,
        "pharmonline",
        actions,
        run_id=run.id,
    )

    out = telegram_bot.cmd_today(db_session, "12345", "")
    assert "Foo" in out


def test_cmd_today_does_not_compute_from_unfinished_run(db_session):
    run = _add_run(db_session)
    run.finished_at = None
    db_session.commit()

    out = telegram_bot.cmd_today(db_session, "12345", "")

    assert "временно недоступны" in out.lower()


def test_cmd_today_hides_old_cache_after_degraded_full_attempt(db_session):
    run = _add_run(db_session, started_at=utcnow() - timedelta(hours=1))
    roi.cache_actions(db_session, "pharmonline", [], run_id=run.id)
    degraded_at = utcnow()
    db_session.add(
        Run(
            started_at=degraded_at,
            finished_at=degraded_at,
            status="degraded",
            catalog_scope="full",
            full_catalog_sites="pharmonline,aptekonline,aloe",
            catalog_verified=False,
            run_quality={
                "baseline_enforced": True,
                "full_catalog_verified": False,
                "financially_eligible": False,
                "sites": {
                    "pharmonline": {"status": "degraded"},
                    "aptekonline": {"status": "ok"},
                    "aloe": {"status": "ok"},
                },
            },
        )
    )
    db_session.commit()

    out = telegram_bot.cmd_today(db_session, "12345", "")

    assert "временно недоступны" in out.lower()


def test_cmd_alerts_empty(db_session):
    out = telegram_bot.cmd_alerts(db_session, "12345", "")
    assert "не было" in out


def test_cmd_alerts_with_events(db_session):
    db_session.add(
        AlertEvent(
            rule_type="undercut_threshold",
            dedup_key="test",
            severity="warning",
            title="Test alert title",
            detail="x",
        )
    )
    db_session.commit()
    out = telegram_bot.cmd_alerts(db_session, "12345", "")
    assert "Test alert title" in out


def test_cmd_status(db_session):
    """Status работает даже на пустой БД (warning: no_runs)."""
    out = telegram_bot.cmd_status(db_session, "12345", "")
    assert "Status" in out


def test_handle_update_routes_to_command(db_session):
    """handle_update должен вызвать правильный handler по команде из update.message.text."""
    sent_messages: list[tuple] = []

    # Monkey-patch send_telegram_message
    from src import notifier

    original = notifier.send_telegram_message
    notifier.send_telegram_message = lambda chat_id, text, **kw: (
        sent_messages.append((chat_id, text)) or True
    )

    try:
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": 99},
                "text": "/help",
            },
        }
        telegram_bot.handle_update(db_session, update)
        assert len(sent_messages) == 1
        chat, text = sent_messages[0]
        assert chat == "99"
        assert "/today" in text
    finally:
        notifier.send_telegram_message = original


def test_handle_update_unknown_command(db_session):
    sent: list = []
    from src import notifier

    original = notifier.send_telegram_message
    notifier.send_telegram_message = lambda c, t, **kw: sent.append((c, t)) or True
    try:
        update = {
            "update_id": 2,
            "message": {"chat": {"id": 1}, "text": "/foo"},
        }
        telegram_bot.handle_update(db_session, update)
        assert sent
        assert "Неизвестная команда" in sent[0][1]
    finally:
        notifier.send_telegram_message = original


def test_handle_update_ignores_non_command(db_session):
    sent: list = []
    from src import notifier

    original = notifier.send_telegram_message
    notifier.send_telegram_message = lambda c, t, **kw: sent.append((c, t)) or True
    try:
        update = {
            "update_id": 3,
            "message": {"chat": {"id": 1}, "text": "просто текст"},
        }
        telegram_bot.handle_update(db_session, update)
        assert sent == []  # ничего не отправлено
    finally:
        notifier.send_telegram_message = original


# ─── Журнал бота: ничего из сообщения и ничего из текста ошибки ──────────────
#
# Бот вызывается как на сервере — `run_polling` с настоящей сессией. Проверка
# смотрит на то, что вышло наружу (журнал structlog, печать, stdlib `logging`),
# а не на то, как написан обработчик. Отправитель (`notifier`) здесь подменён:
# что о своём сбое пишет он сам, эти тесты не видят.

ADDRESS = "viewer@client.example"
CHAT_ID = "700200"
SENDER = "aysel_from_client"
SENDER_NAME = "Айсель"


def _incoming(message_text: str) -> dict:
    """Сообщение боту — как его отдаёт getUpdates."""
    return {
        "update_id": 41,
        "message": {
            "chat": {"id": int(CHAT_ID)},
            "from": {"username": SENDER, "first_name": SENDER_NAME},
            "text": message_text,
        },
    }


@pytest.fixture
def bot_user(db_session, monkeypatch):
    """Бот на базе теста и пользователь, к которому `/start <адрес>` привязывает чат."""
    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *args, **kwargs: Session)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "000:test")
    tenant = tenants.get_or_create_default(db_session)
    row = storage.TenantUser(
        tenant_id=tenant.id, email=ADDRESS, role="admin", is_active=True, created_at=utcnow()
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def replies(monkeypatch):
    """Ответы бота человеку: в сеть не уходят, складываются сюда."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        notifier,
        "send_telegram_message",
        lambda chat_id, text, **kwargs: sent.append((str(chat_id), text)) or True,
    )
    return sent


def _poll(monkeypatch, capsys, caplog, polls: list) -> tuple[list[dict], str]:
    """`run_polling` до остановки: журнал structlog и всё остальное, что бот
    вывел, — печать и stdlib `logging`.

    `polls` — чем getUpdates отвечает на каждый опрос: пачка сообщений или
    ошибка. Дальше — Ctrl-C: так бот и останавливают.
    """
    answers = iter(polls)

    def get_updates(offset=None, timeout=0):
        answer = next(answers, KeyboardInterrupt())
        if isinstance(answer, BaseException):
            raise answer
        return answer

    handled = []
    handle_update = telegram_bot.handle_update

    def handle(session, update):
        handled.append(update)
        return handle_update(session, update)

    monkeypatch.setattr(notifier, "telegram_get_updates", get_updates)
    monkeypatch.setattr(telegram_bot, "handle_update", handle)
    monkeypatch.setattr(telegram_bot, "time", SimpleNamespace(sleep=lambda seconds: None))
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    capsys.readouterr()
    with capture_logs() as logs, pytest.raises(KeyboardInterrupt):
        telegram_bot.run_polling(poll_timeout=0)
    printed = capsys.readouterr()
    # Каждое сообщение дошло до разбора: без этого «в журнале ничего нет» ни о
    # чём не говорит.
    assert handled == [update for batch in polls if isinstance(batch, list) for update in batch]
    return logs, printed.out + printed.err + caplog.text


def _assert_nothing_of_the_message(logs: list[dict], other_output: str) -> None:
    """Нигде нет того, что написал человек, и нет трассировки: под
    `capture_logs` она видна только ключом `exc_info`."""
    written = repr(logs) + other_output
    for piece in ("@", CHAT_ID, SENDER, SENDER_NAME):
        assert piece not in written, written
    assert not any("exc_info" in entry for entry in logs), logs


def _the_failure(logs: list[dict], event: str, *, error_at: str) -> dict:
    """Запись о сбое без `error_at`: место сверено с образцом, номер строки плавает."""
    [failure] = [dict(entry) for entry in logs if entry["event"] == event]
    assert re.fullmatch(error_at, failure.pop("error_at")), logs
    return failure


@pytest.mark.parametrize(
    ("message_text", "reply"),
    [
        (f"/start {ADDRESS}", "Привязано"),
        ("/start nobody@client.example", "не найден"),
        # Неизвестная команда: слово после «/» человек тоже набрал сам.
        (f"/{SENDER} {ADDRESS}", "Неизвестная команда"),
        (f"мой адрес {ADDRESS}", None),
        ("/help", "/today"),
        (f"/today {ADDRESS}", "Сегодня"),
        (f"/alerts {ADDRESS}", "Алертов"),
        (f"/status {ADDRESS}", "Status"),
    ],
)
def test_bot_writes_out_nothing_of_the_incoming_message(
    bot_user, replies, monkeypatch, capsys, caplog, message_text, reply
):
    logs, other_output = _poll(monkeypatch, capsys, caplog, [[_incoming(message_text)]])

    _assert_nothing_of_the_message(logs, other_output)
    # Человеку ответили — в его же чат.
    assert [reply in text for _, text in replies] == ([True] if reply else [])
    assert {chat for chat, _ in replies} <= {CHAT_ID}


@pytest.mark.parametrize(
    ("break_the_database", "carried", "error_type"),
    [
        # Поиск пользователя по адресу: в параметрах запроса — адрес.
        ("alter table tenant_users rename to tenant_users_gone", ADDRESS, "OperationalError"),
        # Запись привязки: в параметрах — chat_id. Он число: маска журнала,
        # последняя линия для адреса, его не вырезает.
        (
            "create trigger refuse_writes before update on tenant_users "
            "begin select raise(abort, 'read-only'); end",
            CHAT_ID,
            "IntegrityError",
        ),
    ],
    ids=["lookup", "write"],
)
def test_bot_command_failure_log_carries_no_error_text(
    db_session,
    bot_user,
    replies,
    monkeypatch,
    capsys,
    caplog,
    break_the_database,
    carried,
    error_type,
):
    """`/start <адрес>` при сбое базы: SQLAlchemy кладёт в текст ошибки параметры запроса."""
    db_session.execute(sqlalchemy.text(break_the_database))
    db_session.commit()
    # Посылка правила. Перестанет SQLAlchemy писать параметры в текст ошибки —
    # тест скажет об этом, а не пройдёт вхолостую.
    with pytest.raises(sqlalchemy.exc.SQLAlchemyError) as raised:
        notifications.bind_telegram(db_session, CHAT_ID, ADDRESS)
    db_session.rollback()
    assert "[parameters: " in str(raised.value) and carried in str(raised.value)
    assert logging_setup.mask_addresses(CHAT_ID) == CHAT_ID

    logs, other_output = _poll(monkeypatch, capsys, caplog, [[_incoming(f"/start {ADDRESS}")]])

    _assert_nothing_of_the_message(logs, other_output)
    # Диагностика остаётся: какая команда, какая ошибка и где в нашем коде.
    failure = _the_failure(
        logs, "telegram_command_failed", error_at=r"notifications\.py:\d+ in bind_telegram"
    )
    assert failure == {
        "event": "telegram_command_failed",
        "log_level": "error",
        "cmd": "/start",
        "error_type": error_type,
    }
    assert replies == [(CHAT_ID, f"❌ Ошибка: {error_type}")]


def test_bot_update_failure_log_carries_no_error_text(bot_user, monkeypatch, capsys, caplog):
    """До этого обработчика доходит то, что не поймал обработчик команды. Здесь —
    отказ отправителя с адресом в тексте, каким его пишет smtplib."""

    def refused(chat_id, text, **kwargs):
        raise RuntimeError(f"{{'{ADDRESS}': (550, b'5.1.1 <{ADDRESS}>: rejected')}}")

    monkeypatch.setattr(notifier, "send_telegram_message", refused)

    logs, other_output = _poll(monkeypatch, capsys, caplog, [[_incoming("/help")]])

    _assert_nothing_of_the_message(logs, other_output)
    failure = _the_failure(
        logs, "telegram_handle_update_failed", error_at=r"telegram_bot\.py:\d+ in handle_update"
    )
    assert failure == {
        "event": "telegram_handle_update_failed",
        "log_level": "error",
        "error_type": "RuntimeError",
    }


def test_bot_poll_failure_log_carries_no_error_text(bot_user, monkeypatch, capsys, caplog):
    """Сегодня ветка недостижима: `notifier.telegram_get_updates` ловит свой сбой
    сама. Обработчик пишет так же, как два других, — на случай, если перестанет."""
    refusal = RuntimeError(f"getUpdates: 5.1.1 <{ADDRESS}>: rejected")

    logs, other_output = _poll(monkeypatch, capsys, caplog, [refusal])

    _assert_nothing_of_the_message(logs, other_output)
    failure = _the_failure(
        logs, "telegram_poll_error", error_at=r"telegram_bot\.py:\d+ in run_polling"
    )
    assert failure == {
        "event": "telegram_poll_error",
        "log_level": "warning",
        "error_type": "RuntimeError",
    }
