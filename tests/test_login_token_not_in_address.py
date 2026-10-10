"""Токен входа не доходит до сервера адресом: в ссылке он стоит после «#».

Адрес запроса пишут журнал прокси и журнал доступа API, он же уходит заголовком
Referer в следующих запросах страницы и заголовком Location, если страницу
отдают через редирект. 2026-10-10 в журнале Caddy на проде токен лежал во всех
трёх местах: адрес — 14 строк, Referer — 35, Location — 3. Ссылка на создание
пароля из такой строки оставалась рабочей, пока пароль не задан: страницу отдаёт
фронтенд, токен при этом не гаснет.

Часть адреса после «#» браузер серверу не отправляет вовсе. Поэтому:

- ссылка в письме несёт токен только там, на каждом пути выдачи;
- страница читает его сама и шлёт телом `POST /auth/verify` или
  `POST /auth/set-password`;
- адрес ведёт сразу на страницу фронтенда — без редиректа и мимо правила
  прокси, которое отдаёт `/auth/*` API.

Чего тест не видит: браузер и страницу в нём. О страницах он знает только
текст их файлов — это растяжка, а не проверка. `GET /auth/verify?token=…`
оставлен намеренно: им входят тесты, e2e и нагрузочный скрипт, письма — нет.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from src import api as api_module
from src import notifier, rate_limit, storage, tenants
from tests.test_magic_link_token import ADDRESS, PASSWORD, _add_user, _sign_in_as_admin

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend" / "src"
LOCALE = api_module.LOGIN_LINK_LOCALE
SIGN_IN_PAGE = f"/{LOCALE}/auth/verify"
PASSWORD_PAGE = f"/{LOCALE}/set-password"


@pytest.fixture
def client(monkeypatch, db_session) -> TestClient:
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", "https://dashboard.example")
    # В CI рядом стоит Redis, и счётчик запросов в нём переживал бы тест.
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


# ─── Что получает сервер, когда ссылку открывают ─────────────────────────────


def _address_in(letter: str) -> str:
    (address,) = set(re.findall(r"href='([^']+)'", letter))
    # Ссылка в письме показана и текстом — тем же адресом.
    assert letter.count(address) == 2
    return address


def _assert_the_token_stays_in_the_browser(address: str) -> tuple[str, str]:
    """Страница, на которую ведёт ссылка, и токен, который до сервера не дошёл."""
    sent_to_server, after_hash = urldefrag(address)
    token = after_hash.removeprefix("token=")
    assert after_hash == f"token={token}" and len(token) >= 43
    page = urlsplit(sent_to_server)
    # Строка запроса страницы (её пишут журналы) и Referer её следующих запросов.
    request_line = f"GET {page.path}{'?' + page.query if page.query else ''} HTTP/1.1"
    referer = sent_to_server
    assert token not in request_line and token not in referer
    assert page.query == ""
    assert f"{page.scheme}://{page.netloc}" == "https://dashboard.example"
    return page.path, token


def test_a_requested_link_keeps_the_token_out_of_the_request(client, letters, db_session):
    _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))

    assert client.post("/auth/request", json={"email": ADDRESS}).status_code == 200

    page, token = _assert_the_token_stays_in_the_browser(_address_in(letters[0]))
    assert page == SIGN_IN_PAGE
    assert tenants.verify_magic_token(db_session, token) is not None


def test_a_requested_link_for_a_user_without_a_password_does_too(client, letters, db_session):
    _add_user(db_session)

    assert client.post("/auth/request", json={"email": ADDRESS}).status_code == 200

    page, token = _assert_the_token_stays_in_the_browser(_address_in(letters[0]))
    assert page == PASSWORD_PAGE
    assert tenants.verify_magic_token(db_session, token) is not None


def test_an_invitation_keeps_the_token_out_of_the_request(client, letters, db_session):
    _sign_in_as_admin(client, db_session)

    created = client.post("/api/v1/dash/recipients", json={"email": ADDRESS, "role": "viewer"})

    assert created.status_code == 200, created.text
    page, token = _assert_the_token_stays_in_the_browser(_address_in(letters[0]))
    assert page == PASSWORD_PAGE
    assert tenants.verify_magic_token(db_session, token) is not None


@pytest.mark.parametrize(
    ("has_password", "page_expected"), [(True, SIGN_IN_PAGE), (False, PASSWORD_PAGE)]
)
def test_a_link_resent_by_an_admin_keeps_the_token_out_of_the_request(
    client, letters, db_session, has_password, page_expected
):
    _sign_in_as_admin(client, db_session)
    fields = {"password_hash": api_module._hash_bcrypt(PASSWORD)} if has_password else {}
    user = _add_user(db_session, **fields)

    resent = client.post(f"/api/v1/dash/recipients/{user.id}/send-login-link")

    assert resent.status_code == 200, resent.text
    page, token = _assert_the_token_stays_in_the_browser(_address_in(letters[0]))
    assert page == page_expected
    assert tenants.verify_magic_token(db_session, token) is not None


# ─── Вход: токен приходит телом запроса ──────────────────────────────────────

AS_THE_PAGE = {"Accept": "application/json"}


def test_the_posted_token_signs_in_once(client, db_session):
    user = _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))
    token = tenants.issue_magic_token(db_session, ADDRESS)

    first = client.post("/auth/verify", json={"token": token}, headers=AS_THE_PAGE)

    assert first.status_code == 200
    assert first.json() == {"ok": True, "user_id": user.id}
    assert api_module.COOKIE_NAME in first.cookies
    assert client.get("/api/v1/dash/me").json()["email"] == ADDRESS
    again = client.post("/auth/verify", json={"token": token}, headers=AS_THE_PAGE)
    assert again.status_code == 401
    # Погашен для любого способа доставки, не только для этого.
    assert client.get("/auth/verify", params={"token": token}).status_code == 401


def test_a_wrong_posted_token_is_refused_and_leaves_the_link_alive(client, db_session):
    _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))
    token = tenants.issue_magic_token(db_session, ADDRESS)

    refused = client.post("/auth/verify", json={"token": token[:-1] + "!"}, headers=AS_THE_PAGE)

    assert refused.status_code == 401
    assert api_module.COOKIE_NAME not in refused.cookies
    assert client.get("/api/v1/dash/me").status_code == 401
    assert client.post("/auth/verify", json={"token": token}).status_code == 200


@pytest.mark.parametrize("body", [{}, {"token": ""}, {"token": "   "}, {"token": "x" * 513}])
def test_a_post_without_a_usable_token_is_a_bad_request(client, body):
    refused = client.post("/auth/verify", json=body)

    assert refused.status_code == 422
    assert api_module.COOKIE_NAME not in refused.cookies


def test_the_longest_allowed_token_is_checked_like_any_other(client):
    """512 знаков — ещё токен (неверный), 513 — уже не запрос."""
    refused = client.post("/auth/verify", json={"token": "x" * 512})

    assert refused.status_code == 401


def test_the_post_takes_the_token_from_the_body_only(client, db_session):
    """Токен, приписанный к адресу POST, входом не служит и остаётся годным."""
    _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))
    token = tenants.issue_magic_token(db_session, ADDRESS)

    in_the_address = client.post("/auth/verify", params={"token": token})

    assert in_the_address.status_code == 422
    assert api_module.COOKIE_NAME not in in_the_address.cookies
    assert client.post("/auth/verify", json={"token": token}).status_code == 200


def test_a_token_that_is_not_text_is_refused_not_a_server_error(client, db_session):
    _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))
    tenants.issue_magic_token(db_session, ADDRESS)

    refused = client.post(
        "/auth/verify",
        content=b'{"token": "\\ud800"}',
        headers={"content-type": "application/json"},
    )

    assert refused.status_code == 401


# ─── Адрес ведёт на страницу фронтенда ───────────────────────────────────────


def test_links_use_the_locale_the_frontend_serves_by_default():
    config = (FRONTEND / "i18n" / "config.ts").read_text(encoding="utf-8")

    (default_locale,) = re.findall(r'defaultLocale: Locale = "(\w+)"', config)
    (served,) = re.findall(r"locales = \[([^\]]+)\] as const", config)

    assert LOCALE == default_locale
    assert f'"{LOCALE}"' in served
    for page in ("auth/verify", "set-password"):
        assert (FRONTEND / "app" / "[locale]" / page / "page.tsx").is_file()


def test_the_proxy_gives_link_pages_to_the_frontend_and_their_posts_to_the_api():
    """Правило из образца `infra/Caddyfile`: `/auth/*` — API, остальное — фронтенд.

    Ссылка без префикса языка (`/auth/verify#…`) пришла бы в API, а не на страницу.
    Образец — не живой файл: тот ставит root руками (docs/RUNBOOK.md).
    """
    caddyfile = (ROOT / "infra" / "Caddyfile").read_text(encoding="utf-8")
    (to_api,) = re.findall(r"^\s*@api path (.+)$", caddyfile, re.MULTILINE)

    def goes_to_api(path: str) -> bool:
        return any(fnmatchcase(path, pattern) for pattern in to_api.split())

    assert not goes_to_api(SIGN_IN_PAGE) and not goes_to_api(PASSWORD_PAGE)
    assert goes_to_api("/auth/verify") and goes_to_api("/auth/set-password")


# ─── Растяжки: текст страниц и шаблонов ссылок ───────────────────────────────


@pytest.mark.parametrize(
    ("page", "posts_to"),
    [("auth/verify", "/auth/verify"), ("set-password", "/auth/set-password")],
)
def test_link_pages_read_the_token_after_the_hash_and_post_it(page, posts_to):
    source = (FRONTEND / "app" / "[locale]" / page / "page.tsx").read_text(encoding="utf-8")

    assert "linkTokenFromHash(window.location.hash)" in source
    # Из строки запроса страница токен не берёт.
    assert "useSearchParams" not in source and "location.search" not in source
    assert re.findall(r'fetch\(\s*"([^"]+)"', source) == [posts_to]
    assert 'method: "POST"' in source
    assert re.search(r"body: JSON\.stringify\(\{ token\b", source)


def test_the_page_helper_reads_the_same_key_the_link_carries():
    helper = (FRONTEND / "lib" / "link-token.ts").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in helper.splitlines() if not line.lstrip().startswith(("*", "/*"))
    )

    assert 'params.get("token")' in code
    assert "location" not in code and "searchParams" not in code


def test_no_address_is_built_with_a_token_in_its_query():
    """Новая ссылка с `?token=` снова положила бы токен в журналы.

    `src/auth.py` — вход старого Streamlit-дашборда, на сервере он не запущен.
    """
    in_python = re.compile(r"[?&]\w*token=\{")
    in_typescript = re.compile(r"[?&]\w*token=\$\{")
    found = {
        path.relative_to(ROOT).as_posix()
        for pattern, files in (
            (in_python, (ROOT / "src").rglob("*.py")),
            (in_typescript, FRONTEND.rglob("*.ts*")),
        )
        for path in files
        if pattern.search(path.read_text(encoding="utf-8"))
    }

    assert found == {"src/auth.py"}
