"""Тесты health-check логики."""

from datetime import timedelta
from src._time import utcnow

from src.health import check_health
from src.storage import PriceSnapshot, Product, Run


def _add_run(s, started_at, status="ok", products_scraped=10):
    r = Run(
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=1),
        status=status,
        products_scraped=products_scraped,
    )
    s.add(r)
    s.flush()
    return r


def _add_snap(s, run, site, count, last_seen_at=None):
    """Создать `count` фейковых product+snapshot для site/run.

    `last_seen_at` важен для тестов health-check: визибильность продукта в
    прогоне определяется через `Product.last_seen_at >= run.started_at`
    (в проде это поле обновляется persist'ом). Без явной установки тесты
    с историческими прогонами не работают — все продукты получают
    last_seen_at=NOW и считаются видимыми везде.
    """
    if last_seen_at is None:
        last_seen_at = run.started_at
    for i in range(count):
        p = Product(
            site=site,
            external_id=f"p-{site}-{run.id}-{i}",
            url=f"http://x/{i}",
            name=f"P {i}",
            name_normalized=f"p {i}",
            last_seen_at=last_seen_at,
        )
        s.add(p)
        s.flush()
        s.add(PriceSnapshot(run_id=run.id, product_id=p.id, price=10.0))


def test_no_runs_returns_warning(db_session):
    rep = check_health(db_session)
    assert rep.status == "warning"
    assert any(i.code == "no_runs" for i in rep.issues)


def test_recent_ok_run_returns_ok(db_session):
    run = _add_run(db_session, utcnow() - timedelta(hours=1))
    _add_snap(db_session, run, "pharmonline", 10)
    db_session.commit()
    rep = check_health(db_session)
    assert rep.is_healthy
    assert rep.status == "ok"


def test_site_silence_critical_when_one_site_stale(db_session):
    """Per-site freshness: pharmonline молчит 9 дней (>198ч), aloe скрейпился час назад.

    `stale_run` смотрит на ПОСЛЕДНИЙ run и пропускает (aloe свежий), но per-site
    silence check должен поймать pharmonline. Реальный сценарий: Mac launchd
    уснул, aloe на проде продолжает скрейпиться. (pharmonline недельный → порог
    198ч, поэтому «молчит» = >8 суток, а не 3 дня.)
    """
    # aloe свежий
    aloe_run = _add_run(db_session, utcnow() - timedelta(hours=1))
    _add_snap(db_session, aloe_run, "aloe", 50)
    # pharmonline молчит 9 дней (> 198ч недельного порога)
    old_pharm_run = _add_run(db_session, utcnow() - timedelta(days=9))
    _add_snap(
        db_session,
        old_pharm_run,
        "pharmonline",
        50,
        last_seen_at=utcnow() - timedelta(days=9),
    )
    db_session.commit()

    rep = check_health(db_session, max_age_hours=26)
    assert rep.status == "critical"
    silent_issues = [i for i in rep.issues if i.code == "site_silent"]
    silent_sites = {i.context.get("site") for i in silent_issues}
    assert "pharmonline" in silent_sites
    assert "aloe" not in silent_sites  # aloe свежий — не должен попасть


def test_site_silence_respects_weekly_aptekonline_threshold(db_session):
    """aptekonline скрейпится РАЗ В НЕДЕЛЮ (Decodo) → порог 198ч, не суточные 26ч.

    100ч давности для aptek — норма (НЕ alert), иначе hourly health-check спамил
    бы critical 6 из 7 дней. Для aloe (суточный) те же 100ч — реальный alert.
    """
    now = utcnow()
    apt_run = _add_run(db_session, now - timedelta(hours=100))
    _add_snap(db_session, apt_run, "aptekonline", 30, last_seen_at=now - timedelta(hours=100))
    aloe_run = _add_run(db_session, now - timedelta(hours=100))
    _add_snap(db_session, aloe_run, "aloe", 30, last_seen_at=now - timedelta(hours=100))
    db_session.commit()

    rep = check_health(db_session, max_age_hours=26)
    silent_sites = {i.context.get("site") for i in rep.issues if i.code == "site_silent"}
    assert "aptekonline" not in silent_sites  # 100ч < 198ч недельного порога
    assert "aloe" in silent_sites  # 100ч > 26ч суточного порога


