"""Знак разметки в чужой строке не съедает Telegram-сообщение и не меняет текст.

Сообщения идут в разметке Markdown (legacy). В ней `_`, `*`, `` ` `` и `[`
открывают сущность: незакрытую Telegram отвергает вместе со всем сообщением
(400 «can't parse entities»), закрытую принимает, но знаки из текста пропадают.
Названия товаров приходят с сайтов, коды проблем health пишутся через
подчёркивание, команду набирает человек.

Боевого бота у тестов нет, поэтому вместо сервера здесь его разбор:
`parse_legacy_markdown` — перенос `parse_markdown` из открытого исходника
сервера (tdlib, `td/telegram/MessageEntity.cpp`, коммит c15d3f5a от 2026-10-08).
Через неё Bot API пропускает текст с `parse_mode=Markdown`
(`Client::get_formatted_text` → `parseTextEntities`, версия 1). Перенесено то, от
чего зависят отказ и текст на экране; перенос сверен с примерами из
документации Bot API — первый тест ниже. Чего в нём нет: сервер ставит ссылку,
только если адрес в скобках годится (здесь «link» ставится всегда), а блок кода
с названием языка здесь зовётся просто «pre». Вокруг разбора `FakeTelegram`
знает только предел длины и отказ на пустой текст. На живом Telegram это не
проверялось.

Тесты зовут отправку как есть и подменяют только `urllib.request.urlopen`:
проверяется тело запроса, которое ушло бы в Telegram.
"""

from __future__ import annotations

import io
import json
import random
import urllib.error
import urllib.parse
from email.message import Message

import pytest
from structlog.testing import capture_logs

from src import alerts, health, notifications, notifier, roi, storage, telegram_bot, tenants
from src._time import utcnow

CHAT_ID = "700100"

# ─── Разбор сервера ──────────────────────────────────────────────────────────

_MARKERS = b"_*`["
_SPACES = b" \t\r\n\x00\x0b"
_ENTITY = {0x5F: "italic", 0x2A: "bold", 0x5B: "link", 0x60: "code"}


class CantParseEntities(Exception):
    """Сервер ответил бы 400 «Bad Request: can't parse entities: …»."""


def parse_legacy_markdown(text: str) -> tuple[str, list[tuple[str, int, int]]]:
    """Что увидит человек и какие сущности выделены: (вид, начало, длина) в UTF-16."""
    src = text.encode("utf-8")
    size = len(src)

    def at(index: int) -> int:
        # У строки C++ за последним байтом стоит ноль; дальше сервер не читает.
        return src[index] if index < size else 0

    shown = bytearray()
    entities: list[tuple[str, int, int]] = []
    utf16 = 0
    i = 0
    while i < size:
        c = src[i]
        if c == 0x5C and at(i + 1) in _MARKERS:
            i += 1
            shown.append(src[i])
            utf16 += 1
            i += 1
            continue
        if c not in _MARKERS:
            if (c & 0xC0) != 0x80:
                utf16 += 1 + (c >= 0xF0)
            shown.append(c)
            i += 1
            continue

        begin = i
        end_character = 0x5D if c == 0x5B else c
        is_pre = False
        i += 1
        if c == 0x60 and at(i) == 0x60 and at(i + 1) == 0x60:
            i += 2
            is_pre = True
            language_end = i
            while at(language_end) not in _SPACES and at(language_end) != 0x60:
                language_end += 1
            if i != language_end and language_end < size and at(language_end) != 0x60:
                i = language_end
            if at(i) in (0x0A, 0x0D):
                i += 2 if at(i + 1) in (0x0A, 0x0D) and at(i) != at(i + 1) else 1

        entity_offset = utf16
        while i < size and (
            src[i] != end_character or (is_pre and not (at(i + 1) == 0x60 and at(i + 2) == 0x60))
        ):
            if (src[i] & 0xC0) != 0x80:
                utf16 += 1 + (src[i] >= 0xF0)
            shown.append(src[i])
            i += 1
        if i == size:
            raise CantParseEntities(f"Can't find end of the entity starting at byte offset {begin}")

        if entity_offset != utf16:
            if c == 0x5B and at(i + 1) == 0x28:
                i += 2
                while i < size and src[i] != 0x29:
                    i += 1
            kind = "pre" if is_pre else _ENTITY[c]
            entities.append((kind, entity_offset, utf16 - entity_offset))
        if is_pre:
            i += 2
        i += 1
    return shown.decode("utf-8"), entities


