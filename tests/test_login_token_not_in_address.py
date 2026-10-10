"""Токен входа не доходит до сервера адресом: в ссылке он стоит после «#».

Адрес запроса пишут журнал прокси и журнал доступа API; он же уходит заголовком
Referer в следующих запросах страницы и заголовком Location, если страницу
отдают через редирект. 2026-10-10 на проде токен единственной открытой ссылки
лежал в записи Caddy в четырёх полях: адрес, Referer, Location и заголовок
`Next-Router-State-Tree`, в который Next.js кладёт адрес своей страницы. Ссылка
на создание пароля из такой записи оставалась рабочей, пока пароль не задан.

Поэтому:

- ссылка в письме несёт токен только после «#» — на каждом пути выдачи;
- открывает её страница API (`/auth/link`, `/auth/link/password`), а не
  фронтенда, и дальше токен идёт только телом запроса;
- форма создания пароля берёт токен из хранилища вкладки, не из своего адреса.

Что браузер при этом отправляет на самом деле — `test_login_link_in_a_browser.py`.
Здесь — формат ссылки, вход телом запроса и растяжки по тексту файлов: о форме
фронтенда тест знает только её исходник. `GET /auth/verify?token=…` оставлен
намеренно: им входят тесты, e2e и нагрузочный скрипт, письма — нет.
"""

from __future__ import annotations

import ast
import re
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from src import api as api_module
from src import login_link_pages, notifier, rate_limit, storage, tenants
from tests.test_magic_link_token import ADDRESS, PASSWORD, _add_user, _sign_in_as_admin

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend" / "src"
SIGN_IN_PAGE = "/auth/link"
PASSWORD_PAGE = "/auth/link/password"


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
def letters(monkeypatch) -> list[tuple[str, str]]:
    """Письма, которые ушли бы получателям: тема и тело."""
    sent: list[tuple[str, str]] = []

    def deliver(*, subject, html_body, to) -> bool:
        sent.append((subject, html_body))
        return True

    monkeypatch.setattr(notifier, "send_email", deliver)
    return sent


# ─── Что получает сервер, когда ссылку открывают ─────────────────────────────


def _assert_the_token_stays_in_the_browser(letter: tuple[str, str]) -> tuple[str, str]:
    """Страница, на которую ведёт ссылка, и токен, который до сервера не дошёл."""
    subject, body = letter
    (address,) = set(re.findall(r"""href=['"]([^'"]+)['"]""", body))
    sent_to_server, after_hash = urldefrag(address)
    token = after_hash.removeprefix("token=")
    assert after_hash == f"token={token}" and len(token) >= 43
    page = urlsplit(sent_to_server)
    # Строка запроса страницы (её пишут журналы) и Referer её следующих запросов.
    request_line = f"GET {page.path}{'?' + page.query if page.query else ''} HTTP/1.1"
    assert token not in request_line and token not in sent_to_server
    assert page.query == ""
    assert f"{page.scheme}://{page.netloc}" == "https://dashboard.example"
    # В письме токен стоит дважды — в ссылке и в её тексте, оба раза после «#».
    assert body.count(token) == body.count(f"#token={token}") == 2
    assert token not in subject
    return page.path, token


def test_a_requested_link_keeps_the_token_out_of_the_request(client, letters, db_session):
    _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))

    assert client.post("/auth/request", json={"email": ADDRESS}).status_code == 200

    page, token = _assert_the_token_stays_in_the_browser(letters[0])
    assert page == SIGN_IN_PAGE
    assert tenants.verify_magic_token(db_session, token) is not None


def test_a_requested_link_for_a_user_without_a_password_does_too(client, letters, db_session):
    _add_user(db_session)

    assert client.post("/auth/request", json={"email": ADDRESS}).status_code == 200

    page, token = _assert_the_token_stays_in_the_browser(letters[0])
    assert page == PASSWORD_PAGE
    assert tenants.verify_magic_token(db_session, token) is not None


def test_an_invitation_keeps_the_token_out_of_the_request(client, letters, db_session):
    _sign_in_as_admin(client, db_session)

    created = client.post("/api/v1/dash/recipients", json={"email": ADDRESS, "role": "viewer"})

    assert created.status_code == 200, created.text
    page, token = _assert_the_token_stays_in_the_browser(letters[0])
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
    page, token = _assert_the_token_stays_in_the_browser(letters[0])
    assert page == page_expected
    assert tenants.verify_magic_token(db_session, token) is not None


# ─── Страницы, которые открывает ссылка ──────────────────────────────────────


@pytest.mark.parametrize("page", [SIGN_IN_PAGE, PASSWORD_PAGE])
def test_the_link_page_is_the_same_for_everyone_and_is_not_cached(client, db_session, page):
    _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))
    token = tenants.issue_magic_token(db_session, ADDRESS)

    served = client.get(page)
    # Токен, приписанный к адресу, страницу не меняет: сервер его не читает.
    with_a_query = client.get(page, params={"token": token})

    assert served.status_code == 200
    assert served.headers["content-type"].startswith("text/html")
    assert served.headers["cache-control"] == "no-store"
    assert with_a_query.text == served.text and token not in served.text
    assert tenants.verify_magic_token(db_session, token) is not None


