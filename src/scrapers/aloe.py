"""Скрейпер для aloe.az — Next.js, динамический рендер.

Структура (по результатам live-probe 2026-04-28):
- Categories:    https://aloe.az/catalog/filters/?product_field=bestseller
                 https://aloe.az/catalog/filters/?category_slug={slug}
- Product page:  https://aloe.az/{slug}/   ← БЕЗ /product/ префикса!
- Card:          `[class*="productCardWrapper"]`  (это <button>, не <a>!)
- Card name:     `[class*="productName"]`
- Card brand:    `[class*="brand"]`  (внутри карточки <a>)
- Card price:    `[class*="price"]:not([class*="old"])`  → 'priceWrapper > price'
- Card image:    `[class*="productImg"] img`  (alt содержит имя)
- Detail title:  `h1[class*="style_title"]`
- Detail price:  `[class*="priceWrapper"] [class*="style_price"]` → e.g. "4.03"

Карточка не имеет href — для извлечения URL нужно либо клик-навигация,
либо синтез slug из имени. Для category-режима пишем имя/цену из карточки
без URL (url='listing-url' — placeholder). Для watchlist-режима юзер даёт URL.
"""

from __future__ import annotations

from typing import AsyncIterator

import structlog

from src.normalize import (
    extract_dosage,
    extract_pack_size,
    normalize_name,
    parse_price,
)
from src.scrapers.base import BaseScraper, ScrapedProduct, ScrapedPromo

log = structlog.get_logger()


