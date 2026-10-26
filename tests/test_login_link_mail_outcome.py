"""Админ видит, ушло ли письмо со ссылкой на вход; посторонний — нет.

2026-10-26 владелец нажал «Отправить ссылку» на странице «Пользователи», увидел
«Ссылка отправлена ✓», а письма не было. В тот раз сервер письмо принял, и
галочка была правдой, — но рисовалась она при любом исходе: и когда почтовый
сервер отказал, и когда `SMTP_HOST` пуст. Теперь:

- повторная отправка админом отвечает 200, только если почтовый сервер письмо
  принял; 503 — почта на сервере не настроена; 502 — сервер отказал или не
  ответил. Причина — классом ошибки и кодами, без адреса;
- создание пользователя проходит при любом исходе письма (fail-open), а исход
  приглашения лежит в ответе;
- публичный `POST /auth/request` отвечает одинаково при любом исходе — и для
  адреса, которого нет: иначе по ответу перебирали бы адреса.
"""

from __future__ import annotations

import smtplib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from structlog.testing import capture_logs

from src import api as api_module
from src import notifier, rate_limit, storage
from src.storage import TenantUser
from tests.test_magic_link_token import ADDRESS, _add_user, _sign_in_as_admin

REPLY = f"5.1.1 <{ADDRESS}>: Recipient address rejected"


@pytest.fixture
def client(monkeypatch, db_session) -> TestClient:
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", "https://dashboard.example")
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: None)
    rate_limit._reset_memory_for_tests()
    return TestClient(api_module.app)


