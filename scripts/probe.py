"""Probe-скрипт: посетить homepage / category / product page на 3 сайтах,
сохранить HTML-снэпшоты и распечатать ключевые DOM-сигналы для выбора селекторов.

Запуск:
    uv run python scripts/probe.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS = ROOT / "data" / "snapshots"
SNAPSHOTS.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

SITES = [
    "https://www.pharmonline.az/",
    "https://www.aptekonline.az/",
    "https://aloe.az/",
]


async def probe_homepage(page, url: str) -> dict:
    print(f"\n=== HOMEPAGE: {url} ===", flush=True)
    response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(3000)  # дать SPA отрендериться
    status = response.status if response else 0
    title = await page.title()

    # Сохраняем HTML
    host = urlparse(url).hostname.replace("www.", "")
    snap_path = SNAPSHOTS / f"{host}_home.html"
    snap_path.write_text(await page.content(), encoding="utf-8")

    # Ссылки
    all_links = await page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href]'))
            .map(a => ({href: a.href, text: (a.innerText || '').trim().slice(0, 60)}))
            .filter(l => l.href && !l.href.startsWith('javascript'))
    """)
    product_like = [l for l in all_links if "/product/" in l["href"] or "/p/" in l["href"]]
    category_like = [
        l for l in all_links
        if any(k in l["href"] for k in ("/category", "/catalog", "/products?", "/products/", "/c/"))
    ]

    print(f"  status={status}  title={title!r}")
    print(f"  total links: {len(all_links)}")
    print(f"  product-like links: {len(product_like)}")
    print(f"  category-like links: {len(category_like)}")
    if product_like:
        print(f"  sample product: {product_like[0]['href']}")
    if category_like:
        print("  sample categories:")
        for l in category_like[:5]:
            print(f"    - {l['href']}  [{l['text'][:40]}]")

    return {
        "url": url,
        "status": status,
        "title": title,
        "product_links": product_like[:3],
        "category_links": category_like[:5],
        "snapshot": str(snap_path.relative_to(ROOT)),
    }


async def probe_url(page, url: str, label: str) -> dict:
    print(f"\n=== {label.upper()}: {url} ===", flush=True)
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)  # SPA hydration + lazy load
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"url": url, "label": label, "error": str(e)}

    status = response.status if response else 0
    title = await page.title()

    host = urlparse(url).hostname.replace("www.", "")
    safe_label = label.replace("/", "_")[:40]
    snap_path = SNAPSHOTS / f"{host}_{safe_label}.html"
    snap_path.write_text(await page.content(), encoding="utf-8")

    # Ищем ценовые элементы и карточки
    signals = await page.evaluate("""
        () => {
            // Все элементы с ценой (содержат AZN, ₼, или цифру с .)
            const allEls = Array.from(document.querySelectorAll('body *'));
            const priceLike = allEls.filter(e => {
                const t = (e.textContent || '').trim();
                return /\\d+[.,]\\d{2}\\s*(AZN|₼|man)/i.test(t) && t.length < 80;
            }).slice(0, 8).map(e => ({
                tag: e.tagName,
                cls: e.className,
                text: (e.textContent || '').trim().slice(0, 60),
            }));

            // h1/h2/h3 как кандидаты на title
            const titles = Array.from(document.querySelectorAll('h1, h2, h3'))
                .slice(0, 6)
                .map(e => ({tag: e.tagName, cls: e.className, text: (e.textContent || '').trim().slice(0, 80)}));

            // Карточки: контейнеры с image+price+title
            const cards = allEls.filter(e => {
                const html = e.outerHTML.length;
                if (html < 200 || html > 5000) return false;
                const hasImg = e.querySelector('img');
                const hasPrice = /\\d+[.,]\\d{2}\\s*(AZN|₼|man)/i.test(e.textContent || '');
                return hasImg && hasPrice;
            }).slice(0, 5).map(e => ({
                tag: e.tagName,
                cls: e.className.slice(0, 100),
                html_len: e.outerHTML.length,
            }));

            // Productные ссылки на этой странице
            const productLinks = Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.href)
                .filter(h => /\\/product\\//.test(h) || /\\/p\\//.test(h))
                .slice(0, 5);

            return { priceLike, titles, cards, productLinks };
        }
    """)

    print(f"  status={status}  title={title!r}")
    print("  Title-like elements:")
    for t in signals["titles"]:
        print(f"    {t['tag']:4s} class={t['cls'][:50]:50s} text={t['text']!r}")
    print("  Price-like elements:")
    for p in signals["priceLike"]:
        print(f"    {p['tag']:4s} class={p['cls'][:50]:50s} text={p['text']!r}")
    print(f"  Card-like containers (img+price): {len(signals['cards'])}")
    for c in signals["cards"][:3]:
        print(f"    {c['tag']} class={c['cls'][:80]}")
    print(f"  Product links on page: {len(signals['productLinks'])}")
    for pl in signals["productLinks"]:
        print(f"    {pl}")

    return {
        "url": url,
        "label": label,
        "status": status,
        "title": title,
        "signals": signals,
        "snapshot": str(snap_path.relative_to(ROOT)),
    }


async def main():
    results = {"sites": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, locale="az-AZ")
        page = await context.new_page()

        for site_url in SITES:
            site_data = {"home_url": site_url}
            try:
                home = await probe_homepage(page, site_url)
                site_data["home"] = home

                # Visit a category-like link (first found)
                if home.get("category_links"):
                    cat_url = home["category_links"][0]["href"]
                    cat = await probe_url(page, cat_url, "category")
                    site_data["category"] = cat
                    # If we found a product link on the category page, visit it
                    pl = cat.get("signals", {}).get("productLinks", []) if cat else []
                    if pl:
                        prod = await probe_url(page, pl[0], "product")
                        site_data["product"] = prod

                # If homepage already gave us a product link, also visit it
                if home.get("product_links") and "product" not in site_data:
                    prod_url = home["product_links"][0]["href"]
                    prod = await probe_url(page, prod_url, "product")
                    site_data["product"] = prod

            except Exception as e:
                print(f"  TOP-LEVEL ERROR for {site_url}: {e}", file=sys.stderr)
                site_data["error"] = str(e)

            results["sites"].append(site_data)

        await browser.close()

    summary_path = SNAPSHOTS / "probe_summary.json"
    summary_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n\nSummary saved to: {summary_path.relative_to(ROOT)}")
    print(f"HTML snapshots in: {SNAPSHOTS.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
