"""Тесты health-check логики."""

from datetime import timedelta

from src import storage
from src._time import utcnow
from src.health import alert_signature, check_health
from src.storage import PriceSnapshot, Product, Run


def _add_run(s, started_at, status="ok", products_scraped=10):
    r = Run(
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=1),
        status=status,
        products_scraped=products_scraped,
        catalog_scope="full" if status == "ok" else "unknown",
        full_catalog_sites="pharmonline,aptekonline,aloe" if status == "ok" else None,
        catalog_verified=status == "ok",
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
    run.run_quality = {
        "baseline_enforced": True,
        "full_catalog_verified": True,
        "financially_eligible": True,
        "sites": {
            "pharmonline": {"status": "ok"},
            "aptekonline": {"status": "ok"},
            "aloe": {"status": "ok"},
        },
    }
    db_session.commit()
    rep = check_health(db_session)
    assert rep.is_healthy
    assert rep.status == "ok"


def test_degraded_run_is_warning_and_changes_alert_signature(db_session):
    run = _add_run(db_session, utcnow() - timedelta(hours=1), status="degraded")
    run.error_message = "aloe=degraded(incomplete_items)"
    run.run_quality = {"sites": {"aloe": {"status": "degraded", "reasons": ["incomplete_items"]}}}
    db_session.commit()
    report = check_health(db_session)
    assert report.status == "warning"
    assert any(issue.code == "last_run_degraded" for issue in report.issues)
    assert '"code":"last_run_degraded"' in alert_signature(report)


def test_running_run_does_not_hide_last_degraded_health(db_session):
    degraded = _add_run(db_session, utcnow() - timedelta(hours=1), status="degraded")
    degraded.run_quality = {
        "sites": {"aloe": {"status": "degraded", "reasons": ["incomplete_items"]}}
    }
    db_session.add(Run(started_at=utcnow(), status="running"))
    db_session.commit()

    report = check_health(db_session)
    assert report.last_run_id == degraded.id
    assert report.status == "warning"
    assert any(issue.code == "last_run_degraded" for issue in report.issues)


def test_post_processing_run_is_not_terminal_until_finished_at(db_session):
    finished = _add_run(db_session, utcnow() - timedelta(hours=1), status="degraded")
    finished.catalog_scope = "full"
    finished.full_catalog_sites = "pharmonline,aptekonline,aloe"
    finished.catalog_verified = False
    finished.run_quality = {
        "baseline_enforced": True,
        "full_catalog_verified": False,
        "financially_eligible": False,
        "sites": {"pharmonline": {"status": "degraded"}},
    }
    post_processing = Run(
        started_at=utcnow(),
        finished_at=None,
        status="running",
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {
                "pharmonline": {"status": "ok"},
                "aptekonline": {"status": "ok"},
                "aloe": {"status": "ok"},
            },
        },
    )
    db_session.add(post_processing)
    db_session.commit()

    report = check_health(db_session)

    assert report.last_run_id == finished.id
    issue = next(i for i in report.issues if i.code == "full_catalog_unverified")
    assert issue.context["sites"]["pharmonline"]["run_id"] == finished.id


def test_partial_history_without_full_catalog_is_warning(db_session):
    partial = _add_run(db_session, utcnow() - timedelta(hours=1), status="ok")
    partial.catalog_scope = "partial"
    partial.full_catalog_sites = None
    partial.catalog_verified = False
    partial.run_quality = {
        "baseline_enforced": False,
        "full_catalog_verified": False,
        "financially_eligible": False,
        "sites": {"pharmonline": {"status": "ok"}},
    }
    db_session.commit()

    report = check_health(db_session)

    assert report.status == "warning"
    issue = next(i for i in report.issues if i.code == "full_catalog_unverified")
    assert set(issue.context["sites"]) == set(storage.FULL_CATALOG_SITES)
    assert all(row["status"] == "missing" for row in issue.context["sites"].values())


def test_health_orders_terminal_runs_by_actual_completion(db_session):
    now = utcnow()
    degraded = _add_run(db_session, now - timedelta(hours=2), status="degraded")
    degraded.finished_at = now
    partial = _add_run(db_session, now - timedelta(hours=1), status="ok")
    partial.finished_at = now - timedelta(minutes=30)
    db_session.commit()

    report = check_health(db_session)

    assert report.last_run_id == degraded.id
    assert report.status == "warning"
    assert any(issue.code == "last_run_degraded" for issue in report.issues)


