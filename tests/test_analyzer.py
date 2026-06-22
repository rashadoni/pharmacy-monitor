"""Тесты diff-анализа: изменения цен, новые товары, undercuts."""

from datetime import timedelta
from src._time import utcnow

from src import analyzer, storage


def _add_product(s, site: str, name: str, ext_id: str, canonical_id=None):
    p = storage.Product(
        site=site,
        external_id=ext_id,
        url=f"http://{site}.az/p/{ext_id}",
        name=name,
        name_normalized=name.lower(),
        canonical_id=canonical_id,
    )
    s.add(p)
    s.flush()
    return p


def _add_run(s, started_at, status="ok") -> storage.Run:
    r = storage.Run(started_at=started_at, status=status, finished_at=started_at)
    s.add(r)
    s.flush()
    return r


def _add_snapshot(s, run, product, price, discount_price=None, is_on_sale=False):
    snap = storage.PriceSnapshot(
        run_id=run.id,
        product_id=product.id,
        price=price,
        discount_price=discount_price,
        is_on_sale=is_on_sale,
    )
    s.add(snap)
    s.flush()
    return snap


def test_first_run_no_diff(db_session):
    run = _add_run(db_session, utcnow())
    p = _add_product(db_session, "aloe", "Foo", "1")
    _add_snapshot(db_session, run, p, 10.0)
    db_session.commit()

    report = analyzer.analyze(db_session, run.id)
    assert report.prev_run_id is None
    assert report.price_changes == []
    assert report.new_products == []


def test_price_change_detected(db_session):
    yesterday = utcnow() - timedelta(days=1)
    today = utcnow()
    run1 = _add_run(db_session, yesterday)
    run2 = _add_run(db_session, today)
    p = _add_product(db_session, "aloe", "Foo 100mg", "1")
    _add_snapshot(db_session, run1, p, 10.0)
    _add_snapshot(db_session, run2, p, 8.0, discount_price=8.0, is_on_sale=True)
    db_session.commit()

    report = analyzer.analyze(db_session, run2.id)
    assert len(report.price_changes) == 1
    change = report.price_changes[0]
    assert change.prev_price == 10.0
    assert change.curr_price == 8.0
    assert change.delta_pct == -20.0


def test_new_product_detected(db_session):
    yesterday = utcnow() - timedelta(days=1)
    today = utcnow()
    run1 = _add_run(db_session, yesterday)
    run2 = _add_run(db_session, today)
    old = _add_product(db_session, "aloe", "Old", "1")
    new = _add_product(db_session, "aloe", "New SKU", "2")
    _add_snapshot(db_session, run1, old, 10.0)
    _add_snapshot(db_session, run2, old, 10.0)
    _add_snapshot(db_session, run2, new, 5.0)
    db_session.commit()

    report = analyzer.analyze(db_session, run2.id)
    assert len(report.new_products) == 1
    assert report.new_products[0].name == "New SKU"


def test_undercut_detected(db_session):
    """Конкурент дешевле клиента → попадает в undercuts."""
    m = storage.Match(canonical_name="Foo", confidence=1.0)
    db_session.add(m)
    db_session.flush()

    client = _add_product(db_session, "pharmonline", "Foo", "ph-1", canonical_id=m.id)
    comp = _add_product(db_session, "aloe", "Foo", "al-1", canonical_id=m.id)
    run = _add_run(db_session, utcnow())
    _add_snapshot(db_session, run, client, 100.0)
    _add_snapshot(db_session, run, comp, 80.0)
    db_session.commit()

    report = analyzer.analyze(db_session, run.id)
    assert len(report.undercuts) == 1
    u = report.undercuts[0]
    assert u.competitor_site == "aloe"
    assert u.client_price == 100.0
    assert u.competitor_price == 80.0
    assert u.diff_pct == 20.0


