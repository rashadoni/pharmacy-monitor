"""Шаред-нормализация имён товаров и единиц.

Нужна и скрейперам (для записи `name_normalized`), и matcher.py (для сравнения).
"""

from __future__ import annotations

import re
import unicodedata

# Распространённые формы лекарств — выкидываем при нормализации,
# но запоминаем отдельно через extract_form().
_FORMS = (
    "tablet",
    "tab",
    "tabletka",
    "kapsul",
    "capsule",
    "kaps",
    "siropla",
    "syrup",
    "şərbət",
    "sirop",
    "məhlul",
    "solution",
    "raztvor",
    "krem",
    "cream",
    "məlhəm",
    "ointment",
    "maz",
    "spreyi",
    "spray",
    "sprey",
    "ampul",
    "ampoule",
    "amp",
    "drops",
    "damci",
    "damcı",  # dotless-i (U+0131)
    "damcısı",  # dotless-i variant with suffix
    "kapli",
    "gel",
    "powder",
    "poroshok",
    "toz",
    "suppoziter",
    "suppository",
    "svecha",
    "şam",
    "eye drops",
    "göz damcisi",
    # Azerbaijani plural / inflected forms (pharmonline adds these in parens)
    "tabletlər",  # plural of "tablet" in AZ
    "tabletkalar",
    "kapsullar",  # plural of "kapsul" in AZ
    "kapsulalar",
    "kapsulları",
    "ampulalar",
    "ampullar",
    "ampulları",
    "damlalar",
    "damcılar",
    "suppozitorlar",
    "suppozitorları",
    "şamlar",
    "dragee",
    "draje",
    "ampoules",  # English plural (seen in aloe/pharmonline)
    "tablets",
    "capsules",
    "suppositories",  # English plural — pharmonline: «(Suppositories)»
    "sorma",  # Azerbaijani "dissolving" — not a form per se but pharmonline uses it
    "yağı",  # масло (oil) — тип препарата; oil ≠ solution/cream (см. _FORM_CANONICAL).
    "yağ",  # «Qliserin yağı»(oil) ≠ «Qliserin məhlul»(solution) — раньше не различались.
)

_FORMS_RE = re.compile(r"\b(" + "|".join(_FORMS) + r")\b", re.IGNORECASE)

# «çay ağacı» (чайное дерево, Melaleuca) и «şam ağacı» (сосна) — РАСТЕНИЯ, а не
# категория-«чай» / форма-«суппозиторий». Склеиваем биграмму ДО стрипа форм и
# category-префиксов, иначе «çay»/«şam» вырезаются → «çay ağacı yağı» и «şam ağacı
# yağı» оба схлопываются в «agaci yagi» → ложный матч чайное-дерево ↔ сосна.
_TREE_PLANT_RE = re.compile(r"\b(çay|şam)\s+(ağac)", re.IGNORECASE)

