"""Матчер не следует за суточным ритмом сбора — ни в «строка пропала», ни в выборе пары.

Две мерки матчера были привязаны к ритму, и переход на ежедневный сбор
(2026-10-13) сжал бы обе всемеро:

- «строка пропала с сайта» — «два цикла сбора с запасом», при недельном ритме
  342 часа, при суточном вышло бы 54: товар, которого двое суток нет в листинге,
  считался бы пропавшим, и его место в паре уходило бы живому двойнику;
- «предложение сейчас в продаже» при выборе между одноимёнными строками сайта —
  срок политики, 174 часа при недельном ритме и 30 при суточном: строка, которой
  две ночи нет в листинге, проигрывала бы соседней с тем же названием, хотя цена
  совпадает именно у неё.

Сайты убирают из листинга товар, которого временно нет в продаже, — это не
«пропал» и не «другой товар». Остальные тесты матчера ставят «давно» на сто
дней и этих границ не замечают; их держит этот файл.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src import matcher, product_policy, storage
from src._time import utcnow
from src.cadence import CADENCE_GRACE_HOURS, SITE_SCRAPE_CADENCE_HOURS, site_cadence_hours
from src.normalize import extract_dosage, extract_pack_size, normalize_name

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


# ─── Выбор между одноимёнными строками сайта ─────────────────────────────────


def _catalog_product(session, run, site, name, external_id, *, hours_unseen, price):
    now = utcnow()
    product = storage.Product(
        tenant_id=1,
        site=site,
        external_id=external_id,
        url=f"https://{site}.example/{external_id}",
        name=name,
        name_normalized=normalize_name(name),
        brand=name.split()[0],
        dosage=extract_dosage(name),
        pack_size=extract_pack_size(name),
        offer_availability_status="in_stock",
        availability_observed_at=now - timedelta(hours=hours_unseen),
        last_seen_at=now - timedelta(hours=hours_unseen),
    )
    session.add(product)
    session.flush()
    session.add(
        storage.PriceSnapshot(
            run_id=run.id,
            product_id=product.id,
            price=price,
            captured_at=now - timedelta(hours=60),
        )
    )
    return product


def _pair_of_the_client_product(session, *, same_price_row_first: bool) -> list[str]:
    """Товар клиента 10.00; у конкурента две одноимённые строки: «same-price» стоит
    10.00, но её две ночи нет в листинге (48 ч), «other-price» стоит 14.00 и видна
    прошлой ночью. Возвращает, кто из двух оказался в паре с товаром клиента."""
    now = utcnow()
    run = storage.Run(
        tenant_id=1,
        status="ok",
        started_at=now - timedelta(hours=60),
        finished_at=now - timedelta(hours=59),
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline",
        catalog_verified=True,
        run_quality={"financially_eligible": True},
    )
    session.add(run)
    session.flush()
    anchor = _catalog_product(
        session,
        run,
        "pharmonline",
        "Polioksidonium 12 mq № 10",
        "client",
        hours_unseen=5,
        price=10.0,
    )
    rivals = [("same-price", 48, 10.0), ("other-price", 5, 14.0)]
    if not same_price_row_first:
        rivals.reverse()
    rows = {
        key: _catalog_product(
            session,
            run,
            "aptekonline",
            "Polioksidonium 12 mq N10",
            key,
            hours_unseen=hours_unseen,
            price=price,
        )
        for key, hours_unseen, price in rivals
    }
    # Остальной каталог конкурента виден прошлой ночью: сайт собирается полностью.
    for i in range(6):
        _catalog_product(
            session,
            run,
            "aptekonline",
            f"Filler{i} 5 mq N{i + 10}",
            f"filler-{i}",
            hours_unseen=5,
            price=3.0 + i,
        )
    session.commit()

    matcher.match_products(session)
    session.commit()

    session.refresh(anchor)
    paired = []
    for key, row in rows.items():
        session.refresh(row)
        if anchor.canonical_id is not None and row.canonical_id == anchor.canonical_id:
            paired.append(key)
    assert product_policy.offer_is_fresh(rows["same-price"]) is False  # срок политики — 30 ч
    return paired


@pytest.mark.parametrize("same_price_row_first", [True, False])
def test_row_missing_from_two_nightly_listings_still_wins_by_price(
    db_session, same_price_row_first
):
    """В паре остаётся строка с ценой клиента, а не соседняя с тем же названием.

    Срок политики для суточного сайта — 30 часов, и строку, которой две ночи нет
    в листинге, политика свежей не считает. Матчер судит своей меркой: иначе в
    паре оказался бы товар за 14.00, и клиент увидел бы разницу цен, которой нет.
    От порядка строк в базе исход не зависит.
    """
    assert site_cadence_hours("aptekonline") == 24

    paired = _pair_of_the_client_product(db_session, same_price_row_first=same_price_row_first)

    assert paired == ["same-price"]


def test_matcher_patience_for_a_current_offer_is_the_weekly_rhythm_one(monkeypatch):
    """174 часа — срок политики при недельном ритме; при суточной карте он тот же."""
    assert matcher._OFFER_FRESH_MIN_HOURS == 168 + CADENCE_GRACE_HOURS

    def offer(hours_unseen: float) -> SimpleNamespace:
        return SimpleNamespace(
            site="aloe",
            url_dead_at=None,
            offer_availability_status="in_stock",
            availability_observed_at=utcnow() - timedelta(hours=hours_unseen),
        )

    assert matcher._current_offer(offer(173)).eligible is True
    assert matcher._current_offer(offer(175)).eligible is False
    # Сайту, который собирают реже раза в неделю, срок длиннее — по его ритму.
    monkeypatch.setitem(SITE_SCRAPE_CADENCE_HOURS, "aloe", 336)
    assert matcher._current_offer(offer(300)).eligible is True
