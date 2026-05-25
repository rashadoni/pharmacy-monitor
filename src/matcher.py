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

import re
from collections import defaultdict
from typing import Sequence

import structlog
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.match_actions import is_rejected
from src.normalize import extract_form, normalize_name
from src.storage import Match, Product

log = structlog.get_logger()

FUZZY_THRESHOLD = 78  # 0..100, минимальный score для авто-матча.
# Понижено с 90: после strip_prefix + brand-unify в normalize.py пары
# одного бренда + pack_size в одном bucket дают ratio 78-87 для baby food.
# Bucket уже строго фильтрует по (brand, dosage, pack_size) → false positives
# редки и фильтруются через match_actions UI.

# Фармацевтические модификаторы — однобуквенные/короткие токены, означающие
# ДРУГОЙ состав препарата. Если у одного товара есть такой токен, а у другого
# нет → разные лекарства, матчинг запрещён.
# Примеры: Lopril vs Lopril H (H = гидрохлоротиазид), Eksforj vs Eksforj H,
#           Valsakor H vs Valsakor HD.
_PHARMA_MODIFIERS: frozenset[str] = frozenset({
    "h", "n", "d", "hd", "nd",           # комбо-добавки (HCT, диуретик и т.д.)
    "plus", "forte", "neo", "extra",       # усиленные/другие формулы
    "sr", "mr", "xr", "xl", "cr",         # модифицированное высвобождение
    "retard", "depot", "long", "mite",
    "ar",                                  # Anti-Reflux (Nutrilon AR, Gaviscon AR)
    "pre",                                 # Pre-формула (для недоношенных/новорождённых)
                                           # Nutrilon Pre ≠ Nutrilon 1/2/3
    "hipertonik",                          # гипертонический раствор ≠ изотонический
                                           # Marimer 100ml ≠ Marimer hipertonik 100ml
    "urso",                                # Fosfoqliv Urso (урсодезоксихолевая к-та)
                                           # ≠ обычный Fosfoqliv (только фосфолипиды)
})


# ── Серийный номер (ступень формулы) ────────────────────────────────────────
# Nutrilon 1 ≠ Nutrilon 4, Huggies 4 ≠ Huggies 5, Friso Gold 1 ≠ Friso Gold 3.
# Матчим ТОЛЬКО одиночные цифры 1-9 с границами слова: «\b1\b» не ловит «10mg»
# или «N20» (там нет правой/левой word-boundary), только свободностоящие цифры.
_SERIES_NUM_RE = re.compile(r"\b([1-9])\b")


def _has_conflicting_series_number(name_a: str, name_b: str) -> bool:
    """True если оба имени содержат серийный номер (ступень), но он разный.

    Расширенный случай: если только у ОДНОГО имени есть серийный номер,
    а у ДРУГОГО есть значащие уникальные токены (длиной ≥ 4, отсутствующие
    в первом) — это именованный вариант («Nutrilon Pronutra Pre», «Nutrilon
    hipoallergik») против нумерованной ступени («Nutrilon 3», «Nutrilon 4») →
    блокируем.

    Пропускает: «Nutrilon» (нет уникальных токенов) vs «Nutrilon 1» —
    неполные данные, матчинг разрешаем.
    """
    nums_a = frozenset(m.group(1) for m in _SERIES_NUM_RE.finditer(name_a))
    nums_b = frozenset(m.group(1) for m in _SERIES_NUM_RE.finditer(name_b))
    if not nums_a and not nums_b:
        return False  # ни у одного нет серийного номера
    if nums_a and nums_b:
        return nums_a != nums_b  # у обоих есть номер — сравниваем напрямую
    # Только у одного есть номер: проверяем, есть ли у другого уникальные токены
    # Если да — это именованный вариант (Nutrilon AR, Nutrilon hipoallergik),
    # а не просто неполное имя (Nutrilon без цифры).
    tokens_no_series = frozenset(name_a.split()) if not nums_a else frozenset(name_b.split())
    tokens_with_series = frozenset(name_b.split()) if not nums_a else frozenset(name_a.split())
    unique_meaningful = {
        t for t in tokens_no_series - tokens_with_series
        if len(t) >= 4
    }
    if unique_meaningful:
        return True  # именованный вариант vs нумерованная ступень → разные
    return False  # бренд-only без токенов vs номер — скорее неполные данные


