"""Запас до нижней границы каталога pharmonline и письма о долгих предупреждениях.

Полный сбор pharmonline отказывает, когда товаров в API меньше границы из
`pharmonline_public_api_catalog_baselines`, и ниже границы отказывает же ручная
сверка. Граница сама не опускается, каталог убывает — отказ виден за недели.
Проверяется, что health говорит о нём заранее и что предупреждение, которое
висит месяцами, не превращается в ежедневное письмо и не прячет закрытие
настоящего инцидента.
"""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src import health, storage
from src import main as main_mod
from src._time import utcnow
from src.health import (
    STANDING_REMINDER_HOURS,
    HealthIssue,
    HealthReport,
    alert_signature,
    check_health,
    health_alert_decision,
    health_alert_state_after,
    standing_alert_keys,
)
from src.scrapers.base import RouteStatus, ScrapedProduct, ScrapeResult
from src.storage import PharmonlinePublicAPICatalogBaseline, PriceSnapshot, Product, Run

REFUSED = "public_api_identity_proof_failed:api_ids={n}, trusted_ids=10284, catalog_floor=9000"
LOW = "pharmonline_catalog_floor|pharmonline|low"
VERY_LOW = "pharmonline_catalog_floor|pharmonline|very_low"


def _floor(session, minimum: int, *, tenant_id: int = 1) -> None:
    session.add(
        PharmonlinePublicAPICatalogBaseline(
            tenant_id=tenant_id,
            catalog_item_count=9565,
            minimum_catalog_item_count=minimum,
            verified_identity_count=9565,
            trusted_ddp_item_count=9859,
            retired_ddp_item_count=365,
            reconciled_item_count=74,
            proof_version="url_continuity_v1",
            source_manifest_sha256="a" * 64,
            catalog_fingerprint_sha256="b" * 64,
            source_transport="decodo",
            preflight_run_ref="test",
        )
    )
    session.flush()


def _read(
    session,
    products: int,
    *,
    hours_ago: float = 1,
    verified: bool = True,
    reason: str | None = None,
    status: str | None = None,
    site_status: str = "ok",
    mode: str = "public_api",
    scope: str = "full",
    sites: str = "pharmonline",
    tenant_id: int = 1,
    finished: bool = True,
) -> Run:
    """Полный сбор pharmonline в том виде, в каком его записывает `run`."""
    started = utcnow() - timedelta(hours=hours_ago)
    if reason is None:
        reason = "complete_nonzero_routes_coverage_ok" if verified else REFUSED.format(n=products)
    if status is None:
        status = ("ok" if verified else "failed") if finished else "running"
    run = Run(
        tenant_id=tenant_id,
        started_at=started,
        finished_at=started + timedelta(minutes=5) if finished else None,
        status=status,
        products_scraped=products if verified else 0,
        products_per_site={"pharmonline": products} if verified else None,
        catalog_scope=scope,
        full_catalog_sites=sites,
        catalog_verified=verified,
        catalog_verification_reason=reason[:300],
        run_quality={
            "mode": mode,
            "full_catalog_verified": verified,
            "financially_eligible": verified,
            "catalog_verification_reason": reason,
            "sites": {"pharmonline": {"status": site_status, "products": products}},
        },
    )
    session.add(run)
    session.flush()
    return run


def _floor_issues(session) -> list[HealthIssue]:
    return health._check_pharmonline_catalog_floor(session)


# --- проверка -----------------------------------------------------------------


def test_large_margin_is_silent(db_session):
    """Замер прода 2026-10-08: каталог 9415 при границе 9000 — писать не о чем."""
    _floor(db_session, 9000)
    _read(db_session, 9415)

    assert _floor_issues(db_session) == []


def test_small_margin_is_a_standing_warning(db_session):
    _floor(db_session, 9000)
    run = _read(db_session, 9250)

    (issue,) = _floor_issues(db_session)

    assert (issue.severity, issue.code, issue.is_standing) == (
        "warning",
        "pharmonline_catalog_floor",
        True,
    )
    assert issue.context == {
        "site": "pharmonline",
        "run_id": run.id,
        "products": 9250,
        "floor": 9000,
        "margin": 250,
        "stage": "low",
    }
    assert "250" in issue.message and "9250" in issue.message and "9000" in issue.message
    assert f"#{run.id}" in issue.message and "RUNBOOK" in issue.message