def test_the_proxy_gives_link_pages_and_their_posts_to_the_api():
    """Правило из образца `infra/Caddyfile`: `/auth/*` — API, остальное — фронтенд.

    Образец — не живой файл: тот ставит root руками (docs/RUNBOOK.md); эту строку
    с живой сверяли 2026-10-10.
    """
    caddyfile = (ROOT / "infra" / "Caddyfile").read_text(encoding="utf-8")
    (to_api,) = re.findall(r"^\s*@api path (.+)$", caddyfile, re.MULTILINE)

    def goes_to_api(path: str) -> bool:
        return any(fnmatchcase(path, pattern) for pattern in to_api.split())

    for path in (SIGN_IN_PAGE, PASSWORD_PAGE, "/auth/verify", "/auth/set-password"):
        assert goes_to_api(path), path
    # Куда страницы уводят дальше — это уже фронтенд.
    for path in ("/overview", "/login", "/set-password", "/az/set-password"):
        assert not goes_to_api(path), path


def test_the_pages_lead_only_to_routes_the_frontend_redirects_to_a_locale():
    """`/overview`, `/login`, `/set-password` без языка: их уводит `next.config.mjs`."""
    config = (ROOT / "frontend" / "next.config.mjs").read_text(encoding="utf-8")
    (legacy,) = re.findall(r"const LEGACY_ROUTES = \[(.*?)\];", config, re.DOTALL)
    redirected = set(re.findall(r'"([^"]+)"', legacy))
    targets = set(
        re.findall(
            r'location\.replace\("/([a-z-]+)',
            login_link_pages.SIGN_IN_PAGE + login_link_pages.SET_PASSWORD_PAGE,
        )
    )

    assert targets == {"overview", "login", "set-password"}
    assert targets <= redirected


# ─── Вход: токен приходит телом запроса ──────────────────────────────────────

AS_THE_PAGE = {"Accept": "application/json"}


def test_the_posted_token_signs_in_once(client, db_session):
    user = _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))
    token = tenants.issue_magic_token(db_session, ADDRESS)

    first = client.post("/auth/verify", json={"token": token}, headers=AS_THE_PAGE)

    assert first.status_code == 200
    assert first.json() == {"ok": True, "user_id": user.id}
    session_cookie = first.headers["set-cookie"].lower()
    assert session_cookie.startswith(f"{api_module.COOKIE_NAME}=")
    assert "httponly" in session_cookie and "samesite=lax" in session_cookie
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


def test_spaces_around_a_posted_token_do_not_matter(client, db_session):
    _add_user(db_session, password_hash=api_module._hash_bcrypt(PASSWORD))
    token = tenants.issue_magic_token(db_session, ADDRESS)

    assert client.post("/auth/verify", json={"token": f"  {token}\n"}).status_code == 200


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


# ─── Растяжки: текст формы фронтенда и шаблонов ссылок ───────────────────────


def _without_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", source, flags=re.MULTILINE)


def test_the_password_form_takes_the_token_from_tab_storage_and_posts_it():
    form = (FRONTEND / "app" / "[locale]" / "set-password" / "page.tsx").read_text(encoding="utf-8")
    code = _without_comments(form)

    assert "setToken(storedLinkToken())" in code
    # Свой адрес форма не читает: ни строку запроса, ни часть после «#».
    for reading_the_address in ("useSearchParams", "searchParams", "location."):
        assert reading_the_address not in code, reading_the_address
    assert re.findall(r"fetch\(([^,)]+)", code) == ['"/auth/set-password"']
    assert 'method: "POST"' in code
    assert re.search(r"body: JSON\.stringify\(\{ token\b", code)


def test_the_form_and_the_link_page_agree_on_where_the_token_waits():
    helper = _without_comments((FRONTEND / "lib" / "link-token.ts").read_text(encoding="utf-8"))

    (key,) = re.findall(r'LINK_TOKEN_STORAGE_KEY = "([^"]+)"', helper)
    assert key == login_link_pages.LINK_TOKEN_STORAGE_KEY
    assert f'sessionStorage.setItem("{key}", token)' in login_link_pages.SET_PASSWORD_PAGE
    assert "sessionStorage.getItem(LINK_TOKEN_STORAGE_KEY)" in helper
    # Вкладка, а не браузер: `localStorage` пережил бы её и достался другим вкладкам.
    assert "localStorage" not in helper + login_link_pages.SET_PASSWORD_PAGE
    assert "location" not in helper


def _string_constants(path: Path) -> list[str]:
    """Строки файла Python, кроме описаний модулей, классов и функций."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    described = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    descriptions = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, described)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in descriptions
    ]


def test_no_address_is_built_with_a_token_in_its_query():
    """Новая ссылка с `?token=` снова положила бы токен в журналы.

    Ловит адрес, собранный строкой, — в любом виде записи. Не ловит `urlencode`
    и `params=`: там слова «token=» в тексте нет.
    `src/auth.py` — вход старого Streamlit-дашборда, на сервере он не запущен.
    """
    in_a_query = re.compile(r"[?&]\w*token=")
    found = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if any(in_a_query.search(text) for text in _string_constants(path))
    } | {
        path.relative_to(ROOT).as_posix()
        for path in FRONTEND.rglob("*.ts*")
        if in_a_query.search(_without_comments(path.read_text(encoding="utf-8")))
    }

    assert found == {"src/auth.py"}