def test_later_partial_ok_does_not_clear_unverified_full_catalog(db_session):
    now = utcnow()
    full = _add_run(db_session, now - timedelta(hours=2), status="degraded")
    full.finished_at = now - timedelta(hours=1)
    full.catalog_scope = "full"
    full.full_catalog_sites = "pharmonline,aptekonline,aloe"
    full.catalog_verified = False
    full.run_quality = {
        "baseline_enforced": True,
        "full_catalog_verified": False,
        "financially_eligible": False,
        "sites": {"pharmonline": {"status": "degraded"}},
    }
    partial = _add_run(db_session, now - timedelta(minutes=30), status="ok")
    partial.catalog_scope = "partial"
    partial.full_catalog_sites = None
    partial.catalog_verified = False
    partial.run_quality = {
        "baseline_enforced": False,
        "full_catalog_verified": False,
        "financially_eligible": False,
        "sites": {"pharmonline": {"status": "ok"}},
    }
    db_session.commit()

    report = check_health(db_session)

    assert report.last_run_id == partial.id
    assert report.last_run_status == "ok"
    assert report.status == "warning"
    issue = next(i for i in report.issues if i.code == "full_catalog_unverified")
    assert issue.context["sites"]["pharmonline"]["run_id"] == full.id


def test_single_site_success_does_not_mask_other_degraded_full_sites(db_session):
    now = utcnow()
    degraded = _add_run(db_session, now - timedelta(hours=2), status="degraded")
    degraded.finished_at = now - timedelta(hours=1)
    degraded.catalog_scope = "full"
    degraded.full_catalog_sites = "pharmonline,aptekonline,aloe"
    degraded.catalog_verified = False
    degraded.run_quality = {
        "baseline_enforced": True,
        "full_catalog_verified": False,
        "financially_eligible": False,
        "sites": {
            "pharmonline": {"status": "degraded"},
            "aptekonline": {"status": "degraded"},
            "aloe": {"status": "ok"},
        },
    }
    aloe_ok = _add_run(db_session, now - timedelta(minutes=30), status="ok")
    aloe_ok.catalog_scope = "full"
    aloe_ok.full_catalog_sites = "aloe"
    aloe_ok.catalog_verified = True
    aloe_ok.run_quality = {
        "baseline_enforced": True,
        "full_catalog_verified": True,
        "financially_eligible": True,
        "sites": {"aloe": {"status": "ok"}},
    }
    db_session.commit()

    report = check_health(db_session)

    assert report.last_run_id == aloe_ok.id
    assert report.status == "warning"
    issue = next(i for i in report.issues if i.code == "full_catalog_unverified")
    assert issue.context["sites"]["pharmonline"]["run_id"] == degraded.id
    assert issue.context["sites"]["aptekonline"]["run_id"] == degraded.id
    assert "aloe" not in issue.context["sites"]


def test_degraded_run_with_failed_site_is_critical(db_session):
    run = _add_run(db_session, utcnow() - timedelta(hours=1), status="degraded")
    run.run_quality = {
        "sites": {
            "aloe": {"status": "ok", "reasons": []},
            "aptekonline": {"status": "failed", "reasons": ["zero_products"]},
        }
    }
    db_session.commit()
    report = check_health(db_session)
    assert report.status == "critical"
    issue = next(item for item in report.issues if item.code == "degraded_site_failed")
    assert issue.context["site"] == "aptekonline"


def test_site_silence_critical_when_one_site_stale(db_session):
    """Per-site freshness: pharmonline молчит 9 дней (>30ч), aloe скрейпился час назад.

    `stale_run` смотрит на ПОСЛЕДНИЙ run и пропускает (aloe свежий), но per-site
    silence check должен поймать pharmonline. Реальный сценарий: Mac launchd
    уснул, aloe на проде продолжает скрейпиться. Daily cadence must surface the
    missing Pharmonline data promptly instead of accepting a weekly gap.
    """
    # aloe свежий
    aloe_run = _add_run(db_session, utcnow() - timedelta(hours=1))
    _add_snap(db_session, aloe_run, "aloe", 50)
    # pharmonline молчит 9 дней (> 30ч суточного порога)
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


