"""A complete source with isolated identities serves only its trusted subset."""

import ast
import io
import json
from contextlib import nullcontext
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest
import pandas as pd
from fastapi import HTTPException
from openpyxl import load_workbook
from sqlalchemy import select, text

from src import api, comparison_export, comparison_rows, health, inventory
from src import match_actions, matcher, notifications, product_policy, roi, storage
from src._time import utcnow


def _product(external_id, *, site="pharmonline", name="Aspirin Bayer 100 mg", match=None):
    return storage.Product(
        tenant_id=1,
        site=site,
        external_id=external_id,
        url=f"https://{site}.example/{external_id}",
        name=name,
        name_normalized=name.lower(),
        brand="Bayer",
        canonical_id=match,
        manufacturer_country_code="de",
        country_resolution_status="resolved",
        offer_availability_status="in_stock",
        availability_observed_at=utcnow(),
        last_seen_at=utcnow(),
    )


def _legacy_run(session):
    now = utcnow() - timedelta(hours=1)
    run = storage.Run(
        tenant_id=1,
        status="ok",
        started_at=now,
        finished_at=now,
        catalog_scope="full",
        full_catalog_sites="pharmonline,aptekonline,aloe",
        catalog_verified=True,
        products_scraped=102,
        products_per_site={"pharmonline": 100, "aptekonline": 1, "aloe": 1},
        run_quality={
            "financially_eligible": True,
            "full_catalog_verified": True,
            "sites": {site: {"status": "ok"} for site in storage.FULL_CATALOG_SITES},
        },
    )
    session.add(run)
    session.flush()
    return run


@pytest.fixture
def partitioned(db_session):
    s = db_session
    legacy = _legacy_run(s)
    match = storage.Match(tenant_id=1, canonical_name="Aspirin Bayer 100 mg", confidence=1.0)
    s.add(match)
    s.flush()
    good = _product("good", match=match.id)
    blocked = _product("conflict", name="Isolated identity")
    blocked.identity_conflicted = True
    competitor = _product("competitor", site="aptekonline", match=match.id)
    aloe = _product("aloe", site="aloe", match=match.id)
    fillers = [_product(f"filler-{index}", name=f"Padding {index}") for index in range(98)]
    s.add_all([good, blocked, competitor, aloe, *fillers])
    s.flush()
    now = utcnow()
    run = storage.Run(
        tenant_id=1,
        status="degraded",
        started_at=now,
        finished_at=now,
        catalog_scope="full",
        full_catalog_sites="pharmonline",
        catalog_verified=False,
        products_scraped=99,
        products_per_site={"pharmonline": 99},
        run_quality={
            "financially_eligible": True,
            "full_catalog_verified": False,
            "sites": {"pharmonline": {"status": "ok", "products": 100}},
            "identity_partition": {
                "version": 1,
                "site": "pharmonline",
                "source_complete": True,
                "total_count": 100,
                "trusted_count": 99,
                "isolated_count": 1,
                "excluded_product_count": 1,
            },
        },
    )
    s.add(run)
    s.flush()
    for product in [good, *fillers]:
        s.add(
            storage.OfferObservation(
                tenant_id=1,
                run_id=run.id,
                product_id=product.id,
                country_code="de",
                country_resolution_status="resolved",
                availability_status="in_stock",
                identity_verified=True,
            )
        )
    for product in [competitor, aloe]:
        s.add(
            storage.OfferObservation(
                tenant_id=1,
                run_id=legacy.id,
                product_id=product.id,
                country_code="de",
                country_resolution_status="resolved",
                availability_status="in_stock",
            )
        )
    for product, price, source_run in [
        (good, 8, run),
        (blocked, 1, legacy),
        (competitor, 10, legacy),
        (aloe, 9, legacy),
    ]:
        s.add(storage.PriceSnapshot(run_id=source_run.id, product_id=product.id, price=price))
    s.commit()
    return SimpleNamespace(
        session=s,
        run=run,
        legacy=legacy,
        good=good,
        blocked=blocked,
        competitor=competitor,
        aloe=aloe,
        match=match,
    )


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_isolation_is_a_hard_gate_for_orm_projections_sql_and_manual_actions(
    db_session, monkeypatch, mode
):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", mode)
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", mode)
    blocked = _product("blocked")
    blocked.identity_conflicted = True
    good = _product("good")
    db_session.add_all([blocked, good])
    db_session.flush()
    projection = db_session.execute(
        select(
            *(getattr(storage.Product, field) for field in product_policy.POLICY_PRODUCT_FIELDS)
        ).where(storage.Product.id == blocked.id)
    ).one()
    for product in [blocked, projection]:
        assert product_policy.policy_offer_eligibility(product).reason == "identity_conflicted"
        assert (
            product_policy.policy_financial_eligibility([product]).reason == "identity_conflicted"
        )
    assert matcher._unmatchable_reason(blocked) == "identity_conflicted"
    assert list(
        db_session.scalars(
            select(storage.Product.id).where(
                product_policy.source_identity_eligible_sql(storage.Product)
            )
        )
    ) == [good.id]
    with pytest.raises(HTTPException, match="conflicting source identity"):
        api._require_match_policy([blocked, good])
    assert blocked.canonical_id is None


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_partition_keeps_comparison_export_and_daily_letter_on_trusted_products(
    partitioned, monkeypatch, mode
):
    c = partitioned
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", mode)
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", mode)
    rows = comparison_rows.comparison_rows(c.session, tenant_id=1)
    assert len(rows) == 1
    assert rows[0]["prices"]["pharmonline"]["product_id"] == c.good.id
    assert rows[0]["prices"]["pharmonline"]["price"] == 8
    assert api.comparisons(db=c.session)[0]["prices"]["pharmonline"]["price"] == 8
    search = api.dash_comparison_search("Isolated", user=SimpleNamespace(tenant_id=1), db=c.session)
    assert json.loads(search.body)["others"] == []
    report, _ = notifications._price_report(c.session, 1, locale="en")
    assert report is not None
    for data in (
        comparison_export.build_workbook(rows, locale="en", generated_at=utcnow()),
        report.attachment[1],
    ):
        workbook = load_workbook(io.BytesIO(data))
        cells = [value for sheet in workbook for row in sheet.values for value in row]
        assert "Isolated identity" not in cells
        assert 8 in cells
    assert (
        c.session.scalar(
            select(storage.PriceSnapshot.price).where(
                storage.PriceSnapshot.product_id == c.blocked.id
            )
        )
        == 1
    )  # exclusion is read-only; history is retained