# Канонические группы форм выпуска: синонимы → одно имя.
# Кремы ≠ мази ≠ капли ≠ спреи — это РАЗНЫЕ препараты, матчинг запрещён.
_FORM_CANONICAL: dict[str, str] = {
    "tablet": "tablet",
    "tab": "tablet",
    "tabletka": "tablet",
    "kapsul": "capsule",
    "capsule": "capsule",
    "kaps": "capsule",
    "krem": "cream",
    "cream": "cream",
    "məlhəm": "ointment",
    "ointment": "ointment",
    "maz": "ointment",
    "spreyi": "spray",
    "spray": "spray",
    "sprey": "spray",
    "drops": "drops",
    "damci": "drops",
    "kapli": "drops",
    "damcı": "drops",
    "damcısı": "drops",  # dotless-i (U+0131) варианты
    "eye drops": "drops",
    "göz damcisi": "drops",
    "göz damcısı": "drops",
    "məhlul": "solution",
    "solution": "solution",
    "raztvor": "solution",
    "gel": "gel",
    "powder": "powder",
    "poroshok": "powder",
    "toz": "powder",
    "suppoziter": "suppository",
    "suppository": "suppository",
    "suppositories": "suppository",
    "svecha": "suppository",
    "şam": "suppository",
    "şamlar": "suppository",
    "suppozitorlar": "suppository",
    "suppozitorları": "suppository",
    "siropla": "syrup",
    "syrup": "syrup",
    "şərbət": "syrup",
    "sirop": "syrup",
    "ampul": "ampoule",
    "ampoule": "ampoule",
    "amp": "ampoule",
    # Масло (oil) — отдельная группа форм: «Qliserin yağı»(oil) ≠ «Qliserin
    # məhlul»(solution), «Çay ağacı yağı»(oil) ≠ свечи. Масло↔масло (Çaytikanı
    # yağı обоих сайтов) остаётся одной формой → матч сохраняется.
    "yağı": "oil",
    "yağ": "oil",
    "yag": "oil",
    "oil": "oil",
    # AZ-плюралы / EN-плюралы / синонимы (2026-05-29): раньше отсутствовали в
    # каноне → extract_form возвращал сам токен → ложный form-конфликт против
    # канона (напр. aptek «sorma tabletlər»→"sorma" vs pharm «tabletlər»→"tabletlər"
    # → блок, хотя это ОДИН товар — сублингвальные таблетки = таблетки). Группы
    # форм остаются раздельными (cream≠ointment, drops≠spray) — мёржим лишь
    # синонимы внутри одной группы.
    "tabletlər": "tablet",
    "tabletkalar": "tablet",
    "tablets": "tablet",
    "dragee": "tablet",  # драже = таблетка в оболочке
    "draje": "tablet",
    "sorma": "tablet",  # «sorma tabletlər» = сублингвальные/рассасывающие таблетки
    "kapsullar": "capsule",
    "kapsulalar": "capsule",
    "kapsulları": "capsule",
    "capsules": "capsule",
    "ampulalar": "ampoule",
    "ampullar": "ampoule",
    "ampulları": "ampoule",
    "ampoules": "ampoule",
    "damlalar": "drops",
    "damcılar": "drops",
}


def extract_form(name: str) -> str | None:
    """Извлечь нормализованную форму выпуска из имени товара.

    Работает на исходном (ненормализованном) имени — форма убирается
    в normalize_name(), поэтому нужно вызывать ДО нормализации.

    Возвращает каноническую группу: 'tablet', 'capsule', 'cream',
    'ointment', 'spray', 'drops', 'solution', 'gel', 'powder',
    'suppository', 'syrup', 'ampoule'. None если форма не распознана.
    """
    if not name:
        return None
    m = _FORMS_RE.search(name)
    if not m:
        return None
    token = m.group(1).lower()
    return _FORM_CANONICAL.get(token, token)


# Числа с пробелами-разделителями тысяч: «1 000 000 BV», «500 000 BV».
# _DOSAGE_RE ожидает непрерывную цифровую строку, поэтому перед применением
# нормализуем «1 000 000» → «1000000».
# Правило: последовательность 1–3 цифры, затем одна или более групп " \d{3}" —
# именно так выглядит тысячный разделитель (пробел + ровно три цифры).
# Примеры: "1 000 000 BV" → "1000000 BV", "500 000 BV" → "500000 BV".
# НЕ затрагивает "N 10" (там группа не 3-значная) или "30 tab" (не цифровая).
_SPACED_THOUSANDS_RE = re.compile(r"\b(\d{1,3})(?:\s(\d{3}))+\b")


def _collapse_spaced_thousands(s: str) -> str:
    """«1 000 000» → «1000000», «500 000» → «500000»."""
    return _SPACED_THOUSANDS_RE.sub(lambda m: m.group(0).replace(" ", ""), s)


# Дозировка: 500 mg, 10 ml, 100 mcg, 1.5 g, 250mg/5ml, etc.
_DOSAGE_RE = re.compile(
    # Единицы дозировки расширены:
    #   ed   = Einheit (нем.) = IU — aptekonline URL-слаги (3500-ED)
    #   ie   = Internationale Einheit (нем.) = IU
    #   tv   = тысяч единиц (aptekonline: «10000 TV»)
    #   bv   = биологических единиц (pharmonline: «10000 BV»)
    #   u    = units (краткая форма: «3500 U»)
    #   units = полная форма
    #   tis  = тысяч единиц (рос.)
    r"\b(\d+(?:[.,]\d+)?\s*(?:mg|mkg|mcg|µg|g|ml|mq|qr|qrm|q|iu|ie|ed|me|tv|bv|u|units|tis|%)(?:\s*/\s*\d+(?:[.,]\d+)?\s*(?:mg|mkg|mcg|µg|g|ml|mq|qr|q|iu|ie|ed|me|tv|bv|u|units|tis|%))?)\b",
    re.IGNORECASE,
)

