"""Каталог известных брендов в категории baby food / детские смеси / молочные продукты.

Используется в `extract_brand()` (normalize.py) и в скрейперах pharmonline/aptekonline,
которые не могут вытащить brand из вёрстки сайтов (поля просто нет в карточке).

Структура:
    BRANDS: dict[canonical_name, list[alias_lowercase]]

Каноническое имя — то, что попадёт в БД (`products.brand`). Алиасы — все встреченные
варианты в реальных названиях (включая кирилличные и азербайджанские транслитерации).

Правило сопоставления: alias matches as a whole word (re.escape + word boundaries).
"""

from __future__ import annotations

import re
from functools import lru_cache

from src.normalize import strip_accents

BRANDS: dict[str, list[str]] = {
    # Baby formula (молочные смеси)
    "Friso": ["friso", "friso pep", "friso gold"],
    "Frisolac": ["frisolac", "frisolak"],
    "Vinni": ["vinni", "винни"],
    "Humana": ["humana"],
    "Nanny": ["nanny", "нэнни"],
    "Bebevit": ["bebevit"],
    "Sahha": ["sahha"],
    "Gipopo": ["gipopo"],
    "Kogda Ya Vyrastu": ["когда я вырасту", "kогда я вырасту", "kogda ya vyrastu"],
    "Bibikol": ["бибиколь", "bibikol"],
    "Aqusha": ["aquşa", "aqusha"],
    "Dlya Lyalya": ["для ляль", "для ляли", "dlya lyalya"],
    "Aptamil": ["aptamil"],
    "Nutrilon": ["nutrilon"],
    "Nutrilak": ["nutrilak"],
    "Nutriben": ["nutriben"],
    "Malyutka": ["malyutka", "malutka", "малютка", "malyuk", "maluk"],
    # Nestle umbrella: NAN, Nestogen, Gerber, Cicibebe — все продуктовые линейки Nestle.
    # Объединяем чтобы matcher грузил их в один bucket по brand.
    # Gerber оставляем отдельно (узнаваемый standalone бренд).
    "Nestle": ["nestle", "нестле", "nan", "нан", "nestogen", "şaqayka", "shaqayka"],
    "Kabrita": ["kabrita", "кабрита"],
    "Similac": ["similac", "similak", "симилак"],
    "Bellakt": ["bellakt", "беллакт"],
    "Hipp": ["hipp", "хипп"],
    "Mamako": ["mamako", "мамако"],
    "Kendamil": ["kendamil"],
    "Hero Baby": ["hero baby"],
    "PediaSure": ["pediasure"],
    "Danalac": ["danalac"],
    "Bebi": ["bebi"],
    "Heinz": ["heinz", "хайнц"],
    "Arilac": ["arilac"],
    "Frutonyanya": ["fruto nyanya", "frutonyanya", "fruto-nyanya", "фруто няня", "фрутоняня"],
    "Gerber": ["gerber", "гербер"],
    "Baby Goat": ["baby goat"],
    "Cicibebe": ["cicibebe", "eti cicibebe"],
    "Agusha": ["agusha", "агуша"],
    "HAH": ["hah"],
    "Kent Boringer": ["kent boringer"],
    # ─── Pharma — top производители на az рынке ───────────────────────────
    "Bayer": ["bayer"],
    "Sandoz": ["sandoz"],
    "Sanofi": ["sanofi"],
    "Berlin Chemie": ["berlin chemie", "berlin-chemie"],
    "Darnitsa": ["darnitsa", "дарница"],
    "GSK": ["gsk", "glaxosmithkline", "glaxo"],
    "Pfizer": ["pfizer"],
    "Roche": ["roche"],
    "Novartis": ["novartis"],
    "Egis": ["egis"],
    "Krka": ["krka"],
    "Servier": ["servier"],
    "Boehringer": ["boehringer", "boehringer ingelheim"],
    "Teva": ["teva"],
    "Stada": ["stada"],
    "Reckitt": ["reckitt", "reckitt benckiser"],
    "Bionorica": ["bionorica"],
    "Gedeon Richter": ["gedeon richter", "richter", "gedeon"],
    "Torrent": ["torrent"],
    "Cipla": ["cipla"],
    "Sun Pharma": ["sun pharma", "sun-pharma"],
    "Lupin": ["lupin"],
    "Abbott": ["abbott"],
    "Mylan": ["mylan"],
    "Mepha": ["mepha"],
    "Polpharma": ["polpharma"],
    "Adamed": ["adamed"],
    "Apotex": ["apotex"],
    "Dr.Reddy's": ["dr.reddy", "dr reddy", "dr.reddy's", "dr reddys"],
    "Adipharm": ["adipharm"],
    "Ferrer": ["ferrer"],
    "Aurobindo": ["aurobindo"],
    "Hetero": ["hetero"],
    "Galena": ["galena"],
    # ─── Витамины / БАДы ─────────────────────────────────────────────────
    "Solgar": ["solgar"],
    "Now Foods": ["now foods", "now"],
    "NaturesPlus": ["naturesplus", "nature's plus", "natures plus"],
    "Doppelherz": ["doppelherz"],
    "Centrum": ["centrum"],
    "Supradyn": ["supradyn"],
    "Berocca": ["berocca"],
    "Vitrum": ["vitrum"],
    "Multi-Tabs": ["multi-tabs", "multi tabs", "multitabs"],
    "Nordic Naturals": ["nordic naturals"],
    "Garden of Life": ["garden of life"],
    "Jamieson": ["jamieson"],
    "Webber Naturals": ["webber naturals"],
    "Swanson": ["swanson"],
    "Country Life": ["country life"],
    # ─── Косметика / гигиена / уход ──────────────────────────────────────
    "Nivea": ["nivea"],
    "L'Oreal": ["l'oreal", "loreal", "l oreal"],
    "Garnier": ["garnier"],
    "Vichy": ["vichy"],
    "La Roche-Posay": ["la roche-posay", "la roche posay", "laroche-posay"],
    "Bioderma": ["bioderma"],
    "Avene": ["avene", "avène"],
    "Eucerin": ["eucerin"],
    "CeraVe": ["cerave"],
    "Neutrogena": ["neutrogena"],
    "Olay": ["olay"],
    "Dove": ["dove"],
    "Pantene": ["pantene"],
    "Head & Shoulders": ["head & shoulders", "head and shoulders"],
    "Schwarzkopf": ["schwarzkopf"],
    "Wella": ["wella"],
    # ─── Детская гигиена ─────────────────────────────────────────────────
    "Pampers": ["pampers"],
    "Huggies": ["huggies"],
    "Libero": ["libero"],
    "Bella Baby": ["bella baby"],
    "Naty": ["naty"],
    "Chicco": ["chicco"],
    "Avent": ["avent", "philips avent"],
    "Tommee Tippee": ["tommee tippee"],
    # ─── Турецкие фарма-производители (популярны в AZ) ───────────────────
    "Abdi İbrahim": ["abdi ibrahim", "abdi i̇brahim", "abdi ibrahım", "abdi"],
    "Bilim": ["bilim", "bilim pharma", "bilim ilac"],
    "Sanovel": ["sanovel"],
    "Atabay": ["atabay"],
    "Drogsan": ["drogsan"],
    "Mustafa Nevzat": ["mustafa nevzat", "nevzat"],
    "Eczacıbaşı": ["eczacıbaşı", "eczacibasi", "eczaci"],
    "Deva": ["deva holding", "deva"],
    "Nobel İlaç": ["nobel ilac", "nobel ilaç"],
    "Recordati": ["recordati"],
    "Generica": ["generica"],
    # ─── Российские/локальные фарма ───────────────────────────────────────
    "Биосинтез": ["биосинтез", "biosintez"],
    "Вертекс": ["вертекс", "vertex"],
    "Озон": ["озон", "ozon"],
    "Канонфарма": ["канонфарма", "kanonfarma"],
    "Фармстандарт": ["фармстандарт", "farmstandart"],
    "Валента": ["валента", "valenta"],
    "Эвалар": ["эвалар", "evalar"],
    "Биокад": ["биокад", "biocad"],
    "Polens": ["polens"],
    # ─── OTC и популярные препараты как отдельные «бренды» ───────────────
    "Kreon": ["kreon"],
    "Espumisan": ["espumisan"],
    "Mezym": ["mezym", "mezim"],
    "No-Spa": ["no-spa", "no spa", "nospa"],
    "Nurofen": ["nurofen"],
    "Cardiomagnyl": ["cardiomagnyl", "кардиомагнил"],
    "Magne B6": ["magne b6", "magne-b6"],
    "Calcium D3": ["calcium d3", "calcium-d3", "kalsium d3"],
    "Linex": ["linex"],
    "Smecta": ["smecta", "smekta"],
    "Lazolvan": ["lazolvan"],
    "Theraflu": ["theraflu"],
    "Faringosept": ["faringosept"],
    "Strepsils": ["strepsils"],
    "Lugol": ["lugol"],
    "Otipax": ["otipax"],
    "Sumamed": ["sumamed", "sumamed forte"],
    "Augmentin": ["augmentin"],
    "Amoxil": ["amoxil"],
    "Ciprofloxacin": ["ciprofloxacin", "siprofloxasin"],
    "Voltaren": ["voltaren"],
    "Diclofenac": ["diclofenac", "diklofenak"],
    "Ibuprofen": ["ibuprofen"],
    "Aspirin": ["aspirin"],
    "Panadol": ["panadol"],
    "Coldrex": ["coldrex"],
    "Fervex": ["fervex"],
    # ─── БАДы / спорт / витамины (популярны в AZ) ────────────────────────
    "Vplab": ["vplab", "vp lab"],
    "Maxler": ["maxler"],
    "Optimum Nutrition": ["optimum nutrition", "on"],
    "Vitabiotics": ["vitabiotics"],
    "BioGaia": ["biogaia"],
    # ─── Местные AZ (на всякий случай) ───────────────────────────────────
    "Akva-Norm": ["akva-norm", "akva norm", "aqva-norm"],
    "Tibmedical": ["tibmedical"],
}