def test_site_silence_flags_aptekonline_past_weekly_threshold(db_session):
    """aptekonline молчит >8 дней (порог 198ч) → alert: реальный сбой Decodo/баланса
    больше не маскируется недельной частотой."""
    now = utcnow()
    apt_run = _add_run(db_session, now - timedelta(hours=210))
    _add_snap(db_session, apt_run, "aptekonline", 30, last_seen_at=now - timedelta(hours=210))
    db_session.commit()

    rep = check_health(db_session, max_age_hours=26)
    silent_sites = {i.context.get("site") for i in rep.issues if i.code == "site_silent"}
    assert "aptekonline" in silent_sites  # 210ч > 198ч → действительно молчит


def test_stale_run_critical(db_session):
    _add_run(db_session, utcnow() - timedelta(hours=48))
    db_session.commit()
    rep = check_health(db_session, max_age_hours=26)
    assert rep.status == "critical"
    assert any(i.code == "stale_run" for i in rep.issues)


def test_failed_run_critical(db_session):
    _add_run(db_session, utcnow() - timedelta(hours=1), status="failed")
    db_session.commit()
    rep = check_health(db_session)
    assert rep.status == "critical"
    assert any(i.code == "last_run_failed" for i in rep.issues)


def test_empty_ok_run_critical(db_session):
    """Run завершился ok, но 0 товаров — подозрительно."""
    _add_run(db_session, utcnow() - timedelta(hours=1), products_scraped=0)
    db_session.commit()
    rep = check_health(db_session, min_products=5)
    assert rep.status == "critical"
    assert any(i.code == "empty_run" for i in rep.issues)


def test_site_drop_warning(db_session):
    """Если за окно покрытия видели <50% живого каталога — alert."""
    base = utcnow()
    cur = _add_run(db_session, base - timedelta(hours=1), products_scraped=30)
    # Живой каталог: 100 товаров, виденных 18 дней назад (в окне свежести 21д,
    # но ВНЕ окна покрытия 14д) — недавние скрейпы их НЕ переснимали.
    old = _add_run(db_session, base - timedelta(days=18), products_scraped=100)
    _add_snap(db_session, old, "pharmonline", 100, last_seen_at=base - timedelta(days=18))
    # Недавнее покрытие: только 30 товаров за окно покрытия.
    _add_snap(db_session, cur, "pharmonline", 30, last_seen_at=base - timedelta(hours=1))
    db_session.commit()

    rep = check_health(db_session, site_drop_threshold=0.5)
    drop_issues = [i for i in rep.issues if i.code == "site_drop"]
    assert drop_issues, "Должен быть site_drop issue"
    assert drop_issues[0].context["site"] == "pharmonline"
    assert drop_issues[0].context["ratio"] < 0.5  # 30/130 = 23%


def test_site_drop_robust_to_partial_intraday_run(db_session):
    """Частичный intraday-прогон (latest) НЕ роняет метрику.

    Кейс 2026-06-16: pharmonline run_277 = 127 товаров (featured-выборка) был
    ПОСЛЕДНИМ прогоном; привязка seen к нему дала ложный 127/10333=1% critical.
    Окно покрытия включает прежний ПОЛНЫЙ прогон → seen считается от него.
    """
    base = utcnow()
    # Полный прогон 2 дня назад: 200 товаров (в окне покрытия 9д).
    full = _add_run(db_session, base - timedelta(days=2), products_scraped=200)
    _add_snap(db_session, full, "pharmonline", 200, last_seen_at=base - timedelta(days=2))
    # Частичный intraday-прогон СЕЙЧАС: всего 5 товаров (latest run).
    partial = _add_run(db_session, base - timedelta(minutes=10), products_scraped=5)
    _add_snap(db_session, partial, "pharmonline", 5, last_seen_at=base - timedelta(minutes=10))
    db_session.commit()

    rep = check_health(db_session, site_drop_threshold=0.5)
    drop = [i for i in rep.issues if i.code == "site_drop"]
    # seen(14д)=205 (200 полного + 5 частичного), total(21д)=205 → 100%, без ложного drop
    assert not drop, "Частичный intraday-прогон не должен давать ложный site_drop"