# Размер упаковки. Двухэтапно (фикс 2026-05-11): сначала ищем «КОЛИЧЕСТВО
# в упаковке» (N66, 30 tab, 54 əd) — это specifically отличает разные SKU
# одного бренда. Только если не нашли — фолбэк на объём/вес (100ml, 400q).
#
# Старый _PACK_RE брал первое совпадение слева → для «Huggies-4 8-14kg N66»
# возвращал «14kg» (вес РЕБЁНКА!), а N66 терялся → разные упаковки сливались
# в один false match (match_id=447, 0.20 AZN ↔ 28.90 AZN).
_PACK_COUNT_RE = re.compile(
    r"\bN(?:o\.?)?\s*(\d+)"  # N20, N 20, No 28, No. 28, No.28 (ASCII N / English «No.»)
    r"|№\s*(\d+)"  # №20 (Unicode № — \b не работает с non-word)
    r"|(\d+)\s*(?:tab|tabletka|kapsul|capsules|kaps|şt|шт|adet|amp|pieces|əd)",  # 30 tab, 54 əd
    re.IGNORECASE,
)
# Vol unit list: ml, kg, kq, qr, qrm, q, gr, g. Используем lookahead вместо \b
# чтобы matchать "200gr" (g + r — оба word-char, обычный \b не пройдёт).
_PACK_VOLUME_RE = re.compile(
    r"\b(\d+\s*(?:ml|kg|kq|qrm|qr|gr|q|g))(?=\b|[^a-zA-Z])",
    re.IGNORECASE,
)
# Концентрация вида «200mq/5ml», «0.5mg/ml», «10%/5ml» — знаменатель «Yml» НЕ
# объём упаковки. Снимаем его ПЕРЕД _PACK_VOLUME_RE, иначе «Azoksin 200mq/5ml
# 15ml» даёт pack=«5ml» (концентрация) вместо 15ml (флакон) → разные по объёму
# флаконы (15ml vs 30ml) сливаются в один bucket. (workflow audit 2026-05-31.)
_CONCENTRATION_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:mg|mkg|mcg|µg|g|mq|qr|q|iu|ie|ed|me|tv|bv|u|%)"
    r"\s*/\s*\d*(?:[.,]\d+)?\s*(?:ml|mg|g|q|qr)",
    re.IGNORECASE,
)
# Backward-compat: оставляем _PACK_RE для других мест (normalize_name использует
# его для удаления pack-токенов из имени).
_PACK_RE = re.compile(
    r"\b(\d+\s*(?:tab|tabletka|kapsul|capsules|kaps|şt|шт|adet|amp|pieces|əd|n\d+)"
    r"|n\s*\d+|no\s*\d+|№\s*\d+|\d+\s*(?:ml|kg|kq|qr|qrm|q|g))\b",
    # no\s*\d+ убирает «No 20» — стейл-артефакт от «№ 20» после strip_punct
    re.IGNORECASE,
)

