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
