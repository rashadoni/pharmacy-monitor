"""Тесты надёжности DDP-клиента pharmonline (2026-05-29).

Прод-симптомы, которые чиним:
- connect-крэш: `websockets.connect()` без таймаута/ретрая → handshake-timeout
  валил весь прогон (exit 1).
- mid-run hang: `ws.send()` не обёрнут таймаутом → half-open сокет висел 5+ мин.
- `call()` ловил только ConnectionClosed, не TimeoutError → зависания не
  триггерили reconnect.

Используем fake in-memory websocket (monkeypatch `websockets.connect`) — без сети.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.scrapers import pharmonline_ddp
from src.scrapers.base import SiteScrapeFatalError

HANG = object()  # sentinel: recv/send зависает (симуляция half-open сокета)


def _sockjs(*msgs: dict) -> str:
    """SockJS 'a'-frame = массив JSON-строк."""
    return "a" + json.dumps([json.dumps(m) for m in msgs])


_OPEN = "o"  # SockJS open frame
_CONNECTED = _sockjs({"msg": "connected", "session": "s1"})


def _result(call_id: str, result: dict) -> str:
    return _sockjs({"msg": "result", "id": call_id, "result": result})


class FakeWS:
    """Скриптуемый websocket. recv_script — список фреймов / HANG / Exception."""

    def __init__(self, recv_script: list, *, send_hang: bool = False):
        self._recv = list(recv_script)
        self.sent: list = []
        self.closed = False
        self.send_hang = send_hang

    async def recv(self):
        if not self._recv:
            raise ConnectionError("fake ws: no more frames")
        item = self._recv.pop(0)
        if item is HANG:
            await asyncio.sleep(3600)
        if isinstance(item, Exception):
            raise item
        return item

    async def send(self, data):
        if self.send_hang:
            await asyncio.sleep(3600)
        self.sent.append(data)

    async def close(self):
        self.closed = True


def _patch_connect(monkeypatch, ws_per_attempt):
    """ws_per_attempt: callable(attempt_n:int)->FakeWS (или raise)."""
    calls = {"n": 0}

    async def fake_connect(url, **kwargs):
        calls["n"] += 1
        return ws_per_attempt(calls["n"])

    monkeypatch.setattr(pharmonline_ddp.websockets, "connect", fake_connect)
    return calls


def _fast_env(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_DDP_OPEN_TIMEOUT", "0.05")
    monkeypatch.setenv("PHARMONLINE_DDP_CONNECT_BACKOFF", "0.01")
    monkeypatch.setenv("PHARMONLINE_DDP_CALL_RETRY_BACKOFF", "0")


# ── Connect robustness ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handshake_timeout_then_succeeds(monkeypatch):
    """1-я попытка виснет на SockJS-handshake (recv HANG) → таймаут → 2-я ок."""
    _fast_env(monkeypatch)
    monkeypatch.setenv("PHARMONLINE_DDP_CONNECT_ATTEMPTS", "4")

    def ws_factory(n):
        if n == 1:
            return FakeWS([HANG])  # recv "o" зависает → wait_for(open_timeout) бросит
        return FakeWS([_OPEN, _CONNECTED])

    calls = _patch_connect(monkeypatch, ws_factory)
    client = pharmonline_ddp._DDPClient(lambda: "wss://x")
    await client._connect()  # не должно бросить
    assert calls["n"] == 2  # одна неудача + успех


@pytest.mark.asyncio
async def test_connect_exhausts_and_raises(monkeypatch):
    """Все попытки виснут → _connect бросает после N попыток."""
    _fast_env(monkeypatch)
    monkeypatch.setenv("PHARMONLINE_DDP_CONNECT_ATTEMPTS", "2")
    calls = _patch_connect(monkeypatch, lambda n: FakeWS([HANG]))
    client = pharmonline_ddp._DDPClient(lambda: "wss://x")
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await client._connect()
    assert calls["n"] == 2  # ровно attempts попыток


# ── Per-call timeout (hard-bounded) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_hang_bounded(monkeypatch):
    """ws.send() виснет → call бросает TimeoutError за ~timeout, НЕ 5+ мин."""
    monkeypatch.setenv("PHARMONLINE_DDP_CALL_ATTEMPTS", "1")
    client = pharmonline_ddp._DDPClient(lambda: "wss://x")
    client._ws = FakeWS([], send_hang=True)
    t0 = asyncio.get_event_loop().time()
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await asyncio.wait_for(client.call("products", [{}], timeout=0.1), timeout=5.0)
    assert asyncio.get_event_loop().time() - t0 < 1.0  # bound сработал (~0.1, не ~5.0)


@pytest.mark.asyncio
async def test_recv_hang_bounded(monkeypatch):
    """recv() виснет (нет matching result) → call бросает TimeoutError за ~timeout."""
    monkeypatch.setenv("PHARMONLINE_DDP_CALL_ATTEMPTS", "1")
    client = pharmonline_ddp._DDPClient(lambda: "wss://x")
    client._ws = FakeWS([HANG])  # send ок, recv зависает
    t0 = asyncio.get_event_loop().time()
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await asyncio.wait_for(client.call("products", [{}], timeout=0.1), timeout=5.0)
    assert asyncio.get_event_loop().time() - t0 < 1.0


@pytest.mark.asyncio
async def test_timeout_triggers_reconnect_then_succeeds(monkeypatch):
    """1-й вызов виснет (timeout) → reconnect → 2-й возвращает result."""
    _fast_env(monkeypatch)
    monkeypatch.setenv("PHARMONLINE_DDP_CALL_ATTEMPTS", "2")
    monkeypatch.setenv("PHARMONLINE_DDP_CONNECT_ATTEMPTS", "1")
    # reconnect откроет свежий сокет (attempt 1 ниже) с готовым result.
    # _call_id сбрасывается в 0 при reconnect → следующий id = "1".
    reconnect_ws = FakeWS([_OPEN, _CONNECTED, _result("1", {"products": [{"_id": "x"}]})])
    _patch_connect(monkeypatch, lambda n: reconnect_ws)
    client = pharmonline_ddp._DDPClient(lambda: "wss://x")
    client._ws = FakeWS([HANG])  # первый вызов: recv зависает → TimeoutError
    res = await asyncio.wait_for(client.call("products", [{}], timeout=0.1), timeout=5.0)
    assert res == {"products": [{"_id": "x"}]}


@pytest.mark.asyncio
async def test_connection_drop_retried(monkeypatch):
    """ConnectionError на вызове → reconnect+retry → успех (регрессия старого пути)."""
    _fast_env(monkeypatch)
    monkeypatch.setenv("PHARMONLINE_DDP_CALL_ATTEMPTS", "2")
    monkeypatch.setenv("PHARMONLINE_DDP_CONNECT_ATTEMPTS", "1")
    reconnect_ws = FakeWS([_OPEN, _CONNECTED, _result("1", {"ok": True})])
    _patch_connect(monkeypatch, lambda n: reconnect_ws)
    client = pharmonline_ddp._DDPClient(lambda: "wss://x")
    # первый вызов: send ок, recv бросает ConnectionError (сокет умер)
    client._ws = FakeWS([ConnectionError("dropped")])
    res = await asyncio.wait_for(client.call("getFilterParam", [{}], timeout=1.0), timeout=5.0)
    assert res == {"ok": True}


@pytest.mark.asyncio
async def test_call_default_timeout_returns_result(monkeypatch):
    """getFilterParam дефолтным timeout=30 работает (внешние вызовы не сломаны)."""
    client = pharmonline_ddp._DDPClient(lambda: "wss://x")
    client._ws = FakeWS([_result("1", {"category": [{"id": "1", "path": "c1"}]})])
    res = await client.call("getFilterParam", [{"query": {}}])
    assert res == {"category": [{"id": "1", "path": "c1"}]}


# ── Decodo proxy for the DDP WebSocket (pharmonline off IPRoyal) ──────────────


def _clear_decodo_env(monkeypatch):
    for k in ("DECODO_USERNAME", "DECODO_PASSWORD", "DECODO_SITES", "DECODO_HOST", "DECODO_PORTS"):
        monkeypatch.delenv(k, raising=False)


def test_decodo_factory_none_without_creds(monkeypatch):
    _clear_decodo_env(monkeypatch)
    monkeypatch.setenv("DECODO_SITES", "pharmonline")
    assert pharmonline_ddp._decodo_proxy_factory() is None


def test_decodo_factory_none_when_pharmonline_excluded(monkeypatch):
    _clear_decodo_env(monkeypatch)
    monkeypatch.setenv("DECODO_USERNAME", "u")
    monkeypatch.setenv("DECODO_PASSWORD", "p")
    monkeypatch.setenv("DECODO_SITES", "aptekonline,aloe")
    assert pharmonline_ddp._decodo_proxy_factory() is None


def test_decodo_factory_rotates_ports_raw_password(monkeypatch):
    """M1: каждый вызов фабрики (= reconnect) отдаёт СЛЕДУЮЩИЙ порт (другой AZ-IP),
    зацикливаясь. Пароль RAW (НЕ URL-encoded): websockets-lib не декодирует userinfo
    → encoded '%3D' ломает с HTTP 407, raw '=' проходит (подтверждено на проде)."""
    _clear_decodo_env(monkeypatch)
    monkeypatch.setenv("DECODO_USERNAME", "spw25z9lwn")
    monkeypatch.setenv("DECODO_PASSWORD", "85Yo=yePfQ")  # '=' остаётся сырым
    monkeypatch.setenv("DECODO_SITES", "pharmonline")
    monkeypatch.setenv("DECODO_PORTS", "30001-30003")
    factory = pharmonline_ddp._decodo_proxy_factory()
    assert factory is not None
    urls = [factory() for _ in range(4)]
    assert urls == [
        "http://spw25z9lwn:85Yo=yePfQ@az.decodo.com:30001",
        "http://spw25z9lwn:85Yo=yePfQ@az.decodo.com:30002",
        "http://spw25z9lwn:85Yo=yePfQ@az.decodo.com:30003",
        "http://spw25z9lwn:85Yo=yePfQ@az.decodo.com:30001",  # цикл
    ]


def test_decodo_factory_preferred_over_iproyal(monkeypatch):
    """Оба настроены → pharmonline берёт Decodo-фабрику (IPRoyal остаётся fallback)."""
    _clear_decodo_env(monkeypatch)
    for k in ("IPROYAL_USERNAME", "IPROYAL_PASSWORD", "IPROYAL_SITES", "IPROYAL_HOST", "IPROYAL_COUNTRY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DECODO_USERNAME", "duser")
    monkeypatch.setenv("DECODO_PASSWORD", "dpass")
    monkeypatch.setenv("DECODO_SITES", "pharmonline")
    monkeypatch.setenv("IPROYAL_USERNAME", "iuser")
    monkeypatch.setenv("IPROYAL_PASSWORD", "ipass")
    monkeypatch.setenv("IPROYAL_SITES", "pharmonline")
    factory = pharmonline_ddp._decodo_proxy_factory()
    iproyal = pharmonline_ddp._iproyal_httpx_proxy()
    assert callable(factory) and iproyal is not None
    # резолюция в коде: `decodo_factory or iproyal` → берётся фабрика Decodo
    chosen = factory or iproyal
    assert callable(chosen) and chosen().startswith("http://duser:")


def test_ddpclient_wraps_proxy_factory_and_string(monkeypatch):
    """_DDPClient принимает proxy как callable-фабрику (ротация) ИЛИ строку
    (статика, backward-compat) ИЛИ None — все три через self.proxy_url_factory."""
    calls = {"n": 0}

    def fac():
        calls["n"] += 1
        return f"http://x:y@h:{3000 + calls['n']}"

    c = pharmonline_ddp._DDPClient(lambda: "wss://x", proxy_url=fac)
    assert c.proxy_url_factory() == "http://x:y@h:3001"
    assert c.proxy_url_factory() == "http://x:y@h:3002"  # ротация на каждый connect

    c2 = pharmonline_ddp._DDPClient(lambda: "wss://x", proxy_url="http://static:1")
    assert c2.proxy_url_factory() == "http://static:1"
    assert c2.proxy_url_factory() == "http://static:1"  # строка → статичная фабрика

    c3 = pharmonline_ddp._DDPClient(lambda: "wss://x")
    assert c3.proxy_url_factory() is None  # нет прокси → None


@pytest.mark.asyncio
async def test_scrape_category_407_aborts_the_whole_site():
    class FatalDDP:
        async def call(self, *args, **kwargs):
            raise ConnectionError("proxy rejected connection: HTTP 407")

    scraper = pharmonline_ddp.PharmonlineDDPScraper()
    scraper._ddp = FatalDDP()
    scraper._locale = "az"
    scraper._page_size = 100
    scraper._cat_map = {}

    with pytest.raises(SiteScrapeFatalError, match="HTTP 407"):
        _ = [p async for p in scraper.scrape_category("vitaminler")]


@pytest.mark.asyncio
async def test_ddp_initial_connect_407_is_site_fatal(monkeypatch):
    async def reject_proxy(self):
        raise ConnectionError("proxy rejected connection: HTTP 407")

    monkeypatch.setattr(pharmonline_ddp._DDPClient, "__aenter__", reject_proxy)
    scraper = pharmonline_ddp.PharmonlineDDPScraper()

    with pytest.raises(SiteScrapeFatalError, match="HTTP 407"):
        await scraper.__aenter__()


@pytest.mark.asyncio
async def test_ddp_generic_initial_failure_still_closes_client(monkeypatch):
    exits = []

    async def reject_connect(self):
        raise OSError("TLS handshake failed")

    async def record_exit(self, exc_type, exc, tb):
        exits.append(True)

    monkeypatch.setattr(pharmonline_ddp._DDPClient, "__aenter__", reject_connect)
    monkeypatch.setattr(pharmonline_ddp._DDPClient, "__aexit__", record_exit)
    scraper = pharmonline_ddp.PharmonlineDDPScraper()

    with pytest.raises(OSError, match="TLS handshake failed"):
        await scraper.__aenter__()

    assert exits == [True]


@pytest.mark.asyncio
async def test_ddp_connect_407_does_not_retry_or_expose_proxy_secret(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_DDP_CONNECT_ATTEMPTS", "9")
    client = pharmonline_ddp._DDPClient(lambda: "wss://example.invalid")
    calls = []

    async def reject_once():
        calls.append(True)
        raise ConnectionError(
            "proxy http://user:top-secret@az.decodo.com:30001 rejected HTTP 407"
        )

    monkeypatch.setattr(client, "_connect_once", reject_once)

    with pytest.raises(SiteScrapeFatalError) as exc_info:
        await client._connect()

    assert calls == [True]
    assert str(exc_info.value) == "proxy access rejected: HTTP 407"
    assert "top-secret" not in str(exc_info.value)
    assert "az.decodo.com" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_ddp_transient_proxy_status_retries(monkeypatch):
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidProxyStatus
    from websockets.http11 import Response

    monkeypatch.setenv("PHARMONLINE_DDP_CONNECT_ATTEMPTS", "2")
    monkeypatch.setenv("PHARMONLINE_DDP_CONNECT_BACKOFF", "0")
    client = pharmonline_ddp._DDPClient(lambda: "wss://example.invalid")
    calls = []

    async def flaky_connect():
        calls.append(True)
        if len(calls) == 1:
            raise InvalidProxyStatus(Response(502, "Bad Gateway", Headers()))

    monkeypatch.setattr(client, "_connect_once", flaky_connect)

    await client._connect()

    assert calls == [True, True]


@pytest.mark.asyncio
async def test_ddp_invalid_proxy_is_fatal_before_raw_logging(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_DDP_CONNECT_ATTEMPTS", "1")
    client = pharmonline_ddp._DDPClient(lambda: "wss://example.invalid")
    logged = []

    async def reject_once():
        raise OSError("InvalidProxy http://user:top-secret@proxy.invalid:9000")

    class CaptureLog:
        def error(self, event, **kwargs):
            logged.append((event, kwargs))

    monkeypatch.setattr(client, "_connect_once", reject_once)
    monkeypatch.setattr(pharmonline_ddp, "log", CaptureLog())

    with pytest.raises(SiteScrapeFatalError) as exc_info:
        await client._connect()

    assert str(exc_info.value) == "proxy configuration rejected"
    assert logged == []


@pytest.mark.asyncio
async def test_ddp_call_407_does_not_reconnect(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_DDP_CALL_ATTEMPTS", "9")
    client = pharmonline_ddp._DDPClient(lambda: "wss://example.invalid")
    send_calls = []
    reconnect_calls = []

    async def reject_send(*args, **kwargs):
        send_calls.append(True)
        raise ConnectionError("proxy rejected connection: HTTP 407")

    async def unexpected_reconnect():
        reconnect_calls.append(True)

    monkeypatch.setattr(client, "_send_and_wait", reject_send)
    monkeypatch.setattr(client, "_reconnect", unexpected_reconnect)

    with pytest.raises(SiteScrapeFatalError, match="HTTP 407"):
        await client.call("products", [], timeout=0.01)

    assert send_calls == [True]
    assert reconnect_calls == []


@pytest.mark.asyncio
async def test_ddp_category_map_407_is_not_treated_as_optional(monkeypatch):
    exits = []

    async def enter_ok(self):
        return self

    async def fatal_map(self, *args, **kwargs):
        raise SiteScrapeFatalError("proxy access rejected: HTTP 407")

    async def record_exit(self, exc_type, exc, tb):
        exits.append(True)

    monkeypatch.setattr(pharmonline_ddp._DDPClient, "__aenter__", enter_ok)
    monkeypatch.setattr(pharmonline_ddp._DDPClient, "call", fatal_map)
    monkeypatch.setattr(pharmonline_ddp._DDPClient, "__aexit__", record_exit)

    scraper = pharmonline_ddp.PharmonlineDDPScraper()
    with pytest.raises(SiteScrapeFatalError, match="HTTP 407"):
        await scraper.__aenter__()

    assert exits == [True]