@pytest.mark.parametrize(
    ("markup", "shown", "entities"),
    [
        ("*bold text*", "bold text", [("bold", 0, 9)]),
        ("_italic text_", "italic text", [("italic", 0, 11)]),
        ("`inline fixed-width code`", "inline fixed-width code", [("code", 0, 23)]),
        ("[inline URL](http://www.example.com/)", "inline URL", [("link", 0, 10)]),
        ("```\npre-formatted\n```", "pre-formatted\n", [("pre", 0, 14)]),
        # Два примера экранирования из раздела «Markdown style»:
        ("_snake_\\__case_", "snake_case", [("italic", 0, 5), ("italic", 6, 4)]),
        ("*2*\\**2=4*", "2*2=4", [("bold", 0, 1), ("bold", 2, 3)]),
    ],
)
def test_the_ported_parser_reads_the_bot_api_documentation_examples(markup, shown, entities):
    assert parse_legacy_markdown(markup) == (shown, entities)


@pytest.mark.parametrize(
    "markup",
    [
        "  • _stale_run_: давно не было прогонов",
        "🔴 `09.10 12:00` Diş fırçası Colgate 360*C №1",
        'Uşaq yağı "Johnson`s"  200 ml',
        "Krem [50 ml",
    ],
)
def test_the_server_refuses_a_message_with_an_unclosed_entity(markup):
    """Гипотеза, с которой началась правка: так сообщения собирались раньше."""
    with pytest.raises(CantParseEntities, match="Can't find end of the entity"):
        parse_legacy_markdown(markup)


def test_the_server_accepts_closed_entities_and_drops_their_markers():
    """Не отказ, а тихая порча: название приходит не таким, как на сайте."""
    assert parse_legacy_markdown("HYAL [PRE] KURSOR serum")[0] == "HYAL PRE KURSOR serum"
    assert parse_legacy_markdown("  • _full_catalog_overdue_: x")[0] == "  • fullcatalogoverdue: x"


# ─── Сервер вместо Telegram ──────────────────────────────────────────────────


class _Response:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _refusal(url: str, status: int, description: str) -> urllib.error.HTTPError:
    """Отказ Bot API: статус HTTP равен `error_code`, причина — в `description`."""
    body = json.dumps({"ok": False, "error_code": status, "description": description})
    return urllib.error.HTTPError(url, status, "", Message(), io.BytesIO(body.encode("utf-8")))


# «Text of the message to be sent, 1-4096 characters after entities parsing».
MESSAGE_LIMIT = 4096


class FakeTelegram:
    """`sendMessage`: текст с разметкой разбирается так же, как на сервере."""

    def __init__(self, *, always: tuple[int, str] | None = None):
        self.requests: list[dict[str, str]] = []
        self.shown: list[str] = []
        self.entities: list[list[tuple[str, int, int]]] = []
        self._always = always

    def __call__(self, request, timeout=None, context=None):
        form = dict(urllib.parse.parse_qsl(request.data.decode("utf-8"), keep_blank_values=True))
        self.requests.append(form)
        if self._always:
            raise _refusal(request.full_url, *self._always)
        # Как в `Client::get_formatted_text`: регистр не важен, пустое значение
        # и «none» — текст без разметки.
        mode = form.get("parse_mode", "").lower()
        if mode in ("", "none"):
            shown, entities = form["text"], []
        elif mode == "markdown":
            try:
                shown, entities = parse_legacy_markdown(form["text"])
            except CantParseEntities as exc:
                reason = f"Bad Request: can't parse entities: {exc}"
                raise _refusal(request.full_url, 400, reason) from None
        else:
            raise _refusal(request.full_url, 400, "Bad Request: unsupported parse_mode")
        if not shown.strip():
            raise _refusal(request.full_url, 400, "Bad Request: message text is empty")
        if len(shown) > MESSAGE_LIMIT:
            raise _refusal(request.full_url, 400, "Bad Request: message is too long")
        self.shown.append(shown)
        self.entities.append(entities)
        return _Response({"ok": True, "result": {"message_id": len(self.shown)}})

    def only_message(self) -> str:
        """Текст на экране. Сообщение одно и принято с разметкой, с первого раза."""
        assert [form.get("parse_mode") for form in self.requests] == ["Markdown"]
        assert [form["chat_id"] for form in self.requests] == [CHAT_ID]
        return self.shown[0]

    def kinds(self) -> list[str]:
        """Виды выделения в единственном сообщении, по порядку."""
        return [kind for kind, _, _ in self.entities[0]]

    def marked(self, kind: str) -> int:
        """Сколько текста единственного сообщения выделено этим видом, в единицах UTF-16."""
        return sum(length for entity, _, length in self.entities[0] if entity == kind)


