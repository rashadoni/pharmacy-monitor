"""Reviewed closure of pending identity isolation cases (production shape of 2026-10-16).

Four source pairs were isolated on the first partitioned run and stayed that
way every night: two cards got a new native ID at the same address, one kept
its ID at a new address, and one unchanged card shared its address with a
historic row nobody had observed since June. Five Products, five cases. These
tests pin that the manual Decodo reconciliation closes exactly those cases,
keeps every historical row where it was, and that every later scrape trusts
the decisions only while they still describe the case state.
"""

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from src import main as m
from src import pharmonline_identity_resolution as resolution
from src import product_policy, storage
from src.pharmonline_identity_isolation import prepare_catalog
from src.scrapers.pharmonline_public_api import PUBLIC_API_AVAILABILITY_SOURCE
from tests.test_pharmonline_scheduled_admission import (
    MANUAL,
    _api,
    _catalog_result,
    _meteor_id,
    _stored,
)

FILLER = range(10, 430)  # 420 trusted cards: four isolated pairs stay under 1%
KALSIMIKS = "Kalsimiks № 30 ( Kapsulalar)"
FRUTO = "Fruto Nyanya uşaq  peçenyesi buğdalı 6+ (2889) 120 q"
RAXME = "Raxme omeqa 3  150 ml"
AZITROMISIN = "Azitromisin Skan 500 mq № 3 (Tabletlər)"


def _run(session):
    run = storage.Run(
        tenant_id=1, status="running", catalog_scope="full", full_catalog_sites="pharmonline"
    )
    session.add(run)
    session.commit()
    return run


def _admitted(session, run, index, slug, name):
    product = _stored(index, slug, availability_source=PUBLIC_API_AVAILABILITY_SOURCE, name=name)
    session.add(product)
    session.flush()
    session.add(
        storage.PharmonlinePublicAPIIdentityAdmission(
            tenant_id=1,
            product_id=product.id,
            admission_kind="new_public_product",
            public_api_external_id=product.external_id,
            public_api_canonical_url=product.url,
            proof_version=MANUAL,
            source_manifest_sha256="a" * 64,
            catalog_fingerprint_sha256="b" * 64,
            source_transport="decodo",
            preflight_run_ref="37229945813",
        )
    )
    session.add(storage.PriceSnapshot(run_id=run.id, product_id=product.id, price=27.0))
    session.add(
        storage.OfferObservation(
            tenant_id=1,
            run_id=run.id,
            product_id=product.id,
            availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
            identity_verified=True,
        )
    )
    return product


def _seed(session):
    history = storage.Run(tenant_id=1, status="ok")
    session.add(history)
    session.flush()
    session.add_all([_stored(i, f"item-{i}") for i in FILLER])
    cards = {
        "kalsimiks": _admitted(session, history, 0, "kalsimiks-turkiye", KALSIMIKS),
        "fruto": _admitted(session, history, 1, "fruto", FRUTO),
        "raxme": _admitted(session, history, 2, "raxme", RAXME),
    }
    cards["current"] = _stored(3, "azitromisin", name=AZITROMISIN)
    cards["shadow"] = _stored(
        4, "azitromisin", name=AZITROMISIN + " (Azərbaycan)", availability_source=None
    )
    session.add_all([cards["current"], cards["shadow"]])
    session.flush()
    session.add(storage.PriceSnapshot(run_id=history.id, product_id=cards["shadow"].id, price=5.93))
    session.add(
        storage.PriceSnapshot(run_id=history.id, product_id=cards["current"].id, price=5.93)
    )
    session.add(
        storage.OfferObservation(
            tenant_id=1,
            run_id=history.id,
            product_id=cards["current"].id,
            availability_source=m._PHARMONLINE_DDP_AVAILABILITY_SOURCE,
        )
    )
    session.commit()
    return cards


def _source(**overrides):
    products = [_api(i, f"item-{i}") for i in FILLER]
    products += [
        _api(0, "kalsimiks-italiya", name=KALSIMIKS),
        replace(_api(1, "fruto", name=FRUTO), external_id=_meteor_id(5001)),
        replace(_api(2, "raxme", name=RAXME), external_id=_meteor_id(5002)),
        _api(3, "azitromisin", name=overrides.pop("azitromisin_name", AZITROMISIN)),
    ]
    products += overrides.pop("extra", [])
    assert not overrides
    return [_catalog_result(*products)]


def _isolate(session, source):
    run = _run(session)
    return prepare_catalog(session, source, tenant_id=1, run_id=run.id, source_transport="direct")


