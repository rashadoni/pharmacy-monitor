"""Ссылка на вход: «отправлена» — только о письме, которое отправитель подтвердил.

`api._send_login_link` отвечает публичному `/auth/request` и намеренно говорит
одно и то же, ушло письмо или нет: по ответу нельзя узнать ни есть ли такой
адрес, ни работает ли почта. Действиям администратора этого мало — кнопка
«Отправить ссылку» и приглашение при создании пользователя говорят оператору
«отправлено». Им исход называет `api._deliver_login_link`.

Каждое место — сама функция, кнопка, создание пользователя, `/auth/request` —
вызывается с отправителем, который не отправил: подменённым (ответил `False`,
ответил `None`, бросил исключение) и настоящим `send_email` (нет SMTP, сервер
отверг адрес, SMTP настроен наполовину). Помощники — из
`test_unconfirmed_send_is_not_sent`.
"""

from __future__ import annotations

import ast
import contextlib
import json
import re
import smtplib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src import api, auth, notifier, storage, tenants
from tests.test_unconfirmed_send_is_not_sent import (
    ALICE,
    BOB,
    REFUSALS,
    _assert_no_person_or_text,
    _error_fields,
    _Journal,
    _refusing,
    _user,
)

ADMIN = "owner@client.example"
NOBODY = "nobody@client.example"
REPO = Path(api.__file__).resolve().parent.parent
SAME_ANSWER = (200, {"sent": True, "detail": "If the email is registered, a magic-link was sent."})
WELCOME = "Вам открыт доступ"  # есть только в письме-приглашении


@pytest.fixture
def desk(monkeypatch, db_session):
    """Страница «Пользователи»: администратор вошёл, Alice заведена; SMTP не
    настроен, сеть запрещена."""
    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *a, **kw: Session)
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", "https://example.com")
    monkeypatch.delenv("SMTP_HOST", raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("тест не должен ходить в сеть")

    monkeypatch.setattr(smtplib, "SMTP", no_network)
    # Пять запросов в минуту с одного адреса — не предмет этих тестов.
    monkeypatch.setattr(api, "_check_rate_limit", lambda *a, **kw: None)
    admin = _user(db_session, ADMIN)
    alice = _user(db_session, ALICE, role="viewer")
    journal, mail_journal = _Journal(), _Journal()
    monkeypatch.setattr(api, "log", journal)
    monkeypatch.setattr(notifier, "log", mail_journal)

    def db():
        yield db_session

    api.app.dependency_overrides[api.require_user] = lambda: admin
    api.app.dependency_overrides[api.get_db] = db
    yield SimpleNamespace(
        db=db_session,
        client=TestClient(api.app),
        admin=admin,
        alice=alice,
        journal=journal,
        mail_journal=mail_journal,
    )
    api.app.dependency_overrides.clear()


def _failure_line(user_id: int, **error) -> dict:
    return {
        "event": "login_link_email_failed",
        "log_level": "warning",
        "user_id": user_id,
        **error,
    }


# Настоящий `send_email`, который письмо не отправил, и что строка сбоя несёт о
# причине. Третий случай — не сетевая ошибка и не отказ сервера.
REAL_SENDER = {
    "no_smtp": {},
    "server_refuses": {
        "error_type": "SMTPRecipientsRefused",
        "smtp_code": 550,
        "smtp_status": "5.1.1",
    },
    "half_configured": {"error_type": "KeyError"},
}


def _real_sender_does_not_send(monkeypatch, mail: str) -> None:
    if mail == "no_smtp":
        return  # так стоит `desk`: отправитель ответит False
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    if mail == "half_configured":
        monkeypatch.delenv("SMTP_USER", raising=False)
        return
    monkeypatch.setenv("SMTP_USER", "resend")

    class RefusingSMTP:
        """Сервер на связи, но адрес получателя отвергает."""

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            pass

        def login(self, *args):
            pass

        def send_message(self, msg):
            reply = f"5.1.1 <{msg['To']}>: Recipient address rejected".encode()
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, reply)})

    monkeypatch.setattr(smtplib, "SMTP", RefusingSMTP)


# ─── 1. Исход отправки — `_deliver_login_link` ───────────────────────────────


