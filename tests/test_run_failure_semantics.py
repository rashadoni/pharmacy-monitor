from __future__ import annotations

from types import SimpleNamespace

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


def test_verified_run_still_fails_when_ready_roi_refresh_returns_failure(
    db_session,
    monkeypatch,
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
        lambda session, *, run_id, tenant_id=1: {
            "pharmonline": 0,
            "aptekonline": -1,
            "aloe": 0,
        },
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
