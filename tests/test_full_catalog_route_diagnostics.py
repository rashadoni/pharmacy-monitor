"""Full-catalog verification must say WHY a route is incomplete, not just count.

Production run 470 (aloe, 2026-07-15) recorded only `incomplete_routes=aloe:6`.
Six routes failed and the cause existed solely in memory, so the failure was
undiagnosable after the fact — and the causes need opposite fixes
(`pagination_incomplete` = scraper under-fetched; `item_parse_failures` on a
99.9%-complete run = this check is too strict).
"""

import pytest

from src.main import _route_incomplete_reason, _verify_full_catalog_results
from src.scrapers.base import RouteStatus, ScrapeResult


def _result(site="aloe", route_statuses=None, products=(1,), errors=()):
    r = ScrapeResult(site=site)
    r.products = list(products)
    r.errors = list(errors)
    r.route_statuses = route_statuses or {}
    r.category_counts = {slug: 1 for slug in (route_statuses or {})}
    return r


def _verify(route_statuses, slugs):
    return _verify_full_catalog_results(
        [_result(route_statuses=route_statuses)],
        sites=["aloe"],
        expected_slugs={"aloe": slugs},
        baselines={"aloe": None},
    )


# ─── the helper names each condition ─────────────────────────────────────────


def test_missing_route_status_is_named():
    assert _route_incomplete_reason(None) == "missing_route_status"


def test_item_parse_failures_are_named_with_a_count():
    st = RouteStatus(complete=False, item_failures=3)
    assert _route_incomplete_reason(st) == "item_parse_failures(3)"


def test_pages_skipped_is_named_with_a_count():
    st = RouteStatus(complete=True, pages_skipped=2)
    assert _route_incomplete_reason(st) == "pages_skipped(2)"


def test_item_count_mismatch_reports_all_three_numbers():
    st = RouteStatus(complete=True, raw_items=9, parsed_items=9, expected_items=10)
    reason = _route_incomplete_reason(st)
    assert "item_count_mismatch" in reason
    assert "raw=9" in reason and "parsed=9" in reason and "expected=10" in reason


def test_scraper_abort_reason_is_surfaced_verbatim():
    st = RouteStatus(complete=False, abort_reason="pagination_incomplete")
    assert _route_incomplete_reason(st) == "pagination_incomplete"


def test_incomplete_without_abort_reason_falls_back_to_page_evidence():
    st = RouteStatus(complete=False, visited_pages=40, expected_pages=47)
    assert _route_incomplete_reason(st) == "incomplete(pages=40/47)"


def test_a_healthy_route_has_no_reason():
    st = RouteStatus(complete=True, visited_pages=5, expected_pages=5)
    assert _route_incomplete_reason(st) is None


# ─── the verification reason carries the cause ───────────────────────────────


def test_reason_string_names_the_cause_not_just_a_count():
    """This is the whole point: `aloe:6` -> `aloe:6[missing_route_statusx6]`."""
    verified, reason = _verify({}, ["a", "b", "c", "d", "e", "f"])
    assert verified is False
    assert "incomplete_routes=aloe:6" in reason
    assert "missing_route_statusx6" in reason


def test_reason_string_aggregates_mixed_causes():
    statuses = {
        "a": RouteStatus(complete=False, abort_reason="pagination_incomplete"),
        "b": RouteStatus(complete=False, abort_reason="pagination_incomplete"),
        "c": RouteStatus(complete=False, item_failures=1),
    }
    verified, reason = _verify(statuses, ["a", "b", "c"])
    assert verified is False
    assert "pagination_incompletex2" in reason
    assert "item_parse_failures(1)x1" in reason


def test_reason_stays_within_the_column_limit():
    """catalog_verification_reason is varchar(300); many causes must not overflow."""
    statuses = {
        f"route-{i}": RouteStatus(complete=False, abort_reason=f"cause_number_{i}")
        for i in range(60)
    }
    _verified, reason = _verify(statuses, list(statuses))
    assert len(reason) <= 300


def test_complete_routes_still_verify():
    statuses = {
        "a": RouteStatus(complete=True, visited_pages=2, expected_pages=2),
        "b": RouteStatus(complete=True, visited_pages=1, expected_pages=1),
    }
    verified, reason = _verify(statuses, ["a", "b"])
    assert verified is True
    assert reason == "complete_nonzero_routes_coverage_ok"


@pytest.mark.parametrize(
    "status",
    [
        RouteStatus(complete=False),
        RouteStatus(complete=True, item_failures=1),
        RouteStatus(complete=True, pages_skipped=1),
    ],
)
def test_helper_agrees_with_the_gate_on_every_failing_shape(status):
    """The helper must not report a reason the gate would pass, or vice versa."""
    verified, _reason = _verify({"a": status}, ["a"])
    assert verified is (_route_incomplete_reason(status) is None)