def _plan(session, source, **kwargs):
    kwargs.setdefault("allow_identity_retirement", True)
    return m._pharmonline_public_api_reconciliation_plan(session, source, tenant_id=1, **kwargs)


def _manifest(session, source):
    return m._pharmonline_public_api_reconciliation_plan_manifest_sha256(*_plan(session, source))


def _apply(session, source, **overrides):
    args = dict(
        tenant_id=1,
        source_manifest_sha256="c" * 64,
        catalog_fingerprint_sha256=m._pharmonline_public_api_catalog_fingerprint(source),
        source_transport="decodo",
        preflight_run_ref="38463791648",
        allow_identity_retirement=True,
        admission_proof_version=MANUAL,
        expected_plan_manifest_sha256=_manifest(session, source),
    )
    args.update(overrides)
    metrics = m._apply_pharmonline_public_api_reconciliation(session, source, **args)
    # The reconciliation script verifies the whole map before it commits.
    m._verify_pharmonline_public_api_identities(session, source, tenant_id=1)
    session.commit()
    return metrics


def _rows(session, model):
    session.expire_all()
    return [
        {c.name: getattr(row, c.name) for c in model.__table__.columns}
        for row in session.scalars(select(model).order_by(model.id))
    ]


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


@pytest.fixture
def isolated(db_session):
    cards = _seed(db_session)
    source = _source()
    _, partition, _, _ = _isolate(db_session, source)
    assert partition["isolated_count"] == 4 and partition["excluded_product_count"] == 5
    assert all(cards[key].identity_conflicted for key in cards)
    return cards, source


def test_reviewed_plan_names_one_decision_for_each_production_case(db_session, isolated):
    cards, source = isolated
    _, _, quarantines, metrics = _plan(db_session, source)
    assert metrics["identity_conflict_cases"] == 5
    assert metrics["identity_conflict_retirements_ready"] == 3
    assert metrics["identity_conflict_shadows_ready"] == 1
    assert metrics["identity_conflict_unresolved"] == metrics["identity_conflict_pending"] == 0
    assert m._pharmonline_public_api_reconciliation_is_safe(metrics)
    kinds = {action.legacy_product_id: action.retirement_kind for action in quarantines}
    assert kinds == {
        cards["kalsimiks"].id: "native_url_changed",
        cards["fruto"].id: "native_id_replaced",
        cards["raxme"].id: "native_id_replaced",
        cards["shadow"].id: resolution.SHADOW_KIND,
    }
    assert all(action.conflict_evidence_sha256 for action in quarantines)
    # The aggregate evidence the plan workflow uploads and the recover
    # workflow requires: safe, schema ready, nothing unclassified.
    diagnostics = m._diagnose_pharmonline_public_api_reconciliation(
        db_session, source, tenant_id=1, allow_identity_retirement=True
    )
    assert diagnostics["reconciliation_safe"] == 1
    assert diagnostics["unclassified_api_identities"] == 0


def test_without_the_explicit_opt_in_pending_cases_block_every_plan(db_session, isolated):
    _, source = isolated
    actions, admissions, quarantines, metrics = _plan(
        db_session, source, allow_identity_retirement=False
    )
    assert (actions, admissions, quarantines) == ([], [], [])
    assert metrics["identity_conflict_pending"] == 4
    assert not m._pharmonline_public_api_reconciliation_is_safe(metrics)


def test_apply_closes_all_cases_and_keeps_every_historical_row(db_session, isolated):
    cards, source = isolated
    before_prices = _rows(db_session, storage.PriceSnapshot)
    before_offers = _rows(db_session, storage.OfferObservation)
    before_admissions = _rows(db_session, storage.PharmonlinePublicAPIIdentityAdmission)
    before_cases = _rows(db_session, storage.PharmonlineIdentityConflict)
    before_variants = _rows(db_session, storage.PharmonlineIdentityConflictVariant)

    _apply(db_session, source)

    assert _rows(db_session, storage.PriceSnapshot) == before_prices
    assert _rows(db_session, storage.OfferObservation) == before_offers
    assert _rows(db_session, storage.PharmonlinePublicAPIIdentityAdmission) == before_admissions
    assert _rows(db_session, storage.PharmonlineIdentityConflict) == before_cases
    assert _rows(db_session, storage.PharmonlineIdentityConflictVariant) == before_variants
    assert _count(db_session, storage.PharmonlinePublicAPIIdentityRetirement) == 3
    decisions = {
        row.product_id: row.decision
        for row in db_session.scalars(select(storage.PharmonlineIdentityConflictResolution))
    }
    assert decisions == {
        cards["kalsimiks"].id: resolution.RETIRED,
        cards["fruto"].id: resolution.RETIRED,
        cards["raxme"].id: resolution.RETIRED,
        cards["shadow"].id: resolution.SHADOW,
        cards["current"].id: resolution.CONFIRMED,
    }
    assert not any(product.identity_conflicted for product in cards.values())
    # Archived rows leave current prices; the unchanged card returns to them.
    for key in ("kalsimiks", "fruto", "raxme", "shadow"):
        assert product_policy.identity_is_retired(cards[key])
        assert cards[key].external_id.startswith("pharmonline-retired-")
    assert product_policy.source_identity_eligibility(cards["current"]).eligible
    assert cards["current"].external_id == _meteor_id(3)
    assert cards["shadow"].url == cards["current"].url


