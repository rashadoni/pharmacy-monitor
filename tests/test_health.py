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
    """Per-site freshness: pharmonline молчит 3 дня, aloe скрейпился час назад.

    `stale_run` смотрит на ПОСЛЕДНИЙ run и пропускает (aloe свежий), но per-site
    silence check должен поймать pharmonline. Реальный сценарий: Mac launchd
    уснул, aloe на проде продолжает скрейпиться.
    """
    # aloe свежий
    aloe_run = _add_run(db_session, utcnow() - timedelta(hours=1))
    _add_snap(db_session, aloe_run, "aloe", 50)
    # pharmonline старый (3 дня назад)
    old_pharm_run = _add_run(db_session, utcnow() - timedelta(days=3))
    _add_snap(
        db_session,
        old_pharm_run,
        "pharmonline",
        50,
        last_seen_at=utcnow() - timedelta(days=3),
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
    """Если current site count <50% от медианы — warning."""
    base = utcnow()
    # 3 исторических прогона по 100 товаров pharmonline
    for d in [10, 5, 3]:
        old = _add_run(db_session, base - timedelta(days=d), products_scraped=100)
        _add_snap(db_session, old, "pharmonline", 100)
    # Текущий прогон — только 30 товаров на pharmonline (drop до 30%)
    cur = _add_run(db_session, base - timedelta(hours=1), products_scraped=30)
    _add_snap(db_session, cur, "pharmonline", 30)
    db_session.commit()

    rep = check_health(db_session, history_days=14, site_drop_threshold=0.5)
    drop_issues = [i for i in rep.issues if i.code == "site_drop"]
    assert drop_issues, "Должен быть site_drop issue"
    assert drop_issues[0].context["site"] == "pharmonline"
    assert drop_issues[0].context["ratio"] < 0.5


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


def test_site_drop_below_20_percent_critical(db_session):
    base = utcnow()
    for d in [10, 5, 3]:
        old = _add_run(db_session, base - timedelta(days=d), products_scraped=100)
        _add_snap(db_session, old, "pharmonline", 100)
    cur = _add_run(db_session, base - timedelta(hours=1), products_scraped=10)
    _add_snap(db_session, cur, "pharmonline", 10)  # 10% от медианы
    db_session.commit()

    rep = check_health(db_session, history_days=14, site_drop_threshold=0.5)
    drop_issues = [i for i in rep.issues if i.code == "site_drop"]
    assert any(i.severity == "critical" for i in drop_issues)


def test_site_drop_skips_sites_not_in_latest_run(db_session):
    """Прогоны по-сайтно: последний run скрейпил ТОЛЬКО pharmonline. aptek/aloe с
    last_seen из своих ПРЕЖНИХ прогонов (seen=0 относительно pharmonline-run) НЕ
    должны ложно флагаться site_drop — это «не в этом прогоне» (staleness ловит
    site_silent), а не «упал». Регрессия: при per-site runs site_drop ложно бил
    2 из 3 сайтов на каждом прогоне.
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