# ── Форма выпуска ────────────────────────────────────────────────────────────
def _has_conflicting_form(name_raw_a: str, name_raw_b: str) -> bool:
    """True если формы выпуска явно конфликтуют (drops ≠ spray, cream ≠ ointment).

    Работает на оригинальных (ненормализованных) именах — в name_normalized
    форма уже вырезана нормализатором.
    Пропускает пару, если форма не определена хотя бы у одного.
    """
    form_a = extract_form(name_raw_a)
    form_b = extract_form(name_raw_b)
    if form_a is None or form_b is None:
        return False  # форма неизвестна — не блокируем
    return form_a != form_b


# ── Диспропорция длины (stub vs полное имя) ──────────────────────────────────
# token_set_ratio("venatura", "venatura vitamin a palmitate retinol zinc") = 100
# потому что короткая строка — подмножество длинной. Это false positive:
# brand-stub ≠ конкретный продукт с тем же брендом.
# Порог 0.40: если более короткое имя < 40% длины более длинного (по словам)
# → скорее всего это разные товары (или недостаток данных), не матчим.
# Защита: проверяем только если длинное >= 3 слов (совсем короткие пары ОК).
# ── Orphan-числа (осиротевшая сила/вариант препарата) ───────────────────────
# После normalize_name() числа из dosage/pack убираются. Если 2+-значное
# число ОСТАЛОСЬ в name_normalized — оно не было распознано как дозировка
# (напр. «3500 ed» пока ED не был в _DOSAGE_RE, или нестандартная единица).
# Правило: если у одного товара такое число есть, а у другого нет (или другое)
# → разные варианты/силы → матчинг запрещён.
# Примеры:
#   «mezim forte»   (10000 İU удалён корректно) vs
#   «mezim forte 3500 ed»  (3500 остался, ED не был в units)  → BLOCK
#   «mezim forte 3500» vs «mezim forte 3500» → OK (одинаковые)
#   «paracetamol» vs «paracetamol» → OK (нет чисел)
_MULTI_DIGIT_RE = re.compile(r"\b(\d{2,})\b")


def _has_conflicting_orphan_number(name_a: str, name_b: str) -> bool:
    """True если в name_normalized осталось незачищенное 2+-значное число,
    и оно разное (или есть только у одного).

    Защищает от ложных матчей типа Mezim forte 10000 İU ↔ Mezim forte 3500 ED:
    первый нормализуется в «mezim forte» (İU → strip_accents → IU, dosage срипнут),
    второй в «mezim forte 3500 ed» (ED не был в _DOSAGE_RE → осталось).
    """
    nums_a = frozenset(m.group(1) for m in _MULTI_DIGIT_RE.finditer(name_a))
    nums_b = frozenset(m.group(1) for m in _MULTI_DIGIT_RE.finditer(name_b))
    if nums_a == nums_b:  # {} vs {} или {3500} vs {3500}
        return False
    return True  # разные числа или одно пустое → блокируем


_DISPARITY_MAX_RATIO = 0.40
_DISPARITY_MIN_LONGER = 3


def _has_extreme_length_disparity(name_a: str, name_b: str) -> bool:
    """True если одно имя подозрительно короче другого.

    Блокирует матч типа «venatura» (1 слово) ↔
    «venatura vitamin a palmitate retinol» (5 слов), где
    token_set_ratio=100 из-за subset-логики rapidfuzz.
    """
    words_a = len(name_a.split())
    words_b = len(name_b.split())
    if words_a == 0 or words_b == 0:
        return False
    longer = max(words_a, words_b)
    if longer < _DISPARITY_MIN_LONGER:
        return False  # оба имени короткие — нормально
    shorter = min(words_a, words_b)
    return shorter / longer < _DISPARITY_MAX_RATIO


def _has_conflicting_modifier(name_a: str, name_b: str) -> bool:
    """True если одно название содержит фарма-модификатор, а другое — нет.

    Это означает разные препараты (разный состав/формула) → матчинг запрещён.
    Работает на уже нормализованных именах (lowercase, без дозировки/упаковки).
    """
    tokens_a = frozenset(name_a.split())
    tokens_b = frozenset(name_b.split())
    mod_a = tokens_a & _PHARMA_MODIFIERS
    mod_b = tokens_b & _PHARMA_MODIFIERS
    # Если модификаторы у обоих одинаковы — ок (оба H, оба SR и т.д.)
    # Если у одного есть модификатор, а у другого нет — разные препараты
    return mod_a != mod_b


