from __future__ import annotations

from datetime import timedelta

import pytest
from click import ClickException
from click.testing import CliRunner
from sqlalchemy.orm import sessionmaker

from src import storage
from src import main as main_mod
from src._time import utcnow
from src.main import (
    classify_run_quality,
    mark_scrape_request_terminal,
    run_quality_baselines_for_sites,
    run_tenant_id_for_request,
    scope_category_run_sites,
)
from src.scrapers.base import (
    BaseScraper,
    CaptchaDetected,
    ScrapedProduct,
    ScrapeResult,
    SiteScrapeFatalError,
)


def _product(site: str, external_id: str, category: str = "cat") -> ScrapedProduct:
    return ScrapedProduct(
        site=site,
        external_id=external_id,
        url=f"https://{site}.az/{external_id}",
        name=f"Product {external_id}",
        category=category,
        price=1.0,
    )


def _result(
    site: str,
    product_count: int,
    *,
    expected: int = 2,
    completed: int = 2,
    failed: int = 0,
) -> ScrapeResult:
    return ScrapeResult(
        site=site,
        products=[_product(site, str(index)) for index in range(product_count)],
        items_expected=expected,
        items_completed=completed,
        items_failed=failed,
        item_results={
            f"cat-{index}": {"status": "ok", "products": 1} for index in range(completed)
        },
    )


@pytest.mark.parametrize(
    ("results", "sites", "enforce_baseline", "baselines", "expected_status"),
    [
        ([_result("aloe", 20)], ["aloe"], True, {"aloe": 20}, "ok"),
        ([_result("aloe", 20, completed=1, failed=1)], ["aloe"], False, {}, "degraded"),
        ([_result("aloe", 0, completed=0, failed=2)], ["aloe"], False, {}, "failed"),
        ([_result("aloe", 20)], ["aloe", "aptekonline"], False, {}, "degraded"),
        ([_result("aloe", 20)], ["aloe"], True, {"aloe": 100}, "degraded"),
    ],
)
def test_classify_run_quality_matrix(
    results,
    sites,
    enforce_baseline,
    baselines,
    expected_status,
):
    status, quality = classify_run_quality(
        results,
        sites,
        mode="category",
        baselines=baselines,
        enforce_baseline=enforce_baseline,
    )
    assert status == expected_status
    assert quality["financially_eligible"] is (expected_status == "ok" and enforce_baseline)


def test_intentional_partial_run_is_ok_but_not_financially_eligible():
    status, quality = classify_run_quality(
        [_result("aloe", 2, expected=1, completed=1)],
        ["aloe"],
        mode="category",
        enforce_baseline=False,
    )
    assert status == "ok"
    assert quality["full_catalog_verified"] is False
    assert quality["financially_eligible"] is False


def test_verified_public_api_full_run_is_financially_eligible():
    status, quality = classify_run_quality(
        [_result("pharmonline", 3, expected=1, completed=1)],
        ["pharmonline"],
        mode="public_api",
        baselines={"pharmonline": 3},
        enforce_baseline=True,
    )

    assert status == "ok"
    assert quality["full_catalog_verified"] is True
    assert quality["financially_eligible"] is True


@pytest.mark.parametrize(
    ("env_name", "env_value", "expected_message"),
    [
        (
            "PHARMONLINE_USE_DDP",
            "1",
            "cannot be combined with PHARMONLINE_USE_DDP",
        ),
        (
            "AI_FALLBACK_ENABLED",
            "true",
            "cannot be combined with AI_FALLBACK_ENABLED",
        ),
    ],
)
def test_public_api_recovery_refuses_mixed_sources(
    monkeypatch, env_name, env_value, expected_message
):
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.delenv("PHARMONLINE_USE_DDP", raising=False)
    monkeypatch.delenv("AI_FALLBACK_ENABLED", raising=False)
    monkeypatch.setenv(env_name, env_value)

    result = CliRunner().invoke(
        main_mod.cli,
        ["run", "--site", "pharmonline", "--mode", "public_api"],
    )

    assert result.exit_code != 0
    assert expected_message in result.output


