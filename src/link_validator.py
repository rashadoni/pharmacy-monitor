"""HTTP-валидация URL товаров → помечает мёртвые страницы (Product.url_dead_at).

Зачем: aptekonline JSON API (productList) листит «фантомные» товары — они есть
в каталоге (значит скрейпятся, last_seen свежий, matched), но публичная страница
товара отдаёт 404. Ни staleness (age=0), ни формат url_id, ни поле API их не
выдают — единственный надёжный сигнал это реальный HTTP-чек URL.

БЕЗОПАСНОСТЬ (аудит 2026-05-30): запускать только с route, который имеет рабочий
доступ к сайту (server/proxy path). Чтобы не спровоцировать бан рабочего route:
  - rate-limit + джиттер (не burst),
  - circuit-breaker: при серии 403/429/timeout прогон ПРЕРЫВАЕТСЯ (не углубляем бан),
  - HEAD-first (минимум трафика и bot-сигнатуры),
  - mass-dead cap на стороне CLI (смена URL-схемы не должна обнулить comparison).
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime

import httpx

# 404 Not Found / 410 Gone / 451 Unavailable — страница реально мертва.
# НЕ включаем 403/429/5xx — это транзиентный бан/троттлинг/сбой, помечать
# товар мёртвым по ним нельзя (ложно скрыл бы живой товар И это ban-сигнал).
_DEAD_CODES = frozenset({404, 410, 451})
# HEAD не поддержан сервером → добиваем GET (легитимно). 403/429/5xx НЕ добиваем —
# это ban/throttle, второй запрос только усугубит.
_HEAD_FALLBACK_CODES = frozenset({405, 501})
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Доля мёртвых выше этой → аномалия (смена URL-схемы / maintenance), НЕ применять.
MAX_DEAD_FRACTION = 0.15


def classify(code: int | None) -> str:
    """HTTP-статус → 'dead' | 'alive' | 'error'.

    error (None=сетевой сбой, 403/429/5xx) → НЕ трогаем url_dead_at: транзиентная
    ошибка не должна ни помечать живой товар мёртвым, ни воскрешать мёртвый.
    """
    if code is None:
        return "error"
    if code in _DEAD_CODES:
        return "dead"
    if 200 <= code < 400:
        return "alive"
    return "error"


async def _fetch_status(client: httpx.AsyncClient, url: str) -> int | None:
    """HEAD-first (мин. трафик/сигнатура); GET-fallback только при 405/501.

    На 403/429/5xx НЕ делаем GET (это ban/throttle — второй запрос вреден).
    """
    try:
        r = await client.head(url)
        code = r.status_code
        if code in _HEAD_FALLBACK_CODES:
            r = await client.get(url)
            code = r.status_code
        return code
    except Exception:
        return None


async def check_urls(
    items: list[tuple[int, str]],
    *,
    concurrency: int = 5,
    rate_per_sec: float = 2.0,
    circuit_breaker: int = 8,
    client: httpx.AsyncClient | None = None,
) -> tuple[dict[int, str], dict]:
    """items: [(product_id, url)] → (results {pid: 'dead'|'alive'|'error'}, meta).

    meta = {checked, aborted}. Rate-limited (~rate_per_sec со случайным джиттером) +
    circuit-breaker: при `circuit_breaker` подряд идущих 'error' (ban-сигнатура)
    прогон прерывается — оставшиеся URL НЕ дёргаются (results их не содержит).
    `client` инжектируется в тестах (httpx.MockTransport).
    """
    if not items:
        return {}, {"checked": 0, "aborted": False}
    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(max(1, concurrency))
    interval = 1.0 / rate_per_sec if rate_per_sec and rate_per_sec > 0 else 0.0
    state = {"consec_err": 0, "aborted": False, "next_start": 0.0}
    results: dict[int, str] = {}
    gate = asyncio.Lock()
    own = client is None
    if own:
        # trust_env=False: всегда прямое соединение с текущего runtime-хоста,
        # игнорируя env-прокси.
        client = httpx.AsyncClient(
            follow_redirects=True, timeout=15.0, headers={"User-Agent": _UA}, trust_env=False
        )

    async def worker(pid: int, url: str) -> None:
        if state["aborted"]:
            return
        async with sem:
            if state["aborted"]:
                return
            # rate-limit: разносим СТАРТЫ запросов на ~interval + джиттер
            if interval:
                async with gate:
                    now = loop.time()
                    start_at = max(now, state["next_start"])
                    state["next_start"] = start_at + interval
                    delay = (start_at - now) + random.uniform(0, interval * 0.5)
                if delay > 0:
                    await asyncio.sleep(delay)
            status = classify(await _fetch_status(client, url))
            results[pid] = status
            if status == "error":
                state["consec_err"] += 1
                if state["consec_err"] >= circuit_breaker:
                    state["aborted"] = True
            else:
                state["consec_err"] = 0

    try:
        await asyncio.gather(*[worker(pid, url) for pid, url in items])
    finally:
        if own:
            await client.aclose()
    return results, {"checked": len(results), "aborted": state["aborted"]}


def dead_fraction(results: dict[int, str]) -> float:
    """Доля 'dead' среди проверенных (для mass-dead cap)."""
    if not results:
        return 0.0
    return sum(1 for s in results.values() if s == "dead") / len(results)


def apply_results(session, results: dict[int, str], now: datetime) -> dict[str, int]:
    """Применяет {pid: status} к Product.url_dead_at. error → не трогаем.

    Bulk-load товаров (чанками по 900 — SQLite-limit + щадит SSH-туннель), без N+1.
    Возвращает счётчики {newly_dead, revived, still_dead, error}.
    """
    from sqlalchemy import select

    from src.storage import Product

    ids = list(results.keys())
    by_id: dict[int, Product] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        for p in session.scalars(select(Product).where(Product.id.in_(chunk))).all():
            by_id[p.id] = p

    counts = {"newly_dead": 0, "revived": 0, "still_dead": 0, "error": 0}
    for pid, status in results.items():
        p = by_id.get(pid)
        if p is None:
            continue
        if status == "dead":
            if p.url_dead_at is None:
                counts["newly_dead"] += 1
            else:
                counts["still_dead"] += 1
            p.url_dead_at = now
        elif status == "alive":
            if p.url_dead_at is not None:
                counts["revived"] += 1
                p.url_dead_at = None
        else:  # error — транзиент, не трогаем
            counts["error"] += 1
    return counts
