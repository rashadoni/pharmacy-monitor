"""Regression тест: pharmonline DDP scrape_category дедуплицирует products
по external_id и не зацикливается при overlapping pagination.

Bug history (2026-05-28): прежний код не имел dedup → большие категории
выдавали ровно 10000 (cap), а в БД сохранялось ≤ 200 unique. Причина —
overlapping страницы от DDP server'а копили дубликаты в счётчике yields.

Этот тест эмулирует DDP-сервер который возвращает одни и те же 50 products
на каждом offset (worst case overlap). Проверяет:
- Yields = 50 (unique count), не 50 * N pages.
- Loop breaks после 3 zero-new pages.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.scrapers import pharmonline_ddp


def _fake_raw_product(idx: int) -> dict:
    """Минимальный raw DDP product для _build_product.

    Требуемые поля: name, _id, path (slug), totalMinPrice.
    """
    return {
        "_id": f"id-{idx}",
        "name": f"Product {idx}",
        "path": f"slug-{idx}",
        "category": "test-cat",
        "totalMinPrice": 10.0 + idx,
        "totalMaxPrice": 10.0 + idx,
    }


@pytest.mark.asyncio
async def test_ddp_scrape_dedups_overlapping_pages(monkeypatch):
    """DDP server возвращает одни и те же 50 products на 5 страницах подряд.
    Скрейпер должен yield'ить ровно 50 unique."""
    # 50 unique products, повторяемые на каждой странице
    fixed_page = [_fake_raw_product(i) for i in range(50)]

    call_count = {"n": 0}

    async def fake_ddp_call(method, params, timeout=30.0):
        call_count["n"] += 1
        if call_count["n"] > 10:
            return {"products": []}  # safety: stop infinite loops
        return {"products": fixed_page}

    # Construct scraper instance + inject mock DDP
    scraper = pharmonline_ddp.PharmonlineDDPScraper.__new__(pharmonline_ddp.PharmonlineDDPScraper)
    scraper._ddp = MagicMock()
    scraper._ddp.call = AsyncMock(side_effect=fake_ddp_call)
    scraper._locale = "az"
    scraper._cat_map = {}
    scraper._page_size = 50

    yielded = []
    async for sp in scraper.scrape_category("test-cat"):
        yielded.append(sp)

    # Должны получить ровно 50 unique (а не 500 при 10 страницах overlap)
    assert len(yielded) == 50
    # Все unique external_ids
    assert len({sp.external_id for sp in yielded}) == 50
    # DDP call'ы прервались early — не 10 раз, не больше 4 (1 initial + 3 zero-new streak)
    assert call_count["n"] <= 4


@pytest.mark.asyncio
async def test_ddp_scrape_breaks_on_short_page(monkeypatch):
    """Если страница вернула меньше page_size — это естественный end-of-stream."""
    pages = [
        [_fake_raw_product(i) for i in range(50)],  # full page
        [_fake_raw_product(i) for i in range(50, 70)],  # partial → end
    ]
    call_idx = {"n": 0}

    async def fake_ddp_call(method, params, timeout=30.0):
        i = call_idx["n"]
        call_idx["n"] += 1
        return {"products": pages[i] if i < len(pages) else []}

    scraper = pharmonline_ddp.PharmonlineDDPScraper.__new__(pharmonline_ddp.PharmonlineDDPScraper)
    scraper._ddp = MagicMock()
    scraper._ddp.call = AsyncMock(side_effect=fake_ddp_call)
    scraper._locale = "az"
    scraper._cat_map = {}
    scraper._page_size = 50

    yielded = []
    async for sp in scraper.scrape_category("test-cat"):
        yielded.append(sp)

    assert len(yielded) == 70  # 50 + 20


@pytest.mark.asyncio
async def test_ddp_scrape_respects_env_max_yield(monkeypatch):
    """`PHARMONLINE_DDP_MAX_YIELD_PER_CATEGORY=10` обрезает на 10."""
    monkeypatch.setenv("PHARMONLINE_DDP_MAX_YIELD_PER_CATEGORY", "10")
    big_page = [_fake_raw_product(i) for i in range(100)]

    async def fake_ddp_call(method, params, timeout=30.0):
        return {"products": big_page}

    scraper = pharmonline_ddp.PharmonlineDDPScraper.__new__(pharmonline_ddp.PharmonlineDDPScraper)
    scraper._ddp = MagicMock()
    scraper._ddp.call = AsyncMock(side_effect=fake_ddp_call)
    scraper._locale = "az"
    scraper._cat_map = {}
    scraper._page_size = 100

    yielded = []
    async for sp in scraper.scrape_category("test-cat"):
        yielded.append(sp)

    assert len(yielded) == 10


@pytest.mark.asyncio
async def test_ddp_scrape_explicit_limit_overrides_env(monkeypatch):
    """Explicit limit аргумент имеет приоритет над env."""
    monkeypatch.setenv("PHARMONLINE_DDP_MAX_YIELD_PER_CATEGORY", "1000")
    big_page = [_fake_raw_product(i) for i in range(100)]

    async def fake_ddp_call(method, params, timeout=30.0):
        return {"products": big_page}

    scraper = pharmonline_ddp.PharmonlineDDPScraper.__new__(pharmonline_ddp.PharmonlineDDPScraper)
    scraper._ddp = MagicMock()
    scraper._ddp.call = AsyncMock(side_effect=fake_ddp_call)
    scraper._locale = "az"
    scraper._cat_map = {}
    scraper._page_size = 100

    yielded = []
    async for sp in scraper.scrape_category("test-cat", limit=5):
        yielded.append(sp)

    assert len(yielded) == 5


@pytest.mark.asyncio
async def test_ddp_scrape_breaks_on_call_failure():
    """Exception в DDP.call → loop breaks (не raise)."""

    async def fake_ddp_call(method, params, timeout=30.0):
        raise RuntimeError("DDP timeout")

    scraper = pharmonline_ddp.PharmonlineDDPScraper.__new__(pharmonline_ddp.PharmonlineDDPScraper)
    scraper._ddp = MagicMock()
    scraper._ddp.call = AsyncMock(side_effect=fake_ddp_call)
    scraper._locale = "az"
    scraper._cat_map = {}
    scraper._page_size = 50

    yielded = []
    async for sp in scraper.scrape_category("test-cat"):
        yielded.append(sp)

    assert yielded == []
