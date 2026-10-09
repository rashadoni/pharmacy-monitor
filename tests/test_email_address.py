"""Что система считает почтовым адресом — и что это правило держит.

Письмо по списку получателей одно на всех. Запись, которая не адрес, срывала
его сборку всему списку, а вход её пропускал: `recipient add` принимал любую
строку, API — любую с «@». Здесь три слоя:

1. само правило (`src/email_address.py`): что принято, в чём отказано и с каким
   кодом; принятый адрес маска журнала вырезает целиком, и настоящая сборка
   письма smtplib проходит с ним без ошибки;
2. входы: `recipient add`, `recipient update --new-email`, `tenant add-user`,
   страница «Пользователи», запрос ссылки на вход — отказ ничего не пишет;
3. отправка: запись, которая в список всё же попала (старая строка, правка
   `EMAIL_TO` руками), остаётся без письма одна, остальные его получают, а в
   журнале о ней — номер и код причины, не она сама.
"""

from __future__ import annotations

import json
import random
import re
import smtplib
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import api, main, notifier, storage, tenants, watchlist
from src._time import utcnow
from src.email_address import (
    _MAX_MAILBOX_LENGTH,
    MAX_LENGTH,
    PROBLEMS,
    InvalidEmailAddress,
    address_problem,
    is_address,
    normalize_address,
)
from src.logging_setup import ADDRESS_MASK, mask_addresses, without_addresses

FIRST = "viewer@client.example"
SECOND = "second@client.example"

ACCEPTED = [
    FIRST,
    "a@b.co",
    "user.name+tag@sub.example.co.uk",
    "u_s%r-1@a-b.az",
    "0@1a.az",
    "x" * 64 + "@example.com",
]

# Запись → код причины. Первые пять — те, на которых сборка письма падала у
# настоящего smtplib (`tests/test_log_carries_no_address.py`).
REFUSED = {
    "[\\,группа:": "not_ascii",
    ":": "no_at",
    ".": "no_at",
    "=?utf-8?q?=0A": "no_at",
    "я": "not_ascii",
    "": "empty",
    "   ": "empty",
    "plain": "no_at",
    "иван@пример.рф": "not_ascii",
    "rəşad@client.example": "not_ascii",
    "viеwer@client.example": "not_ascii",  # «е» кириллическая: раскладка
    "a b@client.example": "whitespace",
    "viewer@client.example\nBcc: other@client.example": "whitespace",
    "viewer​@client.example": "whitespace",  # невидимый знак из буфера обмена
    "Иван Петров <viewer@client.example>": "whitespace",
    "viewer@client.example,second@client.example": "several",
    "viewer@client.example;second@client.example": "several",
    "<viewer@client.example>": "several",
    '"viewer"@client.example': "several",
    "viewer(note)@client.example": "several",
    "viewer@client@example.com": "several_at",
    "x" * (MAX_LENGTH - 11) + "@example.com": "too_long",
    "@client.example": "mailbox",
    ".viewer@client.example": "mailbox",
    "viewer.@client.example": "mailbox",
    "vie..wer@client.example": "mailbox",
    "o'brien@client.example": "mailbox",
    "x" * 65 + "@client.example": "mailbox",
    "viewer@": "domain",
    "viewer@local": "domain",
    "viewer@client.e": "domain",
    "viewer@client.ex4mple": "domain",
    "viewer@-client.example": "domain",
    "viewer@client-.example": "domain",
    "viewer@client..example": "domain",
    "viewer@.example": "domain",
    "viewer@[192.0.2.1]": "domain",
    "viewer@xn--e1afmkfd.xn--p1ai": "domain",
    tenants.BOOTSTRAP_ADMIN_EMAIL: "domain",
}


# ─── Слой 1: правило ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("address", ACCEPTED)
def test_an_address_is_accepted_as_it_is_stored(address):
    assert address_problem(address) is None
    assert is_address(address)
    assert normalize_address(address) == address


def test_case_and_outer_whitespace_are_not_a_reason_to_refuse():
    assert normalize_address("  Viewer@Client.Example\n") == FIRST


@pytest.mark.parametrize(("record", "problem"), REFUSED.items())
def test_a_record_that_is_not_an_address_is_refused_with_its_reason(record, problem):
    assert address_problem(record) == problem
    assert not is_address(record)
    with pytest.raises(InvalidEmailAddress) as refused:
        normalize_address(record)
    assert refused.value.problem == problem
    assert str(refused.value) == f"Адрес не принят: {PROBLEMS[problem]}."