def test_next_scrape_is_whole_again_and_publishes_replacements(db_session, isolated):
    cards, source = isolated
    _apply(db_session, source)
    archived = {key: (p.external_id, p.url, p.name) for key, p in cards.items()}

    trusted, partition, _, _ = _isolate(db_session, source)

    assert partition is None
    assert sum(len(result.products) for result in trusted) == len(source[0].products)
    assert _count(db_session, storage.PharmonlineIdentityConflict) == 5
    assert _count(db_session, storage.PharmonlineIdentityConflictVariant) == 5
    run = _run(db_session)
    assert m.persist_results(db_session, run, trusted) == len(source[0].products)
    db_session.expire_all()
    assert {key: (p.external_id, p.url, p.name) for key, p in cards.items()} == archived
    assert not db_session.scalar(
        select(storage.Product.id).where(storage.Product.identity_conflicted.is_(True))
    )
    # A replayed plan is an empty no-op; the approved manifest cannot repeat.
    actions, admissions, quarantines, metrics = _plan(db_session, source)
    assert (actions, admissions, quarantines) == ([], [], [])
    assert metrics["identity_conflict_cases"] == 0


def test_approved_manifest_cannot_be_replayed_after_success(db_session, isolated):
    _, source = isolated
    approved = _manifest(db_session, source)
    _apply(db_session, source, expected_plan_manifest_sha256=approved)
    with pytest.raises(m.PharmonlinePublicAPIReconciliationError, match="read-only proof"):
        _apply(db_session, source, expected_plan_manifest_sha256=approved)
    assert _count(db_session, storage.PharmonlineIdentityConflictResolution) == 5


def test_case_drift_between_plan_and_apply_writes_nothing(db_session, isolated):
    cards, source = isolated
    approved = _manifest(db_session, source)
    _isolate(db_session, source)  # a nightly run advances every case after the review
    before = _rows(db_session, storage.Product)
    with pytest.raises(m.PharmonlinePublicAPIReconciliationError, match="read-only proof"):
        _apply(db_session, source, expected_plan_manifest_sha256=approved)
    db_session.rollback()
    assert _rows(db_session, storage.Product) == before
    assert _count(db_session, storage.PharmonlineIdentityConflictResolution) == 0
    assert _count(db_session, storage.PharmonlinePublicAPIIdentityRetirement) == 0


@pytest.mark.parametrize(
    "override",
    [
        {"source_transport": "direct"},
        {"allow_identity_retirement": False},
        {"expected_plan_manifest_sha256": None},
        {"admission_proof_version": m._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_PROOF_VERSION},
    ],
)
def test_apply_requires_the_manual_reviewed_decodo_route(db_session, isolated, override):
    _, source = isolated
    before = _rows(db_session, storage.Product)
    with pytest.raises(m.PharmonlinePublicAPIReconciliationError):
        _apply(db_session, source, **override)
    db_session.rollback()
    assert _rows(db_session, storage.Product) == before
    assert _count(db_session, storage.PharmonlineIdentityConflictResolution) == 0


def test_shadow_with_any_observation_is_not_archived(db_session, isolated):
    cards, source = isolated
    run = db_session.scalar(select(storage.Run).order_by(storage.Run.id))
    db_session.add(
        storage.OfferObservation(
            tenant_id=1,
            run_id=run.id,
            product_id=cards["shadow"].id,
            availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
        )
    )
    db_session.commit()
    _, _, quarantines, metrics = _plan(db_session, source)
    assert metrics["identity_conflict_unresolved"] == 1
    assert metrics["identity_conflict_shadows_ready"] == 0
    assert not m._pharmonline_public_api_reconciliation_is_safe(metrics)


def test_shadow_decision_requires_the_current_card_to_keep_its_name(db_session, isolated):
    _, _ = isolated
    renamed = _source(azitromisin_name=AZITROMISIN + " yeni")
    metrics = _plan(db_session, renamed)[-1]
    assert metrics["identity_conflict_unresolved"] == 1
    assert not m._pharmonline_public_api_reconciliation_is_safe(metrics)


