"""Поиск по всему каталогу и подсказки при наборе для страницы сравнения.

Зачем отдельный модуль. До 2026-10 поиск на /comparison был `ILIKE` по
`matches.canonical_name`, то есть искал только среди товаров, уже сопоставленных
между сайтами (≈3.8k из ≈44k). Клиент набирал «veqovi», «ozempik», «kreon» и
получал пусто, хотя товары лежат в каталоге всех трёх сайтов — у них просто нет
пары. Плюс написание: один и тот же препарат на сайтах записан как Kreon и
Creon, Veqovi и Wegovy, а сотрудники набирают ещё и кириллицей.

Что здесь:

- `fold()` — приводит строку к «ключу поиска»: регистр, диакритика,
  азербайджанские буквы, кириллица → латиница и типовые расхождения
  транслитерации (c/k/s, w/v, q/g, y/i, x/ks, ph/f, удвоения). Применяется
  одинаково к запросу и к названию, поэтому важна не «правильность»
  транслитерации, а то, что оба написания сходятся в один ключ.
- `CatalogIndex` — названия живых товаров в памяти процесса. Каталог маленький
  (десятки тысяч строк), поиск по нему — миллисекунды, и не нужны ни миграция,
  ни расширение Postgres. Индекс хранит только то, по чему ищут; цены, статус
  сопоставления и остальное вызывающий код читает из БД свежими.
- `CatalogIndex.search()` — все слова запроса должны встретиться как начала слов
  названия; опечатки ловятся отдельным нечётким проходом, только если точных
  совпадений нет.
- `CatalogIndex.suggest()` — подсказки «как в Google»: не полные названия
  (их по три почти одинаковых на препарат), а торговое имя и его частые
  продолжения: «Kreon», «Kreon 10000», «Kreon 25000».
"""

from __future__ import annotations

import bisect
import re
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import structlog
from rapidfuzz import process
from rapidfuzz.distance import Levenshtein

log = structlog.get_logger()

# ─── Свёртка написания ───────────────────────────────────────────────────────

# Азербайджанская латиница и кириллица → латиница без диакритики.
#  - «İ».lower() в Python даёт «i» + точку-диакритику, поэтому заглавные
#    переводим до lower();
#  - «ц» → «s» (Цефазолин/Sefazolin), «х» → «x» (как в az-латинице),
#    «ч» → «c» (чай/çay), «ш»/«щ» → «s» (шприц/şpris);
#  - последняя группа — az-кириллица, встречается в старых названиях.
_CHAR_MAP = str.maketrans(
    "İIıəƏğĞşŞçÇöÖüÜабвгдеёжзийклмнопрстуфхцчшщыэәғҝөүһҹј",
    "iiieeggssccoouuabvgdeejziiklmnoprstufxscssieeggouhcy",
)
# «№» — знак, а не часть слова: без этого «№20» давало бы слово «no20».
_CHAR_MAP.update(str.maketrans({"ъ": "", "ь": "", "ю": "yu", "я": "ya", "№": " "}))