@pytest.fixture
def telegram(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "100200:test-token")
    fake = FakeTelegram()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


# Строки с сайтов: первые четыре — названия из журнала алертов прода, остальные
# добирают случаи, которых там пока не было.
OUTSIDE_STRINGS = [
    "Diş fırçası Colgate 360*C №1",
    'Uşaq yağı "Johnson`s"  200 ml',
    "HYAL [PRE] KURSOR göz serumu, 15 ml",
    "Vitaminlərlə Energetik Losyon 12*10 ml",
    "Vitamin_D3 2000 TV",
    "Krem [50 ml",
    "a_b_c *x* `y`",
    "back\\_slash \\",
    "😀_*`[",
]


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


# ─── Одна функция экранирования ──────────────────────────────────────────────


def _random_strings(count: int) -> list[str]:
    rng = random.Random(20261010)
    alphabet = "_*`[]()\\ab яə😀\n"
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12))) for _ in range(count)]


def test_an_escaped_string_reads_back_unchanged_and_opens_no_entity():
    for text in [*OUTSIDE_STRINGS, *_random_strings(3000)]:
        assert parse_legacy_markdown(notifier.telegram_escape(text)) == (text, []), repr(text)
        # В шаблоне, между чужими сущностями: они остаются на своих местах.
        shown, entities = parse_legacy_markdown(
            f"*H* `t` {notifier.telegram_escape(text)} (`1`)\n_i_"
        )
        assert shown == f"H t {text} (1)\ni", repr(text)
        assert [kind for kind, _, _ in entities] == ["bold", "code", "code", "italic"], repr(text)


@pytest.mark.parametrize(("wrap", "kind"), [("*", "bold"), ("_", "italic"), ("`", "code")])
def test_a_wrapped_string_reads_back_unchanged_inside_its_entity(wrap, kind):
    for text in [*OUTSIDE_STRINGS, *_random_strings(3000)]:
        shown, entities = parse_legacy_markdown(f"до {notifier.telegram_escape(text, wrap)} после")
        assert shown == f"до {text} после", repr(text)
        assert {entity_kind for entity_kind, _, _ in entities} <= {kind}, repr(text)
        # В сущности — вся строка, кроме самого знака обёртки: его внутри
        # сущности не записать.
        covered = sum(length for _, _, length in entities)
        assert covered == _utf16_len(text.replace(wrap, "")), repr(text)


def test_escape_takes_none_as_an_empty_string_and_refuses_an_unknown_entity():
    assert notifier.telegram_escape(None) == ""
    assert notifier.telegram_escape(None, "*") == ""
    assert notifier.telegram_escape("", "`") == ""
    with pytest.raises(ValueError):
        notifier.telegram_escape("x", "[")


# ─── Бот: /status, /alerts, /today, неизвестная команда ──────────────────────