_PUNCT_RE = re.compile(r"[^\w\s%/.\-]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Страны происхождения — pharmonline добавляет в конце имени: «(Türkiyə)».
# После strip_accents + lower + PUNCT_RE они становятся отдельными токенами в
# name_normalized («turkiyə», «rusiya» и т.п.) и мешают fuzzy-match.
# Список покрывает все страны, встречающиеся в prod-данных.
_COUNTRY_RE = re.compile(
    r"\b(?:"
    r"turkiyə|türkiyə|turkiye|turkey|"
    r"rusiya|russia|"
    r"almaniya|germany|"
    r"italiya|italy|"
    r"polsa|polşa|poland|"
    r"ukrayna|ukraine|"
    r"cin|çin|china|"
    r"avstriya|austria|"
    r"sloveniya|slovakia|slovakiya|slovakia|"
    r"rumıniya|rumaniya|romania|"
    r"ispaniya|spain|"
    r"fransa|france|"
    r"ingiltərə|england|uk|"
    r"belcika|belgium|"
    r"niderlandlar|netherlands|"
    r"danimarka|denmark|"
    r"isvec|sweden|"
    r"norvec|norway|"
    r"finlandiya|finland|"
    r"isveçrə|switzerland|"
    r"hindistan|india|"
    r"yaponiya|japan|"
    r"koreya|korea|"
    r"bolqarıstan|bulgaria|"
    r"cexiya|czech|"
    r"yunanistan|greece|"
    r"macaristan|hungary|"
    r"azerbaycan|azərbaycan|"
    r"belarus|belarusiya|"
    r"qazaxstan|kazakhstan|"
    r"oezbekistan|uzbekistan|"
    r"iordaniya|jordan|"
    r"misir|egypt|"
    r"pakistan"
    r")\b",
    re.IGNORECASE,
)

# Маршруты введения — не форма выпуска, но остаются в name_normalized и мешают
# fuzzy-матчингу. «rektal» — самый частый в AZ/RU фармацевтике (свечи-суппозитории).
# НЕ включаем «oral», «nasal» — встречаются в брендах (Oral-B, Nasal Spray как
# товарное название). Применяем ДО strip_accents: буквы ASCII, re.IGNORECASE хватит.
_ROUTE_RE = re.compile(
    r"\b(?:rektal|intranazal|intranazale|vaginal|sublingual)\b",
    re.IGNORECASE,
)

# Категория-префиксы (тип товара, не имя продукта). Их нужно убрать
# из name_normalized чтобы fuzzy match не считал «Südlü qarışıq» общим
# токеном для разных продуктов.
_CATEGORY_PREFIXES = sorted(
    [
        # Baby food
        "südlü qarışıq",
        "südlü qarisiq",
        "sudlu qarısıq",
        "sudlu qarisiq",
        "südsüz sıyıq",
        "südsüz siyiq",
        "sudsuz sıyıq",
        "sudsuz siyiq",
        "südlü sıyıq",
        "südlü siyiq",
        "sudlu sıyıq",
        "sudlu siyiq",
        "uşaq qidası",
        "uşaq qidasi",
        "usaq qidası",
        "usaq qidasi",
        "uşaq peçenyesi",
        "uşaq peçenye",
        "usaq pecenye",
        "uşaq yeməyi",
        "usaq yeməyi",
        "peçenye",
        "pecenye",
        "püre",
        "pure",
        "su",
        "çay",
        "cay",
        "süd",
        "sud",
        # Лек. формы как префикс — обычно «Tablet 'Brand' ...»
        "tableti",
        "tabletkalar",
        "tabletka",
        "tablet",
        "kapsulalar",
        "kapsulları",
        "kapsulalar",
        "ampullar",
        "ampulalar",
        "ampulları",
        "ampul",
        "şərbət",
        "şərbeti",
        "serbet",
        "şərbeti",
        "məhlul",
        "mehlul",
        "mehlulu",
        "krem",
        "kremi",
        "məlhəm",
        "məlhəmi",
        "melhem",
        "gel",
        "geli",
        "sirop",
        "sirup",
        "suppozitorlar",
        "suppozitorları",
        "suppozitor",
        "drops",
        "damla",
        "damlalar",
        "damcı",
        "damcısı",
        # Косметика / гигиена
        "şampun",
        "şampunu",
        "sampun",
        "balzam",
        "balzamı",
        "sabun",
        "sabunu",
        "dezodorant",
        "dezodorantı",
        "losyon",
        "losyonu",
        "tonik",
        "tonikı",
        "maska",
        "maskası",
        # Универсальные
        "dərman",
        "dərmani",
        "derman",
        "vasitəsi",
        "vasitesi",
    ],
    key=len,
    reverse=True,
)
_PREFIX_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(p) for p in _CATEGORY_PREFIXES) + r")\b\s*",
    re.IGNORECASE,
)