def test_isolated_third_member_does_not_close_a_valid_pair(partitioned, monkeypatch):
    c = partitioned
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "enforce")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "enforce")
    c.good.canonical_id = None
    c.blocked.canonical_id = c.match.id
    c.blocked.manufacturer_country_code = "fr"
    c.session.commit()
    rows = comparison_rows.comparison_rows(c.session, tenant_id=1)
    assert len(rows) == 1
    assert set(rows[0]["prices"]) == {"aptekonline", "aloe"}
    assert c.blocked.canonical_id == c.match.id


def test_partition_health_is_truthful_and_observation_coverage_cannot_be_faked(
    partitioned, monkeypatch
):
    c = partitioned
    monkeypatch.setattr(api, "_ping_redis", lambda: 1.0)
    report = product_policy.full_catalog_trust_report(c.session)
    pharm = next(row for row in report["sites"] if row["site"] == "pharmonline")
    assert pharm["ready"]
    assert pharm["latest_full_attempt_status"] == "degraded"
    assert pharm["latest_full_attempt_verified"] is False
    assert (pharm["source_products"], pharm["trusted_products"], pharm["isolated_products"]) == (
        100,
        99,
        1,
    )
    snapshot = api._health_snapshot(c.session)
    assert snapshot.status == "degraded"
    assert snapshot.full_catalog_verified is False
    assert snapshot.full_catalog_source_complete is True
    assert snapshot.isolated_identity_count == 1
    issues = health.check_health(c.session).issues
    isolated = [issue for issue in issues if issue.code == "catalog_identity_isolated"]
    assert len(isolated) == 1 and isolated[0].severity == "warning"
    assert not {issue.code for issue in issues} & {
        "full_catalog_overdue",
        "full_catalog_unverified",
    }
    observation = c.session.scalar(
        select(storage.OfferObservation).where(
            storage.OfferObservation.run_id == c.run.id,
            storage.OfferObservation.product_id == c.good.id,
        )
    )
    observation.identity_verified = False
    c.session.commit()
    assert product_policy.full_catalog_trust_report(c.session)["sites"][0]["ready"] is False