@pytest.fixture
def bound_admin(db_session):
    """Чат привязан к действующему администратору — как у того, кто спрашивает бота."""
    tenant = tenants.get_or_create_default(db_session)
    user = storage.TenantUser(
        tenant_id=tenant.id,
        email="admin@pharmacy.example",
        role="admin",
        is_active=True,
        created_at=utcnow(),
        email_severity_min="off",
        telegram_severity_min="info",
        telegram_chat_id=CHAT_ID,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _event(session, title: str, detail: str | None = None) -> storage.AlertEvent:
    event = storage.AlertEvent(
        rule_type="price_drop_pct",
        dedup_key=f"k-{title[:40]}",
        severity="critical",
        title=title,
        detail=detail,
        created_at=utcnow(),
    )
    session.add(event)
    session.commit()
    return event


def _ask(session, text: str) -> None:
    telegram_bot.handle_update(session, {"message": {"chat": {"id": int(CHAT_ID)}, "text": text}})


@pytest.mark.parametrize("code", ["stale_run", "site_drop", "full_catalog_overdue", "no_runs"])
@pytest.mark.parametrize("message", OUTSIDE_STRINGS)
def test_status_arrives_with_issue_codes_and_messages_intact(
    db_session, bound_admin, telegram, monkeypatch, code, message
):
    report = health.HealthReport(
        status="critical",
        issues=[health.HealthIssue("critical", code, message)],
        last_run_id=12,
        last_run_status="failed",
    )
    monkeypatch.setattr(health, "check_health", lambda session: report)

    _ask(db_session, "/status")

    shown = telegram.only_message()
    assert f"  • {code}: {message}" in shown
    # Выделены заголовок и код проблемы — код целиком, одной сущностью (отсчёт
    # в единицах UTF-16). Текст проблемы не выделен ничем.
    offset = _utf16_len(shown[: shown.index(code)])
    assert telegram.entities[0] == [("bold", 3, len("Status:")), ("code", offset, len(code))]


def test_status_arrives_with_an_issue_code_intact_whatever_is_in_it(
    db_session, bound_admin, telegram, monkeypatch
):
    """Коды пишем мы, и знаков в них нет — но в сообщение код идёт тем же путём."""
    report = health.HealthReport(
        status="warning", issues=[health.HealthIssue("warning", "odd`code_[x", "текст")]
    )
    monkeypatch.setattr(health, "check_health", lambda session: report)

    _ask(db_session, "/status")

    assert "  • odd`code_[x: текст" in telegram.only_message()


@pytest.mark.parametrize("title", OUTSIDE_STRINGS)
def test_alerts_command_arrives_with_the_product_name_intact(
    db_session, bound_admin, telegram, title
):
    _event(db_session, title)

    _ask(db_session, "/alerts")

    assert telegram.only_message().endswith(f" {title}")
    # Выделены заголовок и время; название — нет.
    assert telegram.kinds() == ["bold", "code"]


@pytest.mark.parametrize("title", OUTSIDE_STRINGS)
def test_today_command_arrives_with_the_action_title_intact(
    db_session, bound_admin, telegram, monkeypatch, title
):
    action = roi.ActionItem(
        type="undercut", severity="critical", title=title, detail="", unit_gap_azn=-1.5
    )
    monkeypatch.setattr(roi, "get_cached_action_items", lambda *args, **kwargs: [action])

    _ask(db_session, "/today")

    assert f"1. 🔴 {title} (-1.50 ₼/ед, спред 0.0%)" in telegram.only_message()
    # Заголовок, две суммы, «Топ-3», разница в цене; название не выделено.
    assert telegram.kinds() == ["bold", "code", "code", "bold", "code"]


def test_every_command_reply_is_accepted_with_its_markup(db_session, bound_admin, telegram):
    """Шаблоны ответов — тоже разметка: непарный знак в них отнял бы у ответа выделение.

    У каждой команды проверяется один ответ — тот, что она даёт привязанному
    чату на пустой базе.
    """
    # Команда, отвязывающая чат (PR #72), — последней: после неё остальные
    # отвечали бы одним и тем же отказом.
    commands = sorted(telegram_bot.COMMANDS, key=lambda command: (command == "/stop", command))

    for command in commands:
        _ask(db_session, command)

    assert [form.get("parse_mode") for form in telegram.requests] == ["Markdown"] * len(commands)
    assert len(telegram.shown) == len(commands)


@pytest.mark.parametrize("command", ["/foo_bar", "/a`b", "/a*b", "/[x", "/``", "/`"])
def test_an_unknown_command_is_answered_with_the_typed_word_intact(db_session, telegram, command):
    _ask(db_session, command)

    assert telegram.only_message() == f"Неизвестная команда: {command}. /help"
    assert telegram.marked("code") == len(command.replace("`", ""))


def test_an_unknown_command_of_any_length_is_answered(db_session, telegram):
    """Без среза ответ на строку в четыре тысячи знаков не уместился бы в сообщение."""
    _ask(db_session, "/" + "`" * 4089)

    assert telegram.only_message() == "Неизвестная команда: /" + "`" * 63 + ". /help"


# Привязка по адресу (PR #72 её убирает: аргумент `/start` становится кодом, и
# бот его не повторяет — эти два теста проходят и тогда).
@pytest.mark.parametrize(
    "address", ["a`b@x.example", "a`b`@x.example", "`[x](http://evil.example)`@a", "a_b@x.example"]
)
def test_start_never_echoes_the_typed_address_altered(db_session, bound_admin, telegram, address):
    bound_admin.telegram_chat_id = None  # чат ещё не привязан: такого адреса у бота нет
    db_session.commit()

    _ask(db_session, f"/start {address}")

    shown = telegram.only_message()
    assert address in shown or address.replace("`", "") not in shown
    assert "link" not in telegram.kinds()


def test_start_never_echoes_a_bound_address_altered(db_session, bound_admin, telegram):
    bound_admin.email = "a`b`@x.example"
    bound_admin.telegram_chat_id = None
    db_session.commit()

    _ask(db_session, "/start a`b`@x.example")

    shown = telegram.only_message()
    assert "a`b`@x.example" in shown or "ab@x.example" not in shown


def test_a_failed_command_names_the_error_class_even_with_an_underscore(
    db_session, bound_admin, telegram, monkeypatch
):
    class _Lock_Lost(RuntimeError):  # noqa: N801 — подчёркивания и есть предмет теста
        pass

    def broken(session):
        raise _Lock_Lost("boom")

    monkeypatch.setattr(health, "check_health", broken)

    _ask(db_session, "/status")

    assert telegram.only_message() == "❌ Ошибка: _Lock_Lost"


def test_bot_commands_cut_a_long_outside_string_before_escaping_it(
    db_session, bound_admin, telegram, monkeypatch
):
    """Срез после экранирования оставил бы «\\» без пары или половину знаков."""
    long = "[" * 300
    report = health.HealthReport(
        status="critical", issues=[health.HealthIssue("critical", "stale_run", long)]
    )
    monkeypatch.setattr(health, "check_health", lambda session: report)
    action = roi.ActionItem(type="undercut", severity="critical", title=long, detail="")
    monkeypatch.setattr(roi, "get_cached_action_items", lambda *args, **kwargs: [action])
    _event(db_session, long)

    for command in ("/status", "/alerts", "/today"):
        _ask(db_session, command)

    assert [form.get("parse_mode") for form in telegram.requests] == ["Markdown"] * 3
    status, alerts_reply, today = telegram.shown
    assert status.endswith("  • stale_run: " + "[" * 80)
    assert alerts_reply.endswith(" " + "[" * 60)
    assert f"1. 🔴 {'[' * 60} (" in today


# ─── Рассылка алертов ────────────────────────────────────────────────────────


@pytest.mark.parametrize("title", OUTSIDE_STRINGS)
@pytest.mark.parametrize("detail", ["aloe: 10.00 → 8.00 ₼", "site_drop [aloe] 5*2 `x`_"])
def test_a_single_alert_arrives_with_title_and_detail_intact(
    db_session, bound_admin, telegram, title, detail
):
    event = _event(db_session, title, detail)

    notifications.dispatch_event(db_session, event)

    shown = telegram.only_message()
    assert shown.endswith(f"\n{title}\n{detail}")
    # Выделение на месте: название жирным, описание курсивом.
    assert telegram.marked("bold") == len("CRITICAL") + _utf16_len(title.replace("*", ""))
    assert telegram.marked("italic") == _utf16_len(detail.replace("_", ""))


def test_a_single_alert_cuts_a_long_title_before_escaping_it(db_session, bound_admin, telegram):
    """Срез после экранирования оставил бы «\\» без пары — и сущность без конца."""
    title = "*" * 300

    notifications.dispatch_event(db_session, _event(db_session, title))

    assert telegram.only_message().endswith("\n" + "*" * 200)


def test_a_single_alert_cuts_a_long_detail_before_escaping_it(db_session, bound_admin, telegram):
    detail = "x" * 295 + "_y" + "z" * 50

    notifications.dispatch_event(db_session, _event(db_session, "Цена упала", detail))

    assert telegram.only_message().endswith("\nЦена упала\n" + "x" * 295 + "_yzzz")


def test_a_batch_arrives_with_every_product_name_intact(db_session, bound_admin, telegram):
    events = [_event(db_session, title) for title in OUTSIDE_STRINGS]

    sent = notifications.dispatch_events_batch(db_session, events)

    assert sent == {"email": 0, "telegram": 1}
    lines = telegram.only_message().split("\n")
    assert lines[0] == f"Pharmacy Monitor — {len(events)} алертов"
    assert lines[1:] == [f"🔴 {title}" for title in OUTSIDE_STRINGS]
    # Выделен только заголовок.
    assert telegram.kinds() == ["bold"]


def test_a_batch_cuts_a_long_title_before_escaping_it(db_session, bound_admin, telegram):
    notifications.dispatch_events_batch(db_session, [_event(db_session, "[" * 300)])

    assert telegram.only_message().split("\n")[1] == "🔴 " + "[" * 120


def test_admin_only_alerts_arrive_with_every_product_name_intact(db_session, bound_admin, telegram):
    events = [_event(db_session, title) for title in OUTSIDE_STRINGS]

    counts = notifications.mail_unstored_events_to_admins(db_session, events, note="с тика")

    assert counts == {"email": 0, "telegram": 1, "failed": 0}
    lines = telegram.only_message().split("\n")
    assert lines[1:-1] == [f"🔴 {title}" for title in OUTSIDE_STRINGS]
    assert lines[-1] == notifications._ADMIN_ONLY_TELEGRAM_NOTE


@pytest.mark.parametrize("title", OUTSIDE_STRINGS)
@pytest.mark.parametrize("detail", [None, "aloe: 10.00 → 8.00 ₼", "run_id [5] 2*2 `x`"])
def test_a_rule_channel_alert_arrives_with_title_and_detail_intact(
    db_session, telegram, title, detail
):
    """`alerts.dispatch_event` — путь по каналам правила, получатели из `recipients`."""
    rule = storage.AlertRule(
        name="r", rule_type="price_drop_pct", params={}, channels=["telegram"], is_active=True
    )
    db_session.add_all(
        [rule, storage.Recipient(email="a@x.example", is_active=True, telegram_chat_id=CHAT_ID)]
    )
    db_session.flush()
    event = _event(db_session, title, detail)
    event.rule_id = rule.id
    db_session.commit()

    result = alerts.dispatch_event(db_session, event)

    assert result == {"telegram": "sent to 1/1"}
    assert telegram.only_message() == f"🔴 {title}\n\n{detail or ''}"
    assert telegram.entities[0] == [] or {kind for kind, _, _ in telegram.entities[0]} == {"bold"}
    assert telegram.marked("bold") == _utf16_len(title.replace("*", ""))


# ─── Отказ на разметке: тот же текст уходит без неё ──────────────────────────

BROKEN_MARKUP = "🔴 *Цена упала*: Vitamin_D3 2000 TV"


def test_a_message_refused_for_its_markup_is_sent_again_as_plain_text(telegram):
    with capture_logs() as logs:
        delivered = notifier.send_telegram_message(CHAT_ID, BROKEN_MARKUP)

    assert delivered is True
    assert telegram.requests == [
        {
            "chat_id": CHAT_ID,
            "text": BROKEN_MARKUP,
            "disable_web_page_preview": "True",
            "parse_mode": "Markdown",
        },
        {"chat_id": CHAT_ID, "text": BROKEN_MARKUP, "disable_web_page_preview": "True"},
    ]
    assert telegram.shown == [BROKEN_MARKUP]
    # В журнале — что разметку не приняли, и ничего о том, кому и что слали.
    assert logs == [
        {"event": "telegram_markup_refused", "log_level": "warning", "parse_mode": "Markdown"}
    ]


def test_a_message_with_sound_markup_is_sent_once(telegram):
    with capture_logs() as logs:
        assert notifier.send_telegram_message(CHAT_ID, "*Цена упала*: `8.00`") is True

    assert [form.get("parse_mode") for form in telegram.requests] == ["Markdown"]
    assert telegram.shown == ["Цена упала: 8.00"]
    assert logs == []


def test_a_refusal_that_is_not_about_markup_stays_a_failure(monkeypatch):
    """400 — и «нет такого чата». Повтор без разметки отказ не превращает в доставку."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "100200:test-token")
    fake = FakeTelegram(always=(400, "Bad Request: chat not found"))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    with capture_logs() as logs:
        delivered = notifier.send_telegram_message(CHAT_ID, "*Цена упала*")

    assert delivered is False
    assert [form.get("parse_mode") for form in fake.requests] == ["Markdown", None]
    assert [entry["event"] for entry in logs] == ["telegram_send_failed"]
    assert CHAT_ID not in json.dumps(logs, ensure_ascii=False)
    assert "Цена" not in json.dumps(logs, ensure_ascii=False)


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429, 500])
def test_only_a_bad_request_is_sent_again(monkeypatch, status):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "100200:test-token")
    fake = FakeTelegram(always=(status, "refused"))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    assert notifier.send_telegram_message(CHAT_ID, BROKEN_MARKUP) is False
    assert len(fake.requests) == 1


def test_a_retry_that_fails_for_another_reason_is_a_failure(monkeypatch):
    """Разметку не приняли, а повтор не дошёл: в журнале остаётся только его сбой."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "100200:test-token")
    replies = [
        _refusal("https://api.telegram.org/", 400, "Bad Request: can't parse entities: …"),
        urllib.error.URLError(OSError(101, "Network is unreachable")),
    ]

    def flaky(request, timeout=None, context=None):
        raise replies.pop(0)

    monkeypatch.setattr("urllib.request.urlopen", flaky)

    with capture_logs() as logs:
        delivered = notifier.send_telegram_message(CHAT_ID, BROKEN_MARKUP)

    assert delivered is False
    assert replies == []
    assert [entry["event"] for entry in logs] == ["telegram_send_failed"]


def test_a_network_failure_is_not_sent_again(monkeypatch):
    """Запрос мог дойти: повтор после обрыва связи — риск второго сообщения."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "100200:test-token")
    calls = []

    def unreachable(request, timeout=None, context=None):
        calls.append(request)
        raise urllib.error.URLError(OSError(101, "Network is unreachable"))

    monkeypatch.setattr("urllib.request.urlopen", unreachable)

    assert notifier.send_telegram_message(CHAT_ID, BROKEN_MARKUP) is False
    assert len(calls) == 1


@pytest.mark.parametrize("no_markup", ["", None])
def test_a_plain_text_message_is_never_sent_twice(monkeypatch, no_markup):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "100200:test-token")
    fake = FakeTelegram(always=(400, "Bad Request: message is too long"))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    assert notifier.send_telegram_message(CHAT_ID, BROKEN_MARKUP, parse_mode=no_markup) is False
    assert fake.requests == [
        {"chat_id": CHAT_ID, "text": BROKEN_MARKUP, "disable_web_page_preview": "True"}
    ]


def test_a_bot_reply_refused_for_its_markup_still_reaches_the_chat(
    db_session, bound_admin, telegram, monkeypatch
):
    """Вторая линия: место, где экранирование забыли, сообщение не теряет."""
    monkeypatch.setitem(telegram_bot.COMMANDS, "/raw", lambda session, chat, args: BROKEN_MARKUP)

    _ask(db_session, "/raw")

    assert [form.get("parse_mode") for form in telegram.requests] == ["Markdown", None]
    assert telegram.shown == [BROKEN_MARKUP]
