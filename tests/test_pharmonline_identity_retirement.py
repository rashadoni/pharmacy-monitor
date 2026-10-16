"""History-preserving recovery of the three observed identity mutation shapes."""

import pytest
from sqlalchemy import select

from src import main as m, storage
from src import pharmonline_identity_recovery as recovery
from src.scrapers.base import ScrapedProduct, ScrapeResult
from src.scrapers.pharmonline_public_api import PUBLIC_API_AVAILABILITY_SOURCE


def public(external_id, url, name):
    return ScrapedProduct(
        site="pharmonline",
        external_id=external_id,
        url=url,
        name=name,
        identity_verified=True,
        availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
    )


def seed(session):
    run = storage.Run(status="ok")
    session.add(run)
    session.flush()
    old, current, proofs = [], [], []
    for index, name in enumerate(("Kalsimiks №30", "Fruto Nyanya 120 g", "Raxme Omega-3 150 ml")):
        external_id = chr(65 + index) * 17
        url = f"https://pharmonline.az/product/item-{index}"
        product = storage.Product(
            tenant_id=1,
            site="pharmonline",
            external_id=external_id,
            url=url,
            name=name,
            name_normalized=name,
            barcode=None,
            availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
            offer_availability_status="in_stock",
            offer_quantity=4,
            availability_run_id=run.id,
        )
        session.add(product)
        session.flush()
        session.add(
            storage.PharmonlinePublicAPIIdentityAdmission(
                tenant_id=1,
                product_id=product.id,
                admission_kind="new_public_product",
                public_api_external_id=external_id,
                public_api_canonical_url=url,
                proof_version=m._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_PROOF_VERSION,
                source_manifest_sha256="a" * 64,
                catalog_fingerprint_sha256="b" * 64,
                source_transport="direct",
                preflight_run_ref=str(run.id),
            )
        )
        session.add(storage.PriceSnapshot(run_id=run.id, product_id=product.id, price=7.0))
        session.add(
            storage.OfferObservation(
                tenant_id=1,
                run_id=run.id,
                product_id=product.id,
                availability_source="pharmonline_ddp_total_count",
            )
        )
        new_url = url + "-moved" if index == 0 else url
        new_id = external_id if index == 0 else chr(68 + index) * 17
        current.append(public(new_id, new_url, name))
        if index == 0:
            proofs.append(
                m._PharmonlinePublicAPILegacySelfRedirectProof(
                    product_id=product.id,
                    public_api_external_id=new_id,
                    legacy_canonical_url=url,
                    public_api_canonical_url=new_url,
                )
            )
        old.append(product)
    session.commit()
    return old, [ScrapeResult(site="pharmonline", products=current)], tuple(proofs)


def plan(session, results, proofs, **kwargs):
    return m._pharmonline_public_api_reconciliation_plan(
        session,
        results,
        tenant_id=1,
        legacy_self_redirect_proofs=proofs,
        allow_identity_retirement=True,
        **kwargs,
    )


def manifest(session, results, proofs):
    return m._pharmonline_public_api_reconciliation_plan_manifest_sha256(
        *plan(session, results, proofs)
    )


def apply(session, results, proofs, **kwargs):
    args = dict(
        tenant_id=1,
        source_manifest_sha256="c" * 64,
        catalog_fingerprint_sha256=m._pharmonline_public_api_catalog_fingerprint(results),
        source_transport="decodo",
        preflight_run_ref="123456",
        legacy_self_redirect_proofs=proofs,
        allow_identity_retirement=True,
        admission_proof_version=m._PHARMONLINE_PUBLIC_API_ADMISSION_PROOF_VERSION,
        expected_plan_manifest_sha256=manifest(session, results, proofs),
    )
    args.update(kwargs)
    return m._apply_pharmonline_public_api_reconciliation(session, results, **args)