def test_cache_invalidates_when_identity_becomes_conflicted_inside_same_run_epoch(partitioned):
    c = partitioned
    action = roi.ActionItem(
        type="price_raise", severity="info", title="Safe", detail="Safe", product_url=c.good.url
    )
    roi.cache_actions(c.session, "pharmonline", [action], run_id=c.run.id)
    assert roi.get_cached_actions(c.session, "pharmonline") is not None
    c.good.identity_conflicted = True
    c.session.commit()
    assert roi.get_cached_actions(c.session, "pharmonline") is None


def test_later_global_failure_closes_partition_epoch(partitioned):
    c = partitioned
    failure = storage.Run(
        tenant_id=1,
        status="failed",
        started_at=utcnow(),
        finished_at=utcnow(),
        catalog_scope="full",
        full_catalog_sites="pharmonline",
        catalog_verified=False,
    )
    c.session.add(failure)
    c.session.commit()
    assert product_policy.trusted_catalog_epoch(c.session) is None
    assert not roi.financial_inputs_are_fresh(c.session, tenant_id=1)


@pytest.mark.parametrize("key", ["product_id", "match_id"])
def test_queued_digest_and_realtime_events_cannot_revive_isolated_identity(
    partitioned, monkeypatch, key
):
    c = partitioned
    c.blocked.canonical_id = c.match.id
    c.session.commit()
    payload = {
        "source_run_id": c.legacy.id,
        key: c.blocked.id if key == "product_id" else c.match.id,
    }
    event = storage.AlertEvent(
        tenant_id=1,
        rule_type="price_drop_pct" if key == "product_id" else "undercut_threshold",
        severity="warning",
        title="Old event",
        detail="Old price",
        dedup_key=f"isolated-{key}",
        payload=payload,
    )
    c.session.add(event)
    c.session.commit()
    assert not notifications._event_is_digest_eligible(c.session, event)
    assert notifications.dispatch_event(c.session, event) == {"skipped": "identity_not_eligible"}
    assert notifications.dispatch_events_batch(c.session, [event]) == {"email": 0, "telegram": 0}
    assert not event.channels_sent


def test_margin_uses_partition_and_never_old_conflicted_price(partitioned):
    c = partitioned
    for product in (c.good, c.blocked):
        c.session.add(
            storage.SupplierPrice(
                product_id=product.id,
                supplier_name="Supplier",
                purchase_price=0.5,
                name=product.name,
            )
        )
    c.session.commit()
    assert [(row.product_id, row.sale_price) for row in inventory.margin_report(c.session)] == [
        (c.good.id, 8)
    ]
    assert inventory.margin_report(c.session, run_id=c.legacy.id) == []


def test_margin_includes_confirmed_unchanged_price_without_reading_later_epochs(partitioned):
    c = partitioned
    partial = storage.Run(status="ok", catalog_scope="partial", finished_at=utcnow())
    c.session.add(partial)
    c.session.flush()
    unchanged = c.session.scalar(
        select(storage.PriceSnapshot).where(storage.PriceSnapshot.product_id == c.good.id)
    )
    unchanged.run_id = partial.id
    unchanged.confirmed_run_id = c.run.id
    c.session.add(
        storage.SupplierPrice(
            product_id=c.good.id,
            supplier_name="Supplier",
            purchase_price=5,
            name=c.good.name,
        )
    )
    c.session.commit()
    assert inventory.margin_report(c.session)[0].sale_price == 8
    later = _legacy_run(c.session)
    later.started_at = later.finished_at = utcnow()
    c.session.add(storage.PriceSnapshot(run_id=later.id, product_id=c.good.id, price=20))
    c.session.commit()
    assert inventory.margin_report(c.session)[0].sale_price == 20
    assert inventory.margin_report(c.session, run_id=c.run.id)[0].sale_price == 8
    later.status = "failed"
    c.session.commit()
    assert inventory.margin_report(c.session) == []


