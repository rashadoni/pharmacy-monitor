"""HTTP-валидация URL товаров → помечает мёртвые страницы (Product.url_dead_at).

Зачем: aptekonline JSON API (productList) листит «фантомные» товары — они есть
в каталоге (значит скрейпятся, last_seen свежий, matched), но публичная страница
товара отдаёт 404. Ни staleness (age=0), ни формат url_id, ни поле API их не
выдают — единственный надёжный сигнал это реальный HTTP-чек URL.

Запускать с НЕ-забаненного IP (Mac/Baku — aptekonline банит Hetzner-IP, прокси
для product-страниц тратил бы residential-трафик). См. CLI `validate-links`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx

# 404 Not Found / 410 Gone / 451 Unavailable — страница реально мертва.
# НЕ включаем 403/429/5xx — это транзиентный бан/троттлинг/сбой, помечать
# товар мёртвым по ним нельзя (ложно скрыл бы живой товар).
_DEAD_CODES = frozenset({404, 410, 451})
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


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


async def _check_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, pid: int, url: str):
    async with sem:
        try:
            r = await client.get(url)
            return pid, r.status_code
        except Exception:
            return pid, None


async def check_urls(
    items: list[tuple[int, str]],
    concurrency: int = 10,
    client: httpx.AsyncClient | None = None,
) -> dict[int, str]:
    """items: [(product_id, url)] → {product_id: 'dead'|'alive'|'error'}.

    GET (а не HEAD) — надёжнее ловит hard-404 (HEAD иногда кэшируется/405). Прямой
    запрос с Baku-IP бесплатен; follow_redirects ловит soft-redirect на 2xx.
    `client` инжектируется в тестах (httpx.MockTransport).
    """
    if not items:
        return {}
    sem = asyncio.Semaphore(concurrency)
    own = client is None
    if own:
        # trust_env=False: всегда прямое соединение (Baku-IP), игнорируя env-прокси
        # (HTTP_PROXY/ALL_PROXY). Job задуман direct с не-забаненного IP — случайный
        # системный/sandbox-прокси мог бы сломать или исказить проверку статуса.
        client = httpx.AsyncClient(
            follow_redirects=True, timeout=15.0, headers={"User-Agent": _UA}, trust_env=False
        )
    try:
        pairs = await asyncio.gather(*[_check_one(client, sem, pid, url) for pid, url in items])
    finally:
        if own:
            await client.aclose()
    return {pid: classify(code) for pid, code in pairs}


def apply_results(session, results: dict[int, str], now: datetime) -> dict[str, int]:
    """Применяет {pid: status} к Product.url_dead_at. error → не трогаем.

    Возвращает счётчики {newly_dead, revived, still_dead, error}.
    """
    from src.storage import Product

    counts = {"newly_dead": 0, "revived": 0, "still_dead": 0, "error": 0}
    for pid, status in results.items():
        p = session.get(Product, pid)
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