def test_what_is_not_a_string_is_not_an_address():
    assert address_problem(None) == "empty"


def test_every_reason_is_reachable_and_has_a_text_that_can_be_shown_anywhere():
    """Код без примера выше — мёртвый или непроверенный. Текст причины
    печатают и мимо журнала, а `without_addresses` скрывает текст с «@» целиком:
    отказ, который оператор не может прочитать, — не отказ."""
    assert set(REFUSED.values()) == set(PROBLEMS)
    for problem in PROBLEMS:
        text = str(InvalidEmailAddress(problem))
        assert without_addresses(text) == text, text


def test_the_refusal_never_repeats_the_record():
    """В записи бывает настоящий адрес с опечаткой, а текст отказа идёт в вывод
    команды и в ответ API."""
    for record in REFUSED:
        if len(record.strip()) < 4:
            continue  # «.» и «:» есть в любом тексте
        with pytest.raises(InvalidEmailAddress) as refused:
            normalize_address(record)
        shown = str(refused.value) + repr(refused.value) + repr(vars(refused.value))
        assert record.strip() not in shown


def _candidates(count: int) -> list[str]:
    """Строки, похожие на адрес. Знаки, на которых правила расходятся (точка,
    дефис, подчёркивание, апостроф, нелатинская буква), стоят в случайных
    местах; посторонних — немного, иначе правило отсеет почти всё."""
    rng = random.Random(20261009)
    mailbox_chars, mailbox_weights = (
        "ab1._%+-'я ,\"<:",
        [8, 8, 8, 4, 4, 4, 4, 4, 1, 1, 1, 1, 1, 1, 1],
    )
    domain_chars, domain_weights = "ab1.-_я [:", [8, 8, 8, 5, 5, 1, 1, 1, 1, 1]
    found = []
    for _ in range(count):
        mailbox = "".join(rng.choices(mailbox_chars, mailbox_weights, k=rng.randint(1, 6)))
        domain = "".join(rng.choices(domain_chars, domain_weights, k=rng.randint(1, 8)))
        zone = rng.choice(["az", "com", "example", "example", "c", "c0m", "рф", ""])
        found.append(f"{mailbox}@{domain}.{zone}" if zone else f"{mailbox}@{domain}")
    return found


def _accepted_candidates() -> list[str]:
    candidates = _candidates(6000)
    accepted = sorted({normalize_address(c) for c in candidates if is_address(c)})
    # Перебор не вхолостую: и принятых, и отвергнутых — сотни.
    assert len(accepted) > 300 and len(candidates) - len(accepted) > 300, len(accepted)
    return accepted


def test_an_accepted_address_is_cut_from_the_log_whole():
    """На этом стоит «имя ящика — только такие знаки»: от адреса, который
    маска вырезает не целиком (`o'brien@…`), в журнале осталось бы начало имени.
    Строка журнала бывает как есть и JSON — проверяются обе записи."""
    for address in ACCEPTED + _accepted_candidates():
        assert mask_addresses(f"to {address} failed") == f"to {ADDRESS_MASK} failed", address
        assert mask_addresses(json.dumps({"to": address})) == json.dumps({"to": ADDRESS_MASK})


class _RecordingSmtplib(smtplib.SMTP):
    """Настоящий smtplib без сети: письмо собирает и укладывает в байты пакет
    `email`, как при настоящей отправке; сервер SMTPUTF8 не объявляет. Что ушло
    бы серверу, лежит в `sent`."""

    sent: list[tuple[list[str], bytes]] = []

    def __init__(self, *args, **kwargs):
        super().__init__()  # без адреса сервера smtplib не соединяется

    def __exit__(self, *args):
        return False

    def starttls(self, *args, **kwargs):
        pass

    def login(self, *args, **kwargs):
        pass

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, name):
        return False

    def sendmail(self, from_addr, to_addrs, msg, *args, **kwargs):
        type(self).sent.append((list(to_addrs), msg))
        return {}


