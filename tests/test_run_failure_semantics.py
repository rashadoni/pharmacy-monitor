from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src import main as main_mod
from src import storage
from src._time import utcnow
from src.scrapers.base import (
    RouteStatus,
    ScrapedProduct,
    ScrapeResult,
    SiteScrapeFatalError,
    site_fatal_result,
)


def _session_factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)


def _patch_verified_aloe_pipeline(db_session, monkeypatch):
    from src import alerts

    async def fake_scrape_all(*args, **kwargs):
        return [
            ScrapeResult(
                site="aloe",
                products=[
                    ScrapedProduct(
                        site="aloe",
                        external_id="aloe-sku",
                        url="https://aloe.example/sku",
                        name="Trusted Aloe SKU",
                        category="cat",
                    )
                ],
                category_counts={"cat": 1},
                route_statuses={
                    "cat": RouteStatus(
                        complete=True,
                        raw_items=1,
                        parsed_items=1,
                        expected_items=1,
                    )
                },
            )
        ]

    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "shadow")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    monkeypatch.setenv("SCRAPE_REPORT_EMAIL", "0")
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: ["cat"],
    )
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: {"aloe": 1})
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "persist_results", lambda *args, **kwargs: 1)
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: None)
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})
    monkeypatch.setattr(main_mod, "_smoke_test_per_site_coverage", lambda *args: None)
    monkeypatch.setattr(main_mod.matcher, "match_products", lambda session: 0)
    monkeypatch.setattr(main_mod.matcher, "revalidate_split", lambda session: [])
    monkeypatch.setattr(main_mod.matcher, "flag_suspected_mismatches", lambda session: 0)
    monkeypatch.setattr(alerts, "evaluate_rules", lambda *args: [])
    monkeypatch.setattr(
        main_mod.analyzer,
        "analyze",
        lambda *args: SimpleNamespace(run_started_at=utcnow()),
    )
    monkeypatch.setattr(main_mod.reporter, "render_html", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "render_excel", lambda report: b"ok")
    monkeypatch.setattr(main_mod.reporter, "email_subject", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "excel_filename", lambda report: "ok.xlsx")


def test_revalidation_failure_marks_run_and_request_failed_before_outputs(db_session, monkeypatch):
    request = storage.ScrapeRequest(
        tenant_id=1,
        mode="all",
        status="running",
    )
    db_session.add(request)
    db_session.commit()
    request_id = request.id
    calls = {"alerts": 0, "analyze": 0, "roi": 0}

    async def fake_scrape_all(*args, **kwargs):
        return [
            ScrapeResult(
                site="aloe",
                products=[
                    ScrapedProduct(
                        site="aloe",
                        external_id="sku-1",
                        url="https://example.test/sku-1",
                        name="SKU 1",
                        category="cat",
                    )
                ],
                category_counts={"cat": 1},
                route_statuses={"cat": RouteStatus(complete=True)},
            )
        ]

    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: ["cat"],
    )
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: {"aloe": None})
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "persist_results", lambda *args, **kwargs: 1)
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: None)
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})
    monkeypatch.setattr(main_mod, "_smoke_test_per_site_coverage", lambda *args: None)
    monkeypatch.setattr(main_mod.matcher, "match_products", lambda session: 0)

    def fail_revalidate(session, *args, **kwargs):
        raise ValueError("country split failed")

    monkeypatch.setattr(main_mod.matcher, "revalidate_split", fail_revalidate)
    monkeypatch.setattr(
        main_mod.analyzer,
        "analyze",
        lambda *args: calls.__setitem__("analyze", calls["analyze"] + 1),
    )

    # Imports happen inside run_cmd, so patch the module-level functions too.
    from src import alerts, roi

    monkeypatch.setattr(
        alerts,
        "evaluate_rules",
        lambda *args: calls.__setitem__("alerts", calls["alerts"] + 1),
    )
    monkeypatch.setattr(
        roi,
        "refresh_all_cached_actions",
        lambda *args, **kwargs: calls.__setitem__("roi", calls["roi"] + 1),
    )

    result = CliRunner().invoke(
        main_mod.cli,
        [
            "run",
            "--site",
            "aloe",
            "--mode",
            "category",
            "--no-alerts",
            "--request-id",
            str(request_id),
        ],
    )

    assert result.exit_code != 0
    assert "identity revalidation failed" in result.output
    verify = _session_factory(db_session)()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        saved_request = verify.get(storage.ScrapeRequest, request_id)
        assert run is not None
        assert run.status == "failed"
        assert "country split failed" in (run.error_message or "")
        assert saved_request is not None
        assert saved_request.status == "failed"
        assert saved_request.run_id == run.id
        assert saved_request.completed_at is not None
    finally:
        verify.close()
    assert calls == {"alerts": 0, "analyze": 0, "roi": 0}


