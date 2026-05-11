"""Захватить ВСЕ network requests aloe.az чтобы найти product API endpoint."""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent

URL = "https://aloe.az/catalog/filters/?product_field=bestseller"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, locale="az-AZ")
        page = await context.new_page()

        all_requests: list[dict] = []
        responses: list[dict] = []

        page.on(
            "request",
            lambda r: all_requests.append({"method": r.method, "url": r.url, "rt": r.resource_type}),
        )

        async def on_response(r):
            if r.url == URL:
                return
            ct = r.headers.get("content-type", "")
            entry = {"status": r.status, "url": r.url, "ct": ct[:50]}
            # Если JSON — сохраним body
            if "json" in ct.lower() and r.status == 200:
                try:
                    body = await r.text()
                    entry["body_len"] = len(body)
                    entry["body_preview"] = body[:300]
                except Exception:
                    pass
            responses.append(entry)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        print(f"Loading {URL}")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        # Длительное ожидание + скролл
        for i in range(8):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

        # ещё на 5 секунд
        await page.wait_for_timeout(5000)

        # Проверим что получилось в DOM
        cards = await page.evaluate(
            """() => document.querySelectorAll('[class*="productCardWrapper"]').length"""
        )
        names = await page.evaluate(
            """() => document.querySelectorAll('[class*="productName"]').length"""
        )
        prices = await page.evaluate(
            """() => document.querySelectorAll('[class*="price"]').length"""
        )
        print(f"DOM after scroll: cards={cards} names={names} priceEls={prices}")

        # Если вдруг есть карточки — выдадим примеры
        if cards > 0:
            sample = await page.evaluate(
                """() => {
                    const card = document.querySelector('[class*="productCardWrapper"]');
                    if (!card) return null;
                    return {outerHTML: card.outerHTML.slice(0, 2000)};
                }"""
            )
            print("\n=== SAMPLE CARD ===")
            print(sample.get("outerHTML"))

        # Какие нерекламные домены вообще обращались
        domains = {}
        for r in all_requests:
            from urllib.parse import urlparse
            d = urlparse(r["url"]).hostname
            domains[d] = domains.get(d, 0) + 1
        print(f"\n=== DOMAIN ACTIVITY ===")
        for d, n in sorted(domains.items(), key=lambda x: -x[1])[:15]:
            print(f"  {n:4d}  {d}")

        # JSON-ответы — кандидаты на product API
        json_resp = [r for r in responses if "json" in r.get("ct", "").lower()]
        print(f"\n=== JSON RESPONSES ({len(json_resp)}) ===")
        for r in json_resp[:20]:
            preview = r.get("body_preview", "")
            print(f"  status={r['status']} ct={r['ct']:30s} {r['url'][:120]}")
            if "product" in preview.lower() or "price" in preview.lower():
                print(f"    *** PRODUCT-LIKE: {preview[:200]}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
