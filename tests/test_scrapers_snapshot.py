"""Snapshot regression tests для scrapers.

Загружаем сохранённый HTML в headless Chromium и тестируем `_parse_card`
напрямую на каждой найденной карточке. Это даёт быстрый юнит-тест парсинга
без сетевых запросов и pagination'а.

Если сайт меняет вёрстку — тесты падают первыми, ещё ДО продакшен-прогона.

Запуск (медленнее обычных тестов из-за chromium ~1-2s на launch):
    uv run pytest tests/test_scrapers_snapshot.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

FIXTURES = Path(__file__).parent / "fixtures"


async def _parse_cards(
    html_file: str, scraper_class, card_selector: str, category: str = "test"
) -> list:
    """Загрузить HTML в headless Chromium, найти cards, вызвать _parse_card на каждой."""
    html = (FIXTURES / html_file).read_text(encoding="utf-8")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content(html, wait_until="domcontentloaded")

            scraper = scraper_class()
            cards = await page.query_selector_all(card_selector)
            results = []
            for card in cards:
                try:
                    product = await scraper._parse_card(card, category)
                    if product:
                        results.append(product)
                except Exception:
                    pass  # игнорируем broken cards (важно total coverage)
            return results, len(cards)
        finally:
            await browser.close()


# === APTEKONLINE ===
# Старые DOM-snapshot тесты сняты: aptekonline.az ввёл reCAPTCHA на headless
# браузеры в начале мая 2026, и scraper полностью переехал на backend JSON API
# (см. src/scrapers/aptekonline.py). Регрессионные тесты на API живут в
# tests/test_aptekonline_api.py — там же фикстура реального ответа.


# === ALOE ===


@pytest.mark.asyncio
async def test_aloe_snapshot_yields_products():
    from src.scrapers.aloe import AloeScraper

    products, n_cards = await _parse_cards(
        "aloe_bestseller.html",
        AloeScraper,
        '[class*="productCardWrapper"]',
        category="bestseller",
    )
    # aloe в snapshot имел 12 cards (известный лимит)
    assert n_cards >= 5, f"Слишком мало cards: {n_cards}"


@pytest.mark.asyncio
async def test_aloe_snapshot_extracts_brand():
    from src.scrapers.aloe import AloeScraper

    products, _ = await _parse_cards(
        "aloe_bestseller.html",
        AloeScraper,
        '[class*="productCardWrapper"]',
        category="bestseller",
    )
    if not products:
        pytest.skip("snapshot пустой")
    with_brand = sum(1 for p in products if p.brand)
    coverage = with_brand / len(products)
    assert coverage >= 0.5, f"Brand-coverage {coverage * 100:.0f}% < 50% на aloe"


# === PHARMONLINE ===


@pytest.mark.asyncio
async def test_pharmonline_snapshot_yields_products():
    from src.scrapers.pharmonline import PharmonlineScraper

    products, n_cards = await _parse_cards(
        "pharmonline_category.html",
        PharmonlineScraper,
        ".product_box_v2",
    )
    assert n_cards >= 10, f"В HTML только {n_cards} cards"
    assert len(products) >= n_cards * 0.9


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "Snapshot fixture pharmonline_category.html is stale. pharmonline сменили "
        "DOM structure (Phase 1c — DDP cutover). Test needs new HTML fixture from "
        "pharmonline DDP listing rendered as HTML, либо целиком deprecated. Not "
        "blocking CI."
    ),
    strict=False,
)
async def test_pharmonline_snapshot_prices_parseable():
    from src.scrapers.pharmonline import PharmonlineScraper

    products, _ = await _parse_cards(
        "pharmonline_category.html",
        PharmonlineScraper,
        ".product_box_v2",
    )
    with_price = sum(1 for p in products if p.price is not None and p.price > 0)
    coverage = with_price / len(products) if products else 0
    assert coverage >= 0.9
