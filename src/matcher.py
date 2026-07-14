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
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.brand_catalog import is_brand_blacklisted
from src.brand_resolver import brands_conflict, is_commodity_name
from src.match_actions import is_rejected
from src.normalize import (
    extract_form,
    extract_pack_size,
    extract_total_volume,
    normalize_name,
    pack_unit_count,
    strip_accents,
)
from src.storage import (
    Match,
    MatchPolicyAudit,
    MatchRejection,
    PriceSnapshot,
    Product,
    latest_snapshots_per_product,
)

log = structlog.get_logger()

MATCH_MUTATION_ADVISORY_LOCK_KEY = "pharmacy_monitor_matcher"


def _is_postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def acquire_match_mutation_xact_lock(session: Session) -> None:
    """Serialize one transaction with every canonical topology mutation."""
    if _is_postgres(session):
        session.scalar(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": MATCH_MUTATION_ADVISORY_LOCK_KEY},
        )


def acquire_match_mutation_lock(session: Session, *, wait: bool = True) -> bool:
    """Session-level lock for multi-transaction operations and rollback."""
    if not _is_postgres(session):
        return True
    fn = "pg_advisory_lock" if wait else "pg_try_advisory_lock"
    value = session.scalar(
        text(f"SELECT {fn}(hashtext(:key))"),
        {"key": MATCH_MUTATION_ADVISORY_LOCK_KEY},
    )
    return True if wait else bool(value)


def release_match_mutation_lock(session: Session) -> None:
    if _is_postgres(session):
        session.scalar(
            text("SELECT pg_advisory_unlock(hashtext(:key))"),
            {"key": MATCH_MUTATION_ADVISORY_LOCK_KEY},
        )

FUZZY_THRESHOLD = 75  # 0..100, минимальный score для авто-матча.
# Снижено с 78 → 75 (2026-05-26): bucket (brand, dosage, pack) уже строго
# фильтрует — дополнительные 3 пункта дают ~2-4% recall на коротких именах
# (5-6 токенов), где реальные матчи дают 75-77. False positives
# фильтруются через match_actions UI.

# Фармацевтические модификаторы — однобуквенные/короткие токены, означающие
# ДРУГОЙ состав препарата. Если у одного товара есть такой токен, а у другого
# нет → разные лекарства, матчинг запрещён.
# Примеры: Lopril vs Lopril H (H = гидрохлоротиазид), Eksforj vs Eksforj H,
#           Valsakor H vs Valsakor HD.
_PHARMA_MODIFIERS: frozenset[str] = frozenset(
    {
        "h",
        "n",
        "d",
        "hd",
        "nd",  # комбо-добавки (HCT, диуретик и т.д.)
        "plus",
        "forte",
        "neo",
        "extra",  # усиленные/другие формулы
        "sr",
        "mr",
        "xr",
        "xl",
        "cr",  # модифицированное высвобождение
        "retard",
        "depot",
        "long",
        "mite",
        "ar",  # Anti-Reflux (Nutrilon AR, Gaviscon AR)
        "pre",  # Pre-формула (для недоношенных/новорождённых)
        # Nutrilon Pre ≠ Nutrilon 1/2/3
        "hipertonik",  # гипертонический раствор ≠ изотонический
        # Marimer 100ml ≠ Marimer hipertonik 100ml
        "urso",  # Fosfoqliv Urso (урсодезоксихолевая к-та)
        # ≠ обычный Fosfoqliv (только фосфолипиды)
    }
)


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
    unique_meaningful = {t for t in tokens_no_series - tokens_with_series if len(t) >= 4}
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

# Шумовые токены, которые НЕ являются дифференциатором товара. Используются
# stub-aware disparity guard'ом (см. ниже): при сравнении «короткое vs длинное»
# имя эти токены не считаются «значимым пересечением». Это позволяет отличить
# brand-stub (venatura) от verbose-superset (тот же товар с маркетинг-хвостом).
#
# Источник (2026-05-29): pharmonline DDP-имена пихают в скобки категорию/бренд-
# хаус/страну/маркетинг: «(Kosmetika) (Herba Flora) (Azərbaycan) Ultra Care
# d/norm, saç üçün». normalize_name снимает скобки-пунктуацию, но СЛОВА остаются
# и раздувают token-count → ложный disparity-block против лаконичного aptekonline.
# Дизайн подтверждён Perplexity + Codex (independent review, оба сошлись на
# «stub-aware overlap» вместо raw length ratio).
_MATCH_NOISE_TOKENS: frozenset[str] = frozenset(
    {
        # категория/тип косметики (AZ) — не дифференциатор товара
        "sampun",
        "krem",
        "gel",
        "balzam",
        "maska",
        "losyon",
        "kosmetika",
        "kosmetik",
        # маркетинг-линии / generic качества
        "ultra",
        "care",
        "extra",
        "premium",
        "classic",
        "professional",
        # бренд-хаус слова, дублирующие brand-поле (pharmonline parens)
        "herba",
        "flora",
        # generic дескрипторы волос/кожи (AZ)
        "sac",
        "ucun",
        "norm",
        "normal",
        "quru",
        "yagli",
        "deri",
        # остатки страны (если _COUNTRY_RE не добил)
        "azerbaycan",
        "azerbaijan",
    }
)


def _is_significant_for_disparity(
    tok: str, brand_tokens: frozenset[str] | set[str], noise: frozenset[str]
) -> bool:
    """Токен «значимый» (дифференцирует товар) для stub-проверки.

    Исключаем: brand-токены, noise-токены, фарма-модификаторы, короткие (<3).
    """
    if tok in brand_tokens:
        return False
    if tok in noise:
        return False
    if tok in _PHARMA_MODIFIERS:
        return False
    if len(tok) < 3:
        return False
    return True


def _has_extreme_length_disparity(name_a: str, name_b: str, brand_hint: str = "") -> bool:
    """True если короткое имя — brand/generic STUB длинного (разные товары).

    Stub-aware redesign (2026-05-29, Perplexity+Codex consensus): прежняя версия
    блокировала ЛЮБУЮ пару с token-ratio < 0.40 — это давало массовый false-
    negative для verbose-vs-terse легитимных пар (pharmonline раздувает имена
    маркетинг-хвостом). Теперь:

    - Если длины не диспропорциональны (ratio ≥ 0.40) → не блокируем.
    - Если диспропорциональны → блокируем ТОЛЬКО когда у короткого имени НЕТ
      значимого (non-brand, non-noise, len≥3) токена, общего с длинным.
      * venatura (brand-stub) ↔ venatura vitamin a palmitate: после удаления
        бренда у короткого 0 значимых токенов → BLOCK (precision сохранён).
      * fitoton sampun cobanyastıgı ↔ …+ultra care kosmetika herba flora:
        «cobanyastıgı» (ромашка) общий и значимый → verbose-superset → ALLOW.

    `brand_hint` — бренд из bucket_key (или auto-brand). Если пуст — строже:
    требуем ≥2 общих значимых токена (защита когда бренд не известен).
    """
    words_a = name_a.split()
    words_b = name_b.split()
    if not words_a or not words_b:
        return False
    longer = words_a if len(words_a) >= len(words_b) else words_b
    shorter = words_b if longer is words_a else words_a
    if len(longer) < _DISPARITY_MIN_LONGER:
        return False  # оба короткие — нормально
    if len(shorter) / len(longer) >= _DISPARITY_MAX_RATIO:
        return False  # длины сопоставимы — не stub

    brand_tokens = set(brand_hint.lower().split())
    short_sig = {
        t for t in shorter if _is_significant_for_disparity(t, brand_tokens, _MATCH_NOISE_TOKENS)
    }
    long_sig = {
        t for t in longer if _is_significant_for_disparity(t, brand_tokens, _MATCH_NOISE_TOKENS)
    }
    overlap = short_sig & long_sig
    # Без brand-hint строже: требуем 2+ общих значимых токена, иначе единственное
    # совпадение могло быть самим брендом (venatura) → ложный allow.
    min_overlap = 1 if brand_tokens else 2
    if len(overlap) >= min_overlap:
        return False  # общий дифференциатор → verbose superset → разрешаем
    return True  # короткое — brand/generic stub → блокируем


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
_GENDER_MALE: frozenset[str] = frozenset(
    {
        "oğlan",
        "oglan",
        "oğlanlar",
        "oglanlar",
        "boy",
        "boys",
        "erkek",
    }
)
_GENDER_FEMALE: frozenset[str] = frozenset(
    {
        "qız",
        "qiz",
        "qızlar",
        "qizlar",  # qız / qızlar (dotless-i и ASCII)
        "girl",
        "girls",
        "qadın",
        "qadin",  # qadın / qadin
    }
)


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

