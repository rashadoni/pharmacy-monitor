"""Verified full-catalog scraper for Pharmonline's public JSON API.

This source is deliberately restricted to the manual recovery flow.  Unlike
the legacy rendered-HTML path, it receives the site's native 17-character
product IDs through an explicitly selected transport. Before yielding one
product it buffers the entire catalog and checks a set of invariants:

* every advertised API page is present and has the expected size;
* IDs and canonical product URLs are unique and total exactly matches;
* the product URL set matches the public product sitemap.

Consequently a late pagination or sitemap failure cannot leak a partial result
to the normal persistence pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import os
import re
import secrets
from collections.abc import AsyncIterator, Iterable
from typing import Any
from urllib.parse import quote, urlencode, unquote, urljoin, urlsplit
from xml.etree import ElementTree

import httpx
import structlog

from src.product_policy import offer_from_quantity
from src.scrapers.base import (
    BaseScraper,
    ScrapedProduct,
    SiteScrapeFatalError,
    fatal_proxy_reason,
)

log = structlog.get_logger()

PUBLIC_CATALOG_ROUTE = "__public_api_catalog__"
PUBLIC_API_AVAILABILITY_SOURCE = "pharmonline_public_api_total_count"
_BASE_URL = "https://pharmonline.az"
_METEOR_ID_RE = re.compile(r"^[A-Za-z0-9]{17}$")
_PRODUCT_SITEMAP_RE = re.compile(r"/sitemap-products-\d+\.xml$", re.I)
_XML_LOC_TAG = "loc"
_ORIGIN_CONTEXT_HEADERS = (
    "cache-control",
    "vary",
    "cf-cache-status",
    "x-cache",
)
_MAX_CRAWLBASE_ATTEMPTS = 2
# A Crawlbase browser context has proved stable for short runs but can return a
# different paginator state after dozens of product requests.  Keep each
# bounded page group sticky, then let the full API metadata, uniqueness and
# sitemap proofs reject any mixed or incomplete catalog before persistence.
_CATALOG_SESSION_PAGE_SPAN = 20
# Direct Decodo HTTP maps each 10-page group to a configured sticky proxy port.
# Each logical group still owns a fresh HTTP client/cookie jar, even when a
# small configured port pool eventually reuses the same exit. The anchors and
# full source+sitemap proof remain mandatory before yielding one product.
_DECODO_CATALOG_SESSION_PAGE_SPAN = 10
_MAX_DECODO_STICKY_PORTS = 64
_PUBLIC_API_TRANSPORT_ENV = "PHARMONLINE_PUBLIC_API_TRANSPORT"
_CRAWLBASE_TRANSPORT = "crawlbase"
_DECODO_TRANSPORT = "decodo"


class PharmonlinePublicAPIError(RuntimeError):
    """The public source did not prove a complete, stable catalog."""


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "required"}


def _configured_decodo_ports() -> tuple[int, ...]:
    """Read the existing Decodo sticky-port configuration without logging it."""
    sites = {
        value.strip() for value in os.environ.get("DECODO_SITES", "").split(",") if value.strip()
    }
    if "pharmonline" not in sites:
        raise SiteScrapeFatalError("Decodo is not configured for Pharmonline")

    raw_ports = os.environ.get("DECODO_PORTS", "30001-30010")
    ports: list[int] = []
    for token in raw_ports.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            try:
                start, end = int(start_raw), int(end_raw)
            except ValueError as exc:
                raise SiteScrapeFatalError("Decodo port range is invalid") from exc
            if start < 1 or end < start or end > 65535:
                raise SiteScrapeFatalError("Decodo port range is invalid")
            if len(ports) + end - start + 1 > _MAX_DECODO_STICKY_PORTS:
                raise SiteScrapeFatalError("Decodo has too many configured sticky ports")
            ports.extend(range(start, end + 1))
            continue
        try:
            port = int(token)
        except ValueError as exc:
            raise SiteScrapeFatalError("Decodo port is invalid") from exc
        if port < 1 or port > 65535:
            raise SiteScrapeFatalError("Decodo port is invalid")
        ports.append(port)
        if len(ports) > _MAX_DECODO_STICKY_PORTS:
            raise SiteScrapeFatalError("Decodo has too many configured sticky ports")

    unique_ports = tuple(dict.fromkeys(ports))
    if not unique_ports:
        raise SiteScrapeFatalError("Decodo has no configured sticky ports")
    return unique_ports


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _canonical_product_url(value: Any, *, source_is_path: bool) -> str | None:
    """Canonicalize a public product path or a sitemap product URL.

    The sitemap can contain locale-prefixed URLs.  Persistence uses the
    locale-free URL that the existing DDP catalog already trusts.
    """
    raw = _text(value)
    if raw is None:
        return None
    parsed = urlsplit(raw)
    if source_is_path:
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return None
        path = parsed.path
    else:
        host = parsed.netloc.lower().removeprefix("www.")
        if host not in {"", "pharmonline.az"} or parsed.query or parsed.fragment:
            return None
        path = parsed.path

    decoded_path = unquote(path).strip().rstrip("/")
    for locale in ("az", "en", "ru"):
        prefix = f"/{locale}/product/"
        if decoded_path.startswith(prefix):
            decoded_path = "/product/" + decoded_path[len(prefix) :]
            break
    if source_is_path:
        decoded_path = "/product/" + decoded_path.lstrip("/")
    if not decoded_path.startswith("/product/"):
        return None
    slug = decoded_path[len("/product/") :].strip("/")
    if not slug or "/" in slug:
        return None
    return f"{_BASE_URL}/product/{quote(slug, safe='-._~')}"


def _same_origin_sitemap_url(value: Any) -> str | None:
    """Return a safe absolute Pharmonline sitemap URL, or reject it.

    Sitemap indexes are remote input.  Do not let a malformed or compromised
    index turn this recovery path into an arbitrary Crawlbase fetch.
    """
    raw = _text(value)
    if raw is None:
        return None
    resolved = urljoin(f"{_BASE_URL}/", raw)
    parsed = urlsplit(resolved)
    host = parsed.netloc.lower().removeprefix("www.")
    if parsed.scheme != "https" or host != "pharmonline.az" or parsed.query or parsed.fragment:
        return None
    return f"{_BASE_URL}{parsed.path}"


def _iter_xml_locs(document: str) -> Iterable[str]:
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise PharmonlinePublicAPIError("invalid_sitemap_xml") from exc
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != _XML_LOC_TAG:
            continue
        text = _text(element.text)
        if text is not None:
            yield text


def _json_from_rendered_body(body: Any) -> dict | list:
    """Parse a Crawlbase target body without accepting arbitrary HTML.

    A JS token can make Chromium display JSON inside a ``<pre>`` element.
    Treat that one deterministic representation as JSON; all other non-JSON
    content is a source failure rather than a candidate catalog.
    """
    if isinstance(body, (dict, list)):
        return body
    if not isinstance(body, str):
        raise PharmonlinePublicAPIError("public_api_body_missing")
    raw_body = body
    pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", body, re.I | re.S)
    if pre_match:
        raw_body = html.unescape(pre_match.group(1))
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise PharmonlinePublicAPIError("public_api_body_not_json") from exc
    if not isinstance(payload, (dict, list)):
        raise PharmonlinePublicAPIError("public_api_json_shape_invalid")
    return payload


class PharmonlinePublicAPIScraper(BaseScraper):
    """Read the public catalog through one explicit transport and prove it first."""

    site_name = "pharmonline"
    base_url = _BASE_URL
    page_size = 100
    max_pages = 200
    sort_by = "name_asc"

    async def __aenter__(self) -> "PharmonlinePublicAPIScraper":  # type: ignore[override]
        if not _env_enabled("PHARMONLINE_PUBLIC_API"):
            raise SiteScrapeFatalError("Pharmonline public API mode is not explicitly enabled")
        transport = os.environ.get(_PUBLIC_API_TRANSPORT_ENV, _CRAWLBASE_TRANSPORT).strip().lower()
        if transport not in {_CRAWLBASE_TRANSPORT, _DECODO_TRANSPORT}:
            raise SiteScrapeFatalError("Pharmonline public API transport is not supported")

        self._public_api_transport = transport
        self._crawlbase_session = secrets.token_hex(16)
        self._catalog_sessions: dict[int, str] = {}
        self._origin_contexts: dict[str, set[str]] = {}
        self._client: httpx.AsyncClient | None = None
        self._decodo_clients: dict[str, httpx.AsyncClient] = {}
        self._decodo_session_ports: dict[str, int] = {}

        if transport == _CRAWLBASE_TRANSPORT:
            token = _text(os.environ.get("CRAWLBASE_JS_TOKEN"))
            if token is None:
                raise SiteScrapeFatalError("Crawlbase JS token is not configured")
            self._crawlbase_token = token
            # Non-product requests retain one short-lived context. Product
            # pages use a bounded sticky context per page group in
            # ``_fetch_catalog``.
            self._client = httpx.AsyncClient(timeout=120.0)
        else:
            username = _text(os.environ.get("DECODO_USERNAME"))
            password = os.environ.get("DECODO_PASSWORD")
            host = _text(os.environ.get("DECODO_HOST", "az.decodo.com"))
            if (
                username is None
                or not password
                or host is None
                or any(character in host for character in ":/@?#")
                or any(character.isspace() for character in host)
            ):
                raise SiteScrapeFatalError("Decodo credentials are not configured")
            self._decodo_username = username
            self._decodo_password = password
            self._decodo_host = host
            self._decodo_ports = _configured_decodo_ports()
            self._decodo_default_session = f"decodo-category-{secrets.token_hex(16)}"
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()
            self._client = None
        for decodo_client in getattr(self, "_decodo_clients", {}).values():
            await decodo_client.aclose()
        self._decodo_clients = {}
        self._decodo_session_ports = {}

    def _transport_name(self) -> str:
        """Use Crawlbase by default for backwards-compatible unit fixtures."""
        return getattr(self, "_public_api_transport", _CRAWLBASE_TRANSPORT)

    def _catalog_session_page_span(self) -> int:
        if self._transport_name() == _DECODO_TRANSPORT:
            return _DECODO_CATALOG_SESSION_PAGE_SPAN
        return _CATALOG_SESSION_PAGE_SPAN

    def _product_api_url(self, page: int) -> str:
        query = urlencode(
            {
                "lng": "az",
                "page": str(page),
                "limit": str(self.page_size),
                "sortBy": self.sort_by,
            }
        )
        return f"{self.base_url}/api/products?{query}"

    def _record_origin_context(self, target_url: str, envelope: dict[str, Any]) -> None:
        """Record a non-sensitive fingerprint of selected origin cache headers."""
        raw_headers = envelope.get("original_headers")
        if not isinstance(raw_headers, dict):
            return
        self._record_origin_headers(target_url, raw_headers)

    def _record_origin_headers(self, target_url: str, raw_headers: Any) -> None:
        """Record selected response headers without storing proxy cookies."""
        if not isinstance(raw_headers, dict) and not hasattr(raw_headers, "items"):
            return
        headers = {str(name).lower(): _text(value) for name, value in raw_headers.items()}
        selected = tuple(
            (name, headers[name])
            for name in _ORIGIN_CONTEXT_HEADERS
            if headers.get(name) is not None
        )
        if not selected:
            return
        fingerprint = hashlib.sha256(repr(selected).encode("utf-8")).hexdigest()[:12]
        resource = urlsplit(target_url).path
        contexts: dict[str, set[str]] = getattr(self, "_origin_contexts", {})
        contexts.setdefault(resource, set()).add(fingerprint)
        self._origin_contexts = contexts

    def _origin_context_evidence(self) -> str:
        contexts: dict[str, set[str]] = getattr(self, "_origin_contexts", {})
        if not contexts:
            return "none"
        return ",".join(
            f"{resource}:{len(fingerprints)}" for resource, fingerprints in sorted(contexts.items())
        )

    def _catalog_session_for_page(self, page: int) -> str:
        """Return a bounded sticky context for one product-page group."""
        if page < 1:
            raise PharmonlinePublicAPIError("products_page_number_invalid")
        span = self._catalog_session_page_span()
        chunk = (page - 1) // span
        sessions: dict[int, str] = getattr(self, "_catalog_sessions", {})
        session = sessions.get(chunk)
        if session is None:
            if self._transport_name() == _DECODO_TRANSPORT:
                session = f"decodo-catalog-{chunk}-{secrets.token_hex(16)}"
            else:
                session = secrets.token_hex(16)
            sessions[chunk] = session
            self._catalog_sessions = sessions
        return session

    def _sitemap_session(self) -> str:
        """Keep sitemap traffic out of every catalog context."""
        if self._transport_name() == _DECODO_TRANSPORT:
            return f"decodo-sitemap-{secrets.token_hex(16)}"
        return secrets.token_hex(16)

    async def _crawlbase_body(
        self,
        target_url: str,
        *,
        accept: str,
        crawlbase_session: str | None = None,
    ) -> Any:
        client: httpx.AsyncClient | None = getattr(self, "_client", None)
        if client is None:
            raise PharmonlinePublicAPIError("public_api_client_not_open")
        params = {
            "token": self._crawlbase_token,
            "url": target_url,
            "request_headers": f"accept:{accept}",
            # A transient retry intentionally retains the exact session used
            # by the page.  A new session is permitted only at an explicit
            # product-page-group boundary, never as an implicit retry.
            "cookies_session": crawlbase_session or self._crawlbase_session,
            "get_headers": "true",
            "format": "json",
        }
        for attempt in range(1, _MAX_CRAWLBASE_ATTEMPTS + 1):
            try:
                response = await client.get("https://api.crawlbase.com/", params=params)
                break
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == _MAX_CRAWLBASE_ATTEMPTS:
                    raise PharmonlinePublicAPIError("crawlbase_transient_request_failed") from exc
                # A single timeout is transport noise, not permission to
                # accept a partial catalog. Retry the exact same request once
                # in the same sticky session; all catalog invariants are still
                # checked before one product can be persisted.
                log.warning(
                    "pharmonline_public_api_transport_retry",
                    resource=urlsplit(target_url).path,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(1)
            except httpx.HTTPError as exc:
                raise PharmonlinePublicAPIError("crawlbase_request_failed") from exc
        if response.status_code in {402, 407}:
            raise SiteScrapeFatalError(
                f"Crawlbase proxy access rejected: HTTP {response.status_code}"
            )
        if response.status_code != 200:
            raise PharmonlinePublicAPIError(f"crawlbase_http_{response.status_code}")
        try:
            envelope = response.json()
        except json.JSONDecodeError as exc:
            raise PharmonlinePublicAPIError("crawlbase_envelope_not_json") from exc
        if not isinstance(envelope, dict):
            raise PharmonlinePublicAPIError("crawlbase_envelope_invalid")
        cb_status = str(envelope.get("cb_status") or "")
        original_status = str(envelope.get("original_status") or "")
        if cb_status in {"402", "407"} or original_status in {"402", "407"}:
            rejected = cb_status if cb_status in {"402", "407"} else original_status
            raise SiteScrapeFatalError(f"Crawlbase proxy access rejected: HTTP {rejected}")
        if cb_status != "200" or original_status != "200":
            raise PharmonlinePublicAPIError(
                f"crawlbase_target_status_{cb_status or 'missing'}_{original_status or 'missing'}"
            )
        if "body" not in envelope:
            raise PharmonlinePublicAPIError("crawlbase_envelope_body_missing")
        self._record_origin_context(target_url, envelope)
        return envelope["body"]

    async def _crawlbase_json(
        self,
        target_url: str,
        *,
        crawlbase_session: str | None = None,
    ) -> dict | list:
        return _json_from_rendered_body(
            await self._crawlbase_body(
                target_url,
                accept="application/json",
                crawlbase_session=crawlbase_session,
            )
        )

    def _decodo_context(self, session: str | None) -> str:
        if session is not None:
            if not session.startswith("decodo-"):
                raise PharmonlinePublicAPIError("decodo_session_context_invalid")
            return session
        context = getattr(self, "_decodo_default_session", None)
        if not isinstance(context, str) or not context.startswith("decodo-"):
            context = f"decodo-category-{secrets.token_hex(16)}"
            self._decodo_default_session = context
        return context

    def _decodo_port_for_context(self, context: str) -> int:
        ports: tuple[int, ...] = getattr(self, "_decodo_ports", ())
        if not ports:
            raise SiteScrapeFatalError("Decodo sticky ports are not configured")
        sessions: dict[str, int] = getattr(self, "_decodo_session_ports", {})
        port = sessions.get(context)
        if port is None:
            # Rotate only when allocating a new logical context. Retries reuse
            # both the exact port and the exact HTTP client/cookie jar.
            port = ports[len(sessions) % len(ports)]
            sessions[context] = port
            self._decodo_session_ports = sessions
        return port

    def _decodo_client_for_context(self, context: str, port: int) -> httpx.AsyncClient:
        clients: dict[str, httpx.AsyncClient] = getattr(self, "_decodo_clients", {})
        client = clients.get(context)
        if client is None:
            username = quote(getattr(self, "_decodo_username", ""), safe="")
            password = quote(getattr(self, "_decodo_password", ""), safe="")
            host = getattr(self, "_decodo_host", "")
            if not username or not password or not host:
                raise SiteScrapeFatalError("Decodo credentials are not configured")
            client = httpx.AsyncClient(
                proxy=f"http://{username}:{password}@{host}:{port}",
                timeout=120.0,
                trust_env=False,
            )
            clients[context] = client
            self._decodo_clients = clients
        return client

    async def _decodo_body(
        self,
        target_url: str,
        *,
        accept: str,
        crawlbase_session: str | None = None,
    ) -> str:
        """Fetch the native public response through one configured Decodo exit."""
        context = self._decodo_context(crawlbase_session)
        port = self._decodo_port_for_context(context)
        client = self._decodo_client_for_context(context, port)
        for attempt in range(1, _MAX_CRAWLBASE_ATTEMPTS + 1):
            try:
                response = await client.get(target_url, headers={"accept": accept})
                break
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == _MAX_CRAWLBASE_ATTEMPTS:
                    raise PharmonlinePublicAPIError("decodo_transient_request_failed") from None
                log.warning(
                    "pharmonline_public_api_transport_retry",
                    transport=_DECODO_TRANSPORT,
                    resource=urlsplit(target_url).path,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(1)
            except httpx.HTTPError as exc:
                proxy_reason = fatal_proxy_reason(exc)
                if proxy_reason is not None:
                    raise SiteScrapeFatalError(proxy_reason) from None
                raise PharmonlinePublicAPIError("decodo_request_failed") from None
        if response.status_code in {402, 407}:
            raise SiteScrapeFatalError(f"Decodo proxy access rejected: HTTP {response.status_code}")
        if response.status_code != 200:
            raise PharmonlinePublicAPIError(f"decodo_http_{response.status_code}")
        self._record_origin_headers(target_url, response.headers)
        return response.text

    async def _source_json(
        self,
        target_url: str,
        *,
        crawlbase_session: str | None = None,
    ) -> dict | list:
        if self._transport_name() == _DECODO_TRANSPORT:
            return _json_from_rendered_body(
                await self._decodo_body(
                    target_url,
                    accept="application/json",
                    crawlbase_session=crawlbase_session,
                )
            )
        return await self._crawlbase_json(target_url, crawlbase_session=crawlbase_session)

    async def _crawlbase_xml(
        self,
        target_url: str,
        *,
        crawlbase_session: str | None = None,
    ) -> str:
        body = await self._crawlbase_body(
            target_url,
            accept="application/xml,text/xml;q=0.9,*/*;q=0.8",
            crawlbase_session=crawlbase_session,
        )
        if not isinstance(body, str):
            raise PharmonlinePublicAPIError("sitemap_body_not_text")
        pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", body, re.I | re.S)
        return html.unescape(pre_match.group(1)) if pre_match else body

    async def _source_xml(
        self,
        target_url: str,
        *,
        crawlbase_session: str | None = None,
    ) -> str:
        if self._transport_name() == _DECODO_TRANSPORT:
            return await self._decodo_body(
                target_url,
                accept="application/xml,text/xml;q=0.9,*/*;q=0.8",
                crawlbase_session=crawlbase_session,
            )
        return await self._crawlbase_xml(target_url, crawlbase_session=crawlbase_session)

    @staticmethod
    def _page_payload(payload: dict | list) -> tuple[list[dict], int, int]:
        if not isinstance(payload, dict):
            raise PharmonlinePublicAPIError("products_payload_not_object")
        rows = payload.get("data")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise PharmonlinePublicAPIError("products_data_invalid")
        try:
            total = int(payload["total"])
            pages = int(payload["pages"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PharmonlinePublicAPIError("products_metadata_invalid") from exc
        if total < 1 or pages < 1:
            raise PharmonlinePublicAPIError("products_metadata_empty")
        return rows, total, pages

    @staticmethod
    def _page_identity_records(rows: list[dict]) -> tuple[tuple[str, str], ...]:
        """Return ordered immutable identities for a page, or fail closed."""
        records: list[tuple[str, str]] = []
        for row in rows:
            external_id = _text(row.get("_id"))
            url = _canonical_product_url(row.get("path"), source_is_path=True)
            if external_id is None or _METEOR_ID_RE.fullmatch(external_id) is None:
                raise PharmonlinePublicAPIError("products_invalid_external_id")
            if url is None:
                raise PharmonlinePublicAPIError("products_invalid_product_path")
            records.append((external_id, url))
        if len({external_id for external_id, _ in records}) != len(records):
            raise PharmonlinePublicAPIError("products_duplicate_external_id")
        if len({url for _, url in records}) != len(records):
            raise PharmonlinePublicAPIError("products_duplicate_product_url")
        return tuple(records)

    @staticmethod
    def _category_map(payload: dict | list) -> dict[str, str]:
        if isinstance(payload, dict):
            rows = payload.get("data")
        else:
            rows = payload
        if not isinstance(rows, list):
            return {}
        out: dict[str, str] = {}
        pending = list(rows)
        while pending:
            row = pending.pop()
            if not isinstance(row, dict):
                continue
            category_id = _text(row.get("_id") or row.get("id"))
            path = _text(row.get("path"))
            if category_id and path and "/" not in path.strip("/"):
                out[category_id] = path.strip("/")
            for key in ("children", "subCategories", "subcategories"):
                nested = row.get(key)
                if isinstance(nested, list):
                    pending.extend(nested)
        return out

    async def _fetch_catalog(self) -> tuple[list[dict], int, int]:
        # A scraper instance is normally used once, but reset this map so a
        # retry in the same process never turns a short chunk context into a
        # long-lived one.
        self._catalog_sessions = {}
        first_payload = await self._source_json(
            self._product_api_url(1),
            crawlbase_session=self._catalog_session_for_page(1),
        )
        first_rows, expected_total, expected_pages = self._page_payload(first_payload)
        first_page_records = self._page_identity_records(first_rows)
        if expected_pages > self.max_pages:
            raise PharmonlinePublicAPIError("products_pages_exceed_safety_limit")
        if expected_pages != math.ceil(expected_total / self.page_size):
            raise PharmonlinePublicAPIError("products_pages_total_mismatch")

        rows: list[dict] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        for page in range(1, expected_pages + 1):
            page_session = self._catalog_session_for_page(page)
            if page > 1 and (page - 1) % self._catalog_session_page_span() == 0:
                # A fresh page-group context must first prove it sees the
                # exact same ordered page one as the initial context. This
                # prevents a different catalog variant from silently joining
                # the buffered output; this anchor is not added twice below.
                anchor_payload = await self._source_json(
                    self._product_api_url(1),
                    crawlbase_session=page_session,
                )
                anchor_rows, anchor_total, anchor_pages = self._page_payload(anchor_payload)
                if anchor_total != expected_total or anchor_pages != expected_pages:
                    raise PharmonlinePublicAPIError("products_chunk_anchor_metadata_changed")
                if self._page_identity_records(anchor_rows) != first_page_records:
                    raise PharmonlinePublicAPIError("products_chunk_anchor_changed")
            payload = (
                first_payload
                if page == 1
                else await self._source_json(
                    self._product_api_url(page),
                    crawlbase_session=page_session,
                )
            )
            page_rows, total, pages = self._page_payload(payload)
            if total != expected_total or pages != expected_pages:
                raise PharmonlinePublicAPIError("products_metadata_changed_during_pagination")
            expected_items = (
                self.page_size
                if page < expected_pages
                else expected_total - self.page_size * (expected_pages - 1)
            )
            if len(page_rows) != expected_items:
                raise PharmonlinePublicAPIError("products_page_size_mismatch")
            for row, (external_id, url) in zip(
                page_rows,
                self._page_identity_records(page_rows),
                strict=True,
            ):
                if external_id in seen_ids:
                    raise PharmonlinePublicAPIError("products_duplicate_external_id")
                if url in seen_urls:
                    raise PharmonlinePublicAPIError("products_duplicate_product_url")
                seen_ids.add(external_id)
                seen_urls.add(url)
                rows.append(row)
        if len(rows) != expected_total or len(seen_ids) != expected_total:
            raise PharmonlinePublicAPIError("products_total_coverage_mismatch")
        return rows, expected_total, expected_pages

    async def _fetch_sitemap_product_urls(self) -> set[str]:
        # Sitemap traffic must not extend either a catalog chunk or the
        # category-map context. It receives its own bounded sticky session,
        # matching the no-write preflight proof.
        sitemap_session = self._sitemap_session()
        index_url = f"{self.base_url}/sitemap.xml"
        index_locs = list(
            _iter_xml_locs(
                await self._source_xml(
                    index_url,
                    crawlbase_session=sitemap_session,
                )
            )
        )
        sitemap_urls: set[str] = set()
        for loc in index_locs:
            sitemap_url = _same_origin_sitemap_url(loc)
            if sitemap_url is None:
                raise PharmonlinePublicAPIError("product_sitemap_index_url_invalid")
            if _PRODUCT_SITEMAP_RE.search(urlsplit(sitemap_url).path):
                sitemap_urls.add(sitemap_url)
        if not sitemap_urls:
            raise PharmonlinePublicAPIError("product_sitemap_index_missing")
        product_urls: set[str] = set()
        for sitemap_url in sorted(sitemap_urls):
            child_locs = list(
                _iter_xml_locs(
                    await self._source_xml(
                        sitemap_url,
                        crawlbase_session=sitemap_session,
                    )
                )
            )
            if not child_locs:
                raise PharmonlinePublicAPIError("product_sitemap_empty")
            for loc in child_locs:
                canonical_url = _canonical_product_url(loc, source_is_path=False)
                if canonical_url is None:
                    raise PharmonlinePublicAPIError("product_sitemap_product_url_invalid")
                product_urls.add(canonical_url)
        if not product_urls:
            raise PharmonlinePublicAPIError("product_sitemap_urls_missing")
        return product_urls

    @staticmethod
    def _country_code(raw: dict) -> str | None:
        value = raw.get("manufacturerCountryData")
        if isinstance(value, list):
            value = next((item for item in value if isinstance(item, dict)), None)
        if not isinstance(value, dict):
            return None
        candidate = _text(value.get("geocode") or value.get("code"))
        if candidate is None or len(candidate) != 2 or not candidate.isalpha():
            return None
        return candidate.lower()

    @staticmethod
    def _product_category(raw: dict, category_id_to_path: dict[str, str]) -> str | None:
        categories = raw.get("category")
        if not isinstance(categories, list):
            categories = [categories]
        for category_id in categories:
            category = category_id_to_path.get(str(category_id))
            if category:
                return category
        return None

    def _build_product(
        self,
        raw: dict,
        category_id_to_path: dict[str, str],
    ) -> ScrapedProduct | None:
        external_id = _text(raw.get("_id"))
        url = _canonical_product_url(raw.get("path"), source_is_path=True)
        if external_id is None or _METEOR_ID_RE.fullmatch(external_id) is None or url is None:
            return None
        i18n = raw.get("i18n") if isinstance(raw.get("i18n"), dict) else {}
        localized = i18n.get("az") if isinstance(i18n.get("az"), dict) else {}
        name = _text(localized.get("name")) or _text(raw.get("name"))
        if name is None:
            return None
        description = _text(localized.get("description"))
        regular_price = _as_number(raw.get("minPrice"))
        current_price = _as_number(raw.get("totalMinPrice"))
        if current_price is None:
            current_price = regular_price
        is_on_sale = (
            regular_price is not None
            and current_price is not None
            and regular_price > current_price + 0.01
        )
        barcode = _text(raw.get("barcode"))
        if barcode is not None and not barcode.isdigit():
            barcode = None
        country_code = self._country_code(raw)
        availability_status, offer_quantity = offer_from_quantity(raw.get("totalCount"))
        return ScrapedProduct(
            site=self.site_name,
            external_id=external_id,
            url=url,
            name=name[:500],
            identity_verified=True,
            manufacturer_country_raw=country_code,
            country_source=("pharmonline_public_api_country" if country_code else None),
            offer_availability_status=availability_status,
            offer_quantity=offer_quantity,
            availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
            category=self._product_category(raw, category_id_to_path),
            description=description,
            price=regular_price if is_on_sale else current_price,
            discount_price=current_price if is_on_sale else None,
            is_on_sale=is_on_sale,
            barcode=barcode,
        )

    async def scrape_category(
        self, category_slug: str, limit: int | None = None
    ) -> AsyncIterator[ScrapedProduct]:
        """Buffer one verified global catalog under the synthetic route."""
        if category_slug != PUBLIC_CATALOG_ROUTE or limit is not None:
            self._set_route_status(
                category_slug,
                complete=False,
                abort_reason="public_api_route_or_limit_invalid",
            )
            return
        try:
            # Category names enrich existing records, but are not catalog
            # identity.  A transient category-tree failure must not weaken the
            # product API + sitemap proof or overwrite old categories with a
            # guessed value.  Continue with an empty map, preserving existing
            # category values during persistence.
            category_id_to_path: dict[str, str] = {}
            try:
                category_payload = await self._source_json(
                    f"{self.base_url}/api/categories?type=category"
                )
                category_id_to_path = self._category_map(category_payload)
            except PharmonlinePublicAPIError as exc:
                log.warning(
                    "pharmonline_public_api_category_map_unavailable",
                    reason=str(exc)[:200],
                )
            raw_rows, expected_total, expected_pages = await self._fetch_catalog()
            products = [self._build_product(raw, category_id_to_path) for raw in raw_rows]
            if any(product is None for product in products):
                raise PharmonlinePublicAPIError("products_mapping_failed")
            catalog = [product for product in products if product is not None]
            api_urls = {product.url for product in catalog}
            sitemap_urls = await self._fetch_sitemap_product_urls()
            if len(catalog) != expected_total or api_urls != sitemap_urls:
                raise PharmonlinePublicAPIError("products_sitemap_set_mismatch")
        except SiteScrapeFatalError:
            raise
        except PharmonlinePublicAPIError as exc:
            reason = str(exc)[:200]
            self._set_route_status(
                category_slug,
                complete=False,
                abort_reason=reason,
            )
            log.warning(
                "pharmonline_public_api_catalog_rejected",
                reason=reason,
                origin_contexts=self._origin_context_evidence(),
            )
            return
        except Exception as exc:  # defensive boundary: never yield partial rows
            reason = f"public_api_unexpected_{type(exc).__name__}"[:200]
            self._set_route_status(
                category_slug,
                complete=False,
                abort_reason=reason,
            )
            log.warning(
                "pharmonline_public_api_catalog_failed",
                error_type=type(exc).__name__,
                origin_contexts=self._origin_context_evidence(),
            )
            return

        log.info(
            "pharmonline_public_api_catalog_verified",
            products=expected_total,
            pages=expected_pages,
            transport=self._transport_name(),
            catalog_session_page_span=self._catalog_session_page_span(),
            catalog_session_chunks=len(getattr(self, "_catalog_sessions", {})),
            origin_contexts=self._origin_context_evidence(),
        )
        self._set_route_status(
            category_slug,
            complete=True,
            expected_pages=expected_pages,
            visited_pages=expected_pages,
            raw_items=expected_total,
            parsed_items=expected_total,
            expected_items=expected_total,
        )
        for product in catalog:
            yield product