def test_site_drop_tolerates_slightly_late_weekly_run(db_session):
    """Слегка опоздавший недельный прогон (в пределах окна покрытия 14д) — не drop.

    Закрепляет выбор окна покрытия (14д > 7д каденции + запас): полный прогон
    10 дней назад ещё в окне → seen считается от него, даже если поверх идёт
    мелкий intraday-прогон. Реальный дроп (полный прогон СТАРШЕ 14д) ловит
    test_site_drop_warning; полный отказ скрейпа ловит site_silent.
    """
    base = utcnow()
    # Полный недельный прогон 10 дней назад (опоздал, но в окне покрытия 14д).
    full = _add_run(db_session, base - timedelta(days=10), products_scraped=200)
    _add_snap(db_session, full, "pharmonline", 200, last_seen_at=base - timedelta(days=10))
    # Мелкий intraday сейчас.
    cur = _add_run(db_session, base - timedelta(minutes=5), products_scraped=3)
    _add_snap(db_session, cur, "pharmonline", 3, last_seen_at=base - timedelta(minutes=5))
    db_session.commit()

    rep = check_health(db_session, site_drop_threshold=0.5)
    drop = [i for i in rep.issues if i.code == "site_drop"]
    # seen(14д)=203, total(21д)=203 → 100%
    assert not drop, "Опоздавший недельный прогон в пределах окна не должен давать drop"


def test_site_drop_excludes_stale_orphans_from_denominator(db_session):
    """Осиротевшие ряды (last_seen за окном свежести) НЕ входят в знаменатель.

    Регрессия 2026-06-16: pharmonline показывал 49% (9840/19811 — все ряды),
    т.к. в total попадали ~9.8K Playwright-дублей при живом каталоге ~9951.
    Freshness-окно (21д для pharmonline) исключает ряды старше окна → ratio
    считается от живого каталога, и ложного site_drop нет.
    """
    base = utcnow()
    # Текущий прогон видит 100 «живых» товаров
    cur = _add_run(db_session, base - timedelta(hours=1), products_scraped=100)
    _add_snap(db_session, cur, "pharmonline", 100, last_seen_at=base - timedelta(hours=1))
    # Старый прогон оставил 200 осиротевших рядов, не виденных 40 дней (> окна 21д)
    old = _add_run(db_session, base - timedelta(days=40), products_scraped=200)
    _add_snap(db_session, old, "pharmonline", 200, last_seen_at=base - timedelta(days=40))
    db_session.commit()

    rep = check_health(db_session, site_drop_threshold=0.5)
    drop = [i for i in rep.issues if i.code == "site_drop"]
    # Без фикса было бы 100/300=33% → critical; с freshness-окном 100/100=100%
    assert not drop, "Старые орфаны не должны раздувать знаменатель → нет site_drop"


def test_zero_prices_critical(db_session):
    """Если >50% snapshots с price=NULL/0 — critical."""
    base = utcnow()
    run = _add_run(db_session, base - timedelta(hours=1), products_scraped=20)
    # 12 NULL/0 prices, 8 нормальных (60% zero)
    for i in range(12):
        p = Product(
            site="aloe",
            external_id=f"z-{i}",
            url="x",
            name=f"Z{i}",
            name_normalized=f"z{i}",
        )
        db_session.add(p)
        db_session.flush()
        db_session.add(PriceSnapshot(run_id=run.id, product_id=p.id, price=0))
    for i in range(8):
        p = Product(
            site="aloe",
            external_id=f"ok-{i}",
            url="x",
            name=f"OK{i}",
            name_normalized=f"ok{i}",
        )
        db_session.add(p)
        db_session.flush()
        db_session.add(PriceSnapshot(run_id=run.id, product_id=p.id, price=10.0))
    db_session.commit()

    rep = check_health(db_session)
    zero = [i for i in rep.issues if i.code == "zero_prices"]
    assert len(zero) == 1
    assert zero[0].severity == "critical"
    assert zero[0].context["ratio"] >= 0.5


