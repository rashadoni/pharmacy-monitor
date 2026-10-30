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


def _cluster_with_one_offer_per_site(db_session, client, competitor, prices):
    match = storage.Match(tenant_id=1, canonical_name="Aspirin Bayer 100 mg")
    db_session.add(match)
    db_session.flush()
    client.canonical_id = competitor.canonical_id = match.id
    run = storage.Run(
        status="ok",
        catalog_scope="full",
        catalog_verified=True,
        finished_at=utcnow(),
        run_quality={"financially_eligible": True},
    )
    db_session.add_all([client, competitor, run])
    db_session.flush()
    for product, price in zip((client, competitor), prices):
        db_session.add(storage.PriceSnapshot(run_id=run.id, product_id=product.id, price=price))
    db_session.flush()


@pytest.mark.parametrize("retired", [True, False])
def test_retired_competitor_history_does_not_produce_an_undercut(db_session, monkeypatch, retired):
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    client = _product("client", site="aptekonline")
    competitor = _product("competitor", retired=retired)
    _cluster_with_one_offer_per_site(db_session, client, competitor, (7.0, 5.0))

    actions = roi._undercut_threats(
        db_session,
        3.0,
        90.0,
        10,
        min_margin_pct=10.0,
        tenant_id=1,
        client_site="aptekonline",
        competitor_sites=("pharmonline",),
    )
    assert len(actions) == (0 if retired else 1)
    assert db_session.query(storage.PriceSnapshot).filter_by(product_id=competitor.id).count() == 1


@pytest.mark.parametrize("retired", [True, False])
def test_retired_client_history_does_not_produce_a_price_raise(db_session, monkeypatch, retired):
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    client = _product("client", retired=retired)
    competitor = _product("competitor", site="aptekonline")
    _cluster_with_one_offer_per_site(db_session, client, competitor, (7.0, 10.0))

    actions = roi._price_raise_opportunities(
        db_session,
        5.0,
        80.0,
        10,
        tenant_id=1,
        client_site="pharmonline",
        competitor_sites=("aptekonline",),
    )
    assert len(actions) == (0 if retired else 1)


def test_retired_cheaper_row_beside_current_row_does_not_lower_competitor_price(
    db_session, monkeypatch
):
    monkeypatch.setenv("OFFER_AVAILABILITY_POLICY", "shadow")
    client = _product("client", site="aptekonline")
    current = _product("current")
    archived = _product("archived", retired=True)
    _cluster_with_one_offer_per_site(db_session, client, current, (7.0, 10.0))
    archived.canonical_id = client.canonical_id
    db_session.add(archived)
    db_session.flush()
    run_id = db_session.query(storage.Run.id).scalar()
    db_session.add(storage.PriceSnapshot(run_id=run_id, product_id=archived.id, price=5.0))
    db_session.flush()

    kwargs = dict(tenant_id=1, client_site="aptekonline", competitor_sites=("pharmonline",))
    assert roi._undercut_threats(db_session, 3.0, 90.0, 10, min_margin_pct=10.0, **kwargs) == []
    raises = roi._price_raise_opportunities(db_session, 5.0, 80.0, 10, **kwargs)
    # Цель от одной живой цены 10,0 (минус 2%): архивная 5,0 её не опускает.
    assert [a.target_value_azn for a in raises] == [9.8]
