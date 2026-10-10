"""Ссылку из письма открывает настоящий браузер: до сервера токен доходит только телом запроса.

Всё, что браузер отправил, записывается на стороне сервера — строка запроса,
заголовки, тело — и токен ищется во всём этом как есть: он состоит из знаков,
которые не меняет ни одно кодирование адреса, поэтому находится и внутри
закодированного заголовка. Рядом собираются строки журнала доступа uvicorn —
того самого, что на сервере идёт в журнал API.

Зачем браузер. Первая версия этой правки вела ссылку на страницу Next.js и
считала, что часть адреса после «#» серверу не уходит. Next.js сам кладёт её в
заголовок `Next-Router-State-Tree` своих запросов; тесты, читавшие страницы как
текст, этого не видели и видеть не могли. Теперь адрес с токеном открывает
страница API (`src/login_link_pages.py`), и её исполняет Chromium.

Чего здесь нет: фронтенда. Вместо него заглушка, которая отвечает как он —
`/overview`, `/login` и `/set-password` уводит на `/az/…`, страницы отдаёт
пустыми. Форму создания пароля тест изображает сам: берёт токен оттуда же,
откуда его берёт форма, и шлёт тем же запросом.

Chromium ставит CI (`playwright install chromium`). Локально без него тесты
пропускаются; в CI пропуск — ошибка. Свой браузер — `PM_TEST_CHROMIUM=<путь>`.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import pytest
import uvicorn
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import Error as BrowserError
from sqlalchemy.orm import sessionmaker

from src import api as api_module
from src import notifier, rate_limit, storage
from src.login_link_pages import LINK_TOKEN_STORAGE_KEY
from tests.test_magic_link_token import ADDRESS, PASSWORD, _add_user, _row

FRONTEND_ROUTES_WITHOUT_LOCALE = ("/overview", "/login", "/set-password")


@dataclass
class Received:
    """Один запрос, каким он дошёл до сервера."""

    method: str
    path: str
    address: str
    headers: list[tuple[str, str]]
    body: bytearray = field(default_factory=bytearray)

    def places_holding(self, value: str) -> list[str]:
        places = []
        if value in self.address:
            places.append("address")
        places += [f"header {name}" for name, held in self.headers if value in held]
        if value.encode() in bytes(self.body):
            places.append("body")
        return [f"{self.method} {self.path}: {place}" for place in places]


class ServerSide:
    """Записывает каждый запрос и отвечает за фронтенд на то, что не `/auth` и не `/api`."""

    def __init__(self, app) -> None:
        self.app = app
        self.received: list[Received] = []

    def places_holding(self, value: str) -> list[str]:
        return [place for request in self.received for place in request.places_holding(value)]

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        query = scope["query_string"].decode("latin-1")
        path = scope["raw_path"].decode("latin-1")
        seen = Received(
            method=scope["method"],
            path=path,
            address=f"{path}?{query}" if query else path,
            headers=[(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]],
        )
        self.received.append(seen)

        async def recording() -> dict:
            message = await receive()
            if message["type"] == "http.request":
                seen.body.extend(message.get("body", b""))
            return message

        if path.startswith(("/auth/", "/api/")):
            await self.app(scope, recording, send)
        elif path in FRONTEND_ROUTES_WITHOUT_LOCALE:
            # next.config.mjs, LEGACY_ROUTES: адрес без языка → `/az/…`, запрос сохраняется.
            await _answer(send, 307, {"location": f"/az{path}" + (f"?{query}" if query else "")})
        elif path.startswith("/az/"):
            await _answer(
                send, 200, {"content-type": "text/html"}, b"<html><body>frontend</body></html>"
            )
        else:
            await _answer(send, 404, {})


async def _answer(send, status: int, headers: dict[str, str], body: bytes = b"") -> None:
    raw = [(name.encode(), value.encode()) for name, value in headers.items()]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


@dataclass
class Site:
    address: str
    server_side: ServerSide
    access_log: list[str]


@pytest.fixture
def site(monkeypatch, db_session) -> Iterator[Site]:
    """Настоящий uvicorn с приложением; база — та же, что у теста."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    monkeypatch.setenv("PHARMACY_COOKIE_SECURE", "false")
    # В CI рядом стоит Redis, и счётчик запросов в нём переживал бы тест.
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: None)
    rate_limit._reset_memory_for_tests()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    address = f"http://127.0.0.1:{listener.getsockname()[1]}"
    monkeypatch.setenv("PHARMACY_PUBLIC_URL", address)

    # Журнал доступа — как на сервере, только строки собираются в список.
    access_log: list[str] = []
    collect = logging.Handler()
    collect.emit = lambda record: access_log.append(record.getMessage())
    access = logging.getLogger("uvicorn.access")
    saved = (access.level, access.propagate)
    access.addHandler(collect)
    access.setLevel(logging.INFO)
    access.propagate = False

    server_side = ServerSide(api_module.app)
    server = uvicorn.Server(
        uvicorn.Config(server_side, log_config=None, lifespan="off", access_log=True)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline and thread.is_alive(), "сервер не поднялся"
            time.sleep(0.01)
        yield Site(address, server_side, access_log)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        access.removeHandler(collect)
        access.level, access.propagate = saved


@pytest.fixture
def letters(monkeypatch) -> list[tuple[str, str]]:
    """Письма, которые ушли бы получателям: тема и тело."""
    sent: list[tuple[str, str]] = []

    def deliver(*, subject, html_body, to) -> bool:
        sent.append((subject, html_body))
        return True

    monkeypatch.setattr(notifier, "send_email", deliver)
    return sent


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    own = os.environ.get("PM_TEST_CHROMIUM")
    async with async_playwright() as playwright:
        try:
            launched = await playwright.chromium.launch(
                headless=True, **({"executable_path": own} if own else {})
            )
        except BrowserError as error:
            if os.environ.get("CI"):
                pytest.fail(f"в CI ссылку обязан открыть настоящий браузер: {error}")
            pytest.skip("нужен Chromium: `playwright install chromium` или PM_TEST_CHROMIUM")
        yield launched
        await launched.close()


@pytest.fixture
async def tab(browser) -> AsyncIterator[Page]:
    context = await browser.new_context()
    yield await context.new_page()
    await context.close()


def _link_for(db_session, letters, site: Site, **user_fields) -> tuple[str, str]:
    """Выдать ссылку так, как её выдаёт любой путь, и вернуть её адрес и токен."""
    _add_user(db_session, **user_fields)
    letters.clear()
    assert api_module._send_login_link(db_session, ADDRESS) is True
    ((_subject, body),) = letters
    link = body.split("href='", 1)[1].split("'", 1)[0]
    assert link.startswith(site.address + "/auth/link")
    token = link.split("#token=", 1)[1]
    site.server_side.received.clear()
    return link, token


async def _session_cookie(context: BrowserContext) -> dict | None:
    cookies = [c for c in await context.cookies() if c["name"] == api_module.COOKIE_NAME]
    return cookies[0] if cookies else None


# ─── Вход ────────────────────────────────────────────────────────────────────


async def test_a_login_link_signs_in_and_the_token_reaches_the_server_only_in_a_body(
    site, tab, letters, db_session
):
    link, token = _link_for(
        db_session, letters, site, password_hash=api_module._hash_bcrypt(PASSWORD)
    )

    await tab.goto(link)
    await tab.wait_for_url(site.address + "/az/overview")

    cookie = await _session_cookie(tab.context)
    assert cookie and cookie["httpOnly"] and cookie["sameSite"] == "Lax"
    me = await tab.evaluate("fetch('/api/v1/dash/me').then(r => r.json())")
    assert me["email"] == ADDRESS
    assert site.server_side.places_holding(token) == ["POST /auth/verify: body"]
    # Журнал доступа запрос записал — и токена в нём нет.
    assert [line for line in site.access_log if '"POST /auth/verify HTTP/1.1" 200' in line]
    assert not [line for line in site.access_log if token in line]


async def test_a_login_link_opens_once(site, tab, letters, db_session):
    link, token = _link_for(
        db_session, letters, site, password_hash=api_module._hash_bcrypt(PASSWORD)
    )
    await tab.goto(link)
    await tab.wait_for_url(site.address + "/az/overview")
    await tab.context.clear_cookies()

    again = await tab.context.new_page()
    await again.goto(link)
    await again.wait_for_url(site.address + "/az/login?error=link")

    assert await _session_cookie(tab.context) is None
    assert set(site.server_side.places_holding(token)) == {"POST /auth/verify: body"}


@pytest.mark.parametrize(
    "after_hash", ["", "token=", "other=1", "token=" + "x" * 600, "token=wrong"]
)
async def test_a_link_that_cannot_sign_in_leads_to_the_login_page(site, tab, after_hash):
    await tab.goto(f"{site.address}/auth/link#{after_hash}")
    await tab.wait_for_url(site.address + "/az/login?error=link")

    assert await _session_cookie(tab.context) is None


@pytest.mark.parametrize("failure", [500, 502, 429, "no answer"])
async def test_a_failed_check_keeps_the_link_alive_and_a_reload_retries(
    site, tab, letters, db_session, failure
):
    """Сбой — не отказ: токен не погашен, адрес прежний, обновление страницы входит."""
    link, token = _link_for(
        db_session, letters, site, password_hash=api_module._hash_bcrypt(PASSWORD)
    )

    async def fail(route) -> None:
        if failure == "no answer":
            await route.abort()
        else:
            await route.fulfill(status=failure, body="unavailable")

    await tab.route("**/auth/verify", fail)
    await tab.goto(link)
    await tab.wait_for_function(
        "document.getElementById('state').textContent.includes('Обновите страницу')"
    )

    assert tab.url == link
    assert "Səhifəni yeniləyin" in await tab.inner_text("#state")
    assert await _session_cookie(tab.context) is None
    assert site.server_side.places_holding(token) == []

    await tab.unroute("**/auth/verify")
    await tab.reload()
    await tab.wait_for_url(site.address + "/az/overview")
    assert await _session_cookie(tab.context)


# ─── Создание пароля ─────────────────────────────────────────────────────────

SUBMIT_THE_FORM = (
    """
password => fetch("/auth/set-password", {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ token: sessionStorage.getItem("%s"), new_password: password }),
}).then(response => response.status)
"""
    % LINK_TOKEN_STORAGE_KEY
)


async def test_an_invitation_hands_the_token_to_the_form_without_putting_it_in_an_address(
    site, tab, letters, db_session
):
    link, token = _link_for(db_session, letters, site)
    assert link.startswith(site.address + "/auth/link/password#token=")

    await tab.goto(link)
    await tab.wait_for_url(site.address + "/az/set-password")

    # Форма открыта по чистому адресу; токен ждёт её в хранилище вкладки.
    assert await tab.evaluate("location.href") == site.address + "/az/set-password"
    assert await tab.evaluate(f"sessionStorage.getItem('{LINK_TOKEN_STORAGE_KEY}')") == token
    assert site.server_side.places_holding(token) == []

    assert await tab.evaluate(SUBMIT_THE_FORM, PASSWORD) == 200

    assert await _session_cookie(tab.context)
    assert site.server_side.places_holding(token) == ["POST /auth/set-password: body"]
    assert not [line for line in site.access_log if token in line]
    (user_row,) = [_row(db_session, user) for user in db_session.query(storage.TenantUser)]
    assert user_row["password_hash"] and user_row["magic_token"] is None


async def test_the_token_stays_in_its_tab(site, tab, letters, db_session):
    link, _token = _link_for(db_session, letters, site)
    await tab.goto(link)
    await tab.wait_for_url(site.address + "/az/set-password")

    other = await tab.context.new_page()
    await other.goto(site.address + "/az/set-password")

    assert await other.evaluate(f"sessionStorage.getItem('{LINK_TOKEN_STORAGE_KEY}')") is None


async def test_a_password_link_without_a_token_forgets_the_one_opened_before(
    site, tab, letters, db_session
):
    link, _token = _link_for(db_session, letters, site)
    await tab.goto(link)
    await tab.wait_for_url(site.address + "/az/set-password")

    await tab.goto(site.address + "/auth/link/password")
    await tab.wait_for_url(site.address + "/az/set-password")

    assert await tab.evaluate(f"sessionStorage.getItem('{LINK_TOKEN_STORAGE_KEY}')") is None


async def test_a_browser_without_tab_storage_is_told_so_and_goes_nowhere(
    site, tab, letters, db_session
):
    link, token = _link_for(db_session, letters, site)
    await tab.add_init_script(
        "Object.defineProperty(window, 'sessionStorage', { get() { throw new Error('denied'); } })"
    )

    await tab.goto(link)
    await tab.wait_for_function(
        "document.getElementById('state').textContent.includes('Откройте её в другом браузере')"
    )

    assert tab.url == link
    assert site.server_side.places_holding(token) == []


# ─── Проверка умеет находить ─────────────────────────────────────────────────


async def test_the_recording_does_find_a_token_sent_in_an_address_or_a_header(
    site, tab, letters, db_session
):
    """Так шли письма раньше и так выдаёт токен Next.js — и то и другое здесь видно."""
    _link, token = _link_for(
        db_session, letters, site, password_hash=api_module._hash_bcrypt(PASSWORD)
    )

    await tab.goto(f"{site.address}/auth/verify?token={token}")
    await tab.wait_for_url(site.address + "/az/overview")
    await tab.evaluate(
        "token => fetch('/az/overview?_rsc=1', { headers: { 'Next-Router-State-Tree':"
        " encodeURIComponent(JSON.stringify(['', {}, '/az/set-password#token=' + token])) } })",
        token,
    )

    assert site.server_side.places_holding(token) == [
        "GET /auth/verify: address",
        "GET /az/overview: header next-router-state-tree",
    ]
    assert [line for line in site.access_log if token in line]
