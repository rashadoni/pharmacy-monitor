"""Safety checks for the guarded Pharmonline HTML recovery bridge."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src import main as main_mod
from src import storage
from src.normalize import normalize_name
from src.scrapers.base import ScrapedProduct, ScrapeResult
from src.scrapers.pharmonline_public_api import PUBLIC_API_AVAILABILITY_SOURCE


_METEOR_ID = "xwJspdCx3iFBDqDWF"


def _stored_product(
    *,
    tenant_id: int = 1,
    url: str,
    external_id: str = _METEOR_ID,
    availability_source: str | None = "pharmonline_ddp_total_count",
    name: str = "Existing Pharmonline product",
    barcode: str | None = None,
):
    return storage.Product(
        tenant_id=tenant_id,
        site="pharmonline",
        external_id=external_id,
        url=url,
        name=name,
        name_normalized=normalize_name(name),
        availability_source=availability_source,
        barcode=barcode,
    )


def _legacy_product(url: str, external_id: str = "legacy-url-slug") -> ScrapedProduct:
    return ScrapedProduct(
        site="pharmonline",
        external_id=external_id,
        url=url,
        name="Rendered Pharmonline product",
        price=12.5,
    )


def test_bridge_replaces_legacy_slug_with_same_tenant_ddp_id(db_session):
    db_session.add(_stored_product(url="https://www.pharmonline.az/product/ringer-400-ml?lng=az"))
    db_session.commit()
    rendered = _legacy_product("https://pharmonline.az/product/ringer-400-ml?lng=en")

    bridged = main_mod._bridge_pharmonline_legacy_ids(
        db_session,
        [ScrapeResult(site="pharmonline", products=[rendered])],
        tenant_id=1,
    )

    assert bridged == 1
    assert rendered.external_id == _METEOR_ID


def test_bridge_checks_url_even_when_rendered_value_looks_like_meteor_id(db_session):
    """A 17-character slug must not bypass the bridge by looking like a DDP ID."""
    db_session.add(_stored_product(url="https://pharmonline.az/product/known-product"))
    db_session.commit()
    rendered = _legacy_product(
        "https://pharmonline.az/product/unknown-product",
        external_id="abcdefghijklmnopq",
    )

    with pytest.raises(
        main_mod.PharmonlineLegacyIdentityBridgeError,
        match="unresolved_urls=1, ambiguous_urls=0",
    ):
        main_mod._bridge_pharmonline_legacy_ids(
            db_session,
            [ScrapeResult(site="pharmonline", products=[rendered])],
            tenant_id=1,
        )

    assert rendered.external_id == "abcdefghijklmnopq"


def test_bridge_does_not_mutate_valid_cards_when_any_url_is_unresolved(db_session):
    known_url = "https://pharmonline.az/product/known-product"
    db_session.add(_stored_product(url=known_url))
    db_session.commit()
    known = _legacy_product(known_url)
    unknown = _legacy_product("https://pharmonline.az/product/unknown-product")

    with pytest.raises(
        main_mod.PharmonlineLegacyIdentityBridgeError,
        match="unresolved_urls=1, ambiguous_urls=0, mismatched_ids=0",
    ):
        main_mod._bridge_pharmonline_legacy_ids(
            db_session,
            [ScrapeResult(site="pharmonline", products=[known, unknown])],
            tenant_id=1,
        )

    assert known.external_id == "legacy-url-slug"
    assert unknown.external_id == "legacy-url-slug"


def test_bridge_rejects_a_direct_meteor_id_that_disagrees_with_its_url(db_session):
    """A first-party card ID is evidence to validate, never a value to overwrite."""
    url = "https://pharmonline.az/product/known-product"
    db_session.add(_stored_product(url=url))
    db_session.commit()
    rendered = _legacy_product(url, external_id="6kHnwLLMpYXyebN8f")
    rendered.identity_verified = True

    with pytest.raises(
        main_mod.PharmonlineLegacyIdentityBridgeError,
        match="unresolved_urls=0, ambiguous_urls=0, mismatched_ids=1",
    ):
        main_mod._bridge_pharmonline_legacy_ids(
            db_session,
            [ScrapeResult(site="pharmonline", products=[rendered])],
            tenant_id=1,
        )

    assert rendered.external_id == "6kHnwLLMpYXyebN8f"


def test_bridge_requires_an_existing_ddp_provenance_marker(db_session):
    url = "https://pharmonline.az/product/unproven-legacy-row"
    db_session.add(
        _stored_product(
            url=url,
            availability_source=None,
        )
    )
    db_session.commit()

    with pytest.raises(
        main_mod.PharmonlineLegacyIdentityBridgeError,
        match="unresolved_urls=1, ambiguous_urls=0, mismatched_ids=0",
    ):
        main_mod._bridge_pharmonline_legacy_ids(
            db_session,
            [ScrapeResult(site="pharmonline", products=[_legacy_product(url)])],
            tenant_id=1,
        )


def test_bridge_rejects_other_tenant_and_ambiguous_url_mappings(db_session):
    """Recovery must not borrow another tenant's catalog or guess among IDs."""
    tenant_two_url = "https://pharmonline.az/product/tenant-two-only"
    db_session.add(
        _stored_product(
            tenant_id=2,
            url=tenant_two_url,
            external_id="qwertyuiopasdfghj",
        )
    )
    db_session.commit()

    with pytest.raises(
        main_mod.PharmonlineLegacyIdentityBridgeError,
        match="unresolved_urls=1, ambiguous_urls=0",
    ):
        main_mod._bridge_pharmonline_legacy_ids(
            db_session,
            [ScrapeResult(site="pharmonline", products=[_legacy_product(tenant_two_url)])],
            tenant_id=1,
        )

    ambiguous_url = "https://pharmonline.az/product/ambiguous-product"
    db_session.add_all(
        [
            _stored_product(url=ambiguous_url, external_id="xwJspdCx3iFBDqDWF"),
            _stored_product(url=ambiguous_url, external_id="6kHnwLLMpYXyebN8f"),
        ]
    )
    db_session.commit()

    with pytest.raises(
        main_mod.PharmonlineLegacyIdentityBridgeError,
        match="unresolved_urls=0, ambiguous_urls=1",
    ):
        main_mod._bridge_pharmonline_legacy_ids(
            db_session,
            [ScrapeResult(site="pharmonline", products=[_legacy_product(ambiguous_url)])],
            tenant_id=1,
        )