def test_brand_coverage_loss_critical(db_session):
    """Brand-coverage rest-of-catalog ≥50%, visible-now <20% → critical."""
    base = utcnow()
    # Предыдущий прогон: 80% с brand. last_seen_at = prev.started_at
    # (продукты «не видны сейчас», попадают в rest-of-catalog)
    prev = _add_run(db_session, base - timedelta(days=1), products_scraped=20)
    for i in range(16):
        p = Product(
            site="aloe",
            external_id=f"prev-b-{i}",
            url="x",
            name=f"P{i}",
            name_normalized=f"p{i}",
            brand="Bayer",
            last_seen_at=prev.started_at,
        )
        db_session.add(p)
        db_session.flush()
        db_session.add(PriceSnapshot(run_id=prev.id, product_id=p.id, price=10.0))
    for i in range(4):
        p = Product(
            site="aloe",
            external_id=f"prev-nb-{i}",
            url="x",
            name=f"P{i}",
            name_normalized=f"p{i}",
            last_seen_at=prev.started_at,
        )
        db_session.add(p)
        db_session.flush()
        db_session.add(PriceSnapshot(run_id=prev.id, product_id=p.id, price=10.0))

    # Текущий: 0% с brand. last_seen_at = curr.started_at (видимы сейчас)
    curr = _add_run(db_session, base - timedelta(hours=1), products_scraped=20)
    for i in range(20):
        p = Product(
            site="aloe",
            external_id=f"curr-{i}",
            url="x",
            name=f"C{i}",
            name_normalized=f"c{i}",
            last_seen_at=curr.started_at,
        )
        db_session.add(p)
        db_session.flush()
        db_session.add(PriceSnapshot(run_id=curr.id, product_id=p.id, price=10.0))
    db_session.commit()

    rep = check_health(db_session)
    bc = [i for i in rep.issues if i.code == "brand_coverage_loss"]
    assert len(bc) == 1
    assert bc[0].severity == "critical"


def test_brand_coverage_partial_intraday_tick_skipped(db_session):
    """Регресс (run #307, 2026-06-21): частичный intraday-тик НЕ должен алертить.

    Featured-тик pharmonline (~116 косметик-товаров без brand) из ~10k каталога
    давал ложный brand_coverage_loss, хотя полный каталог на 89%. Guard по доле
    каталога доминирующего сайта должен пропустить такой тик. Без guard'а этот же
    сценарий сработал бы (rest=100% ≥50%, curr=0% <20%) — что и тестим обратным.
    """
    base = utcnow()
    # Каталог pharmonline: 100 товаров С brand, видны «вчера» (rest-of-catalog).
    prev = _add_run(db_session, base - timedelta(days=1), products_scraped=100)
    for i in range(100):
        p = Product(
            site="pharmonline",
            external_id=f"cat-{i}",
            url="x",
            name=f"P{i}",
            name_normalized=f"p{i}",
            brand="Bayer",
            last_seen_at=prev.started_at,
        )
        db_session.add(p)
        db_session.flush()
        db_session.add(PriceSnapshot(run_id=prev.id, product_id=p.id, price=10.0))

    # Интрадей-тик: 12 товаров БЕЗ brand (≥10 → проходит min-sample guard), но
    # 12 < 0.25*112 = 28 → доминирующий сайт pharmonline, тонкий срез → скип.
    curr = _add_run(db_session, base - timedelta(hours=1), products_scraped=12)
    for i in range(12):
        p = Product(
            site="pharmonline",
            external_id=f"tick-{i}",
            url="x",
            name=f"T{i}",
            name_normalized=f"t{i}",
            last_seen_at=curr.started_at,
        )
        db_session.add(p)
        db_session.flush()
        db_session.add(PriceSnapshot(run_id=curr.id, product_id=p.id, price=10.0))
    db_session.commit()

    rep = check_health(db_session)
    bc = [i for i in rep.issues if i.code == "brand_coverage_loss"]
    assert bc == [], "частичный intraday-тик не должен давать brand_coverage_loss"


def test_site_drop_below_20_percent_critical(db_session):
    base = utcnow()
    cur = _add_run(db_session, base - timedelta(hours=1), products_scraped=10)
    # Каталог 100 виден 18 дней назад (вне окна покрытия), недавно — лишь 10.
    old = _add_run(db_session, base - timedelta(days=18), products_scraped=100)
    _add_snap(db_session, old, "pharmonline", 100, last_seen_at=base - timedelta(days=18))
    _add_snap(db_session, cur, "pharmonline", 10, last_seen_at=base - timedelta(hours=1))
    db_session.commit()

    rep = check_health(db_session, site_drop_threshold=0.5)
    drop_issues = [i for i in rep.issues if i.code == "site_drop"]
    # seen(9д)=10, total(21д)=110 → 9% → critical
    assert any(i.severity == "critical" for i in drop_issues)


