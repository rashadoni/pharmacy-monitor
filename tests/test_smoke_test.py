"""Тесты smoke_test_per_site_coverage — site_drop detection после persist'а.

Регрессии:
- 2026-05-11: AlertEvent(run_id=...) — несуществующий аргумент, падало на каждый
  fire. Зафикшено: run_id зашит в dedup_key.
- 2026-05-11: products_per_site баг — для multi-site Mac прогонов (pharmonline +
  aptekonline) `products_scraped` это total на оба сайта. Сравнение per-site
  baseline неверно — нужен per-site счётчик. Добавлен `Run.products_per_site`.
"""

from datetime import timedelta

from sqlalchemy import select

from src import storage
from src._time import utcnow
from src.main import _smoke_test_per_site_coverage
from src.scrapers.base import ScrapedProduct, ScrapeResult


def _make_product(site, ext_id, name="P", price=10.0):
    return ScrapedProduct(
        site=site, external_id=ext_id,
        url=f"http://{site}/{ext_id}", name=name, price=price,
    )


def _add_run_with_per_site(s, started_at, products_per_site, sites_completed):
    r = storage.Run(
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=10),
        status="ok",
        products_scraped=sum(products_per_site.values()),
        products_per_site=products_per_site,
        sites_completed=sites_completed,
    )
    s.add(r)
    s.flush()
    return r


def test_smoke_test_uses_per_site_baseline(db_session):
    """Multi-site Mac runs: smoke_test берёт per-site счётчик, не total.

    Без products_per_site сравнение pharmonline-current=100 vs baseline=
    avg(100K Mac runs) = 100K даст ratio=0.001 → ложный алерт.
    """
    base = utcnow()
    # 3 исторических Mac multi-site runs: aptekonline 90000, pharmonline 9000
    for d in [5, 3, 1]:
        _add_run_with_per_site(
            db_session,
            base - timedelta(days=d),
            {"pharmonline": 9000, "aptekonline": 90000},
            "pharmonline,aptekonline",
        )

    # Текущий run: pharmonline собрал только 100 (сломался)
    curr = storage.Run(
        started_at=base,
        status="running",
        sites_completed="pharmonline,aptekonline",
    )
    db_session.add(curr)
    db_session.flush()

    results = [
        ScrapeResult(
            site="pharmonline",
            products=[_make_product("pharmonline", str(i)) for i in range(100)],
        ),
        ScrapeResult(
            site="aptekonline",
            products=[_make_product("aptekonline", str(i)) for i in range(90000)],
        ),
    ]
    _smoke_test_per_site_coverage(db_session, results, curr, drop_threshold=0.5)

    # Должен сработать site_drop для pharmonline (100 vs 9000), НЕ для aptekonline.
    events = db_session.scalars(
        select(storage.AlertEvent).where(storage.AlertEvent.rule_type == "site_drop_smoke")
    ).all()
    assert len(events) == 1, "Только pharmonline должен триггернуть alert"
    assert "pharmonline" in events[0].title
    assert events[0].payload["current"] == 100
    assert events[0].payload["avg"] == 9000


def test_smoke_test_no_falsepositive_when_per_site_normal(db_session):
    """Если все сайты в норме — никаких alert'ов."""
    base = utcnow()
    for d in [5, 3, 1]:
        _add_run_with_per_site(
            db_session, base - timedelta(days=d),
            {"aloe": 800}, "aloe",
        )
    curr = storage.Run(started_at=base, status="running", sites_completed="aloe")
    db_session.add(curr)
    db_session.flush()

    results = [ScrapeResult(
        site="aloe",
        products=[_make_product("aloe", str(i)) for i in range(820)],
    )]
    _smoke_test_per_site_coverage(db_session, results, curr)

    events = db_session.scalars(
        select(storage.AlertEvent).where(storage.AlertEvent.rule_type == "site_drop_smoke")
    ).all()
    assert events == []


def test_smoke_test_legacy_run_fallback_to_products_scraped(db_session):
    """Backward-compat: old runs без products_per_site → используем products_scraped.

    Имитирует период транзишена (до 2026-05-11) — runs существуют без
    per-site breakdown.
    """
    base = utcnow()
    # Legacy run: products_per_site=None (но single-site, products_scraped осмыслен)
    legacy = storage.Run(
        started_at=base - timedelta(days=2),
        finished_at=base - timedelta(days=2, hours=-1),
        status="ok",
        products_scraped=800,
        products_per_site=None,
        sites_completed="aloe",
    )
    db_session.add(legacy)
    legacy2 = storage.Run(
        started_at=base - timedelta(days=1),
        finished_at=base - timedelta(days=1, hours=-1),
        status="ok",
        products_scraped=820,
        products_per_site=None,
        sites_completed="aloe",
    )
    db_session.add(legacy2)
    db_session.flush()

    curr = storage.Run(started_at=base, status="running", sites_completed="aloe")
    db_session.add(curr)
    db_session.flush()

    # Текущий: 50 продуктов (drop с avg=810 до 50, ratio=0.06 << 0.5)
    results = [ScrapeResult(
        site="aloe",
        products=[_make_product("aloe", str(i)) for i in range(50)],
    )]
    _smoke_test_per_site_coverage(db_session, results, curr)

    events = db_session.scalars(
        select(storage.AlertEvent).where(storage.AlertEvent.rule_type == "site_drop_smoke")
    ).all()
    assert len(events) == 1, "Fallback на products_scraped должен сработать"


def test_smoke_test_does_not_crash_on_alert_event_creation(db_session):
    """Регрессия 2026-05-11: AlertEvent(run_id=…) ronyat 'invalid keyword argument'.

    После фикса конструкция должна успешно создавать AlertEvent без run_id.
    """
    base = utcnow()
    for d in [5, 3, 1]:
        _add_run_with_per_site(
            db_session, base - timedelta(days=d),
            {"aloe": 800}, "aloe",
        )
    curr = storage.Run(started_at=base, status="running", sites_completed="aloe")
    db_session.add(curr)
    db_session.flush()

    # Drop до 24 (как run_47)
    results = [ScrapeResult(
        site="aloe",
        products=[_make_product("aloe", str(i)) for i in range(24)],
    )]
    # Не должно выбрасывать ничего
    _smoke_test_per_site_coverage(db_session, results, curr)

    events = db_session.scalars(
        select(storage.AlertEvent).where(storage.AlertEvent.rule_type == "site_drop_smoke")
    ).all()
    assert len(events) == 1
    assert events[0].dedup_key == f"site_drop_smoke|run={curr.id}|site=aloe"
