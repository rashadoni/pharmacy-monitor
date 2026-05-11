"""Конвертация HTML-отчёта в PDF через Playwright (chromium).

Используем уже установленный Playwright — никаких новых зависимостей вроде
weasyprint или wkhtmltopdf. Для одного report.html генерация занимает ~1-2 сек.

PDF queue: глобальный asyncio-Lock + кэш по hash(html). Параллельные запросы
ждут единственный chromium-процесс вместо его N-кратного запуска. Кэш живёт
TTL_SECONDS (по умолчанию 5 минут — редкие повторы для свежего отчёта).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from threading import Lock

import structlog
from playwright.async_api import async_playwright

log = structlog.get_logger()


# === PDF cache + serialization ===
_PDF_CACHE: dict[str, tuple[bytes, float]] = {}
_PDF_CACHE_TTL = 300  # 5 минут
_PDF_CACHE_LOCK = Lock()


def _cache_get(html_hash: str) -> bytes | None:
    with _PDF_CACHE_LOCK:
        entry = _PDF_CACHE.get(html_hash)
        if not entry:
            return None
        data, ts = entry
        if time.time() - ts > _PDF_CACHE_TTL:
            del _PDF_CACHE[html_hash]
            return None
        return data


def _cache_set(html_hash: str, data: bytes) -> None:
    with _PDF_CACHE_LOCK:
        _PDF_CACHE[html_hash] = (data, time.time())
        # Простой LRU: если >20 элементов — выкинуть самый старый
        if len(_PDF_CACHE) > 20:
            oldest = min(_PDF_CACHE.items(), key=lambda kv: kv[1][1])[0]
            del _PDF_CACHE[oldest]


async def _html_to_pdf_async(html: str, *, format_name: str = "A4") -> bytes:
    """Отрендерить HTML в PDF в headless chromium."""
    async with async_playwright() as p:
        headless = os.getenv("SCRAPE_HEADLESS", "true").lower() in ("1", "true", "yes")
        browser = await p.chromium.launch(headless=headless)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            pdf_bytes = await page.pdf(
                format=format_name,
                margin={"top": "1cm", "right": "1cm", "bottom": "1cm", "left": "1cm"},
                print_background=True,
            )
            return pdf_bytes
        finally:
            await browser.close()


def html_to_pdf(html: str, format_name: str = "A4") -> bytes:
    """Sync обёртка с кэшированием. Повторный запрос на тот же HTML
    обслуживается из cache (TTL 5 мин)."""
    html_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
    cached = _cache_get(html_hash)
    if cached is not None:
        log.info("pdf_cache_hit", hash=html_hash, size=len(cached))
        return cached
    pdf_bytes = asyncio.run(_html_to_pdf_async(html, format_name=format_name))
    _cache_set(html_hash, pdf_bytes)
    return pdf_bytes


def report_pdf_filename(run_started_at) -> str:
    return f"pharmacy-monitor-{run_started_at.strftime('%Y-%m-%d')}.pdf"