@pytest.mark.parametrize(
    ("products", "stage"),
    [
        (9301, None),
        (9300, "low"),
        (9101, "low"),
        (9100, "very_low"),
        # Сбор отказывает при «меньше границы»: ровно на границе он ещё проходит.
        (9000, "very_low"),
    ],
)
def test_margin_thresholds(db_session, products, stage):
    _floor(db_session, 9000)
    _read(db_session, products)

    issues = _floor_issues(db_session)

    assert [issue.context["stage"] for issue in issues] == ([stage] if stage else [])
    assert all(issue.severity == "warning" and issue.is_standing for issue in issues)


def test_catalog_below_the_floor_is_a_critical_incident(db_session):
    """Ниже границы сбор отказывает сам и сам не восстановится — это уже поломка."""
    _floor(db_session, 9000)
    run = _read(db_session, 8990, verified=False)

    (issue,) = _floor_issues(db_session)

    assert (issue.severity, issue.is_standing) == ("critical", False)
    assert issue.context["stage"] == "below"
    assert issue.context["margin"] == -10
    assert issue.context["run_id"] == run.id
    assert "8990" in issue.message and "9000" in issue.message


def test_missing_table_is_silent(db_session):
    """База до миграции 0019: границы нет, и проверка не должна ронять health."""
    _read(db_session, 9050)
    db_session.commit()
    PharmonlinePublicAPICatalogBaseline.__table__.drop(db_session.get_bind())

    assert _floor_issues(db_session) == []
    assert "pharmonline_catalog_floor" not in {i.code for i in check_health(db_session).issues}


def test_no_baseline_row_is_silent(db_session):
    _read(db_session, 9050)

    assert _floor_issues(db_session) == []


def test_no_complete_read_is_silent(db_session):
    """Границу не с чем сравнить: сбор ни разу не дочитал каталог."""
    _floor(db_session, 9000)
    _read(
        db_session,
        0,
        verified=False,
        site_status="failed",
        reason="missing=-;failed=pharmonline;coverage=-",
    )

    assert _floor_issues(db_session) == []


def test_a_read_refused_only_by_the_identity_proof_counts(db_session):
    """Каталог прочитан целиком, отказала сверка личностей — размер настоящий.

    Подтверждённый сбор бывает неделями старше: с 5 по 8 октября 2026 каждый
    сбор отказывал на новых товарах, а каталог за это время ушёл с 9424 до 9415.
    """
    _floor(db_session, 9200)
    _read(db_session, 9520, hours_ago=72)
    refused = _read(db_session, 9415, hours_ago=2, verified=False)

    (issue,) = _floor_issues(db_session)

    assert issue.context["run_id"] == refused.id
    assert issue.context["margin"] == 215


def test_a_verified_read_counts_even_when_the_run_failed_afterwards(db_session):
    """Каталог прошёл все проверки, упала запись: число товаров от этого не хуже."""
    _floor(db_session, 9000)
    _read(db_session, 9520, hours_ago=72)
    crashed = _read(db_session, 9250, hours_ago=2, status="failed")

    (issue,) = _floor_issues(db_session)

    assert issue.context["run_id"] == crashed.id


@pytest.mark.parametrize(
    "newer",
    [
        # сайт не ответил
        {"products": 0, "site_status": "failed", "reason": "missing=-;failed=pharmonline"},
        # каталог прочитан, но меньше 0.90 прошлого: усечённое чтение, а не размер
        {"products": 4000, "reason": "missing=-;failed=-;coverage=pharmonline"},
        # сверка личностей отказала, но сайт прочитан не целиком
        {"products": 4000, "site_status": "degraded", "reason": REFUSED.format(n=4000)},
        # сбор по категориям считает другие товары (до августа 2026 — 19 тысяч)
        {"products": 19065, "mode": "category", "reason": REFUSED.format(n=19065)},
        # не полный сбор
        {"products": 100, "scope": "partial", "reason": REFUSED.format(n=100)},
        # полный сбор другого сайта
        {"products": 100, "sites": "aloe", "reason": REFUSED.format(n=100)},
        # чужой тенант
        {"products": 100, "tenant_id": 2, "reason": REFUSED.format(n=100)},
    ],
)
def test_reads_that_stopped_before_the_identity_proof_do_not_count(db_session, newer):
    _floor(db_session, 9000)
    complete = _read(db_session, 9250, hours_ago=30)
    _read(db_session, hours_ago=1, verified=False, **newer)

    (issue,) = _floor_issues(db_session)

    assert issue.context["run_id"] == complete.id
    assert issue.context["products"] == 9250


