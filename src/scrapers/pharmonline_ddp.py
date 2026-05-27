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
from urllib.parse import urlparse

import httpx
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


class _DDPClient:
    """Minimal Meteor DDP client over WebSocket. One-call-at-a-time semantics."""

    def __init__(self, ws_url: str, proxy_url: str | None = None):
        self.ws_url = ws_url
        self.proxy_url = proxy_url
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._call_id = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "_DDPClient":
        # websockets library doesn't natively support HTTP proxy CONNECT.
        # For prod use case: when proxy is needed, fall back to httpx-ws or
        # explicit tunnel. For now, IPRoyal supports SOCKS5 too — we'll wire
        # that as needed. Direct connection works for testing from Baku-IP.
        # Long-running scrapes (~20 min total): bump keepalive ping timeout to
        # 60s (default 20s) — pharmonline server occasionally takes 30+s to
        # respond on heavy categories, which triggered 1011 close in run 102.
        kwargs: dict = {
            "max_size": 50 * 1024 * 1024,  # 50MB messages
            "ping_interval": 30,
            "ping_timeout": 60,
            "close_timeout": 10,
        }
        if self.proxy_url:
            # websockets has experimental proxy support via `proxy` param in 13+.
            kwargs["proxy"] = self.proxy_url
        self._ws = await websockets.connect(self.ws_url, **kwargs)
        # SockJS sends "o" opening frame, then we send connect message.
        opening = await self._ws.recv()
        if opening != "o":
            raise RuntimeError(f"Expected SockJS open frame 'o', got {opening!r}")
        # SockJS wraps DDP messages as arrays of JSON strings.
        connect_msg = {"msg": "connect", "version": DDP_PROTOCOL_VERSION, "support": DDP_SUPPORT}
        await self._ws.send(json.dumps([json.dumps(connect_msg)]))
        # Wait for "connected" message.
        while True:
            frame = await self._ws.recv()
            for ddp in self._unwrap_sockjs(frame):
                if ddp.get("msg") == "connected":
                    log.info("ddp_connected", session=ddp.get("session"))
                    return self
                if ddp.get("msg") == "failed":
                    raise RuntimeError(f"DDP handshake failed: {ddp}")

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._ws:
            await self._ws.close()

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
        """Synchronous-style Meteor method call. Returns result payload."""
        async with self._lock:
            self._call_id += 1
            cid = str(self._call_id)
            req = {"msg": "method", "id": cid, "method": method, "params": params}
            assert self._ws is not None
            await self._ws.send(json.dumps([json.dumps(req)]))
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"DDP method {method!r} timeout after {timeout}s")
                try:
                    frame = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"DDP method {method!r} timeout")
                for ddp in self._unwrap_sockjs(frame):
                    if ddp.get("msg") == "result" and ddp.get("id") == cid:
                        if "error" in ddp:
                            raise RuntimeError(f"DDP method {method!r} error: {ddp['error']}")
                        return ddp.get("result", {})


def _build_product(raw: dict, locale: str) -> ScrapedProduct | None:
    """Map pharmonline DDP product → our ScrapedProduct."""
    name = raw.get("name") or ""
    i18n = raw.get("i18n") or {}
    # Prefer locale-specific name if available
    loc_name = (i18n.get(locale, {}) or {}).get("name") if locale in i18n else None
    name = loc_name or name
    if not name:
        return None

    slug = raw.get("postQuery") or raw.get("_id")
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
    is_on_sale = (
        max_price is not None and price is not None and max_price > price + 0.01
    )

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

    # DDP returns `category` as a list (product can belong to multiple
    # categories) and `manufacturer` similarly. Existing scrapers expect a
    # single string — flatten to first/primary slug. Otherwise downstream
    # `_per_category_breakdown` raises TypeError: unhashable type: 'list'.
    category = raw.get("category")
    if isinstance(category, list):
        category = category[0] if category else None
    if category is not None:
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
        self._locale = os.getenv("PHARMONLINE_DDP_LOCALE", "az").lower()
        self._page_size = int(os.getenv("PHARMONLINE_DDP_PAGE_SIZE", "100"))
        proxy_url = _iproyal_httpx_proxy()
        if not proxy_url:
            log.warning("pharmonline_ddp_no_proxy", note="will connect direct")
        ws_url = SOCKJS_BASE + _new_sockjs_path()
        self._ddp = _DDPClient(ws_url, proxy_url=proxy_url)
        await self._ddp.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self._ddp:
            await self._ddp.__aexit__(exc_type, exc, tb)

    async def scrape_category(
        self, category_slug: str, limit: int | None = None
    ) -> AsyncIterator[ScrapedProduct]:
        assert self._ddp is not None
        offset = 0
        yielded = 0
        max_yield = limit if limit is not None else 10_000

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

            for raw in products:
                sp = _build_product(raw, self._locale)
                if sp is None:
                    continue
                yielded += 1
                yield sp
                if yielded >= max_yield:
                    return

            # If we got fewer than page_size, we've hit the end
            if len(products) < self._page_size:
                break
            offset += 1  # offset is page-index, NOT product-index

    async def scrape_promos(self) -> list[ScrapedPromo]:  # type: ignore[override]
        # Promo banners are SSR'd into homepage HTML — separate scraper task.
        # Returning empty for now to keep DDP path lean.
        return []