def test_autonomous_marker_redirects_only_the_generic_pharmonline_timer(
    monkeypatch, tmp_path
):
    marker = tmp_path / "pharmonline-public-api-autonomous-v1"
    monkeypatch.setenv(
        "PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER", str(marker)
    )

    assert not main_mod._pharmonline_public_api_autonomous_mode_requested(
        ["pharmonline"], "auto"
    )

    marker.write_text(
        main_mod._PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER_CONTENT,
        encoding="utf-8",
    )
    assert main_mod._pharmonline_public_api_autonomous_mode_requested(
        ["pharmonline"], "auto"
    )
    assert not main_mod._pharmonline_public_api_autonomous_mode_requested(
        ["pharmonline", "aloe"], "auto"
    )
    assert not main_mod._pharmonline_public_api_autonomous_mode_requested(
        ["pharmonline"], "category"
    )


def test_autonomous_marker_replaces_legacy_ddp_for_its_process(monkeypatch):
    monkeypatch.setenv("PHARMONLINE_USE_DDP", "1")
    monkeypatch.setenv("AI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("SCRAPE_REPORT_EMAIL", "1")

    main_mod._enable_pharmonline_public_api_autonomous_mode()

    assert main_mod._pharmonline_public_api_enabled()
    assert main_mod.os.environ["PHARMONLINE_PUBLIC_API_TRANSPORT"] == "decodo"
    assert main_mod.os.environ["PHARMONLINE_DECODO_BACKCONNECT_STICKY"] == "1"
    assert main_mod.os.environ["PHARMONLINE_PUBLIC_API_REQUIRE_CATALOG_BASELINE"] == "required"
    assert main_mod.os.environ["PHARMONLINE_USE_DDP"] == "0"
    assert main_mod.os.environ["AI_FALLBACK_ENABLED"] == "0"
    assert main_mod.os.environ["SCRAPE_REPORT_EMAIL"] == "0"


def test_single_category_run_only_requests_sites_with_configured_route():
    quality_sites, scrape_scope = scope_category_run_sites(
        {
            "pharmonline": ["sinir-sistemi-xestelikeri"],
            "aptekonline": [],
            "aloe": [],
        },
        ["pharmonline", "aptekonline", "aloe"],
        category_id=9,
    )

    assert quality_sites == ["pharmonline"]
    assert scrape_scope == {"pharmonline": ["sinir-sistemi-xestelikeri"]}


def test_full_catalog_run_preserves_empty_sites_for_fail_closed_quality():
    original = {"pharmonline": ["one"], "aptekonline": [], "aloe": ["two"]}
    quality_sites, scrape_scope = scope_category_run_sites(
        original,
        ["pharmonline", "aptekonline", "aloe"],
        category_id=None,
    )

    assert quality_sites == ["pharmonline", "aptekonline", "aloe"]
    assert scrape_scope is original


def test_single_category_without_any_route_still_fails_closed():
    original = {"pharmonline": [], "aptekonline": [], "aloe": []}
    quality_sites, scrape_scope = scope_category_run_sites(
        original,
        ["pharmonline", "aptekonline", "aloe"],
        category_id=9,
    )

    assert quality_sites == ["pharmonline", "aptekonline", "aloe"]
    assert scrape_scope is original


def test_scraper_error_degrades_even_when_all_items_completed():
    result = _result("aloe", 20)
    result.errors = ["promos: RuntimeError: auxiliary endpoint failed"]
    status, quality = classify_run_quality(
        [result],
        ["aloe"],
        mode="category",
        enforce_baseline=True,
    )
    assert status == "degraded"
    assert quality["financially_eligible"] is False
    assert "scraper_errors" in quality["sites"]["aloe"]["reasons"]


def test_run_quality_caps_items_and_prioritizes_failures():
    result = _result("aloe", 600, expected=600, completed=599, failed=1)
    result.item_results = {
        **{f"ok-{index}": {"status": "ok", "products": 1} for index in range(599)},
        "failed-item": {"status": "failed", "products": 0},
    }
    status, quality = classify_run_quality(
        [result], ["aloe"], mode="category", enforce_baseline=True
    )
    site = quality["sites"]["aloe"]
    assert status == "degraded"
    assert len(site["items"]) == 500
    assert "failed-item" in site["items"]
    assert site["items_truncated"] == 100


def test_run_quality_caps_item_keys_and_values():
    result = _result("aloe", 1, expected=1, completed=0, failed=1)
    result.item_results = {
        "https://example.invalid/" + ("x" * 2_000): {
            "status": "failed" * 20,
            "products": 0,
            "error": "e" * 2_000,
            "error_kind": "kind" * 100,
            "untrusted_extra": "ignored",
        }
    }
    status, quality = classify_run_quality(
        [result], ["aloe"], mode="watchlist", enforce_baseline=False
    )
    item_key, item = next(iter(quality["sites"]["aloe"]["items"].items()))
    assert status == "degraded"
    assert len(item_key) == 300
    assert len(item["status"]) == 30
    assert len(item["error"]) == 500
    assert len(item["error_kind"]) == 50
    assert "untrusted_extra" not in item


class _DummyScraper(BaseScraper):
    site_name = "dummy"
    base_url = "https://dummy.invalid"

    def __init__(self, outcomes: dict[str, str]):
        super().__init__()
        self.outcomes = outcomes

    async def scrape_category(self, category_slug: str, limit: int | None = None):
        outcome = self.outcomes[category_slug]
        if outcome == "error":
            raise RuntimeError("boom")
        if outcome == "captcha":
            raise CaptchaDetected("blocked")
        if outcome == "ok":
            yield _product(self.site_name, category_slug, category_slug)

    async def scrape_product_page(self, url: str):
        if url.endswith("/error"):
            raise RuntimeError("url failed")
        if url.endswith("/missing"):
            return None
        return _product(self.site_name, url.rsplit("/", 1)[-1])


@pytest.mark.asyncio
async def test_scrape_site_initial_proxy_failure_returns_bounded_fatal_result(
    monkeypatch,
):
    class _FatalStartScraper(_DummyScraper):
        site_name = "fatal"

        def __init__(self):
            super().__init__({})

        async def __aenter__(self):
            raise SiteScrapeFatalError("Decodo proxy access rejected: HTTP 407")

    monkeypatch.setitem(main_mod.SCRAPER_CLASSES, "fatal", _FatalStartScraper)

    result = await main_mod.scrape_site("fatal", ["one", "two"], None)
    status, quality = classify_run_quality(
        [result], ["fatal"], mode="category", enforce_baseline=True
    )

    assert result.items_expected == 2
    assert result.items_failed == 2
    assert status == "failed"
    assert quality["financially_eligible"] is False
    assert quality["sites"]["fatal"]["reasons"] == ["site_fatal"]


@pytest.mark.asyncio
async def test_watchlist_partial_fatal_is_bounded_and_skips_remaining(monkeypatch):
    class _WatchFatalScraper(_DummyScraper):
        site_name = "watchfatal"

        def __init__(self):
            super().__init__({})

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def scrape_product_page(self, url):
            if url.endswith("/fatal"):
                raise SiteScrapeFatalError("proxy access rejected: HTTP 407")
            return _product(self.site_name, url.rsplit("/", 1)[-1])

    monkeypatch.setitem(main_mod.SCRAPER_CLASSES, "watchfatal", _WatchFatalScraper)
    urls = ["https://x/ok", "https://x/fatal", "https://x/skipped"]

    result = await main_mod.scrape_watchlist_for_site("watchfatal", urls)
    status, quality = classify_run_quality(
        [result], ["watchfatal"], mode="watchlist", enforce_baseline=False
    )

    assert len(result.products) == 1
    assert result.site_fatal is True
    assert result.items_completed == 1
    assert result.items_failed == 2
    assert result.item_results[urls[1]]["status"] == "failed"
    assert result.item_results[urls[2]]["status"] == "skipped"
    assert status == "failed"
    assert quality["financially_eligible"] is False


@pytest.mark.asyncio
async def test_watchlist_initial_fatal_returns_bounded_result(monkeypatch):
    class _WatchFatalStartScraper(_DummyScraper):
        site_name = "watchstartfatal"

        def __init__(self):
            super().__init__({})

        async def __aenter__(self):
            raise SiteScrapeFatalError("proxy access rejected: HTTP 407")

    monkeypatch.setitem(main_mod.SCRAPER_CLASSES, "watchstartfatal", _WatchFatalStartScraper)
    urls = ["https://x/one", "https://x/two"]

    result = await main_mod.scrape_watchlist_for_site("watchstartfatal", urls)

    assert result.site_fatal is True
    assert result.items_expected == 2
    assert result.items_failed == 2
    assert set(result.item_results) == set(urls)


@pytest.mark.asyncio
async def test_watchlist_promo_proxy_fatal_marks_site_failed_and_redacts(monkeypatch):
    class _WatchPromoFatalScraper(_DummyScraper):
        site_name = "watchpromofatal"

        def __init__(self):
            super().__init__({})

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def scrape_product_page(self, url):
            return _product(self.site_name, "ok")

        async def scrape_promos(self):
            raise RuntimeError(
                "proxy http://user:top-secret@az.decodo.com:30001 rejected "
                "net::ERR_PROXY_AUTH_REQUESTED"
            )

    monkeypatch.setitem(main_mod.SCRAPER_CLASSES, "watchpromofatal", _WatchPromoFatalScraper)

    result = await main_mod.scrape_watchlist_for_site("watchpromofatal", ["https://x/ok"])
    status, quality = classify_run_quality(
        [result], ["watchpromofatal"], mode="watchlist", enforce_baseline=False
    )

    payload = repr((result.errors, quality))
    assert result.site_fatal is True
    assert status == "failed"
    assert quality["financially_eligible"] is False
    assert "top-secret" not in payload
    assert "az.decodo.com" not in payload


@pytest.mark.asyncio
async def test_site_fatal_never_triggers_ai_fallback(monkeypatch):
    class _FatalPrimary:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def scrape(self, *args, **kwargs):
            return ScrapeResult(
                site="fatalprimary",
                products=[_product("fatalprimary", "partial")],
                errors=["site_fatal: proxy access rejected: HTTP 407"],
                site_fatal=True,
                items_expected=2,
                items_completed=1,
                items_failed=1,
            )

    monkeypatch.setitem(main_mod.SCRAPER_CLASSES, "fatalprimary", _FatalPrimary)

    def unexpected_fallback(*args, **kwargs):
        pytest.fail("AI fallback decision must be skipped after site_fatal")

    monkeypatch.setattr(main_mod, "_should_trigger_ai_fallback", unexpected_fallback)

    result = await main_mod.scrape_site(
        "fatalprimary", ["one", "two"], None, ai_fallback_baseline=1000
    )

    assert result.site_fatal is True


@pytest.mark.asyncio
async def test_ai_fallback_proxy_fatal_marks_partial_primary_site_failed(monkeypatch):
    class _PartialPrimary:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def scrape(self, *args, **kwargs):
            return _result("fallbackfatal", 1, expected=2, completed=1, failed=1)

    class _FatalFallback:
        async def __aenter__(self):
            raise SiteScrapeFatalError(
                "proxy http://user:top-secret@az.decodo.com:30001 rejected HTTP 407"
            )

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setitem(main_mod.SCRAPER_CLASSES, "fallbackfatal", _PartialPrimary)
    monkeypatch.setitem(main_mod.AI_CRAWLER_BY_SITE, "fallbackfatal", _FatalFallback)
    monkeypatch.setattr(main_mod, "_should_trigger_ai_fallback", lambda *args, **kwargs: True)

    result = await main_mod.scrape_site(
        "fallbackfatal", ["one", "two"], None, ai_fallback_baseline=1000
    )
    status, quality = classify_run_quality(
        [result], ["fallbackfatal"], mode="category", enforce_baseline=True
    )

    payload = repr((result.errors, quality))
    assert result.site_fatal is True
    assert status == "failed"
    assert quality["financially_eligible"] is False
    assert "top-secret" not in payload
    assert "az.decodo.com" not in payload


def test_partial_single_site_fatal_is_failed_but_mixed_run_is_degraded():
    fatal = _result("aloe", 1, expected=3, completed=1, failed=2)
    fatal.site_fatal = True
    fatal.errors = ["site_fatal: proxy access rejected: HTTP 407"]

    single_status, single_quality = classify_run_quality(
        [fatal], ["aloe"], mode="category", enforce_baseline=True
    )
    mixed_status, mixed_quality = classify_run_quality(
        [fatal, _result("aptekonline", 20)],
        ["aloe", "aptekonline"],
        mode="category",
        enforce_baseline=True,
    )

    assert single_status == "failed"
    assert single_quality["sites"]["aloe"]["status"] == "failed"
    assert single_quality["financially_eligible"] is False
    assert mixed_status == "degraded"
    assert mixed_quality["sites"]["aloe"]["status"] == "failed"
    assert mixed_quality["financially_eligible"] is False


@pytest.mark.asyncio
async def test_base_scraper_tracks_zero_error_and_captcha_categories():
    scraper = _DummyScraper({"ok": "ok", "empty": "empty", "error": "error", "captcha": "captcha"})
    result = await scraper.scrape(["ok", "empty", "error", "captcha"])
    assert result.items_expected == 4
    assert result.items_completed == 1
    assert result.items_failed == 3
    assert result.item_results["empty"]["status"] == "empty"
    assert result.item_results["error"]["error_kind"] == "exception"
    assert result.item_results["captcha"]["error_kind"] == "captcha"


@pytest.mark.asyncio
async def test_watchlist_callback_reports_success_missing_and_error():
    scraper = _DummyScraper({})
    outcomes: list[tuple[str, bool, str | None]] = []

    def record(url, product, error):
        outcomes.append((url, product is not None, error))

    products = await scraper.scrape_urls(
        ["https://dummy/ok", "https://dummy/missing", "https://dummy/error"],
        on_result=record,
    )
    assert len(products) == 1
    assert outcomes[0][1:] == (True, None)
    assert outcomes[1][1:] == (False, "product_not_found")
    assert outcomes[2][1] is False
    assert outcomes[2][2] == "RuntimeError: url failed"


def test_quality_baseline_uses_tenant_scoped_verified_median(db_session):
    for tenant_id, count in [(1, 100), (1, 110), (1, 120), (2, 9999)]:
        db_session.add(
            storage.Run(
                tenant_id=tenant_id,
                started_at=utcnow() - timedelta(minutes=count),
                status="ok",
                products_per_site={"aloe": count},
                run_quality={"financially_eligible": True},
            )
        )
    # Legacy intraday row has only one category and must not enter the baseline.
    db_session.add(
        storage.Run(
            tenant_id=1,
            status="ok",
            products_per_site={"aloe": 5},
            products_per_site_category={"aloe": {"one": 5}},
        )
    )
    db_session.commit()

    assert run_quality_baselines_for_sites(db_session, ["aloe"], tenant_id=1) == {"aloe": 110}


def test_degraded_run_is_copied_to_scrape_request_queue(db_session):
    run = storage.Run(
        tenant_id=1,
        status="degraded",
        error_message="aloe=degraded(incomplete_items)",
        run_quality={
            "financially_eligible": False,
            "sites": {"aloe": {"status": "degraded"}},
        },
    )
    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add_all([run, request])
    db_session.commit()

    updated = mark_scrape_request_terminal(db_session, request.id, run)

    assert updated is not None
    assert updated.status == "degraded"
    assert updated.run_id == run.id
    assert updated.completed_at is not None
    assert updated.error_message == run.error_message


def test_run_inherits_tenant_from_active_scrape_request(db_session):
    request = storage.ScrapeRequest(tenant_id=1, mode="all", status="running")
    db_session.add(request)
    db_session.commit()

    assert run_tenant_id_for_request(db_session, request.id) == 1

    request.status = "degraded"
    db_session.commit()
    with pytest.raises(ClickException, match="already degraded"):
        run_tenant_id_for_request(db_session, request.id)


def test_run_blocks_non_pilot_tenant_scrape_request(db_session):
    request = storage.ScrapeRequest(tenant_id=2, mode="all", status="running")
    db_session.add(request)
    db_session.commit()

    with pytest.raises(ClickException, match="not enabled for non-pilot tenants"):
        run_tenant_id_for_request(db_session, request.id)


def test_scrape_command_scopes_single_category_to_configured_sites(db_session, monkeypatch):
    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "init_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(storage, "make_session", lambda *args, **kwargs: Session)
    monkeypatch.setattr(
        main_mod, "_hold_scrape_lock_until_command_exit", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args, **kwargs: {})
    monkeypatch.setattr(main_mod, "run_quality_baselines_for_sites", lambda *args, **kwargs: {})

    slugs = {
        "pharmonline": ["sinir-sistemi-xestelikeri"],
        "aptekonline": [],
        "aloe": [],
    }
    monkeypatch.setattr(
        main_mod.watchlist,
        "categories_for_site",
        lambda _session, site, only_category_id=None: slugs[site],
    )
    captured = {}

    async def fake_scrape_all(slugs_by_site, limit, **kwargs):
        captured["scope"] = slugs_by_site
        return [_result("pharmonline", 1, expected=1, completed=1)]

    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "persist_results", lambda *args, **kwargs: 1)

    result = CliRunner().invoke(main_mod.cli, ["scrape", "--category-id", "9"])

    assert result.exit_code == 0, result.output
    assert captured["scope"] == {"pharmonline": ["sinir-sistemi-xestelikeri"]}
    run = db_session.query(storage.Run).one()
    assert run.status == "ok"
    assert set(run.run_quality["sites"]) == {"pharmonline"}
    assert run.run_quality["financially_eligible"] is False


