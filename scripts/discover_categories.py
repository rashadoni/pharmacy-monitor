"""Site-map discovery: собрать nav-категории с 3 сайтов.

Запуск:
    .venv/bin/python scripts/discover_categories.py

Результат: data/category_map.json — список словарей с полями:
    {site, slug, name, source_url}

Использование результата: см. scripts/load_categories_from_map.py
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

OUTPUT = Path("data/category_map.json")


async def discover_pharmonline() -> list[dict]:
    """Pharmonline: nav в header содержит ссылки /products?category=<slug>."""
    out: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://pharmonline.az/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        # все ссылки на /products?category=...
        links = await page.query_selector_all('a[href*="category="]')
        seen = set()
        for a in links:
            href = await a.get_attribute("href") or ""
            text = (await a.inner_text() or "").strip()
            m = re.search(r"category=([^&\s]+)", href)
            if not m:
                continue
            slug = m.group(1)
            if slug in seen or not slug:
                continue
            seen.add(slug)
            out.append({
                "site": "pharmonline",
                "slug": slug,
                "name": text or slug,
                "source_url": href,
            })
        await browser.close()
    return out


async def discover_aptekonline() -> list[dict]:
    """Aptekonline: ссылки /products/<slug> или /category/<slug>."""
    out: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://www.aptekonline.az/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        links = await page.query_selector_all('a[href*="/products/"], a[href*="/category/"]')
        seen = set()
        for a in links:
            href = await a.get_attribute("href") or ""
            text = (await a.inner_text() or "").strip()
            m = re.search(r"/(?:products|category)/([^/?#\s]+)", href)
            if not m:
                continue
            slug = m.group(1)
            # отфильтровать товары (длинные slug'и) — категории обычно короткие
            if len(slug) > 60 or slug in seen:
                continue
            seen.add(slug)
            out.append({
                "site": "aptekonline",
                "slug": slug,
                "name": text or slug,
                "source_url": href,
            })
        await browser.close()
    return out


async def discover_aloe() -> list[dict]:
    """Aloe: фильтры по category_slug в URL /catalog/filters/?category_slug=<slug>."""
    out: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://aloe.az/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        # Aloe рендерит nav через JS — попытаться кликнуть по бургер-меню если нужно
        burger = await page.query_selector('[class*="burger"], [class*="menu-toggle"]')
        if burger:
            try:
                await burger.click()
                await page.wait_for_timeout(1500)
            except Exception:
                pass
        links = await page.query_selector_all(
            'a[href*="category_slug="], a[href*="/catalog/"], a[href*="/category/"]'
        )
        seen = set()
        for a in links:
            href = await a.get_attribute("href") or ""
            text = (await a.inner_text() or "").strip()
            m = re.search(r"category_slug=([^&\s#]+)", href)
            if not m:
                # попробовать /category/<slug>
                m = re.search(r"/category/([^/?#\s]+)", href)
            if not m:
                continue
            slug = m.group(1)
            if not slug or len(slug) > 80 or slug in seen:
                continue
            seen.add(slug)
            out.append({
                "site": "aloe",
                "slug": slug,
                "name": text or slug,
                "source_url": href,
            })
        await browser.close()
    return out


async def main():
    print("Discovering pharmonline...")
    ph = await discover_pharmonline()
    print(f"  found {len(ph)} categories")
    print("Discovering aptekonline...")
    ap = await discover_aptekonline()
    print(f"  found {len(ap)} categories")
    print("Discovering aloe...")
    al = await discover_aloe()
    print(f"  found {len(al)} categories")

    all_cats = ph + ap + al
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(all_cats, ensure_ascii=False, indent=2))
    print(f"\nSaved {len(all_cats)} categories to {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
