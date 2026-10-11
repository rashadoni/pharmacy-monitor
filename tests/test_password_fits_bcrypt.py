"""Пароль, который bcrypt не возьмёт, получает отказ до того, как что-то записано.

bcrypt берёт от пароля 72 байта UTF-8. bcrypt 5 на более длинном бросает
`ValueError` — и когда пароль задают, и когда сверяют. До 2026-10-10 сервер
пропускал до 200 знаков и узнавал о пределе от самого bcrypt:

- `POST /auth/set-password` сначала гасил ссылку из письма, потом падал на
  хешировании: ответ 500, приглашение сгорело, пароля нет, повтор по той же
  ссылке — 401;
- смена пароля в дашборде и вход с таким паролем отвечали 500.

То же с одиночным суррогатом: в JSON такая строка допустима, в UTF-8 — нет.

Что здесь проверяется:

- предел считается в байтах: 72 латинских знака и 36 кириллических проходят,
  73 и 37 — нет; нижняя граница — в знаках;
- пробелы по краям в пароль не идут: вход их отбрасывает, и пароль, сохранённый
  с ними, не вошёл бы никогда;
- отказ приходит раньше токена: строка пользователя после него та же до
  последней колонки, и по той же ссылке пароль задаётся;
- в ответе с отказом нет присланного пароля, а причина названа кодом — по нему
  интерфейс берёт текст на своём языке;
- смена пароля и вход на такой строке отказывают, а не падают.

Что сбой после проверки токена ссылку не сжигает — `tests/test_magic_link_token.py`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from src import api as api_module
from src import rate_limit, storage, tenants
from src.storage import TenantUser

ADDRESS = "pharmacist@example.com"
PASSWORD = "a-long-enough-password"

# Ровно 72 байта — и ровно 6 знаков: границы правила, обе внутри.
FITS = [
    pytest.param("a" * 72, id="72-latin"),
    pytest.param("я" * 36, id="36-cyrillic"),
    pytest.param("ə" * 36, id="36-azeri"),
    pytest.param("😀" * 18, id="18-emoji"),
    pytest.param("abcdef", id="6-chars"),
    pytest.param("abc def", id="space-inside"),
]
# На байт или на знак за границей; суррогат — и в длинной строке, и в короткой.
REFUSED = [
    pytest.param("a" * 73, "password_too_long", id="73-latin"),
    pytest.param("я" * 37, "password_too_long", id="37-cyrillic"),
    pytest.param("ə" * 36 + "a", "password_too_long", id="36-azeri-and-one"),
    pytest.param("😀" * 18 + "a", "password_too_long", id="18-emoji-and-one"),
    pytest.param("a" * 201, "password_too_long", id="201-latin"),
    pytest.param("abcde", "password_too_short", id="5-chars"),
    # Пять знаков, десять байт: нижняя граница — в знаках, не в байтах.
    pytest.param("яяяяя", "password_too_short", id="5-cyrillic"),
    # Пробелы по краям не в счёт — остаётся пять знаков и ни одного.
    pytest.param(" abcde ", "password_too_short", id="5-chars-in-spaces"),
    pytest.param(" " * 6, "password_too_short", id="6-spaces"),
    # Нулевой байт bcrypt берёт за конец пароля: «abc\0abc» сверился бы с «abc».
    pytest.param("abc\x00abc", "password_not_text", id="nul-inside"),
    pytest.param("abcdef\ud800", "password_not_text", id="lone-surrogate"),
    pytest.param("\udc00", "password_not_text", id="lone-surrogate-alone"),
    pytest.param("a" * 80 + "\ud800", "password_not_text", id="lone-surrogate-and-long"),
]


@pytest.fixture
def client(monkeypatch, db_session) -> TestClient:
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    # Сессия запроса — как в `storage.make_session`: без autoflush.
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    # Счёт запросов — в памяти процесса и с нуля: в CI рядом стоит Redis, и
    # счётчик в нём переживал бы тест.
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: None)
    rate_limit._reset_memory_for_tests()
    return TestClient(api_module.app)


def _add_user(session: Session, **fields) -> TenantUser:
    tenant = tenants.get_or_create_default(session)
    user = TenantUser(tenant_id=tenant.id, email=ADDRESS, is_active=True, **fields)
    session.add(user)
    session.commit()
    return user


def _row(session: Session, user: TenantUser) -> dict:
    """Строка пользователя, как она лежит в базе, — мимо модели."""
    return dict(
        session.execute(text("SELECT * FROM tenant_users WHERE id = :id"), {"id": user.id})
        .mappings()
        .one()
    )


def _post(client: TestClient, path: str, **body: str) -> Response:
    """Тело — как его шлёт браузер: одиночный суррогат уходит записью `\\udXXX`."""
    return client.post(
        path,
        content=json.dumps(body).encode("ascii"),
        headers={"content-type": "application/json"},
    )


def _assert_refused_for(response: Response, reason: str, password: str) -> None:
    assert response.status_code == 422, response.text
    (problem,) = response.json()["detail"]
    assert problem["type"] == reason
    assert problem["loc"] == ["body", "new_password"]
    assert problem["msg"] == api_module._PASSWORD_PROBLEMS[reason]
    # Присланное в ответ не возвращается: ни полем, ни внутри текста — ни как
    # есть, ни записью `\uXXXX`.
    assert set(problem) == {"type", "loc", "msg"}
    assert json.dumps(password)[1:-1] not in response.text
    assert json.dumps(password, ensure_ascii=False)[1:-1] not in response.text


def _sign_in(client: TestClient, session: Session) -> TenantUser:
    user = _add_user(session, password_hash=api_module._hash_bcrypt(PASSWORD))
    client.cookies.set(
        api_module.COOKIE_NAME, api_module._make_jwt(user.id, user.tenant_id, user.email)
    )
    return user


# ─── Само правило ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("password", FITS)
def test_a_password_bcrypt_takes_is_kept_as_typed(password):
    assert api_module._usable_new_password(password) == password
    assert api_module._verify_bcrypt(password, api_module._hash_bcrypt(password))


@pytest.mark.parametrize(("password", "reason"), REFUSED)
def test_a_password_that_cannot_be_set_is_named(password, reason):
    with pytest.raises(api_module.HTTPException) as refused:
        api_module._usable_new_password(password)

    assert refused.value.status_code == 422
    assert [problem["type"] for problem in refused.value.detail] == [reason]


def test_spaces_at_the_edges_do_not_count_towards_the_limit():
    """72 байта в пробелах — это 72 байта: отбрасывается раньше, чем считается."""
    assert api_module._usable_new_password(" " * 10 + "a" * 72 + "\n") == "a" * 72


def test_the_limit_is_the_one_bcrypt_enforces():
    """Правило не строже и не мягче библиотеки: на байт больше она уже бросает."""
    import bcrypt

    bcrypt.hashpw(b"a" * api_module.PASSWORD_MAX_BYTES, bcrypt.gensalt(4))
    with pytest.raises(ValueError):
        bcrypt.hashpw(b"a" * (api_module.PASSWORD_MAX_BYTES + 1), bcrypt.gensalt(4))


# ─── Создание пароля по ссылке ───────────────────────────────────────────────


@pytest.mark.parametrize("password", FITS)
def test_an_invitation_sets_a_password_that_fits_and_it_signs_in(client, db_session, password):
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)

    created = _post(client, "/auth/set-password", token=token, new_password=password)

    assert created.status_code == 200, created.text
    assert created.json()["user_id"] == user.id
    client.cookies.clear()
    signed_in = _post(client, "/auth/login", login=ADDRESS, password=password)
    assert signed_in.status_code == 200, signed_in.text


@pytest.mark.parametrize(("password", "reason"), REFUSED)
def test_a_password_that_cannot_be_set_leaves_the_invitation_alive(
    client, db_session, password, reason
):
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    before = _row(db_session, user)

    refused = _post(client, "/auth/set-password", token=token, new_password=password)

    _assert_refused_for(refused, reason, password)
    # Ни токен, ни срок, ни отметка входа, ни пароль не тронуты.
    assert _row(db_session, user) == before
    assert before["magic_token"] is not None and before["last_login_at"] is None
    # И по той же ссылке пароль задаётся.
    created = _post(client, "/auth/set-password", token=token, new_password=PASSWORD)
    assert created.status_code == 200, created.text
    db_session.refresh(user)
    assert api_module._verify_bcrypt(PASSWORD, user.password_hash)


def test_the_password_is_judged_before_the_token(client, db_session):
    """С негодной ссылкой и негодным паролем отказ — про пароль: до токена не дошло."""
    _add_user(db_session)

    refused = _post(client, "/auth/set-password", token="not-a-token", new_password="a" * 73)

    _assert_refused_for(refused, "password_too_long", "a" * 73)


def test_spaces_at_the_edges_are_dropped_as_signing_in_drops_them(client, db_session):
    """Вход отбрасывает пробелы по краям. Сохранённый с ними пароль не вошёл бы."""
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)

    created = _post(client, "/auth/set-password", token=token, new_password="  pass word\t")

    assert created.status_code == 200, created.text
    db_session.refresh(user)
    # Пробел внутри — часть пароля, по краям — нет.
    assert api_module._verify_bcrypt("pass word", user.password_hash)
    assert not api_module._verify_bcrypt("  pass word\t", user.password_hash)
    for typed in ("pass word", "  pass word\t", "pass word "):
        client.cookies.clear()
        signed_in = _post(client, "/auth/login", login=ADDRESS, password=typed)
        assert signed_in.status_code == 200, (typed, signed_in.text)


# ─── Смена пароля в дашборде ─────────────────────────────────────────────────


@pytest.mark.parametrize("password", FITS)
def test_the_dashboard_sets_a_new_password_that_fits(client, db_session, password):
    user = _sign_in(client, db_session)

    changed = _post(
        client, "/api/v1/dash/me/password", current_password=PASSWORD, new_password=password
    )

    assert changed.status_code == 200, changed.text
    db_session.refresh(user)
    assert api_module._verify_bcrypt(password, user.password_hash)


@pytest.mark.parametrize(("password", "reason"), REFUSED)
def test_the_dashboard_refuses_a_new_password_that_cannot_be_set_and_keeps_the_old_one(
    client, db_session, password, reason
):
    user = _sign_in(client, db_session)
    before = _row(db_session, user)

    refused = _post(
        client, "/api/v1/dash/me/password", current_password=PASSWORD, new_password=password
    )

    _assert_refused_for(refused, reason, password)
    assert _row(db_session, user) == before
    assert api_module._verify_bcrypt(PASSWORD, before["password_hash"])


def test_the_dashboard_drops_spaces_at_the_edges_of_a_new_password(client, db_session):
    user = _sign_in(client, db_session)

    changed = _post(
        client, "/api/v1/dash/me/password", current_password=PASSWORD, new_password=" new pass-1 "
    )

    assert changed.status_code == 200, changed.text
    db_session.refresh(user)
    assert api_module._verify_bcrypt("new pass-1", user.password_hash)
    client.cookies.clear()
    signed_in = _post(client, "/auth/login", login=ADDRESS, password="new pass-1")
    assert signed_in.status_code == 200, signed_in.text


@pytest.mark.parametrize("current", [PASSWORD, f" {PASSWORD} "], ids=["as-stored", "in-spaces"])
def test_the_dashboard_does_not_take_the_current_password_in_spaces_for_a_new_one(
    client, db_session, current
):
    user = _sign_in(client, db_session)
    before = _row(db_session, user)

    refused = _post(
        client, "/api/v1/dash/me/password", current_password=current, new_password=f" {PASSWORD} "
    )

    assert refused.status_code == 400, refused.text
    assert _row(db_session, user) == before


def test_the_dashboard_takes_the_current_password_the_way_signing_in_takes_it(client, db_session):
    """Набранное с пробелами по краям входит — значит, годится и как текущий пароль."""
    user = _sign_in(client, db_session)

    changed = _post(
        client,
        "/api/v1/dash/me/password",
        current_password=f"  {PASSWORD}\t",
        new_password="new-password-1",
    )

    assert changed.status_code == 200, changed.text
    db_session.refresh(user)
    assert api_module._verify_bcrypt("new-password-1", user.password_hash)


def test_a_password_stored_in_spaces_by_the_old_code_can_be_changed_to_the_same_without_them(
    client, db_session
):
    """Прежний код сохранял пароль как набран, а вход пробелы отбрасывал: войти
    таким нельзя. Сменить его можно, только набрав текущий как есть."""
    stored = " legacy-pass-1 "
    user = _add_user(db_session, password_hash=api_module._hash_bcrypt(stored))
    client.cookies.set(
        api_module.COOKIE_NAME, api_module._make_jwt(user.id, user.tenant_id, user.email)
    )
    assert _post(client, "/auth/login", login=ADDRESS, password=stored).status_code == 401

    changed = _post(
        client, "/api/v1/dash/me/password", current_password=stored, new_password=stored
    )

    assert changed.status_code == 200, changed.text
    client.cookies.clear()
    assert _post(client, "/auth/login", login=ADDRESS, password=stored).status_code == 200


@pytest.mark.parametrize("current", ["a" * 73, "я" * 37, "abcdef\ud800"])
def test_a_current_password_that_cannot_exist_is_a_wrong_password(client, db_session, current):
    user = _sign_in(client, db_session)
    before = _row(db_session, user)

    refused = _post(
        client, "/api/v1/dash/me/password", current_password=current, new_password="new-password-1"
    )

    assert refused.status_code == 401, refused.text
    assert _row(db_session, user) == before


# ─── Вход ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("password", ["a" * 73, "я" * 37, "abcdef\ud800"])
def test_signing_in_with_a_password_that_cannot_exist_is_a_refusal(client, db_session, password):
    _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))

    refused = _post(client, "/auth/login", login=ADDRESS, password=password)

    assert refused.status_code == 401, refused.text
    assert api_module.COOKIE_NAME not in refused.cookies


@pytest.mark.parametrize(
    "stored", [pytest.param("abc12", id="5-chars"), pytest.param("abc\x00abcd", id="nul-inside")]
)
def test_a_password_stored_before_the_rule_still_signs_in(client, db_session, stored):
    """Правило — для нового пароля. Сверка берёт всё, что берёт bcrypt: хеш мог
    прийти из `ADMIN_PASSWORD_HASH` или от кода, который правила не знал."""
    _add_user(db_session, password_hash=api_module._hash_bcrypt(stored))

    signed_in = _post(client, "/auth/login", login=ADDRESS, password=stored)

    assert signed_in.status_code == 200, signed_in.text


def test_a_longer_password_does_not_match_by_its_first_72_bytes(client, db_session):
    """Версии bcrypt до пятой отбрасывали хвост — и такой пароль вошёл бы."""
    stored = api_module._hash_bcrypt("a" * 72)
    _add_user(db_session, password_hash=stored)

    assert api_module._verify_bcrypt("a" * 72, stored)
    assert not api_module._verify_bcrypt("a" * 72 + "b", stored)
    refused = _post(client, "/auth/login", login=ADDRESS, password="a" * 72 + "b")
    assert refused.status_code == 401, refused.text