def test_public_api_recovery_refuses_persistence_after_identity_proof_failure(
    db_session, monkeypatch
):
    """A complete source still cannot write an untrusted ID→URL mapping."""
    from src.scrapers.pharmonline_public_api import (
        PUBLIC_API_AVAILABILITY_SOURCE,
        PUBLIC_CATALOG_ROUTE,
    )

    async def untrusted_catalog(*args, **kwargs):
        return [
            ScrapeResult(
                site="pharmonline",
                products=[
                    ScrapedProduct(
                        site="pharmonline",
                        external_id="xwJspdCx3iFBDqDWF",
                        url="https://pharmonline.az/product/untrusted-product",
                        name="Untrusted public API product",
                        identity_verified=True,
                        availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
                    )
                ],
                category_counts={PUBLIC_CATALOG_ROUTE: 1},
                route_statuses={
                    PUBLIC_CATALOG_ROUTE: RouteStatus(
                        complete=True,
                        raw_items=1,
                        parsed_items=1,
                        expected_items=1,
                    )
                },
            )
        ]

    persisted = []
    Session = _session_factory(db_session)
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.setenv("PHARMONLINE_USE_DDP", "0")
    monkeypatch.setenv("SCRAPE_REPORT_EMAIL", "0")
    monkeypatch.delenv("PHARMONLINE_LEGACY_ID_BRIDGE", raising=False)
    monkeypatch.delenv("AI_FALLBACK_ENABLED", raising=False)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: Session)
    monkeypatch.setattr(
        main_mod, "_hold_scrape_lock_until_command_exit", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod,
        "baselines_for_sites",
        lambda *args, **kwargs: {"pharmonline": 1},
    )
    monkeypatch.setattr(
        main_mod,
        "run_quality_baselines_for_sites",
        lambda *args, **kwargs: {"pharmonline": 1},
    )
    monkeypatch.setattr(main_mod, "scrape_all", untrusted_catalog)
    monkeypatch.setattr(
        main_mod,
        "persist_results",
        lambda *args, **kwargs: persisted.append("called"),
    )

    result = CliRunner().invoke(
        main_mod.cli,
        ["run", "--site", "pharmonline", "--mode", "public_api", "--no-alerts"],
    )

    assert result.exit_code != 0
    assert "identity proof refused persistence" in result.output
    assert persisted == []
    verify = Session()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        assert run is not None
        assert run.status == "failed"
        assert run.catalog_verified is False
        assert (run.catalog_verification_reason or "").startswith(
            "public_api_identity_proof_failed:"
        )
        assert "PharmonlinePublicAPIIdentityError" in (run.error_message or "")
        assert verify.scalars(select(storage.Product)).all() == []
        assert verify.scalars(select(storage.OfferObservation)).all() == []
        assert verify.scalars(select(storage.PriceSnapshot)).all() == []
    finally:
        verify.close()


def test_rematch_revalidate_dissolve_cli_uses_unmatched_key(db_session, monkeypatch):
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(
        main_mod.matcher,
        "revalidate_split",
        lambda session, dry_run=False: [{"action": "dissolve", "match_id": 9, "unmatched": [1, 2]}],
    )

    result = CliRunner().invoke(main_mod.cli, ["rematch", "--revalidate"])

    assert result.exit_code == 0
    assert "DISSOLVE [1, 2]" in result.output
    assert "revalidate: re-split 1" in result.output


