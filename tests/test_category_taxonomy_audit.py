from scripts.audit_category_taxonomy import build_category_taxonomy_audit
from src.storage import Match, Product


def _product(db, match, site, category, external_id):
    product = Product(
        tenant_id=1,
        site=site,
        external_id=external_id,
        url=f"https://{site}.example/{external_id}",
        name=external_id,
        name_normalized=external_id,
        category=category,
        canonical_id=match.id,
    )
    db.add(product)
    return product


def test_audit_separates_aligned_conflicting_and_unverifiable_matches(db_session) -> None:
    aligned = Match(tenant_id=1, canonical_name="Aligned", confidence=1.0)
    conflicting = Match(tenant_id=1, canonical_name="Conflict", confidence=1.0)
    unverifiable = Match(tenant_id=1, canonical_name="Broad aloe", confidence=1.0)
    db_session.add_all([aligned, conflicting, unverifiable])
    db_session.flush()

    _product(db_session, aligned, "pharmonline", "antihipertenziv-dermanlar", "a-client")
    _product(db_session, aligned, "aptekonline", "antihipertenziv", "a-competitor")
    _product(db_session, conflicting, "pharmonline", "antihipertenziv-dermanlar", "c-client")
    _product(db_session, conflicting, "aptekonline", "goz-xestelikleri", "c-competitor")
    _product(db_session, unverifiable, "pharmonline", "antihipertenziv-dermanlar", "u-client")
    _product(db_session, unverifiable, "aloe", "dermanlar", "u-competitor")
    db_session.commit()

    # Test-only slugs are descriptive, so no Category rows are required.
    report = build_category_taxonomy_audit(db_session)

    assert report["summary"]["canonical_categories"] == 1
    row = report["categories"][0]
    assert row["key"] == "cardiovascular_blood"
    assert row["matched_skus"] == 3
    assert row["aligned_skus"] == 1
    assert row["conflicting_skus"] == 1
    assert row["unverifiable_skus"] == 1
    assert row["alignment_pct"] == 50.0
