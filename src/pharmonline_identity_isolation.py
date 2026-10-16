"""Bounded source-identity conflicts cannot stop unrelated catalog updates.

No identity is rebound here. Historic Products and their prices remain intact;
conflicting source pairs are evidence, never offers. The source completeness
gate runs before this transaction; immutable audit and baseline gates still run
against the original catalog before any conflict/admission is committed.
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select, text

from src import storage
from src._time import utcnow

MAX_ISOLATED = 25
MAX_ISOLATED_FRACTION = 0.01


def lock_identity_writes(session):
    """Serialize conflict decisions with every Pharmonline persistence chunk."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(721109, 1)"))


def require_native_identity_continuity(session, products, *, tenant_id):
    """A partial producer cannot change a native binding before a full scan.

    It has no completeness evidence to create a conflict case. Refuse the
    write instead; the next complete public catalog will classify it. Invalid
    historic /True URLs retain their established repair path.
    """
    from src import main as core

    incoming, incoming_urls = {}, {}
    for product in products:
        if not core._PHARMONLINE_METEOR_ID_RE.fullmatch(str(product.external_id)):
            continue
        canonical = core._canonical_pharmonline_product_url(product.url)
        if (
            canonical is None
            or (product.external_id in incoming and incoming[product.external_id] != canonical)
            or (canonical in incoming_urls and incoming_urls[canonical] != product.external_id)
        ):
            raise core.PharmonlinePublicAPIIdentityError(
                "ambiguous native identity in persistence chunk"
            )
        incoming[product.external_id] = canonical
        incoming_urls[canonical] = product.external_id
    if not incoming:
        return
    by_id, by_url = {}, {}
    for external_id, url, owner in session.execute(
        select(
            storage.Product.external_id,
            storage.Product.url,
            storage.Product.tenant_id,
        ).where(storage.Product.site == "pharmonline")
    ):
        if not core._PHARMONLINE_METEOR_ID_RE.fullmatch(str(external_id)):
            continue
        canonical = core._canonical_pharmonline_product_url(url)
        by_id[external_id] = (canonical, owner)
        if canonical:
            by_url.setdefault(canonical, set()).add((external_id, owner))
    for external_id, url in incoming.items():
        existing = by_id.get(external_id)
        if existing and (
            existing[1] != tenant_id or (existing[0] is not None and existing[0] != url)
        ):
            raise core.PharmonlinePublicAPIIdentityError(
                "native identity change requires a complete public catalog before persistence"
            )
        if any(
            other_id != external_id or owner != tenant_id
            for other_id, owner in by_url.get(url, set())
        ):
            raise core.PharmonlinePublicAPIIdentityError(
                "native source URL is already bound; complete public catalog required"
            )