# Унификация написаний одного бренда (frisolak == frisolac etc.)
_BRAND_ALIASES_IN_NAME = [
    (re.compile(r"\bfrisolak\b", re.IGNORECASE), "frisolac"),
    (re.compile(r"\bsimilak\b", re.IGNORECASE), "similac"),
    (re.compile(r"\b(?:malutka|malyuk|maluk)\b", re.IGNORECASE), "malyutka"),
    (re.compile(r"\b(?:nenny|nenni)\b", re.IGNORECASE), "nanny"),
    (re.compile(r"\bbellak\b", re.IGNORECASE), "bellakt"),
]


def _strip_category_prefix(s: str) -> str:
    """Убрать category-префиксы из начала name_normalized (повторно — на случай вложенных)."""
    while True:
        new = _PREFIX_RE.sub("", s)
        if new == s:
            return s
        s = new


def _unify_brand_spelling(s: str) -> str:
    for pat, canon in _BRAND_ALIASES_IN_NAME:
        s = pat.sub(canon, s)
    return s


def _normalize_units(unit_str: str | None) -> str | None:
    """qr→q, kq→kg, gr→g на извлечённой dosage/pack строке. Возвращает None если пусто."""
    if not unit_str:
        return None
    s = unit_str.lower().strip()
    s = re.sub(r"(\d)\s*qr\b", r"\1q", s)
    s = re.sub(r"(\d)\s*kq\b", r"\1kg", s)
    s = re.sub(r"(\d)\s*gr\b", r"\1g", s)
    return s or None


# Азербайджанские буквы, которые NFKD НЕ декомпозирует (это самостоятельные
# буквы, а не accented-варианты). Без явной карты ə/ı оставались в
# name_normalized → «şərbət»→«sərbət» (а не «serbet»), и cross-site матч между
# сайтом с «ə» и сайтом с «e» проваливался. Bug fix 2026-05-29.
# Регистр сохраняем (нижний→нижний, верхний→верхний): strip_accents не должен
# менять case — это делает .lower() у вызывающего при необходимости.
_AZ_CHAR_MAP = str.maketrans(
    {
        "ə": "e",
        "Ə": "E",
        "ı": "i",
        "İ": "I",
        "ş": "s",  # дублируем NFKD-покрытие для надёжности
        "Ş": "S",
        "ç": "c",
        "Ç": "C",
        "ğ": "g",
        "Ğ": "G",
        "ö": "o",
        "Ö": "O",
        "ü": "u",
        "Ü": "U",
    }
)


def strip_accents(text: str) -> str:
    """ə→e, ı→i, ü→u, ş→s и т.п. Helps cross-language matching (AZ ↔ RU translit).

    Двухступенчато: (1) явная AZ-карта для букв, которые NFKD не разбирает
    (ə, ı — самостоятельные буквы), (2) NFKD + удаление combining-марок для
    остальных диакритик (é, ñ, …).
    """
    text = text.translate(_AZ_CHAR_MAP)
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_name(name: str) -> str:
    """Канонизация имени товара для матчинга и поиска.

    Pipeline:
    1. strip_accents (ə→e, ş→s)
    2. lower-case
    3. убираем формы лекарств (tab, kapsul, tabletlər, kapsulalar, …)
    4. убираем dosage и pack-size (числа+единицы) — они хранятся отдельно
    5. убираем страны происхождения (Türkiyə, Rusiya, Germany, …)
    6. убираем category-префиксы («Südlü qarışıq», «Uşaq qidası» — это тип, не имя)
    7. унифицируем написание бренда (frisolak→frisolac etc.)
    8. punctuation→space, collapse whitespace
    9. убираем одиночные незначащие токены (напр. "." из "30 əd.")
    """
    if not name:
        return ""
    s = name.strip()
    # Схлопываем пробелы-разделители тысяч: «1 000 000 BV» → «1000000 BV»
    # ДО strip_accents/lower, чтобы _DOSAGE_RE корректно съел весь диапазон.
    s = _collapse_spaced_thousands(s)
    # Защита «çay/şam ağacı» (растения) ДО стрипа форм/префиксов — см. _TREE_PLANT_RE.
    s = _TREE_PLANT_RE.sub(r"\1\2", s)  # «çay ağacı»→«çayağac…», «şam ağacı»→«şamağac…»
    # Формы выпуска убираем ДО strip_accents — паттерны в _FORMS содержат
    # оригинальные ş/ə/ı (шамлар, дамджы…). После strip_accents ş→s, и тогда
    # «şamlar» не совпадает с паттерном «şamlar» — форма остаётся в имени,
    # снижая точность fuzzy-матча (было: «ukraferon suppositories» не матчилось
    # с «ukraferon rektal samlar»). re.IGNORECASE покрывает регистр.
    s = _FORMS_RE.sub(" ", s)
    # Маршруты введения (rektal, intranazal…) — ASCII, убираем сразу после форм.
    s = _ROUTE_RE.sub(" ", s)
    s = strip_accents(s)
    s = s.lower()
    s = _DOSAGE_RE.sub(" ", s)
    s = _PACK_RE.sub(" ", s)
    s = _COUNTRY_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    s = _strip_category_prefix(s)
    s = _unify_brand_spelling(s)
    # Убираем одиночные незначащие токены: "." из "30 əd.", "-" и т.п.
    # Возникают после stripping pack-суффиксов типа "20 əd." → "."
    s = " ".join(t for t in s.split() if len(t) > 1 or t.isalnum())
    s = _WS_RE.sub(" ", s).strip()
    return s


