"""Точечный осмотр одной карточки на pharmonline + ещё раз aloe со скроллом."""

from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


async def inspect_pharmonline():
    print("\n=== PHARMONLINE: inspect product card structure ===")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()
        await page.goto(
            "https://pharmonline.az/products?category=antiallergik-vasiteler",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(2000)

        info = await page.evaluate(
            """() => {
                const allLinks = document.querySelectorAll('a[href*="/product/"]');
                const unique = {};
                allLinks.forEach(a => unique[a.href] = a);
                const hrefs = Object.keys(unique);
                if (!hrefs.length) return {hrefs: 0};
                const a = unique[hrefs[0]];
                let parent = a;
                const ancestors = [];
                for (let i = 0; i < 8 && parent; i++) {
                    parent = parent.parentElement;
                    if (parent) {
                        ancestors.push({
                            tag: parent.tagName,
                            cls: (parent.className || '').slice(0, 80),
                            children: parent.children.length,
                            hasImg: !!parent.querySelector('img'),
                            hasPrice: /\\d+[.,]\\d{2}/.test(parent.textContent || ''),
                        });
                    }
                }
                return {
                    totalUnique: hrefs.length,
                    sampleHref: hrefs[0],
                    aTag: a.tagName,
                    aTitle: a.title,
                    aText: (a.textContent || '').trim().slice(0, 100),
                    ancestors,
                };
            }"""
        )
        print(f"Unique product links: {info.get('totalUnique')}")
        print(f"Sample link: {info.get('sampleHref')}")
        print(f"  title='{info.get('aTitle')}'  text='{info.get('aText')}'")
        print(f"  Ancestor chain (looking for card with img+price):")
        for i, a in enumerate(info.get("ancestors", [])):
            mark = " ★ CARD" if a["hasImg"] and a["hasPrice"] else ""
            print(f"    [{i}] {a['tag']:6s} cls={a['cls']:55s} img={a['hasImg']} price={a['hasPrice']}{mark}")

        await browser.close()


async def inspect_aloe_again():
    print("\n=== ALOE: re-probe with longer wait ===")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()
        await page.goto(
            "https://aloe.az/catalog/filters/?product_field=bestseller",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        # Подождать сетевой тишины активно
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        for i in range(8):
            count = await page.evaluate(
                'document.querySelectorAll(\'[class*="productCardWrapper"]\').length'
            )
            print(f"  iter {i}: cards = {count}")
            if count > 0:
                break
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)

        await browser.close()


async def main():
    await inspect_pharmonline()
    await inspect_aloe_again()


if __name__ == "__main__":
    asyncio.run(main())