def _public_api_product(
    url: str,
    external_id: str = _METEOR_ID,
    *,
    name: str = "Public API product",
    barcode: str | None = None,
) -> ScrapedProduct:
    return ScrapedProduct(
        site="pharmonline",
        external_id=external_id,
        url=url,
        name=name,
        identity_verified=True,
        availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
        barcode=barcode,
    )


def test_public_api_identity_proof_allows_only_retired_trusted_ddp_rows(db_session):
    current_url = "https://pharmonline.az/product/current-product"
    db_session.add_all(
        [
            _stored_product(url=current_url),
            _stored_product(
                url="https://pharmonline.az/product/retired-product",
                external_id="6kHnwLLMpYXyebN8f",
            ),
        ]
    )
    db_session.commit()
    verified = main_mod._verify_pharmonline_public_api_identities(
        db_session,
        [ScrapeResult(site="pharmonline", products=[_public_api_product(current_url)])],
        tenant_id=1,
    )

    assert verified == 1


def test_public_api_identity_proof_retains_ddp_lineage_from_observation_history(
    db_session,
):
    """A safe retry must not lose DDP lineage after a public-API observation."""
    current_url = "https://pharmonline.az/product/current-product"
    product = _stored_product(
        url=current_url,
        availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
    )
    db_session.add(product)
    db_session.commit()
    run = storage.Run(status="ok")
    db_session.add(run)
    db_session.commit()
    db_session.add(
        storage.OfferObservation(
            tenant_id=1,
            run_id=run.id,
            product_id=product.id,
            availability_source="pharmonline_ddp_total_count",
        )
    )
    db_session.commit()

    verified = main_mod._verify_pharmonline_public_api_identities(
        db_session,
        [ScrapeResult(site="pharmonline", products=[_public_api_product(current_url)])],
        tenant_id=1,
    )

    assert verified == 1