def test_verified_full_run_finalizes_outputs_before_external_publish(db_session, monkeypatch):
    from src import alerts, roi
    from src.product_policy import policy_rollout_eligibility

    sites = ("pharmonline", "aptekonline", "aloe")

    async def fake_scrape_all(*args, **kwargs):
        return [
            ScrapeResult(
                site=site,
                products=[
                    ScrapedProduct(
                        site=site,
                        external_id=f"{site}-sku",
                        url=f"https://{site}.example/sku",
                        name="Trusted SKU",
                        category=f"{site}-cat",
                        manufacturer_country_raw="Latvia",
                        country_source="fixture",
                        offer_availability_status="in_stock",
                        offer_quantity=1,
                        availability_source="fixture",
                    )
                ],
                category_counts={f"{site}-cat": 1},
                route_statuses={
                    f"{site}-cat": RouteStatus(
                        complete=True,
                        raw_items=1,
                        parsed_items=1,
                        expected_items=1,
                    )
                },
            )
            for site in sites
        ]

    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "enforce")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "enforce")
    monkeypatch.setenv("SCRAPE_REPORT_EMAIL", "0")
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: [f"{site}-cat"],
    )
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: dict.fromkeys(sites))
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: None)
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})
    monkeypatch.setattr(main_mod, "_smoke_test_per_site_coverage", lambda *args: None)
    monkeypatch.setattr(main_mod.matcher, "match_products", lambda session: 0)
    monkeypatch.setattr(main_mod.matcher, "revalidate_split", lambda session: [])
    monkeypatch.setattr(main_mod.matcher, "flag_suspected_mismatches", lambda session: 0)

    phases: list[str] = []

    def fake_evaluate(session, run_id):
        current = session.get(storage.Run, run_id)
        assert current.status == "running"
        assert policy_rollout_eligibility(session).eligible is True
        phases.append("alerts")
        return []

    def fake_analyze(session, run_id):
        current = session.get(storage.Run, run_id)
        assert current.status == "running"
        assert policy_rollout_eligibility(session).eligible is True
        phases.append("analyze")
        return SimpleNamespace(run_started_at=utcnow())

    monkeypatch.setattr(alerts, "evaluate_rules", fake_evaluate)
    monkeypatch.setattr(main_mod.analyzer, "analyze", fake_analyze)
    monkeypatch.setattr(main_mod.reporter, "render_html", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "render_excel", lambda report: b"ok")
    monkeypatch.setattr(main_mod.reporter, "email_subject", lambda report: "ok")
    monkeypatch.setattr(main_mod.reporter, "excel_filename", lambda report: "ok.xlsx")

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main_mod.cli, ["run", "--mode", "category"])

    assert result.exit_code == 0, result.output
    verify = _session_factory(db_session)()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        assert run is not None
        assert run.status == "ok"
        assert run.catalog_verified is True
        assert phases == ["alerts", "analyze"]
        rows = verify.scalars(select(storage.RoiActionsCache)).all()
        assert {row.client_site for row in rows} == set(sites)
        assert all(f":{run.id}" in row.trust_epoch for row in rows)
        assert roi.get_cached_actions(verify, "pharmonline") == []
    finally:
        verify.close()


def test_verified_single_site_run_defers_roi_when_other_site_attempt_is_degraded(
    db_session,
    monkeypatch,
):
    from src import roi

    sites = ("pharmonline", "aptekonline", "aloe")
    old_verified = storage.Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=utcnow(),
        status="ok",
        catalog_scope="full",
        full_catalog_sites=",".join(sites),
        catalog_verified=True,
        catalog_verification_reason="verified fixture",
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {site: {"status": "ok"} for site in sites},
        },
    )
    db_session.add(old_verified)
    db_session.commit()

    degraded_aptek = storage.Run(
        tenant_id=1,
        started_at=utcnow(),
        finished_at=utcnow(),
        status="degraded",
        catalog_scope="full",
        full_catalog_sites="aptekonline",
        catalog_verified=False,
        catalog_verification_reason="fixture route loss",
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": False,
            "financially_eligible": False,
            "sites": {"aptekonline": {"status": "degraded"}},
        },
    )
    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add_all([degraded_aptek, request])
    db_session.commit()
    degraded_aptek_id = degraded_aptek.id
    request_id = request.id

    assert roi.financial_inputs_are_fresh(db_session, tenant_id=1) is False
    _patch_verified_aloe_pipeline(db_session, monkeypatch)
    refresh_calls: list[int] = []

    def forbidden_refresh(session, *, run_id, tenant_id=1):
        refresh_calls.append(run_id)
        raise AssertionError("ROI refresh must be deferred while inputs are unverified")

    monkeypatch.setattr(roi, "refresh_all_cached_actions", forbidden_refresh)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main_mod.cli,
            [
                "run",
                "--site",
                "aloe",
                "--mode",
                "category",
                "--no-alerts",
                "--request-id",
                str(request_id),
            ],
        )

    assert result.exit_code == 0, result.output
    verify = _session_factory(db_session)()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        saved_request = verify.get(storage.ScrapeRequest, request_id)
        assert run is not None
        assert run.status == "ok"
        assert run.catalog_verified is True
        assert run.error_message is None
        assert saved_request is not None
        assert saved_request.status == "ok"
        assert saved_request.run_id == run.id
        attempts = storage.latest_full_catalog_attempts_by_site(
            verify,
            sites,
            tenant_id=1,
        )
        assert attempts["aloe"].id == run.id
        assert attempts["aptekonline"].id == degraded_aptek_id
        assert roi.financial_inputs_are_fresh(verify, tenant_id=1) is False
    finally:
        verify.close()
    assert refresh_calls == []


