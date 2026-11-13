"""Сопоставление товаров электроники: один товар на разных сайтах — по модели.

Аптечное сопоставление (src/matcher.py) сверяет дозу, форму, фасовку и страну.
У электроники тот же товар узнаётся иначе, двумя способами:

1. **По коду модели.** «LG 55UR78006LK», «Televizor LG 55UR78006LK 55"» —
   бренд и заводской код совпали, значит товар один.
2. **По линейке.** У телефонов заводского кода в названии обычно нет:
   «Samsung Galaxy A55 8/256GB Awesome Navy». Товар один, когда совпали бренд,
   слова модели, память и цвет.

Правило — точность прежде охвата, как у аптек: пара создаётся только при полном
совпадении признаков. Чего название не говорит, то не додумывается: телефон с
цветом и телефон без цвета парой не становятся. Два товара одного сайта с
одинаковыми признаками — неоднозначность, такой сайт в пару не входит.

Первая версия написана по образцам названий, без замера на полных каталогах:
их ещё нет. Словари типов, цветов и брендов ниже — стартовые; охват и ошибки
мерить на живых данных экземпляра (см. docs/ELECTRONICS.md).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from src import site_profile
from src.storage import Match, MatchRejection, Product, latest_snapshots_per_product

log = structlog.get_logger()

STRATEGY_CODE = "model_code"
STRATEGY_SERIES = "model_series"

# Цены одного товара на разных сайтах расходятся на проценты, а не в разы.
# Пара с разницей больше этой — скорее разные товары под похожим названием.
MAX_PRICE_RATIO = 1.6

_FOLD = str.maketrans("əıöüğşç", "eiougsc")

# Слова «что это за товар»: у одного сайта стоят перед брендом, у другого нет.
# Сюда нельзя вносить слово, которое отличает один товар от другого: названия
# аксессуаров («çexol», «kabel», «üçün») остаются словами модели, иначе чехол
# для телефона получил бы признаки самого телефона.
_TYPE_WORDS = frozenset(
    """
    smartfon smartphone telefon mobil phone televizor tv television noutbuk notbuk laptop
    notebook planset planshet tablet qulaqliq qulaqciq headphones earbuds nausnik saat
    smartsaat smartwatch watch soyuducu refrigerator fridge dondurucu paltaryuyan
    washing machine masin qabyuyan dishwasher kondisioner conditioner tozsoran vacuum
    cleaner utu iron fen hairdryer mikrodalga microwave soba oven plite printer
    monitor kamera camera fotoaparat konsol console klaviatura keyboard
    mouse sican kolonka speaker dinamik powerbank
    qizdirici heater ventilyator blender mikser mixer toster toaster caydan kettle
    qehvebisiren epilyator trimmer britva ustelik desktop komputer computer
    router modem proyektor projector ve and yeni new
    """.split()
)

# Каноническое имя цвета. Оттенок — свой цвет: «navy» и «blue» разные товары.
_COLORS = {
    "black": "black",
    "qara": "black",
    "черный": "black",
    "чёрный": "black",
    "white": "white",
    "ag": "white",
    "белый": "white",
    "blue": "blue",
    "goy": "blue",
    "mavi": "blue",
    "синий": "blue",
    "navy": "navy",
    "green": "green",
    "yasil": "green",
    "зеленый": "green",
    "red": "red",
    "qirmizi": "red",
    "красный": "red",
    "gold": "gold",
    "qizili": "gold",
    "золотой": "gold",
    "silver": "silver",
    "gumusu": "silver",
    "серебристый": "silver",
    "gray": "gray",
    "grey": "gray",
    "boz": "gray",
    "серый": "gray",
    "pink": "pink",
    "cehrayi": "pink",
    "розовый": "pink",
    "purple": "purple",
    "benovseyi": "purple",
    "violet": "violet",
    "lavender": "lavender",
    "yellow": "yellow",
    "sari": "yellow",
    "orange": "orange",
    "narinci": "orange",
    "beige": "beige",
    "bej": "beige",
    "brown": "brown",
    "qehveyi": "brown",
    "graphite": "graphite",
    "qrafit": "graphite",
    "titanium": "titanium",
    "titan": "titanium",
    "mint": "mint",
    "lime": "lime",
    "coral": "coral",
    "cream": "cream",
    "ivory": "ivory",
    "bronze": "bronze",
    "copper": "copper",
    "midnight": "midnight",
    "starlight": "starlight",
    "natural": "natural",
    "desert": "desert",
    "olive": "olive",
    "teal": "teal",
    "cyan": "cyan",
    "turquoise": "turquoise",
    "burgundy": "burgundy",
    "sand": "sand",
    "rose": "rose",
    # Слова оттенка — часть цвета: «Light Blue» и «Blue» разные расцветки.
    "light": "light",
    "dark": "dark",
    "deep": "deep",
    "sky": "sky",
    "ice": "ice",
    "icy": "ice",
    "ocean": "ocean",
    "forest": "forest",
    "space": "space",
    "pearl": "pearl",
    "jet": "jet",
    "onyx": "onyx",
    "glacier": "glacier",
    "aurora": "aurora",
    "sunset": "sunset",
    "polar": "polar",
    "marble": "marble",
    "crystal": "crystal",
    "sierra": "sierra",
    "alpine": "alpine",
    "pacific": "pacific",
    "cloud": "cloud",
    "dawn": "dawn",
}
# Чисто рекламные слова при цвете: «Awesome Navy» и «Navy» — один цвет.
_COLOR_FILLERS = frozenset(
    "awesome phantom mystic cosmic matte glossy rengi reng rengli color colour".split()
)
# Слова, которые о модели ничего не говорят. Технология экрана и связь сюда не
# входят: «OLED» и «LCD», «Wi-Fi» и без него — разные товары.
_NOISE = frozenset(
    "smart uhd fhd hd 4k 8k android ios dual sim nano esim nfc bluetooth gb tb ram rom inch "
    "duym dm ekran display garantiya zemanet resmi original global version versiya".split()
)
_BRANDS = frozenset(
    """
    samsung apple xiaomi huawei honor oppo vivo realme oneplus nokia motorola
    tecno infinix itel google sony lg philips tcl hisense toshiba panasonic sharp haier
    bosch siemens beko arcelik indesit ariston hotpoint whirlpool electrolux zanussi aeg
    gorenje candy hoover midea gree aux daikin mitsubishi hp dell lenovo asus acer msi
    microsoft logitech razer jbl harman bose sennheiser marshall beats anker baseus
    canon nikon fujifilm gopro dji garmin amazfit fitbit tefal braun dyson karcher
    kenwood moulinex delonghi nespresso rowenta remington babyliss vitek polaris
    zass hoffmann eurolux vestel grundig blaupunkt kivi yandex nintendo
    epson brother kingston sandisk seagate western transcend tp-link tplink mercusys
    d-link dlink zyxel ubiquiti keenetic nothing blackview ulefone doogee cubot zte hoco
    """.split()
)
# Линейка, по которой бренд понятен без его имени.
_LINE_BRANDS = {
    "iphone": "apple",
    "ipad": "apple",
    "macbook": "apple",
    "airpods": "apple",
    "imac": "apple",
    "galaxy": "samsung",
    "redmi": "xiaomi",
    "poco": "xiaomi",
    "thinkpad": "lenovo",
    "ideapad": "lenovo",
    "vivobook": "asus",
    "zenbook": "asus",
    "pixel": "google",
    "playstation": "sony",
    "xbox": "microsoft",
}
# Слова-варианты модели: при совпавшем заводском коде они не должны спорить.
_VARIANT_WORDS = frozenset(
    "pro max plus ultra lite fe mini air neo turbo se f s e x t c 4g 5g lte wi-fi wifi "
    "cellular oled qled led lcd".split()
)

_MEMORY_PAIR_RE = re.compile(r"\b(\d{1,2})\s*(?:gb)?\s*[/+]\s*(?:(\d{2,4})\s*(gb)?|(\d)\s*(tb))\b")
_STORAGE_RE = re.compile(r"\b(\d{2,4})\s*(gb|tb)\b")
_TB_RE = re.compile(r"\b(\d)\s*tb\b")
_RAM_RE = re.compile(r"\b(\d{1,2})\s*gb\s*(?:ram|ozu)\b|\bram\s*(\d{1,2})\s*gb\b")
_SCREEN_RE = re.compile(r"""\b(\d{2,3})(?:[.,](\d))?\s*(?:"|''|”|inch|duym|dm)(?=\s|$)""")
_PLUS_RE = re.compile(r"(?<=[0-9a-zа-яё])\+")
_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+(?:[-.][0-9a-zа-яё]+)*")
_NETWORK = frozenset({"4g", "5g", "3g", "lte"})
# Характеристика, а не заводской код: процессор, ёмкость, мощность, класс
# Wi-Fi, память, видеокарта, разрешение. Выглядит как код (буквы с цифрами),
# но одна и та же у разных моделей.
_SPEC_RE = re.compile(
    r"""^(?:
        \d+(?:[.,]\d+)?(?:mah|w|wt|vt|hz|mm|sm|cm|ml|l|kg|kq|qr|btu|rpm|dpi|mp|v|a|k|bar|db)
      | (?:i[3579]|r[3579]|n\d|m[1-4])-?\d{3,5}[a-z]{0,3}
      | (?:ax|ac|be|n)\d{3,5}
      | ddr\d(?:-?\d+)?
      | (?:rtx|gtx|rx|mx)-?\d{3,4}[a-z]*
      | \d+x\d+
      | \d{3,4}p
    )$""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class Signature:
    brand: str
    code: str | None
    words: frozenset[str]
    ram: int | None
    storage: int | None
    screen: str | None
    colors: frozenset[str]

    @property
    def variant(self) -> frozenset[str]:
        """Слова, отличающие вариант модели: с цифрой и из списка вариантов."""
        return frozenset(
            word
            for word in self.words
            if word in _VARIANT_WORDS or any(character.isdigit() for character in word)
        )

    @property
    def series_key(self) -> tuple | None:
        if not self.words:
            return None
        return (self.brand, self.words, self.ram, self.storage, self.screen, self.colors)


def _is_model_code(token: str) -> bool:
    """Заводской код модели: буквы и цифры вперемешку, длинный, не характеристика."""
    if _SPEC_RE.match(token):
        return False
    compact = token.replace("-", "").replace(".", "")
    digits = sum(character.isdigit() for character in compact)
    letters = sum(character.isalpha() for character in compact)
    return len(compact) >= 6 and digits >= 3 and letters >= 2


def _is_designation(token: str) -> bool:
    """Обозначение модели в линейке: буквы и цифры в одном слове, не сеть."""
    return (
        len(token) >= 3
        and token not in _NETWORK
        and any(character.isdigit() for character in token)
        and any(character.isalpha() for character in token)
    )


def signature(name: str | None) -> Signature | None:
    """Признаки товара из названия; ``None``, если бренд не узнать."""
    # «İ» до приведения к нижнему регистру: иначе она распадается на «i» и точку.
    text = " ".join((name or "").replace("İ", "i").lower().translate(_FOLD).split())
    if not text:
        return None

    ram = storage = None
    pair = _MEMORY_PAIR_RE.search(text)
    if pair:
        ram = int(pair.group(1))
        storage = int(pair.group(2)) if pair.group(2) else int(pair.group(4)) * 1024
        text = text[: pair.start()] + " " + text[pair.end() :]
    explicit_ram = _RAM_RE.search(text)
    if explicit_ram:
        ram = int(explicit_ram.group(1) or explicit_ram.group(2))
        text = text[: explicit_ram.start()] + " " + text[explicit_ram.end() :]
    if storage is None:
        terabytes = _TB_RE.search(text)
        single = _STORAGE_RE.search(text)
        if terabytes:
            storage = int(terabytes.group(1)) * 1024
            text = text[: terabytes.start()] + " " + text[terabytes.end() :]
        elif single:
            storage = int(single.group(1)) * (1024 if single.group(2) == "tb" else 1)
            text = text[: single.start()] + " " + text[single.end() :]

    screen = None
    inches = _SCREEN_RE.search(text)
    if inches:
        screen = inches.group(1) + (
            f".{inches.group(2)}" if inches.group(2) not in (None, "0") else ""
        )
        text = _SCREEN_RE.sub(" ", text)
    # «S23+», «Pro+» — другая модель, чем «S23» и «Pro».
    text = _PLUS_RE.sub(" plus ", text)

    tokens = [token.strip("-.") for token in _TOKEN_RE.findall(text)]
    tokens = [token for token in tokens if token]

    # Бренд — известное имя где угодно в названии; линейка называет бренд сама
    # («iPhone 15» — это Apple); иначе — первое слово, которое не тип товара.
    brand = next((token for token in tokens if token in _BRANDS), None)
    if brand is None:
        brand = next((_LINE_BRANDS[token] for token in tokens if token in _LINE_BRANDS), None)
    if brand is None:
        brand = next((token for token in tokens if token not in _TYPE_WORDS), None)
    if brand is None or brand.isdigit() or len(brand) < 2:
        return None

    code = None
    words: set[str] = set()
    colors: set[str] = set()
    for token in tokens:
        if token == brand or token in _COLOR_FILLERS:
            continue
        if token in _COLORS:
            colors.add(_COLORS[token])
            continue
        if token in _TYPE_WORDS or token in _NOISE:
            continue
        if code is None and _is_model_code(token):
            # «55UR78006LK.AMCE»: региональный хвост после точки пишут не все.
            code = token.split(".")[0].replace("-", "")
            continue
        words.add(token)

    return Signature(
        brand=brand,
        code=code,
        words=frozenset(words),
        ram=ram,
        storage=storage,
        screen=screen,
        colors=frozenset(colors),
    )


def _agree(first, second) -> bool:
    """Признак не спорит: назван у обоих одинаково или у кого-то не назван."""
    return not first or not second or first == second


def _same_product(a: Signature, b: Signature) -> str | None:
    """Стратегия, по которой два товара — один; ``None`` — не один."""
    if a.brand != b.brand:
        return None
    if a.code and b.code:
        if a.code != b.code:
            return None
        # Код совпал. Всё, что названо у обоих, должно совпасть; вариант модели
        # (слова с цифрой, «Pro», «Plus», «OLED») — совпасть в любом случае:
        # один и тот же код пишут у ноутбуков с разным процессором и памятью.
        if not (
            _agree(a.ram, b.ram)
            and _agree(a.storage, b.storage)
            and _agree(a.screen, b.screen)
            and _agree(a.colors, b.colors)
            and a.variant == b.variant
        ):
            return None
        return STRATEGY_CODE
    if a.series_key is not None and a.series_key == b.series_key:
        # Линейка без памяти и цвета: товар тот же, только если в названии есть
        # обозначение модели («Tune 520BT», «V15»). «iPhone 15» без памяти и
        # цвета — это несколько разных товаров.
        if a.storage is None and not a.colors and not any(_is_designation(w) for w in a.words):
            return None
        return STRATEGY_SERIES
    return None


def _rejected_pairs(session: Session) -> set[tuple[int, int]]:
    rows = session.execute(
        select(MatchRejection.product_a_id, MatchRejection.product_b_id).where(
            MatchRejection.is_active.is_(True)
        )
    ).all()
    return {(min(a, b), max(a, b)) for a, b in rows}


def _prices_agree(prices: dict[int, float], first: int, second: int) -> bool:
    one, two = prices.get(first), prices.get(second)
    if not one or not two:
        return True
    return max(one, two) / min(one, two) <= MAX_PRICE_RATIO


def match_products(session: Session, *, tenant_id: int = 1) -> int:
    """Свести товары сайтов экземпляра в пары; возвращает число новых пар.

    Существующие пары не разбираются и не сливаются: новый товар входит в пару,
    только если подходит каждому её члену, его сайт в ней не представлен ни
    живым, ни пропавшим товаром и другого такого же кандидата у сайта нет.
    Ручные пары (`is_manual`) не трогаются. Результат фиксируется здесь же —
    как у аптечного `matcher.match_products`: этап сопоставления идёт под своим
    замком, и пары не должны зависеть от шагов прогона после него.
    """
    rows = list(
        session.scalars(
            select(Product).where(
                Product.site.in_(site_profile.SITES), Product.tenant_id == tenant_id
            )
        ).all()
    )
    by_id = {product.id: product for product in rows}
    # Товар с мёртвой ссылкой новых пар не образует, но место своего сайта в
    # уже существующей паре занимает.
    alive = [product for product in rows if product.url_dead_at is None]
    signatures: dict[int, Signature] = {}
    for product in alive:
        found = signature(product.name)
        if found is not None:
            signatures[product.id] = found
    rejected = _rejected_pairs(session)
    snapshots = latest_snapshots_per_product(session, list(signatures), tenant_id=tenant_id)
    prices = {
        product_id: float(snapshot.discount_price or snapshot.price)
        for product_id, snapshot in snapshots.items()
        if (snapshot.discount_price or snapshot.price)
    }

    def compatible(first: int, second: int) -> str | None:
        if (min(first, second), max(first, second)) in rejected:
            return None
        if not _prices_agree(prices, first, second):
            return None
        return _same_product(signatures[first], signatures[second])

    # Кандидаты ищутся не перебором бренда, а по ключам: товары с тем же
    # заводским кодом и товары с теми же признаками линейки. Перебор всех пар
    # бренда на каталоге в десятки тысяч товаров шёл бы десятки минут.
    by_code: dict[tuple, list[int]] = defaultdict(list)
    by_series: dict[tuple, list[int]] = defaultdict(list)
    for product_id, found in signatures.items():
        if found.code:
            by_code[(found.brand, found.code)].append(product_id)
        if found.series_key is not None:
            by_series[found.series_key].append(product_id)

    def neighbours(product_id: int) -> list[int]:
        found = signatures[product_id]
        out: dict[int, None] = {}
        if found.code:
            for other in by_code[(found.brand, found.code)]:
                out[other] = None
        if found.series_key is not None:
            for other in by_series[found.series_key]:
                out[other] = None
        out.pop(product_id, None)
        return sorted(out)

    clusters: dict[int, list[int]] = defaultdict(list)
    for product in rows:
        if product.canonical_id is not None:
            clusters[product.canonical_id].append(product.id)
    manual = set(session.scalars(select(Match.id).where(Match.is_manual.is_(True))).all())
    free = sorted(pid for pid in signatures if by_id[pid].canonical_id is None)

    # 1. Свободный товар входит в существующую пару. Кандидаты пары считаются
    #    по сайтам: два равно подходящих товара одного сайта — никто не входит.
    created = attached = ambiguous = 0
    claims: dict[tuple[int, str], list[int]] = defaultdict(list)
    homes: dict[int, set[int]] = defaultdict(set)
    for product_id in free:
        site = by_id[product_id].site
        for match_id in {by_id[n].canonical_id for n in neighbours(product_id)} - {None}:
            cluster = clusters[match_id]
            if match_id in manual or any(by_id[pid].site == site for pid in cluster):
                continue
            members = [pid for pid in cluster if pid in signatures]
            if members and all(compatible(product_id, pid) for pid in members):
                claims[(match_id, site)].append(product_id)
                homes[product_id].add(match_id)
    for (match_id, _site), candidates in sorted(claims.items()):
        if len(candidates) != 1 or len(homes[candidates[0]]) != 1:
            ambiguous += 1
            continue
        by_id[candidates[0]].canonical_id = match_id
        clusters[match_id].append(candidates[0])
        attached += 1
    # Товар, который спорил за место в готовой паре, новую пару не начинает.
    free = [pid for pid in free if by_id[pid].canonical_id is None and not homes[pid]]

    # 2. Свободные товары разных сайтов образуют новую пару.
    free_set = set(free)
    partners: dict[int, dict[str, list[int]]] = {}
    for product_id in free:
        sites: dict[str, list[int]] = defaultdict(list)
        for other in neighbours(product_id):
            if other in free_set and by_id[other].site != by_id[product_id].site:
                if compatible(product_id, other):
                    sites[by_id[other].site].append(other)
        partners[product_id] = sites

    used: set[int] = set()
    for product_id in free:
        if product_id in used or not partners[product_id]:
            continue
        group = [product_id]
        unsure = False
        for _site, candidates in sorted(partners[product_id].items()):
            candidates = [pid for pid in candidates if pid not in used]
            if len(candidates) != 1:
                # Два одинаково подходящих товара одного сайта — не выбираем.
                unsure = unsure or len(candidates) > 1
                continue
            candidate = candidates[0]
            # Взаимность: и для кандидата наш сайт должен быть однозначным.
            back = [
                pid
                for pid in partners[candidate].get(by_id[product_id].site, [])
                if pid not in used
            ]
            if back != [product_id]:
                unsure = True
                continue
            if all(compatible(candidate, member) for member in group):
                group.append(candidate)
        if unsure:
            ambiguous += 1
        if len(group) < 2:
            continue
        strategies = {_same_product(signatures[group[0]], signatures[other]) for other in group[1:]}
        anchor = next(
            (pid for pid in group if by_id[pid].site == site_profile.CLIENT_SITE), group[0]
        )
        by_code_only = strategies == {STRATEGY_CODE}
        match = Match(
            tenant_id=tenant_id,
            canonical_name=by_id[anchor].name[:500],
            canonical_brand=signatures[anchor].brand[:200],
            confidence=1.0 if by_code_only else 0.9,
            is_manual=False,
            match_strategy=STRATEGY_CODE if by_code_only else STRATEGY_SERIES,
        )
        session.add(match)
        session.flush()
        for pid in group:
            by_id[pid].canonical_id = match.id
            used.add(pid)
        clusters[match.id] = list(group)
        created += 1

    session.commit()
    log.info(
        "electronics_matcher_done",
        products=len(rows),
        with_signature=len(signatures),
        new_clusters=created,
        attached=attached,
        ambiguous=ambiguous,
    )
    return created