def test_site_silence_respects_weekly_aptekonline_cadence(db_session):
    """Регрессия: недельный aptekonline на 100ч идёт по графику, а не молчит.

    Aptekonline собирается раз в неделю (решение владельца 2026-10-04), aloe —
    ежедневно. Один и тот же возраст данных означает для них разное: для aloe
    это пропущенные четверо суток, для aptekonline — середина штатного цикла.
    До правки оба сравнивались с 30ч, и дашборд красил aptekonline красным
    6 дней из 7.
    """
    now = utcnow()
    apt_run = _add_run(db_session, now - timedelta(hours=100))
    _add_snap(db_session, apt_run, "aptekonline", 30, last_seen_at=now - timedelta(hours=100))
    aloe_run = _add_run(db_session, now - timedelta(hours=100))
    _add_snap(db_session, aloe_run, "aloe", 30, last_seen_at=now - timedelta(hours=100))
    db_session.commit()

    rep = check_health(db_session, max_age_hours=26)
    silent_sites = {i.context.get("site") for i in rep.issues if i.code == "site_silent"}
    assert "aptekonline" not in silent_sites
    assert "aloe" in silent_sites


def test_site_thresholds_follow_declared_cadence():
    """Пороги выводятся из ритма сбора, а не прибиты гвоздями по сайтам."""
    from src import health as h

    # Суточные сайты сохраняют исторические значения 30ч / 3д / 2д.
    for site in ("pharmonline", "aloe"):
        assert h.site_cadence_hours(site) == 24
        assert h._SITE_MAX_AGE_HOURS[site] == 30
        assert h._SITE_FRESHNESS_DAYS[site] == 3
        assert h._SITE_COVERAGE_DAYS[site] == 2

    # Недельный сайт получает пропорционально широкие окна.
    assert h.site_cadence_hours("aptekonline") == 168
    assert h._SITE_MAX_AGE_HOURS["aptekonline"] == 174
    assert h._SITE_FRESHNESS_DAYS["aptekonline"] == 9
    assert h._SITE_COVERAGE_DAYS["aptekonline"] == 8

    # Инвариант site_drop: окно покрытия всегда строго уже окна свежести,
    # иначе частичный тик уронит метрику (см. docstring _check_site_drops).
    for site in h.SITE_SCRAPE_CADENCE_HOURS:
        assert h._SITE_COVERAGE_DAYS[site] < h._SITE_FRESHNESS_DAYS[site]


def test_site_silence_flags_long_aptekonline_outage(db_session):
    """Пропущенный недельный сбор (>174ч) — всё ещё critical."""
    now = utcnow()
    apt_run = _add_run(db_session, now - timedelta(hours=210))
    _add_snap(db_session, apt_run, "aptekonline", 30, last_seen_at=now - timedelta(hours=210))
    db_session.commit()

    rep = check_health(db_session, max_age_hours=26)
    silent_sites = {i.context.get("site") for i in rep.issues if i.code == "site_silent"}
    assert "aptekonline" in silent_sites


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


def test_failed_partial_run_is_warning_when_full_catalog_is_verified(db_session):
    full = _add_run(db_session, utcnow() - timedelta(hours=2))
    full.run_quality = {
        "full_catalog_verified": True,
        "financially_eligible": True,
        "sites": {
            "pharmonline": {"status": "ok"},
            "aptekonline": {"status": "ok"},
            "aloe": {"status": "ok"},
        },
    }
    partial = _add_run(
        db_session,
        utcnow() - timedelta(hours=1),
        status="failed",
        products_scraped=0,
    )
    partial.catalog_scope = "partial"
    partial.error_message = "timed out during opening handshake"
    db_session.commit()

    rep = check_health(db_session)

    issues = [issue for issue in rep.issues if issue.code == "last_run_failed"]
    assert rep.status == "warning"
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].context["catalog_scope"] == "partial"


def test_failed_full_run_remains_critical(db_session):
    run = _add_run(db_session, utcnow() - timedelta(hours=1), status="failed")
    run.catalog_scope = "full"
    run.full_catalog_sites = "pharmonline"
    run.error_message = "timed out during opening handshake"
    db_session.commit()

    rep = check_health(db_session)

    assert rep.status == "critical"
    assert any(
        issue.code == "last_run_failed" and issue.severity == "critical" for issue in rep.issues
    )


