"""Tests для PharmonlineAPIScraper — REST-транспорт вместо мёртвого DDP.

Ключевая регрессия, которую сторожит этот файл: `external_id` обязан остаться
Meteor `_id`. Если он поедет на slug/sku, следующий прогон создаст ~10k новых
рядов вместо обновления существующих — ровно тот класс аварии, который чистили
2026-06-16 (9478 слаг-дублей).

Сети нет — httpx.AsyncClient подменяется MockTransport'ом.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from src.scrapers.base import SiteScrapeFatalError
from src.scrapers.pharmonline_api import (
    PharmonlineAPIScraper,
    _country_code_map,
    _page_size,
    _throttle_backoff,
)

_CATEGORIES = [
    {"id": "CAT_ANEMIA", "path": "anemiya-eleyhine-vasiteler", "name": "Anemiya"},
    {"id": "CAT_OTHER", "path": "diger", "name": "Digər"},
]

_COUNTRIES = [
    {"id": "C_TR", "name": "Türkiyə", "i18n": {"en": {"name": "Turkey"}}},
    {"id": "C_PL", "name": "Polşa", "i18n": {"ru": {"name": "Польша"}}},
    {"id": "C_XX", "name": "Заповедная Атлантида", "i18n": {}},
]


def _product(meteor_id: str, name: str = "Nemoir Lipo 30 ml", **extra) -> dict:
    """Meteor-документ в том виде, в каком его отдаёт /api/products."""
    doc = {
        "_id": meteor_id,
        "path": f"slug-{meteor_id.lower()}",
        "name": name,
        "i18n": {"az": {"name": name}},
        "barcode": "8698868000123",
        "images": ["IMG1"],
        "totalMinPrice": 21.7,
        "totalMaxPrice": 21.7,
        "category": ["CAT_ANEMIA"],
        "manufacturerCountry": "C_TR",
        "totalCount": 5,
    }
    doc.update(extra)
    return doc


def _mock_api(
    product_pages: list[dict | int],
    categories: list | None = None,
    countries: list | None = None,
):
    """Подменяет httpx.AsyncClient: роутит по пути запроса.

    `product_pages` — последовательность ответов на /api/products: dict (JSON,
    HTTP 200) или int (голый HTTP-статус). Последний элемент повторяется.
    """
    calls = {"products": 0, "categories": 0, "countries": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/products/countries"):
            calls["countries"] += 1
            return httpx.Response(200, json=_COUNTRIES if countries is None else countries)
        if path.endswith("/categories"):
            calls["categories"] += 1
            return httpx.Response(200, json=_CATEGORIES if categories is None else categories)
        idx = min(calls["products"], len(product_pages) - 1)
        calls["products"] += 1
        page = product_pages[idx]
        if isinstance(page, int):
            return httpx.Response(page, json={"statusCode": page, "message": "nope"})
        return httpx.Response(200, json=page)

    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("proxy", None)  # конфликтует с подменным transport
        return real_init(self, *args, **kwargs)

    return patch.object(httpx.AsyncClient, "__init__", patched_init), calls


@pytest.fixture(autouse=True)
def _fast_and_direct(monkeypatch):
    """Без пауз и без Decodo — тесты не должны спать по 7 секунд на запрос."""
    monkeypatch.setenv("PHARMONLINE_API_MIN_INTERVAL", "0")
    monkeypatch.setenv("PHARMONLINE_API_THROTTLE_BACKOFF", "0.01")
    for var in ("DECODO_USERNAME", "DECODO_PASSWORD", "DECODO_SITES"):
        monkeypatch.delenv(var, raising=False)


# ─── Справочник стран ────────────────────────────────────────────────────────


def test_country_code_map_resolves_from_name_and_i18n():
    """REST не отдаёт geocode → ISO берём из названий (az/ru/en)."""
    mapping = _country_code_map(_COUNTRIES)
    assert mapping["C_TR"] == "tr"
    assert mapping["C_PL"] == "pl"


def test_country_code_map_skips_unresolvable_names():
    """Незнакомая страна НЕ попадает в карту — лучше пусто, чем выдуманный код."""
    assert "C_XX" not in _country_code_map(_COUNTRIES)


def test_country_code_map_tolerates_garbage():
    assert _country_code_map(None) == {}
    assert _country_code_map(["строка", 42, {}, {"name": "Türkiyə"}]) == {}


# ─── Конфигурация ────────────────────────────────────────────────────────────


def test_page_size_capped_at_100(monkeypatch):
    """limit > 100 сервер отвергает (HTTP 400) — клиент не должен его просить."""
    monkeypatch.setenv("PHARMONLINE_API_PAGE_SIZE", "500")
    assert _page_size() == 100
    monkeypatch.setenv("PHARMONLINE_API_PAGE_SIZE", "50")
    assert _page_size() == 50
    monkeypatch.setenv("PHARMONLINE_API_PAGE_SIZE", "мусор")
    assert _page_size() == 100


def test_throttle_backoff_falls_back_on_bad_value(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_API_THROTTLE_BACKOFF", "не-число")
    assert _throttle_backoff() == 20.0


# ─── scrape_category ─────────────────────────────────────────────────────────


async def test_external_id_stays_meteor_id():
    """РЕГРЕССИЯ: external_id == Meteor _id, а не slug и не barcode/sku.

    Иначе следующий прогон не «обновит», а «создаст» весь каталог заново.
    """
    page = {"data": [_product("EDyEdzFrH9nGEHz3c")], "total": 1, "page": 1, "pages": 1}
    ctx, _ = _mock_api([page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            items = [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
    assert len(items) == 1
    assert items[0].external_id == "EDyEdzFrH9nGEHz3c"
    assert items[0].site == "pharmonline"
    assert items[0].price == 21.7
    assert items[0].barcode == "8698868000123"
    # страна разрезолвилась через карту названий → ISO
    assert items[0].manufacturer_country_raw == "tr"


async def test_paginates_and_dedups_by_external_id(monkeypatch):
    """Две страницы + повтор товара между ними → один yield на _id.

    Повтор категории здесь выключен — он покрыт отдельными тестами ниже, а тут
    проверяется именно пагинация с дедупом.
    """
    monkeypatch.setenv("PHARMONLINE_API_RACE_RETRIES", "0")
    p1 = {
        "data": [_product(f"ID{i:015d}") for i in range(100)],
        "total": 150,
        "page": 1,
        "pages": 2,
    }
    p2 = {
        # первый товар — дубль со страницы 1 (каталог сдвинулся под нами)
        "data": [_product("ID000000000000000")]
        + [_product(f"ID{i:015d}") for i in range(100, 149)],
        "total": 150,
        "page": 2,
        "pages": 2,
    }
    ctx, calls = _mock_api([p1, p2])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            items = [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
    assert calls["products"] == 2
    assert len(items) == 149
    assert len({p.external_id for p in items}) == 149


async def test_route_marked_complete_when_total_matches():
    page = {"data": [_product("A" * 17), _product("B" * 17)], "total": 2, "page": 1, "pages": 1}
    ctx, _ = _mock_api([page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
            status = sc._route_statuses["anemiya-eleyhine-vasiteler"]
    assert status.complete is True
    assert status.expected_items == 2
    assert status.parsed_items == 2


async def test_route_marked_incomplete_on_item_count_mismatch():
    """Сервер обещал 5, отдал 2 → маршрут неполный, каталог не «verified»."""
    page = {"data": [_product("A" * 17), _product("B" * 17)], "total": 5, "page": 1, "pages": 1}
    ctx, _ = _mock_api([page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
            status = sc._route_statuses["anemiya-eleyhine-vasiteler"]
    assert status.complete is False
    assert status.abort_reason == "item_count_mismatch"


async def test_site_side_duplicate_id_still_counts_as_complete():
    """Сайт кладёт один и тот же `_id` в категорию дважды — это не пробел покрытия.

    Проверено вживую на `sinir-sistemi-xestelikeri`: total=595, строк отдано 595,
    уникальных 594. Полнота меряется «прошли ли весь листинг» (строк == total), а
    не числом уникальных товаров — иначе такие категории вечно висят в
    `item_count_mismatch` и держат весь прогон в `degraded`.
    """
    page = {
        "data": [_product("A" * 17), _product("B" * 17), _product("A" * 17)],
        "total": 3,
        "page": 1,
        "pages": 1,
    }
    ctx, calls = _mock_api([page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            items = [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
            status = sc._route_statuses["anemiya-eleyhine-vasiteler"]
    assert [p.external_id for p in items] == ["A" * 17, "B" * 17]  # дедуп на выходе
    assert status.complete is True
    assert status.abort_reason is None
    assert calls["products"] == 1  # без лишнего перечитывания


async def test_short_listing_is_still_incomplete():
    """Если строк пришло МЕНЬШЕ обещанного — это реальный недобор, не дедуп."""
    page = {"data": [_product("A" * 17)], "total": 5, "page": 1, "pages": 1}
    ctx, _ = _mock_api([page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
            status = sc._route_statuses["anemiya-eleyhine-vasiteler"]
    assert status.complete is False
    assert status.abort_reason == "item_count_mismatch"


async def test_race_retry_tops_up_missing_items_without_duplicates():
    """Каталог «уехал» на первом проходе → перечитываем и добираем недостающее.

    Ключевое: `seen_external_ids` живёт между проходами, поэтому повторный проход
    отдаёт ТОЛЬКО новое — а не дублирует уже выданные товары.
    """
    incomplete = {"data": [_product("A" * 17)], "total": 2, "page": 1, "pages": 1}
    full = {
        "data": [_product("A" * 17), _product("B" * 17)],
        "total": 2,
        "page": 1,
        "pages": 1,
    }
    ctx, calls = _mock_api([incomplete, full])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            items = [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
            status = sc._route_statuses["anemiya-eleyhine-vasiteler"]
    assert calls["products"] == 2
    assert [p.external_id for p in items] == ["A" * 17, "B" * 17]  # без дублей
    assert status.complete is True
    assert status.abort_reason is None


async def test_race_retry_gives_up_after_configured_attempts(monkeypatch):
    """Если каталог «едет» и на повторе — честно помечаем маршрут неполным."""
    monkeypatch.setenv("PHARMONLINE_API_RACE_RETRIES", "1")
    page = {"data": [_product("A" * 17)], "total": 9, "page": 1, "pages": 1}
    ctx, calls = _mock_api([page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
            status = sc._route_statuses["anemiya-eleyhine-vasiteler"]
    assert calls["products"] == 2  # исходный проход + один повтор
    assert status.complete is False
    assert status.abort_reason == "item_count_mismatch"


async def test_race_retry_disabled_by_env(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_API_RACE_RETRIES", "0")
    page = {"data": [_product("A" * 17)], "total": 9, "page": 1, "pages": 1}
    ctx, calls = _mock_api([page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
    assert calls["products"] == 1


async def test_non_race_failure_is_not_retried():
    """Пропуски страниц — не гонка каталога, перечитывать бессмысленно."""
    ctx, calls = _mock_api([500])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
            status = sc._route_statuses["anemiya-eleyhine-vasiteler"]
    assert calls["products"] == 3  # ровно _MAX_CONSECUTIVE_PAGE_FAILURES, без повтора
    assert status.abort_reason == "consecutive_page_failures"


async def test_unknown_category_slug_yields_nothing_and_flags_route():
    """Без Meteor-id фильтровать нечем: НЕ отдаём весь каталог под видом категории."""
    page = {"data": [_product("A" * 17)], "total": 1, "page": 1, "pages": 1}
    ctx, calls = _mock_api([page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            items = [p async for p in sc.scrape_category("категории-такой-нет")]
            status = sc._route_statuses["категории-такой-нет"]
    assert items == []
    assert calls["products"] == 0  # запрос вообще не ушёл
    assert status.complete is False
    assert status.abort_reason == "unknown_category_slug"


async def test_throttled_request_retries_then_succeeds():
    """429 — не ошибка, а «подожди»: ретраим и добираем страницу."""
    page = {"data": [_product("A" * 17)], "total": 1, "page": 1, "pages": 1}
    ctx, calls = _mock_api([429, 429, page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            items = [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
    assert calls["products"] == 3
    assert len(items) == 1


async def test_pace_slows_after_throttle_and_relaxes_on_success(monkeypatch):
    """429 → замедляемся сразу; успехи → отыгрываем надбавку медленно.

    Асимметрия важна: иначе после отката мы снова разгоняемся в тот же лимит и
    долбим его по кругу, а счётчик у сайта общий с живыми покупателями.
    """
    monkeypatch.setenv("PHARMONLINE_API_MIN_INTERVAL", "7")
    monkeypatch.setenv("PHARMONLINE_API_PENALTY_STEP", "3")
    monkeypatch.setenv("PHARMONLINE_API_PENALTY_MAX", "30")
    monkeypatch.setenv("PHARMONLINE_API_PENALTY_DECAY_AFTER", "2")

    sc = PharmonlineAPIScraper()
    sc._interval_penalty = 0.0
    sc._ok_streak = 0

    assert sc._effective_interval() == 7
    sc._note_throttled()
    assert sc._effective_interval() == 10  # +3 сразу
    sc._note_throttled()
    assert sc._effective_interval() == 13

    # Один успех надбавку ещё не снимает...
    sc._note_ok()
    assert sc._effective_interval() == 13
    # ...а два подряд отыгрывают ровно 1 секунду.
    sc._note_ok()
    assert sc._effective_interval() == 12


def test_pace_penalty_is_capped(monkeypatch):
    """Надбавка не растёт бесконечно — иначе один затык вешает прогон навсегда."""
    monkeypatch.setenv("PHARMONLINE_API_MIN_INTERVAL", "7")
    monkeypatch.setenv("PHARMONLINE_API_PENALTY_STEP", "5")
    monkeypatch.setenv("PHARMONLINE_API_PENALTY_MAX", "12")

    sc = PharmonlineAPIScraper()
    sc._interval_penalty = 0.0
    sc._ok_streak = 0
    for _ in range(20):
        sc._note_throttled()
    assert sc._interval_penalty == 12
    assert sc._effective_interval() == 19


def test_pace_never_goes_below_base(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_API_MIN_INTERVAL", "7")
    monkeypatch.setenv("PHARMONLINE_API_PENALTY_DECAY_AFTER", "1")
    sc = PharmonlineAPIScraper()
    sc._interval_penalty = 0.0
    sc._ok_streak = 0
    for _ in range(50):
        sc._note_ok()
    assert sc._effective_interval() == 7


async def test_throttle_during_run_slows_subsequent_pace(monkeypatch):
    """Сквозная проверка: 429 внутри реального scrape_category двигает темп."""
    monkeypatch.setenv("PHARMONLINE_API_MIN_INTERVAL", "0")
    monkeypatch.setenv("PHARMONLINE_API_PENALTY_STEP", "2")
    page = {"data": [_product("A" * 17)], "total": 1, "page": 1, "pages": 1}
    ctx, _ = _mock_api([429, page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
            assert sc._interval_penalty == 2


def _enable_decodo(monkeypatch, attempts: int = 3) -> None:
    monkeypatch.setenv("DECODO_USERNAME", "u")
    monkeypatch.setenv("DECODO_PASSWORD", "p")
    monkeypatch.setenv("DECODO_SITES", "pharmonline")
    monkeypatch.setenv("DECODO_PORTS", "30001-30003")
    monkeypatch.setenv("DECODO_PAGE_ATTEMPTS", str(attempts))


async def test_single_403_rotates_ip_instead_of_killing_run(monkeypatch):
    """РЕГРЕССИЯ 2026-08-04: один 403 ронял весь прогон (`remaining=199`).

    На residential-прокси 403 привязан к конкретному exit-IP — правильная
    реакция это следующий адрес, а не обрыв сайта.
    """
    _enable_decodo(monkeypatch)
    page = {"data": [_product("A" * 17)], "total": 1, "page": 1, "pages": 1}
    ctx, calls = _mock_api([403, page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            items = [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
    assert len(items) == 1  # пережили плохой адрес
    assert calls["products"] == 2  # 403 + успех на другом порту


async def test_403_on_every_ip_still_raises_site_fatal(monkeypatch):
    """Если блок на ВСЕХ адресах — это системный бан, обрываем сайт.

    Иначе прогон отрапортует ok с 0 товаров и тихо занизит каталог.
    """
    _enable_decodo(monkeypatch, attempts=3)
    ctx, calls = _mock_api([403])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            with pytest.raises(SiteScrapeFatalError):
                [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
    assert calls["products"] == 3  # перебрали все выданные адреса


async def test_proxy_account_rejection_is_fatal_without_rotation(monkeypatch):
    """402/407 — счёт прокси, а не адрес: ретрай по портам только жжёт остаток."""
    _enable_decodo(monkeypatch, attempts=5)
    ctx, calls = _mock_api([407])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            with pytest.raises(SiteScrapeFatalError):
                [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
    assert calls["products"] == 1  # ровно одна попытка, без ротации


async def test_hard_block_raises_site_fatal():
    """Без прокси ротации нет — 403 сразу фатален (одна попытка = все попытки)."""
    ctx, _ = _mock_api([403])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            with pytest.raises(SiteScrapeFatalError):
                [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]


async def test_persistent_transient_failure_aborts_route():
    """N подряд битых страниц → обрыв маршрута, а не бесконечная пагинация."""
    ctx, calls = _mock_api([500])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            items = [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
            status = sc._route_statuses["anemiya-eleyhine-vasiteler"]
    assert items == []
    assert calls["products"] == 3  # _MAX_CONSECUTIVE_PAGE_FAILURES
    assert status.abort_reason == "consecutive_page_failures"


async def test_limit_is_respected():
    page = {
        "data": [_product(f"ID{i:015d}") for i in range(100)],
        "total": 100,
        "page": 1,
        "pages": 1,
    }
    ctx, _ = _mock_api([page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            items = [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler", limit=7)]
            status = sc._route_statuses["anemiya-eleyhine-vasiteler"]
    assert len(items) == 7
    assert status.abort_reason == "requested_limit_reached"


async def test_malformed_items_counted_as_failures_not_crash():
    page = {
        "data": [_product("A" * 17), {"_id": "B" * 17}, "не словарь"],
        "total": 3,
        "page": 1,
        "pages": 1,
    }
    ctx, _ = _mock_api([page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            items = [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
            status = sc._route_statuses["anemiya-eleyhine-vasiteler"]
    assert len(items) == 1
    # Счётчики — по ПОСЛЕДНЕМУ проходу (он перечитывает категорию целиком),
    # а не сумма по проходам: иначе ретрай задваивал бы телеметрию.
    assert status.item_failures == 2
    assert status.raw_items == 3
    assert status.complete is False


async def test_category_map_gives_slug_not_raw_meteor_id():
    """category в ScrapedProduct — человекочитаемый slug, а не Mongo _id."""
    page = {"data": [_product("A" * 17)], "total": 1, "page": 1, "pages": 1}
    ctx, _ = _mock_api([page])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            items = [p async for p in sc.scrape_category("anemiya-eleyhine-vasiteler")]
    assert items[0].category == "anemiya-eleyhine-vasiteler"


async def test_fetch_categories_returns_dicts():
    ctx, _ = _mock_api([{"data": [], "total": 0, "page": 1, "pages": 0}])
    with ctx:
        async with PharmonlineAPIScraper() as sc:
            cats = await sc.fetch_categories()
    assert len(cats) == 2
    assert {c["path"] for c in cats} == {"anemiya-eleyhine-vasiteler", "diger"}