def test_public_api_identity_proof_rejects_an_unexpected_product_source(db_session):
    current_url = "https://pharmonline.az/product/current-product"
    db_session.add(_stored_product(url=current_url))
    db_session.commit()
    invalid_source = _public_api_product(current_url)
    invalid_source.availability_source = "untrusted_source"

    with pytest.raises(
        main_mod.PharmonlinePublicAPIIdentityError,
        match="invalid_api=1",
    ):
        main_mod._verify_pharmonline_public_api_identities(
            db_session,
            [ScrapeResult(site="pharmonline", products=[invalid_source])],
            tenant_id=1,
        )


def test_public_api_identity_proof_rejects_mixed_results(db_session):
    with pytest.raises(
        main_mod.PharmonlinePublicAPIIdentityError,
        match="unexpected_results=1",
    ):
        main_mod._verify_pharmonline_public_api_identities(
            db_session,
            [
                ScrapeResult(site="pharmonline"),
                ScrapeResult(site="aloe"),
            ],
            tenant_id=1,
        )


def test_public_api_identity_proof_rejects_unknown_api_id(db_session):
    db_session.add(_stored_product(url="https://pharmonline.az/product/known-product"))
    db_session.commit()

    with pytest.raises(
        main_mod.PharmonlinePublicAPIIdentityError,
        match="missing_trusted_ids=1",
    ):
        main_mod._verify_pharmonline_public_api_identities(
            db_session,
            [
                ScrapeResult(
                    site="pharmonline",
                    products=[
                        _public_api_product(
                            "https://pharmonline.az/product/new-product",
                            external_id="6kHnwLLMpYXyebN8f",
                        )
                    ],
                )
            ],
            tenant_id=1,
        )


def test_public_api_identity_proof_rejects_rebound_id_url(db_session):
    db_session.add(_stored_product(url="https://pharmonline.az/product/old-path"))
    db_session.commit()

    with pytest.raises(
        main_mod.PharmonlinePublicAPIIdentityError,
        match="mismatched_urls=1",
    ):
        main_mod._verify_pharmonline_public_api_identities(
            db_session,
            [
                ScrapeResult(
                    site="pharmonline",
                    products=[_public_api_product("https://pharmonline.az/product/rebound-path")],
                )
            ],
            tenant_id=1,
        )


def test_public_api_identity_proof_does_not_misclassify_retired_ddp_rows_as_a_source_drop(
    db_session,
):
    current_url = "https://pharmonline.az/product/current-product"
    db_session.add_all(
        [
            _stored_product(url=current_url),
            _stored_product(
                url="https://pharmonline.az/product/retired-product",
                external_id="6kHnwLLMpYXyebN8f",
            ),
        ]
    )
    db_session.commit()

    assert (
        main_mod._verify_pharmonline_public_api_identities(
            db_session,
            [ScrapeResult(site="pharmonline", products=[_public_api_product(current_url)])],
            tenant_id=1,
        )
        == 1
    )


def test_public_api_identity_proof_ignores_a_non_ddp_legacy_url_shadow(db_session):
    current_url = "https://pharmonline.az/product/current-product"
    db_session.add_all(
        [
            _stored_product(url=current_url),
            _stored_product(
                url=current_url,
                external_id="legacy-current-product",
                availability_source=None,
            ),
        ]
    )
    db_session.commit()

    assert (
        main_mod._verify_pharmonline_public_api_identities(
            db_session,
            [ScrapeResult(site="pharmonline", products=[_public_api_product(current_url)])],
            tenant_id=1,
        )
        == 1
    )


_RECOVERY_MANIFEST_SHA = "a" * 64


def _reconcile(
    db_session,
    products: list[ScrapedProduct],
    *,
    preflight_run_ref: str = "123456",
    redirect_proofs=(),
    expected_plan_manifest_sha256: str | None = None,
):
    results = [ScrapeResult(site="pharmonline", products=products)]
    return main_mod._apply_pharmonline_public_api_reconciliation(
        db_session,
        results,
        tenant_id=1,
        source_manifest_sha256=_RECOVERY_MANIFEST_SHA,
        catalog_fingerprint_sha256=main_mod._pharmonline_public_api_catalog_fingerprint(results),
        source_transport="decodo",
        preflight_run_ref=preflight_run_ref,
        redirect_proofs=redirect_proofs,
        expected_plan_manifest_sha256=expected_plan_manifest_sha256,
    )