@pytest.mark.parametrize(
    "run_quality",
    [
        ["not", "a", "dict"],
        {"mode": "public_api", "sites": ["pharmonline"]},
        {"mode": "public_api", "sites": {"pharmonline": "ok"}},
        {"mode": "public_api", "sites": {"pharmonline": {"status": "ok", "products": "9100"}}},
    ],
)
def test_malformed_run_quality_is_skipped_not_fatal(db_session, run_quality):
    """Проверка справочная: странная строка прогона не должна ронять весь health."""
    _floor(db_session, 9000)
    complete = _read(db_session, 9250, hours_ago=30)
    odd = _read(db_session, 100, verified=False)
    odd.run_quality = run_quality
    db_session.flush()

    (issue,) = _floor_issues(db_session)

    assert issue.context["run_id"] == complete.id


def test_run_still_in_progress_is_not_a_read(db_session):
    """Незавершённый прогон ничего не доказывает, даже с готовой причиной отказа.

    Отдельным тестом и без других прогонов: в SQLite пустое время конца уходит
    в конец сортировки, и рядом с завершённым сбором фильтр остался бы без
    проверки.
    """
    _floor(db_session, 9000)
    _read(db_session, 100, verified=False, finished=False)

    assert _floor_issues(db_session) == []


def test_floor_is_the_highest_row_of_the_tenant(db_session):
    """Как в сборе: действует наибольшее значение, строки другого тенанта не в счёт."""
    _floor(db_session, 8000)
    _floor(db_session, 9000)
    _floor(db_session, 9900, tenant_id=2)
    _read(db_session, 9050)

    (issue,) = _floor_issues(db_session)

    assert issue.context["floor"] == 9000


def test_check_health_shows_the_warning_and_no_incident(db_session):
    """Всё остальное здорово: статус «warning», но инцидента нет."""
    _floor(db_session, 9000)
    run = _read(db_session, 9250, sites="pharmonline,aptekonline,aloe")
    run.run_quality = {
        **run.run_quality,
        "baseline_enforced": True,
        "sites": {
            "pharmonline": {"status": "ok", "products": 9250},
            "aptekonline": {"status": "ok"},
            "aloe": {"status": "ok"},
        },
    }
    for index in range(10):
        product = Product(
            site="pharmonline",
            external_id=f"p-{index}",
            url=f"http://x/{index}",
            name=f"P {index}",
            name_normalized=f"p {index}",
            last_seen_at=run.started_at,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(PriceSnapshot(run_id=run.id, product_id=product.id, price=10.0))
    db_session.commit()

    report = check_health(db_session)

    assert [issue.code for issue in report.issues] == ["pharmonline_catalog_floor"]
    assert report.status == "warning"
    assert report.has_incident is False
    assert standing_alert_keys(report) == (LOW,)
    assert alert_signature(report) == "[]"


def test_refused_run_is_recorded_the_way_the_check_reads_it(db_session, monkeypatch):
    """От настоящего `run` до health: сбор под границей даёт CRITICAL.

    Проверка читает `Run.run_quality` и причину отказа, которые пишет `run_cmd`.
    Собранные руками строки в тестах выше этого не доказывают — только прогон
    самой команды.
    """
    from src.scrapers.pharmonline_public_api import (
        PUBLIC_API_AVAILABILITY_SOURCE,
        PUBLIC_CATALOG_ROUTE,
    )

    async def catalog_of_two(*_args, **_kwargs):
        return [
            ScrapeResult(
                site="pharmonline",
                products=[
                    ScrapedProduct(
                        site="pharmonline",
                        external_id=external_id,
                        url=f"https://pharmonline.az/product/{slug}",
                        name=f"Product {slug}",
                        identity_verified=True,
                        availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
                    )
                    for external_id, slug in (
                        ("xwJspdCx3iFBDqDWF", "first"),
                        ("aB3dE6gH9jK2mN5pQ", "second"),
                    )
                ],
                category_counts={PUBLIC_CATALOG_ROUTE: 2},
                route_statuses={
                    PUBLIC_CATALOG_ROUTE: RouteStatus(
                        complete=True, raw_items=2, parsed_items=2, expected_items=2
                    )
                },
            )
        ]

    _floor(db_session, 5)
    db_session.commit()
    Session = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.setenv("PHARMONLINE_USE_DDP", "0")
    monkeypatch.setenv("SCRAPE_REPORT_EMAIL", "0")
    monkeypatch.delenv("PHARMONLINE_LEGACY_ID_BRIDGE", raising=False)
    monkeypatch.delenv("AI_FALLBACK_ENABLED", raising=False)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: Session)
    monkeypatch.setattr(main_mod, "_hold_scrape_lock_until_command_exit", lambda *a, **k: True)
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *a, **k: {"pharmonline": 2})
    monkeypatch.setattr(
        main_mod, "run_quality_baselines_for_sites", lambda *a, **k: {"pharmonline": 2}
    )
    monkeypatch.setattr(main_mod, "scrape_all", catalog_of_two)
    monkeypatch.setattr(main_mod, "persist_results", lambda *a, **k: pytest.fail("persisted"))

    result = CliRunner().invoke(
        main_mod.cli, ["run", "--site", "pharmonline", "--mode", "public_api", "--no-alerts"]
    )

    assert result.exit_code != 0
    assert "catalog_floor_failed=1" in result.output
    with Session() as session:
        run = session.scalar(select(Run).order_by(Run.id.desc()))
        (issue,) = _floor_issues(session)
    assert issue.severity == "critical"
    assert issue.context == {
        "site": "pharmonline",
        "run_id": run.id,
        "products": 2,
        "floor": 5,
        "margin": -3,
        "stage": "below",
    }


