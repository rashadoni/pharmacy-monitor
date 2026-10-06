"""Поиск по каталогу и подсказки (src/catalog_search.py).

Названия в тестах — настоящие, из прод-каталога 2026-10-06: именно на них
клиент получил «ничего не найдено» (veqovi, ozempik, kreon, kalsiy d3, озе).
"""

import pytest

from src import catalog_search, storage
from src._time import utcnow
from src.catalog_search import CatalogIndex, fold

CATALOG = [
    (1, "pharmonline", "Veqovi 0.25 mq (0.68 mq/ml) 1,5 ml № 1", "Veqovi"),
    (2, "aptekonline", "Veqovi   0.25 mq/doza   1 mq  1.5 ml   N1  (Wegovy)", "Veqovi"),
    (3, "pharmonline", "Ozempik 1.0 mq/doza 3.0 ml № 1 (Şpris - qələm)", "Ozempik"),
    (4, "aloe", "Ozempik 1 mq 3ml", "Novo nordisk"),
    (5, "pharmonline", "Kreon 10000 №20 (Kapsula)", "Kreon Abbot"),
    (6, "aptekonline", "Kreon 10000  N20", "Kreon"),
    (7, "aloe", "Creon 10000 20 əd.", "Abbot"),
    (8, "aptekonline", "Kreon  25000  N20", "Kreon"),
    (9, "pharmonline", "Lipakreon № 60 (Tabletlər) (Rumıniya)", "Lipakreon"),
    (10, "pharmonline", "Kalsium-D3 portağal 500 mq №30 (Tabletlər)", "Kalsium"),
    (11, "aloe", "Calcium-D3 500 mq 30 əd.", "Nycomed"),
    (12, "aptekonline", "Spazmalqon  N20", "Spazmalqon"),
    (13, "pharmonline", "Vitamin C 2 ml № 10", "Vitamin"),
    (14, "pharmonline", "Vitamin K 10 mq № 5", "Vitamin"),
    (15, "aptekonline", "No-Şpa  40 mq  N24", "No-Şpa"),
    (16, "aloe", "No-spa 40 mq 24 əd.", "Sanofi"),
    (17, "aptekonline", "Ksarelto  15 mq N28", "Ksarelto"),
    (18, "pharmonline", "Krem Bepanten 30 q", "Krem"),
    (19, "pharmonline", "Krem Bioderma 40 ml", "Krem"),
    (20, "aloe", "KREON 25000 20 əd", "Abbot"),
]


@pytest.fixture
def index():
    return CatalogIndex(CATALOG)


def _ids(index, query):
    return [hit.product_id for hit in index.search(query)]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Wegovy", "Veqovi"),
        ("вегови", "veqovi"),
        ("Ozempic", "Ozempik"),
        ("оземпик", "ozempik"),
        ("Creon", "Kreon"),
        ("креон", "KREON"),
        ("Xarelto", "Ksarelto"),
        ("Cefazolin", "Sefazolin"),
        ("цефазолин", "Sefazolin"),
        ("No-Shpa", "No-Şpa"),
        ("но-шпа", "No-Şpa"),
        ("Allegra", "Alegra"),
        ("чай", "çay"),
        ("İbuprofen", "ibuprofen"),
        ("Dərman", "derman"),
        # Заглавные: телефонная клавиатура ставит первую букву большой.
        ("Креон", "kreon"),
        ("КРЕОН", "kreon"),
        ("Оземпик", "ozempik"),
        ("Но-Шпа", "no-şpa"),
        ("ŞPRİS", "spris"),
        # Кириллические буквы-обозначения, которые читаются как латинские.
        ("витамин с", "Vitamin C"),
        ("Витамин С", "vitamin c"),
        ("в12", "B12"),
    ],
)
def test_fold_brings_spellings_together(left, right):
    assert fold(left) == fold(right)


def test_fold_drops_nothing_from_cyrillic_names():
    """Regression: заглавные кириллические буквы терялись («Креон» → «reon»)."""
    assert fold("Креон") == "kreon"
    assert fold("Кальций Д3") == "kalsi d3"
    assert fold('Südlü qarışıq "Малютка" Gold-2') == "sudlu garisig maliutka gold 2"


def test_fold_keeps_letter_codes_apart():
    """Отдельная буква — обозначение, а не звук: C и K нельзя склеивать."""
    assert fold("Vitamin C") != fold("Vitamin K")
    assert fold("") == "" and fold(None) == ""


def test_search_finds_products_without_a_cross_site_pair(index):
    assert set(_ids(index, "veqovi")) == {1, 2}
    assert set(_ids(index, "ozempik")) == {3, 4}


@pytest.mark.parametrize("query", ["wegovy", "вегови", "Veqovi", "VEQ"])
def test_search_ignores_script_and_spelling(index, query):
    assert set(_ids(index, query)) == {1, 2}


def test_search_kreon_matches_creon_and_ranks_inner_match_last(index):
    ids = _ids(index, "kreon")
    assert set(ids) == {5, 6, 7, 8, 9, 20}
    assert ids[-1] == 9  # Lipakreon — другое лекарство, совпало серединой слова
    assert set(_ids(index, "creon")) == set(ids)


def test_search_requires_every_word(index):
    assert set(_ids(index, "kreon 25000")) == {8, 20}
    assert _ids(index, "kreon 99999") == []


@pytest.mark.parametrize("query", ["kalsiy d3", "kal d3", "kalc d3", "kalciy d3", "calcium d3"])
def test_search_client_calcium_queries(index, query):
    """Все пять вариантов клиент набрал 2026-10-06 и получил пусто."""
    assert set(_ids(index, query)) == {10, 11}