def test_failed_full_refresh_is_warning_when_prior_verified_catalog_is_fresh(db_session):
    """A transient source failure must not claim that safe client data vanished."""
    verified = _add_run(db_session, utcnow() - timedelta(hours=2))
    verified.run_quality = {
        "full_catalog_verified": True,
        "financially_eligible": True,
        "sites": {
            "pharmonline": {"status": "ok"},
            "aptekonline": {"status": "ok"},
            "aloe": {"status": "ok"},
        },
    }
    failed = _add_run(db_session, utcnow() - timedelta(hours=1), status="failed")
    failed.catalog_scope = "full"
    failed.full_catalog_sites = "pharmonline"
    failed.error_message = "RunQualityFailure: pharmonline=failed(zero_products)"
    failed.products_per_site = {"pharmonline": 0}
    failed.run_quality = {
        "full_catalog_verified": False,
        "financially_eligible": False,
        "sites": {"pharmonline": {"status": "failed"}},
    }
    db_session.commit()

    report = check_health(db_session)

    assert report.status == "warning"
    last_failed = next(issue for issue in report.issues if issue.code == "last_run_failed")
    catalog_issue = next(
        issue for issue in report.issues if issue.code == "full_catalog_unverified"
    )
    assert last_failed.severity == "warning"
    assert last_failed.context["fresh_verified_catalog"] is True
    assert catalog_issue.severity == "warning"
    assert catalog_issue.context["sites"]["pharmonline"]["last_verified_run_id"] == verified.id
    zero_scrape = next(issue for issue in report.issues if issue.code == "site_zero_scrape")
    assert zero_scrape.severity == "warning"
    assert zero_scrape.context["last_verified_run_id"] == verified.id


def test_degraded_full_run_critical(db_session):
    run = _add_run(db_session, utcnow() - timedelta(hours=1), status="degraded")
    run.catalog_scope = "full"
    run.catalog_verified = False
    run.catalog_verification_reason = "aloe:item_parse_failures=1"
    run.error_message = "FullCatalogVerificationError: item_parse_failures"
    db_session.commit()

    rep = check_health(db_session)

    assert rep.status == "critical"
    assert rep.last_run_status == "degraded"
    assert any(i.code == "last_run_degraded" for i in rep.issues)


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
    # Живой каталог: 100 товаров, виденных 2.5 дня назад (в окне свежести 3д,
    # но ВНЕ окна покрытия 2д) — недавние скрейпы их НЕ переснимали.
    old = _add_run(db_session, base - timedelta(days=2, hours=12), products_scraped=100)
    _add_snap(db_session, old, "pharmonline", 100, last_seen_at=base - timedelta(days=2, hours=12))
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
    # Полный прогон 44 часа назад: 200 товаров (в окне покрытия 2д).
    full = _add_run(db_session, base - timedelta(hours=44), products_scraped=200)
    _add_snap(db_session, full, "pharmonline", 200, last_seen_at=base - timedelta(hours=44))
    # Частичный intraday-прогон СЕЙЧАС: всего 5 товаров (latest run).
    partial = _add_run(db_session, base - timedelta(minutes=10), products_scraped=5)
    _add_snap(db_session, partial, "pharmonline", 5, last_seen_at=base - timedelta(minutes=10))
    db_session.commit()

    rep = check_health(db_session, site_drop_threshold=0.5)
    drop = [i for i in rep.issues if i.code == "site_drop"]
    # seen(2д)=205 (200 полного + 5 частичного), total(3д)=205 → 100%, без ложного drop
    assert not drop, "Частичный intraday-прогон не должен давать ложный site_drop"


def test_site_drop_tolerates_slightly_late_daily_run(db_session):
    """A 28-hour old daily full run remains inside the coverage buffer."""
    base = utcnow()
    full = _add_run(db_session, base - timedelta(hours=28), products_scraped=200)
    _add_snap(db_session, full, "pharmonline", 200, last_seen_at=base - timedelta(hours=28))
    # Мелкий intraday сейчас.
    cur = _add_run(db_session, base - timedelta(minutes=5), products_scraped=3)
    _add_snap(db_session, cur, "pharmonline", 3, last_seen_at=base - timedelta(minutes=5))
    db_session.commit()

    rep = check_health(db_session, site_drop_threshold=0.5)
    drop = [i for i in rep.issues if i.code == "site_drop"]
    # seen(2д)=203, total(3д)=203 → 100%
    assert not drop, "Слегка опоздавший daily-run не должен давать drop"


