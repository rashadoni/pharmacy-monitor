"""Кликнуть по карточке aloe.az и узнать какой URL открывается + проверить API endpoints."""

from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

URL = "https://aloe.az/catalog/filters/?product_field=bestseller"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, locale="az-AZ")
        page = await context.new_page()

        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        for _ in range(6):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

        # Найти первую карточку и кликнуть
        cards = await page.query_selector_all('[class*="productCardWrapper"]')
        print(f"Cards found: {len(cards)}")
        if cards:
            print("Clicking first card...")
            old_url = page.url
            await cards[0].click()
            await page.wait_for_timeout(3000)
            new_url = page.url
            print(f"  Old URL: {old_url}")
            print(f"  New URL: {new_url}")

            # На странице товара — какие классы?
            signals = await page.evaluate("""() => {
                const titleEls = Array.from(document.querySelectorAll('h1, h2, [class*='ProductName'], [class*='Title']')).slice(0, 5);
                const priceEls = Array.from(document.querySelectorAll('[class*="price"]')).slice(0, 8);
                return {
                    titles: titleEls.map(e => ({tag: e.tagName, cls: (e.className||'').slice(0, 80), text: (e.textContent||'').trim().slice(0, 100)})),
                    prices: priceEls.map(e => ({tag: e.tagName, cls: (e.className||'').slice(0, 80), text: (e.textContent||'').trim().slice(0, 60)})),
                };
            }""")
            print(f"\n  Title-like on product page:")
            for t in signals["titles"]:
                print(f"    {t['tag']:4s} class={t['cls']:50s} text={t['text']!r}")
            print(f"\n  Price-like on product page:")
            for pr in signals["prices"]:
                print(f"    {pr['tag']:4s} class={pr['cls']:50s} text={pr['text']!r}")

        # Прямые тесты API endpoints
        print("\n=== API PROBES ===")
        api_endpoints = [
            "https://ecom.aloe.az/api/products?product_field=bestseller&limit=5",
            "https://ecom.aloe.az/api/products?category_slug=dermanlar",
            "https://ecom.aloe.az/api/products",
            "https://ecom.aloe.az/api/categories",
            "https://ecom.aloe.az/api/products/9149",
        ]
        api_page = await context.new_page()
        for endpoint in api_endpoints:
            try:
                resp = await api_page.goto(endpoint, wait_until="domcontentloaded", timeout=15000)
                if resp and resp.status == 200:
                    body = await resp.text()
                    print(f"  ✓ {resp.status} {endpoint}")
                    print(f"    body: {body[:300]}")
                else:
                    print(f"  ✗ {resp.status if resp else '?'} {endpoint}")
            except Exception as e:
                print(f"  ✗ ERROR {endpoint}: {type(e).__name__}: {e}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
