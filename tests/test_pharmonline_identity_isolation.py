"""A complete source can publish its proven subset without mixing identities."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from src import main, storage
from src._time import utcnow
from src.pharmonline_identity_isolation import prepare_catalog
from tests.test_pharmonline_scheduled_admission import _api, _catalog, _catalog_result, _stored


def _run(session):
    run = storage.Run(
        tenant_id=1, status="running", catalog_scope="full", full_catalog_sites="pharmonline"
    )
    session.add(run)
    session.commit()
    return run


def _seed(session, total=300):
    # Same classes as the incident: 3 conflicts, 12 new, 1 exact native admission.
    products = [_stored(i, f"item-{i}") for i in range(total - 12)]
    products[-1].availability_source = None
    session.add_all(products)
    session.commit()
    source = [_api(i, f"item-{i}") for i in range(total)]
    source[0] = replace(source[0], url="https://pharmonline.az/product/changed-url")
    source[1] = replace(source[1], external_id="ReplacedTest00001")
    source[2] = replace(source[2], external_id="ReplacedTest00002")
    return products, [_catalog_result(*source)]


def _prepare(session, source, run):
    return prepare_catalog(session, source, tenant_id=1, run_id=run.id, source_transport="direct")


@pytest.mark.parametrize("total", [300, 9430])
def test_three_conflicts_leave_every_other_identity_publishable(db_session, total):
    original, source = _seed(db_session, total)
    before = [(p.id, p.external_id, p.url, p.name) for p in original[:3]]
    run = _run(db_session)
    trusted, partition, admission, admitted = _prepare(db_session, source, run)
    assert partition == dict(
        version=1,
        site="pharmonline",
        source_complete=True,
        total_count=total,
        trusted_count=total - 3,
        isolated_count=3,
        excluded_product_count=3,
    )
    assert admission["status"] == "admitted"
    assert len(admitted) == 13
    assert [(p.id, p.external_id, p.url, p.name) for p in original[:3]] == before
    assert all(p.identity_conflicted for p in original[:3])
    assert (
        db_session.scalar(select(func.count()).select_from(storage.PharmonlineIdentityConflict))
        == 3
    )
    assert (
        db_session.scalar(
            select(func.count()).select_from(storage.PharmonlineIdentityConflictVariant)
        )
        == 3
    )
    assert sum(len(result.products) for result in trusted) == total - 3


def test_retry_and_changed_source_pair_share_cases_without_rewriting_history(db_session):
    original, source = _seed(db_session)
    first = _run(db_session)
    _prepare(db_session, source, first)
    _prepare(db_session, source, first)  # same run replay
    second = _run(db_session)
    _prepare(db_session, source, second)
    source[0].products[0].url = "https://pharmonline.az/product/changed-again"
    _prepare(db_session, source, second)
    cases = list(db_session.scalars(select(storage.PharmonlineIdentityConflict)))
    variants = list(db_session.scalars(select(storage.PharmonlineIdentityConflictVariant)))
    assert len(cases) == 3 and len(variants) == 4
    assert {case.first_run_id for case in cases} == {first.id}
    assert {case.last_run_id for case in cases} == {second.id}
    assert original[0].url.endswith("/item-0")
    assert db_session.scalar(select(func.count()).select_from(storage.PriceSnapshot)) == 0


def test_conflicted_prices_stay_out_even_when_partial_producer_replays_variants(db_session):
    original, source = _seed(db_session)
    run = _run(db_session)
    trusted, partition, _, _ = _prepare(db_session, source, run)
    main.persist_results(db_session, run, trusted, trusted_identity_observations=True)
    run.catalog_verified = False
    run.run_quality = {
        "financially_eligible": True,
        "identity_partition": partition,
        "sites": {"pharmonline": {"status": "ok"}},
    }
    run.status = "degraded"
    run.finished_at = utcnow()
    db_session.commit()
    assert storage.run_is_financially_eligible(run)
    eligible = storage.financially_eligible_run_ids(db_session)
    assert eligible == [run.id]
    snapshots = list(
        db_session.scalars(
            select(storage.PriceSnapshot).where(storage.trusted_snapshot_filter(eligible))
        )
    )
    assert len(snapshots) == 297
    assert not {p.id for p in original[:3]} & {s.product_id for s in snapshots}

    tick = _run(db_session)
    tick.catalog_scope = "partial"
    main.persist_results(db_session, tick, _catalog(*source[0].products[:3]))
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(storage.PriceSnapshot)
            .where(storage.PriceSnapshot.run_id == tick.id)
        )
        == 0
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(storage.OfferObservation)
            .where(storage.OfferObservation.run_id == tick.id)
        )
        == 0
    )
    assert original[0].url.endswith("/item-0")


def test_partition_requires_observation_provenance_and_confirmation(db_session):
    original, source = _seed(db_session)
    run = _run(db_session)
    trusted, partition, _, _ = _prepare(db_session, source, run)
    old = _run(db_session)
    old.status, old.catalog_scope, old.finished_at = "ok", "partial", utcnow()
    # Diff-only: the trusted full source sees an unchanged price from a partial run.
    main.persist_results(db_session, old, _catalog(trusted[0].products[0]))
    main.persist_results(db_session, run, trusted, trusted_identity_observations=True)
    run.status, run.finished_at = "degraded", utcnow()
    run.run_quality = {"financially_eligible": True, "identity_partition": partition}
    db_session.commit()
    assert storage.confirm_prices_observed_by_run(db_session, run) == 1
    db_session.commit()
    assert (
        len(
            list(
                db_session.scalars(
                    select(storage.PriceSnapshot).where(storage.trusted_snapshot_filter([run.id]))
                )
            )
        )
        == 297
    )
    # A stray snapshot carrying this run_id without a proven observation is not trusted.
    outsider = _stored(10000, "not-observed")
    db_session.add(outsider)
    db_session.flush()
    stray = storage.PriceSnapshot(product_id=outsider.id, run_id=run.id, price=1)
    db_session.add(stray)
    db_session.flush()
    assert (
        db_session.scalar(
            select(storage.PriceSnapshot.id).where(
                storage.PriceSnapshot.id == stray.id, storage.trusted_snapshot_filter([run.id])
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "defect", ["cross_tenant", "mass_drift", "baseline", "audit", "incomplete"]
)
def test_global_defects_rollback_all_admissions_and_cases(db_session, defect):
    original, source = _seed(db_session)
    if defect == "cross_tenant":
        original[0].tenant_id = 2
    elif defect == "mass_drift":
        source[0].products[3].url = "https://pharmonline.az/product/fourth-conflict"
    elif defect == "baseline":
        db_session.add(
            storage.PharmonlinePublicAPICatalogBaseline(
                tenant_id=1,
                catalog_item_count=400,
                minimum_catalog_item_count=400,
                verified_identity_count=400,
                trusted_ddp_item_count=400,
                retired_ddp_item_count=0,
                reconciled_item_count=0,
                proof_version="url_continuity_v1",
                source_manifest_sha256="a" * 64,
                catalog_fingerprint_sha256="b" * 64,
                source_transport="decodo",
                preflight_run_ref="1",
            )
        )
    elif defect == "incomplete":
        next(iter(source[0].route_statuses.values())).complete = False
    else:
        original[10].identity_conflicted = True  # marker without immutable evidence
    db_session.commit()
    run = _run(db_session)
    with pytest.raises(main.PharmonlinePublicAPIIdentityError):
        _prepare(db_session, source, run)
    assert (
        db_session.scalar(select(func.count()).select_from(storage.PharmonlineIdentityConflict))
        == 0
    )
    assert (
        db_session.scalar(
            select(func.count()).select_from(storage.PharmonlinePublicAPIIdentityAdmission)
        )
        == 0
    )
    assert db_session.scalar(select(func.count()).select_from(storage.Product)) == 288


def test_failed_postprocessing_never_publishes_a_partition(db_session):
    _, source = _seed(db_session)
    run = _run(db_session)
    _, partition, _, _ = _prepare(db_session, source, run)
    run.run_quality = {"financially_eligible": True, "identity_partition": partition}
    for status in ("running", "failed"):
        run.status = status
        assert not storage.run_is_financially_eligible(run)
    run.status = "degraded"
    for key, value in (("source_complete", False), ("trusted_count", 299), ("isolated_count", 26)):
        run.run_quality = {
            "financially_eligible": True,
            "identity_partition": {**partition, key: value},
        }
        assert not storage.run_is_financially_eligible(run)
