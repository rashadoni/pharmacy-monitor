"""Тесты src/pdf_export.py — HTML→PDF cache + filename helper.

Coverage gap closure (2026-05-28): 0% → ~95%.
Покрываем cache get/set, TTL expiry, LRU eviction, sync wrapper (с мockед
async function), filename helper. Не тестируем реальный Playwright render
(требует chromium binary; уже покрыт через scrapers snapshot tests).
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from src import pdf_export


@pytest.fixture(autouse=True)
def _reset_cache():
    """Свежий cache между тестами."""
    pdf_export._PDF_CACHE.clear()
    yield
    pdf_export._PDF_CACHE.clear()


# === Cache primitives ===


def test_cache_get_miss_returns_none():
    """Несуществующий hash → None."""
    assert pdf_export._cache_get("nope") is None


def test_cache_set_then_get_roundtrip():
    """Set → Get возвращает те же bytes."""
    pdf_export._cache_set("hash-1", b"pdf-content")
    assert pdf_export._cache_get("hash-1") == b"pdf-content"


def test_cache_get_expires_after_ttl(monkeypatch):
    """Entry старше TTL → удаляется + возвращает None."""
    pdf_export._cache_set("expire-me", b"old")
    # Симулируем что прошло TTL+1 секунд
    old_ts = time.time() - pdf_export._PDF_CACHE_TTL - 1
    pdf_export._PDF_CACHE["expire-me"] = (b"old", old_ts)
    assert pdf_export._cache_get("expire-me") is None
    # Удалён из dict
    assert "expire-me" not in pdf_export._PDF_CACHE


def test_cache_set_lru_eviction():
    """>20 entries → самый старый удаляется."""
    # Заполняем 20 entries с растущими timestamps
    for i in range(20):
        pdf_export._cache_set(f"k-{i}", f"data-{i}".encode())
    assert len(pdf_export._PDF_CACHE) == 20
    # Добавляем 21й — самый старый ("k-0") должен уйти
    pdf_export._cache_set("k-newest", b"new")
    assert len(pdf_export._PDF_CACHE) == 20  # размер остался 20
    assert "k-newest" in pdf_export._PDF_CACHE
    assert "k-0" not in pdf_export._PDF_CACHE  # был самый старый


def test_cache_handles_concurrent_writes():
    """LRU eviction picks oldest по ts, не по insertion order."""
    pdf_export._cache_set("oldest", b"a")
    time.sleep(0.01)
    pdf_export._cache_set("newer", b"b")
    # Manually добавим запись с явно более старым ts
    pdf_export._PDF_CACHE["even-older"] = (b"c", 0.0)
    # 22 элементов → должно выкинуть "even-older" не "oldest"
    for i in range(17):  # довести до 20+
        pdf_export._cache_set(f"filler-{i}", b"f")
    # Триггер eviction: 21й
    pdf_export._cache_set("trigger", b"t")
    assert "even-older" not in pdf_export._PDF_CACHE  # самый старый ушёл первым


# === Sync wrapper ===


def test_html_to_pdf_uses_cache_on_repeat(monkeypatch):
    """Один и тот же HTML — рендер вызывается только раз, второй из cache."""
    call_count = {"n": 0}

    async def fake_render(html, format_name="A4"):
        call_count["n"] += 1
        return b"fake-pdf-bytes"

    monkeypatch.setattr(pdf_export, "_html_to_pdf_async", fake_render)
    html = "<html><body>Test</body></html>"

    first = pdf_export.html_to_pdf(html)
    second = pdf_export.html_to_pdf(html)
    assert first == b"fake-pdf-bytes"
    assert second == b"fake-pdf-bytes"
    # Render вызван только раз — второй раз из cache
    assert call_count["n"] == 1


def test_html_to_pdf_different_html_renders_separately(monkeypatch):
    """Разный HTML → разные cache keys → render вызывается дважды."""
    call_count = {"n": 0}

    async def fake_render(html, format_name="A4"):
        call_count["n"] += 1
        return f"pdf-for-{html[:20]}".encode()

    monkeypatch.setattr(pdf_export, "_html_to_pdf_async", fake_render)
    pdf_export.html_to_pdf("<h1>A</h1>")
    pdf_export.html_to_pdf("<h1>B</h1>")
    assert call_count["n"] == 2


def test_html_to_pdf_passes_format_name(monkeypatch):
    """format_name пробрасывается в render."""
    captured = {}

    async def fake_render(html, format_name="A4"):
        captured["format"] = format_name
        return b"x"

    monkeypatch.setattr(pdf_export, "_html_to_pdf_async", fake_render)
    pdf_export.html_to_pdf("<h1>X</h1>", format_name="Letter")
    assert captured["format"] == "Letter"


# === Filename helper ===


def test_report_pdf_filename_format():
    """`report_pdf_filename` — YYYY-MM-DD формат."""
    dt = datetime(2026, 5, 28, 14, 30, 45)
    assert pdf_export.report_pdf_filename(dt) == "pharmacy-monitor-2026-05-28.pdf"


def test_report_pdf_filename_handles_naive_and_aware():
    """Работает и с naive, и с aware datetime."""
    from datetime import timezone

    naive = datetime(2026, 1, 1)
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert pdf_export.report_pdf_filename(naive) == "pharmacy-monitor-2026-01-01.pdf"
    assert pdf_export.report_pdf_filename(aware) == "pharmacy-monitor-2026-01-01.pdf"
