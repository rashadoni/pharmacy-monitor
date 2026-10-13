"""Мерка матчера «строка пропала с сайта» не следует за суточным ритмом сбора.

Порог был «два цикла сбора с запасом»: при недельном ритме — 342 часа. С
переходом на ежедневный сбор (2026-10-13) та же формула дала бы 54 часа: товар,
которого двое суток нет в листинге, считался бы пропавшим, и его место в паре
уходило бы живому двойнику с тем же названием. Сайты убирают из листинга товар,
которого временно нет в продаже, — это не «пропал». Тесты матчера на «давно не
видели» ставят сто дней и нижней границы не замечают; её держит этот файл.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src import matcher
from src.cadence import CADENCE_GRACE_HOURS, SITE_SCRAPE_CADENCE_HOURS, site_cadence_hours

NOW = datetime(2026, 10, 20, 5, 0)
SITES = ("pharmonline", "aptekonline", "aloe")


def _row(site: str, *, hours_unseen: float) -> SimpleNamespace:
    return SimpleNamespace(
        site=site,
        last_seen_at=NOW - timedelta(hours=hours_unseen),
        url_dead_at=None,
    )


def _is_gone(site: str, *, hours_unseen: float) -> bool:
    row = _row(site, hours_unseen=hours_unseen)
    # Остальной каталог сайта виден прошлой ночью: сайт собирается полностью.
    catalog = [_row(site, hours_unseen=5) for _ in range(9)]
    is_stale, fully_scraped = matcher._gone_row_rules([*catalog, row], now=NOW)
    assert site in fully_scraped
    return is_stale(row)


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("days_unseen", [3, 10, 14])
def test_row_missing_from_several_nightly_listings_is_not_gone(site, days_unseen):
    assert site_cadence_hours(site) == 24
    assert not _is_gone(site, hours_unseen=days_unseen * 24)


@pytest.mark.parametrize("site", SITES)
def test_row_is_gone_past_two_weeks_with_the_margin(site):
    assert matcher._STALE_MEMBER_MIN_HOURS == 342
    assert not _is_gone(site, hours_unseen=341)
    assert _is_gone(site, hours_unseen=343)


@pytest.mark.parametrize("site", SITES)
def test_threshold_is_the_one_the_weekly_rhythm_had(site, monkeypatch):
    """Смена ритма в любую сторону не меняет, какие строки матчер считает пропавшими."""
    assert (
        matcher._STALE_MEMBER_CYCLES * 168 + CADENCE_GRACE_HOURS == matcher._STALE_MEMBER_MIN_HOURS
    )
    monkeypatch.setitem(SITE_SCRAPE_CADENCE_HOURS, site, 168)

    assert not _is_gone(site, hours_unseen=341)
    assert _is_gone(site, hours_unseen=343)


def test_site_collected_less_often_than_weekly_still_gets_two_cycles(monkeypatch):
    """Нижняя граница — не замена правилу «два цикла»: редкому сайту порог длиннее."""
    monkeypatch.setitem(SITE_SCRAPE_CADENCE_HOURS, "aloe", 336)

    assert not _is_gone("aloe", hours_unseen=600)
    assert _is_gone("aloe", hours_unseen=2 * 336 + CADENCE_GRACE_HOURS + 1)


def test_unseen_site_is_not_judged_at_all():
    """Сайт, который полностью не собирался, пропавших строк не имеет — прежнее правило."""
    rows = [_row("aloe", hours_unseen=400) for _ in range(10)]

    _is_stale, fully_scraped = matcher._gone_row_rules(rows, now=NOW)

    assert "aloe" not in fully_scraped