# ── Гендерные токены ─────────────────────────────────────────────────────────
# Huggies oğlanlar N64 ≠ Huggies qızlar N64: мальчики vs девочки.
# После normalize_name() ğ → g (breve strip) → "oglanlar".
# Азербайджанское ı (U+0131, dotless i) в "qız" СОХРАНЯЕТСЯ нормализатором
# (нет NFKD-декомпозиции) → нужны оба варианта "qız" и "qiz" для надёжности.
_GENDER_MALE: frozenset[str] = frozenset({
    "oğlan", "oglan", "oğlanlar", "oglanlar",
    "boy", "boys", "erkek",
})
_GENDER_FEMALE: frozenset[str] = frozenset({
    "qız", "qiz", "qızlar", "qizlar",   # qız / qızlar (dotless-i и ASCII)
    "girl", "girls", "qadın", "qadin",        # qadın / qadin
})


def _has_conflicting_gender(name_a: str, name_b: str) -> bool:
    """True если одно название содержит мужской гендерный токен, а другое — женский.

    Блокирует ложные матчи типа «Huggies oglanlar N64» ↔ «Huggies qız N64»:
    мальчики vs девочки — принципиально разные продукты.
    Пропускает пару, если гендер определён только у одного (неполные данные).
    """
    tokens_a = frozenset(name_a.split())
    tokens_b = frozenset(name_b.split())
    male_a = bool(tokens_a & _GENDER_MALE)
    female_a = bool(tokens_a & _GENDER_FEMALE)
    male_b = bool(tokens_b & _GENDER_MALE)
    female_b = bool(tokens_b & _GENDER_FEMALE)
    return (male_a and female_b) or (female_a and male_b)


# ── Уникальные вариантные токены ─────────────────────────────────────────────
# «splat aktiv» ≠ «splat lavandasept»: у ОБОИХ имён есть уникальный токен
# (aktiv / lavandasept), который отсутствует у другого. Это признак разных
# продуктовых вариантов, а не просто разной степени полноты названия.
# Правило: если только У ОДНОГО есть уникальный токен — допускаем матч
# (скорее всего неполные данные на одном из сайтов, а не другой вариант).
_MIN_VARIANT_TOKEN_LEN = 3  # токены короче 3 символов игнорируем ("h", "sr", "b")
# Дополнительно: короткие (2 символа) алфавитно-цифровые токены (B12 → "b12",
# D3 → "d3", 2X → "2x") — значащие идентификаторы продуктов, даже если короткие.


def _is_significant_variant_token(t: str) -> bool:
    """True если токен значащий: длина ≥ 3, или длина ≥ 2 и содержит и букву и цифру.

    Примеры значащих: "bal" (3), "aktiv" (5), "b12" (3, буква+цифра),
                      "d3" (2, буква+цифра), "2x" (2, буква+цифра).
    Примеры незначащих: "b" (1), "h" (1), "50" (только цифры), "ml" (только буквы, 2).
    """
    if len(t) >= _MIN_VARIANT_TOKEN_LEN:
        return True
    # Короткие (2 символа) алфавитно-цифровые: витамины B12/D3, формулы 2X/C3 и т.д.
    if len(t) >= 2 and any(c.isdigit() for c in t) and any(c.isalpha() for c in t):
        return True
    return False