def test_public_api_reconciliation_rekeys_only_one_exact_non_ddp_legacy_url(db_session):
    url = "https://pharmonline.az/product/legacy-product"
    legacy = _stored_product(
        url=url,
        external_id="legacy-product-id",
        availability_source=None,
    )
    db_session.add(legacy)
    db_session.commit()
    product_id = legacy.id
    run = storage.Run(status="ok")
    db_session.add(run)
    db_session.commit()
    observation = storage.OfferObservation(
        tenant_id=1,
        run_id=run.id,
        product_id=legacy.id,
        availability_source="legacy_rendered_html",
    )
    db_session.add(observation)
    db_session.commit()

    public = _public_api_product(url, external_id="6kHnwLLMpYXyebN8f")
    metrics = _reconcile(db_session, [public])

    refreshed = db_session.get(storage.Product, product_id)
    record = db_session.query(storage.PharmonlinePublicAPIIdentityReconciliation).one()
    assert metrics["legacy_rekeys_ready"] == 1
    assert refreshed is not None
    assert refreshed.id == product_id
    assert refreshed.external_id == public.external_id
    assert observation.product_id == product_id
    assert record.tenant_id == 1
    assert record.product_id == product_id
    assert record.legacy_external_id == "legacy-product-id"
    assert record.public_api_external_id == public.external_id
    assert main_mod._pharmonline_public_api_recovery_tables_available(db_session)
    assert record.proof_version == main_mod._PHARMONLINE_PUBLIC_API_RECONCILIATION_PROOF_VERSION
    assert record.source_manifest_sha256 == _RECOVERY_MANIFEST_SHA
    assert (
        record.catalog_fingerprint_sha256
        == main_mod._pharmonline_public_api_catalog_fingerprint(
            [ScrapeResult(site="pharmonline", products=[public])]
        )
    )
    assert record.source_transport == "decodo"
    assert record.preflight_run_ref == "123456"
    assert main_mod._canonical_pharmonline_product_url(record.legacy_canonical_url) == url
    assert main_mod._canonical_pharmonline_product_url(record.public_api_canonical_url) == url
    assert main_mod._canonical_pharmonline_product_url(refreshed.url) == url
    assert main_mod._PHARMONLINE_METEOR_ID_RE.fullmatch(record.legacy_external_id) is None
    assert main_mod._PHARMONLINE_METEOR_ID_RE.fullmatch(record.public_api_external_id)
    assert main_mod._pharmonline_public_api_reconciliation_invalid_reason(record, refreshed) is None
    raw_reconciliation_rows = db_session.execute(
        text("SELECT id, tenant_id FROM pharmonline_public_api_identity_reconciliations")
    ).all()
    assert raw_reconciliation_rows == [(record.id, 1)]
    valid_reconciliations, conflicts = main_mod._valid_pharmonline_public_api_reconciliations(
        db_session,
        [refreshed],
        tenant_id=1,
    )
    assert conflicts == 0
    assert valid_reconciliations == {public.external_id: refreshed}
    assert (
        main_mod._verify_pharmonline_public_api_identities(
            db_session,
            [ScrapeResult(site="pharmonline", products=[public])],
            tenant_id=1,
        )
        == 1
    )


