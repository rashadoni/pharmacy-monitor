from sqlalchemy import select
import pytest

from scripts.rollback_match_policy import _restore
from src import matcher, storage


def _product(session, site: str, external_id: str, country: str) -> storage.Product:
    product = storage.Product(
        site=site,
        external_id=external_id,
        url=f"https://{site}/{external_id}",
        name="Ornafer N30",
        name_normalized="ornafer n30",
        manufacturer_country_code=country,
        country_resolution_status="resolved",
    )
    session.add(product)
    return product


def test_country_repartition_can_restore_exact_topology(db_session) -> None:
    match = storage.Match(canonical_name="Ornafer", confidence=0.95, is_manual=False)
    db_session.add(match)
    db_session.flush()
    original_id = match.id
    products = [
        _product(db_session, "pharmonline", "ua-ph", "ua"),
        _product(db_session, "aloe", "ua-aloe", "ua"),
        _product(db_session, "pharmonline", "rs-ph", "rs"),
        _product(db_session, "aptekonline", "rs-aptek", "rs"),
    ]
    db_session.flush()
    product_ids = [product.id for product in products]
    for product in products:
        product.canonical_id = original_id
    db_session.commit()

    matcher.revalidate_split(db_session)
    audit = db_session.scalar(select(storage.MatchPolicyAudit))
    assert audit is not None
    assert len(set(audit.payload["after"]["match_ids"])) == 2

    _restore(db_session, audit)
    db_session.commit()
    db_session.expire_all()

    restored = db_session.scalars(
        select(storage.Product).where(storage.Product.id.in_(product_ids))
    ).all()
    assert {product.canonical_id for product in restored} == {original_id}
    assert db_session.get(storage.Match, original_id).is_manual is False
    assert audit.rolled_back_at is not None
    for match_id in audit.payload["after"]["match_ids"]:
        if match_id != original_id:
            assert db_session.get(storage.Match, match_id) is None


def test_rollback_stops_if_product_was_manually_rematched(db_session) -> None:
    match = storage.Match(canonical_name="Ornafer", confidence=0.95, is_manual=False)
    db_session.add(match)
    db_session.flush()
    left = _product(db_session, "pharmonline", "ua", "ua")
    right = _product(db_session, "aloe", "rs", "rs")
    db_session.flush()
    left.canonical_id = match.id
    right.canonical_id = match.id
    db_session.commit()

    matcher.revalidate_split(db_session)
    audit = db_session.scalar(select(storage.MatchPolicyAudit))
    manual_target = storage.Match(
        canonical_name="Operator rematch", confidence=1.0, is_manual=True
    )
    db_session.add(manual_target)
    db_session.flush()
    right.canonical_id = manual_target.id
    db_session.commit()

    with pytest.raises(RuntimeError, match="topology changed"):
        _restore(db_session, audit)

    assert audit.rolled_back_at is None


def test_v2_rollback_restores_only_exact_rejection_state(db_session) -> None:
    match = storage.Match(canonical_name="Ornafer", confidence=0.95, is_manual=False)
    db_session.add(match)
    db_session.flush()
    left = _product(db_session, "pharmonline", "ua", "ua")
    right = _product(db_session, "aloe", "rs", "rs")
    unrelated = _product(db_session, "aptekonline", "other", "gb")
    db_session.flush()
    left.canonical_id = match.id
    right.canonical_id = match.id
    previous = storage.MatchRejection(
        product_a_id=min(left.id, right.id),
        product_b_id=max(left.id, right.id),
        reason="operator-note",
        reason_type="manual",
        metadata_json={"ticket": "C-17"},
        is_active=False,
    )
    unrelated_rejection = storage.MatchRejection(
        product_a_id=min(left.id, unrelated.id),
        product_b_id=max(left.id, unrelated.id),
        reason="keep-me",
        reason_type="system_country",
        metadata_json={"source_match_id": match.id},
        is_active=True,
    )
    db_session.add_all([previous, unrelated_rejection])
    db_session.commit()

    matcher.revalidate_split(db_session)
    audit = db_session.scalar(select(storage.MatchPolicyAudit))
    assert audit.payload["policy_version"] == 2
    assert previous.is_active is True

    _restore(db_session, audit)
    db_session.commit()
    db_session.refresh(previous)
    db_session.refresh(unrelated_rejection)

    assert previous.is_active is False
    assert previous.reason == "operator-note"
    assert previous.reason_type == "manual"
    assert previous.metadata_json == {"ticket": "C-17"}
    assert unrelated_rejection.is_active is True
    assert unrelated_rejection.reason == "keep-me"