def test_diff_only_detects_change_across_skip_run(db_session):
    """run1: price=10. run2: цена не менялась → snapshot НЕ записан.
    run3: price=12 → snapshot записан.

    После diff-only analyzer должен корректно показать изменение
    10→12 в run3, использовав snapshot из run1 как prev (НЕ из run2,
    в котором snapshot для этого продукта отсутствует).
    """
    day1 = utcnow() - timedelta(days=2)
    day2 = utcnow() - timedelta(days=1)
    day3 = utcnow()
    run1 = _add_run(db_session, day1)
    run2 = _add_run(db_session, day2)
    run3 = _add_run(db_session, day3)

    p = _add_product(db_session, "aloe", "Foo 100mg", "1")
    s1 = _add_snapshot(db_session, run1, p, 10.0)
    s1.captured_at = day1
    # run2: цена осталась 10, но diff-only НЕ записал snapshot — имитируем
    s3 = _add_snapshot(db_session, run3, p, 12.0)
    s3.captured_at = day3
    db_session.commit()

    report = analyzer.analyze(db_session, run3.id)
    assert len(report.price_changes) == 1, (
        "Изменение 10→12 должно быть обнаружено даже без snapshot'а в run2"
    )
    change = report.price_changes[0]
    assert change.prev_price == 10.0
    assert change.curr_price == 12.0
    assert change.delta_pct == 20.0


def test_diff_only_unchanged_product_is_not_a_change(db_session):
    """В curr_run у продукта НЕТ snapshot'а (цена не менялась) →
    в price_changes он не попадает.
    """
    day1 = utcnow() - timedelta(days=1)
    day2 = utcnow()
    run1 = _add_run(db_session, day1)
    run2 = _add_run(db_session, day2)

    stable = _add_product(db_session, "aloe", "Stable", "1")
    changed = _add_product(db_session, "aloe", "Changed", "2")

    s1a = _add_snapshot(db_session, run1, stable, 10.0)
    s1a.captured_at = day1
    s1b = _add_snapshot(db_session, run1, changed, 20.0)
    s1b.captured_at = day1
    # run2: только changed получил snapshot
    s2 = _add_snapshot(db_session, run2, changed, 25.0)
    s2.captured_at = day2
    db_session.commit()

    report = analyzer.analyze(db_session, run2.id)
    assert len(report.price_changes) == 1
    assert report.price_changes[0].product_name == "Changed"
    # stable не упомянут — у него snapshot'а в run2 нет
    assert all(c.product_name != "Stable" for c in report.price_changes)


