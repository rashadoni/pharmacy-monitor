"""Разделы сайта для полного сбора: сверка с сайтом и чтение прогоном."""

from __future__ import annotations

from datetime import datetime

import pytest

from src import site_routes, watchlist
from src.storage import Category, SiteRoute


def _routes(session, site="irshad", active_only=False):
    rows = session.query(SiteRoute).filter(SiteRoute.site == site).order_by(SiteRoute.id).all()
    return [(row.slug, row.is_active) for row in rows if row.is_active or not active_only]


def _sync(session, site, discovered, **kwargs):
    plan = site_routes.plan_route_sync(session, site, discovered)
    site_routes.apply_route_sync(session, plan, **kwargs)
    session.commit()
    return plan


def test_first_sync_writes_every_section_in_site_order(db_session):
    plan = _sync(db_session, "irshad", ["telefonlar", "tv-ve-audio", "telefonlar", " ", ""])
    assert plan.added == ["telefonlar", "tv-ve-audio"]
    assert plan.missing == [] and plan.returned == [] and plan.unchanged == []
    assert _routes(db_session) == [("telefonlar", True), ("tv-ve-audio", True)]


def test_section_gone_from_the_site_is_switched_off_not_deleted(db_session):
    _sync(db_session, "irshad", ["a", "b", "c", "d", "e", "f"], now=datetime(2026, 11, 1))
    plan = _sync(db_session, "irshad", ["a", "b", "c", "d", "e", "g"], now=datetime(2026, 11, 8))
    assert (plan.added, plan.missing, plan.unchanged) == (["g"], ["f"], ["a", "b", "c", "d", "e"])
    assert _routes(db_session) == [
        ("a", True),
        ("b", True),
        ("c", True),
        ("d", True),
        ("e", True),
        ("f", False),
        ("g", True),
    ]
    gone = db_session.query(SiteRoute).filter_by(site="irshad", slug="f").one()
    kept = db_session.query(SiteRoute).filter_by(site="irshad", slug="a").one()
    assert gone.last_seen_at == datetime(2026, 11, 1)
    assert kept.last_seen_at == datetime(2026, 11, 8)
    assert kept.first_seen_at == datetime(2026, 11, 1)

    # Раздел вернулся на сайт — включается та же строка, второй не заводится.
    plan = _sync(db_session, "irshad", ["a", "b", "c", "d", "e", "f", "g"])
    assert plan.returned == ["f"] and plan.added == []
    assert len(_routes(db_session)) == 7
    assert all(active for _, active in _routes(db_session))


def test_empty_discovery_is_never_written(db_session):
    _sync(db_session, "irshad", ["a", "b"])
    for allow_shrink in (False, True):
        plan = site_routes.plan_route_sync(db_session, "irshad", [])
        assert plan.refusal() == "site returned no sections"
        with pytest.raises(site_routes.RouteSyncRefused):
            site_routes.apply_route_sync(db_session, plan, allow_shrink=allow_shrink)
    assert _routes(db_session) == [("a", True), ("b", True)]


def test_many_sections_gone_at_once_need_an_explicit_word(db_session):
    _sync(db_session, "irshad", [f"s{i}" for i in range(10)])

    # Двух из десяти (ровно пятая часть) хватает без разрешения, трёх — нет.
    plan = site_routes.plan_route_sync(db_session, "irshad", [f"s{i}" for i in range(8)])
    assert plan.refusal() is None

    plan = site_routes.plan_route_sync(db_session, "irshad", [f"s{i}" for i in range(7)])
    assert "3 of 10" in plan.refusal()
    with pytest.raises(site_routes.RouteSyncRefused):
        site_routes.apply_route_sync(db_session, plan)
    db_session.rollback()
    assert all(active for _, active in _routes(db_session))

    site_routes.apply_route_sync(db_session, plan, allow_shrink=True)
    db_session.commit()
    assert [slug for slug, active in _routes(db_session) if not active] == ["s7", "s8", "s9"]


def test_sync_of_one_site_does_not_touch_another(db_session):
    _sync(db_session, "irshad", ["a", "b"])
    _sync(db_session, "bakuelectronics", ["a", "x"])
    plan = _sync(db_session, "irshad", ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"])
    assert plan.missing == []
    assert _routes(db_session, "bakuelectronics") == [("a", True), ("x", True)]


def test_run_reads_active_sections_of_a_site_without_a_categories_column(db_session, monkeypatch):
    monkeypatch.setattr(watchlist, "SITES", ("kontakt", "irshad", "bakuelectronics"))
    _sync(db_session, "irshad", ["a", "b", "c", "d", "e", "f"])
    _sync(db_session, "irshad", ["a", "b", "c", "d", "e"])
    _sync(db_session, "bakuelectronics", ["x"])

    assert watchlist.categories_for_site(db_session, "irshad") == ["a", "b", "c", "d", "e"]
    assert watchlist.categories_for_site(db_session, "bakuelectronics") == ["x"]
    assert watchlist.categories_for_site(db_session, "kontakt") == []
    # Связки с категорией у раздела нет: прогон одной категории его не касается.
    assert watchlist.categories_for_site(db_session, "irshad", only_category_id=1) == []
    # Сайт не из профиля экземпляра не собирается, что бы ни лежало в таблице.
    _sync(db_session, "aloe-like", ["z"])
    assert watchlist.categories_for_site(db_session, "aloe-like") == []


def test_pharmacy_sites_still_read_their_sections_from_categories(db_session):
    # Строки site_routes аптечному сайту не разделы: его список — в categories.
    db_session.add(
        Category(
            key="k", label_ru="К", pharmonline_slug="ph", aptekonline_slug="ap", aloe_slug="al"
        )
    )
    db_session.add(Category(key="off", label_ru="Выкл", aloe_slug="skip", is_active=False))
    db_session.commit()
    _sync(db_session, "aloe", ["from-routes"])

    assert watchlist.categories_for_site(db_session, "pharmonline") == ["ph"]
    assert watchlist.categories_for_site(db_session, "aptekonline") == ["ap"]
    assert watchlist.categories_for_site(db_session, "aloe") == ["al"]