def test_site_drop_skips_sites_not_in_latest_run(db_session):
    """Прогоны по-сайтно: последний run скрейпил ТОЛЬКО pharmonline. aptek/aloe
    с last_seen из своих ПРЕЖНИХ прогонов (но в пределах своих окон покрытия) НЕ
    должны ложно флагаться site_drop — каждый сайт меряется по СВОЕМУ окну, а не
    по тому, был ли он в последнем прогоне. Регрессия: при per-site runs site_drop
    ложно бил 2 из 3 сайтов на каждом прогоне.
    """
    base = utcnow()
    # aptek и aloe скрейпились РАНЬШЕ (свои отдельные прогоны), каталог полный
    apt_run = _add_run(db_session, base - timedelta(hours=5))
    _add_snap(db_session, apt_run, "aptekonline", 50, last_seen_at=base - timedelta(hours=5))
    aloe_run = _add_run(db_session, base - timedelta(hours=4))
    _add_snap(db_session, aloe_run, "aloe", 50, last_seen_at=base - timedelta(hours=4))
    # Последний прогон — ТОЛЬКО pharmonline, собрал свой каталог полностью
    ph_run = _add_run(db_session, base - timedelta(hours=1))
    _add_snap(db_session, ph_run, "pharmonline", 50, last_seen_at=base - timedelta(hours=1))
    db_session.commit()

    rep = check_health(db_session, site_drop_threshold=0.5)
    drop_sites = {i.context.get("site") for i in rep.issues if i.code == "site_drop"}
    assert "aptekonline" not in drop_sites  # не в последнем прогоне → не «упал»
    assert "aloe" not in drop_sites
    assert "pharmonline" not in drop_sites  # собрал 50/50 → тоже не падение


def test_site_zero_scrape_alerts_on_full_zero_run(db_session):
    """Сайт собрал РОВНО 0 в последнем прогоне → critical site_zero_scrape.

    Регрессия 06-11: aloe.az отдал 502, прогон собрал 0, но алерта не было.
    """
    base = utcnow()
    db_session.add(
        Run(
            started_at=base - timedelta(hours=1),
            finished_at=base - timedelta(minutes=58),
            status="ok",
            products_scraped=0,
            products_per_site={"aloe": 0},
        )
    )
    db_session.commit()

    rep = check_health(db_session, max_age_hours=999)  # не отвлекаться на staleness
    zero = [i for i in rep.issues if i.code == "site_zero_scrape"]
    assert zero, "Нулевой прогон сайта должен дать site_zero_scrape"
    assert zero[0].context["site"] == "aloe"
    assert zero[0].severity == "critical"


def test_site_zero_scrape_silent_when_site_has_products(db_session):
    """Последний прогон сайта собрал товары → нет site_zero_scrape."""
    base = utcnow()
    db_session.add(
        Run(
            started_at=base - timedelta(hours=1),
            finished_at=base - timedelta(minutes=58),
            status="ok",
            products_scraped=1866,
            products_per_site={"aloe": 1866},
        )
    )
    db_session.commit()

    rep = check_health(db_session, max_age_hours=999)
    assert not [i for i in rep.issues if i.code == "site_zero_scrape"]


def test_site_zero_scrape_ignores_intraday_partial_of_other_site(db_session):
    """Intraday-прогон (pharmonline:127) НЕ маскирует и не триггерит zero для aloe:
    aloe берётся из СВОЕГО последнего прогона (1866), а 127≠0 → нет zero-alerts."""
    base = utcnow()
    db_session.add_all(
        [
            Run(
                started_at=base - timedelta(hours=3),
                finished_at=base - timedelta(hours=2, minutes=58),
                status="ok",
                products_scraped=1866,
                products_per_site={"aloe": 1866},
            ),
            Run(
                started_at=base - timedelta(minutes=10),
                finished_at=base - timedelta(minutes=8),
                status="ok",
                products_scraped=127,
                products_per_site={"pharmonline": 127},
            ),
        ]
    )
    db_session.commit()

    rep = check_health(db_session, max_age_hours=999)
    assert not [i for i in rep.issues if i.code == "site_zero_scrape"]


