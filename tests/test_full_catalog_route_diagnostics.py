"""Full-catalog verification must say WHY a route is incomplete, not just count.

Production run 470 (aloe, 2026-07-15) recorded only `incomplete_routes=aloe:6`.
Six routes failed and the cause existed solely in memory, so the failure was
undiagnosable after the fact — and the causes need opposite fixes
(`pagination_incomplete` = scraper under-fetched; `item_parse_failures` on a
99.9%-complete run = this check is too strict).
"""

import pytest

from src.main import (
    _route_incomplete_causes,
    _route_incomplete_reason,
    _verify_full_catalog_results,
)
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


def test_item_parse_failures_are_named():
    st = RouteStatus(complete=False, item_failures=3)
    assert "item_parse_failures" in _route_incomplete_causes(st)


def test_pages_skipped_is_named():
    st = RouteStatus(complete=True, pages_skipped=2)
    assert _route_incomplete_causes(st) == ["pages_skipped"]


def test_item_count_mismatch_is_named():
    st = RouteStatus(complete=True, raw_items=9, parsed_items=9, expected_items=10)
    assert _route_incomplete_causes(st) == ["item_count_mismatch"]


def test_scraper_abort_reason_is_surfaced_first():
    st = RouteStatus(complete=False, abort_reason="pagination_incomplete")
    assert _route_incomplete_causes(st)[0] == "pagination_incomplete"


def test_incomplete_without_abort_reason_falls_back_to_page_evidence():
    st = RouteStatus(complete=False, visited_pages=40, expected_pages=47)
    assert _route_incomplete_reason(st) == "incomplete(pages=40/47)"
    assert _route_incomplete_causes(st) == ["incomplete"]


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
    assert "item_parse_failuresx1" in reason


def test_many_causes_stay_within_the_column_and_keep_the_count():
    """Capped by cause KIND, so the route COUNT survives even when kinds are cut."""
    statuses = {
        f"route-{i}": RouteStatus(complete=False, abort_reason=f"cause_number_{i}")
        for i in range(60)
    }
    _verified, reason = _verify(statuses, list(statuses))
    assert len(reason) <= 300
    assert "aloe:60" in reason
    assert "more" in reason  # explicitly says causes were elided


def test_complete_routes_still_verify():
    statuses = {
        "a": RouteStatus(complete=True, visited_pages=2, expected_pages=2),
        "b": RouteStatus(complete=True, visited_pages=1, expected_pages=1),
    }
    verified, reason = _verify(statuses, ["a", "b"])
    assert verified is True
    assert reason == "complete_nonzero_routes_coverage_ok"


def _oracle_route_is_incomplete(st) -> bool:
    """Frozen copy of the five conditions as they stood before this refactor.

    Independent of the implementation on purpose: asserting the gate against the
    helper it now calls is a tautology that passes even if the helper is replaced
    by `lambda _: None`, which would disable full-catalog verification entirely.
    """
    return (
        st is None
        or not st.complete
        or st.pages_skipped > 0
        or st.item_failures > 0
        or (
            st.expected_items is not None
            and (st.raw_items != st.expected_items or st.parsed_items != st.expected_items)
        )
    )


@pytest.mark.parametrize(
    "status",
    [
        None,
        RouteStatus(complete=True),
        RouteStatus(complete=True, visited_pages=3, expected_pages=3),
        RouteStatus(complete=False),
        RouteStatus(complete=False, abort_reason="pagination_incomplete"),
        RouteStatus(complete=True, item_failures=1),
        RouteStatus(complete=True, pages_skipped=1),
        RouteStatus(complete=True, raw_items=9, parsed_items=9, expected_items=10),
        RouteStatus(complete=True, raw_items=10, parsed_items=10, expected_items=10),
        # aloe's real shape: the same card-failure count in BOTH fields.
        RouteStatus(
            complete=False,
            abort_reason="card_parse_failures",
            pages_skipped=9,
            item_failures=9,
            raw_items=6411,
            parsed_items=6402,
        ),
    ],
)
def test_gate_verdict_matches_the_pre_refactor_oracle(status):
    """The refactor must not change WHAT fails, only what it says about it."""
    verified, _reason = _verify({"a": status} if status is not None else {}, ["a"])
    assert verified is not _oracle_route_is_incomplete(status)
    # And the reason must be present exactly when the route fails.
    assert (_route_incomplete_reason(status) is not None) is _oracle_route_is_incomplete(status)


def test_card_parse_failures_are_not_reported_as_a_pagination_gap():
    """aloe sets pages_skipped=item_failures=<card failures> (aloe.py:616,627).

    Reading pages_skipped first called a parse failure an under-fetch — the
    opposite diagnosis, on the exact run this exists to explain.
    """
    st = RouteStatus(
        complete=False,
        abort_reason="card_parse_failures",
        pages_skipped=9,
        item_failures=9,
        raw_items=6411,
        parsed_items=6402,
    )
    causes = _route_incomplete_causes(st)
    assert causes[0] == "card_parse_failures"
    assert "item_parse_failures" in causes
    reason = _route_incomplete_reason(st)
    assert reason.startswith("card_parse_failures")


def test_all_firing_causes_are_reported_not_just_the_first():
    st = RouteStatus(complete=False, abort_reason="x", pages_skipped=1, item_failures=2)
    assert set(_route_incomplete_causes(st)) >= {"x", "item_parse_failures", "pages_skipped"}


def test_a_healthy_route_reports_no_causes():
    assert _route_incomplete_causes(RouteStatus(complete=True)) == []


def test_abort_reason_on_a_complete_route_does_not_invent_a_failure():
    """abort_reason is explanatory; only the gate's conditions decide."""
    st = RouteStatus(complete=True, abort_reason="leftover note")
    assert _route_incomplete_causes(st) == []
    assert _verify({"a": st}, ["a"])[0] is True


# ─── truncation must not erase a whole site ──────────────────────────────────


def test_a_second_site_survives_a_verbose_first_site():
    """`reason[:300]` used to cut trailing sites off entirely."""
    noisy = {
        f"r{i}": RouteStatus(complete=False, abort_reason=f"very_long_cause_name_number_{i}")
        for i in range(40)
    }
    quiet = {"q": RouteStatus(complete=False, abort_reason="pagination_incomplete")}
    results = [
        _result(site="aloe", route_statuses=noisy),
        _result(site="aptekonline", route_statuses=quiet),
    ]
    verified, reason = _verify_full_catalog_results(
        results,
        sites=["aloe", "aptekonline"],
        expected_slugs={"aloe": list(noisy), "aptekonline": list(quiet)},
        baselines={"aloe": None, "aptekonline": None},
    )
    assert verified is False
    assert len(reason) <= 300
    assert "aloe:40" in reason
    assert "aptekonline:1" in reason, f"second site lost: {reason}"


def test_a_pathological_abort_reason_cannot_eat_the_column():
    """base.py:887 puts up to 200 chars of raw exception text into abort_reason."""
    st = RouteStatus(complete=False, abort_reason="TimeoutError: " + "x" * 400)
    _verified, reason = _verify({"a": st}, ["a"])
    assert len(reason) <= 300
    assert "incomplete_routes=aloe:1" in reason
