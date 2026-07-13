#!/usr/bin/env python3
"""Restore exact match topology from MatchPolicyAudit records."""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import or_, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import matcher, storage
from src._time import utcnow


def _restore(session, audit: storage.MatchPolicyAudit) -> None:
    payload = audit.payload or {}
    before = payload.get("before") or {}
    match_data = before.get("match") or {}
    member_ids = before.get("members") or []
    after = payload.get("after") or {}
    policy_version = int(payload.get("policy_version") or 1)

    # Lock every row whose topology or rejection state will be validated and
    # changed.  The shared advisory lock prevents application-level rematch;
    # row locks close the preflight-to-write race for direct concurrent SQL.
    product_ids = {int(product_id) for product_id in member_ids}
    product_ids.update(
        int(product_id) for product_id in (after.get("assignments") or {})
    )
    match_ids = {
        int(match_id) for match_id in (after.get("match_ids") or [])
    }
    match_ids.update(int(match_id) for match_id in (after.get("matches") or {}))
    if match_data.get("id") is not None:
        match_ids.add(int(match_data["id"]))
    if product_ids or match_ids:
        predicates = []
        if product_ids:
            predicates.append(storage.Product.id.in_(product_ids))
        if match_ids:
            predicates.append(storage.Product.canonical_id.in_(match_ids))
        session.scalars(
            select(storage.Product).where(or_(*predicates)).with_for_update()
        ).all()
    if match_ids:
        session.scalars(
            select(storage.Match)
            .where(storage.Match.id.in_(match_ids))
            .with_for_update()
        ).all()
    rejection_ids = {
        int(state["id"]) for state in payload.get("rejections") or []
    }
    if rejection_ids:
        session.scalars(
            select(storage.MatchRejection)
            .where(storage.MatchRejection.id.in_(rejection_ids))
            .with_for_update()
        ).all()

    if policy_version >= 2:
        expected_assignments = {
            int(product_id): match_id
            for product_id, match_id in (after.get("assignments") or {}).items()
        }
        current_products = session.scalars(
            select(storage.Product).where(
                storage.Product.id.in_(expected_assignments)
            )
        ).all()
        current_by_id = {product.id: product for product in current_products}
        for product_id, expected_match_id in expected_assignments.items():
            product = current_by_id.get(product_id)
            if product is None or product.canonical_id != expected_match_id:
                raise RuntimeError(
                    f"rollback conflict: product {product_id} topology changed"
                )

        for match_id, expected in (after.get("matches") or {}).items():
            match = session.get(storage.Match, int(match_id))
            if match is None:
                raise RuntimeError(
                    f"rollback conflict: expected match {match_id} is missing"
                )
            current_members = sorted(
                session.scalars(
                    select(storage.Product.id).where(
                        storage.Product.canonical_id == int(match_id)
                    )
                ).all()
            )
            if current_members != sorted(expected.get("members") or []):
                raise RuntimeError(
                    f"rollback conflict: match {match_id} membership changed"
                )
            for field in (
                "canonical_name",
                "canonical_brand",
                "canonical_dosage",
                "canonical_pack_size",
                "confidence",
                "is_manual",
                "match_strategy",
                "needs_review",
            ):
                if getattr(match, field) != expected.get(field):
                    raise RuntimeError(
                        f"rollback conflict: match {match_id} field {field} changed"
                    )

        if not after.get("original_match_exists", False):
            original = session.get(storage.Match, match_data["id"])
            if original is not None:
                raise RuntimeError(
                    "rollback conflict: dissolved original match was recreated"
                )

        for rejection_state in payload.get("rejections") or []:
            rejection = session.get(
                storage.MatchRejection, int(rejection_state["id"])
            )
            expected = rejection_state.get("after") or {}
            if rejection is None:
                raise RuntimeError(
                    f"rollback conflict: rejection {rejection_state['id']} is missing"
                )
            for field in ("is_active", "reason", "reason_type", "metadata_json"):
                if getattr(rejection, field) != expected.get(field):
                    raise RuntimeError(
                        f"rollback conflict: rejection {rejection.id} changed"
                    )

    for match_id in after.get("match_ids") or []:
        if match_id == match_data.get("id"):
            continue
        created = session.get(storage.Match, match_id)
        if created:
            for product in list(created.products):
                product.canonical_id = None
            session.delete(created)
    session.flush()

    match = session.get(storage.Match, match_data["id"])
    if match is None:
        match = storage.Match(id=match_data["id"])
        session.add(match)
    for field in (
        "tenant_id",
        "canonical_name",
        "canonical_brand",
        "canonical_dosage",
        "canonical_pack_size",
        "confidence",
        "is_manual",
        "match_strategy",
        "needs_review",
    ):
        setattr(match, field, match_data.get(field))
    session.flush()
    products = session.scalars(
        select(storage.Product).where(storage.Product.id.in_(member_ids))
    ).all()
    for product in products:
        product.canonical_id = match.id

    if policy_version >= 2:
        for rejection_state in payload.get("rejections") or []:
            rejection = session.get(
                storage.MatchRejection, int(rejection_state["id"])
            )
            previous = rejection_state.get("before")
            if rejection is None:
                continue
            if previous is None:
                session.delete(rejection)
                continue
            rejection.is_active = previous.get("is_active", False)
            rejection.reason = previous.get("reason")
            rejection.reason_type = previous.get("reason_type", "manual")
            rejection.metadata_json = previous.get("metadata_json")
            rejection.resolved_at = None if rejection.is_active else utcnow()
            rejection.updated_at = utcnow()
    else:
        # Backward compatibility for pre-v2 audits which did not record exact
        # rejection IDs.  New audits never take this broad path.
        source_match_id = match.id
        rejections = session.scalars(
            select(storage.MatchRejection).where(
                storage.MatchRejection.reason_type.in_(("system_country", "system_spec")),
                storage.MatchRejection.is_active.is_(True),
            )
        ).all()
        for rejection in rejections:
            if (rejection.metadata_json or {}).get("source_match_id") == source_match_id:
                rejection.is_active = False
                rejection.resolved_at = utcnow()
                rejection.updated_at = utcnow()
    audit.rolled_back_at = utcnow()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-id", type=int, action="append")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.all and not args.audit_id:
        parser.error("use --audit-id ID or --all")

    Session = storage.make_session()
    with Session() as session:
        lock_taken = matcher.acquire_match_mutation_lock(session, wait=True)
        try:
            query = (
                select(storage.MatchPolicyAudit)
                .where(storage.MatchPolicyAudit.rolled_back_at.is_(None))
                .with_for_update()
            )
            if not args.all:
                query = query.where(storage.MatchPolicyAudit.id.in_(args.audit_id))
            audits = list(
                session.scalars(
                    query.order_by(storage.MatchPolicyAudit.id.desc())
                ).all()
            )
            for audit in audits:
                _restore(session, audit)
            print(f"{'restored' if args.apply else 'would_restore'}={len(audits)}")
            if args.apply:
                session.commit()
            else:
                session.rollback()
        finally:
            if lock_taken:
                matcher.release_match_mutation_lock(session)


if __name__ == "__main__":
    main()