# Hard-различители (2026-05-29): токены, которые меняют идентичность товара
# ДАЖЕ если присутствуют только у одной стороны (в отличие от verbose-суффиксов
# вроде «trihydrate»). «toxumu»/«toxum» = семена: «Bağayarpağı» (подорожник-лист)
# ≠ «Bağayarpağı toxumu» (семена подорожника) — это разные товары. Держим
# список МАКСИМАЛЬНО узким — только однозначные различители, чтобы не блокировать
# легитимные verbose-vs-terse пары.
_HARD_DISTINCT_TOKENS: frozenset[str] = frozenset({"toxumu", "toxum"})
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
    - «akriderm sk» ↔ «akriderm qk» (sk/qk — 2-буквенные фармкоды, оба уникальны)

    Пропускает, если только у одного имени есть уникальный токен — это
    интерпретируется как неполное имя (напр. «amoxicillin» vs «amoxicillin trihydrate»),
    а не как разный вариант препарата.

    Специальный кейс: 2-буквенные all-alpha токены (SK, QK, GK и т.п.) — фарм-суффиксы
    вариантов препарата. Стандартный _is_significant_variant_token их не ловит (нет цифр),
    но если у ОБОИХ имён есть разные 2-буквенные all-alpha уникальные токены — это явный
    признак разных вариантов → блокируем.
    """
    tokens_a = frozenset(name_a.split())
    tokens_b = frozenset(name_b.split())
    # Hard-различитель (toxumu=семена) присутствует только у одной стороны →
    # разные товары. Блокируем даже без встречного уникального токена.
    if (tokens_a & _HARD_DISTINCT_TOKENS) != (tokens_b & _HARD_DISTINCT_TOKENS):
        return True
    unique_a = {t for t in tokens_a - tokens_b if _is_significant_variant_token(t)}
    unique_b = {t for t in tokens_b - tokens_a if _is_significant_variant_token(t)}
    if unique_a and unique_b:
        return True
    # 2-буквенные all-alpha фармкоды: SK/QK/GK/GC и подобные.
    # Срабатывает только когда у ОБОИХ имён есть разный 2-буквенный суффикс —
    # это чёткий сигнал разных формул. Одиночный 2-буквенный токен у одного
    # из имён пропускаем (неполные данные).
    alpha2_a = {t for t in tokens_a - tokens_b if len(t) == 2 and t.isalpha()}
    alpha2_b = {t for t in tokens_b - tokens_a if len(t) == 2 and t.isalpha()}
    return bool(alpha2_a) and bool(alpha2_b)


# ── Вариант-атомы из RAW-имени (2026-05-29) ─────────────────────────────────
# normalize_name вырезает одиночные различители: «Normoqlip 2  N30» → 'normoqlip'
# (цифра пропадает рядом с pack), а «Normoqlip M» → 'normoqlip m'. Поэтому
# series/variant-guard'ы (работают на name_normalized) НЕ видят вырезанный «2» →
# Normoqlip M ↔ Normoqlip 2 ошибочно матчатся. Сравниваем атомы прямо из RAW:
#   • одиночная серийная цифра 1-9  (Nutrilon 1, Normoqlip 2)
#   • одиночная буква-вариант КРОМЕ юнитов q/g/l (Lorinden C/A, Vitamin A/C,
#     ASferon C/S, Güzgü M/S, Normoqlip M)
# Перед извлечением удаляем pack/№/возраст/диапазон/числа-с-юнитами/2+-значные
# числа — иначе возраст («6 aylıq»), вес («25 q»), объём, split-числа («50 000»)
# дают ложные атомы. Валидация на прод-дампе (57142 товара, scripts/
# scan_variant_conflicts.py): 7 flagged кластеров, ВСЕ 7 — настоящие wrong-match,
# 0 ложных.
_VA_NUM_HASH_RE = re.compile(r"№\s*\d+")
_VA_PACK_RE = re.compile(r"\b(?:n|no\.?)\s*\d+\b", re.I)
_VA_AZ_PACK_UNIT_RE = re.compile(r"\b\d+\s*(?:eded|ed|dest|sase|sashe|st|saise)\b", re.I)
_VA_RANGE_RE = re.compile(r"\d+\s*-\s*\d+")
_VA_AGE_RE = re.compile(r"\b\d+\s*(?:ay(?:liq|indan|inda|dan)?|il|yas(?:inda)?)\b", re.I)
_VA_NUM_UNIT_RE = re.compile(
    r"\b\d+[.,]?\d*\s*(?:mg|ml|mq|mkg|mcg|kg|kq|qr|g|q|l|iu|tv|ed|bv|mln|million)\b",
    re.I,
)
_VA_MULTIDIGIT_RE = re.compile(r"\b\d{2,}\b")
_VA_TOKEN_RE = re.compile(r"[a-z0-9]+")
_VA_UNIT_LETTERS: frozenset[str] = frozenset({"q", "g", "l"})  # грамм(AZ)/грамм/литр
# Одиночная фарм-масса 1-9 mg/mq/mcg как атом: Normoqlip 2 mq ≠ 4 mq (глимепирид
# 2/3/4 mg). pharm пишет «2 mq» (доза, иначе ушла бы в _VA_NUM_UNIT), aptek «4 N30»
# (голая цифра) → сводим в общий atom-space. Lookbehind (?<![.\d]) исключает
# дробные/многозначные (2.5 mg → не «5», 500 mg → не «0»).
_VA_SINGLE_MASS_RE = re.compile(r"(?<![.\d])([1-9])\s*(?:mg|mq|mcg|mkg)\b", re.I)


def _variant_atoms(raw_name: str) -> frozenset[str]:
    """Вариант-атомы из RAW-имени: серийные цифры 1-9 + одиночные буквы-варианты
    + одиночная фарм-масса 1-9 mg (Normoqlip 2 mq).

    Удаляет pack/№/возраст/диапазон/числа-с-юнитами/2+-значные числа перед
    извлечением (чтобы не зацепить возраст/вес/объём/split-числа). Юнит-буквы
    q/g/l исключены.
    """
    low = strip_accents(raw_name or "").lower()
    atoms: set[str] = set()
    # Одиночная фарм-масса — ДО strip (иначе «2 mq» съест _VA_NUM_UNIT_RE).
    atoms.update(_VA_SINGLE_MASS_RE.findall(low))
    for rx in (
        _VA_NUM_HASH_RE,
        _VA_PACK_RE,
        _VA_AZ_PACK_UNIT_RE,
        _VA_RANGE_RE,
        _VA_AGE_RE,
        _VA_NUM_UNIT_RE,
    ):
        low = rx.sub(" ", low)
    low = _VA_MULTIDIGIT_RE.sub(" ", low)  # после unit-strip: 2+-значные остатки = шум
    for t in _VA_TOKEN_RE.findall(low):
        if len(t) != 1:
            continue
        if t in "123456789" or (t.isalpha() and t not in _VA_UNIT_LETTERS):
            atoms.add(t)
    return frozenset(atoms)


def _has_conflicting_variant_atoms(name_raw_a: str, name_raw_b: str) -> bool:
    """True если у КАЖДОЙ стороны есть свой уникальный вариант-атом.

    Симметричное правило (как _has_conflicting_variant_tokens): блокируем только
    при взаимно-уникальных атомах (Lorinden c|a, Normoqlip m|2, Vitamin a|c).
    Односторонний/superset атом («brand c 1» vs «brand c») пропускаем — это
    неполные данные, а не другой вариант.
    """
    atoms_a = _variant_atoms(name_raw_a)
    atoms_b = _variant_atoms(name_raw_b)
    return bool(atoms_a - atoms_b) and bool(atoms_b - atoms_a)


# ── Вариант-маркеры: серебро (Ag) + номер модели (type/tip/тип N) (2026-05-30) ──
# Класс ложных матчей в линейках с модификациями (Юнона Био-Т: Ag/Cu380/type1/2):
# матчер сцеплял «Bio-T Ag» ↔ «Bio-T Tip 1» по общим токенам. «Ag» (2 буквы) <
# _MIN_VARIANT_TOKEN_LEN=3 → игнорился; type/tip N не сверялся как номер модели.
# ЛОВУШКА: az «ağ»=белый → после strip_accents «ag» коллизит с серебром Ag, плюс
# «AG»=антиген (COVID-19 AG тест). Поэтому: (1) detection БЕЗ strip_accents (ğ≠g,
# так «ağ gil» не даёт маркер); (2) СИММЕТРИЧНОЕ правило (как variant_tokens/atoms)
# — блок только при взаимно-уникальных маркерах. «Maska (ag)»{ag} vs «Maska»{} →
# НЕ блок (односторонний = неполные данные). type-номер 1-2 цифры + \b: «Cu380
# type1»→{t1} (380 не зацепляется), «2 tip»/«type1»/«Tip 2» — все ловятся.
# Серебро: явные слова (gümüş/silver/серебро) — всегда; голый «ag» — только ВНЕ
# антиген-контекста (аудит: «COVID-19 AG Test» = антиген-тест, не серебро).
_SILVER_EXPLICIT_RE = re.compile(r"g[üu]m[üu][sş]|silver|серебро", re.IGNORECASE)
_SILVER_AG_RE = re.compile(r"\bag\b", re.IGNORECASE)
_ANTIGEN_RE = re.compile(r"\b(?:antigen|test|rapid)\b", re.IGNORECASE)
# Номер модели type/tip/тип N: ЛЕВАЯ \b (иначе proTYPE/genoTYPE/seroTYPE → ложный
# маркер), [\s-]* (ловим «Tip-1»), 1-2 цифры (380 = медь Cu, не номер типа).
_TYPE_FWD_RE = re.compile(r"\b(?:tip|type|тип)[\s-]*(\d{1,2})\b", re.IGNORECASE)
_TYPE_REV_RE = re.compile(r"\b(\d{1,2})[\s-]*(?:tip|type|тип)\b", re.IGNORECASE)
# Медь Cu<N> — симметричный маркер (Bio-T Cu380 ≠ Cu375 ≠ Ag).
_COPPER_RE = re.compile(r"\bcu\s*(\d{2,4})\b", re.IGNORECASE)


def _variant_markers(raw_name: str) -> frozenset[str]:
    """Маркеры модификации из RAW (БЕЗ strip_accents): серебро→'ag', медь→'cuNNN',
    номер модели type/tip/тип N→'t<N>'. Симметричное правило в
    `_has_conflicting_variant_marker`. Антиген-контекст (test/rapid/antigen) гасит
    голый «ag» (COVID AG Test = антиген, не серебро); proTYPE — левая \\b в regex."""
    low = (raw_name or "").lower()
    markers: set[str] = set()
    if _SILVER_EXPLICIT_RE.search(low) or (
        _SILVER_AG_RE.search(low) and not _ANTIGEN_RE.search(low)
    ):
        markers.add("ag")
    for m in _COPPER_RE.finditer(low):
        markers.add("cu" + m.group(1))
    for rx in (_TYPE_FWD_RE, _TYPE_REV_RE):
        for m in rx.finditer(low):
            markers.add("t" + m.group(1))
    return frozenset(markers)


def _has_conflicting_variant_marker(name_raw_a: str, name_raw_b: str) -> bool:
    """True если у КАЖДОЙ стороны свой уникальный вариант-маркер (серебро / номер
    модели). Симметрично: «Bio-T Ag»{ag} vs «Bio-T Tip 1»{t1} → блок; type1 vs
    type2 → блок; «Ag» vs «Ag» / «Ag» vs «Ag Tip 2» (superset) → НЕ блок."""
    ma = _variant_markers(name_raw_a)
    mb = _variant_markers(name_raw_b)
    return bool(ma - mb) and bool(mb - ma)


# ── Многозначная сила/доза (2026-05-29) ─────────────────────────────────────
# variant-atoms берёт только 1-9 (серия/одиночная mg). Большие силы — enzyme/IU
# единицы (Mikrazim 25000 ED ≠ 10000, Creon 25000 ≠ 10000, D3 50000 IU) — это
# 4+-значные числа, которые normalize вырезает как dosage/число. Сравниваем их
# из RAW: thousand-space «50 000»→«50000», убираем pack (N20/№20/20 əd), берём
# standalone 4+-значные. Блок только если у ОБОИХ есть такое число и они разные
# (50000 IU == 50 000 BV → не блок; 25000 ≠ 10000 → блок). Volume/доза <1000
# (100ml, 500mg) не трогаем — это bucket/другие guard'ы.
# \d{4,7}: 1000–9 999 999 покрывает enzyme/IU/BV силы (Mikrazim 25000, D3 50000,
# Ukraferon 1000000 BV); 8+ знаков = EAN-8/EAN-13/рег-коды в имени → НЕ сила.
_STRENGTH_NUM_RE = re.compile(r"(?<![\d.])\d{4,7}(?![\d.])")
_THOUSAND_SPACE_RE = re.compile(r"(\d)\s+(\d{3})(?!\d)")


def _strength_numbers(raw_name: str) -> frozenset[str]:
    low = strip_accents(raw_name or "").lower()
    low = _THOUSAND_SPACE_RE.sub(r"\1\2", low)  # «50 000» → «50000»
    # NB: НЕ применяем _VA_AZ_PACK_UNIT_RE — её «ed» (ədəd=штук после strip_accents)
    # коллизит с «ED» (enzyme units: Mikrazim 25000 ED) → съедал бы силу. Pack-
    # счётчики <1000, на 4+-значную силу не влияют, так что убирать pack тут не нужно.
    for rx in (_VA_NUM_HASH_RE, _VA_PACK_RE):
        low = rx.sub(" ", low)
    return frozenset(_STRENGTH_NUM_RE.findall(low))


def _has_conflicting_strength_number(name_raw_a: str, name_raw_b: str) -> bool:
    """True если у обоих имён есть 4+-значная сила и множества различаются."""
    a = _strength_numbers(name_raw_a)
    b = _strength_numbers(name_raw_b)
    return bool(a) and bool(b) and a != b


# ── Страна производителя (2026-05-29) ────────────────────────────────────────
# Генерик-коммодити (глицерин, новокаин, шприцы) каждый сайт берёт у СВОЕГО
# производителя → ложный «85% дешевле» между Talya(Türkiyə) и Azerfarm(Azərbaycan).
# Сигнал страны ЕСТЬ: aptekonline хранит её в Product.manufacturer (поле «olke»);
# pharmonline — в хвосте URL-слага («…azerfarm-mmc-azerbaycan», «…nobel-ilac-turkiye»).
# Канон-словарь унифицирует AZ/EN-написания (после strip_accents) в ISO-код.
_COUNTRY_CANON: dict[str, str] = {
    "turkiye": "tr",
    "turkiya": "tr",
    "turkey": "tr",
    "rusiya": "ru",
    "russia": "ru",
    "rossiya": "ru",
    "azerbaycan": "az",
    "azerbaijan": "az",
    "fransa": "fr",
    "france": "fr",
    "almaniya": "de",
    "germany": "de",
    "ger": "de",
    "germaniya": "de",
    "ukrayna": "ua",
    "ukraine": "ua",
    "polsa": "pl",
    "polsha": "pl",
    "poland": "pl",
    "polonya": "pl",
    "italiya": "it",
    "italy": "it",
    "cin": "cn",
    "chin": "cn",
    "china": "cn",
    "hindistan": "in",
    "india": "in",
    "belarus": "by",
    "macaristan": "hu",
    "hungary": "hu",
    "ispaniya": "es",
    "spain": "es",
    "bolqaristan": "bg",
    "bulgaria": "bg",
    "latviya": "lv",
    "latvia": "lv",
    "sloveniya": "si",
    "slovenia": "si",
    "abs": "us",
    "usa": "us",
    "amerika": "us",
    "yaponiya": "jp",
    "japan": "jp",
    "cexiya": "cz",
    "chexiya": "cz",
    "czech": "cz",
    "niderland": "nl",
    "hollandiya": "nl",
    "isvecre": "ch",
    "isveckre": "ch",
    "switzerland": "ch",
    "isvec": "se",
    "sweden": "se",
    "avstriya": "at",
    "austria": "at",
    "koreya": "kr",
    "korea": "kr",
    "ingiltere": "gb",
    "uk": "gb",
    "britaniya": "gb",
    "misir": "eg",
    "egypt": "eg",
    "iran": "ir",
    "pakistan": "pk",
    "vyetnam": "vn",
    "vietnam": "vn",
    "yunanistan": "gr",
    "greece": "gr",
    "rumıniya": "ro",
    "rumeniya": "ro",
    "romania": "ro",
    "portuqaliya": "pt",
    "portugal": "pt",
    "belcika": "be",
    "belgium": "be",
    "danimarka": "dk",
    "denmark": "dk",
    "finlandiya": "fi",
    "finland": "fi",
    "norvec": "no",
    "norway": "no",
    "litva": "lt",
    "estoniya": "ee",
    "xorvatiya": "hr",
    "serbiya": "rs",
    "sloveniya2": "si",
    "gurcustan": "ge",
    "georgia": "ge",
    "qazaxistan": "kz",
    "kazakhstan": "kz",
    "ozbekistan": "uz",
    "uzbekistan": "uz",
}


def _country_token(raw: str | None) -> str | None:
    """Канон ISO-код страны из строки (apte manufacturer-поле = «olke»)."""
    from src.product_policy import normalize_country_code

    return normalize_country_code(raw)


def _country_from_url(url: str | None) -> str | None:
    """Страна из хвоста URL-слага (pharmonline: «…-azerfarm-mmc-azerbaycan»).

    Берём последние до 3 токенов слага и проверяем по словарю стран (чтобы не
    принять «ml»/«tabletler»/бренд за страну).
    """
    if not url:
        return None
    slug = strip_accents(url).rstrip("/").split("/")[-1].split("?")[0].lower()
    parts = slug.split("-")
    for tok in reversed(parts[-3:]):
        c = _COUNTRY_CANON.get(tok)
        if c:
            return c
    return None


def _country_of(p) -> str | None:
    """Country with legacy fallbacks, for diagnostics and commodity rollout."""
    from src.product_policy import country_code_of

    return country_code_of(p) or _country_token(getattr(p, "manufacturer", None)) or _country_from_url(
        getattr(p, "url", None)
    )


def _has_conflicting_country(a, b) -> bool:
    """True only for two conflicting *verified SKU country* observations.

    URL tails and overloaded legacy ``manufacturer`` values are not sufficient
    to split ordinary medicines.  They remain available to the older,
    commodity-gated rule below until the production backfill is complete.
    """
    from src.product_policy import country_code_of

    ca, cb = country_code_of(a), country_code_of(b)
    return bool(ca) and bool(cb) and ca != cb


def _has_conflicting_legacy_country(a, b) -> bool:
    ca, cb = _country_of(a), _country_of(b)
    return bool(ca) and bool(cb) and ca != cb


# Grade-слова: косметическое масло ≠ пищевое/обычное (разный товар, разная цена).
_GRADE_WORDS = {"kosmetik", "kosmetika", "kosmeticeskoe", "naruzhnoe", "cosmetic"}


def _grade_tokens(name: str | None) -> frozenset[str]:
    t = strip_accents((name or "").lower())
    return frozenset(g for g in _GRADE_WORDS if g in t)


def _has_conflicting_origin_or_grade(a, b) -> bool:
    """Строгая идентичность КОММОДИТИ (политика клиента 2026-05-31: идентичный товар =
    бренд + объём + номер + СТРАНА — всё совпадает). Два commodity-товара с РАЗНОЙ
    страной происхождения ИЛИ разным grade (косметическое vs обычное) — НЕ один товар,
    ДАЖЕ если бренд совпадает (Medoil Türkiyə ≠ Medoil Azərbaycan — клиент явно
    потребовал учитывать страну, увидев эти пары). Commodity-gated → trade-name
    препараты (один и тот же выпускается на разных заводах) НЕ затрагиваются."""
    an, bn = getattr(a, "name", None), getattr(b, "name", None)
    if not (is_commodity_name(an) and is_commodity_name(bn)):
        return False
    return (
        _has_conflicting_country(a, b)
        or _has_conflicting_legacy_country(a, b)
        or _grade_tokens(an) != _grade_tokens(bn)
    )


# ── Габариты AxB (2026-05-29) ────────────────────────────────────────────────
# Пластыри/повязки/марля различаются размером: «Leykoplastr Alban 10sm x 10sm»
# ≠ «10sm x 25sm» (разная площадь → разная цена, не арбитраж). Размер остаётся в
# name_normalized, но fuzzy игнорирует разницу чисел. Извлекаем «ЧИСЛО[ед] x
# ЧИСЛО ед» (ед: mm/sm/cm/m, разделитель x/х/×/*, смешанные единицы, без ед у
# первого), нормализуем в мм, сравниваем как неупорядоченную пару.
_DIM_UNIT_MM = {"mm": 1.0, "sm": 10.0, "cm": 10.0, "m": 1000.0}
_DIM_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(mm|sm|cm|m)?\s*[x×х*]\s*"
    r"(\d+(?:[.,]\d+)?)\s*(mm|sm|cm|m)(?![a-z])",
    re.IGNORECASE,
)


def _dimensions(raw_name: str) -> frozenset[tuple[float, float]]:
    """Габариты товара в мм как множество неупорядоченных пар (10×30 == 30×10)."""
    low = strip_accents(raw_name or "").lower()
    dims: set[tuple[float, float]] = set()
    for m in _DIM_RE.finditer(low):
        n1, u1, n2, u2 = m.group(1), m.group(2), m.group(3), m.group(4)
        u1 = u1 or u2  # нет единицы у первого числа → берём от второго
        try:
            mm1 = float(n1.replace(",", ".")) * _DIM_UNIT_MM[u1.lower()]
            mm2 = float(n2.replace(",", ".")) * _DIM_UNIT_MM[u2.lower()]
        except (KeyError, ValueError, AttributeError):
            continue
        a, b = sorted((round(mm1, 1), round(mm2, 1)))
        dims.add((a, b))
    return frozenset(dims)


def _has_conflicting_dimensions(name_raw_a: str, name_raw_b: str) -> bool:
    """True если у ОБОИХ товаров есть габариты и они различаются (10×10 ≠ 10×25)."""
    a = _dimensions(name_raw_a)
    b = _dimensions(name_raw_b)
    return bool(a) and bool(b) and a != b


# ── Концентрация % (2026-05-29) ──────────────────────────────────────────────
# «Tetrasiklin 3% 15q» ≠ «Tetrasiklin 1% 15q», «Novokain 2%» ≠ «0.5%» — разная
# концентрация = разный препарат. extract_dosage % НЕ ловит (берёт вес/объём).
_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def _concentrations(raw_name: str) -> frozenset[float]:
    return frozenset(
        round(float(m.group(1).replace(",", ".")), 3) for m in _PERCENT_RE.finditer(raw_name or "")
    )


def _has_conflicting_concentration(name_raw_a: str, name_raw_b: str) -> bool:
    """True если у ОБОИХ есть процент-концентрация и множества различаются."""
    a = _concentrations(name_raw_a)
    b = _concentrations(name_raw_b)
    return bool(a) and bool(b) and a != b


def _significant_name_tokens(name_norm: str | None) -> frozenset[str]:
    """Значащие токены name_normalized для ambiguity: len≥4, БЕЗ цифр, не noise/modifier.

    Цифро-содержащие токены («100ml», «60ml», «250mq») исключены: объём/доза уже
    в bucket_key, ambiguity должна драйвиться брендом/вариантом СЛОВАМИ (altay,
    mirrolla, medoil), а не объёмными артефактами.
    """
    return frozenset(
        t
        for t in (name_norm or "").split()
        if len(t) >= 4
        and not any(ch.isdigit() for ch in t)
        and t not in _MATCH_NOISE_TOKENS
        and t not in _PHARMA_MODIFIERS
    )


def _has_conflicting_brand(a, b) -> bool:
    """Разные ПОТРЕБИТЕЛЬСКИЕ бренды (brand_verified) → разные товары — но ТОЛЬКО
    для товаров-коммодити (масла/семена/чаи/экстракты с дженерик-именем).

    Корень бага клиента: коммодити «Alaqanqal yağı 100 ml» продаётся разными
    фирмами (Biola vs Herba Flora), но `brand`-поле хранит generic-имя, и matcher
    их склеивает. Сравниваем ВОССТАНОВЛЕННЫЙ бренд (brand_verified).

    Почему ТОЛЬКО коммодити (gate is_commodity_name): для trade-name препаратов
    brand_verified = ЗАВОД, а aloe и pharmonline пишут его по-разному
    (Nijfarm/Nizhfarm, Merk/Merck Sante, Berinqer/Boehringer) → блокировка
    разорвала бы ~115 ВЕРНЫХ матчей (замерено на проде, dry-run 2026-05-31).
    Поэтому guard срабатывает лишь когда ОБА имени — ботанический коммодити И
    у обоих уверенный различающийся потребительский бренд. Заводы-компании
    (Merck KGaA, Egis İlaç) неразличающи; NULL/unknown → не срабатывает."""
    if not (
        is_commodity_name(getattr(a, "name", None)) and is_commodity_name(getattr(b, "name", None))
    ):
        return False
    return brands_conflict(getattr(a, "brand_verified", None), getattr(b, "brand_verified", None))


# Витаминно-минеральные коды-маркеры состава: буква D/K/B + 1-2 цифры
# (d3, d2, k1, k2, b1…b12). Однозначны (в отличие от одиночных C/E/A). Разный
# НАБОР кодов = разный состав (D3 ≠ D3+K2 — комбо-препарат, не моно).
_INGREDIENT_CODE_RE = re.compile(r"\b([dkb]\d{1,2})\b")


def _ingredient_codes(name: str) -> frozenset[str]:
    return frozenset(m.group(1) for m in _INGREDIENT_CODE_RE.finditer((name or "").lower()))


def _has_conflicting_ingredient_codes(a, b) -> bool:
    """Разный состав по витаминным кодам → разные товары.

    Корень бага клиента: «Venatura Vitamin D3» (моно, 13.20) ошибочно склеен с
    «Venatura Vitamin D3, K2» (комбо D3+K2, 25.60) — тот же бренд, объём, префикс
    имени, но РАЗНЫЙ состав (K2 — добавленный ингредиент). Сравниваем НАБОР
    кодов d/k/b+цифра в именах.

    Срабатывает ТОЛЬКО когда у ОБОИХ есть код И наборы различаются (D3 vs D3+K2,
    B6 vs B12). Если у одной стороны кодов НЕТ — это опущение в названии
    (DetriBus = DetriBus D3), НЕ конфликт → recall сохраняется."""
    ca = _ingredient_codes(a.name_normalized or a.name or "")
    cb = _ingredient_codes(b.name_normalized or b.name or "")
    return bool(ca) and bool(cb) and ca != cb


# Вариант/линейка-модификаторы: WHITELIST слов, наличие которых у ОДНОЙ стороны и
# отсутствие у другой = разный товар (Linkas vs Linkas Plyus, Kodelak vs Kodelak
# Bronxo, подгузник Newborn vs Mini, Molped Daily vs Soft). НЕ форм-слова
# (otu/məlhəm/şam — это опущение, тот же товар) и НЕ страны — поэтому именно
# whitelist, а не «любое лишнее слово»: ложный split тут авто-повторялся бы каждый
# скрейп (revalidate_split в пайплайне). Accent-normalized (strip_accents).
# ВНИМАНИЕ: только ОДНОЗНАЧНЫЕ модификаторы линейки/формулы, у которых нет
# size/translation-двойника. НЕ включать (проверено dry-run'ом 2026-05-31 — давали
# ~15 ложных split'ов): размеры подгузников (newborn/mini/midi/maxi/junior — = номер
# размера, тот же товар: «Sleepy Natural 3» = «Sleepy Natural Midi»), линейки
# (ultra/comfort/active/baby — не различают), men/women (= kişilər/qadın, перевод),
# super/maks/intensiv (маркетинг-хвост). Эти классы НЕ авто-гардятся — только UI-флаг.
_VARIANT_WORDS = {
    "plyus",
    "plus",
    "forte",
    "fort",
    "bronxo",
    "bronho",
    "pain",
    # тоничность солевых спреев — однозначная линейка без size/translation-двойника:
    # Marimer/Aqua Maris izotonik ≠ hipertonik (разная концентрация соли, разный товар).
    # Verified 2026-05-31 recall dry-run: «Marimer 100ml» ✗ «Marimer hipertonik 100ml».
    "izotonik",
    "izotonic",
    "isotonik",
    "isotonic",
    "hipertonik",
    "hipertonic",
    "hypertonik",
    "hypertonic",
}


def _variant_words(name: str) -> frozenset[str]:
    return frozenset(strip_accents((name or "").lower()).split()) & _VARIANT_WORDS


def _has_conflicting_variant_words(a, b) -> bool:
    """Разный набор вариант/линейка-слов из whitelist → разные товары.

    Срабатывает, когда у одной стороны есть вариант-слово (Plyus/Bronxo/Newborn/
    Daily/…), которого нет у другой (или наборы различаются). Whitelist держит это
    точным: форм-слова (otu/məlhəm/şam) и страны НЕ в нём → опущения не ломаются."""
    return _variant_words(a.name or "") != _variant_words(b.name or "")


_VOL_RE = re.compile(r"^(\d+(?:[.,]\d+)?)(ml|q|g|gr|qr)$")
_VOL_FAMILY = {"ml": "ml", "q": "g", "g": "g", "gr": "g", "qr": "g"}


def _parse_volume(s: str | None) -> tuple[float, str] | None:
    """«15ml»→(15.0,"ml"), «90q»→(90.0,"g"). None для kg/kq (это вес РЕБЁНКА у
    подгузников «11-25kg», а не объём упаковки) и для непарсимого."""
    if not s:
        return None
    m = _VOL_RE.match(s)
    if not m:
        return None
    return float(m.group(1).replace(",", ".")), _VOL_FAMILY[m.group(2)]


def _has_conflicting_pack_volume(a, b) -> bool:
    """Разный ОБЪЁМ упаковки (флакон/туба) → разные товары.

    Корень (workflow-аудит 2026-05-31): «Azoksin 200mq/5ml 15ml» vs «…30ml» —
    extract_pack_size брал «5ml» (знаменатель концентрации) у обоих → одинаковый
    bucket → склейка 15ml-флакона с 30ml. Сравниваем НАСТОЯЩИЙ объём
    (extract_total_volume снимает «X/Yml»).

    Блок ТОЛЬКО когда у обоих есть объём в ОДНОЙ единице измерения и он различается.
    Разные единицы (100ml vs 100q — паста в мл/г = тот же товар) и kg-веса
    подгузников НЕ блокируем (false-positive'ы из dry-run); одна сторона без объёма
    → не блок (recall цел)."""
    pa = _parse_volume(extract_total_volume(a.name or ""))
    pb = _parse_volume(extract_total_volume(b.name or ""))
    if not pa or not pb or pa[1] != pb[1]:
        return False
    return pa[0] != pb[0]


def _has_conflicting_pack_count(a, b) -> bool:
    """Different explicit unit counts are different physical packs.

    Both sides must carry a high-confidence count marker (N/№/ədəd/etc.).
    Unknown counts stay recall-friendly, while volume and dose numbers are
    excluded by ``pack_unit_count``.
    """
    count_a, confidence_a = pack_unit_count(
        getattr(a, "pack_size", None), getattr(a, "name", None)
    )
    count_b, confidence_b = pack_unit_count(
        getattr(b, "pack_size", None), getattr(b, "name", None)
    )
    return (
        confidence_a == "high"
        and confidence_b == "high"
        and count_a != count_b
    )


# Сила дозы препарата: число + mg/mq/mkg/mcg (НЕ ml/g — то объём/вес упаковки).
_DOSE_MG_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(mg|mq|mkg|mcg|µg)(?![a-z])", re.IGNORECASE)

# Some sites omit the dose from the visible title but retain it in a clean URL
# slug (for example aptekonline ``/product/risek-40mg-n10``).  This parser is
# deliberately stricter than the title parser: a dose must occupy a complete
# path/slug segment, and a digit-hyphen prefix is rejected so decimal slugs such
# as ``7-5mg`` are not misread as 5 mg.
_URL_DOSE_MG_RE = re.compile(
    r"(?<!\d)(?:^|[-_/])(\d+(?:[.,]\d+)?)(mg|mq|mkg|mcg|µg)(?=$|[-_/])",
    re.IGNORECASE,
)


_SPACED_THOUSANDS_RE = re.compile(r"\b(\d{1,3})(?:\s(\d{3}))+\b")


def _doses_mg(text: str) -> frozenset[float]:
    # «1 000 mq» → «1000 mq» (иначе regex берёт «000 mq» = 0)
    t = strip_accents(text or "").lower()
    t = _SPACED_THOUSANDS_RE.sub(lambda m: m.group(0).replace(" ", ""), t)
    out = set()
    for m in _DOSE_MG_RE.finditer(t):
        v = float(m.group(1).replace(",", "."))
        if m.group(2) in ("mkg", "mcg", "µg"):
            v /= 1000.0
        out.add(round(v, 4))
    return frozenset(out)


def _doses_mg_from_url(url: str | None) -> frozenset[float]:
    text = strip_accents(url or "").lower()
    out = set()
    for match in _URL_DOSE_MG_RE.finditer(text):
        value = float(match.group(1).replace(",", "."))
        if match.group(2) in ("mkg", "mcg", "µg"):
            value /= 1000.0
        out.add(round(value, 4))
    return frozenset(out)


def _doses_mg_for_product(product) -> frozenset[float]:
    """Return title dose, falling back to a conservative URL slug parser."""
    from_name = _doses_mg(getattr(product, "name", "") or "")
    if from_name:
        return from_name
    return _doses_mg_from_url(getattr(product, "url", None))


def _has_conflicting_dose(a, b) -> bool:
    """Разная сила дозы (mg) в ИМЕНАХ → разные товары.

    Сравниваем НАБОР доз (mg/mq/mkg/mcg) из имён — корректно для много-
    компонентных (5/1.25/10 vs 5/1.25/5). Требуется единица (не голое число, не
    объём ml/g) → нет ложных на pack-count. Одна сторона без дозы → не блок.

    URL читаем только fallback-ом и только по безопасным slug-сегментам: это
    закрывает aptekonline title-poor кейсы вроде Risek «N10 (toz)» при URL
    `risek-40mg-n10`, не возвращая старые false-positive на `7-5mg`."""
    da = _doses_mg_for_product(a)
    db = _doses_mg_for_product(b)
    return bool(da) and bool(db) and da != db


# Буквенные размеры (подгузники/одежда/бельё): M ≠ L — разный товар. Отдельно от
# числовых, т.к. в имени стоят как голые токены.
_SIZE_LETTERS = {"xs", "s", "m", "l", "xl", "xxl", "xxxl"}
_SIZE_SPLIT_RE = re.compile(r"[^a-zəçşğöüı]+")


def _size_letters(name: str | None) -> frozenset[str]:
    return frozenset(
        t for t in _SIZE_SPLIT_RE.split((name or "").lower()) if t in _SIZE_LETTERS
    )


def ultra_equal(a, b) -> bool:
    """Высокоуверенная эквивалентность двух продуктов — one-click безопасно.

    РАВЕНСТВО (не подмножество) по всем спец-осям: значимые токены имени +
    буквенный размер (M≠L) + ingredient-коды (D3≠D3K2) + pack-size + набор доз +
    форма + вариант-слова. Под этими равенствами отношения транзитивны, поэтому
    кандидат, ultra-равный одному члену кластера, согласован со всем кластером.

    Источник истины для recall_candidates.py --ultra И для UI suggest-analog
    (бейдж auto_safe). НЕ заменяет _hard_conflict/_pairwise_spec_conflict —
    это ДОПОЛНИТЕЛЬНЫЙ, более строгий фильтр поверх них."""
    na, nb = a.name_normalized or a.name or "", b.name_normalized or b.name or ""
    ra, rb = a.name or "", b.name or ""
    return (
        _significant_name_tokens(na) == _significant_name_tokens(nb)
        and _size_letters(ra) == _size_letters(rb)
        and _ingredient_codes(ra) == _ingredient_codes(rb)
        and extract_pack_size(ra) == extract_pack_size(rb)
        and _doses_mg_for_product(a) == _doses_mg_for_product(b)
        and extract_form(ra) == extract_form(rb)
        and _variant_words(ra) == _variant_words(rb)
        # Не помечаем cross-brand коммодити «точным совпадением»: для масел/чаёв/
        # экстрактов бренд — главный различитель (Herba Flora ≠ Mirrolla). Commodity-
        # gated + fuzzy/NULL/manufacturer-safe внутри _has_conflicting_brand, поэтому
        # same-firm транслитерации и NULL-brand твины остаются auto_safe. (workflow
        # wmol35wiv, 2026-05-31: единственный одобренный после adversarial-проверки фикс.)
        and not _has_conflicting_brand(a, b)
    )


def _hard_conflict(a, b) -> bool:
    """Pairwise hard-guard'ы (как в проходах), отличающие РАЗНЫЕ товары.

    Для ambiguity pre-pass: кандидата, которого настоящий проход всё равно бы
    отверг (Nutrilon Premium 1 vs Comfort/Pepti → series-guard), НЕ считаем —
    иначе генерик-якорь ложно «неоднозначен» и теряет легит-двойника.
    НЕ включаем length-disparity: короткий генерик vs длинное брендовое имя —
    это как раз brand-stub, который ДОЛЖЕН считаться неоднозначностью.
    """
    an, bn = a.name_normalized or "", b.name_normalized or ""
    ar, br = a.name or "", b.name or ""
    return (
        _has_conflicting_brand(a, b)
        or _has_conflicting_country(a, b)
        or _has_conflicting_origin_or_grade(a, b)
        or _has_conflicting_ingredient_codes(a, b)
        or _has_conflicting_variant_words(a, b)
        or _has_conflicting_pack_count(a, b)
        or _has_conflicting_pack_volume(a, b)
        or _has_conflicting_dose(a, b)
        or _has_conflicting_form(ar, br)
        or _has_conflicting_gender(an, bn)
        or _has_conflicting_series_number(an, bn)
        or _has_conflicting_orphan_number(an, bn)
        or _has_conflicting_variant_tokens(an, bn)
        or _has_conflicting_variant_atoms(ar, br)
        or _has_conflicting_strength_number(ar, br)
        or _has_conflicting_variant_marker(ar, br)
    )


def _pairwise_spec_conflict(a, b) -> bool:
    """Высокоточные СИММЕТРИЧНЫЕ guard'ы для РЕТРО-перепроверки (revalidate): серебро/
    тип/медь, одиночный вариант-атом, многозначная сила, габариты, %. Все требуют
    конфликтующего сигнала с ОБЕИХ сторон → near-zero false-positive на одном товаре.

    Country is now included only from the dedicated verified SKU field.  The
    former URL/manufacturer heuristic is still too noisy for irreversible
    trade-name splits and is used only by the commodity rule.

    + brand-conflict (2026-05-31): brand_verified из АВТОРИТЕТНОГО источника
    (slug/page-JSON/aloe), оба потребительские и различаются — надёжный сигнал
    разных товаров (Biola≠Herba Flora), компании-заводы не считаются."""
    ar, br = a.name or "", b.name or ""
    return (
        _has_conflicting_brand(a, b)
        or _has_conflicting_country(a, b)
        or _has_conflicting_origin_or_grade(a, b)
        or _has_conflicting_ingredient_codes(a, b)
        or _has_conflicting_variant_words(a, b)
        or _has_conflicting_pack_count(a, b)
        or _has_conflicting_pack_volume(a, b)
        or _has_conflicting_dose(a, b)
        or _has_conflicting_variant_marker(ar, br)
        or _has_conflicting_variant_atoms(ar, br)
        or _has_conflicting_strength_number(ar, br)
        or _has_conflicting_dimensions(ar, br)
        or _has_conflicting_concentration(ar, br)
    )


def find_conflicting_clusters(
    session: Session,
    *,
    tenant_id: int = 1,
    match_ids: set[int] | None = None,
) -> list:
    """Авто-Match'и, где хоть одна cross-site пара членов конфликтует по ТЕКУЩИМ
    guard'ам. Возвращает [(match, product_a, product_b)] — первая конфликтная пара
    на кластер. Корень «whack-a-mole»: инкрементальный матчинг переиспользует
    canonical_id и НЕ перепроверяет старые кластеры; этот хелпер питает точечный
    `rematch --revalidate` (split-only, без 78% churn полного --reset).

    `_pairwise_spec_conflict` использует только RAW-имя (серебро/тип/медь/сила/
    габариты/%), поэтому не зависит от (возможно устаревшей) stored name_normalized."""
    import itertools

    from sqlalchemy.orm import selectinload

    if match_ids is not None and not match_ids:
        return []
    statement = select(Match).where(Match.tenant_id == tenant_id)
    if match_ids is not None:
        statement = statement.where(Match.id.in_(match_ids))
    matches = session.scalars(
        statement.options(selectinload(Match.products))
    ).all()
    flagged = []
    for m in matches:
        prods = list(m.products)
        # Availability is temporal, so OOS does not create a permanent
        # rejection.  It does, however, remove the website offer from the
        # active cross-site match until a later in-stock scrape can rematch it.
        oos = next(
            (
                p
                for p in prods
                if getattr(p, "offer_availability_status", None) == "out_of_stock"
            ),
            None,
        )
        if oos is not None:
            flagged.append((m, oos, oos))
            continue
        for a, b in itertools.combinations(prods, 2):
            conflict = _has_conflicting_country(a, b) if m.is_manual else _pairwise_spec_conflict(a, b)
            if a.site != b.site and conflict:
                flagged.append((m, a, b))
                break
    return flagged


def _spec_coherent_groups(members: list, conflict_fn=None) -> list[list]:
    """Группы членов, попарно НЕ конфликтующих по _pairwise_spec_conflict.
    Greedy connected-components: член идёт в первую группу, где не конфликтует ни с кем."""
    conflict_fn = conflict_fn or _pairwise_spec_conflict
    groups: list[list] = []
    for p in members:
        for g in groups:
            if all(not conflict_fn(p, q) for q in g):
                g.append(p)
                break
        else:
            groups.append([p])
    return groups


def revalidate_split(
    session: Session,
    *,
    dry_run: bool = False,
    tenant_id: int = 1,
    match_ids: set[int] | None = None,
) -> list[dict]:
    """Разбить кластеры с cross-site spec-конфликтом на spec-когерентные группы.

    Для каждого флагнутого кластера: бьём членов на группы, где никто не конфликтует
    (_pairwise_spec_conflict). Оставляем БОЛЬШУЮ группу с cross-site парой как Match,
    «выкидышей» отвязываем (canonical_id=None) + reject против оставшихся, чтобы пара
    не сматчилась снова. Если когерентной cross-site группы нет — кластер распускаем.
    Это лучше тупого dissolve-all в CLI: верную same-brand пару в 3-членном кластере
    (Herba Flora+Herba Flora vs Xerbes) сохраняем, выкидываем только Xerbes.

    Запускается ПОСЛЕ match_products в пайплайне скрейпа (main.py) — иначе пойманные
    guard'ом несоответствия (бренд/состав/вариант/сила) пересоздаются каждый прогон и
    висят до ручного `rematch --revalidate` (корень «whack-a-mole»). Возвращает список
    действий [{match_id, action, ...}]. При dry_run БД не меняется."""
    import itertools

    from src import match_actions

    acquire_match_mutation_xact_lock(session)

    actions: list[dict] = []
    seen: set[int] = set()
    for m, _a, _b in find_conflicting_clusters(
        session, tenant_id=tenant_id, match_ids=match_ids
    ):
        if m.id in seen:
            continue
        # Manual clusters encode an explicit user decision and are immutable
        # under automatic revalidation. This also matches the CLI contract:
        # `rematch --revalidate` must never dissolve or repartition them.
        if m.is_manual:
            continue
        seen.add(m.id)
        members = list(m.products)
        oos_members = [
            product
            for product in members
            if getattr(product, "offer_availability_status", None) == "out_of_stock"
        ]
        active_members = [product for product in members if product not in oos_members]
        has_country_conflict = any(
            left.site != right.site and _has_conflicting_country(left, right)
            for left, right in itertools.combinations(active_members, 2)
        )
        repartition_strategy = (
            "offer_repartition"
            if oos_members
            else "country_repartition"
            if has_country_conflict
            else "spec_repartition"
        )
        conflict_fn = _has_conflicting_country if m.is_manual else _pairwise_spec_conflict
        groups = sorted(
            _spec_coherent_groups(active_members, conflict_fn), key=len, reverse=True
        )
        viable = [g for g in groups if len({p.site for p in g}) >= 2]
        leftovers = oos_members + [p for g in groups if g not in viable for p in g]
        action = {
            "match_id": m.id,
            "action": "split" if viable else "dissolve",
            "groups": [[p.id for p in group] for group in viable],
            "unmatched": [p.id for p in leftovers],
            "keep": [p.id for p in viable[0]] if viable else [],
            "eject": [p.id for p in leftovers],
        }
        actions.append(action)
        if dry_run:
            continue

        before = {
            "match": {
                "id": m.id,
                "tenant_id": m.tenant_id,
                "canonical_name": m.canonical_name,
                "canonical_brand": m.canonical_brand,
                "canonical_dosage": m.canonical_dosage,
                "canonical_pack_size": m.canonical_pack_size,
                "confidence": m.confidence,
                "is_manual": m.is_manual,
                "match_strategy": m.match_strategy,
                "needs_review": m.needs_review,
            },
            "members": [p.id for p in members],
        }

        rejection_audits: list[dict] = []
        for x, y in itertools.combinations(active_members, 2):
            if x.site == y.site or not conflict_fn(x, y):
                continue
            country_conflict = _has_conflicting_country(x, y)
            a_id, b_id = sorted((x.id, y.id))
            previous = session.scalar(
                select(MatchRejection).where(
                    MatchRejection.product_a_id == a_id,
                    MatchRejection.product_b_id == b_id,
                )
            )
            before_rejection = (
                {
                    "is_active": previous.is_active,
                    "reason": previous.reason,
                    "reason_type": previous.reason_type,
                    "metadata_json": previous.metadata_json,
                }
                if previous is not None
                else None
            )
            rejection = match_actions.add_rejection(
                session,
                x.id,
                y.id,
                reason="country-conflict" if country_conflict else "revalidate-split",
                reason_type="system_country" if country_conflict else "system_spec",
                metadata={"source_match_id": m.id, "policy_version": 1},
            )
            session.flush()
            rejection_audits.append(
                {
                    "id": rejection.id,
                    "before": before_rejection,
                    "after": {
                        "is_active": rejection.is_active,
                        "reason": rejection.reason,
                        "reason_type": rejection.reason_type,
                        "metadata_json": rejection.metadata_json,
                    },
                }
            )

        for product in members:
            product.canonical_id = None

        created_match_ids: list[int] = []
        if viable:
            for index, group in enumerate(viable):
                target = m
                if index > 0:
                    target = Match(
                        tenant_id=m.tenant_id,
                        canonical_name=group[0].name,
                        canonical_brand=group[0].brand,
                        canonical_dosage=group[0].dosage,
                        canonical_pack_size=group[0].pack_size,
                        confidence=m.confidence,
                        is_manual=m.is_manual,
                        match_strategy=repartition_strategy,
                        needs_review=False,
                    )
                    session.add(target)
                    session.flush()
                else:
                    target.canonical_name = group[0].name
                    target.canonical_brand = group[0].brand
                    target.canonical_dosage = group[0].dosage
                    target.canonical_pack_size = group[0].pack_size
                    target.match_strategy = repartition_strategy
                created_match_ids.append(target.id)
                for product in group:
                    product.canonical_id = target.id
        else:
            session.delete(m)

        after_assignments = {
            str(product.id): product.canonical_id for product in members
        }
        after_matches: dict[str, dict] = {}
        for match_id in created_match_ids:
            target = session.get(Match, match_id)
            if target is None:
                continue
            after_matches[str(match_id)] = {
                "canonical_name": target.canonical_name,
                "canonical_brand": target.canonical_brand,
                "canonical_dosage": target.canonical_dosage,
                "canonical_pack_size": target.canonical_pack_size,
                "confidence": target.confidence,
                "is_manual": target.is_manual,
                "match_strategy": target.match_strategy,
                "needs_review": target.needs_review,
                "members": sorted(
                    product.id
                    for product in members
                    if product.canonical_id == match_id
                ),
            }

        session.add(
            MatchPolicyAudit(
                tenant_id=m.tenant_id,
                match_id=m.id,
                action=(
                    "offer_repartition"
                    if oos_members and viable
                    else "offer_dissolve"
                    if oos_members
                    else "country_repartition"
                    if viable and has_country_conflict
                    else "country_dissolve"
                    if has_country_conflict
                    else "spec_repartition"
                    if viable
                    else "spec_dissolve"
                ),
                payload={
                    "policy_version": 2,
                    "conflict_kind": (
                        "offer"
                        if oos_members
                        else "country"
                        if has_country_conflict
                        else "spec"
                    ),
                    "before": before,
                    "after": {
                        "groups": action["groups"],
                        "unmatched": action["unmatched"],
                        "match_ids": created_match_ids,
                        "assignments": after_assignments,
                        "matches": after_matches,
                        "original_match_exists": bool(
                            viable and m.id in created_match_ids
                        ),
                    },
                    "rejections": rejection_audits,
                },
            )
        )
    if actions and not dry_run:
        session.commit()
    return actions


def relink_dead_members(
    session: Session, *, dry_run: bool = False, min_score: int = 80
) -> list[dict]:
    """Авто-кластеры с мёртвым (url_dead_at) членом → подменить его живой
    альтернативой того же сайта/спеки (через match_actions.swap_alternative).

    Зачем: validate-links метит фантомные URL мёртвыми, comparison их прячет, и
    строка теряет конкурента — хотя живая альтернатива того же товара существует
    unmatched (напр. Asiklovir: aptek Terapia сдохла, живой дженерик не подвязан).

    Безопасность кандидата: (1) живой + unmatched (find_alternatives отдаёт только
    canonical_id IS NULL), (2) тот же pack_count и strength, что у мёртвого (один
    сайт → единый формат), (3) НЕ конфликтует с живым cross-site anchor'ом по
    _hard_conflict/_pairwise_spec_conflict, (4) fuzzy score >= min_score. Ручные
    (is_manual) кластеры не трогаем. Возвращает план [{match_id,site,old,new,score,
    action}]; при dry_run БД не меняется (swap_alternative не вызывается)."""
    from src import match_actions

    results: list[dict] = []
    dead_members = session.scalars(
        select(Product).where(
            Product.url_dead_at.is_not(None),
            Product.canonical_id.is_not(None),
        )
    ).all()
    seen: set[tuple[int, str]] = set()
    for d in dead_members:
        key = (d.canonical_id, d.site)
        if key in seen:
            continue
        seen.add(key)
        m = session.get(Match, d.canonical_id)
        if m is None or m.is_manual:
            continue
        anchor = next((p for p in m.products if p.url_dead_at is None and p.site != d.site), None)
        if anchor is None:
            results.append(
                {
                    "match_id": d.canonical_id,
                    "site": d.site,
                    "old": d.id,
                    "new": None,
                    "score": None,
                    "action": "skip-no-anchor",
                }
            )
            continue
        d_pack = _pack_count(d.pack_size or "")
        d_str = _strength_numbers(d.name or "")
        chosen = None
        for cand, score in match_actions.find_alternatives(session, m.id, d.site, limit=25):
            if score < min_score:
                break  # отсортировано по убыванию
            if cand.url_dead_at is not None:
                continue
            if _pack_count(cand.pack_size or "") != d_pack:
                continue
            if _strength_numbers(cand.name or "") != d_str:
                continue
            if _hard_conflict(anchor, cand) or _pairwise_spec_conflict(anchor, cand):
                continue
            chosen = (cand, score)
            break
        if chosen is None:
            results.append(
                {
                    "match_id": m.id,
                    "site": d.site,
                    "old": d.id,
                    "new": None,
                    "score": None,
                    "action": "skip-no-live-alt",
                }
            )
            continue
        cand, score = chosen
        results.append(
            {
                "match_id": m.id,
                "site": d.site,
                "old": d.id,
                "new": cand.id,
                "score": score,
                "action": "swap",
            }
        )
        if not dry_run:
            match_actions.swap_alternative(session, m.id, d.site, cand.id)
    return results


def _build_word_freq(products: list) -> dict[str, int]:
    """Частота слов по всем name_normalized.

    Используется для авто-обнаружения категорийных слов без хардкода.
    Высокочастотные слова (sac=волосы, dis=зубы, usaq=дети) = общекатегорийные.
    Низкочастотные слова = специфичные торговые названия / бренды.
    """
    freq: dict[str, int] = defaultdict(int)
    for p in products:
        seen: set[str] = set()
        for tok in (p.name_normalized or "").split():
            if len(tok) >= 3 and tok not in seen:
                freq[tok] += 1
                seen.add(tok)
    return dict(freq)


def _first_brand_token(
    name_norm: str,
    freq: dict[str, int],
    max_freq: int,
) -> str | None:
    """Первый токен name_norm с частотой ниже max_freq.

    Токены с высокой частотой = категорийные (sac, dis, gigiyenik, usaq).
    Первый редкий токен ≈ торговое название/бренд.
    None — если все токены высокочастотные (неразличимый товар без бренда).
    """
    for tok in name_norm.split():
        if len(tok) >= 3 and freq.get(tok, 0) < max_freq:
            return tok
    return None


def _norm_units(s: str) -> str:
    """Нормализует единицы дозировки/объёма для bucket_key.

    Унифицирует азербайджанские/русские/немецкие аббревиатуры с международными:
    - mq → mg   (милиграм по-азербайджански = milligram)
    - mkg → mcg (микрограм)
    - цифра+q (напр. «5q», «0.5q») → цифра+g (gram)
    - цифра+bv → цифра+iu  (BV = Bioloji Vahid ≈ IU — Ukraferon/interferon, aptekonline)
    - цифра+me → цифра+iu  (ME = Mezinárodní jednotka = IU — aptekonline URL-slugs)
    - цифра+ie → цифра+iu  (IE = Internationale Einheit = IU — немецкий)

    Паттерн (\\d)(bv|me|ie)\\b требует цифру перед единицей — исключает
    случайное совпадение с брендами или словами (me в названии препарата).

    Без этого «ukraferon 500000bv/n10» (aptekonline) и «ukraferon 500000iu/n10»
    (pharmonline) попадают в разные bucket'ы, хотя это идентичный товар.

    strip_accents в начале убирает combining chars из stale данных БД:
    «500000i̇u» (İU после .lower() в старом extract_dosage) → «500000iu».
    """
    s = strip_accents(s)  # İ → i (combining dot removed), Ü → u и т.п.
    s = s.replace("mq", "mg").replace("mkg", "mcg")
    s = re.sub(r"(\d)q\b", r"\1g", s)
    # Международные единицы: bv/me/ie → iu
    s = re.sub(r"(\d)(bv|me|ie)\b", r"\1iu", s)
    return s


def _pack_count(pack_size: str) -> float:
    """Извлекает число из pack_size: 'n10' → 10.0, 'n30' → 30.0, '' → 1.0."""
    if not pack_size:
        return 1.0
    m = re.search(r"(\d+(?:\.\d+)?)", pack_size)
    return float(m.group(1)) if m else 1.0


# ── Мismatch «по штуке vs по упаковке» ──────────────────────────────────────
# Aloe.az продаёт ряд товаров поштучно (1 флакон за 8.90 AZN), тогда как
# pharmonline/aptekonline продают заводскую упаковку N10 за 89.00 AZN.
# Нормализованная цена: price / pack_count.
# Если у одного продукта норм-цена в ≥5 раз ниже другого — это единица vs упаковка.
# Порог 5× (не 10×) чтобы поймать N30/N20 тоже (30/5=6×, 20/4=5×).
_PERUNIT_PRICE_RATIO = 5.0


def _has_perunit_mismatch(
    cluster: list[Product],
    prices: dict[int, PriceSnapshot],
) -> bool:
    """True если кластер смешивает цену-за-штуку с ценой-за-упаковку.

    Вычисляет нормализованную цену = price / pack_count для каждого продукта
    в кластере. Если max/min нормализованных цен ≥ _PERUNIT_PRICE_RATIO →
    это ложный матч (разные единицы продажи).

    Примеры блокировки:
      aloe 8.90 / n10  → 0.89 AZN/ед
      pharmonline 89.00 / n10 → 8.90 AZN/ед
      ratio = 10.0 ≥ 5 → BLOCK

    Примеры пропуска:
      aloe 0.95 / n30 → 0.032 AZN/ед
      pharmonline 2.29 / n30 → 0.076 AZN/ед
      ratio = 2.4 < 5 → OK (реальная разница цен)

    Если цена неизвестна хотя бы для одного продукта — пропускаем проверку
    (не блокируем без данных).
    """
    norms: list[float] = []
    for p in cluster:
        snap = prices.get(p.id)
        if snap is None or snap.price is None or snap.price <= 0:
            return False  # нет данных — не блокируем
        count = _pack_count(p.pack_size or "")
        norms.append(snap.price / count)
    if len(norms) < 2:
        return False
    ratio = max(norms) / min(norms)
    if ratio >= _PERUNIT_PRICE_RATIO:
        log.debug(
            "matcher_perunit_mismatch",
            products=[p.id for p in cluster],
            norms=norms,
            ratio=round(ratio, 1),
        )
        return True
    return False


def match_products(
    session: Session,
    fuzzy_threshold: int = FUZZY_THRESHOLD,
    *,
    tenant_id: int = 1,
) -> int:
    """Прогнать матчинг на всех товарах в БД.

    Не трогает товары с is_manual=True их Match.
    Возвращает количество новых/обновлённых связок.
    """
    acquire_match_mutation_xact_lock(session)
    products = session.scalars(
        select(Product).where(Product.tenant_id == tenant_id)
    ).all()
    if not products:
        return 0

    by_site: dict[str, list[Product]] = defaultdict(list)
    for p in products:
        by_site[p.site].append(p)

    sites = sorted(by_site.keys())
    log.info("matcher_start", total_products=len(products), sites=sites)

    # Pre-fetch последние цены для всех продуктов — нужны для проверки
    # per-unit vs per-pack (цена-за-штуку vs цена-за-упаковку).
    # Один SELECT на все product_ids, не N+1.
    all_ids = [p.id for p in products]
    latest_prices: dict[int, PriceSnapshot] = latest_snapshots_per_product(session, all_ids)

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
        # Blacklist-фильтр (2026-05-29): generic AZ-слова (baby, sabun, günəş,
        # qoruyucu…) массово извлекаются как «бренд» и раздувают/искажают
        # bucket → пропущенные cross-site матчи. Если brand в блок-листе —
        # трактуем как пустой, чтобы упасть на name-based fallback (ниже).
        if brand and is_brand_blacklisted(brand):
            brand = ""
        # _norm_units: mq→mg, mkg→mcg, (\d)q→\1g — унифицирует AZ/RU единицы
        # с международными, чтобы «250mq» и «250mg» попадали в один bucket.
        dosage = _norm_units((p.dosage or "").lower().replace(" ", ""))
        pack = _norm_units((p.pack_size or "").lower().replace(" ", ""))
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

    # ── Ambiguity pre-pass (2026-05-29): неоднозначный генерик ──────────────────
    # aptek листит коммодити генерически («Çaytikanı yağı 100ml»), pharm — с
    # брендом (Altay/Mirrolla/Medoil/Seide). Генерик подходит под НЕСКОЛЬКО разных
    # брендов → какой «тот же товар» неизвестно → матчер цеплял произвольно (ложный
    # cross-brand spread). Правило: если товар fuzzy-матчится к ≥2 кросс-сайт
    # кандидатам с РАЗНЫМИ значащими «лишними» токенами — он неоднозначен → не
    # матчим (кладём в visited ниже). Легит verbose-vs-terse НЕ страдает: Nestogen-1
    # матчится к ОДНОМУ Nestogen-1 (один кандидат, не ≥2 различных). Валидировано
    # на прод-дампе (scripts/scan_brandstub.py).
    ambiguous_ids: set[int] = set()
    for _agrp in buckets.values():
        if len(_agrp) < 3:
            continue
        _sig = {p.id: _significant_name_tokens(p.name_normalized) for p in _agrp}
        for q in _agrp:
            _qsig = _sig[q.id]
            _extras: set[frozenset[str]] = set()
            for p in _agrp:
                if p.site == q.site or p.id == q.id:
                    continue
                if (
                    fuzz.token_set_ratio(q.name_normalized or "", p.name_normalized or "")
                    < fuzzy_threshold
                ):
                    continue
                # Считаем кандидата, только если настоящий проход его НЕ отверг бы
                # (иначе Nutrilon Premium 1 ложно неоднозначен из-за Comfort/Pepti).
                if _hard_conflict(q, p):
                    continue
                # Считаем «лишнее» ТОЛЬКО когда q — генерик ОТНОСИТЕЛЬНО p (q ⊆ p):
                # тогда p добавляет бренд/вариант, которого у q нет. Если бренды
                # ВЗАИМНО различаются (Medoil vs Fitooil — ни один не подмножество),
                # это просто разные товары, q не генерик → не считаем (иначе легит
                # same-brand Medoil↔Medoil ложно подавляется соседом Fitooil).
                _psig = _sig[p.id]
                if _qsig <= _psig:
                    _extra = _psig - _qsig
                    if _extra:
                        _extras.add(_extra)
            # Неоднозначен, если q-генерик подходит к ≥2 кандидатам с РАЗНЫМИ
            # добавочными токенами (разные бренды/варианты: Altay vs Mirrolla vs Seide).
            if len(_extras) >= 2:
                ambiguous_ids.add(q.id)
    if ambiguous_ids:
        log.info("matcher_ambiguous_generics_suppressed", count=len(ambiguous_ids))

    # ── Sibling-form check ───────────────────────────────────────────────────
    # Если продукт P не имеет формы выпуска, но на его сайте в том же bucket'е
    # уже есть другой продукт с явной формой X → P НЕ является формой X (иначе
    # зачем сайт листил бы оба?).  Используется ниже в обоих проходах.
    #
    # Пример: aptk 11201 «Ukraferon 1000000 BV N10» (форма = None, вероятно
    # назальные капли) vs pharm 4119 «…(Suppositories)» (форма = suppository).
    # На aptekonline в том же bucket'е есть aptk 10761 «…(rektal şamlar)»
    # (форма = suppository) → aptk 11201 — не суппозиторий → матч запрещён.
    #
    # _prod_form  : product.id → extracted form (or None)
    # _prod_bk    : product.id → bucket_key(product)
    # site_bucket_forms : (site, bucket_key) → set of forms present on that site
    _prod_form: dict[int, str | None] = {p.id: extract_form(p.name or "") for p in products}
    _prod_bk: dict[int, tuple] = {p.id: bucket_key(p) for p in products}

    site_bucket_forms: dict[tuple, set[str]] = defaultdict(set)
    for p in products:
        f = _prod_form[p.id]
        if f is not None:
            site_bucket_forms[(p.site, _prod_bk[p.id])].add(f)

    created_or_updated = 0
    visited: set[int] = set()

    # ── Pass 0: barcode-based matching (Phase 2.3, 2026-05-27) ──────────────
    # Если два продукта с РАЗНЫХ сайтов имеют одинаковый barcode (EAN/GTIN/UPC),
    # это canonical signal — один и тот же товар. Confidence = 1.0, skip всех
    # эвристик name/dosage/form (barcode trumps name).
    #
    # Эвристики ниже (modifier conflicts, series numbers и т.д.) могут давать
    # false negative — два продукта с одинаковым barcode но слегка разными
    # name'ами (опечатка, другая транслитерация) НЕ будут смэтчены через fuzzy.
    # Barcode pass их спасает. Скорее всего barcode coverage ~50-70%, остальные
    # уйдут в fuzzy ниже.
    #
    # Защита от мусора: если barcode пустой / "0" / "null" / < 8 digits —
    # игнорируем (это noise от scrapers, не реальный barcode).
    by_barcode: dict[str, list[Product]] = defaultdict(list)
    for p in products:
        bc = (p.barcode or "").strip()
        if not bc or bc in ("0", "null", "none") or not bc.isdigit() or len(bc) < 8:
            continue
        by_barcode[bc].append(p)

    barcode_matches_created = 0
    for bc, group in by_barcode.items():
        if len(group) < 2:
            continue
        # A dirty/reused barcode must not let one conflicting pack poison a
        # correct pair. Build deterministic pack-compatible cohorts first;
        # this also makes the result independent from product-id order.
        pack_groups = _spec_coherent_groups(
            sorted(group, key=lambda product: product.id),
            conflict_fn=_has_conflicting_pack_count,
        )
        for pack_group in pack_groups:
            # Берём по одному продукту с каждого уникального сайта (если на одном
            # сайте несколько продуктов с тем же barcode — это нормально для variants
            # одного товара, но мы матчим cross-site).
            seen_sites: set[str] = set()
            cluster: list[Product] = []
            for p in pack_group:
                if p.site in seen_sites:
                    continue
                if any(is_rejected(session, c.id, p.id) for c in cluster):
                    continue
                cluster.append(p)
                seen_sites.add(p.site)
            if len(cluster) < 2:
                continue
            # Per-unit price sanity check ещё держим — даже одинаковый barcode на
            # разных сайтах может быть продан per-pack vs per-piece.
            if _has_perunit_mismatch(cluster, latest_prices):
                log.warning(
                    "matcher_barcode_perunit_mismatch",
                    barcode=bc,
                    products=[p.id for p in cluster],
                )
                continue
            persisted = _persist_match(session, cluster, confidence=1.0)
            if persisted:
                created_or_updated += persisted
                visited.update(c.id for c in cluster)
                barcode_matches_created += persisted

    log.info(
        "matcher_barcode_pass",
        unique_barcodes=len(by_barcode),
        clusters_created=barcode_matches_created,
    )

    # Неоднозначные генерики (ambiguity pre-pass выше) → в visited, чтобы все 4
    # fuzzy-прохода их пропускали (не матчили ни анкером, ни кандидатом). Barcode-
    # матч их не трогает — у генериков нет штрихкода.
    visited |= ambiguous_ids

    for key, group in buckets.items():
        if len(group) < 2:
            continue
        # внутри ведра — попарно искать матчи между сайтами
        # group:[p1, p2, ...]
        for i, p in enumerate(group):
            if p.id in visited:
                continue
            cluster = [p]
            _min_score = 100.0  # минимальный fuzzy score в кластере → confidence
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
                if any(_has_conflicting_pack_count(c, q) for c in cluster):
                    continue
                # Sibling-form: у q нет формы, но на сайте q в этом bucket'е
                # уже есть продукт с явной формой p → q НЕ является этой формой.
                # И наоборот — у p нет формы, а на сайте p уже есть форма q.
                _fp, _fq = _prod_form[p.id], _prod_form[q.id]
                if _fp is not None and _fq is None:
                    if _fp in site_bucket_forms.get((q.site, _prod_bk[q.id]), set()):
                        continue
                if _fq is not None and _fp is None:
                    if _fq in site_bucket_forms.get((p.site, _prod_bk[p.id]), set()):
                        continue
                # Stub vs полное имя: «venatura» vs «venatura vitamin a palmitate…»
                # (проверяем только против якоря — длина якоря самая репрезентативная)
                if _has_extreme_length_disparity(
                    p.name_normalized or "",
                    q.name_normalized or "",
                    brand_hint=(p.brand or q.brand or ""),
                ):
                    continue
                # Осиротевшее число дозировки: «mezim forte» vs «mezim forte 3500 ed»
                if _has_conflicting_orphan_number(p.name_normalized or "", q.name_normalized or ""):
                    continue
                # Гендерный конфликт: мальчики vs девочки → разные продукты
                if _has_conflicting_gender(p.name_normalized or "", q.name_normalized or ""):
                    continue
                # Вариантный конфликт: splat aktiv vs splat lavandasept → разные варианты
                # Проверяем против ВСЕХ членов кластера (транзитивная защита).
                if any(
                    _has_conflicting_variant_tokens(
                        c.name_normalized or "", q.name_normalized or ""
                    )
                    for c in cluster
                ):
                    continue
                # Вариант-атомы из RAW (буква/серийная цифра, вырезанные
                # нормализатором): Lorinden C ≠ A, Vitamin A ≠ C, Normoqlip M ≠ 2.
                if any(_has_conflicting_variant_atoms(c.name or "", q.name or "") for c in cluster):
                    continue
                # Многозначная сила: Mikrazim 25000 ED ≠ 10000 (enzyme/IU единицы).
                if any(
                    _has_conflicting_strength_number(c.name or "", q.name or "") for c in cluster
                ):
                    continue
                # Разная страна производителя: Talya(Türkiyə) ≠ Azerfarm(Azərbaycan)
                # глицерин — разный товар, ложный 85% spread. Блок только если у
                # обоих страна известна и различается (см. _has_conflicting_country).
                if any(_has_conflicting_country(c, q) for c in cluster):
                    continue
                # Габариты: пластырь 10×10 sm ≠ 10×25 sm (разный размер = разный товар).
                if any(_has_conflicting_dimensions(c.name or "", q.name or "") for c in cluster):
                    continue
                # Концентрация: Tetrasiklin 3% ≠ 1%, Novokain 2% ≠ 0.5%.
                if any(_has_conflicting_concentration(c.name or "", q.name or "") for c in cluster):
                    continue
                # Вариант-маркеры (СИММЕТРИЧНО, взаимно-уникальные): Ag↔Tip1, type1↔type2,
                # Ag↔Cu380. Односторонний (Ag vs без маркера) НЕ блокирует.
                if any(
                    _has_conflicting_variant_marker(c.name or "", q.name or "") for c in cluster
                ):
                    continue
                score = fuzz.token_set_ratio(p.name_normalized, q.name_normalized)
                if score >= fuzzy_threshold:
                    cluster.append(q)
                    _min_score = min(_min_score, float(score))

            if len(cluster) >= 2:
                # Проверка: не смешиваем цену-за-штуку с ценой-за-упаковку
                if _has_perunit_mismatch(cluster, latest_prices):
                    continue
                confidence = _min_score / 100.0
                persisted = _persist_match(session, cluster, confidence)
                if persisted:
                    created_or_updated += persisted
                    visited.update(c.id for c in cluster)

    # ── Secondary pass: (brand, pack) без досировки ─────────────────────────
    # Охватывает пары, где один сайт спарсил dosage, другой — нет.
    # Пример: aptekonline ('alvis', '', 'n40') ↔ pharmonline ('alvis', '60mg/300mg', 'n40').
    # После первого прохода оба остаются unvisited (разные bucket_key).
    # Используем порог 85 (строже базового 78), чтобы компенсировать
    # ослабленное ограничение на dosage.
    _SEC_THRESHOLD = 85
    _SEC_CONF_CAP = 0.80  # вторичный проход: dosage не совпал → ниже уверенность
    by_brand_pack: dict[tuple, list[Product]] = defaultdict(list)
    for p in products:
        if p.id in visited:
            continue
        bk = bucket_key(p)
        bp_key = (bk[0], bk[2])  # (brand, pack)
        if not bp_key[0] or not bp_key[1]:
            continue  # без бренда или упаковки — слишком широкий bucket
        by_brand_pack[bp_key].append(p)

    log.info(
        "matcher_secondary_pass",
        brand_pack_buckets=len(by_brand_pack),
        candidates=sum(len(g) for g in by_brand_pack.values()),
    )

    for _bp_key, group in by_brand_pack.items():
        if len(group) < 2:
            continue
        for i, p in enumerate(group):
            if p.id in visited:
                continue
            dosage_p = _norm_units((p.dosage or "").lower().replace(" ", ""))
            cluster = [p]
            _min_score_sec = float(_SEC_THRESHOLD)
            for q in group[i + 1 :]:
                if q.id in visited:
                    continue
                if q.site == p.site:
                    continue
                if any(c.site == q.site for c in cluster):
                    continue
                dosage_q = _norm_units((q.dosage or "").lower().replace(" ", ""))
                # Вторичный проход: только если хотя бы у одного пустая дозировка.
                # Пары с двумя непустыми разными дозировками — это явно разные
                # препараты (даже если brand+pack совпадают), не матчим.
                if dosage_p and dosage_q:
                    continue
                if any(is_rejected(session, c.id, q.id) for c in cluster):
                    continue
                if any(
                    _has_conflicting_modifier(c.name_normalized or "", q.name_normalized or "")
                    for c in cluster
                ):
                    continue
                if any(
                    _has_conflicting_series_number(c.name_normalized or "", q.name_normalized or "")
                    for c in cluster
                ):
                    continue
                if _has_conflicting_form(p.name or "", q.name or ""):
                    continue
                _fp, _fq = _prod_form[p.id], _prod_form[q.id]
                if _fp is not None and _fq is None:
                    if _fp in site_bucket_forms.get((q.site, _prod_bk[q.id]), set()):
                        continue
                if _fq is not None and _fp is None:
                    if _fq in site_bucket_forms.get((p.site, _prod_bk[p.id]), set()):
                        continue
                if _has_extreme_length_disparity(
                    p.name_normalized or "",
                    q.name_normalized or "",
                    brand_hint=(p.brand or q.brand or ""),
                ):
                    continue
                if _has_conflicting_orphan_number(p.name_normalized or "", q.name_normalized or ""):
                    continue
                if _has_conflicting_gender(p.name_normalized or "", q.name_normalized or ""):
                    continue
                if any(
                    _has_conflicting_variant_tokens(
                        c.name_normalized or "", q.name_normalized or ""
                    )
                    for c in cluster
                ):
                    continue
                # Вариант-атомы из RAW (буква/серийная цифра, вырезанные
                # нормализатором): Lorinden C ≠ A, Vitamin A ≠ C, Normoqlip M ≠ 2.
                if any(_has_conflicting_variant_atoms(c.name or "", q.name or "") for c in cluster):
                    continue
                # Многозначная сила: Mikrazim 25000 ED ≠ 10000 (enzyme/IU единицы).
                if any(
                    _has_conflicting_strength_number(c.name or "", q.name or "") for c in cluster
                ):
                    continue
                # Разная страна производителя: Talya(Türkiyə) ≠ Azerfarm(Azərbaycan)
                # глицерин — разный товар, ложный 85% spread. Блок только если у
                # обоих страна известна и различается (см. _has_conflicting_country).
                if any(_has_conflicting_country(c, q) for c in cluster):
                    continue
                # Габариты: пластырь 10×10 sm ≠ 10×25 sm (разный размер = разный товар).
                if any(_has_conflicting_dimensions(c.name or "", q.name or "") for c in cluster):
                    continue
                # Концентрация: Tetrasiklin 3% ≠ 1%, Novokain 2% ≠ 0.5%.
                if any(_has_conflicting_concentration(c.name or "", q.name or "") for c in cluster):
                    continue
                # Вариант-маркеры (СИММЕТРИЧНО, взаимно-уникальные): Ag↔Tip1, type1↔type2,
                # Ag↔Cu380. Односторонний (Ag vs без маркера) НЕ блокирует.
                if any(
                    _has_conflicting_variant_marker(c.name or "", q.name or "") for c in cluster
                ):
                    continue
                if any(_has_conflicting_pack_count(c, q) for c in cluster):
                    continue
                score = fuzz.token_set_ratio(p.name_normalized, q.name_normalized)
                if score >= _SEC_THRESHOLD:
                    cluster.append(q)
                    _min_score_sec = min(_min_score_sec, float(score))

            if len(cluster) >= 2:
                if _has_perunit_mismatch(cluster, latest_prices):
                    continue
                confidence = min(_min_score_sec / 100.0, _SEC_CONF_CAP)
                persisted = _persist_match(session, cluster, confidence)
                if persisted:
                    created_or_updated += persisted
                    visited.update(c.id for c in cluster)

    # ── Tertiary pass: (brand, dosage) без pack ─────────────────────────────
    # Охватывает пары, где один сайт не вытащил pack_size (или разный).
    # Пример: pharmonline ('aspirin', '500mg', '') ↔ aptekonline ('aspirin', '500mg', 'n20')
    #   → primary: разные bucket_key → не матчатся
    #   → secondary: ('aspirin', 'n20') vs ('aspirin', '') → secondary пропускает (нет pack у одного)
    #   → tertiary: (brand='aspirin', dosage='500mg') → совпадает → матчим
    #
    # Требует: brand != '' И dosage != '' (без них bucket слишком широкий).
    # Порог строже базового (85 vs 75), confidence capped 0.72.
    _TERT_THRESHOLD = 85
    _TERT_CONF_CAP = 0.72
    by_brand_dosage: dict[tuple, list[Product]] = defaultdict(list)
    for p in products:
        if p.id in visited:
            continue
        bk = bucket_key(p)
        bd_key = (bk[0], bk[1])  # (brand, dosage)
        if not bd_key[0] or not bd_key[1]:
            continue  # без бренда или дозировки — слишком широкий bucket
        by_brand_dosage[bd_key].append(p)

    log.info(
        "matcher_tertiary_pass",
        brand_dosage_buckets=len(by_brand_dosage),
        candidates=sum(len(g) for g in by_brand_dosage.values()),
    )

    for _bd_key, group in by_brand_dosage.items():
        if len(group) < 2:
            continue
        for i, p in enumerate(group):
            if p.id in visited:
                continue
            pack_p = _norm_units((p.pack_size or "").lower().replace(" ", ""))
            cluster = [p]
            _min_score_tert = float(_TERT_THRESHOLD)
            for q in group[i + 1 :]:
                if q.id in visited:
                    continue
                if q.site == p.site:
                    continue
                if any(c.site == q.site for c in cluster):
                    continue
                pack_q = _norm_units((q.pack_size or "").lower().replace(" ", ""))
                # Tertiary: только если у кого-то из пары нет pack_size.
                # Если оба с pack — они разошлись бы в primary (разный pack),
                # что значит они осознанно разные SKU (N10 vs N20 etc.).
                if pack_p and pack_q:
                    continue
                if any(is_rejected(session, c.id, q.id) for c in cluster):
                    continue
                if any(
                    _has_conflicting_modifier(c.name_normalized or "", q.name_normalized or "")
                    for c in cluster
                ):
                    continue
                if any(
                    _has_conflicting_series_number(c.name_normalized or "", q.name_normalized or "")
                    for c in cluster
                ):
                    continue
                if _has_conflicting_form(p.name or "", q.name or ""):
                    continue
                _fp, _fq = _prod_form[p.id], _prod_form[q.id]
                if _fp is not None and _fq is None:
                    if _fp in site_bucket_forms.get((q.site, _prod_bk[q.id]), set()):
                        continue
                if _fq is not None and _fp is None:
                    if _fq in site_bucket_forms.get((p.site, _prod_bk[p.id]), set()):
                        continue
                if _has_extreme_length_disparity(
                    p.name_normalized or "",
                    q.name_normalized or "",
                    brand_hint=(p.brand or q.brand or ""),
                ):
                    continue
                if _has_conflicting_orphan_number(p.name_normalized or "", q.name_normalized or ""):
                    continue
                if _has_conflicting_gender(p.name_normalized or "", q.name_normalized or ""):
                    continue
                if any(
                    _has_conflicting_variant_tokens(
                        c.name_normalized or "", q.name_normalized or ""
                    )
                    for c in cluster
                ):
                    continue
                # Вариант-атомы из RAW (буква/серийная цифра, вырезанные
                # нормализатором): Lorinden C ≠ A, Vitamin A ≠ C, Normoqlip M ≠ 2.
                if any(_has_conflicting_variant_atoms(c.name or "", q.name or "") for c in cluster):
                    continue
                # Многозначная сила: Mikrazim 25000 ED ≠ 10000 (enzyme/IU единицы).
                if any(
                    _has_conflicting_strength_number(c.name or "", q.name or "") for c in cluster
                ):
                    continue
                # Разная страна производителя: Talya(Türkiyə) ≠ Azerfarm(Azərbaycan)
                # глицерин — разный товар, ложный 85% spread. Блок только если у
                # обоих страна известна и различается (см. _has_conflicting_country).
                if any(_has_conflicting_country(c, q) for c in cluster):
                    continue
                # Габариты: пластырь 10×10 sm ≠ 10×25 sm (разный размер = разный товар).
                if any(_has_conflicting_dimensions(c.name or "", q.name or "") for c in cluster):
                    continue
                # Концентрация: Tetrasiklin 3% ≠ 1%, Novokain 2% ≠ 0.5%.
                if any(_has_conflicting_concentration(c.name or "", q.name or "") for c in cluster):
                    continue
                # Вариант-маркеры (СИММЕТРИЧНО, взаимно-уникальные): Ag↔Tip1, type1↔type2,
                # Ag↔Cu380. Односторонний (Ag vs без маркера) НЕ блокирует.
                if any(
                    _has_conflicting_variant_marker(c.name or "", q.name or "") for c in cluster
                ):
                    continue
                if any(_has_conflicting_pack_count(c, q) for c in cluster):
                    continue
                score = fuzz.token_set_ratio(p.name_normalized, q.name_normalized)
                if score >= _TERT_THRESHOLD:
                    cluster.append(q)
                    _min_score_tert = min(_min_score_tert, float(score))

            if len(cluster) >= 2:
                if _has_perunit_mismatch(cluster, latest_prices):
                    continue
                confidence = min(_min_score_tert / 100.0, _TERT_CONF_CAP)
                persisted = _persist_match(session, cluster, confidence)
                if persisted:
                    created_or_updated += persisted
                    visited.update(c.id for c in cluster)

    # ── Quaternary pass: авто-обнаружение бренда по частоте слов ──────────────
    # Для no-brand продуктов определяет "бренд" из name_normalized автоматически:
    # - строит карту частот слов по всему каталогу (один проход)
    # - порог = 1% каталога (адаптируется при росте данных, без хардкода)
    # - высокочастотные токены = категорийные слова (sac, dis, usaq, gigiyenik)
    # - первый низкочастотный токен = специфичное торговое название / бренд
    #
    # Не требует ручного обслуживания: по мере роста каталога порог растёт,
    # редкие бренды остаются различимыми.
    #
    # Порог fuzzy строже базового (88 vs 75): bucket шире (нет dosage/pack),
    # поэтому нужна более высокая уверенность в имени.
    _QUART_FREQ_MAX = max(50, len(products) // 100)  # 1% от каталога
    _QUART_THRESHOLD = 88
    _QUART_CONF_CAP = 0.68

    word_freq = _build_word_freq(products)

    by_auto_brand: dict[str, list[Product]] = defaultdict(list)
    for p in products:
        if p.id in visited:
            continue
        if p.brand:
            continue  # только no-brand продукты
        auto_brand = _first_brand_token(p.name_normalized or "", word_freq, _QUART_FREQ_MAX)
        if not auto_brand:
            continue
        by_auto_brand[auto_brand].append(p)

    log.info(
        "matcher_quaternary_pass",
        freq_threshold=_QUART_FREQ_MAX,
        auto_brand_buckets=len(by_auto_brand),
        candidates=sum(len(g) for g in by_auto_brand.values()),
    )

    for _ab_key, group in by_auto_brand.items():
        if len(group) < 2:
            continue
        for i, p in enumerate(group):
            if p.id in visited:
                continue
            cluster = [p]
            _min_score_q = float(_QUART_THRESHOLD)
            for q in group[i + 1 :]:
                if q.id in visited:
                    continue
                if q.site == p.site:
                    continue
                if any(c.site == q.site for c in cluster):
                    continue
                if any(is_rejected(session, c.id, q.id) for c in cluster):
                    continue
                if any(
                    _has_conflicting_modifier(c.name_normalized or "", q.name_normalized or "")
                    for c in cluster
                ):
                    continue
                if any(
                    _has_conflicting_series_number(c.name_normalized or "", q.name_normalized or "")
                    for c in cluster
                ):
                    continue
                if _has_conflicting_form(p.name or "", q.name or ""):
                    continue
                _fp, _fq = _prod_form[p.id], _prod_form[q.id]
                if _fp is not None and _fq is None:
                    if _fp in site_bucket_forms.get((q.site, _prod_bk[q.id]), set()):
                        continue
                if _fq is not None and _fp is None:
                    if _fq in site_bucket_forms.get((p.site, _prod_bk[p.id]), set()):
                        continue
                if _has_extreme_length_disparity(
                    p.name_normalized or "",
                    q.name_normalized or "",
                    brand_hint=(p.brand or q.brand or ""),
                ):
                    continue
                if _has_conflicting_orphan_number(p.name_normalized or "", q.name_normalized or ""):
                    continue
                if _has_conflicting_gender(p.name_normalized or "", q.name_normalized or ""):
                    continue
                if any(
                    _has_conflicting_variant_tokens(
                        c.name_normalized or "", q.name_normalized or ""
                    )
                    for c in cluster
                ):
                    continue
                # Вариант-атомы из RAW (буква/серийная цифра, вырезанные
                # нормализатором): Lorinden C ≠ A, Vitamin A ≠ C, Normoqlip M ≠ 2.
                if any(_has_conflicting_variant_atoms(c.name or "", q.name or "") for c in cluster):
                    continue
                # Многозначная сила: Mikrazim 25000 ED ≠ 10000 (enzyme/IU единицы).
                if any(
                    _has_conflicting_strength_number(c.name or "", q.name or "") for c in cluster
                ):
                    continue
                # Разная страна производителя: Talya(Türkiyə) ≠ Azerfarm(Azərbaycan)
                # глицерин — разный товар, ложный 85% spread. Блок только если у
                # обоих страна известна и различается (см. _has_conflicting_country).
                if any(_has_conflicting_country(c, q) for c in cluster):
                    continue
                # Габариты: пластырь 10×10 sm ≠ 10×25 sm (разный размер = разный товар).
                if any(_has_conflicting_dimensions(c.name or "", q.name or "") for c in cluster):
                    continue
                # Концентрация: Tetrasiklin 3% ≠ 1%, Novokain 2% ≠ 0.5%.
                if any(_has_conflicting_concentration(c.name or "", q.name or "") for c in cluster):
                    continue
                # Вариант-маркеры (СИММЕТРИЧНО, взаимно-уникальные): Ag↔Tip1, type1↔type2,
                # Ag↔Cu380. Односторонний (Ag vs без маркера) НЕ блокирует.
                if any(
                    _has_conflicting_variant_marker(c.name or "", q.name or "") for c in cluster
                ):
                    continue
                if any(_has_conflicting_pack_count(c, q) for c in cluster):
                    continue
                score = fuzz.token_set_ratio(p.name_normalized, q.name_normalized)
                if score >= _QUART_THRESHOLD:
                    cluster.append(q)
                    _min_score_q = min(_min_score_q, float(score))

            if len(cluster) >= 2:
                if _has_perunit_mismatch(cluster, latest_prices):
                    continue
                confidence = min(_min_score_q / 100.0, _QUART_CONF_CAP)
                persisted = _persist_match(session, cluster, confidence)
                if persisted:
                    created_or_updated += persisted
                    visited.update(c.id for c in cluster)

    session.commit()
    log.info("matcher_done", clusters=created_or_updated)
    try:
        from src.observability import metrics

        metrics.matcher_clusters_total.inc(created_or_updated)
    except Exception:
        pass
    return created_or_updated


def _persist_match(session: Session, cluster: Sequence[Product], confidence: float = 1.0) -> int:
    """Создать или обновить Match для кластера товаров.

    Snowball-guard (фикс 2026-05-25):
    При добавлении новых продуктов к существующему матчу проверяем,
    что для каждого нового продукта его сайт ещё не представлен в матче.
    Без этой проверки кластер рос за несколько прогонов: aloe-продукт
    с canonical_id=X «заражал» следующую пару (aptk+pharm), те получали X,
    в следующем прогоне та пара заражала ещё одну — кластер раздувался
    до 70+ разных вариантов одного бренда.
    """
    from src.product_policy import (
        OFFER_OUT_OF_STOCK,
        country_code_of,
        country_policy_enforced,
        current_offer_eligibility,
        availability_policy_enforced,
    )

    tenant_ids = {int(getattr(product, "tenant_id", 1)) for product in cluster}
    if len(tenant_ids) != 1:
        log.error(
            "persist_match_mixed_tenant_rejected",
            product_ids=[product.id for product in cluster],
            tenant_ids=sorted(tenant_ids),
        )
        return 0
    tenant_id = next(iter(tenant_ids))

    # Central invariant: every producer (barcode, fuzzy, recall) ends here.
    # Compare candidates with existing members before any canonical_id mutation.
    cohort = list(cluster)
    existing_match_ids = {p.canonical_id for p in cohort if p.canonical_id}
    for existing_match_id in existing_match_ids:
        existing_match = session.get(Match, existing_match_id)
        if existing_match:
            if existing_match.tenant_id != tenant_id:
                log.error(
                    "persist_match_cross_tenant_rejected",
                    match_id=existing_match.id,
                    match_tenant_id=existing_match.tenant_id,
                    product_tenant_id=tenant_id,
                )
                return 0
            cohort.extend(
                p
                for p in existing_match.products
                if p not in cohort and p.tenant_id == tenant_id
            )
    for idx, left in enumerate(cohort):
        if getattr(left, "offer_availability_status", None) == OFFER_OUT_OF_STOCK:
            return 0
        if availability_policy_enforced() and not current_offer_eligibility(left).eligible:
            return 0
        if country_policy_enforced() and country_code_of(left) is None:
            return 0
        for right in cohort[idx + 1 :]:
            if left.site != right.site and _has_conflicting_pack_count(left, right):
                log.info(
                    "persist_match_pack_count_conflict",
                    left_id=left.id,
                    right_id=right.id,
                    left_pack_size=left.pack_size,
                    right_pack_size=right.pack_size,
                )
                return 0
            if left.site != right.site and _has_conflicting_country(left, right):
                log.info(
                    "persist_match_country_conflict",
                    left_id=left.id,
                    right_id=right.id,
                    left_country=country_code_of(left),
                    right_country=country_code_of(right),
                )
                return 0

    # Если у кого-то уже есть canonical_id — переиспользуем (если не is_manual)
    existing_ids = existing_match_ids
    if existing_ids:
        match_id = next(iter(existing_ids))
        match = session.get(Match, match_id)
        if match and match.is_manual:
            return 0  # ручной match — не трогаем
        # Snowball-guard: не добавляем продукт если его сайт уже есть в матче
        if match:
            occupied_sites = {p.site for p in match.products if p.canonical_id == match_id}
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
            tenant_id=tenant_id,
            canonical_name=first.name,
            canonical_brand=first.brand,
            canonical_dosage=first.dosage,
            canonical_pack_size=first.pack_size,
            confidence=confidence,
            is_manual=False,
        )
        session.add(match)
        session.flush()
    else:
        # Обновляем confidence при каждом пересчёте
        match.confidence = confidence

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
    products = session.scalars(select(Product).where(Product.canonical_id.is_(None))).all()
    by_site: dict[str, list[Product]] = defaultdict(list)
    for p in products:
        by_site[p.site].append(p)
    return dict(by_site)


_PRICE_FLAG_RATIO = 1.50  # расхождение ≥50% → нужна ручная проверка


def flag_suspected_mismatches(session: Session) -> int:
    """Флагирует авто-матчи с подозрительным расхождением цен (needs_review=True).

    Только информационный флаг для UI — ничего не удаляет и не блокирует.
    Порог: max(цена) / min(цена) ≥ 1.50 (50% разница).
    Флаг сбрасывается автоматически если цены выровнялись.

    Возвращает количество изменённых флагов.
    """
    matches = session.scalars(select(Match).where(Match.is_manual.is_(False))).all()

    # Один pre-fetch для всех продуктов — не N+1
    all_pids = [p.id for m in matches for p in m.products]
    if not all_pids:
        return 0
    prices = latest_snapshots_per_product(session, all_pids)

    changed = 0
    for m in matches:
        prods = m.products
        if not prods or len(prods) < 2:
            continue
        vals = [
            prices[p.id].price
            for p in prods
            if p.id in prices and prices[p.id] and prices[p.id].price and prices[p.id].price > 0
        ]
        if len(vals) < 2:
            continue
        ratio = max(vals) / min(vals)
        should_flag = ratio >= _PRICE_FLAG_RATIO
        if m.needs_review != should_flag:
            m.needs_review = should_flag
            changed += 1

    if changed:
        session.commit()
        log.info(
            "flag_mismatches_done",
            changed=changed,
            flagged=sum(1 for m in matches if m.needs_review),
        )
    return changed


def normalize_for_matching(name: str) -> str:
    """Backwards-compat alias."""
    return normalize_name(name)