def test_manual_candidate_lists_exclude_conflicted_identity(partitioned):
    c = partitioned
    c.blocked.name = c.good.name
    c.blocked.name_normalized = c.good.name_normalized
    c.session.commit()
    assert c.blocked.id not in [
        p.id for p, _ in match_actions.find_alternatives(c.session, c.match.id, "pharmonline")
    ]
    results = api.dash_match_candidate_analogs(
        c.match.id, "pharmonline", user=SimpleNamespace(tenant_id=1), db=c.session
    )
    assert c.blocked.id not in [row["product_id"] for row in results["items"]]


def test_current_price_helpers_and_manual_ui_never_present_conflicted_history_as_current(
    partitioned,
):
    c = partitioned
    c.blocked.canonical_id = c.match.id
    c.match.confidence = 0.8
    c.session.commit()
    assert storage.latest_snapshots_per_product(c.session, [c.blocked.id]) == {}
    assert storage.latest_prices_per_product(c.session, [c.blocked.id]) == {}
    user = SimpleNamespace(tenant_id=1)
    suggestions = api.dash_match_suggestions(user=user, db=c.session)
    blocked = next(p for row in suggestions for p in row.products if p.product_id == c.blocked.id)
    assert blocked.price is None
    products = api.dash_products("pharmonline", search="Isolated", user=user, db=c.session)
    assert products["items"][0]["price"] is None
    assert (
        c.session.scalar(
            select(storage.PriceSnapshot.price).where(
                storage.PriceSnapshot.product_id == c.blocked.id
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "function", ["load_latest_snapshot", "load_price_history", "load_comparison"]
)
def test_legacy_dashboard_sql_does_not_read_conflicted_prices(partitioned, function):
    # Execute the actual legacy SQL without importing its top-level Streamlit UI.
    c = partitioned
    c.blocked.canonical_id = c.match.id
    c.session.commit()
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src/dashboard.py").read_text())
    node = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function
    )
    query = next(
        node.value.value
        for node in node.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
    )
    rows = c.session.execute(text(query)).mappings().all()
    if function == "load_comparison":
        assert all(row["ph_price"] is None for row in rows)
    else:
        assert c.blocked.id not in {row["product_id"] for row in rows}


@pytest.mark.parametrize("function", ["load_latest_snapshot", "load_comparison"])
@pytest.mark.parametrize("transition", ["pending", "partition", "failed", "running", "stale"])
def test_legacy_current_views_recheck_trust_after_a_previous_read(
    partitioned, function, transition
):
    c = partitioned
    c.blocked.identity_conflicted = False
    c.blocked.canonical_id = c.match.id
    c.run.catalog_scope = "partial"
    c.session.commit()
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src/dashboard.py").read_text())
    functions = {"_legacy_current_prices_blocked", "_blocked_legacy_prices", function}
    nodes = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in functions
    ]
    namespace = {
        "pd": pd,
        "storage": storage,
        "utcnow": utcnow,
        "SITES": storage.FULL_CATALOG_SITES,
        "get_session": lambda: nullcontext(c.session),
        "get_engine": lambda: c.session.connection(),
        # Execute actual decorators too: reintroducing a cached public reader
        # would return the first frame and fail the subsequent trust assertion.
        "st": SimpleNamespace(cache_data=lambda **kwargs: lru_cache(maxsize=None)),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "legacy_readers", "exec"), namespace)
    load = namespace[function]
    assert not load().empty

    if transition == "pending":
        c.blocked.identity_conflicted = True
    elif transition == "stale":
        c.legacy.started_at = c.legacy.finished_at = utcnow() - timedelta(days=30)
    else:
        c.run.catalog_scope = "full"
        if transition != "partition":
            c.run.status = transition
    c.session.commit()
    blocked = load()
    assert blocked.empty
    assert blocked.attrs["current_prices_blocked"] is True
