"""Скрейпер для pharmonline.az — server-rendered HTML (НЕ SPA).

Структура (по результатам live-probe 2026-04-28):
- Categories:    https://pharmonline.az/products?category={slug}
- Product page:  https://pharmonline.az/product/{slug}
- Price (main):  `.main-price.heading4`           (e.g. "0.66 AZN")
- Card price:    `.price_box_v2 .second_price`    (на карточках в каталоге)
- Card link:     `a[href*="/product/"]`
- Title:         page <title> или внутри карточки

Поскольку контент серверно отрендерен, ждать SPA hydration не нужно.
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


class PharmonlineScraper(BaseScraper):
    site_name = "pharmonline"
    base_url = "https://pharmonline.az"  # без www

    async def scrape_category(
        self, category_slug: str, limit: int | None = None, max_pages: int = 100
    ) -> AsyncIterator[ScrapedProduct]:
        """Скрейпинг категории с пагинацией ?page=1,2,3...

        max_pages=100 — safety net. Реально цикл обрывается через
        `pharmonline_pagination_done` когда страница 0 новых уникальных.
        """
        if not category_slug:
            return
        base = f"{self.base_url}/products?category={category_slug}"
        seen_external_ids: set[str] = set()
        yielded = 0
        total_cards_found = 0
        total_card_failures = 0

        for page_num in range(1, max_pages + 1):
            url = base if page_num == 1 else f"{base}&page={page_num}"
            page = await self.new_page()
            page_first_id: str | None = None
            page_yielded = 0
            try:
                await self.goto(page, url)
                # pharmonline нуждается ~3-4с для рендера карточек
                await page.wait_for_timeout(3500)
                # подстраховка: ждать пока появится хотя бы 1 карточка
                for _ in range(10):
                    count = await page.evaluate(
                        'document.querySelectorAll(\'.product_box_v2\').length'
                    )
                    if count >= 1:
                        break
                    await page.wait_for_timeout(800)
                else:
                    if page_num == 1:
                        log.warning("pharmonline_no_cards", category=category_slug)
                    break  # пустая страница — конец пагинации

                # Lazy-load: scroll до стабилизации количества карточек
                prev_count = -1
                for _ in range(12):
                    cards_now = await page.query_selector_all(".product_box_v2")
                    if len(cards_now) == prev_count:
                        break
                    await page.evaluate(
                        "window.scrollTo(0, document.body.scrollHeight)"
                    )
                    await page.wait_for_timeout(700)
                    prev_count = len(cards_now)

                cards = await page.query_selector_all(".product_box_v2")
                total_cards_found += len(cards)
                log.info(
                    "pharmonline_cards_found",
                    category=category_slug,
                    page=page_num,
                    count=len(cards),
                )

                page_dropped_dup = 0
                page_dropped_empty = 0
                for card in cards:
                    if limit and yielded >= limit:
                        return
                    try:
                        product = await self._parse_card(card, category_slug)
                        if not product:
                            page_dropped_empty += 1
                            continue
                        if page_first_id is None:
                            page_first_id = product.external_id
                        if product.external_id in seen_external_ids:
                            page_dropped_dup += 1
                            continue  # дубликат с предыдущей страницы
                        seen_external_ids.add(product.external_id)
                        page_yielded += 1
                        yielded += 1
                        yield product
                    except Exception as e:
                        total_card_failures += 1
                        log.warning("pharmonline_card_parse_failed", error=str(e))
                if page_dropped_empty or page_dropped_dup:
                    log.info(
                        "pharmonline_page_drops",
                        page=page_num,
                        dropped_empty=page_dropped_empty,
                        dropped_dup=page_dropped_dup,
                    )
            finally:
                await page.close()

            # Если на странице 0 новых уникальных товаров — пагинация исчерпана
            if page_yielded == 0:
                log.info(
                    "pharmonline_pagination_done",
                    category=category_slug,
                    pages=page_num,
                    total_cards_found=total_cards_found,
                    total_yielded=yielded,
                    total_card_failures=total_card_failures,
                )
                break

    async def _parse_card(self, card, category: str) -> ScrapedProduct | None:
        """Парс карточки .product_box_v2.

        Структура:
            .product_box_v2
              .product_top > .product_img > a[href, aria-label]
              .second_price (цена)
              .old-price/s/del (если есть)
        """
        link = await card.query_selector('a[href*="/product/"]')
        if not link:
            return None
        href = await link.get_attribute("href")
        if not href:
            return None
        full_url = href if href.startswith("http") else f"{self.base_url}{href}"
        external_id = href.rstrip("/").split("/")[-1]

        # Имя — в aria-label у ссылки, либо в alt у изображения
        name = await link.get_attribute("aria-label") or ""
        if not name.strip():
            img = await link.query_selector("img")
            if img:
                name = await img.get_attribute("alt") or ""
        if not name.strip():
            return None
        name = name.strip()

        # цена
        price_handle = await card.query_selector(".second_price, .main-price")
        price = parse_price(await price_handle.inner_text()) if price_handle else None

        old_price_handle = await card.query_selector(
            ".old-price, [class*='old-price'], s, del"
        )
        old_price = (
            parse_price(await old_price_handle.inner_text()) if old_price_handle else None
        )

        is_on_sale = old_price is not None and price is not None and old_price > price
        discount_percent = None
        if is_on_sale and old_price:
            discount_percent = round((1 - price / old_price) * 100, 1)  # type: ignore[operator]

        img_handle = await card.query_selector("img")
        image_url = await img_handle.get_attribute("src") if img_handle else None

        promo_handle = await card.query_selector('[class*="badge"], [class*="label"]')
        promo_label = (await promo_handle.inner_text()).strip() if promo_handle else None
        if promo_label and len(promo_label) > 50:
            promo_label = None

        return ScrapedProduct(
            site=self.site_name,
            external_id=external_id,
            url=full_url,
            name=name,
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
        """Watchlist-режим: одна страница товара. Использует реальные селекторы pharmonline."""
        page = await self.new_page()
        try:
            await self.goto(page, url)
            await page.wait_for_timeout(2500)
            try:
                await page.wait_for_selector(".main-price, h1", timeout=10000)
            except Exception:
                log.warning("pharmonline_product_page_no_main", url=url)

            # Имя из <title>
            title = await page.title()
            name = title.split("|")[0].strip() if title else ""

            price_handle = await page.query_selector(".main-price.heading4, .main-price")
            price = parse_price(await price_handle.inner_text()) if price_handle else None

            # У pharmonline старая цена может быть рядом — пока не находим её отдельно.
            # Если найдётся в HTML — добавим селектор.
            old_price = None
            is_on_sale = False
            discount_percent = None

            img_handle = await page.query_selector("main img, .product-image img, [class*='product'] img")
            image_url = await img_handle.get_attribute("src") if img_handle else None

            external_id = url.rstrip("/").split("/")[-1]

            if not name:
                return None

            return ScrapedProduct(
                site=self.site_name,
                external_id=external_id,
                url=url,
                name=name,
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
            await self.goto(page, self.base_url)
            promos: list[ScrapedPromo] = []
            banners = await page.query_selector_all(
                '[class*="banner"], [class*="slide"], .swiper-slide'
            )
            for banner in banners[:10]:
                try:
                    text = (await banner.inner_text()).strip()
                    if not text or len(text) > 500:
                        continue
                    img = await banner.query_selector("img")
                    img_url = await img.get_attribute("src") if img else None
                    link = await banner.query_selector("a")
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
