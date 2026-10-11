"""Одноразовый токен входа лежит в базе хешем: сам токен сервер не хранит.

Токен из ссылки — полноценный вход: `/auth/verify` ставит по нему сессию,
`/auth/set-password` задаёт пароль. До 2026-10-10 он лежал в
`tenant_users.magic_token` открытым текстом, и вошёл бы любой, кто прочитал базу
или дамп, снятый, пока токен жив.

Что здесь проверяется:

- токена нет ни в строке пользователя, ни в одном запросе к базе (в любую
  таблицу), ни в журнале приложения, ни в ответе того, кто ссылку выдал;
- токен из письма входит один раз и полчаса; новая ссылка гасит прежнюю;
- содержимое колонки токеном не служит — ни хеш, ни токен, записанный открытым
  текстом прежним кодом;
- так на каждом пути выдачи: запрос ссылки, приглашение, повторная отправка
  админом, команда `tenant issue-token`;
- токен гаснет вместе с тем, ради чего предъявлен: сбой после проверки (bcrypt,
  сессия) ссылку не сжигает — до 2026-10-10 она сгорала, а пароля не было.

Что из двух одновременных запросов с одним токеном проходит один, SQLite не
покажет — `tests/test_magic_link_redeem_postgres.py`.

Чего здесь нет: журнала прокси. Caddy пишет адрес запроса вместе с `?token=…`,
и это тест не видит — docs/RUNBOOK.md, «Ссылка из письма не входит».
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import structlog
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from src import api as api_module
from src import main, notifier, rate_limit, storage, tenants
from src._time import utcnow
from src.storage import TenantUser

ADDRESS = "pharmacist@example.com"
PASSWORD = "a-long-enough-password"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _add_user(session: Session, address: str = ADDRESS, **fields) -> TenantUser:
    tenant = tenants.get_or_create_default(session)
    user = TenantUser(tenant_id=tenant.id, email=address, **{"is_active": True, **fields})
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


def _assert_only_the_hash_is_stored(
    session: Session, user: TenantUser, token: str, *, minutes: int = 30
) -> None:
    """В строке — хеш токена и срок, который назвали при выдаче."""
    row = _row(session, user)
    assert row["magic_token"] == _sha256(token)
    assert token not in repr(row)
    session.refresh(user)
    # С запасом в минуту в обе стороны: часы машины могут шагнуть и назад.
    left = user.magic_token_expires_at - utcnow()
    assert timedelta(minutes=minutes - 1) < left < timedelta(minutes=minutes + 1)


@pytest.fixture
def written(db_session, caplog, capsys) -> Iterator[Callable[[], list[str]]]:
    """Всё, что сервер записал за тест: запросы к базе с параметрами, журнал, печать.

    Параметры запроса — тоже запись: база кладёт их в текст своей ошибки.
    """
    queries: list[str] = []
    printed: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        queries.append(f"{statement} {parameters!r}")

    def everything() -> list[str]:
        printed.extend(capsys.readouterr())
        # Обычный `logging` — только свой: адрес запроса пишет в журнал и сам
        # тестовый клиент, а это не сервер.
        logged = [
            record.getMessage()
            for record in caplog.records
            if record.name == "root" or record.name.split(".")[0] == "src"
        ]
        return [*queries, *(repr(entry) for entry in entries), *logged, *printed]

    caplog.set_level(logging.DEBUG)
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        with capture_logs() as entries:
            yield everything
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _assert_written_nowhere(written: Callable[[], list[str]], token: str) -> None:
    lines = written()
    # Запись хеша на месте — иначе вторая проверка ничего бы не значила.
    assert [line for line in lines if _sha256(token) in line], lines
    assert not [line for line in lines if token in line]


# ─── Выдача и проверка ───────────────────────────────────────────────────────


def test_the_row_holds_the_hash_of_the_token_not_the_token(db_session):
    user = _add_user(db_session)

    token = tenants.issue_magic_token(db_session, ADDRESS)

    assert token
    _assert_only_the_hash_is_stored(db_session, user, token)


def test_the_token_is_in_no_query_and_no_log_line(db_session, written):
    _add_user(db_session)

    token = tenants.issue_magic_token(db_session, ADDRESS)
    assert tenants.verify_magic_token(db_session, token) is not None
    assert tenants.verify_magic_token(db_session, token) is None

    _assert_written_nowhere(written, token)
    # Хеш ушёл в базу трижды: запись и две попытки гашения.
    assert sum(_sha256(token) in line for line in written()) >= 3


def test_every_link_carries_its_own_long_random_token(db_session):
    _add_user(db_session)
    _add_user(db_session, "second@example.com")

    tokens = [
        tenants.issue_magic_token(db_session, ADDRESS),
        tenants.issue_magic_token(db_session, "second@example.com"),
        tenants.issue_magic_token(db_session, ADDRESS),
    ]

    assert len(set(tokens)) == 3
    assert all(len(token) >= 43 for token in tokens)  # 32 случайных байта


def test_no_link_is_issued_to_a_switched_off_account(db_session):
    user = _add_user(db_session, is_active=False)

    assert tenants.issue_magic_token(db_session, ADDRESS) is None
    assert tenants.issue_magic_token(db_session, ADDRESS, ttl_minutes=5) is None
    assert _row(db_session, user)["magic_token"] is None


def test_the_token_signs_in_once(db_session):
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)

    assert tenants.verify_magic_token(db_session, token).id == user.id
    assert tenants.verify_magic_token(db_session, token) is None
    row = _row(db_session, user)
    assert row["magic_token"] is None and row["magic_token_expires_at"] is None


def test_a_new_link_spends_the_previous_one(db_session):
    user = _add_user(db_session)
    earlier = tenants.issue_magic_token(db_session, ADDRESS)
    later = tenants.issue_magic_token(db_session, ADDRESS)

    assert tenants.verify_magic_token(db_session, earlier) is None
    assert tenants.verify_magic_token(db_session, later).id == user.id


@pytest.mark.parametrize(("minutes_later", "signs_in"), [(29, True), (31, False)])
def test_the_token_lives_half_an_hour(db_session, monkeypatch, minutes_later, signs_in):
    _add_user(db_session)
    issued_at = utcnow()
    monkeypatch.setattr(tenants, "utcnow", lambda: issued_at)
    token = tenants.issue_magic_token(db_session, ADDRESS)

    monkeypatch.setattr(tenants, "utcnow", lambda: issued_at + timedelta(minutes=minutes_later))

    assert (tenants.verify_magic_token(db_session, token) is not None) is signs_in


@pytest.mark.parametrize(("seconds_past_the_term", "signs_in"), [(0, True), (1, False)])
def test_the_term_ends_to_the_second(db_session, monkeypatch, seconds_past_the_term, signs_in):
    """Срок сверяет сама база, в условии гашения: ни минуты сверх названного."""
    _add_user(db_session)
    issued_at = utcnow()
    monkeypatch.setattr(tenants, "utcnow", lambda: issued_at)
    token = tenants.issue_magic_token(db_session, ADDRESS)

    now = issued_at + timedelta(minutes=30, seconds=seconds_past_the_term)
    monkeypatch.setattr(tenants, "utcnow", lambda: now)

    assert (tenants.verify_magic_token(db_session, token) is not None) is signs_in


def test_a_token_without_a_term_does_not_sign_in(db_session):
    """Срок ставит выдача; строка без срока — ручная правка базы, а не вечный вход."""
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    user.magic_token_expires_at = None
    db_session.commit()

    assert tenants.verify_magic_token(db_session, token) is None


def test_what_the_column_holds_does_not_sign_in(db_session):
    """Кто прочитал базу, получил хеш. Хеш — не вход."""
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    stored = _row(db_session, user)["magic_token"]

    assert tenants.verify_magic_token(db_session, stored) is None
    # Отказ — из-за того, что прислали, а не из-за сломанного поиска: настоящий
    # токен после него входит.
    assert tenants.verify_magic_token(db_session, token).id == user.id


def test_a_token_stored_in_the_clear_by_the_old_code_does_not_sign_in(db_session):
    """Цена того, что колонка осталась прежней: выданное до выкладки не действует.

    Такая строка на проде есть (2026-10-10 — одна, просрочена): прежний код
    не стирал токен, по которому не вошли.
    """
    in_the_clear = "kJ3vQ1x0Zt8mB5nR7cW2yH4dL6fS9aG0pT1uE3iO5qA"
    user = _add_user(db_session)
    user.magic_token_hash = in_the_clear
    user.magic_token_expires_at = utcnow() + timedelta(minutes=30)
    db_session.commit()

    assert tenants.verify_magic_token(db_session, in_the_clear) is None


# ─── Гашение: условие в самом запросе, запись — за вызывающим ────────────────


def test_a_live_token_of_a_switched_off_account_does_not_sign_in(db_session):
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    user.is_active = False
    db_session.commit()
    before = _row(db_session, user)

    assert tenants.verify_magic_token(db_session, token) is None
    assert _row(db_session, user) == before


def test_the_user_handed_back_is_the_row_as_it_is_now(db_session):
    """Сессии проекта не сбрасывают объекты ни перед запросом, ни на commit.

    Пользователь уже загружен в сессию, а его строку меняет другая: вернуться
    должен он же — с нынешним паролем и без токена. По устаревшему паролю
    `/auth/set-password` решил бы, что пароля ещё нет.
    """
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    with sessionmaker(db_session.get_bind(), autoflush=False)() as elsewhere:
        elsewhere.get(TenantUser, user.id).password_hash = "set-by-another-session"
        elsewhere.commit()
    assert user.password_hash is None and user.magic_token_hash == _sha256(token)

    handed_back = tenants.verify_magic_token(db_session, token)

    assert handed_back is user
    assert user.password_hash == "set-by-another-session"
    assert user.magic_token_hash is None and user.magic_token_expires_at is None
    assert user.last_login_at is not None


def test_redeeming_is_not_final_until_the_caller_commits(db_session):
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    before = _row(db_session, user)

    assert tenants.redeem_magic_token(db_session, token).id == user.id
    assert _row(db_session, user)["magic_token"] is None
    db_session.rollback()

    assert _row(db_session, user) == before
    # Вторым заходом — уже с commit: ссылка та же, и теперь она погашена.
    assert tenants.redeem_magic_token(db_session, token).id == user.id
    db_session.commit()
    db_session.rollback()
    assert _row(db_session, user)["magic_token"] is None
    assert tenants.redeem_magic_token(db_session, token) is None


# ─── Пути выдачи: токен уходит письмом, в базе — хеш ─────────────────────────


@pytest.fixture
def client(monkeypatch, db_session) -> TestClient:
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    # Адрес в ссылке — свой, а не из окружения того, кто запустил тесты.
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", "https://dashboard.example")
    # Счёт запросов — в памяти процесса и с нуля: в CI рядом стоит Redis, и
    # счётчик в нём переживал бы тест.
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: None)
    rate_limit._reset_memory_for_tests()
    return TestClient(api_module.app)


@pytest.fixture
def letters(monkeypatch) -> list[str]:
    """Тела писем, которые ушли бы получателям."""
    sent: list[str] = []
    monkeypatch.setattr(
        notifier, "send_email", lambda *, subject, html_body, to: sent.append(html_body)
    )
    return sent


def _link_in(letter: str) -> tuple[str, str]:
    """Куда ведёт ссылка из письма и какой токен она несёт."""
    (address,) = set(re.findall(r"href='([^']+)'", letter))
    link = urlsplit(address)
    assert f"{link.scheme}://{link.netloc}" == "https://dashboard.example"
    (token,) = parse_qs(link.query)["token"]
    return link.path, token


def _sign_in_as_admin(client: TestClient, session: Session) -> TenantUser:
    admin = _add_user(session, "owner@example.com", role="admin")
    client.cookies.set(
        api_module.COOKIE_NAME, api_module._make_jwt(admin.id, admin.tenant_id, admin.email)
    )
    return admin


def test_a_requested_login_link_signs_in_once(client, letters, db_session, written):
    user = _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))

    requested = client.post("/auth/request", json={"email": ADDRESS})

    assert requested.status_code == 200
    path, token = _link_in(letters[0])
    assert path == "/auth/verify"
    assert token not in requested.text
    _assert_only_the_hash_is_stored(db_session, user, token)
    first = client.get(path, params={"token": token})
    assert first.status_code == 200 and first.json()["user_id"] == user.id
    assert api_module.COOKIE_NAME in first.cookies
    assert client.get(path, params={"token": token}).status_code == 401
    _assert_written_nowhere(written, token)


def test_an_invitation_sets_a_password_once(client, letters, db_session, written):
    _sign_in_as_admin(client, db_session)

    created = client.post("/api/v1/dash/recipients", json={"email": ADDRESS, "role": "viewer"})

    assert created.status_code == 200, created.text
    user = db_session.get(TenantUser, created.json()["id"])
    path, token = _link_in(letters[0])
    assert path == "/set-password"
    assert token not in created.text
    _assert_only_the_hash_is_stored(db_session, user, token)
    client.cookies.clear()
    first = client.post("/auth/set-password", json={"token": token, "new_password": PASSWORD})
    assert first.status_code == 200 and first.json()["user_id"] == user.id
    again = client.post("/auth/set-password", json={"token": token, "new_password": "another-1"})
    assert again.status_code == 401
    db_session.refresh(user)
    assert api_module._verify_bcrypt(PASSWORD, user.password_hash)
    _assert_written_nowhere(written, token)


def test_a_link_resent_by_an_admin_signs_in_once(client, letters, db_session, written):
    _sign_in_as_admin(client, db_session)
    user = _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))

    resent = client.post(f"/api/v1/dash/recipients/{user.id}/send-login-link")

    assert resent.status_code == 200, resent.text
    path, token = _link_in(letters[0])
    assert token not in resent.text
    _assert_only_the_hash_is_stored(db_session, user, token)
    client.cookies.clear()
    assert client.get(path, params={"token": token}).status_code == 200
    assert client.get(path, params={"token": token}).status_code == 401
    _assert_written_nowhere(written, token)


def test_what_the_column_holds_opens_neither_door(client, db_session):
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    stored = _row(db_session, user)["magic_token"]

    assert client.get("/auth/verify", params={"token": stored}).status_code == 401
    refused = client.post("/auth/set-password", json={"token": stored, "new_password": PASSWORD})
    assert refused.status_code == 401
    db_session.refresh(user)
    assert user.password_hash is None
    assert client.get("/auth/verify", params={"token": token}).status_code == 200


def test_a_token_that_is_not_text_is_refused_not_a_server_error(client, db_session):
    """Одиночный суррогат в JSON — строка, которую UTF-8 не кодирует."""
    _add_user(db_session)
    tenants.issue_magic_token(db_session, ADDRESS)

    refused = client.post(
        "/auth/set-password",
        content=b'{"token": "\\ud800", "new_password": "%s"}' % PASSWORD.encode(),
        headers={"content-type": "application/json"},
    )

    assert refused.status_code == 401


# ─── Сбой после проверки токена ссылку не сжигает ────────────────────────────


def _broken(*args, **kwargs):
    raise RuntimeError("сломано нарочно")


@pytest.mark.parametrize("broken", ["_hash_bcrypt", "_make_jwt"])
def test_a_failure_while_setting_the_password_does_not_spend_the_invitation(
    client, db_session, monkeypatch, broken
):
    """Раньше токен гас отдельным commit до хеширования: ссылка сгорала без пароля."""
    user = _add_user(db_session)
    token = tenants.issue_magic_token(db_session, ADDRESS)
    before = _row(db_session, user)
    body = {"token": token, "new_password": PASSWORD}

    with monkeypatch.context() as patch:
        patch.setattr(api_module, broken, _broken)
        failed = TestClient(api_module.app, raise_server_exceptions=False).post(
            "/auth/set-password", json=body
        )

    assert failed.status_code == 500
    # Ни токен, ни срок, ни отметка входа, ни пароль не записаны.
    assert _row(db_session, user) == before
    created = client.post("/auth/set-password", json=body)
    assert created.status_code == 200, created.text
    db_session.refresh(user)
    assert api_module._verify_bcrypt(PASSWORD, user.password_hash)


def test_a_failure_while_signing_in_does_not_spend_the_link(client, db_session, monkeypatch):
    user = _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))
    token = tenants.issue_magic_token(db_session, ADDRESS)
    before = _row(db_session, user)

    with monkeypatch.context() as patch:
        patch.setattr(api_module, "_make_jwt", _broken)
        failed = TestClient(api_module.app, raise_server_exceptions=False).get(
            "/auth/verify", params={"token": token}
        )

    assert failed.status_code == 500
    assert _row(db_session, user) == before
    signed_in = client.get("/auth/verify", params={"token": token})
    assert signed_in.status_code == 200 and signed_in.json()["user_id"] == user.id


def test_a_link_clicked_in_a_browser_signs_in_once(client, db_session):
    """Клик из письма — навигация браузера: ответ другой, гашение то же."""
    user = _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))
    token = tenants.issue_magic_token(db_session, ADDRESS)
    click = {"params": {"token": token}, "headers": {"accept": "text/html"}}

    first = client.get("/auth/verify", follow_redirects=False, **click)

    assert first.status_code == 303 and first.headers["location"] == "/overview"
    assert api_module.COOKIE_NAME in first.cookies
    row = _row(db_session, user)
    assert row["magic_token"] is None and row["last_login_at"] is not None
    client.cookies.clear()
    again = client.get("/auth/verify", follow_redirects=False, **click)
    assert again.status_code == 303 and again.headers["location"] == "/login?error=link"
    assert api_module.COOKIE_NAME not in again.cookies


def test_a_login_link_sets_no_password_and_is_spent_by_the_attempt(client, db_session):
    """У кого пароль есть, тому ссылка — вход, а не новый пароль: это отказ, не сбой.

    Ссылка при этом гаснет, как и раньше: предъявленная не туда, она не остаётся
    входом для того, кто её перехватил.
    """
    user = _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))
    token = tenants.issue_magic_token(db_session, ADDRESS)

    refused = client.post("/auth/set-password", json={"token": token, "new_password": "another-1"})

    assert refused.status_code == 400
    row = _row(db_session, user)
    assert api_module._verify_bcrypt(PASSWORD, row["password_hash"])
    assert row["magic_token"] is None and row["magic_token_expires_at"] is None
    assert client.get("/auth/verify", params={"token": token}).status_code == 401


@pytest.fixture
def cli_logging(monkeypatch, tmp_path) -> Iterator[None]:
    """`_setup_logging` пишет в `logs/` текущего каталога и меняет общий конфиг."""
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield
    for handler in root.handlers:
        handler.close()
    root.handlers[:], root.level = saved[0], saved[1]
    structlog.reset_defaults()


def test_the_token_printed_by_the_cli_signs_in_and_is_not_stored(
    cli_logging, db_session, monkeypatch
):
    user = _add_user(db_session)
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: factory)

    result = CliRunner().invoke(main.cli, ["tenant", "issue-token", ADDRESS, "--ttl-min", "5"])

    assert result.exit_code == 0, result.output
    token = re.search(r"^Token: (\S+)$", result.output, re.MULTILINE).group(1)
    _assert_only_the_hash_is_stored(db_session, user, token, minutes=5)
    # Токен печатается оператору один раз; журнал команды его не повторяет.
    assert result.output.count(token) == 1
    log_file = Path("logs/app.jsonl")
    assert not log_file.exists() or token not in log_file.read_text(encoding="utf-8")
    assert tenants.verify_magic_token(db_session, token).id == user.id