_QU_RE = re.compile(r"qu(?=[aeio])")
_SOFT_C_RE = re.compile(r"c(?=[ei])")
_LONE_C_RE = re.compile(r"(?<![a-z0-9])c(?![a-z0-9])")
_REPEAT_RE = re.compile(r"([a-z])\1+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_LONE_C_MARK = "\x01"


def fold(text: str | None) -> str:
    """Ключ поиска: слова из [a-z0-9], разделённые одним пробелом."""
    if not text:
        return ""
    s = text.translate(_CHAR_MAP)
    if not s.isascii():
        # NFKD до lower(): совместимые формы раскрываются в заглавные («™» → «TM»).
        s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = _NON_ALNUM_RE.sub(" ", s.lower()).strip()
    s = s.replace("ph", "f").replace("th", "t").replace("sh", "s")
    s = s.replace("ch", "c").replace("ck", "k")
    s = _QU_RE.sub("kv", s)  # Quetiapine ↔ Kvetiapin
    s = s.replace("q", "g").replace("w", "v").replace("y", "i").replace("x", "ks")
    # Отдельно стоящая «c» — это буква-обозначение, а не звук: «Vitamin C» не
    # должен схлопнуться с «Vitamin K».
    s = _LONE_C_RE.sub(_LONE_C_MARK, s)
    s = _SOFT_C_RE.sub("s", s).replace("c", "k")  # Cefazolin ↔ Sefazolin, Creon ↔ Kreon
    s = s.replace(_LONE_C_MARK, "c")
    return _REPEAT_RE.sub(r"\1", s)  # Allegra ↔ Aleqra, кальций → kalsi


_fold_cached = lru_cache(maxsize=100_000)(fold)


def _query_tokens(query: str) -> list[list[str]]:
    """Слова запроса; у каждого — варианты написания.

    Каждое слово запроса ищется как НАЧАЛО слова названия, поэтому слово,
    кончающееся на «c», двусмысленно: что стоит после неё, неизвестно, а от этого
    зависит, читается она как «k» или как «s» («kalc» → kalsium, «ozempic» →
    ozempik). Для таких слов пробуем оба чтения.
    """
    tokens: list[list[str]] = []
    for word in _WORD_RE.findall(query):
        variants = [t for t in fold(word).split() if t]
        if not variants:
            continue
        last = variants[-1]
        tokens.extend([v] for v in variants[:-1])
        alternatives = [last]
        if len(last) > 1 and last.endswith("k") and word.translate(_CHAR_MAP).lower().endswith("c"):
            alternatives.append(last[:-1] + "s")
        tokens.append(alternatives)
    return tokens


# ─── Подсказки: какие слова считать началом торгового имени ──────────────────

# Единицы измерения и маркеры фасовки не продолжают название: «Ozempik 1 mq» —
# это дозировка, а не препарат «Ozempik 1».
_UNIT_TOKENS = frozenset(
    "mg mq mkg mkq mcg ml l g q gr qr kg kq sm mm m bv iu me ed eded n no x doza".split()
)
_PACK_TOKEN_RE = re.compile(r"^n\d+$")  # N20, №20 → «n20»
_LETTER_DIGIT_RE = re.compile(r"^[a-z]{1,2}\d{1,3}$")  # d3, b12, q10


def _is_head_continuation(token: str) -> bool:
    if token in _UNIT_TOKENS or _PACK_TOKEN_RE.match(token):
        return False
    if token.isalpha():
        return True  # в т.ч. одна буква: «Vitamin C», «Vitamin E»
    if token.isdigit():
        # «Kreon 10000» — имя; «Ozempik 1», «Aspirin 0» (из 0.5) — дозировка.
        return len(token) >= 4
    return bool(_LETTER_DIGIT_RE.match(token))


@dataclass(frozen=True)
class SearchHit:
    product_id: int
    site: str
    name: str
    rank: int  # 0 — название начинается с запроса … 4 — найдено нечётко


@dataclass(frozen=True)
class Suggestion:
    text: str
    count: int


@dataclass
class _Head:
    """Одно торговое имя (или имя + продолжение) и сколько товаров его носят."""

    key: str
    count: int = 0
    spellings: Counter = field(default_factory=Counter)

    @property
    def text(self) -> str:
        return self.spellings.most_common(1)[0][0]


class CatalogIndex:
    """Названия товаров одного арендатора, подготовленные к поиску."""

    def __init__(self, rows: list[tuple[int, str, str, str | None]]):
        """`rows` — (product_id, site, name, brand)."""
        self._ids: list[int] = []
        self._sites: list[str] = []
        self._names: list[str] = []
        # Ключ с пробелами по краям и перед каждым словом: «начало слова» тогда
        # ищется одной подстрокой " kre" без разбора на токены.
        self._name_keys: list[str] = []
        self._full_keys: list[str] = []  # название + бренд
        vocab: set[str] = set()
        heads1: dict[str, _Head] = {}
        heads2: dict[str, _Head] = {}

        for product_id, site, name, brand in rows:
            name_key = fold(name)
            if not name_key:
                continue
            brand_key = _fold_cached(brand) if brand else ""
            self._ids.append(product_id)
            self._sites.append(site)
            self._names.append(name)
            self._name_keys.append(f" {name_key} ")
            self._full_keys.append(f" {name_key} {brand_key} " if brand_key else f" {name_key} ")
            tokens = name_key.split()
            vocab.update(t for t in tokens if len(t) >= 3)
            if brand_key:
                vocab.update(t for t in brand_key.split() if len(t) >= 3)
            self._collect_heads(name, heads1, heads2)

        self._vocab: list[str] = sorted(vocab)
        self._vocab_by_initial: dict[str, list[str]] = defaultdict(list)
        for token in self._vocab:
            if token.isalpha():
                self._vocab_by_initial[token[0]].append(token)
        self._heads1 = heads1
        self._heads1_sorted: list[str] = sorted(heads1)
        self._heads2 = heads2
        self._heads2_sorted: list[str] = sorted(heads2)
        # «d3» → [«kalsium d3», «vitamin d3»]: подсказки по второму слову.
        self._heads2_by_second: dict[str, list[str]] = defaultdict(list)
        for key in self._heads2_sorted:
            self._heads2_by_second[key.split(" ", 1)[1]].append(key)
        self._seconds_sorted: list[str] = sorted(self._heads2_by_second)

    def __len__(self) -> int:
        return len(self._ids)

    @staticmethod
    def _collect_heads(name: str, heads1: dict[str, _Head], heads2: dict[str, _Head]) -> None:
        found = list(_WORD_RE.finditer(name))
        if not found:
            return
        first = found[0].group()
        first_key = _fold_cached(first)
        if " " in first_key or len(first_key) < 2 or not first_key.isalpha():
            return
        # «KREON» и «kreon» — одно имя; иначе подсказка возьмёт то написание,
        # которое случайно чаще встретилось на сайтах.
        first_text = first.capitalize() if first.isupper() or first.islower() else first
        # Имя из двух букв само по себе не подсказываем («No» из «No-Şpa»),
        # но как начало пары оно годится.
        if len(first_key) >= 3:
            head = heads1.setdefault(first_key, _Head(first_key))
            head.count += 1
            head.spellings[first_text] += 1
        if len(found) < 2:
            return
        second = found[1].group()
        second_key = _fold_cached(second)
        if " " in second_key or not _is_head_continuation(second_key):
            return
        # «0.25» режется на «0» и «25» — дробь не продолжение имени.
        if (
            second_key.isdigit()
            and len(found) > 2
            and _fold_cached(found[2].group()) in _UNIT_TOKENS
        ):
            return
        key = f"{first_key} {second_key}"
        # Дефис между словами — часть имени («No-Şpa», «Kalsium-D3»); всё
        # остальное (скобки, запятые, двойные пробелы) в подсказке не нужно.
        separator = "-" if name[found[0].end() : found[1].start()].strip() == "-" else " "
        head2 = heads2.setdefault(key, _Head(key))
        head2.count += 1
        head2.spellings[f"{first_text}{separator}{second}"] += 1

    # ── поиск ────────────────────────────────────────────────────────────────

    def _has_prefix(self, token: str) -> bool:
        i = bisect.bisect_left(self._vocab, token)
        return i < len(self._vocab) and self._vocab[i].startswith(token)

    def _fuzzy_tokens(self, token: str) -> list[str]:
        """Слова каталога, которые пользователь, вероятно, имел в виду.

        Сравниваем с НАЧАЛОМ слова той же длины: запрос обычно недописан
        («spazmaq» → «spazmalqon»). Первая буква должна совпасть — это режет
        шум и на порядок сужает перебор; расхождения транслитерации в первой
        букве уже сняты `fold()`.
        """
        if len(token) < 4 or not token.isalpha():
            return []
        max_distance = 1 if len(token) <= 5 else 2
        length = len(token)
        matches = process.extract(
            token,
            self._vocab_by_initial.get(token[0], ()),
            processor=lambda value: value[:length],
            scorer=Levenshtein.distance,
            score_cutoff=max_distance,
            limit=12,
        )
        return [value for value, _distance, _index in matches]

    def search(self, query: str, *, limit: int = 300) -> list[SearchHit]:
        """Товары, в названии (или бренде) которых есть все слова запроса."""
        tokens = _query_tokens(query)
        if not tokens:
            return []

        fuzzy = False
        needles: list[list[str]] = []
        for alternatives in tokens:
            exact = [a for a in alternatives if len(a) < 3 or self._has_prefix(a)]
            if exact:
                needles.append([f" {a}" for a in exact])
                continue
            guessed = self._fuzzy_tokens(alternatives[0])
            if not guessed:
                needles.append([f" {alternatives[0]}"])
                continue
            fuzzy = True
            needles.append([f" {g}" for g in guessed])

        # Самое длинное слово отсекает больше всего строк — проверяем его первым.
        needles.sort(key=lambda group: -max(len(n) for n in group))
        first, rest = needles[0], needles[1:]
        keys = self._full_keys
        if len(first) == 1:
            needle = first[0]
            candidates = [i for i, key in enumerate(keys) if needle in key]
        else:
            candidates = [i for i, key in enumerate(keys) if any(n in key for n in first)]
        for group in rest:
            candidates = [i for i in candidates if any(n in keys[i] for n in group)]

        phrase = " ".join(alternatives[0] for alternatives in tokens)
        starts = f" {phrase}"
        # Внутри одного ранга — по алфавиту ключа: один и тот же препарат с
        # разных сайтов называется почти одинаково и встаёт в списке рядом.
        ranked: list[tuple[int, str, int]] = []
        for i in candidates:
            name_key = self._name_keys[i]
            if fuzzy:
                rank = 4
            elif name_key.startswith(starts):
                rank = 0
            elif starts in name_key:
                rank = 1
            elif all(any(n in name_key for n in group) for group in needles):
                rank = 2
            else:
                rank = 3  # нашлось по бренду
            ranked.append((rank, name_key, i))

        # Запасной проход: одно слово внутри другого («kreon» → Lipakreon).
        # В конец списка, и только если запрос достаточно длинный, чтобы не
        # тащить всё подряд.
        if len(tokens) == 1 and len(phrase) >= 4 and len(ranked) < limit:
            seen = set(candidates)
            for i, key in enumerate(self._name_keys):
                if i not in seen and phrase in key:
                    ranked.append((5, key, i))

        ranked.sort()
        return [
            SearchHit(self._ids[i], self._sites[i], self._names[i], rank)
            for rank, _key, i in ranked[:limit]
        ]

    # ── подсказки ────────────────────────────────────────────────────────────

    @staticmethod
    def _with_prefix(sorted_keys: list[str], prefix: str) -> list[str]:
        start = bisect.bisect_left(sorted_keys, prefix)
        out: list[str] = []
        for key in sorted_keys[start:]:
            if not key.startswith(prefix):
                break
            out.append(key)
        return out

    def suggest(self, query: str, *, limit: int = 8) -> list[Suggestion]:
        """Варианты завершения набранного: торговые имена и их продолжения."""
        tokens = _query_tokens(query)
        if not tokens or len(tokens) > 2:
            return []
        if sum(len(alternatives[0]) for alternatives in tokens) < 2:
            return []

        picked: dict[str, _Head] = {}

        def take(heads: dict[str, _Head], keys: list[str], cap: int = limit) -> None:
            for key in sorted(keys, key=lambda k: (-heads[k].count, len(k), k)):
                if len(picked) >= cap:
                    return
                picked.setdefault(key, heads[key])

        if len(tokens) == 1:
            names: list[str] = []
            for phrase in tokens[0]:
                names.extend(self._with_prefix(self._heads1_sorted, phrase))
            exact = [phrase for phrase in tokens[0] if phrase in self._heads1]
            # Имя набрано целиком и оно одно такое → оно и его продолжения
            # («kreon» → Kreon, Kreon 10000, Kreon 25000). Если с набранного
            # начинается много имён, продолжения только мешают выбрать.
            if exact and len(names) <= 3:
                for phrase in exact:
                    picked.setdefault(phrase, self._heads1[phrase])
                    take(self._heads2, self._with_prefix(self._heads2_sorted, f"{phrase} "))
            take(self._heads1, names)
            # «d3» → «Kalsium D3», «Vitamin D3».
            for phrase in tokens[0]:
                for second in self._with_prefix(self._seconds_sorted, phrase):
                    take(self._heads2, self._heads2_by_second[second])
        else:
            pairs: list[str] = []
            for first in tokens[0]:
                for key in self._with_prefix(self._heads2_sorted, first):
                    second = key.split(" ", 1)[1]
                    if any(second.startswith(option) for option in tokens[1]):
                        pairs.append(key)
            take(self._heads2, pairs)

        if not picked and len(tokens) == 1:
            # Ничего не начинается с набранного — вероятно, опечатка.
            token = tokens[0][0]
            if len(token) >= 4 and token.isalpha():
                max_distance = 1 if len(token) <= 5 else 2
                length = len(token)
                pool = [k for k in self._heads1_sorted if k[0] == token[0]]
                matches = process.extract(
                    token,
                    pool,
                    processor=lambda value: value[:length],
                    scorer=Levenshtein.distance,
                    score_cutoff=max_distance,
                    limit=limit * 3,
                )
                guessed = sorted(matches, key=lambda m: (m[1], -self._heads1[m[0]].count))
                for key, _distance, _index in guessed[:limit]:
                    picked.setdefault(key, self._heads1[key])

        return [Suggestion(head.text, head.count) for head in list(picked.values())[:limit]]


# ─── Кэш индекса в процессе ──────────────────────────────────────────────────

# Индекс пересобирается, когда меняется «отпечаток» каталога (появились товары
# или завершился прогон), и в любом случае раз в _MAX_AGE_SECONDS: правка
# названия без нового прогона не должна оставаться невидимой надолго.
_MAX_AGE_SECONDS = 30 * 60
# …но не чаще раза в _MIN_AGE_SECONDS: пока прогон пишет товары пачками,
# отпечаток меняется каждые несколько секунд, и без этого порога каждый запрос
# поиска во время прогона платил бы за пересборку.
_MIN_AGE_SECONDS = 60

_lock = threading.Lock()
_cache: dict[int, tuple[tuple[Any, ...], float, CatalogIndex]] = {}


def reset_cache() -> None:
    """Сбросить индекс (тесты; после массовой правки каталога)."""
    with _lock:
        _cache.clear()


def _stamp(session: Any, tenant_id: int) -> tuple[Any, ...]:
    """Дешёвый отпечаток каталога: два запроса по индексам, без сканов таблиц."""
    from sqlalchemy import func, select

    from src import storage

    max_product_id = session.scalar(select(func.max(storage.Product.id)))
    last_run = session.execute(
        select(func.max(storage.Run.id), func.max(storage.Run.finished_at)).where(
            storage.Run.tenant_id == tenant_id
        )
    ).one()
    return (max_product_id, last_run[0], last_run[1])


def _fresh(cached: tuple[tuple[Any, ...], float, CatalogIndex] | None, stamp, now: float):
    if cached is None:
        return None
    age = now - cached[1]
    if age < _MIN_AGE_SECONDS or (cached[0] == stamp and age < _MAX_AGE_SECONDS):
        return cached[2]
    return None


def get_index(session: Any, *, tenant_id: int = 1) -> CatalogIndex:
    """Актуальный индекс арендатора; собирает его при первом обращении."""
    from sqlalchemy import select

    from src import storage

    stamp = _stamp(session, tenant_id)
    now = time.monotonic()
    index = _fresh(_cache.get(tenant_id), stamp, now)
    if index is not None:
        return index

    with _lock:
        index = _fresh(_cache.get(tenant_id), stamp, now)
        if index is not None:
            return index
        started = time.perf_counter()
        rows = session.execute(
            select(
                storage.Product.id,
                storage.Product.site,
                storage.Product.name,
                storage.Product.brand,
                storage.Product.brand_verified,
            ).where(
                storage.Product.tenant_id == tenant_id,
                storage.Product.url_dead_at.is_(None),
            )
        ).all()
        # `brand` на двух сайтах из трёх — первое слово названия, настоящий
        # производитель лежит в `brand_verified`; искать нужно по обоим.
        index = CatalogIndex(
            [
                (pid, site, name, " ".join(b for b in (brand, verified) if b) or None)
                for pid, site, name, brand, verified in rows
            ]
        )
        _cache[tenant_id] = (stamp, now, index)
        log.info(
            "catalog_search_index_built",
            tenant_id=tenant_id,
            products=len(index),
            seconds=round(time.perf_counter() - started, 3),
        )
        return index