def test_name_mismatch_keeps_retirement_case_open(db_session, isolated):
    _, source = isolated
    source[0].products[-3] = replace(source[0].products[-3], name="Fruto Nyanya 200 q")
    metrics = _plan(db_session, source)[-1]
    assert metrics["identity_conflict_unresolved"] == 1
    assert metrics["identity_conflict_retirements_ready"] == 2


def test_case_with_two_source_pairs_is_not_closed_by_either(db_session, isolated):
    cards, source = isolated
    moved = _source()
    moved[0].products[-1] = _api(3, "azitromisin-yeni", name=AZITROMISIN)
    _isolate(db_session, moved)  # the current card's case now has two variants
    metrics = _plan(db_session, moved)[-1]
    assert metrics["identity_conflict_unresolved"] >= 1
    assert not m._pharmonline_public_api_reconciliation_is_safe(metrics)


def test_closed_case_reopens_when_the_product_conflicts_again(db_session, isolated):
    cards, source = isolated
    _apply(db_session, source)
    moved = _source()
    moved[0].products[-1] = _api(3, "azitromisin-yeni", name=AZITROMISIN)

    _, partition, _, _ = _isolate(db_session, moved)

    assert partition["isolated_count"] == 1
    db_session.expire_all()
    assert cards["current"].identity_conflicted
    assert not cards["shadow"].identity_conflicted
    assert _count(db_session, storage.PharmonlineIdentityConflict) == 5
    state = resolution.load_case_state(
        db_session, {p.id: p for p in db_session.scalars(select(storage.Product))}
    )
    assert [case.product_id for case in state.pending] == [cards["current"].id]
    assert _count(db_session, storage.PharmonlineIdentityConflictResolution) == 5
    # The archived shadow stays archived and excluded.
    assert product_policy.identity_is_retired(cards["shadow"])


@pytest.mark.parametrize("tamper", ["marker", "evidence", "decision"])
def test_tampered_decision_refuses_the_next_catalog(db_session, isolated, tamper):
    cards, source = isolated
    _apply(db_session, source)
    row = db_session.scalar(
        select(storage.PharmonlineIdentityConflictResolution).where(
            storage.PharmonlineIdentityConflictResolution.product_id == cards["current"].id
        )
    )
    if tamper == "marker":
        cards["fruto"].identity_conflicted = True
    elif tamper == "evidence":
        row.evidence_sha256 = "not-a-digest"
    else:
        row.decision = resolution.RETIRED
    db_session.commit()
    with pytest.raises(m.PharmonlinePublicAPIIdentityError):
        _isolate(db_session, source)


def test_archive_marker_without_decision_fails_verification(db_session, isolated):
    cards, source = isolated
    _apply(db_session, source)
    db_session.query(storage.PharmonlineIdentityConflictResolution).filter_by(
        decision=resolution.SHADOW
    ).delete()
    db_session.commit()
    with pytest.raises(m.PharmonlinePublicAPIIdentityError):
        m._verify_pharmonline_public_api_identities(db_session, source, tenant_id=1)


def test_injected_failure_rolls_back_retirements_and_decisions(db_session, isolated, monkeypatch):
    cards, source = isolated
    approved = _manifest(db_session, source)
    before = _rows(db_session, storage.Product)

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure after retirements")

    monkeypatch.setattr(resolution, "apply_resolutions", boom)
    with pytest.raises(RuntimeError, match="injected"):
        _apply(db_session, source, expected_plan_manifest_sha256=approved)
    db_session.rollback()
    assert _rows(db_session, storage.Product) == before
    assert _count(db_session, storage.PharmonlinePublicAPIIdentityRetirement) == 0
    assert _count(db_session, storage.PharmonlineIdentityConflictResolution) == 0


def test_later_reconciled_move_of_the_current_card_keeps_decisions_valid(db_session, isolated):
    cards, source = isolated
    _apply(db_session, source)
    # A separate reviewed rebind may later move the confirmed card in place.
    cards["current"].url = "https://pharmonline.az/product/azitromisin-yeni"
    db_session.commit()
    moved = _source()
    moved[0].products[-1] = _api(3, "azitromisin-yeni", name=AZITROMISIN)

    _, partition, _, _ = _isolate(db_session, moved)

    assert partition is None
    assert not cards["current"].identity_conflicted
    assert product_policy.identity_is_retired(cards["shadow"])
    m._verify_pharmonline_public_api_identities(db_session, moved, tenant_id=1)
