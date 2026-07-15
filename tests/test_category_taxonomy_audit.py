from datetime import datetime

from scripts.audit_category_taxonomy import build_category_taxonomy_audit
from src.storage import Match, Product


def _product(db, match, site, category, external_id, *, tenant_id=1, url_dead_at=None):
    product = Product(
        tenant_id=tenant_id,
        site=site,
        external_id=external_id,
        url=f"https://{site}.example/{external_id}",
        name=external_id,
        name_normalized=external_id,
        category=category,
        canonical_id=match.id,
        url_dead_at=url_dead_at,
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


def test_audit_reports_a_clean_rule_set(db_session) -> None:
    """`--strict` gates on this: a defect here means order could decide."""
    assert build_category_taxonomy_audit(db_session)["policy_defects"] == []


def test_source_inventory_is_scoped_to_the_requested_tenant(db_session) -> None:
    """Coverage must not silently count another tenant's catalogue."""
    mine = Match(tenant_id=1, canonical_name="Mine", confidence=1.0)
    theirs = Match(tenant_id=2, canonical_name="Theirs", confidence=1.0)
    db_session.add_all([mine, theirs])
    db_session.flush()

    _product(db_session, mine, "pharmonline", "antihipertenziv-dermanlar", "t1-client")
    _product(db_session, mine, "aptekonline", "antihipertenziv", "t1-competitor")
    _product(db_session, theirs, "pharmonline", "quru-goz-sindromu", "t2-client", tenant_id=2)
    db_session.commit()

    report = build_category_taxonomy_audit(db_session, tenant_id=1)
    # Tenant 2's category must not appear in any inventory surface...
    inventoried = {
        (row["site"], row["category"])
        for entry in report["mapped_categories_by_rule"]
        for row in entry["sample"]
    }
    assert ("pharmonline", "quru-goz-sindromu") not in inventoried
    # ...nor inflate tenant 1's coverage.
    assert report["source_coverage"]["pharmonline"]["categories"] == 1
    assert report["source_coverage"]["pharmonline"]["products"] == 1


def test_source_inventory_excludes_dead_urls(db_session) -> None:
    """The matches half already drops dead products; coverage must agree."""
    match = Match(tenant_id=1, canonical_name="Dead", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    _product(db_session, match, "pharmonline", "antihipertenziv-dermanlar", "live")
    _product(
        db_session,
        match,
        "pharmonline",
        "antihipertenziv-dermanlar",
        "dead",
        url_dead_at=datetime(2026, 1, 1),
    )
    db_session.commit()

    report = build_category_taxonomy_audit(db_session, tenant_id=1)
    assert report["source_coverage"]["pharmonline"]["products"] == 1


def test_audit_surfaces_segment_policy_and_ambiguity_queues(db_session) -> None:
    match = Match(tenant_id=1, canonical_name="Kids", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    # Kids + oral care → resolved by the documented segment policy.
    _product(db_session, match, "pharmonline", "ushaq-uchun-aghiz-boshlughuna-qulluq", "kid")
    # Pregnancy + vitamins → no signal dominates → fails closed into the queue.
    _product(db_session, match, "pharmonline", "hamileler-uchun-vitamin-mineral-kompleks", "preg")
    db_session.commit()

    report = build_category_taxonomy_audit(db_session, tenant_id=1)
    policy = {row["category"] for row in report["segment_policy_source_categories"]}
    assert "ushaq-uchun-aghiz-boshlughuna-qulluq" in policy
    assert report["classification_reasons"]["segment_policy"] == 1

    ambiguous = {row["category"] for row in report["ambiguous_source_categories"]}
    assert "hamileler-uchun-vitamin-mineral-kompleks" in ambiguous
    assert report["classification_reasons"]["ambiguous"] == 1


def test_audit_groups_mapped_categories_by_rule_for_review(db_session) -> None:
    """A broad signal quietly owning many categories must be visible.

    `usaq` (child) matching `uşaqlıq` (uterus) survived a full production audit
    precisely because a confidently-wrong `matched` row landed in no queue.
    """
    match = Match(tenant_id=1, canonical_name="Kids", confidence=1.0)
    db_session.add(match)
    db_session.flush()
    _product(db_session, match, "pharmonline", "ushaq-bezleri", "a")
    _product(db_session, match, "pharmonline", "ushaq-qidasi", "b")
    db_session.commit()

    report = build_category_taxonomy_audit(db_session, tenant_id=1)
    by_rule = {e["rule_id"]: e for e in report["mapped_categories_by_rule"]}
    assert by_rule["mb.usaq"]["categories"] == 2
    assert {r["category"] for r in by_rule["mb.usaq"]["sample"]} == {
        "ushaq-bezleri",
        "ushaq-qidasi",
    }
