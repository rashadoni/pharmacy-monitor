"""Pharmonline DDP-based scraper (Phase 1c, 2026-05-27).

Reverse-engineered Meteor `products` method protocol. Bypasses Cloudflare JS
challenge that blocks our Playwright-based scraper on Hetzner IP. Works with
plain WebSocket over IPRoyal residential proxy.

Protocol summary
----------------
DDP version "1" over SockJS WebSocket at wss://pharmonline.az/sockjs/<seg>/<sess>/websocket.

After connection establish:
  send: ["{\"msg\":\"connect\",\"version\":\"1\",\"support\":[\"1\",\"pre2\",\"pre1\"]}"]
  recv: ["{\"msg\":\"connected\",\"session\":\"...\"}"]

Then for each method call:
  send: ["{\"msg\":\"method\",\"id\":\"<n>\",\"method\":\"products\",\"params\":[<args>]}"]
  recv: many messages including "result" with id=<n>.

`products` method params (verified by browser DOM inspection 2026-05-27):
  params[0] (object):
    {
      "query": {
        "category": "<slug>",       # required
        "discount": true,           # optional, default not present
        "type": "most",             # optional, sort variant
        ...                          # other filters: price range, manufacturer
      },
      "sortBy": {"totalMinPrice": 1},   # 1=asc, -1=desc; field: totalMinPrice|name|...
      "productLimit": 24,               # page size
    }
  params[1] = locale: "en" | "az" | "ru"
  params[2] = page_offset: 0-indexed (page 0 = first 24 products)

Response shape:
  {
    "products": [Product, ...],   # array len <= productLimit
    "productImages": [...],
    "manufacturers": [...],
    "forms": [...],
  }

Product fields used:
  _id, GUID, parentCode, postQuery (URL slug), name, i18n.{az,ru,en}.name,
  barcode (string, may be ""), images (array of image ids), totalMinPrice,
  totalMaxPrice, category (slug), manufacturer (slug), totalCount (stock).

For full category coverage call getCategoryCount first or iterate page_offset
until response['products'] shorter than productLimit.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import string
from typing import AsyncIterator

import structlog
import websockets

from src.scrapers.base import BaseScraper, ScrapedProduct, ScrapedPromo

log = structlog.get_logger()


DDP_PROTOCOL_VERSION = "1"
DDP_SUPPORT = [DDP_PROTOCOL_VERSION, "pre2", "pre1"]
SOCKJS_BASE = "wss://pharmonline.az/sockjs"


# SockJS spec: URL format /<server_id_3digits>/<session_8chars>/websocket
def _new_sockjs_path() -> str:
    server = "".join(random.choices(string.digits, k=3))
    sess = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"/{server}/{sess}/websocket"


def _iproyal_httpx_proxy() -> str | None:
    """Resolve IPRoyal HTTP proxy URL from env (mirrors logic in aptekonline)."""
    username = os.getenv("IPROYAL_USERNAME")
    password = os.getenv("IPROYAL_PASSWORD")
    if not username or not password:
        return None
    sites_csv = os.getenv("IPROYAL_SITES", "")
    sites = {s.strip() for s in sites_csv.split(",") if s.strip()}
    if "pharmonline" not in sites:
        return None
    country = os.getenv("IPROYAL_COUNTRY", "").strip().lower()
    if country and "_country-" not in username:
        username = f"{username}_country-{country}"
    host = os.getenv("IPROYAL_HOST", "geo.iproyal.com:12321").strip()
    return f"http://{username}:{password}@{host}"


def _decodo_ports_list() -> list[int]:
    """Порты Decodo (DECODO_PORTS: диапазон '30001-30010' или CSV). Деф. 30001-30010.

    Каждый порт = отдельная sticky AZ-сессия (свой exit-IP). Гарантирует непустой
    список (fallback [30001]).
    """
    raw = os.getenv("DECODO_PORTS", "30001-30010").strip()
    ports: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.strip().isdigit() and hi.strip().isdigit():
                ports.extend(range(int(lo), int(hi) + 1))
        elif part.isdigit():
            ports.append(int(part))
    return ports or [30001]


def _decodo_proxy_factory():
    """Фабрика Decodo-прокси для DDP: callable, отдающий URL с РОТАЦИЕЙ порта на
    каждый вызов (= другой sticky AZ-IP на каждый (re)connect).

    Лечит ~38% флайки residential-IP: reconnect берёт свежий IP, а не лупится в
    мёртвый (особенно важно — IPRoyal-fallback мёртв на 402, подстраховки нет).
    Зеркало per-request port-cycling из aptekonline, адаптированное под одну
    долгоживущую DDP-сессию (ротация на reconnect, а не на запрос). None если
    Decodo не настроен для pharmonline → caller fallback'ит на статичный IPRoyal.

    ВАЖНО — пароль RAW, НЕ URL-encoded. Библиотека `websockets` НЕ percent-декодирует
    userinfo из proxy-URL (в отличие от httpx у aptek): encoded '85Yo%3D...' → она
    шлёт его буквально → HTTP 407. Подтверждено эмпирически (raw connect ok / encoded
    407). Пароли Decodo base64-подобные (alnum + '='); '=' валиден в userinfo. Если
    пароль когда-нибудь будет содержать '@'/':' — это сломает парсинг URL (но Decodo
    такие не выдаёт; тогда понадобится отдельная схема).
    """
    user = os.getenv("DECODO_USERNAME")
    pwd = os.getenv("DECODO_PASSWORD")
    if not user or not pwd:
        return None
    sites = {s.strip() for s in os.getenv("DECODO_SITES", "").split(",") if s.strip()}
    if "pharmonline" not in sites:
        return None
    host = os.getenv("DECODO_HOST", "az.decodo.com").strip()
    ports = _decodo_ports_list()
    state = {"i": 0}

    def _next_proxy() -> str:
        port = ports[state["i"] % len(ports)]
        state["i"] += 1
        return f"http://{user}:{pwd}@{host}:{port}"

    return _next_proxy


# ── DDP reliability tuning (2026-05-29) ──────────────────────────────────────
# Читаются из env ПРИ ВЫЗОВЕ (не module-level) → прод-override без редеплоя +
# тесты через monkeypatch.setenv. Дефолты: connect 4 попытки с backoff 2/4/8/16с,
# каждая ограничена open_timeout; call 3 попытки с reconnect.
def _ddp_open_timeout() -> float:
    return float(os.getenv("PHARMONLINE_DDP_OPEN_TIMEOUT", "20"))


def _ddp_connect_attempts() -> int:
    return max(1, int(os.getenv("PHARMONLINE_DDP_CONNECT_ATTEMPTS", "4")))


def _ddp_connect_backoff() -> float:
    return float(os.getenv("PHARMONLINE_DDP_CONNECT_BACKOFF", "2"))


def _ddp_call_attempts() -> int:
    return max(1, int(os.getenv("PHARMONLINE_DDP_CALL_ATTEMPTS", "3")))


def _ddp_call_retry_backoff() -> float:
    return float(os.getenv("PHARMONLINE_DDP_CALL_RETRY_BACKOFF", "1.0"))


class _DDPClient:
    """Minimal Meteor DDP client over WebSocket. One-call-at-a-time semantics.

    Phase 1c.4 (2026-05-27): handles persistent-connection failures via
    transparent reconnect-on-close in `call()`. Persist phase в pharmacy-monitor
    блочит event loop на ~5 минут, и pharmonline сервер бросает 1011 close
    (keepalive ping timeout). Reconnect восстанавливает session и retry'ит
    call один раз. Call IDs reset на новой сессии — это OK, мы не subscriber'им
    long-lived data.
    """

    # Connection params (constants for testability)
    PING_INTERVAL = 30
    PING_TIMEOUT = 60
    CLOSE_TIMEOUT = 10
    MAX_MSG_SIZE = 50 * 1024 * 1024

    def __init__(self, ws_url_factory, proxy_url: str | None = None):
        """ws_url_factory: callable returning fresh SockJS URL on each connect.

        Each reconnect generates a new random session id (see _new_sockjs_path)
        — Meteor server treats it as a new client and won't reject as duplicate.
        """
        self.ws_url_factory = (
            ws_url_factory if callable(ws_url_factory) else (lambda: ws_url_factory)
        )
        # proxy_url: строка ИЛИ callable-фабрика (как ws_url_factory выше). Decodo
        # передаёт фабрику, циклящую sticky-порты → каждый (re)connect берёт ДРУГОЙ
        # AZ-IP. Без этого reconnect лупился бы в тот же (возможно мёртвый при ~38%
        # 522) IP до исчерпания попыток, убивая всю DDP-сессию (IPRoyal-fallback=402).
        self.proxy_url_factory = (
            proxy_url if callable(proxy_url) else (lambda: proxy_url)
        )
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._call_id = 0
        self._lock = asyncio.Lock()

    async def _connect(self) -> None:
        """Connect with retry + exp-backoff. Used by __aenter__ AND _reconnect.

        Fix 2026-05-29: раньше один naked `await websockets.connect()` без таймаута
        и ретрая → transient handshake-timeout валил весь прогон (exit 1). Теперь
        каждая попытка ограничена open_timeout, между попытками exp-backoff.
        """
        attempts = _ddp_connect_attempts()
        backoff_base = _ddp_connect_backoff()
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                await self._connect_once()
                if attempt > 0:
                    log.info("ddp_connect_recovered", attempt=attempt + 1)
                return
            except (
                asyncio.TimeoutError,
                TimeoutError,
                OSError,
                websockets.exceptions.WebSocketException,
                ConnectionError,
            ) as exc:
                last_exc = exc
                if self._ws is not None:  # закрыть half-open сокет перед ретраем
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                    self._ws = None
                if attempt < attempts - 1:
                    backoff = backoff_base ** (attempt + 1)  # 2,4,8,16
                    log.warning(
                        "ddp_connect_retry",
                        attempt=attempt + 1,
                        max_attempts=attempts,
                        backoff_s=backoff,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    await asyncio.sleep(backoff)
        log.error(
            "ddp_connect_exhausted",
            attempts=attempts,
            error=f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown",
        )
        raise last_exc if last_exc else RuntimeError("DDP connect failed")

    async def _connect_once(self) -> None:
        """Single connect attempt: open websocket + SockJS open + DDP handshake.

        websockets-native `open_timeout` бьёт только WS-handshake; SockJS 'o' frame
        и DDP connect→connected идут ПОСЛЕ connect() → оборачиваем их в wait_for.
        """
        open_timeout = _ddp_open_timeout()
        kwargs: dict = {
            "max_size": self.MAX_MSG_SIZE,
            "ping_interval": self.PING_INTERVAL,
            "ping_timeout": self.PING_TIMEOUT,
            "close_timeout": self.CLOSE_TIMEOUT,
            "open_timeout": open_timeout,
        }
        proxy = self.proxy_url_factory()
        if proxy:
            kwargs["proxy"] = proxy
        ws_url = self.ws_url_factory()
        self._ws = await websockets.connect(ws_url, **kwargs)
        await asyncio.wait_for(self._ddp_handshake(), timeout=open_timeout)

    async def _ddp_handshake(self) -> None:
        """SockJS open frame + DDP connect/connected. Assumes self._ws is open."""
        opening = await self._ws.recv()
        if opening != "o":
            raise RuntimeError(f"Expected SockJS open frame 'o', got {opening!r}")
        connect_msg = {"msg": "connect", "version": DDP_PROTOCOL_VERSION, "support": DDP_SUPPORT}
        await self._ws.send(json.dumps([json.dumps(connect_msg)]))
        while True:
            frame = await self._ws.recv()
            for ddp in self._unwrap_sockjs(frame):
                if ddp.get("msg") == "connected":
                    log.info("ddp_connected", session=ddp.get("session"))
                    return
                if ddp.get("msg") == "failed":
                    raise RuntimeError(f"DDP handshake failed: {ddp}")

    async def __aenter__(self) -> "_DDPClient":
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _reconnect(self) -> None:
        """Close dead websocket and re-establish DDP session."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._call_id = 0  # new server session = id counter restarts
        log.info("ddp_reconnecting")
        await self._connect()

    @staticmethod
    def _unwrap_sockjs(frame: str) -> list[dict]:
        """SockJS frames: 'a[\"json1\",\"json2\"]' or 'h' (heartbeat) or 'c' (close)."""
        if not frame:
            return []
        type_char = frame[0]
        if type_char == "h":
            return []  # heartbeat
        if type_char == "c":
            log.warning("sockjs_close", frame=frame)
            return []
        if type_char != "a":
            log.debug("sockjs_unknown_frame", frame=frame[:80])
            return []
        try:
            outer = json.loads(frame[1:])  # array of JSON-encoded strings
        except json.JSONDecodeError:
            return []
        msgs = []
        for s in outer:
            try:
                msgs.append(json.loads(s))
            except json.JSONDecodeError:
                continue
        return msgs

    async def call(self, method: str, params: list, timeout: float = 30.0) -> dict:
        """Synchronous-style Meteor method call с auto-reconnect.

        Если socket закрылся (1011 ping timeout либо ConnectionClosed),
        делаем один reconnect и retry'им call. Это критично для long-running
        scrapes где persist phase блочит event loop на 5+ минут.
        """
        attempts = _ddp_call_attempts()
        retry_backoff = _ddp_call_retry_backoff()
        async with self._lock:
            last_exc: Exception | None = None
            for attempt in range(attempts):
                try:
                    return await self._send_and_wait(method, params, timeout)
                except (
                    asyncio.TimeoutError,  # half-open сокет: send/recv завис дольше timeout
                    TimeoutError,
                    websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.WebSocketException,
                    ConnectionError,
                ) as exc:
                    last_exc = exc
                    if attempt < attempts - 1:
                        log.warning(
                            "ddp_call_reconnecting",
                            method=method,
                            attempt=attempt + 1,
                            max_attempts=attempts,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        try:
                            await self._reconnect()
                        except Exception as rexc:  # reconnect исчерпал ретраи — фиксируем
                            last_exc = rexc
                            log.warning(
                                "ddp_call_reconnect_failed",
                                method=method,
                                error=f"{type(rexc).__name__}: {rexc}",
                            )
                        if retry_backoff > 0:
                            await asyncio.sleep(retry_backoff)
                        continue
            log.error(
                "ddp_call_exhausted",
                method=method,
                attempts=attempts,
                error=f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown",
            )
            raise last_exc if last_exc else RuntimeError("DDP call exhausted retries")

    async def _send_and_wait(self, method: str, params: list, timeout: float) -> dict:
        """Single attempt, hard-bounded by `timeout`.

        Fix 2026-05-29: оборачиваем ВЕСЬ метод (вкл. `ws.send`) в asyncio.wait_for.
        Раньше дедлайн покрывал только recv-loop, а `ws.send` перед ним — нет → на
        half-open сокете send() висел 5+ мин (TCP). Теперь wait_for отменяет всю
        операцию (send+recv) по timeout и бросает asyncio.TimeoutError → call()
        делает reconnect+retry.
        """
        if self._ws is None:
            raise ConnectionError("DDP socket not open")
        return await asyncio.wait_for(self._send_and_wait_inner(method, params), timeout=timeout)

    async def _send_and_wait_inner(self, method: str, params: list) -> dict:
        self._call_id += 1
        cid = str(self._call_id)
        req = {"msg": "method", "id": cid, "method": method, "params": params}
        await self._ws.send(json.dumps([json.dumps(req)]))
        while True:
            frame = await self._ws.recv()
            for ddp in self._unwrap_sockjs(frame):
                if ddp.get("msg") == "result" and ddp.get("id") == cid:
                    if "error" in ddp:
                        raise RuntimeError(f"DDP method {method!r} error: {ddp['error']}")
                    return ddp.get("result", {})


def _build_product(
    raw: dict,
    locale: str,
    category_id_to_slug: dict[str, str] | None = None,
) -> ScrapedProduct | None:
    """Map pharmonline DDP product → our ScrapedProduct.

    `category_id_to_slug` (optional): map Meteor _id → human-readable slug for
    category resolution. DDP returns category as list of Meteor _id's; we want
    the URL-slug for frontend rendering. Pass via getFilterParam pre-fetch.
    """
    name = raw.get("name") or ""
    i18n = raw.get("i18n") or {}
    # Prefer locale-specific name if available
    loc_name = (i18n.get(locale, {}) or {}).get("name") if locale in i18n else None
    name = loc_name or name
    if not name:
        return None

    # IMPORTANT: pharmonline uses `path` as URL slug (e.g. "dimedrol-005-q-10-tabletler-ukrayna"),
    # NOT `postQuery` (which is boolean `true` indicating that path field exists).
    slug = raw.get("path") or raw.get("_id")
    if not slug:
        return None

    price = raw.get("totalMinPrice")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None

    max_price = raw.get("totalMaxPrice")
    try:
        max_price = float(max_price) if max_price is not None else None
    except (TypeError, ValueError):
        max_price = None
    is_on_sale = max_price is not None and price is not None and max_price > price + 0.01

    images = raw.get("images") or []
    image_url = None
    if images and isinstance(images, list):
        img_id = images[0]
        # pharmonline image URL pattern: /cdn/storage/product_images/<id>/HD/<id>.<ext>
        image_url = f"https://pharmonline.az/cdn/storage/product_images/{img_id}/HD/{img_id}"

    barcode = raw.get("barcode") or None
    if barcode and not str(barcode).strip().isdigit():
        barcode = None
    elif barcode:
        barcode = str(barcode).strip()

    # DDP returns `category` as a list of Meteor _id's (product can belong
    # to multiple categories). We resolve first _id → human-readable slug via
    # the pre-fetched `category_id_to_slug` map (built from getFilterParam).
    # Falls back to None if no map provided — downstream uses external_id for
    # URL anyway, only frontend filtering needs category slug.
    category = raw.get("category")
    if isinstance(category, list) and category:
        cat_id = str(category[0])
        if category_id_to_slug and cat_id in category_id_to_slug:
            category = category_id_to_slug[cat_id]
        else:
            # Unknown _id (perhaps newer than our map) — leave raw _id rather
            # than None so we at least have something stable for grouping.
            category = cat_id
    elif isinstance(category, list):
        category = None  # empty list
    elif category is not None:
        category = str(category)

    manufacturer = raw.get("manufacturer")
    if isinstance(manufacturer, list):
        manufacturer = manufacturer[0] if manufacturer else None
    if manufacturer is not None:
        manufacturer = str(manufacturer)

    return ScrapedProduct(
        site="pharmonline",
        external_id=str(raw["_id"]),
        url=f"https://pharmonline.az/product/{slug}",
        name=str(name)[:500],
        manufacturer=manufacturer,
        category=category,
        image_url=image_url,
        description=(i18n.get(locale, {}) or {}).get("description") or None,
        price=max_price if is_on_sale else price,
        discount_price=price if is_on_sale else None,
        is_on_sale=is_on_sale,
        barcode=barcode,
    )


class PharmonlineDDPScraper(BaseScraper):
    """Pharmonline scraper via Meteor DDP protocol (no browser needed).

    Bypasses Cloudflare anti-bot because DDP WebSocket has different fingerprint
    surface than Playwright Chromium. Works through IPRoyal residential proxy.

    Falls back to Playwright PharmonlineScraper if `IPROYAL_*` env not set.
    """

    site_name = "pharmonline"
    base_url = "https://pharmonline.az"

    async def __aenter__(self) -> "PharmonlineDDPScraper":  # type: ignore[override]
        # Don't init Playwright — DDP doesn't need it. Light alternative entry.
        self._ddp: _DDPClient | None = None
        self._cat_map: dict[str, str] = {}
        self._locale = os.getenv("PHARMONLINE_DDP_LOCALE", "az").lower()
        self._page_size = int(os.getenv("PHARMONLINE_DDP_PAGE_SIZE", "100"))
        # Decodo (AZ residential, ротация порта на reconnect) первым; IPRoyal —
        # fallback (его баланс кончился 2026-06-12 → HTTP 402). decodo_factory это
        # callable (циклит порты); IPRoyal — статичный URL. _DDPClient принимает оба.
        decodo_factory = _decodo_proxy_factory()
        proxy_url = decodo_factory or _iproyal_httpx_proxy()
        if proxy_url:
            log.info(
                "pharmonline_ddp_proxy",
                provider="decodo" if decodo_factory else "iproyal",
            )
        else:
            log.warning("pharmonline_ddp_no_proxy", note="will connect direct")
        # Pass URL factory (not static URL) so reconnect gets a fresh SockJS
        # session path each time — pharmonline server rejects stale session ids.
        self._ddp = _DDPClient(
            lambda: SOCKJS_BASE + _new_sockjs_path(),
            proxy_url=proxy_url,
        )
        await self._ddp.__aenter__()
        # Pre-fetch category _id → slug map. Without this, products have raw
        # Mongo ObjectIds ("Fom7dQ8wnDWSgAeyn") as `category` field, breaking
        # frontend "click category → browse" UX. getFilterParam returns a list
        # of {id: _id, path: slug, name: human-readable} dicts.
        try:
            flt = await self._ddp.call(
                "getFilterParam",
                [{"query": {}, "sortBy": {"totalMinPrice": 1}, "productLimit": 24}, self._locale],
                timeout=30.0,
            )
            for c in flt.get("category", []) or []:
                cid = c.get("id")
                slug = c.get("path")
                if cid and slug:
                    self._cat_map[str(cid)] = str(slug)
            log.info("pharmonline_ddp_cat_map_loaded", count=len(self._cat_map))
        except Exception as exc:
            log.warning(
                "pharmonline_ddp_cat_map_failed",
                error=f"{type(exc).__name__}: {exc}",
                note="продукты получат raw Mongo _id в поле category",
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self._ddp:
            await self._ddp.__aexit__(exc_type, exc, tb)

    async def scrape_category(
        self, category_slug: str, limit: int | None = None
    ) -> AsyncIterator[ScrapedProduct]:
        """Yield products from DDP pagination, deduped by external_id.

        Bug fix 2026-05-28 (Codex/DB-audit): прежний код выдавал ровно 10K
        для больших категорий — но в БД оказывалось ≤ 200 уникальных. Причина:
        (а) НИКАКОГО dedup по external_id внутри одной категории.
        (б) `offset += 1` неоднозначно: может быть page-index, а DDP API
            возможно ожидает product-index. Pharmonline возвращал overlapping
            страницы → counter рос, unique оставались десятки.

        Fix:
        - `seen_external_ids` set предотвращает duplicate yields.
        - Если 3 страницы подряд дали 0 новых → end-of-stream (защита от
          server возвращающего infinite-overlap).
        - `max_yield` теперь env-configurable через
          `PHARMONLINE_DDP_MAX_YIELD_PER_CATEGORY` (default 50_000 — pharmonline
          катaлог 26K, всю категорию покрываем).
        """
        assert self._ddp is not None
        offset = 0
        yielded = 0
        max_yield = (
            limit
            if limit is not None
            else int(os.getenv("PHARMONLINE_DDP_MAX_YIELD_PER_CATEGORY", "50000"))
        )
        seen_external_ids: set[str] = set()
        zero_new_streak = 0  # подряд страниц с 0 новыми → break

        while yielded < max_yield:
            params = [
                {
                    "query": {"category": category_slug},
                    "sortBy": {"totalMinPrice": 1},
                    "productLimit": self._page_size,
                },
                self._locale,
                offset,
            ]
            try:
                result = await self._ddp.call("products", params, timeout=30.0)
            except Exception as e:
                log.warning(
                    "pharmonline_ddp_call_failed",
                    category=category_slug,
                    offset=offset,
                    error=str(e),
                )
                break

            products = result.get("products") or []
            if not products:
                break

            page_new = 0
            page_dup = 0
            for raw in products:
                sp = _build_product(raw, self._locale, self._cat_map)
                if sp is None:
                    continue
                if sp.external_id in seen_external_ids:
                    page_dup += 1
                    continue
                seen_external_ids.add(sp.external_id)
                page_new += 1
                yielded += 1
                yield sp
                if yielded >= max_yield:
                    return

            # Если на странице 0 новых — копим streak
            if page_new == 0:
                zero_new_streak += 1
                if zero_new_streak >= 3:
                    log.info(
                        "pharmonline_ddp_pagination_exhausted",
                        category=category_slug,
                        pages=offset + 1,
                        yielded=yielded,
                        reason="3 pages in a row with 0 new products",
                    )
                    break
            else:
                zero_new_streak = 0

            if page_dup and page_new == 0:
                # Лог если страница полностью overlapping — это сигнал что
                # pharmonline DDP pagination возвращает одни и те же items
                # на разных offset'ах
                log.debug(
                    "pharmonline_ddp_page_all_dup",
                    category=category_slug,
                    offset=offset,
                    dup=page_dup,
                )

            # If we got fewer than page_size, we've hit the end
            if len(products) < self._page_size:
                break
            offset += 1  # offset is page-index in current DDP semantics

    async def scrape_promos(self) -> list[ScrapedPromo]:  # type: ignore[override]
        # Promo banners are SSR'd into homepage HTML — separate scraper task.
        # Returning empty for now to keep DDP path lean.
        return []
