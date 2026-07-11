"""Тесты Telegram-бота: маршрутизация команд + рендер сводок."""

from src._time import utcnow

from src import telegram_bot
from src.storage import AlertEvent, Match, PriceSnapshot, Product, Run


def _add_run(s, started_at=None, products_scraped=10):
    r = Run(
        started_at=started_at or utcnow(),
        status="ok",
        products_scraped=products_scraped,
        run_quality={"full_catalog_verified": True, "financially_eligible": True},
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
    assert "данных пока нет" in out.lower() or "запусти" in out.lower()


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

    out = telegram_bot.cmd_today(db_session, "12345", "")
    assert "Foo" in out


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