def test_diff_only_undercut_uses_latest_snapshot_globally(db_session):
    """Конкурент дешевле клиента, но снапшот его — из run1, не run2 →
    undercut всё равно должен сработать (latest snapshot globally).
    """
    m = storage.Match(canonical_name="Foo", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    client = _add_product(db_session, "pharmonline", "Foo", "ph-1", canonical_id=m.id)
    comp = _add_product(db_session, "aloe", "Foo", "al-1", canonical_id=m.id)

    day1 = utcnow() - timedelta(days=1)
    day2 = utcnow()
    run1 = _add_run(db_session, day1)
    run2 = _add_run(db_session, day2)

    # run1: client=100, comp=80 (snapshots от обоих)
    sc = _add_snapshot(db_session, run1, client, 100.0)
    sc.captured_at = day1
    scc = _add_snapshot(db_session, run1, comp, 80.0)
    scc.captured_at = day1
    # run2: ничего не менялось → нет snapshot'ов в run2
    db_session.commit()

    report = analyzer.analyze(db_session, run2.id)
    # undercut должен найти client=100, comp=80 из run1 (latest globally)
    assert len(report.undercuts) == 1
    u = report.undercuts[0]
    assert u.client_price == 100.0
    assert u.competitor_price == 80.0


def test_undercut_skips_obvious_data_error_prices(db_session):
    """Конкурент с ценой < 10% клиента — пропускаем как data error.

    Регрессия 2026-05-11: aptekonline.az имеет SKU с typo ценой 0.20 ₼
    при реальной цене ~28 ₼ на других сайтах. Эти fake undercuts
    замусоривали /comparison и алерты. Фильтр в _detect_undercuts.
    """
    m = storage.Match(canonical_name="Foo", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    client = _add_product(db_session, "pharmonline", "Foo", "ph-1", canonical_id=m.id)
    comp_bad = _add_product(db_session, "aloe", "Foo", "al-1", canonical_id=m.id)
    comp_good = _add_product(db_session, "aptekonline", "Foo", "apt-1", canonical_id=m.id)
    run = _add_run(db_session, utcnow())
    _add_snapshot(db_session, run, client, 28.0)
    _add_snapshot(db_session, run, comp_bad, 0.20)  # data error — должен пропуститься
    _add_snapshot(db_session, run, comp_good, 25.0)  # legitimate undercut
    db_session.commit()

    report = analyzer.analyze(db_session, run.id)
    sites = {u.competitor_site for u in report.undercuts}
    assert sites == {"aptekonline"}, (
        f"Должен попасть только aptekonline (25₼ < 28₼). aloe 0.20₼ — outlier. Got: {sites}"
    )


def test_undercut_zero_client_price_no_zerodiv(db_session):
    """Регресс #311 (2026-06-22): клиент с ценой 0.0 (нет в наличии) ронял ВЕСЬ
    прогон через ZeroDivisionError в _detect_undercuts (client_price в знаменателе
    diff_pct). Теперь 0-цена клиента пропускается — без краха, без undercut.
    """
    m = storage.Match(canonical_name="ZeroFoo", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    client = _add_product(db_session, "pharmonline", "ZeroFoo", "ph-z", canonical_id=m.id)
    comp = _add_product(db_session, "aloe", "ZeroFoo", "al-z", canonical_id=m.id)
    run = _add_run(db_session, utcnow())
    _add_snapshot(db_session, run, client, 0.0)  # клиент: цена 0 = нет в наличии
    _add_snapshot(db_session, run, comp, 80.0)
    db_session.commit()

    # До фикса: ZeroDivisionError здесь ронял весь run_cmd.
    report = analyzer.analyze(db_session, run.id)
    assert report.undercuts == []  # 0-цена клиента не сравнивается


def test_price_change_zero_prev_price_no_zerodiv(db_session):
    """Регресс того же класса: prev_price=0 (restock из нуля) ронял
    _detect_price_changes (prev_price в знаменателе delta_pct). Теперь — пропуск.
    """
    day1 = utcnow() - timedelta(days=1)
    day2 = utcnow()
    run1 = _add_run(db_session, day1)
    run2 = _add_run(db_session, day2)
    p = _add_product(db_session, "aloe", "RestockFoo", "rf-1")
    s1 = _add_snapshot(db_session, run1, p, 0.0)  # был 0 (нет в наличии)
    s1.captured_at = day1
    s2 = _add_snapshot(db_session, run2, p, 5.0)  # появилась цена
    s2.captured_at = day2
    db_session.commit()

    report = analyzer.analyze(db_session, run2.id)  # НЕ должно бросать
    assert report.price_changes == []  # restock из нуля — не % изменение


def test_no_undercut_when_competitor_more_expensive(db_session):
    m = storage.Match(canonical_name="Foo", confidence=1.0)
    db_session.add(m)
    db_session.flush()
    client = _add_product(db_session, "pharmonline", "Foo", "ph-1", canonical_id=m.id)
    comp = _add_product(db_session, "aloe", "Foo", "al-1", canonical_id=m.id)
    run = _add_run(db_session, utcnow())
    _add_snapshot(db_session, run, client, 80.0)
    _add_snapshot(db_session, run, comp, 100.0)
    db_session.commit()

    report = analyzer.analyze(db_session, run.id)
    assert report.undercuts == []
