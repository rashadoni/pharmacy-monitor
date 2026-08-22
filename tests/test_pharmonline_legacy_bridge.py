"""Safety checks for the guarded Pharmonline HTML recovery bridge."""

from __future__ import annotations

import pytest

from src import main as main_mod
from src import storage
from src.scrapers.base import ScrapedProduct, ScrapeResult


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
        ("https://other.example/product/example", None),
        ("https://pharmonline.az/category/example", None),
    ],
)
def test_canonical_pharmonline_product_url(raw_url, expected):
    assert main_mod._canonical_pharmonline_product_url(raw_url) == expected
