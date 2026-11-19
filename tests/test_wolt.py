"""Аптеки на Wolt: разбор ответа, полнота сбора, запись, сопоставление, API."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src import api as api_module
from src import storage, tenants, wolt
from src._time import utcnow

BUTA, ALOE, KANON = list(wolt.VENUES)


@pytest.fixture(autouse=True)
def _fresh_cache():
    wolt._rows_cache.clear()
    yield
    wolt._rows_cache.clear()


def _raw(item_id: str, name: str, price: int, **extra) -> dict:
    return {"id": item_id, "name": name, "price": price, **extra}


# ─── Разбор товара ───────────────────────────────────────────────────────────


def test_price_comes_in_qepik():
    offer = wolt.parse_item(_raw("a1", "Levomekol  40 qr", 300))

    assert offer == wolt.WoltOffer("a1", "Levomekol 40 qr", 3.0, None, True)


@pytest.mark.parametrize(
    "raw",
    [
        {"name": "X", "price": 100},
        {"id": "a", "name": "", "price": 100},
        {"id": "a", "name": "X", "price": 0},
        {"id": "a", "name": "X", "price": None},
        {"id": "a", "name": "X", "price": True},
    ],
)
def test_item_without_id_name_or_price_is_skipped(raw):
    assert wolt.parse_item(raw) is None


def test_stock_and_discount():
    assert not wolt.parse_item(_raw("a", "X", 100, purchasable_balance=0)).in_stock
    assert not wolt.parse_item(_raw("a", "X", 100, disabled_info={"reason": "x"})).in_stock
    assert wolt.parse_item(_raw("a", "X", 100, purchasable_balance=None)).in_stock
    assert wolt.parse_item(_raw("a", "X", 100, original_price=150)).original_price == 1.5
    # «Зачёркнутая» цена не выше действующей — не скидка.
    assert wolt.parse_item(_raw("a", "X", 100, original_price=100)).original_price is None


# ─── Сбор ────────────────────────────────────────────────────────────────────


def _catalog_transport(tree: list[dict], pages: dict[str, list[list[dict]]], *, fail=()):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        path = urlsplit(url).path
        if path.endswith("/assortment"):
            return httpx.Response(200, json={"categories": tree, "items": []})
        slug = path.rsplit("/", 1)[-1]
        if slug in fail:
            return httpx.Response(503, text="busy")
        token = parse_qs(urlsplit(url).query).get("page_token", ["0"])[0]
        index = int(token)
        chunks = pages[slug]
        next_token = str(index + 1) if index + 1 < len(chunks) else None
        return httpx.Response(
            200,
            json={"items": chunks[index], "metadata": {"next_page_token": next_token}},
        )

    return httpx.MockTransport(handler), requests


def _client(transport) -> httpx.Client:
    return httpx.Client(transport=transport)


def test_fetch_walks_leaf_categories_and_pages_and_dedupes():
    tree = [
        {"slug": "darmanlar", "subcategories": []},
        {
            "slug": "top",
            "subcategories": [
                {"slug": "vitamins", "subcategories": []},
                {"slug": "darmanlar", "subcategories": []},
            ],
        },
    ]
    pages = {
        "darmanlar": [[_raw("a", "A", 100), _raw("b", "B", 200)], [_raw("c", "C", 300)]],
        "vitamins": [[_raw("a", "A", 100), _raw("d", "D", 400)]],
    }
    transport, requests = _catalog_transport(tree, pages)

    offers = wolt.fetch_venue(BUTA, client=_client(transport), sleep=lambda _s: None)

    assert sorted(o.item_id for o in offers) == ["a", "b", "c", "d"]
    # Дерево + 2 страницы «darmanlar» (раздел в дереве дважды — читается раз) + 1.
    assert len(requests) == 4
    assert all("language=az" in url for url in requests)


def test_failed_page_fails_the_whole_venue():
    tree = [{"slug": "a", "subcategories": []}, {"slug": "b", "subcategories": []}]
    pages = {"a": [[_raw("x", "X", 100)]], "b": [[_raw("y", "Y", 100)]]}
    transport, requests = _catalog_transport(tree, pages, fail={"b"})

    with pytest.raises(wolt.WoltScrapeError, match="request_failed:http_503"):
        wolt.fetch_venue(BUTA, client=_client(transport), sleep=lambda _s: None)
    # Повторы были: страница «b» спрошена столько раз, сколько попыток.
    assert sum(url.endswith("/b?language=az") for url in requests) == wolt.ATTEMPTS


def test_empty_tree_is_a_failure():
    transport, _ = _catalog_transport([], {})

    with pytest.raises(wolt.WoltScrapeError, match="no_categories"):
        wolt.fetch_venue(BUTA, client=_client(transport), sleep=lambda _s: None)


# ─── Запись ──────────────────────────────────────────────────────────────────


def _offer(item_id, name, price, **kw):
    return wolt.WoltOffer(item_id, name, price, kw.get("original"), kw.get("in_stock", True))


def _items(db, venue):
    db.expire_all()
    return {
        row.item_id: row
        for row in db.scalars(select(storage.WoltItem).where(storage.WoltItem.venue == venue))
    }


def test_store_tracks_new_changed_and_gone(db_session):
    wolt.store_catalog(db_session, BUTA, [_offer("a", "A", 1.0), _offer("b", "B", 2.0)])
    db_session.commit()

    counts = wolt.store_catalog(db_session, BUTA, [_offer("a", "A", 1.5), _offer("c", "C", 3.0)])
    db_session.commit()

    assert counts == {"new": 1, "price_changed": 1, "gone": 1}
    items = _items(db_session, BUTA)
    assert items["a"].price == 1.5 and items["a"].price_changed_at is not None
    assert items["b"].in_catalog is False
    assert items["c"].in_catalog is True


def test_catalog_half_the_previous_is_not_written(db_session):
    db_session.add(storage.WoltScrape(venue=BUTA, status="ok", items=10, finished_at=utcnow()))
    db_session.commit()

    with pytest.raises(wolt.WoltScrapeError, match="catalog_shrank"):
        wolt.store_catalog(db_session, BUTA, [_offer(str(i), "X", 1.0) for i in range(4)])


def test_failed_sync_keeps_the_previous_catalog(db_session):
    wolt.store_catalog(db_session, BUTA, [_offer("a", "A", 1.0)])
    db_session.commit()
    transport, _ = _catalog_transport([{"slug": "x", "subcategories": []}], {}, fail={"x"})

    attempt = wolt.sync_venue(db_session, BUTA, client=_client(transport), sleep=lambda _s: None)

    assert attempt.status == "failed"
    assert attempt.error.startswith("request_failed")
    assert _items(db_session, BUTA)["a"].in_catalog is True


def test_successful_sync_records_the_attempt(db_session):
    transport, _ = _catalog_transport(
        [{"slug": "x", "subcategories": []}], {"x": [[_raw("a", "A", 250)]]}
    )

    attempt = wolt.sync_venue(db_session, BUTA, client=_client(transport), sleep=lambda _s: None)

    assert (attempt.status, attempt.items) == ("ok", 1)
    assert _items(db_session, BUTA)["a"].price == 2.5


def test_unknown_venue_is_refused(db_session):
    with pytest.raises(ValueError):
        wolt.sync_venue(db_session, "someone-else", client=None)


# ─── Сопоставление ───────────────────────────────────────────────────────────


def _item(venue, item_id, name, price):
    return storage.WoltItem(
        venue=venue, item_id=item_id, name=name, price=price, in_stock=True, in_catalog=True
    )


def test_same_product_in_three_venues_is_one_group():
    groups = wolt.group_items(
        [
            _item(BUTA, "1", "Novofan №60 Tabletlər", 55.8),
            _item(ALOE, "2", "Novofan №60 Tabletlər", 55.8),
            _item(KANON, "3", "Novofan N60", 59.2),
        ]
    )

    assert [sorted(group) for group in groups] == [sorted([BUTA, ALOE, KANON])]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # «Həblər» — таблетки по-азербайджански; общий словарь форм его не знает.
        ("Ursosan 250 mq Kapsul", "Ursosan 250 mq Həblər"),
        ("Pharmatex Vaginal Həblər", "Pharmatex Vaginal Şamlar"),
        ("Linkas 100 ml", "Linkas Plyus 100 ml"),
        ("Ferrovef № 30 tablet", "Ferrovef № 60 tablet"),
    ],
)
def test_different_products_are_not_grouped(a, b):
    assert wolt.group_items([_item(BUTA, "1", a, 10.0), _item(ALOE, "2", b, 10.0)]) == []


def test_two_candidates_in_one_venue_leave_its_place_empty():
    groups = wolt.group_items(
        [
            _item(BUTA, "1", "Keratodrop 10 ml", 17.0),
            _item(ALOE, "2", "Keratodrop 10 ml", 17.0),
            _item(ALOE, "3", "Keratodrop 10 ml", 18.0),
            _item(KANON, "4", "Keratodrop 10 ml", 18.2),
        ]
    )

    assert [sorted(group) for group in groups] == [sorted([BUTA, KANON])]


def _seed(db, *items):
    db.add_all(items)
    for venue in wolt.VENUES:
        db.add(storage.WoltScrape(venue=venue, status="ok", items=1, finished_at=utcnow()))
    db.commit()


def test_comparison_rows_totals_and_filters(db_session):
    _seed(
        db_session,
        _item(BUTA, "1", "Betadin 10% 30 ml", 6.6),
        _item(ALOE, "2", "Betadin 10% 30 ml", 9.3),
        _item(BUTA, "3", "Duspatalin 200 mq N30", 17.0),
        _item(KANON, "4", "Duspatalin 200 mq N30", 17.0),
        # Втрое дороже — почти наверняка другая фасовка: строки нет.
        _item(ALOE, "5", "Viaqra 100 mq N4", 9.43),
        _item(KANON, "6", "Viaqra 100 mq N4", 68.03),
    )

    result = wolt.comparison(db_session)

    assert result["totals"] == {"rows": 2, "differ": 1, "all_venues": 0, "suspicious": 1}
    first = result["rows"][0]
    assert first["name"] == "Betadin 10% 30 ml"
    assert first["cheapest"] == [BUTA]
    assert first["spread_pct"] == pytest.approx(40.9)
    assert first["prices"][BUTA]["url"].endswith(f"/venue/{BUTA}/itemid-1")
    assert "_search" not in first
    assert [r["name"] for r in wolt.comparison(db_session, only_diff=True)["rows"]] == [
        "Betadin 10% 30 ml"
    ]
    assert [r["name"] for r in wolt.comparison(db_session, search="duspatal")["rows"]] == [
        "Duspatalin 200 mq N30"
    ]
    assert [v["venue"] for v in result["venues"]] == list(wolt.VENUES)


def test_new_scrape_refreshes_the_cached_rows(db_session):
    _seed(
        db_session,
        _item(BUTA, "1", "Enfekderm 15 q", 7.49),
        _item(ALOE, "2", "Enfekderm 15 q", 7.49),
    )
    assert wolt.comparison(db_session)["totals"]["differ"] == 0

    db_session.scalars(
        select(storage.WoltItem).where(storage.WoltItem.item_id == "2")
    ).one().price = 8.0
    db_session.add(storage.WoltScrape(venue=ALOE, status="ok", items=1, finished_at=utcnow()))
    db_session.commit()

    assert wolt.comparison(db_session)["totals"]["differ"] == 1


# ─── API ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch, db_session):
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    monkeypatch.setenv("PHARMACY_AUTH_DEV_SHOW_TOKEN", "1")
    return TestClient(api_module.app)


def test_api_needs_a_login(client):
    assert client.get("/api/v1/dash/wolt/comparison").status_code == 401


def test_api_returns_the_comparison(client, db_session):
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed")
    tenant = tenants.get_or_create_default(db_session)
    db_session.add(
        storage.TenantUser(
            tenant_id=tenant.id,
            email="viewer@example.test",
            role="viewer",
            is_active=True,
            created_at=utcnow(),
        )
    )
    db_session.commit()
    _seed(
        db_session,
        _item(BUTA, "1", "Ketonal Forte N20", 6.5),
        _item(KANON, "2", "Ketonal Forte N20", 7.0),
    )
    token = tenants.issue_magic_token(db_session, "viewer@example.test")
    assert client.get(f"/auth/verify?token={token}").status_code == 200

    response = client.get("/api/v1/dash/wolt/comparison", params={"only_diff": "true"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"]["rows"] == 1
    assert json.dumps(body["rows"][0]["prices"][BUTA]["price"]) == "6.5"
    assert (
        client.get("/api/v1/dash/wolt/comparison", params={"search": "x" * 201}).status_code == 422
    )


# ─── Юнит ────────────────────────────────────────────────────────────────────


def test_unit_runs_the_sync_as_pm():
    from pathlib import Path

    unit = Path("infra/systemd/pharmacy-monitor-wolt.service").read_text()
    assert "ExecStart=/opt/pharmacy-monitor/.venv/bin/pharmacy-monitor wolt sync\n" in unit
    assert "User=pm" in unit
    timer = Path("infra/systemd/pharmacy-monitor-wolt.timer").read_text()
    assert "Unit=pharmacy-monitor-wolt.service" in timer


# ─── Замечания разбора ───────────────────────────────────────────────────────


def test_database_failure_on_write_marks_the_attempt_failed(db_session, monkeypatch):
    """Каталог уходит в базу на commit; его отказ — сбой сбора, а не «running»."""
    wolt.store_catalog(db_session, BUTA, [_offer("a", "A", 1.0)])
    db_session.commit()
    transport, _ = _catalog_transport(
        [{"slug": "x", "subcategories": []}], {"x": [[_raw("b", "B", 100)]]}
    )

    def store_a_duplicate(session, venue, offers, **kwargs):
        session.add(storage.WoltItem(venue=venue, item_id="a", name="dup", price=1.0))
        return {"new": 1, "price_changed": 0, "gone": 0}

    monkeypatch.setattr(wolt, "store_catalog", store_a_duplicate)

    attempt = wolt.sync_venue(db_session, BUTA, client=_client(transport), sleep=lambda _s: None)

    assert attempt.status == "failed"
    assert attempt.finished_at is not None
    assert attempt.error == "IntegrityError"
    assert set(_items(db_session, BUTA)) == {"a"}


def test_allow_shrink_writes_a_smaller_catalog(db_session):
    db_session.add(storage.WoltScrape(venue=BUTA, status="ok", items=10, finished_at=utcnow()))
    db_session.commit()

    wolt.store_catalog(db_session, BUTA, [_offer("a", "A", 1.0)], allow_shrink=True)
    db_session.commit()

    assert set(_items(db_session, BUTA)) == {"a"}


def test_repeated_page_token_fails_instead_of_looping():
    def handler(request: httpx.Request) -> httpx.Response:
        if urlsplit(str(request.url)).path.endswith("/assortment"):
            return httpx.Response(200, json={"categories": [{"slug": "x"}]})
        return httpx.Response(
            200, json={"items": [_raw("a", "A", 100)], "metadata": {"next_page_token": "same"}}
        )

    with pytest.raises(wolt.WoltScrapeError, match="page_token_invalid"):
        wolt.fetch_venue(BUTA, client=_client(httpx.MockTransport(handler)), sleep=lambda _s: None)


def test_metadata_that_is_not_an_object_ends_the_category():
    def handler(request: httpx.Request) -> httpx.Response:
        if urlsplit(str(request.url)).path.endswith("/assortment"):
            return httpx.Response(200, json={"categories": [{"slug": "x"}]})
        return httpx.Response(200, json={"items": [_raw("a", "A", 100)], "metadata": []})

    offers = wolt.fetch_venue(
        BUTA, client=_client(httpx.MockTransport(handler)), sleep=lambda _s: None
    )

    assert [o.item_id for o in offers] == ["a"]


def test_out_of_stock_offer_is_shown_but_not_compared(db_session):
    cheap_but_gone = _item(BUTA, "1", "Ketonal Forte N20", 5.0)
    cheap_but_gone.in_stock = False
    _seed(
        db_session,
        cheap_but_gone,
        _item(ALOE, "2", "Ketonal Forte N20", 6.5),
        _item(KANON, "3", "Ketonal Forte N20", 7.0),
    )

    (row,) = wolt.comparison(db_session)["rows"]

    assert row["min_price"] == 6.5
    assert row["cheapest"] == [ALOE]
    assert row["prices"][BUTA]["in_stock"] is False


def test_equal_prices_have_no_cheapest(db_session):
    _seed(
        db_session,
        _item(BUTA, "1", "Enfekderm 15 q", 7.49),
        _item(ALOE, "2", "Enfekderm 15 q", 7.49),
    )

    (row,) = wolt.comparison(db_session)["rows"]

    assert row["cheapest"] == []


def test_search_by_dose_alone_still_filters(db_session):
    _seed(
        db_session,
        _item(BUTA, "1", "Duspatalin 200 mq N30", 17.0),
        _item(KANON, "2", "Duspatalin 200 mq N30", 17.5),
        _item(BUTA, "3", "Betadin 10% 30 ml", 6.6),
        _item(ALOE, "4", "Betadin 10% 30 ml", 9.3),
    )

    assert [r["name"] for r in wolt.comparison(db_session, search="200 mq")["rows"]] == [
        "Duspatalin 200 mq N30"
    ]


def test_long_name_is_cut_to_the_column():
    assert len(wolt.parse_item(_raw("a", "X" * 900, 100)).name) == 500


def test_api_is_absent_in_another_profile(client, monkeypatch):
    from src import site_profile

    monkeypatch.setattr(site_profile, "is_pharmacy", lambda: False)
    monkeypatch.setitem(
        api_module.app.dependency_overrides, api_module.require_user, lambda: object()
    )

    assert client.get("/api/v1/dash/wolt/comparison").status_code == 404


# ─── Замечания архитектора ──────────────────────────────────────────────────


def _price_log(db, venue):
    db.expire_all()
    return [
        (row.item_id, row.price, row.original_price, row.in_stock)
        for row in db.scalars(
            select(storage.WoltPriceChange)
            .where(storage.WoltPriceChange.venue == venue)
            .order_by(storage.WoltPriceChange.id)
        )
    ]


def test_price_log_records_appearance_and_changes_only(db_session):
    wolt.store_catalog(db_session, BUTA, [_offer("a", "A", 1.0), _offer("b", "B", 2.0)])
    db_session.commit()
    # Ничего не изменилось — новых строк нет.
    wolt.store_catalog(db_session, BUTA, [_offer("a", "A", 1.0), _offer("b", "B", 2.0)])
    db_session.commit()
    wolt.store_catalog(
        db_session,
        BUTA,
        [_offer("a", "A", 1.5), _offer("b", "B", 2.0, in_stock=False)],
    )
    db_session.commit()

    assert _price_log(db_session, BUTA) == [
        ("a", 1.0, None, True),
        ("b", 2.0, None, True),
        ("a", 1.5, None, True),
        ("b", 2.0, None, False),
    ]


def test_killed_attempt_is_closed_by_the_next_sync(db_session):
    from datetime import timedelta

    killed = storage.WoltScrape(
        venue=BUTA, status="running", started_at=utcnow() - timedelta(hours=3)
    )
    recent = storage.WoltScrape(venue=ALOE, status="running", started_at=utcnow())
    db_session.add_all([killed, recent])
    db_session.commit()
    transport, _ = _catalog_transport(
        [{"slug": "x", "subcategories": []}], {"x": [[_raw("a", "A", 100)]]}
    )

    wolt.sync_venue(db_session, BUTA, client=_client(transport), sleep=lambda _s: None)

    db_session.expire_all()
    assert (killed.status, killed.error) == ("failed", "interrupted")
    assert killed.finished_at is not None
    # Чужая аптека и свежая попытка не трогаются.
    assert recent.status == "running"


def test_allow_shrink_needs_a_venue():
    from click.testing import CliRunner

    from src import main as main_mod

    result = CliRunner().invoke(main_mod.cli, ["wolt", "sync", "--allow-shrink"])

    assert result.exit_code != 0
    assert "--allow-shrink needs --venue" in result.output


# ─── Пояснения для клиента ──────────────────────────────────────────────────


def test_pair_summary_explains_why_few_prices_differ(db_session):
    _seed(
        db_session,
        _item(BUTA, "1", "Novofan №60 Tabletlər", 55.8),
        _item(ALOE, "2", "Novofan №60 Tabletlər", 55.8),
        _item(KANON, "3", "Novofan N60", 59.2),
        _item(BUTA, "4", "Keratodrop 10 ml", 17.0),
        _item(ALOE, "5", "Keratodrop 10 ml", 17.0),
        _item(ALOE, "6", "Concor 5 mq № 30", 13.0),
        _item(KANON, "7", "Concor 5 mq N30", 12.0),
    )

    pairs = {(p["a"], p["b"]): p for p in wolt.comparison(db_session)["pairs"]}

    assert pairs[(BUTA, ALOE)] == {
        "a": BUTA,
        "b": ALOE,
        "common": 2,
        "same_price": 2,
        "a_cheaper": 0,
        "b_cheaper": 0,
    }
    assert pairs[(ALOE, KANON)]["common"] == 2
    assert (pairs[(ALOE, KANON)]["a_cheaper"], pairs[(ALOE, KANON)]["b_cheaper"]) == (1, 1)
    assert pairs[(BUTA, KANON)]["a_cheaper"] == 1


def test_out_of_stock_items_are_not_counted_in_pairs(db_session):
    gone = _item(KANON, "2", "Keratodrop 10 ml", 18.0)
    gone.in_stock = False
    _seed(db_session, _item(BUTA, "1", "Keratodrop 10 ml", 17.0), gone)

    pairs = {(p["a"], p["b"]): p for p in wolt.comparison(db_session)["pairs"]}

    assert pairs[(BUTA, KANON)]["common"] == 0


def test_announced_time_matches_the_timer():
    from pathlib import Path

    timer = Path("infra/systemd/pharmacy-monitor-wolt.timer").read_text()
    assert f"OnCalendar=*-*-* {wolt.DAILY_SYNC_UTC_HOUR:02d}:00:00 UTC" in timer
