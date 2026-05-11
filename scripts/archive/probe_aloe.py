"""Углублённая разведка aloe.az — Next.js, продукты не подгрузились в первом probe.

Стратегия:
1. Открыть категорию, дождаться сетевой тишины
2. Скроллить и смотреть когда появятся карточки
3. Поймать XHR/fetch к API (Next.js часто SSG/SSR + клиентский fetch)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "data" / "snapshots"
SNAP.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

URLS = [
    "https://aloe.az/catalog/filters/?product_field=bestseller",
    "https://aloe.az/catalog/filters/?category_slug=dermanlar/",
]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA, viewport={"width": 1366, "height": 900}, locale="az-AZ"
        )

        for url in URLS:
            page = await context.new_page()

            api_calls = []
            page.on(
                "response",
                lambda r: api_calls.append({
                    "url": r.url,
                    "status": r.status,
                    "ct": r.headers.get("content-type", ""),
                }) if (r.url.find("api") > -1 or r.url.find(".json") > -1)
                  and r.url != url
                  else None,
            )

            print(f"\n=== {url} ===", flush=True)
            await page.goto(url, wait_until="networkidle", timeout=60000)

            # Дополнительный скролл
            for i in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000)
                count = await page.evaluate("document.querySelectorAll('a[href*=\"/product/\"]').length")
                print(f"  after scroll #{i+1}: product links = {count}")

            # Сохранить HTML
            host = urlparse(url).hostname
            slug = url.split("?")[-1].replace("&", "_").replace("=", "-")[:50]
            snap = SNAP / f"{host}_{slug}.html"
            snap.write_text(await page.content(), encoding="utf-8")
            print(f"  saved: {snap.relative_to(ROOT)}")

            # Сигналы DOM
            signals = await page.evaluate("""
                () => {
                    const cards = Array.from(document.querySelectorAll('a[href*="/product/"]'));
                    const out = cards.slice(0, 5).map(a => {
                        const card = a.closest('article, li, div');
                        const text = (card?.textContent || a.textContent || '').trim().slice(0, 200);
                        return {
                            href: a.href,
                            cardTag: card?.tagName,
                            cardCls: (card?.className || '').slice(0, 100),
                            text,
                        };
                    });

                    const allClasses = new Set();
                    document.querySelectorAll('*').forEach(e => {
                        if (typeof e.className === 'string') {
                            e.className.split(/\\s+/).forEach(c => {
                                if (c && (c.toLowerCase().includes('product') || c.toLowerCase().includes('card') || c.toLowerCase().includes('price'))) {
                                    allClasses.add(c);
                                }
                            });
                        }
                    });

                    return { cardSamples: out, classesOfInterest: [...allClasses].slice(0, 30) };
                }
            """)
            print(f"  Card samples: {len(signals['cardSamples'])}")
            for c in signals["cardSamples"]:
                print(f"    {c['cardTag']} class={c['cardCls'][:60]}")
                print(f"      → {c['href']}")
                print(f"      text: {c['text'][:120]!r}")
            print(f"  Classes of interest: {signals['classesOfInterest'][:15]}")

            print(f"  API calls observed: {len(api_calls)}")
            for c in api_calls[:8]:
                print(f"    {c['status']} {c['url'][:120]} [{c['ct'][:40]}]")

            await page.close()

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