def row_values(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def test_three_mutations_preserve_history_and_original_admissions(db_session):
    session = db_session
    old, results, proofs = seed(session)
    before_products = {p.id: row_values(p) for p in old}
    before_audits = [
        row_values(a)
        for a in session.scalars(select(storage.PharmonlinePublicAPIIdentityAdmission))
    ]
    before_prices = [row_values(a) for a in session.scalars(select(storage.PriceSnapshot))]
    before_offers = [row_values(a) for a in session.scalars(select(storage.OfferObservation))]
    normal = m._pharmonline_public_api_reconciliation_plan(
        session, results, tenant_id=1, legacy_self_redirect_proofs=proofs
    )[-1]
    assert not m._pharmonline_public_api_reconciliation_is_safe(normal)
    actions, admissions, retirements, metrics = plan(session, results, proofs)
    assert not actions and not admissions and len(retirements) == 3
    assert metrics["identity_retirements_ready"] == 3
    assert m._pharmonline_public_api_reconciliation_is_safe(metrics)
    apply(session, results, proofs)
    assert m._verify_pharmonline_public_api_identities(session, results, tenant_id=1) == 3
    assert [
        row_values(a)
        for a in session.scalars(select(storage.PharmonlinePublicAPIIdentityAdmission))
    ] == before_audits
    assert [row_values(a) for a in session.scalars(select(storage.PriceSnapshot))] == before_prices
    assert [
        row_values(a) for a in session.scalars(select(storage.OfferObservation))
    ] == before_offers
    allowed = {
        "external_id",
        "offer_availability_status",
        "offer_quantity",
        "availability_source",
        "availability_observed_at",
        "availability_run_id",
    }
    for product in old:
        after = row_values(product)
        assert {k: v for k, v in after.items() if k not in allowed} == {
            k: v for k, v in before_products[product.id].items() if k not in allowed
        }
        assert product.availability_source == recovery.AVAILABILITY_SOURCE
        assert product.external_id.startswith("pharmonline-retired-")
    ledgers = session.scalars(select(storage.PharmonlinePublicAPIIdentityRetirement)).all()
    assert len(ledgers) == 3
    for ledger in ledgers:
        replacement = session.get(storage.Product, ledger.replacement_product_id)
        assert replacement.canonical_id is None
        for model in (
            storage.PriceSnapshot,
            storage.OfferObservation,
            storage.PharmonlinePublicAPIIdentityAdmission,
        ):
            assert (
                session.scalar(select(model.id).where(model.product_id == replacement.id)) is None
            )
    # Replanning is a safe no-op, never a second admission or retirement.
    assert plan(session, results, proofs)[:3] == ([], [], [])
    apply(session, results, proofs)
    assert session.query(storage.Product).count() == 6
    assert session.query(storage.PharmonlinePublicAPIIdentityRetirement).count() == 3


@pytest.mark.parametrize(
    "override",
    [
        {"expected_plan_manifest_sha256": None},
        {"expected_plan_manifest_sha256": "d" * 64},
        {"catalog_fingerprint_sha256": "d" * 64},
        {"source_transport": "direct"},
        {"admission_proof_version": m._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_PROOF_VERSION},
        {"allow_identity_retirement": False},
    ],
)
def test_apply_requires_exact_manual_reviewed_proof(db_session, override):
    old, results, proofs = seed(db_session)
    before = [row_values(p) for p in old]
    with pytest.raises(m.PharmonlinePublicAPIReconciliationError):
        apply(db_session, results, proofs, **override)
    assert [row_values(p) for p in old] == before
    assert db_session.query(storage.Product).count() == 3


def test_prior_audit_drift_changes_approved_manifest(db_session):
    _, results, proofs = seed(db_session)
    approved = manifest(db_session, results, proofs)
    admission = db_session.query(storage.PharmonlinePublicAPIIdentityAdmission).first()
    admission.source_manifest_sha256 = "e" * 64
    db_session.flush()
    with pytest.raises(
        m.PharmonlinePublicAPIReconciliationError, match="differs from its read-only proof"
    ):
        apply(db_session, results, proofs, expected_plan_manifest_sha256=approved)
    assert db_session.query(storage.Product).count() == 3


@pytest.mark.parametrize(
    "damage", ["admission", "ledger", "marker", "replacement", "missing_ledger"]
)
def test_invalid_recovery_evidence_cannot_trust_or_hide_history(db_session, damage):
    old, results, proofs = seed(db_session)
    apply(db_session, results, proofs)
    ledger = db_session.query(storage.PharmonlinePublicAPIIdentityRetirement).first()
    if damage == "admission":
        db_session.get(
            storage.PharmonlinePublicAPIIdentityAdmission, ledger.prior_admission_id
        ).source_manifest_sha256 = "e" * 64
    elif damage == "ledger":
        ledger.prior_admission_sha256 = "e" * 64
    elif damage == "marker":
        old[0].availability_source = PUBLIC_API_AVAILABILITY_SOURCE
    elif damage == "replacement":
        db_session.get(storage.Product, ledger.replacement_product_id).tenant_id = 2
    else:
        db_session.delete(ledger)
    db_session.flush()
    with pytest.raises(m.PharmonlinePublicAPIIdentityError):
        m._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1)
    assert not m._pharmonline_public_api_reconciliation_is_safe(
        plan(db_session, results, proofs)[-1]
    )


def test_failed_replacement_rolls_back_entire_batch(db_session, monkeypatch):
    old, results, proofs = seed(db_session)
    before = [row_values(p) for p in old]
    original = m._new_pharmonline_public_api_identity_product
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected failure after first retirement")
        return original(*args, **kwargs)

    monkeypatch.setattr(m, "_new_pharmonline_public_api_identity_product", fail_second)
    with pytest.raises(RuntimeError, match="injected failure"):
        with db_session.begin_nested():
            apply(db_session, results, proofs)
    assert [row_values(p) for p in old] == before
    assert db_session.query(storage.Product).count() == 3
    assert db_session.query(storage.PharmonlinePublicAPIIdentityRetirement).count() == 0


@pytest.mark.parametrize(
    "conflict", ["name", "foreign_url", "old_id_present", "missing_admission", "missing_redirect"]
)
def test_ambiguous_source_or_history_keeps_plan_closed(db_session, conflict):
    old, results, proofs = seed(db_session)
    if conflict == "name":
        results[0].products[1].name = "Different pack 240 g"
    elif conflict == "foreign_url":
        shadow = storage.Product(
            tenant_id=2,
            site="pharmonline",
            external_id="Z" * 17,
            url=old[1].url,
            name="shadow",
            name_normalized="shadow",
        )
        db_session.add(shadow)
    elif conflict == "old_id_present":
        results[0].products.append(
            public(old[1].external_id, old[1].url + "-still-live", old[1].name)
        )
    elif conflict == "missing_admission":
        db_session.delete(
            db_session.query(storage.PharmonlinePublicAPIIdentityAdmission)
            .filter_by(product_id=old[1].id)
            .one()
        )
    else:
        proofs = ()
    db_session.flush()
    assert not m._pharmonline_public_api_reconciliation_is_safe(
        plan(db_session, results, proofs)[-1]
    )
    with pytest.raises(m.PharmonlinePublicAPIReconciliationError):
        apply(db_session, results, proofs)
    assert db_session.query(storage.PharmonlinePublicAPIIdentityRetirement).count() == 0