def test_floor_and_latest_read_on_postgres():
    """Тот же запрос на PostgreSQL: JSON-колонка и порядок пустых значений там другие.

    Остальные тесты идут на SQLite. Здесь — настоящие таблицы базы из
    `DATABASE_URL`, свой тенант и откат транзакции в конце.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        if os.environ.get("CI"):
            pytest.fail("в CI проверка границы обязана идти и на PostgreSQL из DATABASE_URL")
        pytest.skip("нужен PostgreSQL в DATABASE_URL")
    tenant = 7_000_000 + os.getpid() % 1_000_000
    with storage.make_session(url)() as session:
        try:
            _floor(session, 8000, tenant_id=tenant)
            _floor(session, 9000, tenant_id=tenant)
            _read(session, 9520, hours_ago=72, tenant_id=tenant)
            refused = _read(session, 9250, hours_ago=3, verified=False, tenant_id=tenant)
            _read(session, 100, hours_ago=2, verified=False, mode="category", tenant_id=tenant)
            # В PostgreSQL пустое время конца при сортировке по убыванию идёт первым.
            _read(session, 100, hours_ago=1, verified=False, finished=False, tenant_id=tenant)

            assert health._pharmonline_catalog_floor(session, tenant_id=tenant) == 9000
            assert health._latest_complete_pharmonline_catalog_read(session, tenant_id=tenant) == (
                refused.id,
                refused.finished_at,
                9250,
            )
        finally:
            session.rollback()


# --- письма -------------------------------------------------------------------


def _standing(stage: str = "low", margin: int = 250) -> HealthIssue:
    return HealthIssue(
        "warning",
        "pharmonline_catalog_floor",
        f"запас {margin}",
        {"site": "pharmonline", "stage": stage, "margin": margin},
        standing=True,
    )


def _report(*issues: HealthIssue) -> HealthReport:
    severities = {issue.severity for issue in issues}
    status = "critical" if "critical" in severities else "warning" if severities else "ok"
    return HealthReport(status=status, issues=list(issues))


INCIDENT = HealthIssue("critical", "site_silent", "silent", {"site": "aloe"})


class _Mailbox:
    """Почасовые проверки с настоящей машиной состояний, без SMTP и файлов."""

    def __init__(self, state: dict | None = None) -> None:
        self.state = state
        self.now = utcnow()
        self.sent: list[str] = []

    def check(self, report: HealthReport, *, hours: float = 1) -> str | None:
        self.now += timedelta(hours=hours)
        decision = health_alert_decision(report, self.state, now=self.now, reminder_hours=24)
        if decision.action is not None:
            # Состояние читается с диска на каждой проверке — ходим тем же путём.
            self.state = json.loads(
                json.dumps(health_alert_state_after(decision, self.state, now=self.now))
            )
            self.sent.append(decision.action)
        return decision.action


def test_standing_warning_is_mailed_once_and_then_weekly_not_daily():
    box = _Mailbox()
    warning = _report(_standing())

    assert box.check(warning) == "notice"
    assert box.state["status"] == "ok" and box.state["signature"] is None

    for _ in range(60 * 24):
        box.check(warning)

    # 60 дней почасовых проверок: первое письмо и по одному в неделю.
    assert box.sent == ["notice"] * (1 + 60 * 24 // STANDING_REMINDER_HOURS)
    assert len(box.sent) == 9


def test_changing_numbers_do_not_restart_the_standing_warning():
    box = _Mailbox()

    assert box.check(_report(_standing(margin=250))) == "notice"
    assert box.check(_report(_standing(margin=243)), hours=48) is None
    assert box.check(_report(_standing(margin=120)), hours=48) is None


def test_next_stage_is_mailed_at_once_and_flapping_is_not():
    box = _Mailbox()

    assert box.check(_report(_standing("low"))) == "notice"
    assert box.check(_report(_standing("very_low"))) == "notice"
    # Замер колеблется у порога: обе ступени уже называли, срок не вышел.
    assert box.check(_report(_standing("low"))) is None
    assert box.check(_report(_standing("very_low"))) is None
    # Предупреждение пропало и вернулось — тоже не новость.
    assert box.check(_report()) is None
    assert box.check(_report(_standing("low"))) is None

    assert box.check(_report(_standing("low")), hours=STANDING_REMINDER_HOURS) == "notice"


def test_standing_warning_does_not_change_the_incident():
    with_warning = _report(INCIDENT, _standing())

    assert alert_signature(with_warning) == alert_signature(_report(INCIDENT))
    assert with_warning.has_incident is True
    assert _report(_standing()).has_incident is False
    assert _report().has_incident is False
    # Отчёт без разобранных проблем, но не «ok» — инцидент: письмо терять нельзя.
    assert HealthReport(status="critical").has_incident is True


def test_critical_is_an_incident_whatever_it_is_marked():
    """Пометка «долгое» на critical не действует: о поломке пишут как о поломке."""
    marked = HealthIssue("critical", "some_check", "broken", {"site": "aloe"}, standing=True)
    report = _report(marked)
    box = _Mailbox()

    assert marked.is_standing is False
    assert report.has_incident is True
    assert standing_alert_keys(report) == ()
    assert "some_check" in alert_signature(report)
    assert box.check(report) == "incident"
    assert box.check(report, hours=24) == "reminder"
    assert "Долгое предупреждение" not in health.render_alert_html(report)


def test_incident_keeps_its_daily_rhythm_next_to_a_standing_warning():
    box = _Mailbox()
    report = _report(INCIDENT, _standing())

    assert box.check(report) == "incident"
    for _ in range(3 * 24):
        box.check(report)

    assert box.sent == ["incident", "reminder", "reminder", "reminder"]


def test_standing_warning_that_appears_during_an_incident_is_mailed_once():
    box = _Mailbox()

    assert box.check(_report(INCIDENT)) == "incident"
    # Появилось долгое предупреждение: одно письмо сразу, инцидент — прежний.
    assert box.check(_report(INCIDENT, _standing())) == "reminder"
    assert box.state["signature"] == alert_signature(_report(INCIDENT))
    assert box.check(_report(INCIDENT, _standing())) is None
    assert box.check(_report(INCIDENT, _standing()), hours=23) == "reminder"


def test_incident_closing_is_a_recovery_even_while_the_warning_stays():
    """Без отдельной дорожки отчёт не стал бы «ok», и RECOVERED не пришло бы."""
    box = _Mailbox()
    assert box.check(_report(INCIDENT, _standing())) == "incident"

    assert box.check(_report(_standing())) == "recovery"
    assert box.state["status"] == "ok"
    # О предупреждении написали вместе с инцидентом — второго письма следом нет.
    assert box.check(_report(_standing())) is None
    assert box.check(_report(_standing()), hours=STANDING_REMINDER_HOURS - 2) == "notice"
    # Новый инцидент после этого — снова инцидент, а не напоминание.
    assert box.check(_report(INCIDENT, _standing())) == "incident"


@pytest.mark.parametrize("before", [(), ("low",)])
def test_recovery_mail_does_not_stand_in_for_a_new_warning(before):
    """Новое предупреждение или новая ступень под темой RECOVERED остались бы незамеченными.

    Запас меняется, когда проходит сбор pharmonline, — тогда же закрываются его
    инциденты, так что совпадение обычное.
    """
    box = _Mailbox()
    assert box.check(_report(INCIDENT, *(_standing(stage) for stage in before))) == "incident"

    assert box.check(_report(_standing("very_low"))) == "recovery"
    assert box.check(_report(_standing("very_low"))) == "notice"
    assert box.check(_report(_standing("very_low"))) is None


def test_catalog_below_the_floor_is_mailed_as_an_incident():
    below = HealthIssue(
        "critical",
        "pharmonline_catalog_floor",
        "ниже границы",
        {"site": "pharmonline", "stage": "below"},
    )
    box = _Mailbox()
    assert box.check(_report(_standing("very_low"))) == "notice"

    assert box.check(_report(below)) == "incident"
    assert box.check(_report(below), hours=24) == "reminder"
    assert box.check(_report()) == "recovery"


def test_state_written_before_this_change_sends_nothing_new():
    """Состояние прода на 2026-10-08: активный инцидент, дорожки предупреждений нет."""
    report = _report(INCIDENT)
    sent_at = utcnow()
    state = {
        "version": 2,
        "status": "active",
        "signature": alert_signature(report),
        "incident_started_at": sent_at.isoformat(),
        "last_sent_at": sent_at.isoformat(),
    }
    box = _Mailbox(state)
    box.now = sent_at

    assert box.check(report) is None
    assert box.check(_report(INCIDENT, _standing())) == "reminder"
    assert box.state["incident_started_at"] == sent_at.isoformat()


def test_legacy_state_keeps_its_incident_when_a_warning_joins():
    """Состояние первой версии (`signature` + `sent_at`): предупреждение не новый инцидент."""
    warning = HealthIssue("warning", "site_silent", "silent", {"site": "pharmonline"})
    now = utcnow()
    legacy = {"signature": "site_silent:pharmonline", "sent_at": now.isoformat()}

    decision = health_alert_decision(
        _report(warning, _standing()), legacy, now=now + timedelta(hours=1), reminder_hours=24
    )

    assert decision.action == "reminder"
    assert decision.standing_keys == (LOW,)


@pytest.mark.parametrize(
    "standing",
    [
        "not-a-dict",
        {LOW: "garbage"},
        {LOW: 5},
        {"another||": "2026-10-08T10:00:00"},
    ],
)
def test_unreadable_standing_state_mails_again_rather_than_never(standing):
    now = utcnow()
    state = {
        "version": 2,
        "status": "ok",
        "signature": None,
        "last_sent_at": now.isoformat(),
        "standing": standing,
    }

    decision = health_alert_decision(_report(_standing()), state, now=now, reminder_hours=24)

    assert decision.action == "notice"


def test_a_warning_mailed_this_week_stays_quiet_after_other_mail():
    """Другое письмо переписывает состояние — недавняя запись при этом не теряется."""
    box = _Mailbox()
    assert box.check(_report(_standing("low"))) == "notice"
    assert box.check(_report(_standing("very_low")), hours=100) == "notice"

    assert box.check(_report(_standing("low"))) is None


def test_old_standing_entries_are_dropped_from_the_state():
    """Запись, по которой срок давно вышел, ни на что не влияет — файл не должен расти."""
    box = _Mailbox()
    assert box.check(_report(_standing("low"))) == "notice"
    assert box.check(_report(_standing("very_low")), hours=24) == "notice"
    assert set(box.state["standing"]) == {LOW, VERY_LOW}

    assert box.check(_report(_standing("very_low")), hours=5 * STANDING_REMINDER_HOURS) == "notice"

    assert set(box.state["standing"]) == {VERY_LOW}


# --- отправка и файл состояния ------------------------------------------------


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(
        main_mod.notifier, "send_email", lambda **kwargs: sent.append(kwargs) or True
    )
    return sent


def _dispatch(report: HealthReport, path: Path, now) -> str | None:
    return main_mod._dispatch_health_alert_email(
        report, alert_state_file=str(path), reminder_hours=24, now=now
    )


def test_dispatch_mails_the_warning_and_remembers_it_on_disk(tmp_path, outbox):
    path = tmp_path / "health.json"
    now = utcnow()
    warning = _report(_standing())

    assert _dispatch(warning, path, now) == "notice"
    # Своя тема: «WARNING» — тема нового инцидента.
    assert outbox[-1]["subject"] == "Pharmacy Monitor — NOTICE"
    assert "запас 250" in outbox[-1]["html_body"]
    assert "Долгое предупреждение" in outbox[-1]["html_body"]

    # Состояние пережило запись и строгую проверку при чтении.
    assert main_mod._read_health_alert_state(str(path))["standing"] == {LOW: now.isoformat()}
    assert _dispatch(warning, path, now + timedelta(hours=1)) is None
    assert _dispatch(warning, path, now + timedelta(days=6, hours=23)) is None
    assert _dispatch(warning, path, now + timedelta(days=7)) == "notice"
    assert len(outbox) == 2


def test_dispatch_recovery_names_the_closed_incident_and_what_remains(tmp_path, outbox):
    path = tmp_path / "health.json"
    now = utcnow()
    assert _dispatch(_report(INCIDENT, _standing()), path, now) == "incident"
    assert outbox[-1]["subject"] == "Pharmacy Monitor — CRITICAL"

    assert _dispatch(_report(_standing()), path, now + timedelta(hours=1)) == "recovery"

    assert outbox[-1]["subject"] == "Pharmacy Monitor — RECOVERED"
    assert "Инцидент закрыт" in outbox[-1]["html_body"]
    assert "запас 250" in outbox[-1]["html_body"]
    state = main_mod._read_health_alert_state(str(path))
    assert state["status"] == "ok" and state["signature"] is None
    assert _dispatch(_report(_standing()), path, now + timedelta(hours=2)) is None


def test_dispatch_keeps_the_warning_quiet_while_an_incident_is_active(tmp_path, outbox):
    """Дорожка предупреждений должна пережить чтение и у активного инцидента."""
    path = tmp_path / "health.json"
    now = utcnow()
    report = _report(INCIDENT, _standing())

    assert _dispatch(report, path, now) == "incident"
    assert _dispatch(report, path, now + timedelta(hours=1)) is None
    assert len(outbox) == 1


def test_recovery_without_leftovers_still_reads_all_checks_pass(tmp_path, outbox):
    path = tmp_path / "health.json"
    now = utcnow()
    _dispatch(_report(INCIDENT), path, now)

    assert _dispatch(_report(), path, now + timedelta(hours=1)) == "recovery"
    assert "All checks pass." in outbox[-1]["html_body"]
    assert "Инцидент закрыт" not in outbox[-1]["html_body"]


def test_broken_standing_entry_does_not_discard_the_incident_state(tmp_path, outbox):
    """Иначе битая запись о предупреждении дала бы повторное письмо об инциденте."""
    path = tmp_path / "health.json"
    now = utcnow()
    report = _report(INCIDENT, _standing())
    _dispatch(report, path, now)
    state = json.loads(path.read_text())
    state["standing"] = {LOW: "garbage"}
    path.write_text(json.dumps(state))

    restored = main_mod._read_health_alert_state(str(path))

    assert restored["status"] == "active"
    assert "standing" not in restored
    # О предупреждении напишут ещё раз — одним письмом, без нового инцидента.
    assert _dispatch(report, path, now + timedelta(hours=1)) == "reminder"
    assert len(outbox) == 2


def test_undelivered_warning_is_not_marked_as_sent(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    monkeypatch.setattr(main_mod.notifier, "send_email", lambda **_kwargs: False)

    with pytest.raises(RuntimeError, match="SMTP is not configured"):
        _dispatch(_report(_standing()), path, utcnow())

    assert not path.exists()


def test_health_check_command_says_why_the_warning_is_not_mailed_again(
    tmp_path, monkeypatch, outbox
):
    """Почасовой юнит: первое письмо, потом в журнале — причина молчания, код выхода 1."""
    path = tmp_path / "health.json"
    monkeypatch.setattr(main_mod.storage, "init_db", lambda: None)
    monkeypatch.setattr(main_mod.storage, "make_session", lambda: lambda: nullcontext(object()))
    monkeypatch.setattr(health, "check_health", lambda *_args, **_kwargs: _report(_standing()))
    command = ["health-check", "--alert-email", "--quiet-on-ok", "--alert-state-file", str(path)]

    first = CliRunner().invoke(main_mod.cli, command)
    second = CliRunner().invoke(main_mod.cli, command)

    assert (first.exit_code, second.exit_code) == (1, 1), (first.output, second.output)
    assert "Email с долгим предупреждением отправлен" in first.output
    assert "Email подавлен (долгое предупреждение: напоминание раз в 7 дней)" in second.output
    assert len(outbox) == 1
