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
from src.normalize import extract_form, normalize_name, strip_accents
from src.storage import Match, PriceSnapshot, Product, latest_snapshots_per_product

log = structlog.get_logger()

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
        # Берём по одному продукту с каждого уникального сайта (если на одном
        # сайте несколько продуктов с тем же barcode — это нормально для variants
        # одного товара, но мы матчим cross-site).
        seen_sites: set[str] = set()
        cluster: list[Product] = []
        for p in sorted(group, key=lambda x: x.id):  # deterministic order
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
        created_or_updated += _persist_match(session, cluster, confidence=1.0)
        visited.update(c.id for c in cluster)
        barcode_matches_created += 1

    log.info(
        "matcher_barcode_pass",
        unique_barcodes=len(by_barcode),
        clusters_created=barcode_matches_created,
    )

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
                if _has_extreme_length_disparity(p.name_normalized or "", q.name_normalized or ""):
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
                score = fuzz.token_set_ratio(p.name_normalized, q.name_normalized)
                if score >= fuzzy_threshold:
                    cluster.append(q)
                    _min_score = min(_min_score, float(score))

            if len(cluster) >= 2:
                # Проверка: не смешиваем цену-за-штуку с ценой-за-упаковку
                if _has_perunit_mismatch(cluster, latest_prices):
                    continue
                confidence = _min_score / 100.0
                created_or_updated += _persist_match(session, cluster, confidence)
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
                if _has_extreme_length_disparity(p.name_normalized or "", q.name_normalized or ""):
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
                score = fuzz.token_set_ratio(p.name_normalized, q.name_normalized)
                if score >= _SEC_THRESHOLD:
                    cluster.append(q)
                    _min_score_sec = min(_min_score_sec, float(score))

            if len(cluster) >= 2:
                if _has_perunit_mismatch(cluster, latest_prices):
                    continue
                confidence = min(_min_score_sec / 100.0, _SEC_CONF_CAP)
                created_or_updated += _persist_match(session, cluster, confidence)
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
                if _has_extreme_length_disparity(p.name_normalized or "", q.name_normalized or ""):
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
                score = fuzz.token_set_ratio(p.name_normalized, q.name_normalized)
                if score >= _TERT_THRESHOLD:
                    cluster.append(q)
                    _min_score_tert = min(_min_score_tert, float(score))

            if len(cluster) >= 2:
                if _has_perunit_mismatch(cluster, latest_prices):
                    continue
                confidence = min(_min_score_tert / 100.0, _TERT_CONF_CAP)
                created_or_updated += _persist_match(session, cluster, confidence)
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
                if _has_extreme_length_disparity(p.name_normalized or "", q.name_normalized or ""):
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
                score = fuzz.token_set_ratio(p.name_normalized, q.name_normalized)
                if score >= _QUART_THRESHOLD:
                    cluster.append(q)
                    _min_score_q = min(_min_score_q, float(score))

            if len(cluster) >= 2:
                if _has_perunit_mismatch(cluster, latest_prices):
                    continue
                confidence = min(_min_score_q / 100.0, _QUART_CONF_CAP)
                created_or_updated += _persist_match(session, cluster, confidence)
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
    # Если у кого-то уже есть canonical_id — переиспользуем (если не is_manual)
    existing_ids = {p.canonical_id for p in cluster if p.canonical_id}
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