def test_site_drop_excludes_stale_orphans_from_denominator(db_session):
    """Осиротевшие ряды (last_seen за окном свежести) НЕ входят в знаменатель.

    Регрессия 2026-06-16: pharmonline показывал 49% (9840/19811 — все ряды),
    т.к. в total попадали ~9.8K Playwright-дублей при живом каталоге ~9951.
    Freshness-окно (3д для pharmonline) исключает ряды старше окна → ratio
    считается от живого каталога, и ложного site_drop нет.
    """
    base = utcnow()
    # Текущий прогон видит 100 «живых» товаров
    cur = _add_run(db_session, base - timedelta(hours=1), products_scraped=100)
    _add_snap(db_session, cur, "pharmonline", 100, last_seen_at=base - timedelta(hours=1))
    # Старый прогон оставил 200 осиротевших рядов, не виденных 40 дней (> окна 3д)
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
    # Каталог 100 виден 2.5 дня назад (вне окна покрытия), недавно — лишь 10.
    old = _add_run(db_session, base - timedelta(days=2, hours=12), products_scraped=100)
    _add_snap(db_session, old, "pharmonline", 100, last_seen_at=base - timedelta(days=2, hours=12))
    _add_snap(db_session, cur, "pharmonline", 10, last_seen_at=base - timedelta(hours=1))
    db_session.commit()

    rep = check_health(db_session, site_drop_threshold=0.5)
    drop_issues = [i for i in rep.issues if i.code == "site_drop"]
    # seen(2д)=10, total(3д)=110 → 9% → critical
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
    """Signature includes severity/code/site, ignores messages and issue order."""
    import json

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
    assert json.loads(sig) == [
        {
            "code": "site_drop",
            "context": {"site": "pharmonline"},
            "severity": "warning",
        },
        {
            "code": "site_zero_scrape",
            "context": {"site": "aloe"},
            "severity": "critical",
        },
    ]
    r2 = HealthReport(status="critical", issues=list(reversed(r.issues)))
    assert alert_signature(r2) == sig

    r3 = HealthReport(
        status="critical",
        issues=[
            HealthIssue("critical", "site_zero_scrape", "new text", {"site": "aloe"}),
            HealthIssue(
                "warning",
                "site_drop",
                "changed counters",
                {"site": "pharmonline", "ratio": 0.123, "products": 999},
            ),
        ],
    )
    assert alert_signature(r3) == sig


def test_alert_signature_tracks_severity_and_nested_site_status_not_volatile_values():
    from src.health import HealthIssue, HealthReport, alert_signature

    def signature(*, severity="warning", site="aptekonline", status="degraded", run_id=631):
        return alert_signature(
            HealthReport(
                status=severity,
                issues=[
                    HealthIssue(
                        severity,
                        "full_catalog_unverified",
                        "catalog trust issue",
                        {
                            "sites": {
                                site: {
                                    "run_id": run_id,
                                    "status": status,
                                    "site_status": status,
                                    "failed_routes": 17,
                                }
                            }
                        },
                    )
                ],
            )
        )

    base = signature()
    assert signature(run_id=999) == base  # run ids/counts are volatile, not incident identity
    assert signature(status="failed") != base
    assert signature(site="pharmonline") != base
    assert signature(severity="critical") != base