def extract_dosage(name: str) -> str | None:
    """Вытащить дозировку (500mg, 10ml, 250mg/5ml). qr→q нормализуется.

    Применяет strip_accents к результату: «İU» (турецкий I с точкой, U+0130)
    после .lower() даёт «i̇u» (с combining dot) вместо ASCII «iu».
    strip_accents убирает combining chars → «iu». Без этого такие продукты
    попадают в другой bucket_key и не матчатся с IU/BV/ME продуктами.
    """
    if not name:
        return None
    name = _collapse_spaced_thousands(name)  # «1 000 000 BV» → «1000000 BV»
    m = _DOSAGE_RE.search(name)
    if not m:
        return None
    raw = re.sub(r"\s+", "", m.group(1).lower()).replace(",", ".")
    raw = strip_accents(raw)  # İU → iu, Ü → u (убирает combining chars)
    return _normalize_units(raw)


def extract_pack_size(name: str) -> str | None:
    """Вытащить размер упаковки. qr→q нормализуется.

    Приоритет (фикс 2026-05-11):
    1. КОЛИЧЕСТВО в упаковке: «N66», «№ 20», «30 tab», «54 əd» —
       это разделяет SKU одного бренда (Huggies-4 N54 vs N66).
    2. Объём/вес (фолбэк): «100ml», «400q», «8-14kg» —
       только если pack-count не найден.

    Раньше единая regex брала левое первое совпадение, поэтому
    для diapers «Huggies-4 8-14kg N66» получали «14kg» (= вес ребёнка!)
    вместо «N66» (= кол-во в упаковке) → false matches.
    """
    if not name:
        return None
    # Этап 1: пытаемся достать count
    m = _PACK_COUNT_RE.search(name)
    if m:
        count = next((g for g in m.groups() if g), None)
        if count:
            return f"n{count}"
    # Этап 2: фолбэк на vol/weight — но СНАЧАЛА снимаем концентрацию «X/Yml»,
    # чтобы знаменатель не приняли за объём упаковки (Azoksin 200mq/5ml 15ml → 15ml).
    stripped = _CONCENTRATION_RE.sub(" ", name)
    m = _PACK_VOLUME_RE.search(stripped)
    if m:
        raw = re.sub(r"\s+", "", m.group(1).lower())
        return _normalize_units(raw)
    return None