@pytest.mark.parametrize(
    "summary",
    [
        {"pharmonline": 0, "aptekonline": -1, "aloe": 0},
        # Срез упал, а на следующем расчёт отказался: ответ неполный, но сбой в нём есть.
        {"pharmonline": 0, "aptekonline": -1},
    ],
)
def test_verified_run_still_fails_when_ready_roi_refresh_returns_failure(
    db_session,
    monkeypatch,
    summary,
):
    from src import roi

    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add(request)
    db_session.commit()
    request_id = request.id

    _patch_verified_aloe_pipeline(db_session, monkeypatch)
    monkeypatch.setattr(
        roi,
        "financial_inputs_are_fresh",
        lambda session, *, tenant_id: True,
    )
    monkeypatch.setattr(
        roi,
        "refresh_all_cached_actions",
        lambda session, *, run_id, tenant_id=1: summary,
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main_mod.cli,
            [
                "run",
                "--site",
                "aloe",
                "--mode",
                "category",
                "--no-alerts",
                "--request-id",
                str(request_id),
            ],
        )

    assert result.exit_code != 0
    assert "ROI refresh failed for trusted epoch: aptekonline" in result.output
    verify = _session_factory(db_session)()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        saved_request = verify.get(storage.ScrapeRequest, request_id)
        assert run is not None
        assert run.status == "failed"
        assert run.catalog_verified is True
        assert "ROI refresh failed for trusted epoch: aptekonline" in (run.error_message or "")
        assert saved_request is not None
        assert saved_request.status == "failed"
        assert saved_request.run_id == run.id
    finally:
        verify.close()


def _trusted_catalog_with_one_recommendation(db_session) -> None:
    """Подтверждённый каталог всех сайтов и пара, по которой есть что советовать."""
    from tests.test_roi_refresh import _cluster, _full_run

    trusted = _full_run(db_session)
    _cluster(db_session, trusted, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})


def _run_verified_aloe(request_id: int):
    runner = CliRunner()
    with runner.isolated_filesystem():
        return runner.invoke(
            main_mod.cli,
            [
                "run",
                "--site",
                "aloe",
                "--mode",
                "category",
                "--no-alerts",
                "--request-id",
                str(request_id),
            ],
        )