@pytest.mark.parametrize("how", REFUSALS)
def test_login_link_is_not_sent_when_the_sender_did_not_confirm(desk, how):
    with patch("src.notifier.send_email", **_refusing(how)) as send:
        outcome = api._deliver_login_link(desk.db, ALICE)

    assert outcome == api.LOGIN_LINK_NOT_SENT == "not_sent"
    assert send.call_count == 1  # одна попытка: повтора нет
    assert desk.journal.lines == [_failure_line(desk.alice.id, **_error_fields(how))]
    _assert_no_person_or_text(desk.journal.lines)


def test_login_link_is_sent_only_when_the_sender_confirmed(desk):
    with patch("src.notifier.send_email", return_value=True) as send:
        outcome = api._deliver_login_link(desk.db, f"  {ALICE.upper()} ")

    assert outcome == api.LOGIN_LINK_SENT == "sent"
    assert send.call_count == 1
    assert send.call_args.kwargs["to"] == [ALICE]
    assert desk.journal.lines == []


@pytest.mark.parametrize("who", ["unknown", "deactivated", "token_not_issued"])
def test_login_link_without_an_active_user_composes_no_letter(desk, monkeypatch, who):
    if who == "deactivated":
        desk.alice.is_active = False
        desk.db.commit()
    if who == "token_not_issued":  # пользователя отключили между проверкой и выдачей
        monkeypatch.setattr(tenants, "issue_magic_token", lambda *a, **kw: None)
    address = NOBODY if who == "unknown" else ALICE

    with patch("src.notifier.send_email", return_value=True) as send:
        outcome = api._deliver_login_link(desk.db, address)

    assert outcome == api.LOGIN_LINK_NO_USER == "no_user"
    assert not send.called
    assert desk.journal.lines == []


@pytest.mark.parametrize("mail", REAL_SENDER)
def test_login_link_the_real_sender_did_not_send_is_not_sent(desk, monkeypatch, mail):
    """Без SMTP настоящий отправитель отвечает False, при отказе сервера и
    недонастроенной почте — бросает; исход один."""
    _real_sender_does_not_send(monkeypatch, mail)

    outcome = api._deliver_login_link(desk.db, ALICE)

    assert outcome == api.LOGIN_LINK_NOT_SENT
    # Кому не ушло, отправитель не знает — это пишет вызывающий.
    assert desk.journal.lines == [_failure_line(desk.alice.id, **REAL_SENDER[mail])]
    said_by_sender = [line["event"] for line in desk.mail_journal.lines]
    assert (
        said_by_sender
        == {
            "no_smtp": ["email_skipped_no_smtp"],
            "server_refuses": ["smtp_send"],
            "half_configured": [],
        }[mail]
    )
    _assert_no_person_or_text([desk.journal.lines, desk.mail_journal.lines])


# ─── 2. Публичный `/auth/request` отвечает одинаково ─────────────────────────


@pytest.mark.parametrize("sender", ["confirmed", *REFUSALS])
def test_public_function_tells_whether_the_user_exists_not_whether_mail_went(desk, sender):
    answer = {"return_value": True} if sender == "confirmed" else _refusing(sender)

    with patch("src.notifier.send_email", **answer) as send:
        assert api._send_login_link(desk.db, ALICE) is True
        assert send.call_count == 1
        assert api._send_login_link(desk.db, NOBODY) is False
        assert send.call_count == 1  # несуществующему письмо и не собирали


def test_public_function_still_passes_the_invite_wording_through(desk):
    with patch("src.notifier.send_email", return_value=True) as send:
        api._send_login_link(desk.db, ALICE)
        api._send_login_link(desk.db, ALICE, invite=True)

    plain, invite = (call.kwargs["html_body"] for call in send.call_args_list)
    assert WELCOME in invite and WELCOME not in plain


def test_auth_request_answers_the_same_whatever_happened_to_the_letter(desk):
    """По ответу нельзя узнать ни есть ли такой адрес, ни ушло ли письмо."""
    bob = _user(desk.db, BOB, role="viewer", is_active=False)
    answers = {}

    def ask(case: str, address: str, **sender):
        with patch("src.notifier.send_email", **sender):
            response = desk.client.post("/auth/request", json={"email": address})
        answers[case] = (response.status_code, response.json())

    ask("unknown address", NOBODY, return_value=True)
    ask("deactivated user", bob.email, return_value=True)
    ask("letter confirmed", ALICE, return_value=True)
    for how in REFUSALS:
        ask(how, ALICE, **_refusing(how))

    assert answers == dict.fromkeys(answers, SAME_ANSWER)
    assert len(answers) == 6
    # Наружу — ничего, а в журнале сервера сбой есть, с `user_id`.
    assert desk.journal.lines == [
        _failure_line(desk.alice.id, **_error_fields(how)) for how in REFUSALS
    ]