def test_public_api_reconciliation_rebinds_native_id_only_with_barcode_and_name_proof(
    db_session,
):
    old_url = "https://pharmonline.az/product/rebind-old-path"
    new_url = "https://pharmonline.az/product/rebind-new-path"
    name = "Rebind medicine 500 mg N20"
    stored = _stored_product(
        url=old_url,
        name=name,
        barcode="1234567890123",
    )
    db_session.add(stored)
    db_session.commit()
    product_id = stored.id

    public = _public_api_product(
        new_url,
        name=name,
        barcode="1234567890123",
    )
    metrics = _reconcile(db_session, [public])

    refreshed = db_session.get(storage.Product, product_id)
    record = db_session.query(storage.PharmonlinePublicAPIIdentityReconciliation).one()
    assert metrics["native_id_url_rebind"] == 1
    assert metrics["native_id_url_rebind_ready"] == 1
    assert metrics["native_id_url_rebind_barcode_match"] == 1
    assert metrics["native_id_url_rebind_name_match"] == 1
    assert refreshed is not None
    assert refreshed.id == product_id
    assert refreshed.external_id == public.external_id
    assert refreshed.url == new_url
    assert record.product_id == product_id
    assert record.legacy_canonical_url == old_url
    assert record.public_api_canonical_url == new_url
    assert record.proof_version == main_mod._PHARMONLINE_PUBLIC_API_REBIND_PROOF_VERSION
    assert (
        main_mod._verify_pharmonline_public_api_identities(
            db_session,
            [ScrapeResult(site="pharmonline", products=[public])],
            tenant_id=1,
        )
        == 1
    )


def test_public_api_reconciliation_rebinds_native_id_with_strict_redirect_proof(
    db_session,
):
    old_url = "https://pharmonline.az/product/rebind-redirect-old-path"
    new_url = "https://pharmonline.az/product/rebind-redirect-new-path"
    stored = _stored_product(url=old_url, name="Old medicine")
    db_session.add(stored)
    db_session.commit()
    public = _public_api_product(new_url, name="Current medicine")
    redirect_proof = main_mod._PharmonlinePublicAPIRedirectProof(
        product_id=stored.id,
        public_api_external_id=public.external_id,
        legacy_canonical_url=old_url,
        public_api_canonical_url=new_url,
    )

    metrics = _reconcile(db_session, [public], redirect_proofs=(redirect_proof,))

    refreshed = db_session.get(storage.Product, stored.id)
    record = db_session.query(storage.PharmonlinePublicAPIIdentityReconciliation).one()
    assert metrics["native_id_url_rebind_redirect_ready"] == 1
    assert refreshed is not None and refreshed.url == new_url
    assert record.proof_version == main_mod._PHARMONLINE_PUBLIC_API_REDIRECT_REBIND_PROOF_VERSION


def test_public_api_reconciliation_attests_an_existing_exact_native_identity(db_session):
    url = "https://pharmonline.az/product/existing-native-public-product"
    stored = _stored_product(url=url, availability_source=None)
    db_session.add(stored)
    db_session.commit()

    public = _public_api_product(url)
    metrics = _reconcile(db_session, [public])

    refreshed = db_session.get(storage.Product, stored.id)
    admission = db_session.query(storage.PharmonlinePublicAPIIdentityAdmission).one()
    assert metrics["existing_native_admissions_ready"] == 1
    assert refreshed is not None and refreshed.id == stored.id
    assert admission.product_id == stored.id
    assert admission.admission_kind == "existing_native_id"
    assert admission.public_api_external_id == public.external_id
    assert main_mod._pharmonline_public_api_admission_invalid_reason(admission, refreshed) is None
    assert (
        main_mod._verify_pharmonline_public_api_identities(
            db_session,
            [ScrapeResult(site="pharmonline", products=[public])],
            tenant_id=1,
        )
        == 1
    )


def test_public_api_reconciliation_creates_only_a_guarded_new_public_identity(db_session):
    url = "https://pharmonline.az/product/new-public-product"
    public = _public_api_product(url, name="New public medicine 500 mg N20")

    metrics = _reconcile(db_session, [public])

    product = (
        db_session.query(storage.Product)
        .filter_by(site="pharmonline", external_id=public.external_id)
        .one()
    )
    admission = db_session.query(storage.PharmonlinePublicAPIIdentityAdmission).one()
    assert metrics["new_public_product_admissions_ready"] == 1
    assert product.url == url
    assert product.availability_source is None
    assert admission.product_id == product.id
    assert admission.admission_kind == "new_public_product"
    assert db_session.query(storage.PriceSnapshot).count() == 0
    assert (
        main_mod._verify_pharmonline_public_api_identities(
            db_session,
            [ScrapeResult(site="pharmonline", products=[public])],
            tenant_id=1,
        )
        == 1
    )