def test_orphan_run_neither_fails_a_verified_run_nor_empties_recommendations(
    db_session,
    monkeypatch,
):
    """Строка `running` от упавшего раньше тика мешает расчёту в конце сбора.

    Сбор от этого не становится failed, а в кэш не ложится пустой список —
    дашборд показывал бы «Нет рекомендаций» до следующего полного сбора. Прогон
    оставляет заявку; watcher снимает сироту и тем же тиком считает.
    """
    from src import roi, roi_refresh

    _trusted_catalog_with_one_recommendation(db_session)
    # Рекомендации прошлого сбора уже лежат в кэше.
    assert roi_refresh.refresh_from_trusted_epoch(db_session).outcome == "refreshed"
    cached_before = {
        row.client_site: (row.run_id, row.computed_at, list(row.payload))
        for row in db_session.scalars(select(storage.RoiActionsCache)).all()
    }
    assert len(cached_before) == 3
    orphan = storage.Run(tenant_id=1, started_at=utcnow(), finished_at=None, status="running")
    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add_all([orphan, request])
    db_session.commit()
    request_id = request.id
    _patch_verified_aloe_pipeline(db_session, monkeypatch)

    result = _run_verified_aloe(request_id)

    assert result.exit_code == 0, result.output
    verify = _session_factory(db_session)()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        saved_request = verify.get(storage.ScrapeRequest, request_id)
        assert run is not None
        assert run.status == "ok"
        assert run.catalog_verified is True
        assert run.error_message is None
        assert saved_request is not None
        assert saved_request.status == "ok"
        # Кэш не тронут: ни пустого списка, ни удаления. Прежние рекомендации
        # при этом не отдаются — они от прошлой эпохи каталога.
        cached_after = {
            row.client_site: (row.run_id, row.computed_at, list(row.payload))
            for row in verify.scalars(select(storage.RoiActionsCache)).all()
        }
        assert cached_after == cached_before
        assert roi.get_cached_actions(verify, "pharmonline") is None
        owed = verify.scalars(select(storage.RoiRefreshRequest)).all()
        assert [(item.reason, item.status) for item in owed] == [("full_run_deferred", "pending")]

        # Пока сирота висит, пересчёт ждёт и заявку не закрывает.
        assert roi_refresh.run_refresh(verify, only_if_requested=True).outcome == "busy"
        assert main_mod.reap_stale_running_runs(verify, max_age_hours=0) == 1
        refreshed = roi_refresh.run_refresh(verify, only_if_requested=True)

        assert (refreshed.outcome, refreshed.run_id) == ("refreshed", run.id)
        cached = roi.get_cached_actions(verify, "pharmonline")
        assert cached is not None
        assert [item["type"] for item in cached] == ["price_raise"]
    finally:
        verify.close()


def test_closed_policy_gate_neither_fails_a_verified_run_nor_empties_recommendations(
    db_session,
    monkeypatch,
):
    """Гейт политики закрыт: расчёта нет, но и ответа «рекомендаций нет» тоже.

    Заявка не ставится: пересчёт без сбора упёрся бы в тот же гейт и закрыл её
    `skipped` — открывает его только следующий подтверждённый полный сбор.
    """
    from src import product_policy, roi

    _trusted_catalog_with_one_recommendation(db_session)
    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add(request)
    db_session.commit()
    request_id = request.id
    _patch_verified_aloe_pipeline(db_session, monkeypatch)
    monkeypatch.setattr(
        product_policy,
        "policy_rollout_eligibility",
        lambda *args, **kwargs: product_policy.Eligibility(False, "full_catalog_trust_not_ready"),
    )

    result = _run_verified_aloe(request_id)

    assert result.exit_code == 0, result.output
    verify = _session_factory(db_session)()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        assert run is not None
        assert run.status == "ok"
        assert run.error_message is None
        assert verify.scalars(select(storage.RoiActionsCache)).all() == []
        assert roi.get_cached_actions(verify, "pharmonline") is None
        assert verify.scalars(select(storage.RoiRefreshRequest)).all() == []
    finally:
        verify.close()


@pytest.mark.parametrize("error", [ValueError("wrong run"), RuntimeError("epoch is gone")])
def test_only_a_refusal_to_compute_is_forgiven_at_the_end_of_a_verified_run(
    db_session, monkeypatch, error
):
    """Любая другая ошибка пересчёта по-прежнему роняет прогон."""
    from src import roi

    _trusted_catalog_with_one_recommendation(db_session)
    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add(request)
    db_session.commit()
    _patch_verified_aloe_pipeline(db_session, monkeypatch)

    def broken_refresh(session, *, run_id, tenant_id=1):
        raise error

    monkeypatch.setattr(roi, "refresh_all_cached_actions", broken_refresh)

    result = _run_verified_aloe(request.id)

    assert result.exit_code != 0
    verify = _session_factory(db_session)()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        assert run is not None
        assert run.status == "failed"
        assert str(error) in (run.error_message or "")
        assert verify.scalars(select(storage.RoiRefreshRequest)).all() == []
    finally:
        verify.close()