# Сетка от пропусков: если строка содержит ALL-CAPS Latin word длиной 4+
# и он не часть category-префикса — это вероятно бренд (например INDIVID, KINOJEL).
# Используется как fallback в extract_brand если catalog не нашёл совпадение.
_ALLCAPS_RE = re.compile(r"\b([A-Z][A-Z0-9\-]{3,})\b")
_BLOCKLIST_ALLCAPS = {
    "GOLD",
    "PRO",
    "PRE",
    "AC",
    "HA",
    "AR",
    "FORTE",
    "NEW",
    "BIO",
    "ECO",
    "OPT",
    "OPTI",
    "PLUS",
    "MAX",
    "MIN",
    "PREMIUM",
    "EXPERT",
    "COMFORT",
    "ULTRA",
    "CLASSIC",
    "ORIGINAL",
    "SUPER",
    "MEGA",
    "EXTRA",
    "TOTAL",
    "BL",
    "PEP",
    "VOM",
    "HD",
    "UV",
    "ML",
    "MG",
    "AZN",
    "USD",
    # P0.2 (PO Audit 2026-05-17): SPF/SPF15/SPF30/SPF50/SPF50+ путались как
    # бренды для солнцезащитной косметики. Дополнительно числовые комбинации
    # типа N20, N30 (количество в упаковке) — это pack-size, не brand.
    "SPF",
    "SPF15",
    "SPF30",
    "SPF50",
    "SPF50+",
    "SPF100",
    "N5",
    "N10",
    "N20",
    "N30",
    "N50",
    "N60",
    "N100",
    "OTC",
    "RX",
    "IU",
    "BV",
}