def extract_total_volume(name: str | None) -> str | None:
    """Объём/вес УПАКОВКИ (ml/g/kg…), с снятым знаменателем концентрации «X/Yml».

    Отличается от extract_pack_size: всегда про объём (не count), для matcher-guard'а
    _has_conflicting_pack_volume — чтобы 15ml-флакон не слился с 30ml даже когда у
    обоих одинаковый count (N1) и concentration-base (5ml). None если объёма нет."""
    if not name:
        return None
    stripped = _CONCENTRATION_RE.sub(" ", _collapse_spaced_thousands(name))
    m = _PACK_VOLUME_RE.search(stripped)
    if not m:
        return None
    return _normalize_units(re.sub(r"\s+", "", m.group(1).lower()))


_PRICE_TOKEN_RE = re.compile(r"\d[\d,.]*")


def parse_price(text: str | None) -> float | None:
    """'24,50 AZN' → 24.50. Возвращает None если не парсится.

    Robust to multi-price concatenation (e.g. ``<del>22 AZN</del>317 AZN``
    rendered without a separator, common in aptekonline's Angular template
    when ``ng-binding`` interpolates two raw numbers). We extract the FIRST
    digit-run-with-dots/commas and stop at any other character — preventing
    "22 AZN 317 AZN" from being parsed as 22317.
    """
    if not text:
        return None
    m = _PRICE_TOKEN_RE.search(str(text))
    if not m:
        return None
    cleaned = m.group(0).replace(",", ".")
    if cleaned.count(".") > 1:
        # 1.234.56 → 1234.56 (точка — разделитель тысяч)
        parts = cleaned.split(".")
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


# ── Pack-unit count для per-unit price comparison (2026-05-29) ───────────────
# Извлекает КОЛИЧЕСТВО штук в упаковке (N10, №10, 10 ədəd, 10x2), НЕ объём/вес.
# Используется comparison endpoint'ом для нормализации цены за штуку: один товар
# в разной фасовке (маска поштучно 0.20 vs пачка 50 за 10.00) не должен давать
# ложный 98% spread. Возвращает (count, confidence): confidence='high' если
# найден явный count-маркер, 'low' если маркера нет (по умолчанию 1 штука).
#
# КРИТИЧНО: исключаем числа с единицами объёма/веса/дозы (ml/g/q/mg/iu…), иначе
# «50 ml» распарсится как count=50. Дизайн подтверждён Perplexity + Codex.
_UNIT_SUFFIX = r"(?:mm|cm|sm|ml|m2|m|l|mg|mcg|mkg|µg|g|gr|q|qr|kg|kq|%|iu|bv|me|ie|мл|мг|г|кг)"
_PACK_MULT_RE = re.compile(r"\b(?:n|no)?\s*(\d{1,4})\s*[x×]\s*(\d{1,4})\b", re.IGNORECASE)
_PACK_N_RE = re.compile(r"\b(?:n|no)\s*(\d{1,4})\b", re.IGNORECASE)
_PACK_WORD_RE = re.compile(
    r"\b(\d{1,4})\s*(?:ədəd|eded|əd\b|şt|adet|pcs?|pieces?|штук|шт)\b",
    re.IGNORECASE,
)


def _followed_by_unit(text: str, end: int) -> bool:
    """True если сразу после позиции end идёт единица объёма/веса (→ это не count)."""
    return re.match(r"\s*" + _UNIT_SUFFIX + r"\b", text[end : end + 8], re.IGNORECASE) is not None


def pack_unit_count(pack_size: str | None, name: str | None = None) -> tuple[int, str]:
    """Количество штук в упаковке + уверенность ('high'|'low').

    Ищет count-маркеры (N10, №10, 10 ədəd, 10x2) в pack_size, затем в name.
    Игнорирует объём/вес («50 ml» → не count). Без маркера → (1, 'low').
    """
    for src in (pack_size or "", name or ""):
        if not src:
            continue
        s = src.lower().replace("№", "n").replace("nº", "n").replace("no.", "no")

        m = _PACK_MULT_RE.search(s)
        if m and not _followed_by_unit(s, m.end()):
            return int(m.group(1)) * int(m.group(2)), "high"

        m = _PACK_N_RE.search(s)
        if m and not _followed_by_unit(s, m.end()):
            return int(m.group(1)), "high"

        m = _PACK_WORD_RE.search(s)
        if m:
            return int(m.group(1)), "high"

    return 1, "low"