def test_public_api_reconciliation_rejects_a_different_approved_plan_manifest(db_session):
    url = "https://pharmonline.az/product/new-public-product-plan-mismatch"
    public = _public_api_product(url)

    with pytest.raises(
        main_mod.PharmonlinePublicAPIReconciliationError,
        match="differs from its read-only proof",
    ):
        _reconcile(
            db_session,
            [public],
            expected_plan_manifest_sha256="b" * 64,
        )

    assert db_session.query(storage.Product).count() == 0
    assert db_session.query(storage.PharmonlinePublicAPIIdentityAdmission).count() == 0


def test_public_api_reconciliation_refuses_native_id_rebind_without_two_signals(
    db_session,
):
    old_url = "https://pharmonline.az/product/rebind-old-path"
    new_url = "https://pharmonline.az/product/rebind-new-path"
    stored = _stored_product(url=old_url, name="Old medicine 500 mg")
    db_session.add(stored)
    db_session.commit()

    assert main_mod._pharmonline_native_url_rebind_evidence(
        stored,
        _public_api_product(new_url, name="New medicine 500 mg"),
    ) == ("stored_barcode_missing", "name_mismatch")

    with pytest.raises(
        main_mod.PharmonlinePublicAPIReconciliationError,
        match="native_id_url_rebind_unproven=1",
    ):
        _reconcile(
            db_session,
            [_public_api_product(new_url, name="New medicine 500 mg")],
        )

    refreshed = db_session.get(storage.Product, stored.id)
    assert refreshed is not None
    assert refreshed.url == old_url
    assert db_session.query(storage.PharmonlinePublicAPIIdentityReconciliation).count() == 0


def test_public_api_reconciliation_refuses_an_ambiguous_legacy_url_without_mutation(db_session):
    url = "https://pharmonline.az/product/ambiguous-legacy-product"
    db_session.add_all(
        [
            _stored_product(url=url, external_id="legacy-one", availability_source=None),
            _stored_product(url=url, external_id="legacy-two", availability_source=None),
        ]
    )
    db_session.commit()

    with pytest.raises(
        main_mod.PharmonlinePublicAPIReconciliationError,
        match="legacy_url_ambiguous=1",
    ):
        _reconcile(db_session, [_public_api_product(url, external_id="6kHnwLLMpYXyebN8f")])

    assert {
        product.external_id
        for product in db_session.query(storage.Product).filter_by(site="pharmonline").all()
    } == {"legacy-one", "legacy-two"}
    assert db_session.query(storage.PharmonlinePublicAPIIdentityReconciliation).count() == 0


def test_catalog_baseline_never_lowers_after_its_initial_recovery_proof(db_session, monkeypatch):
    monkeypatch.setattr(main_mod, "_PHARMONLINE_PUBLIC_API_BOOTSTRAP_MIN_PRODUCTS", 2)
    first_results = [
        ScrapeResult(
            site="pharmonline",
            products=[
                _public_api_product("https://pharmonline.az/product/one"),
                _public_api_product(
                    "https://pharmonline.az/product/two",
                    external_id="6kHnwLLMpYXyebN8f",
                ),
            ],
        )
    ]
    fingerprint = main_mod._pharmonline_public_api_catalog_fingerprint(first_results)
    floor = main_mod._ensure_pharmonline_public_api_catalog_baseline(
        db_session,
        first_results,
        tenant_id=1,
        verified_identity_count=2,
        trusted_ddp_item_count=2,
        retired_ddp_item_count=0,
        reconciled_item_count=0,
        source_manifest_sha256=_RECOVERY_MANIFEST_SHA,
        catalog_fingerprint_sha256=fingerprint,
        source_transport="decodo",
        preflight_run_ref="123456",
    )
    assert floor == 2
    assert db_session.query(storage.PharmonlinePublicAPICatalogBaseline).count() == 1
    assert (
        db_session.query(storage.PharmonlinePublicAPICatalogBaseline)
        .one()
        .minimum_catalog_item_count
        == 2
    )

    one_product_results = [
        ScrapeResult(
            site="pharmonline",
            products=[_public_api_product("https://pharmonline.az/product/one")],
        )
    ]
    with pytest.raises(
        main_mod.PharmonlinePublicAPIReconciliationError,
        match="immutable recovery floor",
    ):
        main_mod._ensure_pharmonline_public_api_catalog_baseline(
            db_session,
            one_product_results,
            tenant_id=1,
            verified_identity_count=1,
            trusted_ddp_item_count=1,
            retired_ddp_item_count=1,
            reconciled_item_count=0,
            source_manifest_sha256=_RECOVERY_MANIFEST_SHA,
            catalog_fingerprint_sha256=main_mod._pharmonline_public_api_catalog_fingerprint(
                one_product_results
            ),
            source_transport="decodo",
            preflight_run_ref="123456",
        )