def test_report_send_rejects_intentional_partial_run(db_session, monkeypatch):
    run = storage.Run(
        tenant_id=1,
        status="ok",
        run_quality={"financially_eligible": False, "sites": {}},
    )
    db_session.add(run)
    db_session.commit()
    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *args, **kwargs: Session)
    sent = []
    monkeypatch.setattr(main_mod.notifier, "send_email", lambda **kwargs: sent.append(kwargs))

    result = CliRunner().invoke(
        main_mod.cli,
        ["report", "--run-id", str(run.id), "--send"],
    )
    assert result.exit_code != 0
    assert "not a verified full-catalog run" in result.output
    assert sent == []


def test_report_send_rejects_missing_all_site_freshness(db_session, monkeypatch):
    now = utcnow()
    run = storage.Run(
        tenant_id=1,
        status="ok",
        started_at=now,
        finished_at=now,
        catalog_scope="full",
        full_catalog_sites="aloe",
        catalog_verified=True,
        catalog_verification_reason="test_single_site_full_catalog",
        run_quality={
            "baseline_enforced": True,
            "full_catalog_verified": True,
            "financially_eligible": True,
            "sites": {"aloe": {"status": "ok"}},
        },
    )
    db_session.add(run)
    db_session.commit()
    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda *args, **kwargs: Session)
    sent = []
    monkeypatch.setattr(main_mod.notifier, "send_email", lambda **kwargs: sent.append(kwargs))

    result = CliRunner().invoke(
        main_mod.cli,
        ["report", "--run-id", str(run.id), "--send"],
    )
    assert result.exit_code != 0
    assert "Fresh verified full-catalog inputs are missing" in result.output
    assert sent == []


def test_ai_crawl_fatal_creates_failed_run_with_sanitized_quality(db_session, monkeypatch):
    from src.scrapers import ai_crawler

    class _FatalAICrawler:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def crawl(self, *args, **kwargs):
            raise SiteScrapeFatalError(
                "proxy http://user:top-secret@az.decodo.com:30001 rejected HTTP 407"
            )

    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "init_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(storage, "make_session", lambda *args, **kwargs: Session)
    monkeypatch.setitem(ai_crawler.AI_CRAWLER_BY_SITE, "aloe", _FatalAICrawler)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    result = CliRunner().invoke(
        main_mod.cli,
        ["ai-crawl", "--site", "aloe", "--max-urls", "3"],
    )

    run = db_session.query(storage.Run).one()
    payload = repr((run.run_quality, run.error_message))
    assert result.exit_code != 0
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.run_quality["financially_eligible"] is False
    assert run.run_quality["sites"]["aloe"]["site_fatal"] is True
    assert "top-secret" not in payload
    assert "az.decodo.com" not in payload