# Подменяется разговор с сервером, а не `send_email`: обёртку, которая решает,
# что из этого отказ, тест проходит настоящую.
def _accepted(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(notifier, "_send_email", lambda *args, **kwargs: True)


def _no_smtp_host(monkeypatch):
    # Настоящий `send_email`: без `SMTP_HOST` он не бросает, а возвращает False.
    monkeypatch.delenv("SMTP_HOST", raising=False)


def _raising(monkeypatch, exc: BaseException):
    """Разговор с сервером кончается `exc`."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")

    def talk(*args, **kwargs):
        raise exc

    monkeypatch.setattr(notifier, "_send_email", talk)


def _refused(monkeypatch):
    _raising(monkeypatch, smtplib.SMTPRecipientsRefused({ADDRESS: (550, REPLY.encode())}))


def _unreachable(monkeypatch):
    _raising(monkeypatch, ConnectionRefusedError(111, "Connection refused"))


def _disconnected(monkeypatch):
    _raising(monkeypatch, smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))


# ─── Повторная отправка админом ──────────────────────────────────────────────


def test_resend_says_the_mail_server_accepted_the_letter(client, db_session, monkeypatch):
    _sign_in_as_admin(client, db_session)
    user = _add_user(db_session)
    _accepted(monkeypatch)

    r = client.post(f"/api/v1/dash/recipients/{user.id}/send-login-link")

    assert r.status_code == 200, r.text
    assert r.json()["mail"]["sent"] is True
    assert r.json()["mail"]["reason"] is None


def test_resend_without_smtp_host_is_503_not_a_check_mark(client, db_session, monkeypatch):
    _sign_in_as_admin(client, db_session)
    user = _add_user(db_session)
    _no_smtp_host(monkeypatch)

    with capture_logs() as logs:
        r = client.post(f"/api/v1/dash/recipients/{user.id}/send-login-link")

    assert r.status_code == 503, r.text
    assert r.json()["detail"] == {
        "sent": False,
        "reason": "smtp_not_configured",
        "error_type": None,
        "smtp_code": None,
        "smtp_status": None,
        "errno": None,
    }
    failed = [e for e in logs if e["event"] == "login_link_email_failed"]
    assert failed == [
        {
            "event": "login_link_email_failed",
            "log_level": "warning",
            "user_id": user.id,
            "reason": "smtp_not_configured",
        }
    ]


@pytest.mark.parametrize(
    ("fail", "reason", "fields"),
    [
        (
            _refused,
            "smtp_refused",
            {"error_type": "SMTPRecipientsRefused", "smtp_code": 550, "smtp_status": "5.1.1"},
        ),
        (_unreachable, "send_failed", {"error_type": "ConnectionRefusedError", "errno": 111}),
        # Обрыв приходит из smtplib, но ответа с кодом у него нет — это не отказ.
        (_disconnected, "send_failed", {"error_type": "SMTPServerDisconnected"}),
    ],
)
def test_resend_failure_is_502_with_the_cause_and_no_address(
    client, db_session, monkeypatch, fail, reason, fields
):
    admin = _sign_in_as_admin(client, db_session)
    user = _add_user(db_session)
    fail(monkeypatch)

    with capture_logs() as logs:
        r = client.post(f"/api/v1/dash/recipients/{user.id}/send-login-link")

    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert detail["sent"] is False and detail["reason"] == reason
    assert {k: v for k, v in detail.items() if k not in ("sent", "reason") and v is not None} == (
        fields
    )
    # Ни в ответе, ни в журнале нет ни адреса, ни ответа сервера.
    assert "@" not in r.text and "rejected" not in r.text
    assert "@" not in repr(logs)
    (failed,) = [e for e in logs if e["event"] == "login_link_email_failed"]
    assert failed["user_id"] == user.id and failed["reason"] == reason
    # Кто нажал — журнал аудита неудачные ответы не пишет.
    (pressed,) = [e for e in logs if e["event"] == "recipient_login_link_not_sent"]
    assert pressed["id"] == user.id and pressed["by_user_id"] == admin.id
    assert "recipient_login_link_sent" not in {e["event"] for e in logs}


def test_resend_failure_still_replaces_the_previous_link(client, db_session, monkeypatch):
    """Токен выпускается до письма: прежняя ссылка гаснет и при сбое почты."""
    _sign_in_as_admin(client, db_session)
    user = _add_user(db_session)
    _accepted(monkeypatch)
    assert client.post(f"/api/v1/dash/recipients/{user.id}/send-login-link").status_code == 200
    db_session.refresh(user)
    first = user.magic_token_hash
    _refused(monkeypatch)

    assert client.post(f"/api/v1/dash/recipients/{user.id}/send-login-link").status_code == 502

    db_session.refresh(user)
    assert user.magic_token_hash not in (None, first)


def test_resend_says_so_when_no_link_was_issued(client, db_session, monkeypatch):
    """Строка есть, а активного пользователя по её адресу нет — ссылки нет.

    `_send_login_link` ищет адрес в нижнем регистре; адрес, записанный иначе
    (так его кладёт не только эта страница), раньше давал «ok» без письма.
    """
    _sign_in_as_admin(client, db_session)
    user = _add_user(db_session, ADDRESS.upper())
    _accepted(monkeypatch)

    r = client.post(f"/api/v1/dash/recipients/{user.id}/send-login-link")

    assert r.status_code == 409, r.text
    db_session.refresh(user)
    assert user.magic_token_hash is None


# ─── Создание пользователя ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mail", "sent", "reason"),
    [
        (_accepted, True, None),
        (_no_smtp_host, False, "smtp_not_configured"),
        (_refused, False, "smtp_refused"),
        (_unreachable, False, "send_failed"),
    ],
)
def test_user_is_created_whatever_happens_to_the_invitation(
    client, db_session, monkeypatch, mail, sent, reason
):
    _sign_in_as_admin(client, db_session)
    mail(monkeypatch)

    r = client.post("/api/v1/dash/recipients", json={"email": ADDRESS, "role": "viewer"})

    assert r.status_code == 200, r.text
    assert r.json()["email"] == ADDRESS
    assert r.json()["invite_mail"]["sent"] is sent
    assert r.json()["invite_mail"]["reason"] == reason
    assert db_session.scalar(select(TenantUser).where(TenantUser.email == ADDRESS)) is not None


# ─── Публичный запрос ссылки ─────────────────────────────────────────────────


@pytest.mark.parametrize("mail", [_accepted, _no_smtp_host, _refused, _unreachable])
def test_public_request_answers_the_same_whatever_happens_to_the_letter(
    client, db_session, monkeypatch, mail
):
    _add_user(db_session)
    mail(monkeypatch)

    unknown = client.post("/auth/request", json={"email": "nobody@example.org"})
    known = client.post("/auth/request", json={"email": ADDRESS})

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    # Путь действительно пройден: токен выпущен, письмо пробовали отправить.
    user = db_session.scalar(select(TenantUser).where(TenantUser.email == ADDRESS))
    db_session.refresh(user)
    assert user.magic_token_hash is not None