def test_public_api_identity_diagnostics_separate_stale_duplicates_from_new_ids(db_session):
    """The read-only preflight evidence must not turn legacy rows into trust."""
    exact_url = "https://pharmonline.az/product/exact-product"
    duplicate_url = "https://pharmonline.az/product/duplicate-product"
    missing_url = "https://pharmonline.az/product/missing-product"
    db_session.add_all(
        [
            _stored_product(url=exact_url),
            _stored_product(
                url=duplicate_url,
                external_id="6kHnwLLMpYXyebN8f",
            ),
            _stored_product(
                url=duplicate_url,
                external_id="legacy-duplicate",
                availability_source=None,
            ),
            _stored_product(
                url=missing_url,
                external_id="legacy-missing",
                availability_source=None,
            ),
            _stored_product(
                url="https://pharmonline.az/product/retired-product",
                external_id="qwertyuiopasdfghj",
            ),
        ]
    )
    db_session.commit()

    diagnostics = main_mod._diagnose_pharmonline_public_api_identities(
        db_session,
        [
            ScrapeResult(
                site="pharmonline",
                products=[
                    _public_api_product(exact_url),
                    _public_api_product(
                        duplicate_url,
                        external_id="6kHnwLLMpYXyebN8f",
                    ),
                    _public_api_product(
                        missing_url,
                        external_id="abcdefghijklmnopq",
                    ),
                ],
            )
        ],
        tenant_id=1,
    )

    assert diagnostics == {
        "api_ids": 3,
        "invalid_api": 0,
        "duplicate_api_ids": 0,
        "duplicate_api_urls": 0,
        "trusted_ids": 3,
        "invalid_trusted": 0,
        "duplicate_trusted_ids": 0,
        "missing_trusted_ids": 1,
        "retired_trusted_ids": 1,
        "trusted_coverage_per_thousand": 1000,
        "mismatched_urls": 0,
        "api_urls_exact_trusted_only": 1,
        "api_urls_exact_trusted_with_extra_rows": 1,
        "api_urls_without_stored_rows": 0,
        "api_urls_unique_nonexact_rows": 1,
        "api_urls_multiple_nonexact_rows": 0,
        "missing_id_existing_untrusted_row": 0,
        "missing_id_absent_from_existing": 1,
        "missing_id_no_url_row": 0,
        "missing_id_unique_url_row": 1,
        "missing_id_multiple_url_rows": 0,
        "missing_id_url_has_other_trusted_row": 0,
    }


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("/product/example/", "https://pharmonline.az/product/example"),
        (
            "https://www.pharmonline.az/product/example?lng=ru#reviews",
            "https://pharmonline.az/product/example",
        ),
        (
            "https://pharmonline.az/az/product/example?lng=az",
            "https://pharmonline.az/product/example",
        ),
        (
            "https://www.pharmonline.az/ru/product/na%C3%AFve%20item?lng=ru#reviews",
            "https://pharmonline.az/product/na%C3%AFve%20item",
        ),
        ("https://other.example/product/example", None),
        ("https://pharmonline.az/category/example", None),
    ],
)
def test_canonical_pharmonline_product_url(raw_url, expected):
    assert main_mod._canonical_pharmonline_product_url(raw_url) == expected