# First-word fallback (для pharma названий типа `Çetirizin №10`, `Fomaksi 30`)
# Берём первое значимое слово как brand если catalog не нашёл и оно не в blocklist.
_FIRST_WORD_RE = re.compile(r"^([A-ZÇŞĞÖÜİƏ][a-zçşğöüıəA-ZÇŞĞÖÜİƏ\-]{3,})\b")
_BLOCKLIST_FIRST_WORD = {
    # Префиксы baby food (хоть и в strip, может остаться при разных написаниях)
    "uşaq",
    "uşağ",
    "usaq",
    "usaqlar",
    "usaq",
    "südlü",
    "südsüz",
    "sudlu",
    "sudsuz",
    "peçenye",
    "pecenye",
    "südlüsiyiq",
    # Generic vitamin / drug forms
    "vitamin",
    "vitamins",
    "vitamini",
    "kompleks",
    "tablet",
    "tabletka",
    "tableti",
    "tabletlər",
    "tabletkalar",
    "kapsul",
    "kapsulalar",
    "kapsulları",
    "ampul",
    "ampullar",
    "ampulalar",
    "şərbət",
    "serbet",
    "məhlul",
    "mehlul",
    "krem",
    "məlhəm",
    "melhem",
    "gel",
    "geli",
    "sirop",
    "drops",
    "damla",
    "damlar",
    "spray",
    "sprey",
    "sprayi",
    # Описательные слова
    "premium",
    "forte",
    "extra",
    "plus",
    "active",
    "max",
    "ultra",
    "natural",
    "original",
    "classic",
    "gold",
    "silver",
    "düyü",
    "duyu",
    "süd",
    "sud",
    "məhsul",
    "mehsul",
    "tibbi",
    "vasitə",
    "vasitesi",
    "vasiteler",
    # Bottle / packaging
    "bottle",
    "şüşe",
    "buterılka",
    # ─── P0.2 (PO Audit 2026-05-17): generic слова попавшие как «бренды»
    # на /analytics top-15 и /site/X top brands. Аудит показал что эти слова
    # — типы товаров / категории, не названия производителей.
    # Gigiyenik (Гигиенические средства), Optik (оптика), Diapers (англ.
    # подгузники), Antiperspirant, Günəşdən («от солнца» — SPF crema),
    # Linkas (вид сиропа), Daha («Daha çox» = Show more — UI artifact в данных),
    # Cece (обрезок какого-то слова), Maddələr (вещества), Maddələrin (родит.).
    "gigiyenik",
    "gigiyenika",
    "gigiyena",
    "optik",
    "optika",
    "optiki",
    "diapers",
    "diaper",
    "antiperspirant",
    "antiperspiranti",
    "günəşdən",
    "gunesden",
    "gunesdän",
    "günəs",
    "gunes",
    "linkas",  # народное обозначение сиропа, не бренд (Himalaya — настоящий)
    "daha",
    "çox",
    "cox",  # фрагменты «Daha çox» — UI «Show more»
    "cece",
    "maddələr",
    "maddələrin",
    "maddeler",
    "maddelerin",
    # SPF cosmetics (title-cased в БД после extract_brand .title())
    "spf",
    "spf15",
    "spf30",
    "spf50",
    "spf100",
    "spf50+",
    "şampun",
    "şampunu",
    "şampunlar",
    "sampun",  # шампунь — не бренд
    "balzam",
    "balzamı",
    "duş",
    "dush",
    "krem",
    "kremi",
    "kremə",
    "yağ",
    "yağı",
    "yagi",
    "yag",
    "salfet",
    "salfeti",
    "salfetlər",
    # NB: "pampers" НЕ в blocklist — это валидный brand (Procter&Gamble),
    # отлавливается через BRANDS catalog. Раньше ошибочно был здесь.
    # Возрастные группы baby food (часто попадают в начало имени)
    "yaşdan",
    "yashdan",
    "yaşadan",
    "ayından",
    "ayindan",
    "aylığ",
    "ayligh",
    # Описание лекарственных форм / форм выпуска
    "məhlulu",
    "mehlulu",
    "məhlullar",
    "tibbi-vasitə",
    "tibbi-vasitələr",
    "tampon",
    "tamponi",
    "tamponlar",
    # ─── 2026-05-29 (matcher coverage deep-dive): generic AZ слова, массово
    # извлечённые как «бренды» (top-40 brand audit на проде). Это категории/
    # дескрипторы, не производители. Раздувают bucket_key и маскируют реальный
    # бренд → пропущенные cross-site матчи. Сравнение теперь accent-insensitive
    # (через strip_accents), поэтому достаточно ASCII-форм.
    "baby",
    "body",
    "sabun",  # мыло
    "leykoplastr",  # пластырь
    "boyukler",  # böyüklər = взрослые
    "boyuklar",
    "elastik",  # эластик(бинт)
    "emzik",  # əmzik = соска
    "qoruyucu",  # защитный
    "kalqotka",  # колготки
    "elcek",  # əlcək = перчатка
    "prezervativ",  # презерватив
    "beden",  # bədən = тело
    "maye",  # жидкость
    "varikoz",  # варикоз (категория)
    "agiz",  # ağız = рот/полость рта
    "toothpaste",
    "salfetka",
    "salfetkalar",
}


