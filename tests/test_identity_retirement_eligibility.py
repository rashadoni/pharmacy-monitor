"""Archived source identities stay historical under either policy rollout mode."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src import api, match_actions, matcher, roi, storage
from src._time import utcnow
from src.product_policy import (
    PHARMONLINE_IDENTITY_RETIREMENT_SOURCE,
    POLICY_PRODUCT_FIELDS,
    current_offer_eligibility,
    current_offer_sql,
    identity_not_retired_sql,
    policy_financial_eligibility,
    policy_offer_eligibility,
)


def _product(external_id, *, site="pharmonline", retired=False, status="unknown"):
    return storage.Product(
        tenant_id=1,
        site=site,
        external_id=external_id,
        url=f"https://{site}.az/product/{external_id}",
        name="Aspirin Bayer 100 mg",
        name_normalized="aspirin bayer 100 mg",
        brand="Bayer",
        manufacturer_country_code="de",
        country_resolution_status="resolved",
        offer_availability_status=status,
        availability_observed_at=utcnow(),
        availability_source=PHARMONLINE_IDENTITY_RETIREMENT_SOURCE if retired else None,
    )


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@pytest.mark.parametrize("status", ["unknown", "in_stock", "out_of_stock"])
def test_retired_identity_cannot_be_an_offer_or_matching_candidate(monkeypatch, mode, status):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", mode)
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", mode)
    archived = _product("archived", retired=True, status=status)
    archived.id = 42
    archived.canonical_id = 7

    for eligibility in (
        current_offer_eligibility(archived),
        policy_offer_eligibility(archived),
        policy_financial_eligibility([archived]),
    ):
        assert not eligibility.eligible
        assert eligibility.reason == "identity_retired"
    assert matcher._unmatchable_reason(archived) == "identity_retired"
    with pytest.raises(HTTPException, match="archived source identity") as error:
        api._require_match_policy([archived])
    assert error.value.status_code == 409
    assert archived.canonical_id == 7  # Eligibility must not rewrite historic membership.


def test_retirement_marker_does_not_change_other_sites_or_unknown_shadow_offers(monkeypatch):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "shadow")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    for product in (
        _product("unobserved"),
        _product("other-site", site="aloe", retired=True),
    ):
        assert policy_offer_eligibility(product).eligible
        assert policy_financial_eligibility([product]).eligible
        assert matcher._unmatchable_reason(product) is None
        api._require_match_policy([product])


def test_sql_policy_and_projected_rows_exclude_retired_even_if_in_stock(db_session):
    archived = _product("archived", retired=True, status="in_stock")
    current = _product("current", status="in_stock")
    other = _product("other", site="aloe", retired=True, status="in_stock")
    unknown = _product("unknown")
    db_session.add_all([archived, current, other, unknown])
    db_session.flush()

    assert set(
        db_session.scalars(
            select(storage.Product.id).where(identity_not_retired_sql(storage.Product))
        )
    ) == {current.id, other.id, unknown.id}
    assert set(
        db_session.scalars(select(storage.Product.id).where(current_offer_sql(storage.Product)))
    ) == {current.id, other.id}
    projection = db_session.execute(
        select(*(getattr(storage.Product, field) for field in POLICY_PRODUCT_FIELDS)).where(
            storage.Product.id == archived.id
        )
    ).one()
    assert policy_offer_eligibility(projection).reason == "identity_retired"


def test_manual_alternatives_and_api_candidates_do_not_offer_archived_rows(db_session, monkeypatch):
    monkeypatch.setenv("COUNTRY_IDENTITY_POLICY", "shadow")
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    match = storage.Match(tenant_id=1, canonical_name="Aspirin Bayer 100 mg")
    db_session.add(match)
    db_session.flush()
    anchor = _product("anchor", site="aloe")
    anchor.canonical_id = match.id
    current = _product("current")
    archived = _product("archived", retired=True)
    db_session.add_all([anchor, current, archived])
    db_session.flush()

    alternatives = match_actions.find_alternatives(db_session, match.id, "pharmonline")
    assert [product.id for product, _score in alternatives] == [current.id]
    response = api.dash_match_candidate_analogs(
        match.id, "pharmonline", user=SimpleNamespace(tenant_id=1), db=db_session
    )
    assert [item["product_id"] for item in response["items"]] == [current.id]
    assert archived.canonical_id is None


def test_retired_competitor_history_cannot_lower_brand_price_floor(db_session, monkeypatch):
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    current = _product("current")
    archived = _product("archived", retired=True)
    client = _product("client", site="aptekonline")
    run = storage.Run(status="ok")
    db_session.add_all([current, archived, client, run])
    db_session.flush()
    monkeypatch.setattr(storage, "financially_eligible_run_ids", lambda *_a, **_kw: [run.id])
    for product, price in [(current, 10.0)] * 3 + [(archived, 5.0), (client, 7.0)]:
        db_session.add(storage.PriceSnapshot(run_id=run.id, product_id=product.id, price=price))
    db_session.flush()

    actions = roi._map_violations(
        db_session, 10, tenant_id=1, client_site="aptekonline", competitor_sites=("pharmonline",)
    )
    assert len(actions) == 1
    assert actions[0].target_value_azn == 9.9
    assert db_session.query(storage.PriceSnapshot).filter_by(product_id=archived.id).count() == 1


def test_retired_client_history_does_not_produce_a_price_raise(db_session, monkeypatch):
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    competitor = _product("competitor", site="aptekonline")
    archived = _product("archived", retired=True)
    run = storage.Run(status="ok")
    db_session.add_all([competitor, archived, run])
    db_session.flush()
    monkeypatch.setattr(storage, "financially_eligible_run_ids", lambda *_a, **_kw: [run.id])
    for product, price in [(competitor, 10.0)] * 3 + [(archived, 7.0)]:
        db_session.add(storage.PriceSnapshot(run_id=run.id, product_id=product.id, price=price))
    db_session.flush()

    assert (
        roi._map_violations(
            db_session,
            10,
            tenant_id=1,
            client_site="pharmonline",
            competitor_sites=("aptekonline",),
        )
        == []
    )