def test_health_alert_state_machine_incident_reminder_recovery_and_recurrence():
    from src.health import (
        HealthIssue,
        HealthReport,
        health_alert_decision,
        health_alert_state_after,
    )

    now = utcnow()
    problem = HealthReport(
        status="critical",
        issues=[HealthIssue("critical", "site_silent", "silent", {"site": "pharmonline"})],
    )
    healthy = HealthReport(status="ok")

    first = health_alert_decision(problem, None, now=now, reminder_hours=24)
    assert first.action == "incident"
    active_state = health_alert_state_after(first, None, now=now)
    assert active_state["status"] == "active"
    assert active_state["signature"] == first.signature

    unchanged = health_alert_decision(
        problem,
        active_state,
        now=now + timedelta(hours=23, minutes=59),
        reminder_hours=24,
    )
    assert unchanged.action is None

    reminder = health_alert_decision(
        problem,
        active_state,
        now=now + timedelta(hours=24),
        reminder_hours=24,
    )
    assert reminder.action == "reminder"
    reminder_state = health_alert_state_after(
        reminder,
        active_state,
        now=now + timedelta(hours=24),
    )
    assert reminder_state["incident_started_at"] == active_state["incident_started_at"]

    recovery = health_alert_decision(
        healthy,
        reminder_state,
        now=now + timedelta(hours=25),
        reminder_hours=24,
    )
    assert recovery.action == "recovery"
    healthy_state = health_alert_state_after(
        recovery,
        reminder_state,
        now=now + timedelta(hours=25),
    )
    assert healthy_state["status"] == "ok"
    assert healthy_state["signature"] is None

    assert (
        health_alert_decision(
            healthy,
            healthy_state,
            now=now + timedelta(hours=26),
            reminder_hours=24,
        ).action
        is None
    )
    assert (
        health_alert_decision(
            problem,
            healthy_state,
            now=now + timedelta(hours=26),
            reminder_hours=24,
        ).action
        == "incident"
    )


def test_health_alert_changed_signature_is_immediate_and_legacy_state_is_supported():
    from src.health import (
        HealthIssue,
        HealthReport,
        health_alert_decision,
        health_alert_state_after,
    )

    now = utcnow()
    warning = HealthReport(
        status="warning",
        issues=[HealthIssue("warning", "site_silent", "silent", {"site": "pharmonline"})],
    )
    legacy_state = {
        "signature": "site_silent:pharmonline",
        "sent_at": (now - timedelta(hours=23)).isoformat(),
    }
    assert health_alert_decision(warning, legacy_state, now=now, reminder_hours=24).action is None

    escalated = HealthReport(
        status="critical",
        issues=[HealthIssue("critical", "site_silent", "silent", {"site": "pharmonline"})],
    )
    assert (
        health_alert_decision(escalated, legacy_state, now=now, reminder_hours=24).action
        == "incident"
    )

    changed_site = HealthReport(
        status="critical",
        issues=[HealthIssue("critical", "site_silent", "silent", {"site": "aptekonline"})],
    )
    assert (
        health_alert_decision(changed_site, legacy_state, now=now, reminder_hours=24).action
        == "incident"
    )
    legacy_state["sent_at"] = (now - timedelta(hours=24)).isoformat()
    reminder = health_alert_decision(warning, legacy_state, now=now, reminder_hours=24)
    assert reminder.action == "reminder"
    migrated = health_alert_state_after(reminder, legacy_state, now=now)
    assert migrated["version"] == 2
    assert migrated["signature"] != legacy_state["signature"]

    nested_warning = HealthReport(
        status="warning",
        issues=[
            HealthIssue(
                "warning",
                "full_catalog_unverified",
                "degraded",
                {"sites": {"aptekonline": {"status": "degraded"}}},
            )
        ],
    )
    nested_legacy = {
        "signature": "full_catalog_unverified:",
        "sent_at": (now - timedelta(hours=1)).isoformat(),
    }
    assert (
        health_alert_decision(nested_warning, nested_legacy, now=now, reminder_hours=24).action
        == "incident"
    )


def test_health_alert_invalid_aware_or_future_timestamp_fails_open():
    from datetime import timezone

    from src.health import HealthIssue, HealthReport, alert_signature, health_alert_decision

    now = utcnow()
    report = HealthReport(
        status="warning",
        issues=[HealthIssue("warning", "site_silent", "silent", {"site": "pharmonline"})],
    )
    signature = alert_signature(report)

    for sent_at in (
        "garbage",
        (now + timedelta(hours=1)).isoformat(),
        now.replace(tzinfo=timezone.utc).isoformat(),
    ):
        state = {
            "version": 2,
            "status": "active",
            "signature": signature,
            "last_sent_at": sent_at,
        }
        assert health_alert_decision(report, state, now=now, reminder_hours=24).action == "reminder"


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
