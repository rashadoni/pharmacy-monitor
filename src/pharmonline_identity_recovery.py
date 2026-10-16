"""Explicit v1 retirement of admitted Pharmonline identities.

This is separation, never continuity: historical rows and their admission stay
on the old Product. A distinct Product starts empty. Only a manual, SHA-bound
Decodo plan may create the append-only ledger that authenticates it. There is
no automatic recovery, chained retirement, or generic native-ID overwrite.
"""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from sqlalchemy import inspect, or_, select

from src import storage
from src._time import utcnow

PROOF_VERSION = "admitted_identity_retirement_v1"
AVAILABILITY_SOURCE = "pharmonline_identity_retirement"
URL_CHANGED = "native_url_changed"
ID_REPLACED = "native_id_replaced"
TABLE = storage.PharmonlinePublicAPIIdentityRetirement.__tablename__


def available(session) -> bool:
    return TABLE in inspect(session.connection()).get_table_names()


def archived_id(product_id: int, external_id: str) -> str:
    digest = hashlib.sha256(f"{PROOF_VERSION}:{product_id}:{external_id}".encode()).hexdigest()[:20]
    return f"pharmonline-retired-{product_id}-{digest}"


def admission_digest(admission) -> str:
    # All persisted evidence, including the original timestamp, is bound to
    # the reviewed plan. Never print/serialize the underlying identity pairs.
    payload = {
        column.name: getattr(admission, column.name)
        for column in storage.PharmonlinePublicAPIIdentityAdmission.__table__.columns
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def prior_admission(session, product, *, projection=None, allowed_retirement_id=None):
    """Exactly one valid original admission; every other audit role blocks v1."""
    from src import main as m

    admission_rows = session.scalars(
        select(storage.PharmonlinePublicAPIIdentityAdmission)
        .where(storage.PharmonlinePublicAPIIdentityAdmission.product_id == product.id)
        .execution_options(populate_existing=True)
    ).all()
    if len(admission_rows) != 1:
        return None
    admission = admission_rows[0]
    if m._pharmonline_public_api_admission_invalid_reason(admission, projection or product):
        return None
    if admission.admission_kind == "quarantined_public_product":
        return None
    if (
        session.scalar(
            select(storage.PharmonlinePublicAPIIdentityReconciliation.id)
            .where(storage.PharmonlinePublicAPIIdentityReconciliation.product_id == product.id)
            .limit(1)
        )
        is not None
    ):
        return None
    for model in (
        storage.PharmonlinePublicAPIIdentityQuarantine,
        storage.PharmonlinePublicAPIIdentityRetirement,
    ):
        if model is storage.PharmonlinePublicAPIIdentityRetirement and not available(session):
            continue
        query = select(model.id).where(
            or_(model.legacy_product_id == product.id, model.replacement_product_id == product.id)
        )
        if model is storage.PharmonlinePublicAPIIdentityRetirement and allowed_retirement_id:
            query = query.where(model.id != allowed_retirement_id)
        if session.scalar(query.limit(1)) is not None:
            return None
    return admission


def candidate(session, legacy, public, api_records, all_products, *, tenant_id, kind):
    """Return a narrow, fully bound action, or leave the normal guard in place."""
    from src import main as m

    if not available(session) or legacy.tenant_id != tenant_id or legacy.site != "pharmonline":
        return None
    old_id, new_id = str(legacy.external_id), str(public.external_id)
    old_url = m._canonical_pharmonline_product_url(legacy.url)
    new_url = m._canonical_pharmonline_product_url(public.url)
    name = m._pharmonline_product_name_signature(legacy.name)
    if (
        not name
        or name != m._pharmonline_product_name_signature(public.name)
        or not old_url
        or not new_url
        or not m._PHARMONLINE_METEOR_ID_RE.fullmatch(old_id)
    ):
        return None
    if kind == URL_CHANGED:
        if old_id != new_id or old_url == new_url:
            return None
    elif kind == ID_REPLACED:
        if old_id == new_id or old_url != new_url or old_id in api_records:
            return None
    else:
        return None
    # Both sides must have one global owner. No cross-tenant borrowing, URL
    # shadows, swaps, or second retirement is inferred from a shared name.
    for row in all_products:
        if row.id == legacy.id:
            continue
        if str(row.external_id) in {
            old_id,
            new_id,
            archived_id(legacy.id, old_id),
        } or m._canonical_pharmonline_product_url(row.url) in {old_url, new_url}:
            return None
    admission = prior_admission(session, legacy)
    if admission is None:
        return None
    if (
        session.scalar(
            select(storage.PharmonlinePublicAPIIdentityRetirement.id)
            .where(storage.PharmonlinePublicAPIIdentityRetirement.public_api_external_id == new_id)
            .limit(1)
        )
        is not None
    ):
        return None
    # A target ledger on another Product cannot be silently shadowed.
    for model in (
        storage.PharmonlinePublicAPIIdentityAdmission,
        storage.PharmonlinePublicAPIIdentityReconciliation,
    ):
        if (
            session.scalar(
                select(model.id)
                .where(model.public_api_external_id == new_id, model.product_id != legacy.id)
                .limit(1)
            )
            is not None
        ):
            return None
    return m._PharmonlinePublicAPIIdentityQuarantineAction(
        legacy_product_id=legacy.id,
        legacy_external_id=old_id,
        legacy_canonical_url=old_url,
        public_api_external_id=new_id,
        public_api_canonical_url=new_url,
        public_product=public,
        retirement_kind=kind,
        prior_admission_sha256=admission_digest(admission),
    )


def valid_state(session, products, *, tenant_id):
    """Return retired IDs, trusted replacements and conflict count; fail closed."""
    from src import main as m

    by_id = {p.id: p for p in products}
    marked = {
        p.id
        for p in products
        if p.availability_source == AVAILABILITY_SOURCE
        or str(p.external_id).startswith("pharmonline-retired-")
    }
    if not available(session):
        return set(), {}, len(marked)
    records = session.scalars(
        select(storage.PharmonlinePublicAPIIdentityRetirement).execution_options(
            populate_existing=True
        )
    ).all()
    retired, replacements, conflicts = set(), {}, 0
    for record in records:
        if (
            record.tenant_id != tenant_id
            and record.legacy_product_id not in by_id
            and record.replacement_product_id not in by_id
        ):
            continue
        old, new = by_id.get(record.legacy_product_id), by_id.get(record.replacement_product_id)
        old_url = m._canonical_pharmonline_product_url(record.legacy_canonical_url)
        new_url = m._canonical_pharmonline_product_url(record.public_api_canonical_url)
        old_id, new_id = str(record.legacy_external_id), str(record.public_api_external_id)
        shape = (
            record.retirement_kind == URL_CHANGED and old_id == new_id and old_url != new_url
        ) or (record.retirement_kind == ID_REPLACED and old_id != new_id and old_url == new_url)
        valid = bool(
            old
            and new
            and old.id != new.id
            and shape
            and old_url
            and new_url
            and m._PHARMONLINE_METEOR_ID_RE.fullmatch(old_id)
            and m._PHARMONLINE_METEOR_ID_RE.fullmatch(new_id)
            and record.proof_version == PROOF_VERSION
            and record.source_transport == "decodo"
            and all(
                m._PHARMONLINE_PUBLIC_API_PROOF_SHA_RE.fullmatch(value or "")
                for value in (
                    record.source_manifest_sha256,
                    record.catalog_fingerprint_sha256,
                    record.prior_admission_sha256,
                )
            )
            and re.fullmatch(r"[0-9]{1,20}", str(record.preflight_run_ref))
            and old.tenant_id == new.tenant_id == record.tenant_id == tenant_id
            and old.site == new.site == "pharmonline"
            and old.external_id == record.archived_external_id == archived_id(old.id, old_id)
            and m._canonical_pharmonline_product_url(old.url) == old_url
            and old.availability_source == AVAILABILITY_SOURCE
            and old.offer_availability_status == "unknown"
            and old.offer_quantity is None
            and old.availability_run_id is None
            and old.availability_observed_at is not None
            and new.external_id == new_id
            and m._canonical_pharmonline_product_url(new.url) == new_url
        )
        admission = None
        if valid:
            projection = SimpleNamespace(
                tenant_id=old.tenant_id, site=old.site, external_id=old_id, url=old_url
            )
            admission = prior_admission(
                session, old, projection=projection, allowed_retirement_id=record.id
            )
            valid = bool(
                admission
                and admission.id == record.prior_admission_id
                and admission_digest(admission) == record.prior_admission_sha256
            )
        if valid:
            # Replacement has only this recovery proof. No independent audit
            # or second graph edge may turn a split into ambiguous continuity.
            for model in (
                storage.PharmonlinePublicAPIIdentityAdmission,
                storage.PharmonlinePublicAPIIdentityReconciliation,
            ):
                if session.scalar(select(model.id).where(model.product_id == new.id).limit(1)):
                    valid = False
            for model in (
                storage.PharmonlinePublicAPIIdentityQuarantine,
                storage.PharmonlinePublicAPIIdentityRetirement,
            ):
                query = select(model.id).where(
                    or_(model.legacy_product_id == new.id, model.replacement_product_id == new.id)
                )
                if model is storage.PharmonlinePublicAPIIdentityRetirement:
                    query = query.where(model.id != record.id)
                if session.scalar(query.limit(1)):
                    valid = False
        if not valid:
            conflicts += 1
            continue
        retired.add(old.id)
        replacements[new_id] = new
    conflicts += len(marked - retired)
    return retired, replacements, conflicts


def apply_retirements(
    session,
    actions,
    results,
    *,
    tenant_id,
    source_manifest_sha256,
    catalog_fingerprint_sha256,
    source_transport,
    preflight_run_ref,
):
    """Apply already reviewed actions inside the caller's SERIALIZABLE transaction."""
    from src import main as m

    api_records = m._public_api_identity_records(results)
    for action in actions:
        all_products = session.scalars(
            select(storage.Product)
            .where(storage.Product.site == "pharmonline")
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        legacy = next((p for p in all_products if p.id == action.legacy_product_id), None)
        fresh = (
            candidate(
                session,
                legacy,
                action.public_product,
                api_records,
                all_products,
                tenant_id=tenant_id,
                kind=action.retirement_kind,
            )
            if legacy
            else None
        )
        if fresh != action:
            raise m.PharmonlinePublicAPIReconciliationError("identity retirement evidence changed")
        admission = prior_admission(session, legacy)
        archive = archived_id(legacy.id, action.legacy_external_id)
        legacy.external_id = archive
        legacy.offer_availability_status = "unknown"
        legacy.offer_quantity = None
        legacy.availability_source = AVAILABILITY_SOURCE
        legacy.availability_observed_at = utcnow()
        legacy.availability_run_id = None
        session.flush()
        replacement = m._new_pharmonline_public_api_identity_product(
            action.public_product,
            tenant_id=tenant_id,
            canonical_url=action.public_api_canonical_url,
        )
        session.add(replacement)
        session.flush()
        session.add(
            storage.PharmonlinePublicAPIIdentityRetirement(
                tenant_id=tenant_id,
                legacy_product_id=legacy.id,
                replacement_product_id=replacement.id,
                prior_admission_id=admission.id,
                prior_admission_sha256=action.prior_admission_sha256,
                retirement_kind=action.retirement_kind,
                legacy_external_id=action.legacy_external_id,
                archived_external_id=archive,
                legacy_canonical_url=action.legacy_canonical_url,
                public_api_external_id=action.public_api_external_id,
                public_api_canonical_url=action.public_api_canonical_url,
                proof_version=PROOF_VERSION,
                source_manifest_sha256=source_manifest_sha256,
                catalog_fingerprint_sha256=catalog_fingerprint_sha256,
                source_transport=source_transport,
                preflight_run_ref=preflight_run_ref,
            )
        )
        session.flush()