class AloeScraper(BaseScraper):
    site_name = "aloe"
    base_url = "https://aloe.az"

    async def scrape_category(
        self, category_slug: str, limit: int | None = None, max_pages: int = 100
    ) -> AsyncIterator[ScrapedProduct]:
        """Aloe пагинация через ?page=N (12 товаров на страницу).

        max_pages=100 — safety net. Реально цикл обрывается через
        pagination_done когда страница 0 новых уникальных.
        """
        if not category_slug:
            return
        if "=" in category_slug:
            base_url = f"{self.base_url}/catalog/filters/?{category_slug}"
        elif category_slug in ("bestseller", "new", "promo", "seasonal_product_now_is_the_time"):
            base_url = f"{self.base_url}/catalog/filters/?product_field={category_slug}"
        else:
            base_url = f"{self.base_url}/catalog/filters/?category_slug={category_slug}"

        seen_external_ids: set[str] = set()
        yielded = 0
        total_cards_found = 0
        total_card_failures = 0

        for page_num in range(1, max_pages + 1):
            url = base_url if page_num == 1 else f"{base_url}&page={page_num}"
            page = await self.new_page()
            page_yielded = 0
            page_dropped_dup = 0
            page_dropped_empty = 0
            try:
                await self.goto(page, url)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    log.debug("aloe_networkidle_timeout", url=url)
                await self._scroll_until_stable(page, max_iterations=15)

                cards = await page.query_selector_all('[class*="productCardWrapper"]')
                total_cards_found += len(cards)
                log.info(
                    "aloe_cards_found",
                    category=category_slug, page=page_num, count=len(cards),
                )

                for card in cards:
                    if limit and yielded >= limit:
                        return
                    try:
                        product = await self._parse_card(card, category_slug, url)
                        if not product:
                            page_dropped_empty += 1
                            continue
                        if product.external_id in seen_external_ids:
                            page_dropped_dup += 1
                            continue
                        seen_external_ids.add(product.external_id)
                        page_yielded += 1
                        yielded += 1
                        yield product
                    except Exception as e:
                        total_card_failures += 1
                        log.warning("aloe_card_parse_failed", error=str(e))
                if page_dropped_empty or page_dropped_dup:
                    log.info(
                        "aloe_page_drops",
                        page=page_num,
                        dropped_empty=page_dropped_empty,
                        dropped_dup=page_dropped_dup,
                    )
            finally:
                await page.close()

            if page_yielded == 0:
                log.info(
                    "aloe_pagination_done",
                    category=category_slug, pages=page_num,
                    total_cards_found=total_cards_found,
                    total_yielded=yielded,
                    total_card_failures=total_card_failures,
                )
                break

    async def _scroll_until_stable(self, page, max_iterations: int = 20) -> None:
        prev_count = -1
        same_streak = 0
        for _ in range(max_iterations):
            count = await page.evaluate(
                'document.querySelectorAll(\'[class*="productCardWrapper"]\').length'
            )
            if count == prev_count:
                same_streak += 1
                if same_streak >= 2:
                    break
            else:
                same_streak = 0
            prev_count = count
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

    async def _parse_card(self, card, category: str, listing_url: str) -> ScrapedProduct | None:
        # имя — первое предпочтение productName, затем alt у изображения
        name_handle = await card.query_selector('[class*="productName"]')
        name = (await name_handle.inner_text()).strip() if name_handle else ""
        if not name:
            img = await card.query_selector("img")
            if img:
                name = (await img.get_attribute("alt") or "").strip()
        if not name:
            return None

        # бренд — внутри карточки есть <a class="style_brand_*">
        brand_handle = await card.query_selector('[class*="brand"]')
        brand = (await brand_handle.inner_text()).strip() if brand_handle else None

        # цена
        price_handle = await card.query_selector(
            '[class*="priceWrapper"] [class*="style_price"]'
        )
        if not price_handle:
            price_handle = await card.query_selector('[class*="price"]:not([class*="old"])')
        price_text = await price_handle.inner_text() if price_handle else None
        price = parse_price(price_text)

        old_price_handle = await card.query_selector(
            '[class*="oldPriceText"], [class*="oldPrice"]'
        )
        old_price_text = await old_price_handle.inner_text() if old_price_handle else None
        old_price = parse_price(old_price_text)

        is_on_sale = old_price is not None and price is not None and old_price > price
        discount_percent = None
        if is_on_sale and old_price:
            discount_percent = round((1 - price / old_price) * 100, 1)  # type: ignore[operator]

        img_handle = await card.query_selector('[class*="productImg"] img')
        image_url = await img_handle.get_attribute("src") if img_handle else None

        promo_handle = await card.query_selector(
            '[class*="properties"], [class*="badge"], [class*="discount"]'
        )
        promo_text = (await promo_handle.inner_text()).strip() if promo_handle else None
        promo_label = promo_text if promo_text and len(promo_text) < 50 else None

        # external_id: aloe не даёт стабильного id в карточке, генерируем из имени
        external_id = name.lower().replace(" ", "-")[:100]

        return ScrapedProduct(
            site=self.site_name,
            external_id=external_id,
            url=listing_url,  # на карточке нет href — fallback
            name=name,
            brand=brand,
            category=category,
            dosage=extract_dosage(name),
            pack_size=extract_pack_size(name),
            image_url=image_url,
            price=old_price if is_on_sale else price,
            discount_price=price if is_on_sale else None,
            discount_percent=discount_percent,
            is_on_sale=is_on_sale,
            promo_label=promo_label,
        )

    async def scrape_product_page(self, url: str) -> ScrapedProduct | None:
        """Watchlist-режим: страница товара по адресу https://aloe.az/{slug}/."""
        page = await self.new_page()
        try:
            await self.goto(page, url)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            try:
                await page.wait_for_selector('h1, [class*="style_title"]', timeout=20000)
            except Exception:
                log.warning("aloe_product_page_timeout", url=url)
                return None

            name_handle = await page.query_selector('h1[class*="style_title"], h1')
            name = (await name_handle.inner_text()).strip() if name_handle else ""
            if not name:
                title = await page.title()
                name = title.split("|")[0].strip() if title else ""
            if not name:
                return None

            price_handle = await page.query_selector(
                '[class*="priceWrapper"] [class*="style_price"]'
            )
            price = parse_price(await price_handle.inner_text()) if price_handle else None

            old_price_handle = await page.query_selector(
                '[class*="oldPrice"], [class*="OldPrice"]'
            )
            old_price = (
                parse_price(await old_price_handle.inner_text())
                if old_price_handle
                else None
            )

            is_on_sale = old_price is not None and price is not None and old_price > price
            discount_percent = None
            if is_on_sale and old_price:
                discount_percent = round((1 - price / old_price) * 100, 1)  # type: ignore[operator]

            # бренд может быть указан рядом с именем
            brand_handle = await page.query_selector(
                '[class*="brand"]:not([class*="brand-"]), [class*="Brand"]'
            )
            brand = (await brand_handle.inner_text()).strip() if brand_handle else None

            img_handle = await page.query_selector(
                '[class*="productImg"] img, main img, [class*="gallery"] img'
            )
            image_url = await img_handle.get_attribute("src") if img_handle else None

            external_id = url.rstrip("/").split("/")[-1]

            return ScrapedProduct(
                site=self.site_name,
                external_id=external_id,
                url=url,
                name=name,
                brand=brand,
                dosage=extract_dosage(name),
                pack_size=extract_pack_size(name),
                image_url=image_url,
                price=old_price if is_on_sale else price,
                discount_price=price if is_on_sale else None,
                discount_percent=discount_percent,
                is_on_sale=is_on_sale,
            )
        finally:
            await page.close()

    async def scrape_promos(self) -> list[ScrapedPromo]:
        page = await self.new_page()
        try:
            await self.goto(page, f"{self.base_url}/promos/")
            await page.wait_for_timeout(3000)
            promos: list[ScrapedPromo] = []
            cards = await page.query_selector_all(
                '[class*="banner"], [class*="Banner"], [class*="promoCard"], [class*="PromoCard"]'
            )
            for card in cards[:15]:
                try:
                    text = (await card.inner_text()).strip()
                    if not text or len(text) > 500:
                        continue
                    img = await card.query_selector("img")
                    img_url = await img.get_attribute("src") if img else None
                    link = await card.query_selector("a")
                    href = await link.get_attribute("href") if link else None
                    promos.append(
                        ScrapedPromo(
                            site=self.site_name,
                            title=text[:200],
                            image_url=img_url,
                            landing_url=href,
                        )
                    )
                except Exception:
                    continue
            return promos
        finally:
            await page.close()


def to_db_dict(p: ScrapedProduct) -> dict:
    return {
        "site": p.site,
        "external_id": p.external_id,
        "url": p.url,
        "name": p.name,
        "name_normalized": normalize_name(p.name),
        "brand": p.brand,
        "manufacturer": p.manufacturer,
        "category": p.category,
        "dosage": p.dosage,
        "pack_size": p.pack_size,
        "image_url": p.image_url,
        "description": p.description,
    }