def _has_conflicting_variant_tokens(name_a: str, name_b: str) -> bool:
    """True если оба имени имеют значащие уникальные токены (отсутствующие в другом).

    Блокирует ложные матчи типа:
    - «splat aktiv» ↔ «splat lavandasept» (token_set_ratio=84, общая база «splat»)
    - «venatura methylfolate» ↔ «venatura b12» (b12 = 3-символьный код)
    - «makson» ↔ «makson d3» (d3 = 2-символьный алфавитно-цифровой код)
    - «safeguard bal» ↔ «safeguard limon fresh» (bal = 3 символа)

    Пропускает, если только у одного имени есть уникальный токен — это
    интерпретируется как неполное имя (напр. «amoxicillin» vs «amoxicillin trihydrate»),
    а не как разный вариант препарата.
    """
    tokens_a = frozenset(name_a.split())
    tokens_b = frozenset(name_b.split())
    unique_a = {t for t in tokens_a - tokens_b if _is_significant_variant_token(t)}
    unique_b = {t for t in tokens_b - tokens_a if _is_significant_variant_token(t)}
    return bool(unique_a) and bool(unique_b)


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
    #
    # Дополнение (2026-05-25): aloe.az хранит ПРОИЗВОДИТЕЛЯ в brand (Alcon,
    # Bausch+Lomb, İlaçsan Medikal), а не торговое название продукта. Такой brand
    # не встречается в name_normalized («tobradex», «renu multiplus») → кластер
    # попадает не в тот bucket. Эвристика: если бренд НЕ встречается подстрокой в
    # name_normalized — это имя производителя, а не продукта → падаем на первое
    # слово name_normalized, которое обычно и есть торговое название.
    def bucket_key(p: Product) -> tuple:
        brand = (p.brand or "").lower().strip()
        dosage = (p.dosage or "").lower().replace(" ", "")
        pack = (p.pack_size or "").lower().replace(" ", "")
        name_norm = (p.name_normalized or "").lower()
        tokens = name_norm.split()
        if brand:
            # Проверяем, встречается ли хотя бы одно слово brand в name_normalized.
            # Если нет — это имя производителя (aloe-паттерн), используем первое
            # слово name_normalized как ключ (торговое название).
            if not any(word in name_norm for word in brand.split()):
                brand = tokens[0] if tokens else brand
        else:
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
                # ── Проверки против ВСЕХ членов кластера (не только якоря p) ──
                # Предотвращает транзитивные ложные матчи: «голое» имя-якорь
                # (fosfoqliv, spris) не конфликтует с собой → становится мостом
                # между несовместимыми вариантами. Теперь q проверяется против
                # всего кластера перед добавлением.
                #
                # Фарма-модификатор: Lopril vs Lopril H → разные препараты
                if any(
                    _has_conflicting_modifier(c.name_normalized or "", q.name_normalized or "")
                    for c in cluster
                ):
                    continue
                # Серийный номер/ступень: Nutrilon 1 vs Nutrilon 4 → разные
                if any(
                    _has_conflicting_series_number(c.name_normalized or "", q.name_normalized or "")
                    for c in cluster
                ):
                    continue
                # Форма выпуска: drops vs spray, cream vs ointment → разные
                # (проверяем только против якоря — форма берётся из raw name)
                if _has_conflicting_form(p.name or "", q.name or ""):
                    continue
                # Stub vs полное имя: «venatura» vs «venatura vitamin a palmitate…»
                # (проверяем только против якоря — длина якоря самая репрезентативная)
                if _has_extreme_length_disparity(
                    p.name_normalized or "", q.name_normalized or ""
                ):
                    continue
                # Осиротевшее число дозировки: «mezim forte» vs «mezim forte 3500 ed»
                if _has_conflicting_orphan_number(
                    p.name_normalized or "", q.name_normalized or ""
                ):
                    continue
                # Гендерный конфликт: мальчики vs девочки → разные продукты
                if _has_conflicting_gender(
                    p.name_normalized or "", q.name_normalized or ""
                ):
                    continue
                # Вариантный конфликт: splat aktiv vs splat lavandasept → разные варианты
                # Проверяем против ВСЕХ членов кластера (транзитивная защита).
                if any(
                    _has_conflicting_variant_tokens(c.name_normalized or "", q.name_normalized or "")
                    for c in cluster
                ):
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
    """Создать или обновить Match для кластера товаров.

    Snowball-guard (фикс 2026-05-25):
    При добавлении новых продуктов к существующему матчу проверяем,
    что для каждого нового продукта его сайт ещё не представлен в матче.
    Без этой проверки кластер рос за несколько прогонов: aloe-продукт
    с canonical_id=X «заражал» следующую пару (aptk+pharm), те получали X,
    в следующем прогоне та пара заражала ещё одну — кластер раздувался
    до 70+ разных вариантов одного бренда.
    """
    # Если у кого-то уже есть canonical_id — переиспользуем (если не is_manual)
    existing_ids = {p.canonical_id for p in cluster if p.canonical_id}
    if existing_ids:
        match_id = next(iter(existing_ids))
        match = session.get(Match, match_id)
        if match and match.is_manual:
            return 0  # ручной match — не трогаем
        # Snowball-guard: не добавляем продукт если его сайт уже есть в матче
        if match:
            occupied_sites = {
                p.site for p in match.products if p.canonical_id == match_id
            }
            new_products = [p for p in cluster if p.canonical_id != match_id]
            for np in new_products:
                if np.site in occupied_sites:
                    log.debug(
                        "persist_match_site_conflict",
                        match_id=match_id,
                        site=np.site,
                        product_id=np.id,
                    )
                    return 0  # конфликт сайтов — пропускаем
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
