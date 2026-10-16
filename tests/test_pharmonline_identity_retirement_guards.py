"""Independent guard regressions for manual, history-preserving identity recovery."""

import pytest
from sqlalchemy import select

from src import main as m, storage
from tests.test_pharmonline_identity_retirement import (
    apply,
    manifest,
    plan,
    public,
    row_values,
    seed,
)


def _retirement_count(session):
    return session.query(storage.PharmonlinePublicAPIIdentityRetirement).count()


def test_older_manual_recovery_cannot_bypass_pending_identity_review(db_session):
    old, results, proofs = seed(db_session)
    approved = manifest(db_session, results, proofs)
    old[0].identity_conflicted = True
    db_session.commit()
    before = [row_values(product) for product in old]
    with pytest.raises(m.PharmonlinePublicAPIReconciliationError):
        apply(db_session, results, proofs, expected_plan_manifest_sha256=approved)
    assert [row_values(product) for product in old] == before
    assert _retirement_count(db_session) == 0


def test_explicit_retirement_requires_new_schema_before_any_write(db_session):
    old, results, proofs = seed(db_session)
    approved = manifest(db_session, results, proofs)
    before = [row_values(product) for product in old]
    storage.PharmonlinePublicAPIIdentityRetirement.__table__.drop(db_session.connection())

    with pytest.raises(m.PharmonlinePublicAPIReconciliationError, match="retirement schema"):
        apply(db_session, results, proofs, expected_plan_manifest_sha256=approved)

    assert [row_values(product) for product in old] == before
    assert db_session.query(storage.Product).count() == 3
    assert db_session.query(storage.PharmonlinePublicAPIIdentityAdmission).count() == 3


def test_pre_recovery_manifest_cannot_be_replayed_after_success(db_session):
    _, results, proofs = seed(db_session)
    approved = manifest(db_session, results, proofs)
    apply(db_session, results, proofs, expected_plan_manifest_sha256=approved)
    before = [row_values(row) for row in db_session.scalars(select(storage.Product))]

    with pytest.raises(m.PharmonlinePublicAPIReconciliationError, match="read-only proof"):
        apply(db_session, results, proofs, expected_plan_manifest_sha256=approved)

    assert [row_values(row) for row in db_session.scalars(select(storage.Product))] == before
    assert _retirement_count(db_session) == 3


@pytest.mark.parametrize("change", ["native_id", "url"])
def test_recovered_identity_cannot_be_retired_again(db_session, change):
    _, results, proofs = seed(db_session)
    apply(db_session, results, proofs)
    current = results[0].products[1]
    replacement = db_session.scalar(
        select(storage.Product).where(storage.Product.external_id == current.external_id)
    )
    assert replacement is not None
    if change == "native_id":
        current.external_id = "G" * 17
        later_proofs = ()
    else:
        previous_url = current.url
        current.url += "-moved-again"
        later_proofs = (
            m._PharmonlinePublicAPILegacySelfRedirectProof(
                product_id=replacement.id,
                public_api_external_id=current.external_id,
                legacy_canonical_url=previous_url,
                public_api_canonical_url=current.url,
            ),
        )

    assert not m._pharmonline_public_api_reconciliation_is_safe(
        plan(db_session, results, later_proofs)[-1]
    )
    with pytest.raises(m.PharmonlinePublicAPIReconciliationError):
        apply(db_session, results, later_proofs)
    assert _retirement_count(db_session) == 3
    assert db_session.query(storage.Product).count() == 6


@pytest.mark.parametrize("duplicate", ["native_id", "url"])
def test_duplicate_current_source_identity_never_reaches_retirement(db_session, duplicate):
    old, results, proofs = seed(db_session)
    first = results[0].products[0]
    duplicate_id = first.external_id if duplicate == "native_id" else "Z" * 17
    duplicate_url = first.url if duplicate == "url" else first.url + "-duplicate"
    results[0].products.append(public(duplicate_id, duplicate_url, first.name))

    with pytest.raises(m.PharmonlinePublicAPIIdentityError, match="duplicate_api"):
        apply(db_session, results, proofs)

    assert _retirement_count(db_session) == 0
    assert db_session.query(storage.Product).count() == len(old)


