"""AI-driven crawler — Level 3 of the AI scraping ladder.

Strategy:
  1. Discover URLs via sitemap.xml (preferred) or BFS over <a> links
  2. Classify each page: "product" / "category" / "other"
     - Cheap heuristic first (DOM markers like schema.org/Product, price classes)
     - LLM fallback only if heuristics ambiguous
  3. Extract structured product from HTML via LLM (gpt-4o-mini / claude-3.5-haiku)
  4. Stream into the regular persist_results() pipeline

Why this is useful:
  - Don't need pre-configured categories — discover automatically
  - Survives site redesigns (LLM is robust to selector changes)
  - Plugs into existing infrastructure (BaseScraper interface)

Cost guardrails:
  - AI_CRAWL_BUDGET_USD env (default 5.0) — abort when exceeded
  - URL deduplication via in-memory + DB cache
  - Skip URLs that look like category lists (don't extract product from them)
  - Cap pages per crawl session via --max-urls

Provider abstraction:
  - AI_CRAWL_PROVIDER=openai (default) | anthropic
  - AI_CRAWL_MODEL=gpt-4o-mini | claude-haiku-4-5
  - OPENAI_API_KEY or ANTHROPIC_API_KEY required
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import structlog
from playwright.async_api import Page

from src.scrapers.base import (
    BaseScraper,
    CaptchaDetected,
    ScrapedProduct,
    ScrapeResult,
)

log = structlog.get_logger()

# ─── Cost model (per 1M tokens, USD) ────────────────────────────────────────
# Update when model prices change. Source: provider pricing pages.
PROVIDER_PRICING = {
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "openai/gpt-4o": {"input": 2.50, "output": 10.00},
    "anthropic/claude-haiku-4-5": {"input": 0.25, "output": 1.25},
    "anthropic/claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
}


@dataclass
class CrawlSession:
    """In-memory state for one crawl run.

    Tracks: URLs visited, products yielded, total tokens spent (for budget guard).
    """

    site_name: str
    base_url: str
    visited: set[str] = field(default_factory=set)
    queued: list[str] = field(default_factory=list)
    products_yielded: int = 0
    failed_urls: list[str] = field(default_factory=list)
    captcha_urls: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def estimated_cost_usd(self) -> float:
        provider = os.getenv("AI_CRAWL_PROVIDER", "openai")
        model = os.getenv("AI_CRAWL_MODEL", "gpt-4o-mini")
        key = f"{provider}/{model}"
        pricing = PROVIDER_PRICING.get(key, {"input": 0.15, "output": 0.60})
        return (self.input_tokens / 1_000_000) * pricing["input"] + (
            self.output_tokens / 1_000_000
        ) * pricing["output"]


# ─── URL discovery ───────────────────────────────────────────────────────────


async def fetch_sitemap_urls(base_url: str, page: Page) -> list[str]:
    """Try common sitemap locations, return all <loc> URLs.

    Common paths checked: /sitemap.xml, /sitemap_index.xml, /robots.txt → Sitemap line.
    Returns up to 50K URLs (cap to avoid memory blow-up on huge sites).
    """
    candidates = [
        f"{base_url.rstrip('/')}/sitemap.xml",
        f"{base_url.rstrip('/')}/sitemap_index.xml",
        f"{base_url.rstrip('/')}/sitemaps/sitemap.xml",
    ]
    seen: set[str] = set()
    out: list[str] = []

    # robots.txt may declare sitemap location
    try:
        robots_resp = await page.goto(f"{base_url.rstrip('/')}/robots.txt", timeout=10000)
        if robots_resp and robots_resp.ok:
            text = await robots_resp.text()
            for line in re.findall(r"(?im)^Sitemap:\s*(\S+)", text):
                candidates.insert(0, line.strip())
    except Exception:
        pass

    # Try every candidate; sites often split products/blog/categories into
    # separate sitemaps, so merge URLs from all that respond.
    tried: set[str] = set()
    for sm_url in candidates:
        if sm_url in tried:
            continue
        tried.add(sm_url)
        try:
            resp = await page.goto(sm_url, timeout=15000)
            if not resp or not resp.ok:
                continue
            # resp.text() returns raw body; page.content() returns Chromium's
            # rendered HTML view which strips the <?xml declaration.
            xml = await resp.text()
            m = re.search(r"<\?xml.*?</(?:urlset|sitemapindex)>", xml, re.DOTALL)
            if not m:
                continue
            try:
                root = ET.fromstring(m.group(0))
            except ET.ParseError:
                continue
            ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            before = len(out)

            # Sitemap-index → recurse into sub-sitemaps (but cap depth at 1)
            for sub in root.findall(".//s:sitemap/s:loc", ns):
                if sub.text and sub.text not in seen:
                    seen.add(sub.text)
                    sub_resp = await page.goto(sub.text, timeout=15000)
                    if sub_resp and sub_resp.ok:
                        sub_xml = await sub_resp.text()
                        sm2 = re.search(r"<\?xml.*?</urlset>", sub_xml, re.DOTALL)
                        if sm2:
                            try:
                                root2 = ET.fromstring(sm2.group(0))
                                for loc in root2.findall(".//s:url/s:loc", ns):
                                    if loc.text:
                                        out.append(loc.text.strip())
                            except ET.ParseError:
                                continue

            # Direct urlset
            for loc in root.findall(".//s:url/s:loc", ns):
                if loc.text:
                    out.append(loc.text.strip())

            added = len(out) - before
            if added:
                log.info("sitemap_loaded", url=sm_url, urls_added=added)
        except Exception as e:
            log.debug("sitemap_fetch_failed", url=sm_url, error=str(e))
            continue

    # Dedup while preserving order (some sitemaps overlap).
    deduped: list[str] = []
    seen_urls: set[str] = set()
    for u in out:
        if u not in seen_urls:
            seen_urls.add(u)
            deduped.append(u)
    return deduped[:50_000]


# ─── Page classification ────────────────────────────────────────────────────


_PRODUCT_HEURISTIC_MARKERS = [
    'itemtype="http://schema.org/Product"',
    'itemtype="https://schema.org/Product"',
    '"@type":"Product"',
    'property="og:type" content="product"',
    'property="product:price:amount"',
    "data-price",
    'name="twitter:label1" content="Price"',
]


def heuristic_is_product_page(html: str) -> bool:
    """Cheap O(n) string check — schema.org markers, og:type=product, etc.

    True positive: definitely a product page (skip LLM call, save $$).
    False / undetermined → caller may fall back to LLM classification.
    """
    sample = html[:30_000]  # check first 30KB only
    return any(marker in sample for marker in _PRODUCT_HEURISTIC_MARKERS)


# ─── JSON-LD short-circuit (preferred over LLM when site has schema.org) ─────


_JSONLD_PATTERN = re.compile(
    r'<script[^>]*type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def parse_jsonld_product(html: str) -> dict | None:
    """Extract main schema.org Product from JSON-LD scripts.

    Pattern works on most modern e-commerce — server-rendered for SEO.
    Pages often embed multiple Product entries (related items, "you might
    also like" carousels). The reliable signal for "this page IS a product
    page" is **exactly one** product with `offers.availability` set —
    related-product carousels typically omit availability, and listing /
    category pages either omit it or set it on every item.
    """
    products: list[dict] = []
    for raw in _JSONLD_PATTERN.findall(html):
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        items: list = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            graph = data.get("@graph")
            items = graph if isinstance(graph, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Product":
                products.append(item)

    with_avail = [
        p for p in products if isinstance(p.get("offers"), dict) and p["offers"].get("availability")
    ]
    if len(with_avail) == 1:
        return with_avail[0]
    return None


def _build_product_from_jsonld(site_name: str, url: str, jsonld: dict) -> ScrapedProduct | None:
    """Map schema.org Product JSON-LD → our ScrapedProduct dataclass."""
    name = jsonld.get("name")
    offers = jsonld.get("offers")
    if not name or not isinstance(offers, dict):
        return None
    try:
        price_raw = offers.get("price")
        price = float(price_raw) if price_raw not in (None, "") else None
    except (TypeError, ValueError):
        price = None
    if price is None:
        return None

    brand = jsonld.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")

    image = jsonld.get("image")
    image_url = image[0] if isinstance(image, list) and image else image
    if isinstance(image_url, dict):  # ImageObject schema variant
        image_url = image_url.get("url")

    sku = jsonld.get("sku")
    external_id = str(sku) if sku else hashlib.sha1(url.encode()).hexdigest()[:16]

    # Phase 2.2 — schema.org Product может содержать canonical barcode под
    # одним из ключей: gtin13 (EAN-13), gtin (general), gtin8, gtin12 (UPC),
    # gtin14, mpn (manufacturer part — НЕ barcode, но иногда сайты кладут туда).
    # Берём первое непустое цифровое значение, нормализуем (digits only).
    barcode = _extract_barcode_from_jsonld(jsonld)

    return ScrapedProduct(
        site=site_name,
        external_id=external_id,
        url=url,
        name=str(name)[:500],
        brand=str(brand)[:200] if brand else None,
        pack_size=None,
        dosage=None,
        image_url=str(image_url)[:500] if image_url else None,
        price=price,
        discount_price=None,
        is_on_sale=False,
        barcode=barcode,
    )


_BARCODE_KEYS = ("gtin13", "gtin14", "gtin12", "gtin8", "gtin")
_BARCODE_DIGITS_RE = re.compile(r"^\d{8,14}$")


def _extract_barcode_from_jsonld(jsonld: dict) -> str | None:
    """Return canonical barcode from schema.org Product, or None.

    schema.org defines multiple GTIN sub-types — try them in order of preference
    (most specific first). Strip whitespace, validate length 8-14 digits.
    `mpn` is intentionally skipped: in pharma it's manufacturer part-number,
    not always a barcode, and mixing the two would degrade matcher precision.
    """
    for key in _BARCODE_KEYS:
        val = jsonld.get(key)
        if not val:
            continue
        s = str(val).strip().replace(" ", "").replace("-", "")
        if _BARCODE_DIGITS_RE.match(s):
            return s
    return None


# ─── Next.js RSC stream short-circuit ────────────────────────────────────────
#
# Next.js 13+ App Router emits hydration data as ~20 chunks of the form
#   <script>self.__next_f.push([1, "...escaped JSON string..."])</script>
# When concatenated in document order they form a Flight payload that includes
# embedded schema.org JSON-LD (with one extra layer of escape). aloe.az puts
# its product schema there rather than in a standalone <script type="application/ld+json">.
# parse_jsonld_product() misses these because it only matches the dedicated tag.

_NEXT_RSC_PUSH_PATTERN = re.compile(r'self\.__next_f\.push\(\[\s*\d+\s*,\s*"')


def _decode_rsc_chunks(html: str) -> str:
    """Reconstruct the Next.js RSC stream by joining all push() string chunks.

    The HTML may contain non-string chunks (arrays, nulls) — those are skipped.
    Returns the empty string when no chunks decode.
    """
    parts: list[str] = []
    for m in _NEXT_RSC_PUSH_PATTERN.finditer(html):
        # m.end() is just past the opening quote of the JSON string literal.
        # Walk forward to the matching unescaped closing quote.
        i = m.end()
        n = len(html)
        while i < n:
            c = html[i]
            if c == "\\":
                i += 2
                continue
            if c == '"':
                break
            i += 1
        if i >= n:
            continue
        try:
            parts.append(json.loads(html[m.end() - 1 : i + 1]))
        except (json.JSONDecodeError, ValueError):
            continue
    return "".join(parts)


def _collect_products_from_obj(obj: object, sink: list[dict]) -> None:
    """Walk a JSON object and collect any schema.org Product entries."""
    if isinstance(obj, dict):
        if obj.get("@type") == "Product":
            sink.append(obj)
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                _collect_products_from_obj(item, sink)
    elif isinstance(obj, list):
        for item in obj:
            _collect_products_from_obj(item, sink)


def parse_next_rsc_jsonld(html: str) -> dict | None:
    """Extract schema.org Product from a Next.js RSC stream.

    Next.js App Router emits product JSON-LD via `dangerouslySetInnerHTML`,
    which serializes through the RSC stream as `"__html":"<escaped-json>"` —
    the JSON-LD object is encoded as a JSON string nested inside the chunks,
    so we need two json.loads passes (chunk literal → __html string → JSON-LD).

    Mirrors parse_jsonld_product()'s contract: returns the single Product dict
    that has offers.availability, or None when zero or multiple are present
    (the latter typically indicates a listing / related-items page).
    """
    payload = _decode_rsc_chunks(html)
    if not payload:
        return None

    decoder = json.JSONDecoder()
    products: list[dict] = []

    # Strategy A: dangerouslySetInnerHTML wrapper — the common Next.js pattern.
    # The JSON-LD is the value of __html, encoded as a JSON string.
    pos = 0
    while pos < len(payload):
        i = payload.find('"__html":', pos)
        if i == -1:
            break
        try:
            value, end = decoder.raw_decode(payload, i + len('"__html":'))
        except json.JSONDecodeError:
            pos = i + 1
            continue
        pos = end
        if not isinstance(value, str) or '"@type"' not in value:
            continue
        try:
            inner = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            continue
        _collect_products_from_obj(inner, products)

    # Strategy B: occasionally JSON-LD is embedded directly (unusual in RSC,
    # but covers edge cases like server-injected <script type="application/ld+json">
    # whose content survives into the stream as a plain object).
    pos = 0
    while pos < len(payload):
        i = payload.find('{"@context"', pos)
        if i == -1:
            break
        try:
            obj, end = decoder.raw_decode(payload, i)
        except json.JSONDecodeError:
            pos = i + 1
            continue
        pos = end
        _collect_products_from_obj(obj, products)

    # Dedup by (sku, name) — same Product can be found by both strategies.
    seen: set[tuple] = set()
    unique: list[dict] = []
    for p in products:
        key = (p.get("sku"), p.get("name"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)

    with_avail = [
        p for p in unique if isinstance(p.get("offers"), dict) and p["offers"].get("availability")
    ]
    if len(with_avail) == 1:
        return with_avail[0]
    return None


# ─── LLM provider abstraction ────────────────────────────────────────────────


_EXTRACTION_PROMPT = """Extract product info from this e-commerce HTML page in Azerbaijan.

Return ONLY valid JSON with these fields (use null for missing):
{
  "is_product": boolean,        // false if this is a category/list page or anything other than a product
  "name": "string",              // full product name as shown
  "brand": "string|null",        // brand if visible
  "price_azn": number|null,      // price in AZN (current sale price if discounted)
  "old_price_azn": number|null,  // pre-discount price if shown
  "is_on_sale": boolean,         // true if price is reduced
  "pack_size": "string|null",    // e.g. "400 q", "30 tab"
  "dosage": "string|null",       // e.g. "500 mg" for drugs
  "image_url": "string|null"     // main product image absolute URL
}

If the page is NOT a product page (category/search/etc), return {"is_product": false} with all other fields null."""


async def llm_extract(html: str, page_url: str, session: CrawlSession) -> dict | None:
    """Call configured LLM provider, return extracted JSON.

    Returns None on failure (network, parse, cost cap).
    Increments session.input_tokens / output_tokens.
    """
    if session.estimated_cost_usd >= float(os.getenv("AI_CRAWL_BUDGET_USD", "5.0")):
        log.warning("budget_exceeded", cost=session.estimated_cost_usd, url=page_url)
        return None

    # Trim HTML to first ~12k chars (5k for input cost control)
    cleaned = _clean_html(html)
    sample = cleaned[:12_000]

    provider = os.getenv("AI_CRAWL_PROVIDER", "openai").lower()
    model = os.getenv("AI_CRAWL_MODEL", "gpt-4o-mini")

    try:
        if provider == "openai":
            return await _openai_extract(sample, page_url, model, session)
        elif provider == "anthropic":
            return await _anthropic_extract(sample, page_url, model, session)
        else:
            log.error("unknown_provider", provider=provider)
            return None
    except Exception as e:
        log.warning("llm_extract_failed", url=page_url, error=str(e))
        return None


async def _openai_extract(html: str, url: str, model: str, session: CrawlSession) -> dict | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log.error("openai_no_key")
        return None
    try:
        from openai import AsyncOpenAI
    except ImportError:
        log.error("openai_not_installed")
        return None

    client = AsyncOpenAI(api_key=api_key)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _EXTRACTION_PROMPT},
            {"role": "user", "content": f"URL: {url}\n\nHTML:\n{html}"},
        ],
        response_format={"type": "json_object"},
        max_tokens=400,
        temperature=0,
    )
    if response.usage:
        session.input_tokens += response.usage.prompt_tokens
        session.output_tokens += response.usage.completion_tokens
    raw = response.choices[0].message.content or "{}"
    return json.loads(raw)


async def _anthropic_extract(html: str, url: str, model: str, session: CrawlSession) -> dict | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("anthropic_no_key")
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        log.error("anthropic_not_installed")
        return None

    client = AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=model,
        max_tokens=400,
        system=_EXTRACTION_PROMPT,
        messages=[{"role": "user", "content": f"URL: {url}\n\nHTML:\n{html}"}],
    )
    session.input_tokens += response.usage.input_tokens
    session.output_tokens += response.usage.output_tokens
    text = response.content[0].text if response.content else "{}"
    # Anthropic doesn't have native JSON mode, extract from text
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0)) if m else None


# ─── HTML cleaning (reduce token cost) ───────────────────────────────────────


def _clean_html(html: str) -> str:
    """Strip <script>, <style>, <svg>, comments, noscript. Cuts token cost ~3x."""
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    html = re.sub(r"<svg[^>]*>.*?</svg>", "", html, flags=re.DOTALL)
    html = re.sub(r"<noscript[^>]*>.*?</noscript>", "", html, flags=re.DOTALL)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


# ─── AI Scraper class ────────────────────────────────────────────────────────


class AICrawlerScraper(BaseScraper):
    """Generic AI-powered scraper for any e-commerce site.

    Subclass requirements: site_name + base_url. Everything else is automatic.

    Usage:
        class AloeAICrawler(AICrawlerScraper):
            site_name = "aloe"
            base_url = "https://aloe.az"

        async with AloeAICrawler() as s:
            result = await s.crawl(max_urls=200)
            for product in result.products:
                ...
    """

    async def scrape_category(
        self, category_slug: str, limit: int | None = None
    ) -> AsyncIterator[ScrapedProduct]:
        """AICrawler ignores category_slug — it discovers everything itself."""
        async for product in self._crawl_iter(max_urls=limit or 500):
            yield product

    async def crawl(self, max_urls: int = 500, dry_run: bool = False) -> ScrapeResult:
        """Full crawl session with cost tracking.

        Args:
            max_urls: stop after visiting this many URLs (default 500)
            dry_run: discover URLs + classify but don't extract (free)
        """
        result = ScrapeResult(site=self.site_name)
        session = CrawlSession(site_name=self.site_name, base_url=self.base_url)
        budget_usd = float(os.getenv("AI_CRAWL_BUDGET_USD", "5.0"))

        page = await self.new_page()
        try:
            # 1. Discover via sitemap
            log.info(
                "ai_crawl_start", site=self.site_name, max_urls=max_urls, budget_usd=budget_usd
            )
            urls = await fetch_sitemap_urls(self.base_url, page)

            # Filter to same-domain only (avoid leaking to external domains)
            base_host = urlparse(self.base_url).netloc
            urls = [u for u in urls if urlparse(u).netloc == base_host or urlparse(u).netloc == ""]
            urls = urls[:max_urls]

            if not urls:
                log.warning(
                    "no_sitemap_urls", site=self.site_name, fallback="BFS not yet implemented"
                )
                result.errors.append("no sitemap and BFS fallback not implemented")
                return result

            log.info("ai_crawl_discovered", site=self.site_name, urls=len(urls))

            # 2. Visit each URL, extract if product
            for i, url in enumerate(urls):
                if session.estimated_cost_usd >= budget_usd:
                    log.warning("budget_exceeded_stopping", cost=session.estimated_cost_usd)
                    break

                # Skip if already visited (deduplication)
                if url in session.visited:
                    continue
                session.visited.add(url)

                try:
                    await self.goto(page, url, check_captcha=True)
                    # Many SPAs (Meteor, Next, etc.) inject schema.org JSON-LD
                    # after hydration — wait for network to settle so we don't
                    # miss it. Don't fail if site is slow; just take what we have.
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    html = await page.content()
                except CaptchaDetected:
                    session.captcha_urls.append(url)
                    continue
                except Exception as e:
                    session.failed_urls.append(url)
                    log.debug("ai_crawl_url_failed", url=url, error=str(e))
                    continue

                # JSON-LD short-circuit — free, fast, structured. Most modern
                # e-commerce SSRs schema.org Product for SEO. Wins over LLM.
                jsonld = parse_jsonld_product(html)
                source = "jsonld" if jsonld else None
                # Next.js App Router (e.g. aloe.az) embeds JSON-LD inside the
                # RSC stream chunks rather than as a standalone <script> tag —
                # try to recover it before falling back to heuristics + LLM.
                if not jsonld:
                    jsonld = parse_next_rsc_jsonld(html)
                    if jsonld:
                        source = "rsc_jsonld"
                if jsonld:
                    if not dry_run:
                        product = _build_product_from_jsonld(self.site_name, url, jsonld)
                        if product:
                            result.products.append(product)
                    session.products_yielded += 1
                    if (i + 1) % 25 == 0:
                        log.info(
                            "ai_crawl_progress",
                            site=self.site_name,
                            processed=i + 1,
                            total=len(urls),
                            products=session.products_yielded,
                            cost_usd=round(session.estimated_cost_usd, 4),
                            source=source,
                        )
                    continue

                # Heuristic shortcut: skip pages without product markers
                if not heuristic_is_product_page(html):
                    continue

                if dry_run:
                    session.products_yielded += 1
                    log.debug("ai_crawl_dry_product", url=url)
                    continue

                # LLM extraction
                data = await llm_extract(html, url, session)
                if not data or not data.get("is_product"):
                    continue

                product = ScrapedProduct(
                    site=self.site_name,
                    external_id=hashlib.sha1(url.encode()).hexdigest()[:16],
                    url=url,
                    name=str(data.get("name") or "")[:500],
                    brand=str(data.get("brand"))[:200] if data.get("brand") else None,
                    pack_size=str(data.get("pack_size"))[:100] if data.get("pack_size") else None,
                    dosage=str(data.get("dosage"))[:100] if data.get("dosage") else None,
                    image_url=str(data.get("image_url"))[:500] if data.get("image_url") else None,
                    price=float(data.get("old_price_azn") or data.get("price_azn") or 0) or None,
                    discount_price=float(data.get("price_azn") or 0)
                    if data.get("is_on_sale")
                    else None,
                    is_on_sale=bool(data.get("is_on_sale")),
                )
                if product.name:
                    result.products.append(product)
                    session.products_yielded += 1

                # Progress log every 25 URLs
                if (i + 1) % 25 == 0:
                    log.info(
                        "ai_crawl_progress",
                        site=self.site_name,
                        processed=i + 1,
                        total=len(urls),
                        products=session.products_yielded,
                        cost_usd=round(session.estimated_cost_usd, 4),
                    )
        finally:
            await page.close()

        log.info(
            "ai_crawl_summary",
            site=self.site_name,
            visited=len(session.visited),
            products=session.products_yielded,
            captcha_hits=len(session.captcha_urls),
            failures=len(session.failed_urls),
            input_tokens=session.input_tokens,
            output_tokens=session.output_tokens,
            cost_usd=round(session.estimated_cost_usd, 4),
        )
        return result

    async def _crawl_iter(self, max_urls: int) -> AsyncIterator[ScrapedProduct]:
        """Streaming version of crawl() — used by scrape_category contract."""
        result = await self.crawl(max_urls=max_urls)
        for p in result.products:
            yield p


# ─── Per-site subclasses ─────────────────────────────────────────────────────


class AloeAICrawler(AICrawlerScraper):
    site_name = "aloe"
    base_url = "https://aloe.az"


class PharmonlineAICrawler(AICrawlerScraper):
    site_name = "pharmonline"
    base_url = "https://pharmonline.az"


class AptekonlineAICrawler(AICrawlerScraper):
    site_name = "aptekonline"
    base_url = "https://www.aptekonline.az"


AI_CRAWLER_BY_SITE = {
    "aloe": AloeAICrawler,
    "pharmonline": PharmonlineAICrawler,
    "aptekonline": AptekonlineAICrawler,
}