def test_verified_run_writes_recommendations_when_nothing_blocks_them(db_session, monkeypatch):
    """Парный к двум тестам выше: та же обвязка без помехи кэш пишет."""
    from src import roi

    _trusted_catalog_with_one_recommendation(db_session)
    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add(request)
    db_session.commit()
    _patch_verified_aloe_pipeline(db_session, monkeypatch)

    result = _run_verified_aloe(request.id)

    assert result.exit_code == 0, result.output
    verify = _session_factory(db_session)()
    try:
        cached = roi.get_cached_actions(verify, "pharmonline")
        assert cached is not None
        assert [item["type"] for item in cached] == ["price_raise"]
        assert verify.scalars(select(storage.RoiRefreshRequest)).all() == []
    finally:
        verify.close()


def test_incomplete_full_run_is_degraded_and_stops_before_consumers(db_session, monkeypatch):
    from src import alerts, roi

    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add(request)
    db_session.commit()
    request_id = request.id
    calls = {"match": 0, "alerts": 0, "analyze": 0, "roi": 0}

    async def fake_scrape_all(*args, **kwargs):
        return [
            ScrapeResult(
                site="aloe",
                products=[
                    ScrapedProduct(
                        site="aloe",
                        external_id="sku-1",
                        url="https://aloe.example/sku-1",
                        name="SKU 1",
                        category="cat",
                    )
                ],
                category_counts={"cat": 1},
                route_statuses={
                    "cat": RouteStatus(
                        complete=False,
                        abort_reason="item_parse_failures",
                        raw_items=2,
                        parsed_items=1,
                        item_failures=1,
                        expected_items=2,
                    )
                },
            )
        ]

    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: ["cat"],
    )
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: {"aloe": 2})
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "persist_results", lambda *args, **kwargs: 1)
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: None)
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})
    monkeypatch.setattr(main_mod, "_smoke_test_per_site_coverage", lambda *args: None)
    monkeypatch.setattr(
        main_mod.matcher,
        "match_products",
        lambda session: calls.__setitem__("match", calls["match"] + 1),
    )
    monkeypatch.setattr(
        alerts,
        "evaluate_rules",
        lambda *args: calls.__setitem__("alerts", calls["alerts"] + 1),
    )
    monkeypatch.setattr(
        main_mod.analyzer,
        "analyze",
        lambda *args: calls.__setitem__("analyze", calls["analyze"] + 1),
    )
    monkeypatch.setattr(
        roi,
        "refresh_all_cached_actions",
        lambda *args, **kwargs: calls.__setitem__("roi", calls["roi"] + 1),
    )

    result = CliRunner().invoke(
        main_mod.cli,
        [
            "run",
            "--site",
            "aloe",
            "--mode",
            "category",
            "--request-id",
            str(request_id),
        ],
    )

    assert result.exit_code != 0
    assert "full catalog verification failed" in result.output
    verify = _session_factory(db_session)()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        saved_request = verify.get(storage.ScrapeRequest, request_id)
        assert run is not None
        assert run.status == "degraded"
        assert run.catalog_verified is False
        assert "aloe" in (run.catalog_verification_reason or "")
        assert "FullCatalogVerificationError" in (run.error_message or "")
        assert saved_request is not None
        assert saved_request.status == "degraded"
        assert saved_request.run_id == run.id
    finally:
        verify.close()
    assert calls == {"match": 0, "alerts": 0, "analyze": 0, "roi": 0}


