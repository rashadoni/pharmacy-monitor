"""Verified full-catalog scraper for Pharmonline's public JSON API.

This source is deliberately restricted to the manual recovery flow.  Unlike
the legacy rendered-HTML path, it receives the site's native 17-character
product IDs.  Before yielding one product it buffers the entire catalog and
checks a set of invariants:

* every advertised API page is present and has the expected size;
* IDs and canonical product URLs are unique and total exactly matches;
* the product URL set matches the public product sitemap.

Consequently a late pagination or sitemap failure cannot leak a partial result
to the normal persistence pipeline.
"""

from __future__ import annotations

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
from src.scrapers.base import BaseScraper, ScrapedProduct, SiteScrapeFatalError

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


class PharmonlinePublicAPIError(RuntimeError):
    """The public source did not prove a complete, stable catalog."""


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "required"}


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
    """Read the public catalog via Crawlbase and prove it before yielding."""

    site_name = "pharmonline"
    base_url = _BASE_URL
    page_size = 100
    max_pages = 200
    sort_by = "name_asc"

    async def __aenter__(self) -> "PharmonlinePublicAPIScraper":  # type: ignore[override]
        if not _env_enabled("PHARMONLINE_PUBLIC_API"):
            raise SiteScrapeFatalError("Pharmonline public API mode is not explicitly enabled")
        token = _text(os.environ.get("CRAWLBASE_JS_TOKEN"))
        if token is None:
            raise SiteScrapeFatalError("Crawlbase JS token is not configured")
        self._crawlbase_token = token
        # Keep API pagination and sitemap requests in one Crawlbase context.
        # Pharmonline can vary its public catalog by browser/cookie/exit state;
        # a fresh, opaque 32-character session prevents pages from being
        # assembled from unrelated contexts while preserving the existing
        # fail-closed API+sitemap proof.
        self._crawlbase_session = secrets.token_hex(16)
        self._origin_contexts: dict[str, set[str]] = {}
        self._client = httpx.AsyncClient(timeout=120.0)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()
            self._client = None

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
            f"{resource}:{len(fingerprints)}"
            for resource, fingerprints in sorted(contexts.items())
        )

    async def _crawlbase_body(self, target_url: str, *, accept: str) -> Any:
        client: httpx.AsyncClient | None = getattr(self, "_client", None)
        if client is None:
            raise PharmonlinePublicAPIError("public_api_client_not_open")
        params = {
            "token": self._crawlbase_token,
            "url": target_url,
            "request_headers": f"accept:{accept}",
            "cookies_session": self._crawlbase_session,
            "get_headers": "true",
            "format": "json",
        }
        try:
            response = await client.get("https://api.crawlbase.com/", params=params)
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

    async def _crawlbase_json(self, target_url: str) -> dict | list:
        return _json_from_rendered_body(
            await self._crawlbase_body(target_url, accept="application/json")
        )

    async def _crawlbase_xml(self, target_url: str) -> str:
        body = await self._crawlbase_body(
            target_url,
            accept="application/xml,text/xml;q=0.9,*/*;q=0.8",
        )
        if not isinstance(body, str):
            raise PharmonlinePublicAPIError("sitemap_body_not_text")
        pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", body, re.I | re.S)
        return html.unescape(pre_match.group(1)) if pre_match else body

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
        first_payload = await self._crawlbase_json(self._product_api_url(1))
        first_rows, expected_total, expected_pages = self._page_payload(first_payload)
        if expected_pages > self.max_pages:
            raise PharmonlinePublicAPIError("products_pages_exceed_safety_limit")
        if expected_pages != math.ceil(expected_total / self.page_size):
            raise PharmonlinePublicAPIError("products_pages_total_mismatch")

        rows: list[dict] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        for page in range(1, expected_pages + 1):
            payload = (
                first_payload
                if page == 1
                else await self._crawlbase_json(self._product_api_url(page))
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
            for row in page_rows:
                external_id = _text(row.get("_id"))
                url = _canonical_product_url(row.get("path"), source_is_path=True)
                if external_id is None or _METEOR_ID_RE.fullmatch(external_id) is None:
                    raise PharmonlinePublicAPIError("products_invalid_external_id")
                if url is None:
                    raise PharmonlinePublicAPIError("products_invalid_product_path")
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
        index_url = f"{self.base_url}/sitemap.xml"
        index_locs = list(_iter_xml_locs(await self._crawlbase_xml(index_url)))
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
            child_locs = list(_iter_xml_locs(await self._crawlbase_xml(sitemap_url)))
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
                category_payload = await self._crawlbase_json(
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
