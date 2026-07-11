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
)
from src.scrapers.base import BaseScraper, CaptchaDetected, ScrapedProduct, ScrapeResult


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
            f"cat-{index}": {"status": "ok", "products": 1}
            for index in range(completed)
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
    assert quality["financially_eligible"] is (
        expected_status == "ok" and enforce_baseline
    )


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
async def test_base_scraper_tracks_zero_error_and_captcha_categories():
    scraper = _DummyScraper(
        {"ok": "ok", "empty": "empty", "error": "error", "captcha": "captcha"}
    )
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

    assert run_quality_baselines_for_sites(db_session, ["aloe"], tenant_id=1) == {
        "aloe": 110
    }


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
    run = storage.Run(
        tenant_id=1,
        status="ok",
        run_quality={
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