def test_single_site_startup_failure_stays_failed_with_sanitized_cause(db_session, monkeypatch):
    from src import alerts, roi

    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add(request)
    db_session.commit()
    request_id = request.id
    calls = {"match": 0, "alerts": 0, "analyze": 0, "roi": 0}

    async def fake_scrape_all(*args, **kwargs):
        return [
            site_fatal_result(
                "pharmonline",
                ["cat"],
                SiteScrapeFatalError(
                    "DDP startup failed: OSError: opening handshake via "
                    "http://proxy-user:run-secret@az.decodo.com:30001 timed out"
                ),
            )
        ]

    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: ["cat"],
    )
    monkeypatch.setattr(
        main_mod,
        "baselines_for_sites",
        lambda *args, **kwargs: {"pharmonline": 1},
    )
    monkeypatch.setattr(
        main_mod,
        "run_quality_baselines_for_sites",
        lambda *args, **kwargs: {"pharmonline": 1},
    )
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "persist_results", lambda *args, **kwargs: 0)
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: None)
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})
    monkeypatch.setattr(
        main_mod.matcher,
        "match_products",
        lambda session: calls.__setitem__("match", calls["match"] + 1),
    )
    monkeypatch.setattr(
        alerts,
        "evaluate_rules",
        lambda *args: calls.__setitem__("alerts", calls["alerts"] + 1),
    )
    monkeypatch.setattr(
        main_mod.analyzer,
        "analyze",
        lambda *args: calls.__setitem__("analyze", calls["analyze"] + 1),
    )
    monkeypatch.setattr(
        roi,
        "refresh_all_cached_actions",
        lambda *args, **kwargs: calls.__setitem__("roi", calls["roi"] + 1),
    )

    result = CliRunner().invoke(
        main_mod.cli,
        [
            "run",
            "--site",
            "pharmonline",
            "--mode",
            "category",
            "--request-id",
            str(request_id),
        ],
    )

    assert result.exit_code != 0
    assert "run quality failed" in result.output
    verify = _session_factory(db_session)()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        saved_request = verify.get(storage.ScrapeRequest, request_id)
        assert run is not None
        assert run.status == "failed"
        assert run.catalog_verified is False
        assert run.run_quality["sites"]["pharmonline"]["status"] == "failed"
        assert "DDP startup failed" in (run.error_message or "")
        assert "[redacted-proxy-url]" in (run.error_message or "")
        assert "run-secret" not in (run.error_message or "")
        assert "az.decodo.com" not in (run.error_message or "")
        assert saved_request is not None
        assert saved_request.status == "failed"
        assert saved_request.run_id == run.id
    finally:
        verify.close()
    assert calls == {"match": 0, "alerts": 0, "analyze": 0, "roi": 0}


def test_multi_site_startup_failure_is_degraded_and_keeps_healthy_result(db_session, monkeypatch):
    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add(request)
    db_session.commit()
    request_id = request.id
    persisted_sites = []

    async def fake_scrape_all(*args, **kwargs):
        return [
            site_fatal_result(
                "pharmonline",
                ["cat"],
                SiteScrapeFatalError("DDP startup failed: TimeoutError: handshake timed out"),
            ),
            ScrapeResult(
                site="aloe",
                products=[
                    ScrapedProduct(
                        site="aloe",
                        external_id="healthy-sku",
                        url="https://aloe.example/healthy-sku",
                        name="Healthy SKU",
                        category="cat",
                    )
                ],
                category_counts={"cat": 1},
                route_statuses={
                    "cat": RouteStatus(
                        complete=True,
                        expected_pages=1,
                        visited_pages=1,
                        raw_items=1,
                        parsed_items=1,
                        expected_items=1,
                    )
                },
            ),
        ]

    def fake_persist(session, run, results):
        persisted_sites.extend(result.site for result in results)
        return sum(len(result.products) for result in results)

    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: ["cat"],
    )
    monkeypatch.setattr(
        main_mod,
        "baselines_for_sites",
        lambda *args, **kwargs: {"pharmonline": 1, "aloe": 1},
    )
    monkeypatch.setattr(
        main_mod,
        "run_quality_baselines_for_sites",
        lambda *args, **kwargs: {"pharmonline": 1, "aloe": 1},
    )
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "persist_results", fake_persist)
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: None)
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})

    result = CliRunner().invoke(
        main_mod.cli,
        [
            "run",
            "--site",
            "pharmonline",
            "--site",
            "aloe",
            "--mode",
            "category",
            "--request-id",
            str(request_id),
        ],
    )

    assert result.exit_code != 0
    assert "full catalog verification failed" in result.output
    verify = _session_factory(db_session)()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        saved_request = verify.get(storage.ScrapeRequest, request_id)
        assert run is not None
        assert run.status == "degraded"
        assert run.products_scraped == 1
        assert run.products_per_site == {"pharmonline": 0, "aloe": 1}
        assert run.run_quality["sites"]["pharmonline"]["status"] == "failed"
        assert run.run_quality["sites"]["aloe"]["status"] == "ok"
        assert "DDP startup failed" in (run.error_message or "")
        assert saved_request is not None
        assert saved_request.status == "degraded"
        assert saved_request.run_id == run.id
    finally:
        verify.close()
    assert persisted_sites == ["pharmonline", "aloe"]