def test_search_tolerates_typo_only_when_nothing_matches_exactly(index):
    typo = index.search("ozenpik")
    assert {hit.product_id for hit in typo} == {3, 4}
    assert all(hit.rank == 4 for hit in typo)
    assert all(hit.rank == 0 for hit in index.search("ozempik"))


def test_search_single_letter_code_does_not_cross_match(index):
    assert _ids(index, "vitamin c") == [13]
    assert _ids(index, "vitamin k") == [14]


def test_search_by_brand_ranks_after_name_matches(index):
    hits = index.search("abbot")
    assert {hit.product_id for hit in hits} == {5, 7, 20}
    assert all(hit.rank == 3 for hit in hits)


@pytest.mark.parametrize("query", ["Креон", "КРЕОН", "креон"])
def test_search_cyrillic_in_any_case(index, query):
    assert set(_ids(index, query)) == {5, 6, 7, 8, 9, 20}
    assert _ids(index, "Витамин С") == [13]


def test_search_is_not_capped_unless_asked(index):
    """Строки сравнения строятся по ВСЕМ найденным товарам (нужны в Excel целиком)."""
    big = CatalogIndex([(i, "aloe", f"Tablet {i:05d}", None) for i in range(1, 2501)])
    assert len(big.search("tablet")) == 2500
    assert len(big.search("tablet", limit=10)) == 10


def test_long_repetitive_query_is_bounded(index):
    from src.catalog_search import _MAX_QUERY_TOKENS, _query_tokens

    assert _query_tokens("mq " * 500) == [["mg"]]
    many = " ".join(f"slovo{i}" for i in range(200))
    assert len(_query_tokens(many)) == _MAX_QUERY_TOKENS
    assert index.search(many) == []


def test_search_empty_and_punctuation_only(index):
    assert index.search("") == []
    assert index.search("  -- ") == []


def test_search_respects_limit(index):
    assert len(index.search("k", limit=3)) == 3


def _texts(index, query, **kwargs):
    return [s.text for s in index.suggest(query, **kwargs)]


def test_suggest_completes_a_trade_name(index):
    assert _texts(index, "oze") == ["Ozempik"]
    assert _texts(index, "veq") == ["Veqovi"]
    assert _texts(index, "озе") == ["Ozempik"]


def test_suggest_full_name_offers_its_continuations(index):
    # «KREON»/«Creon» — то же имя, показываем самое частое написание.
    assert _texts(index, "kreon") == ["Kreon", "Kreon 10000", "Kreon 25000"]
    assert _texts(index, "creon") == ["Kreon", "Kreon 10000", "Kreon 25000"]


def test_suggest_orders_by_how_many_products_carry_the_name(index):
    texts = _texts(index, "kre")
    assert texts[0] == "Kreon"  # 5 товаров против 2 у «Krem»
    assert "Krem" in texts
    counts = {s.text: s.count for s in index.suggest("kre")}
    assert counts["Kreon"] == 5 and counts["Krem"] == 2


def test_suggest_does_not_treat_dosage_as_part_of_the_name(index):
    assert _texts(index, "ozempik") == ["Ozempik"]  # не «Ozempik 1», не «Ozempik 1.0»


def test_suggest_pairs_and_second_word(index):
    assert _texts(index, "kal d3") == ["Kalsium-D3"]
    assert _texts(index, "d3") == ["Kalsium-D3"]
    assert _texts(index, "vitamin") == ["Vitamin", "Vitamin C", "Vitamin K"]
    assert _texts(index, "no-s") == ["No-Şpa"]


def test_suggest_typo_and_too_short(index):
    assert _texts(index, "spazmalkon") == ["Spazmalqon"]
    assert _texts(index, "ozenpik") == ["Ozempik"]
    assert _texts(index, "o") == []
    assert _texts(index, "") == []


def test_suggest_limit(index):
    assert len(_texts(index, "k", limit=2)) <= 2
    assert len(_texts(index, "kr", limit=1)) == 1


def _product(session, pid, site, name, *, dead=False, tenant_id=1):
    session.add(
        storage.Product(
            id=pid,
            tenant_id=tenant_id,
            site=site,
            external_id=f"{site}-{pid}",
            url=f"https://{site}.example/{pid}",
            name=name,
            name_normalized=name.lower(),
            url_dead_at=utcnow() if dead else None,
        )
    )


def test_get_index_reads_live_products_of_the_tenant_only(db_session):
    _product(db_session, 1, "pharmonline", "Veqovi 1 mq")
    _product(db_session, 2, "aptekonline", "Veqovi 1 mq (Wegovy)", dead=True)
    _product(db_session, 3, "aloe", "Veqovi 1 mq", tenant_id=2)
    db_session.commit()

    index = catalog_search.get_index(db_session, tenant_id=1)

    assert [hit.product_id for hit in index.search("veqovi")] == [1]


def test_get_index_rebuilds_when_catalog_changes(db_session, monkeypatch):
    _product(db_session, 1, "pharmonline", "Veqovi 1 mq")
    db_session.commit()
    first = catalog_search.get_index(db_session)
    assert catalog_search.get_index(db_session) is first  # тот же объект из кэша

    _product(db_session, 2, "aptekonline", "Ozempik 1 mq")
    db_session.commit()
    # Сразу после сборки индекс не пересобирается (прогон пишет товары пачками)…
    assert catalog_search.get_index(db_session) is first
    # …а по истечении минимального интервала — подхватывает новый товар.
    monkeypatch.setattr(catalog_search, "_MIN_AGE_SECONDS", 0)
    rebuilt = catalog_search.get_index(db_session)
    assert rebuilt is not first
    assert [hit.product_id for hit in rebuilt.search("ozempik")] == [2]
