"""Fuzzy-сопоставление товаров между 3 сайтами.

Алгоритм:
1. Берём все товары без `canonical_id` или из текущего прогона
2. Для каждого товара ищем кандидатов на других сайтах:
   - Точный матч по (brand, dosage, pack_size, name_normalized) → confidence=1.0
   - Fuzzy через rapidfuzz.token_set_ratio на name_normalized → если >= 90 → match
3. Создаём/обновляем запись в `matches` и проставляем `canonical_id`

Усечённое требование к точности:
- precision важнее recall: лучше не матчить, чем сматчить два разных товара
- порог 90 для fuzzy — намеренно высокий
- manual override через таблицу matches (is_manual=True) перекрывает автоматику
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

import structlog
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.match_actions import is_rejected
from src.normalize import normalize_name
from src.storage import Match, Product

log = structlog.get_logger()

FUZZY_THRESHOLD = 78  # 0..100, минимальный score для авто-матча.
# Понижено с 90: после strip_prefix + brand-unify в normalize.py пары
# одного бренда + pack_size в одном bucket дают ratio 78-87 для baby food.
# Bucket уже строго фильтрует по (brand, dosage, pack_size) → false positives
# редки и фильтруются через match_actions UI.


def match_products(session: Session, fuzzy_threshold: int = FUZZY_THRESHOLD) -> int:
    """Прогнать матчинг на всех товарах в БД.

    Не трогает товары с is_manual=True их Match.
    Возвращает количество новых/обновлённых связок.
    """
    products = session.scalars(select(Product)).all()
    if not products:
        return 0

    by_site: dict[str, list[Product]] = defaultdict(list)
    for p in products:
        by_site[p.site].append(p)

    sites = sorted(by_site.keys())
    log.info("matcher_start", total_products=len(products), sites=sites)

    # Группируем по эвристическому ключу для O(N*K) вместо O(N^2).
    # Если brand пустой — fallback на первые 2 значащих слова из name_normalized
    # (это сильно улучшает recall на товарах где скрейпер не вытащил brand —
    # таких как Friso Gold 1 / Pampers Premium — имя само по себе различимо).
    def bucket_key(p: Product) -> tuple:
        brand = (p.brand or "").lower().strip()
        dosage = (p.dosage or "").lower().replace(" ", "")
        pack = (p.pack_size or "").lower().replace(" ", "")
        if not brand:
            tokens = (p.name_normalized or "").split()
            brand = "_".join(tokens[:2]) if tokens else ""
        return (brand, dosage, pack)

    buckets: dict[tuple, list[Product]] = defaultdict(list)
    for p in products:
        buckets[bucket_key(p)].append(p)

    created_or_updated = 0
    visited: set[int] = set()

    for key, group in buckets.items():
        if len(group) < 2:
            continue
        # внутри ведра — попарно искать матчи между сайтами
        # group:[p1, p2, ...]
        for i, p in enumerate(group):
            if p.id in visited:
                continue
            cluster = [p]
            for q in group[i + 1 :]:
                if q.id in visited:
                    continue
                if q.site == p.site:
                    continue
                if any(c.site == q.site for c in cluster):
                    continue  # уже есть товар с этого сайта в кластере
                # Анти-матч: пара уже была развязана вручную — пропускаем
                if any(is_rejected(session, c.id, q.id) for c in cluster):
                    continue
                score = fuzz.token_set_ratio(p.name_normalized, q.name_normalized)
                if score >= fuzzy_threshold:
                    cluster.append(q)

            if len(cluster) >= 2:
                created_or_updated += _persist_match(session, cluster)
                visited.update(c.id for c in cluster)

    session.commit()
    log.info("matcher_done", clusters=created_or_updated)
    try:
        from src.observability import metrics

        metrics.matcher_clusters_total.inc(created_or_updated)
    except Exception:
        pass
    return created_or_updated


def _persist_match(session: Session, cluster: Sequence[Product]) -> int:
    """Создать или обновить Match для кластера товаров."""
    # Если у кого-то уже есть canonical_id — переиспользуем (если не is_manual)
    existing_ids = {p.canonical_id for p in cluster if p.canonical_id}
    if existing_ids:
        match_id = next(iter(existing_ids))
        match = session.get(Match, match_id)
        if match and match.is_manual:
            return 0  # ручной match — не трогаем
    else:
        match = None

    if not match:
        first = cluster[0]
        match = Match(
            canonical_name=first.name,
            canonical_brand=first.brand,
            canonical_dosage=first.dosage,
            canonical_pack_size=first.pack_size,
            confidence=1.0,
            is_manual=False,
        )
        session.add(match)
        session.flush()

    for p in cluster:
        p.canonical_id = match.id

    return 1


def find_matched_groups(session: Session) -> list[dict]:
    """Вернуть кластеры [(canonical_name, [products])] — для отчёта."""
    matches = session.scalars(select(Match)).all()
    out = []
    for m in matches:
        if not m.products:
            continue
        out.append(
            {
                "canonical_id": m.id,
                "name": m.canonical_name,
                "brand": m.canonical_brand,
                "dosage": m.canonical_dosage,
                "pack_size": m.canonical_pack_size,
                "products": list(m.products),
                "is_manual": m.is_manual,
            }
        )
    return out


def find_unmatched(session: Session) -> dict[str, list[Product]]:
    """Товары без canonical_id — кандидаты для gap-анализа."""
    products = session.scalars(
        select(Product).where(Product.canonical_id.is_(None))
    ).all()
    by_site: dict[str, list[Product]] = defaultdict(list)
    for p in products:
        by_site[p.site].append(p)
    return dict(by_site)


def normalize_for_matching(name: str) -> str:
    """Backwards-compat alias."""
    return normalize_name(name)