@pytest.fixture
def outbox(monkeypatch):
    """Письма, которые дошли бы до почтового сервера: (конверт, байты письма)."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "resend")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_FROM", "digest@sender.example")
    monkeypatch.delenv("EMAIL_TO", raising=False)
    monkeypatch.setattr(_RecordingSmtplib, "sent", [])
    monkeypatch.setattr("smtplib.SMTP", _RecordingSmtplib)
    return _RecordingSmtplib.sent


def _to_header(message: bytes) -> str:
    return next(
        line.split(b":", 1)[1].strip().decode()
        for line in message.split(b"\r\n")
        if line.lower().startswith(b"to:")
    )


def test_an_accepted_address_goes_through_the_real_assembly(outbox):
    """Правило не просто строже прежнего: с адресом, который оно приняло,
    пакет `email` письмо собирает, а в конверте стоит ровно этот адрес."""
    addresses = ACCEPTED + _accepted_candidates()[:300]
    for address in addresses:
        assert notifier.send_email("Тема", "<p>x</p>", to=[address]) is True
    assert [envelope for envelope, _ in outbox] == [[address] for address in addresses]


# ─── Слой 2: входы ───────────────────────────────────────────────────────────


@pytest.fixture
def database(monkeypatch, db_session):
    """База, в которую ходят команды CLI и API."""
    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *args, **kwargs: Session)
    monkeypatch.setattr(storage, "init_db", lambda *args, **kwargs: None)
    return db_session


def _stored_recipients(session) -> list[str]:
    session.expire_all()
    return sorted(session.scalars(select(storage.Recipient.email)).all())


def _stored_users(session) -> list[str]:
    session.expire_all()
    return sorted(session.scalars(select(storage.TenantUser.email)).all())


@pytest.mark.parametrize("record", REFUSED)
def test_add_recipient_stores_no_record_that_is_not_an_address(db_session, record):
    with pytest.raises(InvalidEmailAddress):
        watchlist.add_recipient(db_session, record)
    assert _stored_recipients(db_session) == []


def test_add_recipient_stores_the_address_normalized(db_session):
    watchlist.add_recipient(db_session, "  Viewer@Client.Example ")
    assert _stored_recipients(db_session) == [FIRST]


def test_update_recipient_keeps_the_old_address_when_the_new_one_is_refused(db_session):
    watchlist.add_recipient(db_session, FIRST)
    with pytest.raises(InvalidEmailAddress):
        watchlist.update_recipient(db_session, FIRST, new_email="viewer@local")
    db_session.rollback()
    assert _stored_recipients(db_session) == [FIRST]


def test_an_old_record_that_is_not_an_address_can_still_be_found_and_fixed(db_session):
    """Запись, попавшая в список до правила, ищется по своему тексту: её можно
    выключить, заменить адресом и удалить — проверка стоит на новом адресе, а
    не на поиске."""
    record = "[\\,группа:"
    db_session.add(storage.Recipient(email=record, is_active=True))
    db_session.commit()

    assert watchlist.toggle_recipient(db_session, record).is_active is False
    assert watchlist.update_recipient(db_session, record, new_email=FIRST).email == FIRST

    db_session.add(storage.Recipient(email=record, is_active=True))
    db_session.commit()
    assert watchlist.remove_recipient(db_session, record) is True
    assert _stored_recipients(db_session) == [FIRST]


@pytest.mark.parametrize("record", [r for r in REFUSED if r != tenants.BOOTSTRAP_ADMIN_EMAIL])
def test_add_user_stores_no_record_that_is_not_an_address(db_session, record):
    tenant = tenants.get_or_create_default(db_session)
    with pytest.raises(InvalidEmailAddress):
        tenants.add_user(db_session, tenant.id, record)
    assert _stored_users(db_session) == []


def test_the_first_administrator_still_gets_its_placeholder(db_session):
    """Под заглушкой входят паролем, пока администратору не задан адрес; без
    неё вход на чистой базе не создал бы пользователя вовсе."""
    tenant = tenants.get_or_create_default(db_session)
    user = tenants.add_user(db_session, tenant.id, tenants.BOOTSTRAP_ADMIN_EMAIL, role="admin")
    assert user.email == tenants.BOOTSTRAP_ADMIN_EMAIL
    assert not is_address(user.email)  # писем на неё нет: при отправке её пропускают


@pytest.mark.parametrize(
    "command",
    [
        ["recipient", "add", "viewer@local"],
        ["recipient", "add", "я"],
        ["recipient", "update", FIRST, "--new-email", "viewer@local"],
    ],
)
def test_the_recipient_commands_refuse_in_words_and_store_nothing(database, command):
    watchlist.add_recipient(database, FIRST)

    result = CliRunner().invoke(main.cli, command)

    assert result.exit_code == 1, result.output
    assert result.output.startswith("Error: Адрес не принят: "), result.output
    assert "Traceback" not in result.output
    assert _stored_recipients(database) == [FIRST]


def test_tenant_add_user_refuses_in_words_and_stores_nothing(database):
    tenants.get_or_create_default(database)

    result = CliRunner().invoke(main.cli, ["tenant", "add-user", "default", "viewer@local"])

    assert result.exit_code == 1, result.output
    assert result.output == f"Error: Адрес не принят: {PROBLEMS['domain']}.\n"
    assert _stored_users(database) == []


def test_the_commands_still_take_an_address(database):
    tenants.get_or_create_default(database)
    runner = CliRunner()

    assert runner.invoke(main.cli, ["recipient", "add", " Viewer@Client.Example"]).exit_code == 0
    assert runner.invoke(main.cli, ["tenant", "add-user", "default", SECOND]).exit_code == 0

    assert _stored_recipients(database) == [FIRST]
    assert _stored_users(database) == [SECOND]


@pytest.fixture
def admin_page(database, monkeypatch):
    """Страница «Пользователи»: запросы от имени администратора."""
    tenant = tenants.get_or_create_default(database)
    admin = storage.TenantUser(
        tenant_id=tenant.id,
        email="admin@client.example",
        role="admin",
        is_active=True,
        created_at=utcnow(),
    )
    database.add(admin)
    database.commit()
    monkeypatch.setitem(api.app.dependency_overrides, api.require_user, lambda: admin)
    return TestClient(api.app)


@pytest.mark.parametrize(
    ("record", "problem"),
    [
        ("viewer@local", "domain"),
        ("rəşad@client.example", "not_ascii"),
        ("o'brien@client.example", "mailbox"),
        ("viewer@client.example, second@client.example", "whitespace"),
    ],
)
def test_the_users_page_gets_the_reason_as_a_code_and_in_words(
    admin_page, database, record, problem
):
    """Раньше хватало «@» в строке. Код причины — для перевода на странице,
    текст — для того, кто зовёт API напрямую."""
    response = admin_page.post("/api/v1/dash/recipients", json={"email": record})

    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "email_address"
    assert error["loc"] == ["body", "email"]
    assert error["ctx"]["problem"] == problem
    assert error["msg"] == f"Адрес не принят: {PROBLEMS[problem]}."
    assert _stored_users(database) == ["admin@client.example"]


def test_the_users_page_still_adds_an_address(admin_page, database):
    response = admin_page.post("/api/v1/dash/recipients", json={"email": " Viewer@Client.Example"})

    assert response.status_code == 200, response.text
    assert response.json()["email"] == FIRST
    assert _stored_users(database) == ["admin@client.example", FIRST]


def test_a_login_link_is_not_requested_for_what_is_not_an_address(database):
    client = TestClient(api.app)

    refused = client.post("/auth/request", json={"email": "viewer@local"})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"][0]["ctx"]["problem"] == "domain"

    # Адрес, которого в базе нет, — по-прежнему 200: есть ли такой пользователь,
    # ответ не выдаёт.
    assert client.post("/auth/request", json={"email": FIRST}).status_code == 200


FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
USERS_PAGE = (
    FRONTEND / "src" / "app" / "[locale]" / "(dashboard)" / "settings" / "users" / "page.tsx"
)


def test_the_users_page_has_words_for_every_reason_in_every_language():
    """Страница берёт перевод по коду причины. Новый код без перевода показал
    бы администратору имя ключа, а число в тексте («длиннее 200 знаков») —
    разошлось бы с правилом молча."""
    listed = re.search(
        r"const ADDRESS_PROBLEMS = \[(.*?)\] as const", USERS_PAGE.read_text("utf-8"), re.S
    )
    assert listed, "страница «Пользователи» больше не держит список причин ADDRESS_PROBLEMS"
    assert set(re.findall(r'"(\w+)"', listed.group(1))) == set(PROBLEMS)

    for locale in ("ru", "az", "en"):
        users = json.loads((FRONTEND / "messages" / f"{locale}.json").read_text("utf-8"))["users"]
        prefix = "address_problem_"
        translated = {
            key.removeprefix(prefix): text for key, text in users.items() if prefix in key
        }
        assert set(translated) == set(PROBLEMS), locale
        assert str(MAX_LENGTH) in translated["too_long"], locale
        assert str(_MAX_MAILBOX_LENGTH) in translated["mailbox"], locale
        # next-intl читает текст как ICU: фигурная скобка и апостроф в нём — разметка.
        assert not any(set("{}'<") & set(text) for text in translated.values()), locale


# ─── Слой 3: отправка ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("record", "problem"), REFUSED.items())
def test_one_bad_record_no_longer_takes_the_letter_from_the_rest(outbox, record, problem):
    """Письмо на список одно. Запись, которая не адрес, остаётся без него одна;
    в журнале — её место в списке и код причины, самой записи и соседей нет."""
    with capture_logs() as logs:
        assert notifier.send_email("Тема", "<p>x</p>", to=[FIRST, record, SECOND]) is True

    ((envelope, message),) = outbox
    assert envelope == [FIRST, SECOND]
    assert _to_header(message) == f"{FIRST}, {SECOND}"
    assert logs[0] == {
        "event": "email_recipients_skipped",
        "log_level": "warning",
        "source": "to",
        "positions": [2],
        "problems": [problem],
        "skipped": 1,
        "sending_to": 2,
    }
    assert [entry["event"] for entry in logs[1:]] == ["smtp_send", "smtp_sent_ok"]
    assert logs[1]["recipients"] == 2
    assert "@" not in repr(logs), logs
    if len(record.strip()) > 3:
        assert record.strip() not in repr(logs), logs


def test_a_bad_row_of_the_recipients_table_is_named_by_its_id(outbox, database):
    """Список по умолчанию — таблица `recipients`: запись в журнале названа так,
    как её найдут в базе."""
    watchlist.add_recipient(database, FIRST)
    bad = storage.Recipient(email="viewer@local", is_active=True)
    off = storage.Recipient(email="выключен", is_active=False)
    database.add_all([bad, off])
    database.commit()
    watchlist.add_recipient(database, SECOND)

    with capture_logs() as logs:
        assert notifier.send_email("Тема", "<p>x</p>") is True

    ((envelope, _),) = outbox
    assert sorted(envelope) == [SECOND, FIRST]
    assert logs[0] == {
        "event": "email_recipients_skipped",
        "log_level": "warning",
        "source": "recipients",
        "recipient_ids": [bad.id],
        "problems": ["domain"],
        "skipped": 1,
        "sending_to": 2,
    }


def test_a_bad_entry_of_email_to_is_named_by_its_place(outbox, database, monkeypatch):
    """`EMAIL_TO` правят руками в файле окружения — проверки на входе у него нет."""
    monkeypatch.setenv("EMAIL_TO", f"{FIRST}, viewer@local,я ,{SECOND}")

    with capture_logs() as logs:
        assert notifier.send_email("Тема", "<p>x</p>") is True

    ((envelope, _),) = outbox
    assert envelope == [FIRST, SECOND]
    assert logs[0]["source"] == "EMAIL_TO"
    assert logs[0]["positions"] == [2, 3]
    assert logs[0]["problems"] == ["domain", "not_ascii"]
    assert "@" not in repr(logs), logs


def test_the_address_goes_out_as_it_is_stored(outbox):
    """Перевод строки по краю записи `EMAIL_TO` — уже ошибка сборки заголовка."""
    assert notifier.send_email("Тема", "<p>x</p>", to=[" Viewer@Client.Example\n"]) is True
    assert outbox[0][0] == [FIRST]


@pytest.mark.parametrize("records", [["viewer@local"], ["я", ":", "viewer@local"]])
def test_a_list_without_a_single_address_is_a_failure_not_silence(outbox, records):
    """Некому слать — письмо не «отправлено»: вызывающий получает ошибку
    отправки со своим классом, как при любом другом сбое, и пишет её в журнал."""
    with capture_logs() as logs, pytest.raises(notifier.EmailSendError) as raised:
        notifier.send_email("Тема", "<p>x</p>", to=records)

    assert outbox == []
    assert notifier.delivery_error_fields(raised.value) == {"error_type": "NoValidRecipients"}
    assert "@" not in str(raised.value)
    assert [entry["event"] for entry in logs] == ["email_recipients_skipped"]
    assert logs[0]["sending_to"] == 0


def test_a_letter_to_one_user_names_the_user_when_the_record_is_not_an_address(
    outbox, database, monkeypatch
):
    """Письма по `tenant_users` идут каждому отдельно: запись, которая не
    адрес, срывает письмо только своему пользователю, и назван он `user_id`."""
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", "https://example.com")
    tenant = tenants.get_or_create_default(database)
    user = tenants.add_user(database, tenant.id, tenants.BOOTSTRAP_ADMIN_EMAIL, role="admin")

    with capture_logs() as logs:
        assert api._send_login_link(database, user.email) is True

    assert outbox == []
    failed = next(entry for entry in logs if entry["event"] == "login_link_email_failed")
    assert failed["user_id"] == user.id
    assert failed["error_type"] == "NoValidRecipients"
    assert "@" not in repr(logs), logs