# Accent-insensitive вид блок-листа: strip_accents(ə→e, ş→s, ı→i…)+lower.
# Без этого «Günəş».lower()=«günəş» не совпадал с «gunes» в списке. Строим
# один раз на импорте.
_BLOCKLIST_NORMALIZED: frozenset[str] = frozenset(
    strip_accents(w).lower() for w in _BLOCKLIST_FIRST_WORD
)


def _is_blocklisted_word(word: str) -> bool:
    """Accent-insensitive проверка слова против блок-листа."""
    return strip_accents(word).lower() in _BLOCKLIST_NORMALIZED


def _first_word_brand(name: str) -> str | None:
    """Берём первое слово как brand если оно длиннее 3 символов и не в blocklist."""
    if not name:
        return None
    s = name.strip()
    # Удалить leading non-alpha
    s = re.sub(r"^[\"'«»\(\[]+", "", s)
    m = _FIRST_WORD_RE.match(s)
    if not m:
        return None
    word = m.group(1)
    if _is_blocklisted_word(word):
        return None
    # Не возвращать просто numeric/short
    if len(word) < 4:
        return None
    return word.title()  # Çetirizin, Fomaksi


@lru_cache(maxsize=1)
def _compiled_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Скомпилировать regex для каждого алиаса. Сортируем алиасы по длине desc,
    чтобы 'friso pep' матчился раньше 'friso' (избежать ложных срабатываний)."""
    patterns: list[tuple[str, re.Pattern[str]]] = []
    aliases = []
    for canonical, alist in BRANDS.items():
        for alias in alist:
            aliases.append((alias, canonical))
    aliases.sort(key=lambda x: len(x[0]), reverse=True)
    for alias, canonical in aliases:
        # \b не работает с кириллицей в Python без re.UNICODE, используем lookarounds
        pat = re.compile(
            r"(?<![\w])" + re.escape(alias) + r"(?![\w])",
            re.IGNORECASE | re.UNICODE,
        )
        patterns.append((canonical, pat))
    return patterns


def is_brand_blacklisted(brand: str | None) -> bool:
    """True если строка — generic слово которое не должно быть в `brand` поле.

    Используется (а) внутри extract_brand fallback (через _BLOCKLIST_FIRST_WORD),
    (б) для backfill чистки существующих записей в БД (см.
    scripts/cleanup_bad_brands.py), (в) для пост-валидации скрейпер output.

    Регистро- И акцент-нечувствительная проверка против объединённого blocklist
    (Günəş→gunes, Şampun→sampun ловятся несмотря на ş/ə-варианты).
    """
    if not brand:
        return False
    return _is_blocklisted_word(brand.strip())


def extract_brand(name: str | None) -> str | None:
    """Найти бренд в названии товара.

    Возвращает каноническое имя бренда (как в BRANDS keys) или None.
    Если совпало несколько — берём первый по позиции в строке (самый ранний).

    Fallback: если catalog не нашёл, ищем ALL-CAPS Latin слово (вероятно бренд).
    Это спасает от пропусков для редких брендов на категориях лекарств.
    """
    if not name:
        return None
    text = name.lower()
    best_canonical: str | None = None
    best_pos = len(text) + 1
    for canonical, pat in _compiled_patterns():
        m = pat.search(text)
        if m and m.start() < best_pos:
            best_pos = m.start()
            best_canonical = canonical
    if best_canonical:
        return best_canonical
    # Fallback 1: ALL-CAPS слово как бренд (если не в blocklist)
    for m in _ALLCAPS_RE.finditer(name):
        word = m.group(1)
        if word in _BLOCKLIST_ALLCAPS:
            continue
        return word.title()  # KINOJEL → Kinojel
    # Fallback 2: первое слово (для pharma имён типа `Çetirizin №10`)
    fw = _first_word_brand(name)
    if fw:
        return fw
    return None
