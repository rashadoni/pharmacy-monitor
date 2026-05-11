"""Разведка страницы товара aloe.az + проверка API."""

from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

PRODUCT_URL = "https://aloe.az/nimesil-100-q-30-ed/"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA, viewport={"width": 1366, "height": 900}, locale="az-AZ"
        )
        page = await context.new_page()

        await page.goto(PRODUCT_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)

        signals = await page.evaluate(
            "() => { "
            "const titles = Array.from(document.querySelectorAll('h1, h2, [class*=\"ProductName\"], [class*=\"Title\"]')).slice(0, 8);"
            "const prices = Array.from(document.querySelectorAll('[class*=\"price\"], [class*=\"Price\"]')).slice(0, 10);"
            "const brand = document.querySelector('[class*=\"brand\"], [class*=\"Brand\"]');"
            "const desc = document.querySelector('[class*=\"description\"], [class*=\"Description\"]');"
            "const img = document.querySelector('main img, [class*=\"ProductImg\"] img, [class*=\"productImg\"] img');"
            "return {"
            "  url: location.href,"
            "  title: document.title,"
            "  titles: titles.map(e => ({tag: e.tagName, cls: (e.className||'').slice(0, 80), text: (e.textContent||'').trim().slice(0, 100)})),"
            "  prices: prices.map(e => ({tag: e.tagName, cls: (e.className||'').slice(0, 80), text: (e.textContent||'').trim().slice(0, 60)})),"
            "  brand: brand ? {cls: brand.className, text: brand.textContent.trim().slice(0, 60)} : null,"
            "  hasDesc: !!desc,"
            "  imageSrc: img ? img.src : null,"
            "}; }"
        )

        print(f"URL: {signals['url']}")
        print(f"Page title: {signals['title']}")
        print(f"\nTitle-like elements:")
        for t in signals["titles"]:
            print(f"  {t['tag']:4s} class={t['cls']:50s} text={t['text']!r}")
        print(f"\nPrice-like elements:")
        for pr in signals["prices"]:
            print(f"  {pr['tag']:4s} class={pr['cls']:50s} text={pr['text']!r}")
        print(f"\nBrand: {signals['brand']}")
        print(f"Has description: {signals['hasDesc']}")
        print(f"Image: {signals['imageSrc']}")

        # Probe API endpoints
        print("\n=== API PROBES ===")
        api_page = await context.new_page()
        for endpoint in [
            "https://ecom.aloe.az/api/products?product_field=bestseller&limit=3",
            "https://ecom.aloe.az/api/products?category=dermanlar&limit=3",
            "https://ecom.aloe.az/api/product/nimesil-100-q-30-ed",
            "https://ecom.aloe.az/api/products/nimesil-100-q-30-ed",
        ]:
            try:
                resp = await api_page.goto(endpoint, wait_until="domcontentloaded", timeout=10000)
                status = resp.status if resp else "?"
                if resp and resp.status == 200:
                    body = await resp.text()
                    print(f"  ✓ {status}  {endpoint}")
                    print(f"    body[:200]: {body[:200]}")
                else:
                    print(f"  ✗ {status}  {endpoint}")
            except Exception as e:
                print(f"  ✗ ERROR {endpoint}: {e}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
