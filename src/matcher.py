"""Сопоставление товаров между 3 сайтами.

Двухступенчатый алгоритм (2026-05-16):
1. **AI-attrs pass**: для продуктов с `normalized_attrs` (см. src/ai_normalize.py)
   bucket по `(active_ingredient, form, is_pharma)`. Внутри bucket'а — exact
   dosage_mg (±5%) + exact pack_count + fuzzy brand_canonical (>=85). Это
   снимает зависимость от написания имени и решает проблему aloe.az с
   синтетическим external_id.
2. **Legacy fuzzy pass**: для продуктов БЕЗ normalized_attrs (новые продукты
   ещё не прошли ai_normalize, не-pharma категории, или ai_normalize упал/
   вернул низкий confidence) — старый bucket `(brand, dosage, pack_size)` +
   `rapidfuzz.token_set_ratio >= 78` на name_normalized.

Backwards-compat:
- Match.is_manual=True не пересматривается ни в одном проходе
- Существующие 83 матча в проде сохраняются как есть
- Старые тесты (продукты без normalized_attrs) проходят через legacy path

Match.match_strategy фиксирует, какая стратегия дала match:
- 'ai_attrs_strict'   — все поля совпали (active_ingredient + dosage_mg + pack_count + brand)
- 'ai_attrs_partial'  — active_ingredient + 1-2 других поля
- 'legacy_fuzzy'      — старый token_set_ratio path
- 'manual'            — пользователь подтвердил через UI (is_manual=True)
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

# Legacy fuzzy threshold. 78 — после strip_prefix + brand-unify в normalize.py
# пары одного бренда + pack_size в одном bucket дают 78-87 ratio для baby food.
FUZZY_THRESHOLD = 78

# AI-attrs пути: бренд fuzzy threshold выше, потому что имя больше не сравнивается
# и attrs сами по себе высокоселективные.
AI_BRAND_FUZZY_THRESHOLD = 85
# Толерантность по dosage_mg для AI-strict матча. ±5% — позволяет склеить
# 100 mg и 99 mg, но не 100 mg и 75 mg.
DOSAGE_MG_TOLERANCE = 0.05


def match_products(session: Session, fuzzy_threshold: int = FUZZY_THRESHOLD) -> int:
    """Прогнать матчинг на всех товарах в БД.

    Не трогает товары у которых Match.is_manual=True.
    Возвращает количество новых/обновлённых связок.
    """
    products = list(session.scalars(select(Product)).all())
    if not products:
        return 0

    sites = sorted({p.site for p in products})
    log.info("matcher_start", total_products=len(products), sites=sites)

    visited: set[int] = set()
    created_or_updated = 0

    # ── Pass 1: AI-attrs matching ──
    ai_pending = [p for p in products if _has_ai_attrs(p)]
    if ai_pending:
        created_or_updated += _ai_attrs_pass(session, ai_pending, visited)

    # ── Pass 2: Legacy fuzzy (для всех, включая unmatched после AI-pass) ──
    legacy_pending = [p for p in products if p.id not in visited]
    if legacy_pending:
        created_or_updated += _legacy_fuzzy_pass(
            session, legacy_pending, fuzzy_threshold, visited
        )

    session.commit()
    log.info("matcher_done", clusters=created_or_updated)
    try:
        from src.observability import metrics

        metrics.matcher_clusters_total.inc(created_or_updated)
    except Exception:
        pass
    return created_or_updated


# ─── AI-attrs pass ───────────────────────────────────────────────────────────


def _has_ai_attrs(p: Product) -> bool:
    """Продукт пригоден для AI-attrs матчинга, если у него заполнен
    active_ingredient (иначе bucket будет null = бесполезен)."""
    if not p.normalized_attrs:
        return False
    return bool(p.normalized_attrs.get("active_ingredient"))


def _ai_bucket_key(p: Product) -> tuple:
    """`(active_ingredient, form, is_pharma)` — селективный bucket."""
    a = p.normalized_attrs or {}
    return (
        (a.get("active_ingredient") or "").lower().strip(),
        (a.get("form") or "").lower().strip(),
        bool(a.get("is_pharma", True)),
    )


def _ai_attrs_pass(
    session: Session, products: list[Product], visited: set[int]
) -> int:
    """Pass 1: матчинг по structured attrs.

    Bucket по (active_ingredient, form, is_pharma) → попарное сравнение
    dosage_mg + pack_count + brand_canonical.
    """
    buckets: dict[tuple, list[Product]] = defaultdict(list)
    for p in products:
        buckets[_ai_bucket_key(p)].append(p)

    created = 0
    for key, group in buckets.items():
        if len(group) < 2:
            continue
        # пустой active_ingredient — невалидный bucket
        if not key[0]:
            continue

        for i, p in enumerate(group):
            if p.id in visited:
                continue
            cluster: list[Product] = [p]
            best_strategy = "ai_attrs_strict"

            for q in group[i + 1 :]:
                if q.id in visited:
                    continue
                if q.site == p.site:
                    continue
                if any(c.site == q.site for c in cluster):
                    continue
                if any(is_rejected(session, c.id, q.id) for c in cluster):
                    continue

                strategy = _ai_score_pair(p, q)
                if strategy is None:
                    continue

                cluster.append(q)
                # ослабляем strategy кластера до самой слабой пары
                if strategy == "ai_attrs_partial":
                    best_strategy = "ai_attrs_partial"

            if len(cluster) >= 2:
                confidence = 0.95 if best_strategy == "ai_attrs_strict" else 0.75
                created += _persist_match(
                    session, cluster, strategy=best_strategy, confidence=confidence
                )
                visited.update(c.id for c in cluster)
    return created


def _ai_score_pair(p: Product, q: Product) -> str | None:
    """Сравнить пару продуктов по AI-attrs.

    Возвращает:
    - 'ai_attrs_strict'  — все 3 поля совпали (dosage_mg + pack_count + brand)
    - 'ai_attrs_partial' — 2 из 3 совпали
    - None               — недостаточно совпадений, не матчим
    """
    a = p.normalized_attrs or {}
    b = q.normalized_attrs or {}

    # is_pharma asymmetry — лекарства с косметикой не склеиваем (handled by bucket key, но проверим явно)
    if a.get("is_pharma") != b.get("is_pharma"):
        return None

    matches: list[bool] = []

    # 1. dosage_mg ±5%
    da, db = a.get("dosage_mg"), b.get("dosage_mg")
    if da is not None and db is not None:
        try:
            da_f, db_f = float(da), float(db)
            if da_f == 0 and db_f == 0:
                matches.append(True)
            elif max(da_f, db_f) == 0:
                matches.append(False)
            else:
                rel = abs(da_f - db_f) / max(da_f, db_f)
                matches.append(rel <= DOSAGE_MG_TOLERANCE)
        except (TypeError, ValueError):
            matches.append(False)
    else:
        # Если у одного есть dosage а у другого null — фиксируем mismatch только
        # если оба должны иметь dosage (is_pharma=True). Для БАДов/косметики null
        # это нормально, считаем neutral.
        if a.get("is_pharma") and (da is None or db is None):
            matches.append(False)

    # 2. pack_count точно совпадает
    pa, pb = a.get("pack_count"), b.get("pack_count")
    if pa is not None and pb is not None:
        try:
            matches.append(int(pa) == int(pb))
        except (TypeError, ValueError):
            matches.append(False)
    elif pa is None and pb is None:
        # оба null — нейтрально
        pass
    else:
        matches.append(False)

    # 3. brand_canonical fuzzy >=85 (или exact lower)
    ba = (a.get("brand_canonical") or p.brand or "").lower().strip()
    bb = (b.get("brand_canonical") or q.brand or "").lower().strip()
    if ba and bb:
        score = fuzz.token_set_ratio(ba, bb)
        matches.append(score >= AI_BRAND_FUZZY_THRESHOLD)
    elif not ba and not bb:
        pass  # оба пусты — нейтрально
    else:
        matches.append(False)

    if not matches:
        return None

    confirmed = sum(1 for m in matches if m)
    rejected = sum(1 for m in matches if not m)
    if rejected > 0:
        # хотя бы одно конкретное несовпадение — это разные продукты
        return None
    if confirmed >= 3:
        return "ai_attrs_strict"
    if confirmed >= 2:
        return "ai_attrs_partial"
    if confirmed >= 1:
        # один подтверждённый match при отсутствии явных mismatch'ей — слабо.
        # На активном веществе уже сматчены через bucket. Дозировка ИЛИ pack
        # ИЛИ бренд должны совпасть дополнительно — иначе риск false positive.
        return None
    return None


# ─── Legacy fuzzy pass ───────────────────────────────────────────────────────


def _legacy_bucket_key(p: Product) -> tuple:
    brand = (p.brand or "").lower().strip()
    dosage = (p.dosage or "").lower().replace(" ", "")
    pack = (p.pack_size or "").lower().replace(" ", "")
    if not brand:
        tokens = (p.name_normalized or "").split()
        brand = "_".join(tokens[:2]) if tokens else ""
    return (brand, dosage, pack)


def _legacy_fuzzy_pass(
    session: Session,
    products: list[Product],
    fuzzy_threshold: int,
    visited: set[int],
) -> int:
    """Pass 2: старый rapidfuzz путь для продуктов без normalized_attrs (или
    отвергнутых AI-pass'ом).
    """
    buckets: dict[tuple, list[Product]] = defaultdict(list)
    for p in products:
        buckets[_legacy_bucket_key(p)].append(p)

    created = 0
    for key, group in buckets.items():
        if len(group) < 2:
            continue
        for i, p in enumerate(group):
            if p.id in visited:
                continue
            cluster: list[Product] = [p]
            for q in group[i + 1 :]:
                if q.id in visited:
                    continue
                if q.site == p.site:
                    continue
                if any(c.site == q.site for c in cluster):
                    continue
                if any(is_rejected(session, c.id, q.id) for c in cluster):
                    continue
                score = fuzz.token_set_ratio(p.name_normalized, q.name_normalized)
                if score >= fuzzy_threshold:
                    cluster.append(q)
            if len(cluster) >= 2:
                created += _persist_match(
                    session, cluster, strategy="legacy_fuzzy", confidence=1.0
                )
                visited.update(c.id for c in cluster)
    return created


# ─── Persist ─────────────────────────────────────────────────────────────────


def _persist_match(
    session: Session,
    cluster: Sequence[Product],
    *,
    strategy: str,
    confidence: float,
) -> int:
    """Создать/обновить Match. is_manual защищает от перезаписи."""
    existing_ids = {p.canonical_id for p in cluster if p.canonical_id}
    if existing_ids:
        match_id = next(iter(existing_ids))
        match = session.get(Match, match_id)
        if match and match.is_manual:
            return 0  # ручной — не трогаем
    else:
        match = None

    if not match:
        first = cluster[0]
        match = Match(
            canonical_name=first.name,
            canonical_brand=first.brand,
            canonical_dosage=first.dosage,
            canonical_pack_size=first.pack_size,
            confidence=confidence,
            is_manual=False,
            match_strategy=strategy,
        )
        session.add(match)
        session.flush()
    else:
        # Обновляем strategy, если новая сильнее старой (промоут partial → strict)
        match.match_strategy = _stronger_strategy(match.match_strategy, strategy)
        # confidence не понижаем для существующих матчей
        if confidence > (match.confidence or 0):
            match.confidence = confidence

    for p in cluster:
        p.canonical_id = match.id

    return 1


_STRATEGY_RANK = {
    None: -1,
    "legacy_fuzzy": 0,
    "ai_attrs_partial": 1,
    "ai_attrs_strict": 2,
    "manual": 3,
}


def _stronger_strategy(old: str | None, new: str) -> str:
    """Не понижать стратегию существующего Match'а."""
    if _STRATEGY_RANK.get(new, -1) > _STRATEGY_RANK.get(old, -1):
        return new
    return old or new


# ─── Read helpers (unchanged) ────────────────────────────────────────────────


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
                "match_strategy": m.match_strategy,
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