def test_site_zero_scrape_fires_for_zero_under_newer_intraday(db_session):
    """Точная регрессия 06-11: aloe собрал 0, СВЕРХУ прошёл intraday-прогон
    другого сайта (pharmonline:127) → zero для aloe ВСЁ РАВНО ловится (intraday
    не маскирует более старый 0)."""
    base = utcnow()
    db_session.add_all(
        [
            Run(
                started_at=base - timedelta(hours=2),
                finished_at=base - timedelta(hours=1, minutes=58),
                status="ok",
                products_scraped=0,
                products_per_site={"aloe": 0},
            ),
            Run(
                started_at=base - timedelta(minutes=10),
                finished_at=base - timedelta(minutes=8),
                status="ok",
                products_scraped=127,
                products_per_site={"pharmonline": 127},
            ),
        ]
    )
    db_session.commit()

    rep = check_health(db_session, max_age_hours=999)
    zero = [i for i in rep.issues if i.code == "site_zero_scrape" and i.context["site"] == "aloe"]
    assert zero, "aloe:0 под более новым intraday-прогоном должен всё равно алертить"


def test_site_zero_scrape_ignores_in_flight_run(db_session):
    """Бегущий прогон (finished_at=NULL) с 0 НЕ алертит — берём предыдущий
    ЗАВЕРШЁННЫЙ прогон (1866)."""
    base = utcnow()
    db_session.add_all(
        [
            Run(
                started_at=base - timedelta(minutes=5),
                finished_at=None,
                status="running",
                products_scraped=0,
                products_per_site={"aloe": 0},
            ),
            Run(
                started_at=base - timedelta(hours=2),
                finished_at=base - timedelta(hours=1, minutes=58),
                status="ok",
                products_scraped=1866,
                products_per_site={"aloe": 1866},
            ),
        ]
    )
    db_session.commit()

    rep = check_health(db_session, max_age_hours=999)
    assert not [i for i in rep.issues if i.code == "site_zero_scrape"]


def test_alert_signature_stable_and_sorted():
    """Подпись = отсортированный набор code:site по warning/critical; ok отброшен,
    порядок issues не влияет."""
    from src.health import HealthIssue, HealthReport, alert_signature

    r = HealthReport(
        status="critical",
        issues=[
            HealthIssue("critical", "site_zero_scrape", "x", {"site": "aloe"}),
            HealthIssue("warning", "site_drop", "y", {"site": "pharmonline"}),
            HealthIssue("ok", "noise", "z", {}),
        ],
    )
    sig = alert_signature(r)
    assert sig == "site_drop:pharmonline|site_zero_scrape:aloe"
    r2 = HealthReport(status="critical", issues=list(reversed(r.issues)))
    assert alert_signature(r2) == sig  # порядок не влияет


def test_alert_due_cooldown_logic():
    """alert_due: новая/изменённая подпись → слать; та же в пределах cooldown → нет;
    та же после cooldown → слать; нет/битое состояние → слать (fail-safe)."""
    from src.health import alert_due

    now = utcnow()
    sig = "site_zero_scrape:aloe"
    assert alert_due(sig, None, now=now, cooldown_hours=6) is True  # нет состояния
    assert (
        alert_due(
            sig, {"signature": "other", "sent_at": now.isoformat()}, now=now, cooldown_hours=6
        )
        is True
    )  # другая подпись
    recent = {"signature": sig, "sent_at": (now - timedelta(hours=1)).isoformat()}
    assert alert_due(sig, recent, now=now, cooldown_hours=6) is False  # в пределах cooldown
    old = {"signature": sig, "sent_at": (now - timedelta(hours=7)).isoformat()}
    assert alert_due(sig, old, now=now, cooldown_hours=6) is True  # cooldown прошёл
    assert (
        alert_due(sig, {"signature": sig, "sent_at": "garbage"}, now=now, cooldown_hours=6) is True
    )  # битый timestamp → fail-safe
