"""Safety checks for the guarded Pharmonline HTML recovery bridge."""

from __future__ import annotations

import pytest

from src import main as main_mod
from src import storage
from src.scrapers.base import ScrapedProduct, ScrapeResult
from src.scrapers.pharmonline_public_api import PUBLIC_API_AVAILABILITY_SOURCE


_METEOR_ID = "xwJspdCx3iFBDqDWF"


def _stored_product(
    *,
    tenant_id: int = 1,
    url: str,
    external_id: str = _METEOR_ID,
    availability_source: str | None = "pharmonline_ddp_total_count",
):
    return storage.Product(
        tenant_id=tenant_id,
        site="pharmonline",
        external_id=external_id,
        url=url,
        name="Existing Pharmonline product",
        name_normalized="existing pharmonline product",
        availability_source=availability_source,
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


def _public_api_product(url: str, external_id: str = _METEOR_ID) -> ScrapedProduct:
    return ScrapedProduct(
        site="pharmonline",
        external_id=external_id,
        url=url,
        name="Public API product",
        identity_verified=True,
        availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
    )


def test_public_api_identity_proof_allows_only_retired_trusted_ddp_rows(db_session, monkeypatch):
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
    monkeypatch.setattr(main_mod, "_PHARMONLINE_PUBLIC_API_MIN_TRUSTED_COVERAGE", 0.5)

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


def test_public_api_identity_proof_rejects_coverage_drop_below_threshold(db_session):
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

    with pytest.raises(
        main_mod.PharmonlinePublicAPIIdentityError,
        match="trusted_coverage=0.5000",
    ):
        main_mod._verify_pharmonline_public_api_identities(
            db_session,
            [ScrapeResult(site="pharmonline", products=[_public_api_product(current_url)])],
            tenant_id=1,
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
