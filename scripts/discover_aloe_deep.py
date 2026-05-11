"""Глубокий обход aloe.az: пройти внутрь dermanlar / bad / usaq-dunyasi
и собрать подкатегории.

Aloe.az имеет structure: /category/<slug> с side-bar навигацией.
Внутри страницы категории есть фильтры/sub-categories (тоже /category/<slug>).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

OUTPUT = Path("data/category_map.json")
TOP_CATEGORIES = ["dermanlar", "bad", "usaq-dunyasi", "uşaq-qidası"]


async def discover_subcategories(top_slug: str, page) -> list[dict]:
    """Открыть top-категорию и собрать все ссылки на подкатегории."""
    out = []
    url = f"https://aloe.az/{top_slug}"
    print(f"  → {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        # scroll to make sure side-nav rendered
        await page.evaluate("window.scrollTo(0, 200)")
        await page.wait_for_timeout(1500)

        # Все ссылки на /category_slug=... или /<az-text>
        # Aloe формат: /catalog/filters/?category_slug=foo, либо /<slug>/
        seen = set()
        # 1. ссылки по category_slug
        links = await page.query_selector_all(
            'a[href*="category_slug="], a[href*="/dermanlar/"], a[href*="/bad/"], a[href*="/uşaq"], a[href*="/usaq"]'
        )
        for a in links:
            href = await a.get_attribute("href") or ""
            text = (await a.inner_text() or "").strip()
            # Try category_slug param
            m = re.search(r"category_slug=([^&\s#]+)", href)
            if m:
                slug = m.group(1)
            else:
                # Path-based: /dermanlar/<sub>/
                m = re.search(r"/(dermanlar|bad|usaq-dunyasi|uşaq-qidası)/([^/?#\s]+)", href)
                if m:
                    slug = m.group(2)
                else:
                    continue
            if not slug or len(slug) > 100 or slug in seen or slug in TOP_CATEGORIES:
                continue
            seen.add(slug)
            out.append({
                "site": "aloe",
                "slug": slug,
                "name": text or slug,
                "source_url": href,
                "parent": top_slug,
            })
    except Exception as e:
        print(f"    ERROR: {e}")
    return out


async def main():
    print("Deep discovery for aloe.az...")
    found_total: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        for top in TOP_CATEGORIES:
            subs = await discover_subcategories(top, page)
            print(f"  {top}: {len(subs)} subcategories")
            found_total.extend(subs)
        await browser.close()

    # Загрузить существующий map, добавить новые aloe entries (без дублей)
    existing = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else []
    existing_aloe_slugs = {c["slug"] for c in existing if c["site"] == "aloe"}
    new_count = 0
    for c in found_total:
        if c["slug"] not in existing_aloe_slugs:
            existing.append(c)
            new_count += 1
    OUTPUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
    print(f"\nAdded {new_count} new aloe subcategories. Total in map: {len(existing)}")


if __name__ == "__main__":
    asyncio.run(main())