def test_foreign_tenant_target_native_id_cannot_be_taken(db_session):
    old, results, proofs = seed(db_session)
    before = [row_values(product) for product in old]
    target = results[0].products[1]
    foreign = storage.Product(
        tenant_id=2,
        site="pharmonline",
        external_id=target.external_id,
        url="https://pharmonline.az/product/foreign-owner",
        name="Foreign tenant",
        name_normalized="foreign tenant",
    )
    db_session.add(foreign)
    db_session.flush()
    assert plan(db_session, results, proofs)[-1]["target_id_cross_tenant"] == 1

    with pytest.raises(m.PharmonlinePublicAPIReconciliationError):
        apply(db_session, results, proofs)

    assert [row_values(product) for product in old] == before
    assert foreign.tenant_id == 2
    assert _retirement_count(db_session) == 0


def test_apply_refreshes_stale_admission_before_checking_approved_digest(db_session):
    old, results, proofs = seed(db_session)
    admission = db_session.scalar(
        select(storage.PharmonlinePublicAPIIdentityAdmission).where(
            storage.PharmonlinePublicAPIIdentityAdmission.product_id == old[0].id
        )
    )
    approved = manifest(db_session, results, proofs)
    before = [row_values(product) for product in old]
    table = storage.PharmonlinePublicAPIIdentityAdmission.__table__
    db_session.execute(
        table.update().where(table.c.id == admission.id).values(source_manifest_sha256="e" * 64)
    )
    assert admission.source_manifest_sha256 == "a" * 64  # Deliberately stale identity map.

    with pytest.raises(m.PharmonlinePublicAPIReconciliationError, match="read-only proof"):
        apply(db_session, results, proofs, expected_plan_manifest_sha256=approved)

    assert admission.source_manifest_sha256 == "e" * 64
    assert [row_values(product) for product in old] == before
    assert _retirement_count(db_session) == 0


def test_apply_refreshes_stale_product_before_reclassifying_identity(db_session):
    old, results, proofs = seed(db_session)
    approved = manifest(db_session, results, proofs)
    original_name = old[1].name
    table = storage.Product.__table__
    db_session.execute(
        table.update()
        .where(table.c.id == old[1].id)
        .values(name="Conflicting identity after the read-only plan")
    )
    assert old[1].name == original_name

    with pytest.raises(m.PharmonlinePublicAPIReconciliationError, match="read-only proof"):
        apply(db_session, results, proofs, expected_plan_manifest_sha256=approved)

    assert old[1].name != original_name
    assert _retirement_count(db_session) == 0
    assert db_session.query(storage.Product).count() == 3


def test_verifier_refreshes_stale_admission_after_retirement(db_session):
    _, results, proofs = seed(db_session)
    apply(db_session, results, proofs)
    admission = db_session.scalar(select(storage.PharmonlinePublicAPIIdentityAdmission))
    table = storage.PharmonlinePublicAPIIdentityAdmission.__table__
    db_session.execute(
        table.update().where(table.c.id == admission.id).values(preflight_run_ref="999999")
    )
    assert admission.preflight_run_ref != "999999"

    with pytest.raises(m.PharmonlinePublicAPIIdentityError, match="retirement_conflicts"):
        m._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1)

    assert admission.preflight_run_ref == "999999"


def test_stock_supplier_and_canonical_references_remain_on_historical_products(db_session):
    old, results, proofs = seed(db_session)
    for product in old:
        match = storage.Match(tenant_id=1, canonical_name=product.name, is_manual=True)
        db_session.add(match)
        db_session.flush()
        product.canonical_id = match.id
        db_session.add_all(
            [
                storage.StockLevel(
                    product_id=product.id,
                    canonical_id=match.id,
                    sku="erp-original",
                    qty=12,
                    is_in_stock=True,
                ),
                storage.SupplierPrice(
                    product_id=product.id,
                    canonical_id=match.id,
                    supplier_name="Original supplier",
                    purchase_price=3.5,
                ),
            ]
        )
    db_session.flush()
    models = (
        storage.Match,
        storage.StockLevel,
        storage.SupplierPrice,
        storage.PriceSnapshot,
        storage.OfferObservation,
    )
    before = {
        model: [row_values(row) for row in db_session.scalars(select(model))] for model in models
    }
    canonical_ids = {product.id: product.canonical_id for product in old}

    apply(db_session, results, proofs)

    for model in models:
        assert [row_values(row) for row in db_session.scalars(select(model))] == before[model]
    for product in old:
        assert product.canonical_id == canonical_ids[product.id]
    replacements = db_session.scalars(
        select(storage.Product).where(storage.Product.id.not_in([product.id for product in old]))
    ).all()
    assert len(replacements) == 3
    for product in replacements:
        assert product.canonical_id is None
        for model in (storage.StockLevel, storage.SupplierPrice):
            assert db_session.scalar(select(model.id).where(model.product_id == product.id)) is None