def _classify(session, results, tenant_id):
    from src import main as core

    records = core._public_api_identity_records(results)
    products = list(
        session.scalars(
            select(storage.Product)
            .where(storage.Product.site == "pharmonline")
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    by_product = {product.id: product for product in products}
    tenant_products = [product for product in products if product.tenant_id == tenant_id]
    retired, _, _ = core.identity_recovery.valid_state(
        session, tenant_products, tenant_id=tenant_id
    )
    quarantined, _, _ = core._valid_pharmonline_public_api_identity_quarantines(
        session, tenant_products, tenant_id=tenant_id
    )
    _, history_ids, _, _ = core._trusted_ddp_products_by_id(
        session,
        tenant_products,
        tenant_id=tenant_id,
        quarantined_product_ids=retired | quarantined,
    )
    by_id, by_url = {}, {}
    for product in products:
        if product.id in retired | quarantined:
            continue
        by_id.setdefault(product.external_id, []).append(product)
        url = core._canonical_pharmonline_product_url(product.url)
        if url:
            by_url.setdefault(url, []).append(product)

    cases = list(session.scalars(select(storage.PharmonlineIdentityConflict)))
    cases_by_id = {case.id: case for case in cases}
    pending_by_product = {case.product_id: case for case in cases}
    variants_by_id, variants_by_url = {}, {}
    for case in cases:
        product = by_product.get(case.product_id)
        if (
            product is None
            or product.tenant_id != case.tenant_id
            or not product.identity_conflicted
        ):
            raise core.PharmonlinePublicAPIIdentityError("identity conflict ledger is inconsistent")
    if any(p.identity_conflicted and p.id not in pending_by_product for p in products):
        raise core.PharmonlinePublicAPIIdentityError("identity conflict evidence is missing")
    for variant in session.scalars(select(storage.PharmonlineIdentityConflictVariant)):
        case = cases_by_id.get(variant.conflict_id)
        if case is None:
            raise core.PharmonlinePublicAPIIdentityError("identity variant has no conflict case")
        variants_by_id.setdefault(variant.external_id, set()).add(case.product_id)
        variants_by_url.setdefault(variant.canonical_url, set()).add(case.product_id)

    associations, isolated = {}, set()
    for external_id, (url, _) in records.items():
        id_rows, url_rows = by_id.get(external_id, []), by_url.get(url, [])
        pending = variants_by_id.get(external_id, set()) | variants_by_url.get(url, set())
        related = {p.id for p in id_rows + url_rows} | pending
        if any(by_product[pid].tenant_id != tenant_id for pid in related):
            raise core.PharmonlinePublicAPIIdentityError("cross-tenant source identity collision")
        if len(id_rows) > 1:
            raise core.PharmonlinePublicAPIIdentityError("duplicate source identity in storage")
        current = id_rows[0] if id_rows else None
        # Non-native URL shadows are historical; the original verifier checks
        # their lineage. A second native/trusted identity is always isolated.
        competing = [
            p
            for p in url_rows
            if p is not current
            and (
                core._PHARMONLINE_METEOR_ID_RE.fullmatch(str(p.external_id))
                or p.id in history_ids
                or p.identity_conflicted
            )
        ]
        conflict = bool(pending) or any(by_product[pid].identity_conflicted for pid in related)
        conflict |= bool(current and core._canonical_pharmonline_product_url(current.url) != url)
        conflict |= bool(current and competing) or bool(not current and url_rows)
        associations[external_id] = related
        if conflict:
            isolated.add(external_id)

    # If two current pairs claim one historical row, neither pair can escape
    # isolation by having matched the row before the other pair was examined.
    affected = set()
    while True:
        affected.update(pid for ext in isolated for pid in associations[ext])
        expanded = {ext for ext, ids in associations.items() if ids & affected}
        if expanded <= isolated:
            break
        isolated.update(expanded)
    existing_pending = {c.product_id for c in cases if c.tenant_id == tenant_id}
    if (
        len(isolated) > MAX_ISOLATED
        or len(isolated) > len(records) * MAX_ISOLATED_FRACTION
        or len(existing_pending | affected) > MAX_ISOLATED
    ):
        raise core.PharmonlinePublicAPIIdentityError("identity drift exceeds isolation limit")
    trusted_results = [
        replace(result, products=[p for p in result.products if p.external_id not in isolated])
        for result in results
    ]
    return trusted_results, frozenset(isolated), associations, by_product, pending_by_product


def prepare_catalog(session, results, *, tenant_id, run_id, source_transport):
    """Atomic admissions + pending evidence, followed by trusted-only persistence.

    Caller must have checked full API/sitemap coverage and run quality first.
    Serialization failures are failures, not retries with weaker evidence.
    """
    from src import main as core

    from src.scrapers.pharmonline_public_api import PUBLIC_CATALOG_ROUTE

    complete, reason = core._verify_full_catalog_results(
        results,
        sites=["pharmonline"],
        expected_slugs={"pharmonline": [PUBLIC_CATALOG_ROUTE]},
        baselines={},
    )
    if not complete or any(result.site_fatal for result in results):
        raise core.PharmonlinePublicAPIIdentityError(f"source catalog incomplete: {reason}")
    records = core._public_api_identity_records(results)
    source = results[0]
    if source.category_counts.get(PUBLIC_CATALOG_ROUTE) != len(records) or source.route_statuses[
        PUBLIC_CATALOG_ROUTE
    ].parsed_items != len(records):
        raise core.PharmonlinePublicAPIIdentityError(
            "source catalog item count differs from coverage"
        )
    session.commit()
    try:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        lock_identity_writes(session)
        trusted, isolated, associations, products, cases = _classify(session, results, tenant_id)
        admission, admitted = core._admit_pharmonline_public_api_scheduled_identities(
            session,
            trusted,
            tenant_id=tenant_id,
            run_id=run_id,
            source_transport=source_transport,
            own_transaction=False,
            verification_results=results,
            isolated_external_ids=isolated,
        )
        try:
            core._verify_pharmonline_public_api_identities(
                session,
                results,
                tenant_id=tenant_id,
                isolated_external_ids=isolated,
            )
        except core.PharmonlinePublicAPIIdentityError as exc:
            if admission["status"] == "refused":
                raise core.PharmonlinePublicAPIIdentityError(
                    f"{exc}; scheduled_admission=refused({admission['reason']})"
                ) from exc
            raise
        fingerprint = core._pharmonline_public_api_catalog_fingerprint(results)
        records = core._public_api_identity_records(results)
        variants = {
            (v.conflict_id, v.external_id, v.canonical_url): v
            for v in session.scalars(select(storage.PharmonlineIdentityConflictVariant))
        }
        for external_id in sorted(isolated):
            url = records[external_id][0]
            for product_id in sorted(associations[external_id]):
                product = products[product_id]
                case = cases.get(product_id)
                if case is None:
                    case = storage.PharmonlineIdentityConflict(
                        tenant_id=tenant_id,
                        product_id=product_id,
                        first_run_id=run_id,
                        last_run_id=run_id,
                    )
                    session.add(case)
                    session.flush()
                    cases[product_id] = case
                case.last_run_id = run_id
                case.last_seen_at = utcnow()
                product.identity_conflicted = True
                key = (case.id, external_id, url)
                variant = variants.get(key)
                if variant is None:
                    variant = storage.PharmonlineIdentityConflictVariant(
                        conflict_id=case.id,
                        external_id=external_id,
                        canonical_url=url,
                        first_run_id=run_id,
                        last_run_id=run_id,
                        catalog_fingerprint_sha256=fingerprint,
                    )
                    session.add(variant)
                    variants[key] = variant
                else:
                    variant.last_run_id = run_id
        session.commit()
    except BaseException:
        session.rollback()
        raise
    partition = None
    if isolated:
        partition = {
            "version": 1,
            "site": "pharmonline",
            "source_complete": True,
            "total_count": len(records),
            "trusted_count": len(records) - len(isolated),
            "isolated_count": len(isolated),
            "excluded_product_count": len({pid for ext in isolated for pid in associations[ext]}),
        }
    return trusted, partition, admission, admitted


def exclude_pending_products(session, products):
    """Also protect partial/watchlist producers from pending pairs and variants."""
    from src.main import _canonical_pharmonline_product_url

    blocked_ids, blocked_urls = set(), set()
    for external_id, url in session.execute(
        select(storage.Product.external_id, storage.Product.url).where(
            storage.Product.site == "pharmonline", storage.Product.identity_conflicted.is_(True)
        )
    ):
        blocked_ids.add(external_id)
        blocked_urls.add(_canonical_pharmonline_product_url(url))
    for external_id, url in session.execute(
        select(
            storage.PharmonlineIdentityConflictVariant.external_id,
            storage.PharmonlineIdentityConflictVariant.canonical_url,
        )
    ):
        blocked_ids.add(external_id)
        blocked_urls.add(url)
    return [
        p
        for p in products
        if p.external_id not in blocked_ids
        and _canonical_pharmonline_product_url(p.url) not in blocked_urls
    ]
