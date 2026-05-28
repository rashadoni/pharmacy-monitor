"""Разведка пагинации pharmonline и aptekonline для категории baby food.

Что выясняем:
- Тип навигации: ?page=N в URL? Кнопка "Daha çox"? Бесконечный скролл?
- Сколько товаров всего в категории
- Сколько на одной "странице"
- Есть ли индикатор общего количества (e.g. "из 247 товаров")
"""

from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

TARGETS = [
    ("pharmonline", "https://pharmonline.az/products?category=ushaq-qidasi"),
    ("aptekonline", "https://www.aptekonline.az/products/252"),
]


async def probe(site: str, url: str):
    print(f"\n{'='*70}\n=== {site.upper()}: {url} ===\n{'='*70}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=UA, viewport={"width": 1366, "height": 900}, locale="az-AZ"
        )
        page = await ctx.new_page()

        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)

        # Селектор карточек
        cards_sel = (
            ".product_box_v2" if site == "pharmonline" else "div.single-product-wrap"
        )

        # Initial count
        initial = await page.evaluate(
            f'document.querySelectorAll("{cards_sel}").length'
        )
        print(f"\n--- Initial cards: {initial} ---")

        # Probe pagination signals
        signals = await page.evaluate("""
            () => {
                const out = {};
                // Pagination links (numbered pages)
                const pageLinks = Array.from(document.querySelectorAll('a[href*="page="]'))
                    .map(a => a.href).slice(0, 10);
                out.page_query_links = pageLinks;
                // Кнопки "Load more" / "Daha çox" / "Next"
                const loadButtons = Array.from(document.querySelectorAll('button, a'))
                    .filter(el => /daha|more|sonraki|загр|next|şəraf|göster|показать ещё/i.test(el.textContent || ''))
                    .slice(0, 10)
                    .map(el => ({tag: el.tagName, text: (el.textContent || '').trim().slice(0, 40), cls: el.className.slice(0, 60)}));
                out.load_buttons = loadButtons;
                // pagination container hints
                const paginators = Array.from(document.querySelectorAll('[class*="pagination"], [class*="pager"], nav[aria-label*="page" i]'))
                    .slice(0, 5)
                    .map(e => ({cls: e.className.slice(0, 80), text: (e.textContent || '').trim().slice(0, 100)}));
                out.paginator_blocks = paginators;
                // total count indicator
                const counts = Array.from(document.querySelectorAll('body *'))
                    .filter(el => {
                        const t = (el.textContent || '').trim();
                        return /^\\d+\\s*(товар|məhsul|product|item|tap|nəticə)/i.test(t) && t.length < 80;
                    }).slice(0, 5).map(el => ({text: (el.textContent || '').trim().slice(0, 80), cls: el.className.slice(0,60)}));
                out.count_hints = counts;
                return out;
            }
        """)

        print(f"\n--- ?page=N links: {len(signals['page_query_links'])} ---")
        for link in signals["page_query_links"][:5]:
            print(f"    {link}")

        print(f"\n--- Load-more buttons: {len(signals['load_buttons'])} ---")
        for b in signals["load_buttons"][:5]:
            print(f"    {b['tag']:6s} text={b['text']!r:35s} cls={b['cls'][:50]}")

        print(f"\n--- Paginator blocks: {len(signals['paginator_blocks'])} ---")
        for p_blk in signals["paginator_blocks"][:3]:
            print(f"    cls={p_blk['cls']}")
            print(f"    text={p_blk['text']!r}")

        print(f"\n--- Total count hints: {len(signals['count_hints'])} ---")
        for c in signals["count_hints"]:
            print(f"    {c['text']!r}")

        # Try scrolling — do new cards appear?
        print("\n--- Scroll test ---")
        prev = initial
        for i in range(10):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)
            count = await page.evaluate(
                f'document.querySelectorAll("{cards_sel}").length'
            )
            if count == prev:
                print(f"    iter {i}: stable at {count}")
                break
            print(f"    iter {i}: {prev} → {count}")
            prev = count

        # Try ?page=2 if pharmonline
        if site == "pharmonline":
            print(f"\n--- Try {url}&page=2 ---")
            try:
                resp = await page.goto(
                    url + "&page=2", wait_until="domcontentloaded", timeout=20000
                )
                await page.wait_for_timeout(3000)
                p2_count = await page.evaluate(
                    f'document.querySelectorAll("{cards_sel}").length'
                )
                print(f"    page=2: {p2_count} cards (status {resp.status if resp else '?'})")
                # Are they DIFFERENT products?
                first_url_p2 = await page.evaluate(
                    'document.querySelector("a[href*=\\"/product/\\"]")?.href'
                )
                print(f"    first product on p2: {first_url_p2}")
            except Exception as e:
                print(f"    ERROR: {e}")

        await browser.close()


async def main():
    for site, url in TARGETS:
        await probe(site, url)


if __name__ == "__main__":
    asyncio.run(main())