def test_scrape_command_is_non_publishing_diagnostic_producer(db_session, monkeypatch):
    async def fake_scrape_all(*args, **kwargs):
        return [
            ScrapeResult(
                site="aloe",
                products=[
                    ScrapedProduct(
                        site="aloe",
                        external_id="diagnostic-sku",
                        url="https://aloe.example/diagnostic",
                        name="Diagnostic SKU",
                        category="cat",
                    )
                ],
                category_counts={"cat": 1},
                route_statuses={"cat": RouteStatus(complete=True)},
            )
        ]

    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: _session_factory(db_session))
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda session, site, only_category_id=None: ["cat"],
    )
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: {"aloe": 1})
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "persist_results", lambda *args, **kwargs: 1)
    monkeypatch.setattr(main_mod, "persist_aloe_country_mappings", lambda *args: None)
    monkeypatch.setattr(main_mod, "load_aloe_country_map", lambda *args: {})

    result = CliRunner().invoke(main_mod.cli, ["scrape", "--site", "aloe"])

    assert result.exit_code == 0, result.output
    verify = _session_factory(db_session)()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        assert run is not None
        assert run.status == "ok"
        assert run.catalog_scope == "partial"
        assert run.catalog_verified is False
        assert run.catalog_verification_reason == "scrape_command_diagnostic_non_publishing"
        assert run.full_catalog_sites is None
        assert run.run_quality is not None
        assert run.run_quality["full_catalog_verified"] is False
        assert run.run_quality["financially_eligible"] is False
        assert storage.run_is_financially_eligible(run) is False
        assert storage.financially_eligible_run_ids(verify, tenant_id=run.tenant_id) == []
    finally:
        verify.close()


def test_public_api_recovery_refuses_persistence_after_catalog_proof_failure(
    db_session, monkeypatch
):
    """The recovery source must not refresh any SKU when its proof is incomplete."""
    from src.scrapers.pharmonline_public_api import PUBLIC_CATALOG_ROUTE

    async def failed_catalog(*args, **kwargs):
        return [
            ScrapeResult(
                site="pharmonline",
                category_counts={PUBLIC_CATALOG_ROUTE: 0},
                route_statuses={
                    PUBLIC_CATALOG_ROUTE: RouteStatus(
                        complete=False,
                        abort_reason="products_sitemap_set_mismatch",
                    )
                },
            )
        ]

    persisted = []
    Session = _session_factory(db_session)
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.setenv("PHARMONLINE_USE_DDP", "0")
    monkeypatch.setenv("SCRAPE_REPORT_EMAIL", "0")
    monkeypatch.delenv("PHARMONLINE_LEGACY_ID_BRIDGE", raising=False)
    monkeypatch.delenv("AI_FALLBACK_ENABLED", raising=False)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: Session)
    monkeypatch.setattr(
        main_mod, "_hold_scrape_lock_until_command_exit", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(
        main_mod,
        "baselines_for_sites",
        lambda *args, **kwargs: {"pharmonline": 1},
    )
    monkeypatch.setattr(
        main_mod,
        "run_quality_baselines_for_sites",
        lambda *args, **kwargs: {"pharmonline": 1},
    )
    monkeypatch.setattr(main_mod, "scrape_all", failed_catalog)
    monkeypatch.setattr(
        main_mod,
        "persist_results",
        lambda *args, **kwargs: persisted.append("called"),
    )

    result = CliRunner().invoke(
        main_mod.cli,
        ["run", "--site", "pharmonline", "--mode", "public_api", "--no-alerts"],
    )

    assert result.exit_code != 0
    assert "run quality failed" in result.output
    assert persisted == []
    verify = Session()
    try:
        run = verify.scalar(select(storage.Run).order_by(storage.Run.id.desc()))
        assert run is not None
        assert run.status == "failed"
        assert run.catalog_verified is False
        assert "products_sitemap_set_mismatch" in (run.catalog_verification_reason or "")
        assert "FullCatalogVerificationError" not in (run.error_message or "")
    finally:
        verify.close()