@pytest.mark.parametrize("mail", REAL_SENDER)
def test_auth_request_answers_the_same_when_the_real_sender_did_not_send(desk, monkeypatch, mail):
    _real_sender_does_not_send(monkeypatch, mail)

    response = desk.client.post("/auth/request", json={"email": ALICE})

    assert (response.status_code, response.json()) == SAME_ANSWER
    assert desk.journal.lines == [_failure_line(desk.alice.id, **REAL_SENDER[mail])]


def test_only_the_public_endpoint_calls_the_function_that_hides_delivery():
    """`_send_login_link` отвечает «пользователь найден» — после неё нельзя
    говорить «отправлено», и зовёт её один `/auth/request`.

    `_deliver_login_link` отвечает строкой, а любая непустая строка истинна:
    `if _deliver_login_link(...)` значило бы «отправлено всегда». Новый вызов
    сравнивает исход с `LOGIN_LINK_SENT` явно — и вписывается сюда.
    """
    callers: dict[str, set[str]] = {"_send_login_link": set(), "_deliver_login_link": set()}
    for path in sorted((REPO / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                name = getattr(node, "id", None) or getattr(node, "attr", None)
                if name in callers:
                    callers[name].add(f"{path.name}:{function.name}")

    assert callers == {
        "_send_login_link": {"api.py:auth_request"},
        "_deliver_login_link": {
            "api.py:_send_login_link",
            "api.py:dash_recipients_create",
            "api.py:dash_recipients_send_login_link",
        },
    }


# ─── 3. Кнопка «Отправить ссылку» ────────────────────────────────────────────


def _press_send_link(desk, user_id: int, **sender):
    """Без `sender` работает настоящий `send_email`."""
    desk.journal.lines.clear()
    with contextlib.ExitStack() as stack:
        send = stack.enter_context(patch("src.notifier.send_email", **sender)) if sender else None
        response = desk.client.post(f"/api/v1/dash/recipients/{user_id}/send-login-link")
    return response, send


def _button_refusal_line(desk, user_id: int, outcome: str) -> dict:
    return {
        "event": "recipient_login_link_not_sent",
        "log_level": "warning",
        "id": user_id,
        "by_user_id": desk.admin.id,
        "outcome": outcome,
    }


def _assert_the_button_refused(response) -> None:
    # Ошибка запроса: «не отправлено» видит и клиент, который читает один статус.
    # Не 5xx: отказ почты — не сбой API, Sentry его не забирает.
    assert response.status_code == api.LOGIN_LINK_NOT_SENT_STATUS == 424, response.text
    answer = response.json()
    assert set(answer) == {"detail"}
    assert set(answer["detail"]) == {"code", "message"}
    assert answer["detail"]["code"] == api.LOGIN_LINK_NOT_SENT_CODE == "login_link_not_sent"
    _assert_no_person_or_text(answer)


@pytest.mark.parametrize("how", REFUSALS)
def test_send_link_button_does_not_answer_sent_when_the_letter_did_not_go(desk, how):
    response, send = _press_send_link(desk, desk.alice.id, **_refusing(how))

    _assert_the_button_refused(response)
    assert send.call_count == 1
    assert desk.journal.lines == [
        _failure_line(desk.alice.id, **_error_fields(how)),
        _button_refusal_line(desk, desk.alice.id, "not_sent"),
    ]
    _assert_no_person_or_text(desk.journal.lines)


def test_send_link_button_answers_ok_when_the_sender_confirmed(desk):
    response, send = _press_send_link(desk, desk.alice.id, return_value=True)

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "email": ALICE}
    assert send.call_count == 1
    assert send.call_args.kwargs["to"] == [ALICE]
    assert WELCOME not in send.call_args.kwargs["html_body"]  # ссылка, а не приглашение
    assert desk.journal.lines == [
        {
            "event": "recipient_login_link_sent",
            "log_level": "info",
            "id": desk.alice.id,
            "by_user_id": desk.admin.id,
        }
    ]


@pytest.mark.parametrize("mail", REAL_SENDER)
def test_send_link_button_when_the_real_sender_did_not_send(desk, monkeypatch, mail):
    _real_sender_does_not_send(monkeypatch, mail)

    response, _ = _press_send_link(desk, desk.alice.id)

    _assert_the_button_refused(response)
    assert desk.journal.lines == [
        _failure_line(desk.alice.id, **REAL_SENDER[mail]),
        _button_refusal_line(desk, desk.alice.id, "not_sent"),
    ]
    _assert_no_person_or_text(desk.journal.lines)


@pytest.mark.parametrize("why", ["address_not_lowercase", "deactivated_meanwhile"])
def test_send_link_button_for_a_user_the_lookup_does_not_find(desk, monkeypatch, why):
    """Адрес записан в базе не в нижнем регистре (заведён мимо приложения) или
    пользователя отключили, пока шёл запрос: письмо и не собиралось — это не
    «отправлено» и не отказ почты, и в журнале видно почему."""
    if why == "address_not_lowercase":
        carol = _user(desk.db, "Carol@Client.example", role="viewer")
    else:
        carol = desk.alice
        monkeypatch.setattr(tenants, "issue_magic_token", lambda *a, **kw: None)

    response, send = _press_send_link(desk, carol.id, return_value=True)

    # Не отказ почты: страница покажет общую ошибку, а не «проверьте адрес и почту».
    assert response.status_code == 409 != api.LOGIN_LINK_NOT_SENT_STATUS, response.text
    answer = response.json()
    assert set(answer) == {"detail"} and set(answer["detail"]) == {"code", "message"}
    assert answer["detail"]["code"] == api.LOGIN_LINK_NO_USER_CODE == "login_link_no_user"
    assert api.LOGIN_LINK_NO_USER_CODE != api.LOGIN_LINK_NOT_SENT_CODE
    assert answer["detail"]["message"] == (
        "No active user is found by this address: no link was issued."
    )
    _assert_no_person_or_text(answer)
    assert not send.called
    assert desk.journal.lines == [_button_refusal_line(desk, carol.id, "no_user")]


# ─── 4. Приглашение при создании пользователя ────────────────────────────────


def _add_user(desk, **sender):
    """Без `sender` работает настоящий `send_email`."""
    desk.journal.lines.clear()
    with contextlib.ExitStack() as stack:
        send = stack.enter_context(patch("src.notifier.send_email", **sender)) if sender else None
        response = desk.client.post(
            "/api/v1/dash/recipients", json={"email": BOB, "name": "Bob", "role": "viewer"}
        )
    assert response.status_code == 200, response.text  # пользователь создан при любой почте
    created = desk.db.scalar(select(storage.TenantUser).where(storage.TenantUser.email == BOB))
    assert created is not None and created.is_active
    assert response.json()["id"] == created.id
    return response.json(), created, send


def _created_line(desk, user_id: int, *, invite_sent: bool) -> dict:
    return {
        "event": "recipient_created",
        "log_level": "info",
        "id": user_id,
        "by_user_id": desk.admin.id,
        "invite_sent": invite_sent,
    }


@pytest.mark.parametrize("how", REFUSALS)
def test_new_user_is_created_but_the_invite_is_not_called_sent(desk, how):
    answer, created, send = _add_user(desk, **_refusing(how))

    assert answer["invite_sent"] is False
    assert send.call_count == 1
    assert desk.journal.lines == [
        _failure_line(created.id, **_error_fields(how)),
        _created_line(desk, created.id, invite_sent=False),
    ]
    _assert_no_person_or_text(desk.journal.lines)


@pytest.mark.parametrize("mail", REAL_SENDER)
def test_new_user_invite_is_not_called_sent_when_the_real_sender_did_not_send(
    desk, monkeypatch, mail
):
    _real_sender_does_not_send(monkeypatch, mail)

    answer, created, _ = _add_user(desk)

    assert answer["invite_sent"] is False
    assert desk.journal.lines == [
        _failure_line(created.id, **REAL_SENDER[mail]),
        _created_line(desk, created.id, invite_sent=False),
    ]
    _assert_no_person_or_text(desk.journal.lines)


def test_new_user_invite_is_called_sent_when_the_sender_confirmed(desk):
    answer, created, send = _add_user(desk, return_value=True)

    assert answer["invite_sent"] is True
    assert send.call_count == 1
    assert send.call_args.kwargs["to"] == [BOB]
    assert WELCOME in send.call_args.kwargs["html_body"]  # письмо-приглашение
    assert desk.journal.lines == [_created_line(desk, created.id, invite_sent=True)]
    # Исход приглашения — свойство ответа на создание, а не пользователя.
    listed = desk.client.get("/api/v1/dash/recipients").json()
    assert {row["email"] for row in listed} == {ADMIN, ALICE, BOB}
    assert all("invite_sent" not in row for row in listed)


# ─── 5. Что видит оператор ───────────────────────────────────────────────────
#
# Тестов фронта в CI нет: страница сверяется по тексту исходника — без строк
# комментариев, переносов и отступов. Это ловит правку, после которой
# «отправлена» покажется без подтверждения, но не заменяет взгляд на экран.


def _squeezed(path: Path) -> str:
    code = re.sub(r"(?m)^\s*//.*$", "", path.read_text(encoding="utf-8"))
    # Закомментированный JSX — только скобки, в которых нет ничего, кроме комментария.
    code = re.sub(r"\{\s*/\*(?:(?!\*/).)*\*/\s*\}", "", code, flags=re.DOTALL)
    return re.sub(r"\s+", " ", code)


def _says(page: str, phrase: str) -> int:
    """Сколько раз страница берёт фразу — в любых кавычках."""
    return len(re.findall(rf"""\bt\(\s*["'`]{phrase}["'`]""", page))


def test_dashboard_client_recognises_the_refusal_the_server_sends():
    client = _squeezed(REPO / "frontend" / "src" / "lib" / "api.ts")

    assert f'const LOGIN_LINK_NOT_SENT_CODE = "{api.LOGIN_LINK_NOT_SENT_CODE}";' in client
    # Отказ почты — только этот статус с этим кодом; ответ без кода им не считается.
    assert (
        "export function isLoginLinkNotSentError(err: unknown): boolean { "
        f"if (!(err instanceof ApiError) || err.status !== {api.LOGIN_LINK_NOT_SENT_STATUS}) "
        "return false; "
        "try { return JSON.parse(err.detail)?.detail?.code === LOGIN_LINK_NOT_SENT_CODE; } "
        "catch { return false; } }"
    ) in client
    # Ошибка запроса доходит до страницы как ошибка, а не как ответ.
    assert (
        "recipientSendLoginLink: (id: number) => "
        "request<{ ok: boolean; email: string }>( "
        "`/api/v1/dash/recipients/${id}/send-login-link`, "
        '{ method: "POST" }, ),'
    ) in client
    assert 'request<RecipientCreated>("/api/v1/dash/recipients", {' in client
    assert (
        "export interface RecipientCreated extends Recipient { invite_sent?: boolean; }" in client
    )


def test_users_page_reads_the_outcome_before_saying_sent():
    page = _squeezed(
        REPO
        / "frontend"
        / "src"
        / "app"
        / "[locale]"
        / "(dashboard)"
        / "settings"
        / "users"
        / "page.tsx"
    )

    # «Отправить ссылку»: «отправлена» сказано в одном месте и только на ответ 200.
    assert "mutationFn: () => api.recipientSendLoginLink(u.id), });" in page
    assert _says(page, "login_link_sent") == 1
    # Пока запрос идёт, кнопка не нажимается: второе нажатие погасило бы первую ссылку.
    assert "disabled={!u.is_active || sendLinkMut.isPending}" in page
    assert (
        "{sendLinkMut.isSuccess && ( "
        '<span className="text-[11px] text-success">{t("login_link_sent")}</span> )}'
    ) in page
    # Ошибка — красным; отказ почты — своей фразой.
    assert (
        "{sendLinkMut.isError && ( "
        '<span className="text-[11px] text-destructive"> '
        "{isLoginLinkNotSentError(sendLinkMut.error) "
        '? t("login_link_not_sent") : t("login_link_error")} </span> )}'
    ) in page
    # Создание: «ссылка отправлена» — только когда сервер ответил invite_sent: true.
    assert (
        "doneEmail={createMut.isSuccess ? createMut.data?.email : null} "
        "inviteSent={createMut.data?.invite_sent} />"
    ) in page
    for shown_when, (phrase, colour) in {
        "inviteSent === true": ("added", "text-success"),
        "inviteSent === false": ("added_invite_not_sent", "text-destructive"),
        "inviteSent === undefined": ("added_invite_unknown", "text-success"),
    }.items():
        said = re.findall(
            r'\{doneEmail && (inviteSent === \w+) && \( <span className="text-xs ([\w-]+)"> ?'
            rf'\{{t\("{phrase}", \{{ email: doneEmail \}}\)\}}',
            page,
        )
        assert said == [(shown_when, colour)], (phrase, said)
        assert _says(page, phrase) == 1, phrase


def test_users_page_has_every_phrase_in_every_language():
    for locale in ("ru", "az", "en"):
        copy = json.loads(
            (REPO / "frontend" / "messages" / f"{locale}.json").read_text(encoding="utf-8")
        )["users"]
        for phrase in ("added", "added_invite_not_sent", "added_invite_unknown"):
            assert re.findall(r"\{(\w+)\}", copy[phrase]) == ["email"], (locale, phrase)
        assert "{" not in copy["login_link_not_sent"], locale
        for phrase in ("added_invite_not_sent", "added_invite_unknown", "login_link_not_sent"):
            # Апостроф вплотную к скобке превращает подстановку в буквальный
            # текст, тег роняет форматтер.
            assert not re.search(r"'[{}<>]|[{}<>]'", copy[phrase]), (locale, phrase)
            assert "<" not in copy[phrase], (locale, phrase)
        # Оператору сказано, какой кнопкой отправить ссылку заново, — её же словами.
        assert f"«{copy['send_login_link']}»" in copy["added_invite_not_sent"].replace(
            "“", "«"
        ).replace("”", "»"), locale
        # У каждого исхода своя фраза; успех от неудачи отличается не одной галочкой.
        said = [
            copy[key]
            for key in ("login_link_sent", "login_link_not_sent", "login_link_error", "added")
        ]
        said += [copy["added_invite_not_sent"], copy["added_invite_unknown"]]
        assert len(set(said)) == len(said), locale


# ─── 6. Старый Streamlit-дашборд (`src/auth.py`) ─────────────────────────────


class _Streamlit:
    """Форма входа старого дашборда: адрес введён, «Запросить ссылку» нажата."""

    def __init__(self):
        self.session_state: dict = {}
        self.query_params: dict = {}
        self.said: list[tuple[str, str]] = []

    def markdown(self, *args, **kwargs):
        pass

    def columns(self, spec):
        return [contextlib.nullcontext() for _ in spec]

    def form(self, key):
        return contextlib.nullcontext()

    def expander(self, label):
        return contextlib.nullcontext()

    def text_input(self, label):
        return ALICE if label == "Email" else ""

    def form_submit_button(self, label, **kwargs):
        return "Запросить ссылку" in label

    def success(self, text):
        self.said.append(("success", text))

    def error(self, text):
        self.said.append(("error", text))


@pytest.fixture
def legacy_gate(desk, monkeypatch):
    monkeypatch.setenv("PHARMACY_AUTH_ENABLED", "true")
    monkeypatch.delenv("PHARMACY_AUTH_DEV_SHOW_TOKEN", raising=False)
    streamlit = _Streamlit()
    monkeypatch.setattr(auth, "st", streamlit)
    return streamlit


@pytest.mark.parametrize("how", REFUSALS)
def test_legacy_login_form_does_not_say_sent_when_the_letter_did_not_go(legacy_gate, how):
    with patch("src.notifier.send_email", **_refusing(how)) as send:
        assert auth.render_login_gate() is False

    assert send.call_count == 1
    assert [kind for kind, _ in legacy_gate.said] == ["error"]
    assert "отправлена" not in repr(legacy_gate.said)


def test_legacy_login_form_says_sent_when_the_sender_confirmed(legacy_gate):
    with patch("src.notifier.send_email", return_value=True) as send:
        assert auth.render_login_gate() is False

    assert send.call_args.kwargs["to"] == [ALICE]
    assert legacy_gate.said == [("success", "Ссылка отправлена на email!")]


def test_legacy_login_form_with_the_real_sender_without_smtp(legacy_gate):
    assert auth.render_login_gate() is False

    assert legacy_gate.said == [("error", "Письмо со ссылкой не ушло: отправка не подтверждена.")]
