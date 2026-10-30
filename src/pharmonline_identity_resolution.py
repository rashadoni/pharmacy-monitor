"""Reviewed closure of pending Pharmonline identity isolation cases.

Isolation (``pharmonline_identity_isolation``) keeps a disputed card out of
prices and pairs, and by design no scheduled scrape ever closes a case. This
module is the only way to close one, and only inside the manual Decodo
reconciliation: two identical complete catalogs, a read-only plan the owner
approves by its manifest hash, a backup, then one SERIALIZABLE apply.

Every decision is separation, never continuity: history stays on the Product
it was recorded for. Three decisions, each bound to the exact case state:

* ``retired`` — the case Product is separated by the existing retirement v1
  ledger (same native ID at a new address whose old address left the catalog,
  or a new native ID at the same address). A new Product starts empty.
* ``shadow_archived`` — a historic row with a native-looking ID that no run
  ever observed, with no audit lineage, which only shares the address of the
  trusted Product the source still names exactly. It is archived like a
  retired row and keeps its history.
* ``current_confirmed`` — that trusted Product's own case closes together with
  the shadow: its stored pair is the current pair and nothing competes for it.

A decision holds only while the case's ``last_run_id`` and variant digest are
the ones it names. A later isolation of the same Product advances the case and
reopens it; the old decision stays as history. Variants are append-only and v1
closes only a case with exactly one source pair, so a reopened case stays open
until a later decision version handles several pairs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy import inspect, or_, select

from src import storage
from src._time import utcnow

PROOF_VERSION = "identity_conflict_resolution_v1"
RETIRED = "retired"
SHADOW = "shadow_archived"
CONFIRMED = "current_confirmed"
DECISIONS = frozenset({RETIRED, SHADOW, CONFIRMED})
# ``retirement_kind`` of the reconciliation plan action that archives a shadow.
SHADOW_KIND = "isolated_url_shadow"
TABLE = storage.PharmonlineIdentityConflictResolution.__table__.name

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_Resolution = storage.PharmonlineIdentityConflictResolution
_Case = storage.PharmonlineIdentityConflict
_Variant = storage.PharmonlineIdentityConflictVariant


def available(session) -> bool:
    tables = set(inspect(session.connection()).get_table_names())
    return {TABLE, _Case.__table__.name, _Variant.__table__.name}.issubset(tables)


def variant_digest(pairs) -> str:
    payload = "\n".join(f"{external_id}\t{url}" for external_id, url in sorted(pairs))
    return hashlib.sha256(f"identity_conflict_variants_v1\n{payload}".encode()).hexdigest()


def shadow_archived_id(product_id: int, external_id: str) -> str:
    payload = f"{PROOF_VERSION}:shadow:{product_id}:{external_id}"
    return f"pharmonline-retired-{product_id}-{hashlib.sha256(payload.encode()).hexdigest()[:20]}"


@dataclass
class CaseState:
    """Every isolation case, its source pairs and the decisions still in force."""

    cases: dict[int, _Case]
    variants: dict[int, frozenset[tuple[str, str]]]
    current: dict[int, _Resolution] = field(default_factory=dict)

    @property
    def pending(self) -> list[_Case]:
        return [case for case_id, case in sorted(self.cases.items()) if case_id not in self.current]

    @property
    def pending_by_product(self) -> dict[int, _Case]:
        return {case.product_id: case for case in self.pending}

    @property
    def cases_by_product(self) -> dict[int, _Case]:
        return {case.product_id: case for case in self.cases.values()}


def _canonical(url):
    from src.main import _canonical_pharmonline_product_url

    return _canonical_pharmonline_product_url(url)


def _retired_product_ids(session, products_by_id, tenants) -> set[int]:
    from src import pharmonline_identity_recovery as recovery

    retired: set[int] = set()
    for tenant in sorted(tenants):
        tenant_products = [p for p in products_by_id.values() if p.tenant_id == tenant]
        ids, _, _ = recovery.valid_state(session, tenant_products, tenant_id=tenant)
        retired |= ids
    return retired


def load_case_state(session, products_by_id) -> CaseState:
    """Read and validate the whole ledger; any contradiction fails closed.

    ``products_by_id`` must hold every Pharmonline Product. A Product is
    marked ``identity_conflicted`` exactly while one of its cases is pending.
    """
    from src.main import PharmonlinePublicAPIIdentityError

    cases = {
        case.id: case
        for case in session.scalars(select(_Case).execution_options(populate_existing=True))
    }
    grouped: dict[int, set[tuple[str, str]]] = {case_id: set() for case_id in cases}
    for variant in session.scalars(select(_Variant).execution_options(populate_existing=True)):
        if variant.conflict_id not in cases:
            raise PharmonlinePublicAPIIdentityError("identity variant has no conflict case")
        grouped[variant.conflict_id].add((str(variant.external_id), variant.canonical_url))
    state = CaseState(cases, {case_id: frozenset(pairs) for case_id, pairs in grouped.items()})
    rows = (
        session.scalars(
            select(_Resolution).order_by(_Resolution.id).execution_options(populate_existing=True)
        ).all()
        if available(session)
        else []
    )
    for row in rows:
        case = cases.get(row.conflict_id)
        if case is None or case.product_id != row.product_id or case.tenant_id != row.tenant_id:
            raise PharmonlinePublicAPIIdentityError(
                "identity conflict resolution has no matching case"
            )
        if row.case_last_run_id != case.last_run_id or row.variant_set_sha256 != variant_digest(
            state.variants[case.id]
        ):
            continue  # superseded: the Product was isolated again after this decision
        if case.id in state.current:
            raise PharmonlinePublicAPIIdentityError("identity conflict case has two decisions")
        state.current[case.id] = row
    if state.current:
        retired = _retired_product_ids(
            session, products_by_id, {row.tenant_id for row in state.current.values()}
        )
        for row in state.current.values():
            reason = _decision_invalid_reason(session, row, state, products_by_id, retired)
            if reason is not None:
                raise PharmonlinePublicAPIIdentityError(
                    f"identity conflict resolution is invalid: {reason}"
                )
    for case in cases.values():
        product = products_by_id.get(case.product_id)
        if (
            product is None
            or product.tenant_id != case.tenant_id
            or bool(product.identity_conflicted) != (case.id not in state.current)
        ):
            raise PharmonlinePublicAPIIdentityError("identity conflict ledger is inconsistent")
    pending_products = set(state.pending_by_product)
    if any(p.identity_conflicted and p.id not in pending_products for p in products_by_id.values()):
        raise PharmonlinePublicAPIIdentityError("identity conflict evidence is missing")
    return state


def _decision_invalid_reason(session, row, state, products_by_id, retired) -> str | None:
    from src.main import _PHARMONLINE_METEOR_ID_RE

    if row.proof_version != PROOF_VERSION or row.decision not in DECISIONS:
        return "proof_shape"
    if not all(
        _SHA_RE.fullmatch(value or "")
        for value in (
            row.variant_set_sha256,
            row.evidence_sha256,
            row.source_manifest_sha256,
            row.catalog_fingerprint_sha256,
        )
    ):
        return "proof_hash"
    if row.source_transport != "decodo":
        return "transport"
    if re.fullmatch(r"[0-9]{1,20}", str(row.preflight_run_ref)) is None:
        return "preflight_run"
    public_id, public_url = str(row.public_api_external_id), row.public_api_canonical_url
    if (
        _PHARMONLINE_METEOR_ID_RE.fullmatch(public_id) is None
        or _canonical(public_url) != public_url
    ):
        return "public_pair"
    if _PHARMONLINE_METEOR_ID_RE.fullmatch(str(row.legacy_external_id)) is None:
        return "legacy_id"
    if state.variants.get(row.conflict_id) != frozenset({(public_id, public_url)}):
        return "variants"
    product = products_by_id.get(row.product_id)
    current = products_by_id.get(row.current_product_id)
    if product is None or current is None:
        return "product_missing"
    if (
        product.tenant_id != row.tenant_id
        or current.tenant_id != row.tenant_id
        or product.site != "pharmonline"
        or current.site != "pharmonline"
    ):
        return "product_tenant"
    # The current Product's later life is not part of the decision: a case
    # stays closed until isolation reopens it, and a legitimate later change
    # of that Product must not turn this record into a catalog-wide refusal.
    if row.decision == RETIRED:
        retirement = (
            session.get(storage.PharmonlinePublicAPIIdentityRetirement, row.retirement_id)
            if row.retirement_id is not None
            else None
        )
        if (
            retirement is None
            or row.archived_external_id is not None
            or product.id == current.id
            or product.id not in retired
            or retirement.legacy_product_id != product.id
            or retirement.replacement_product_id != current.id
            or str(retirement.legacy_external_id) != str(row.legacy_external_id)
            or str(retirement.public_api_external_id) != public_id
            or _canonical(retirement.public_api_canonical_url) != public_url
        ):
            return "retirement"
    elif row.decision == SHADOW:
        if (
            row.retirement_id is not None
            or product.id == current.id
            or product.id not in retired
            or str(row.legacy_external_id) == public_id
            or row.archived_external_id
            != shadow_archived_id(product.id, str(row.legacy_external_id))
            or str(product.external_id) != row.archived_external_id
            or _canonical(product.url) != public_url
        ):
            return "shadow"
    elif (
        row.retirement_id is not None
        or row.archived_external_id is not None
        or product.id != current.id
        or str(row.legacy_external_id) != public_id
        or not any(
            other.decision == SHADOW and other.current_product_id == current.id
            for other in state.current.values()
        )
    ):
        return "confirmation"
    return None


def valid_shadow_product_ids(session, products_by_id, *, tenant_id) -> set[int]:
    """Archived shadows that a single intact decision authenticates.

    Called by ``pharmonline_identity_recovery.valid_state``: an archive marker
    without such a decision stays a hard verification failure there.
    """
    from src import pharmonline_identity_recovery as recovery

    if not available(session):
        return set()
    rows = session.scalars(
        select(_Resolution)
        .where(_Resolution.decision == SHADOW)
        .execution_options(populate_existing=True)
    ).all()
    counts: dict[int, int] = {}
    for row in rows:
        counts[row.product_id] = counts.get(row.product_id, 0) + 1
    valid: set[int] = set()
    for row in rows:
        product = products_by_id.get(row.product_id)
        current = products_by_id.get(row.current_product_id)
        case = session.get(_Case, row.conflict_id)
        if (
            counts[row.product_id] != 1
            or row.proof_version != PROOF_VERSION
            or row.source_transport != "decodo"
            or not all(
                _SHA_RE.fullmatch(value or "")
                for value in (
                    row.variant_set_sha256,
                    row.evidence_sha256,
                    row.source_manifest_sha256,
                    row.catalog_fingerprint_sha256,
                )
            )
            or re.fullmatch(r"[0-9]{1,20}", str(row.preflight_run_ref)) is None
            or product is None
            or current is None
            or case is None
            or case.product_id != product.id
            or row.tenant_id != tenant_id
            or product.tenant_id != tenant_id
            or current.tenant_id != tenant_id
            or product.site != "pharmonline"
            or product.id == current.id
            or row.archived_external_id
            != shadow_archived_id(product.id, str(row.legacy_external_id))
            or str(product.external_id) != row.archived_external_id
            or _canonical(product.url) != row.public_api_canonical_url
            or product.availability_source != recovery.AVAILABILITY_SOURCE
            or product.offer_availability_status != "unknown"
            or product.offer_quantity is not None
            or product.availability_run_id is not None
            or product.availability_observed_at is None
        ):
            continue
        valid.add(product.id)
    return valid


def _fact(product, *, external_id=None) -> str:
    return "\t".join(
        (
            "product",
            str(product.id),
            str(product.tenant_id),
            str(external_id if external_id is not None else product.external_id),
            str(_canonical(product.url)),
        )
    )


def evidence_sha256(state: CaseState, facts) -> str:
    """Bind a decision to the exact Products and pending case state it closes.

    ``facts`` is ``[(product, external_id_or_None)]``; an explicit ID lets the
    apply step re-derive the digest after retirement archived the row.
    """
    pending = state.pending_by_product
    lines = ["identity_conflict_resolution_evidence_v1"]
    for product, external_id in sorted(facts, key=lambda item: item[0].id):
        lines.append(_fact(product, external_id=external_id))
        case = pending.get(product.id)
        if case is not None:
            lines.append(
                f"case\t{case.id}\t{case.tenant_id}\t{case.first_run_id}\t{case.last_run_id}\t"
                f"{variant_digest(state.variants[case.id])}"
            )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def retirement_evidence(
    session, legacy, *, new_id, new_url, all_products, api_records, kind
) -> str | None:
    """Allow retirement v1 of a pending Product only when its case is exactly this move."""
    from src import pharmonline_identity_recovery as recovery
    from src.main import PharmonlinePublicAPIIdentityError

    if not available(session):
        return None
    try:
        state = load_case_state(session, {p.id: p for p in all_products})
    except PharmonlinePublicAPIIdentityError:
        return None
    case = state.pending_by_product.get(legacy.id)
    if case is None or state.variants[case.id] != frozenset({(new_id, new_url)}):
        return None
    for other in state.pending:
        if other.id != case.id and any(
            external_id == new_id or url == new_url for external_id, url in state.variants[other.id]
        ):
            return None
    if kind == recovery.URL_CHANGED:
        # Same native ID at a new address: the old address must have left the
        # complete catalog, otherwise two live cards claim one lineage.
        old_url = _canonical(legacy.url)
        if any(url == old_url for url, _ in api_records.values()):
            return None
    return evidence_sha256(state, [(legacy, None)])


@dataclass
class PlanIndex:
    """Pending cases visible to one reconciliation plan."""

    state: CaseState | None
    by_variant_id: dict[str, set[int]] = field(default_factory=dict)
    by_variant_url: dict[str, set[int]] = field(default_factory=dict)
    flagged_by_id: dict[str, set[int]] = field(default_factory=dict)
    flagged_by_url: dict[str, set[int]] = field(default_factory=dict)

    @property
    def broken(self) -> bool:
        return self.state is None

    def touching(self, external_id: str, url: str) -> set[int]:
        return (
            self.by_variant_id.get(external_id, set())
            | self.by_variant_url.get(url, set())
            | self.flagged_by_id.get(external_id, set())
            | self.flagged_by_url.get(url, set())
        )

    def pending_count(self, tenant_id: int) -> int:
        if self.state is None:
            return 0
        return sum(1 for case in self.state.pending if case.tenant_id == tenant_id)


def plan_index(session, all_site_products) -> PlanIndex:
    from src.main import PharmonlinePublicAPIIdentityError

    index = PlanIndex(state=None)
    for product in all_site_products:
        if product.identity_conflicted:
            index.flagged_by_id.setdefault(str(product.external_id), set()).add(product.id)
            index.flagged_by_url.setdefault(_canonical(product.url), set()).add(product.id)
    if not {_Case.__table__.name, _Variant.__table__.name}.issubset(
        set(inspect(session.connection()).get_table_names())
    ):
        index.state = CaseState({}, {}) if not index.flagged_by_id else None
        return index
    try:
        index.state = load_case_state(session, {p.id: p for p in all_site_products})
    except PharmonlinePublicAPIIdentityError:
        return index
    for case in index.state.pending:
        for external_id, url in index.state.variants[case.id]:
            index.by_variant_id.setdefault(external_id, set()).add(case.product_id)
            index.by_variant_url.setdefault(url, set()).add(case.product_id)
    return index


def plan_action(
    session,
    index: PlanIndex,
    *,
    external_id,
    url,
    public_product,
    api_records,
    all_site_products,
    retired_ids,
    trusted_by_id,
    trusted_history_product_ids,
    tenant_id,
):
    """Return the one decision that closes every case this source pair touches."""
    from src import pharmonline_identity_recovery as recovery

    if index.broken or not available(session):
        return None
    touched = index.touching(external_id, url)
    pending = index.state.pending_by_product
    if not touched or any(product_id not in pending for product_id in touched):
        return None
    live = [p for p in all_site_products if p.id not in retired_ids]
    with_id = [p for p in live if str(p.external_id) == external_id]
    at_url = [p for p in live if _canonical(p.url) == url]
    touched_rows = [p for p in all_site_products if p.id in touched]
    if len(with_id) > 1 or any(p.tenant_id != tenant_id for p in with_id + at_url + touched_rows):
        return None
    current = with_id[0] if with_id else None
    if current is not None and _canonical(current.url) == url:
        return _shadow_action(
            session,
            index,
            current=current,
            at_url=at_url,
            touched=touched,
            external_id=external_id,
            url=url,
            public_product=public_product,
            api_records=api_records,
            all_site_products=all_site_products,
            trusted_by_id=trusted_by_id,
            trusted_history_product_ids=trusted_history_product_ids,
        )
    if len(touched_rows) != 1:
        return None
    legacy = touched_rows[0]
    legacy_url = _canonical(legacy.url)
    if str(legacy.external_id) != external_id and legacy_url == url:
        kind = recovery.ID_REPLACED
    elif str(legacy.external_id) == external_id and legacy_url != url:
        kind = recovery.URL_CHANGED
    else:
        return None
    return recovery.candidate(
        session,
        legacy,
        public_product,
        api_records,
        all_site_products,
        tenant_id=tenant_id,
        kind=kind,
    )


def _has_lineage(session, product_id: int) -> bool:
    """Any observation or audit edge makes a row more than a forgotten shadow."""
    checks = [
        select(storage.OfferObservation.id).where(
            storage.OfferObservation.product_id == product_id
        ),
        select(storage.PharmonlinePublicAPIIdentityAdmission.id).where(
            storage.PharmonlinePublicAPIIdentityAdmission.product_id == product_id
        ),
        select(storage.PharmonlinePublicAPIIdentityReconciliation.id).where(
            storage.PharmonlinePublicAPIIdentityReconciliation.product_id == product_id
        ),
        select(_Resolution.id).where(
            or_(_Resolution.product_id == product_id, _Resolution.current_product_id == product_id)
        ),
    ]
    for model in (
        storage.PharmonlinePublicAPIIdentityQuarantine,
        storage.PharmonlinePublicAPIIdentityRetirement,
    ):
        checks.append(
            select(model.id).where(
                or_(
                    model.legacy_product_id == product_id,
                    model.replacement_product_id == product_id,
                )
            )
        )
    return any(session.scalar(query.limit(1)) is not None for query in checks)


def _shadow_action(
    session,
    index,
    *,
    current,
    at_url,
    touched,
    external_id,
    url,
    public_product,
    api_records,
    all_site_products,
    trusted_by_id,
    trusted_history_product_ids,
):
    from src import main as core

    shadows = [p for p in at_url if p.id != current.id]
    if len(shadows) != 1:
        return None
    shadow = shadows[0]
    if shadow.id not in touched or not touched <= {shadow.id, current.id}:
        return None
    pair = frozenset({(external_id, url)})
    pending = index.state.pending_by_product
    for product in (shadow, current):
        case = pending.get(product.id)
        if case is not None and index.state.variants[case.id] != pair:
            return None
    # The current card must already be trusted for exactly this pair and keep
    # its strict display name: the decision asserts nothing new about it.
    trusted = trusted_by_id.get(external_id)
    if trusted is None or trusted.id != current.id:
        return None
    name = core._pharmonline_product_name_signature(current.name)
    if not name or name != core._pharmonline_product_name_signature(public_product.name):
        return None
    legacy_id = str(shadow.external_id)
    if (
        core._PHARMONLINE_METEOR_ID_RE.fullmatch(legacy_id) is None
        or legacy_id == external_id
        or legacy_id in api_records
        or shadow.id in trusted_history_product_ids
        or shadow.availability_source == core._PHARMONLINE_DDP_AVAILABILITY_SOURCE
        or _has_lineage(session, shadow.id)
    ):
        return None
    archived = shadow_archived_id(shadow.id, legacy_id)
    if any(str(p.external_id) == archived for p in all_site_products):
        return None
    return core._PharmonlinePublicAPIIdentityQuarantineAction(
        legacy_product_id=shadow.id,
        legacy_external_id=legacy_id,
        legacy_canonical_url=url,
        public_api_external_id=external_id,
        public_api_canonical_url=url,
        public_product=public_product,
        retirement_kind=SHADOW_KIND,
        conflict_evidence_sha256=evidence_sha256(index.state, [(shadow, None), (current, None)]),
    )


def apply_resolutions(
    session,
    actions,
    *,
    tenant_id,
    source_manifest_sha256,
    catalog_fingerprint_sha256,
    source_transport,
    preflight_run_ref,
) -> dict[str, int]:
    """Write the decisions of an approved plan inside the caller's transaction.

    Retirements of conflicted Products must already be applied by
    ``pharmonline_identity_recovery.apply_retirements``. Any drift from the
    approved evidence raises and the caller rolls the whole batch back.
    """
    from src import main as core
    from src import pharmonline_identity_recovery as recovery

    actions = [action for action in actions if action.conflict_evidence_sha256 is not None]
    counts = {RETIRED: 0, SHADOW: 0, CONFIRMED: 0}
    if not actions:
        return counts
    if not available(session):
        raise core.PharmonlinePublicAPIReconciliationError(
            "identity conflict resolution schema is not migrated"
        )
    if source_transport != "decodo":
        raise core.PharmonlinePublicAPIReconciliationError(
            "identity conflict resolution requires manual Decodo proof"
        )

    def changed():
        return core.PharmonlinePublicAPIReconciliationError(
            "identity conflict resolution evidence changed"
        )

    products = session.scalars(
        select(storage.Product)
        .where(storage.Product.site == "pharmonline")
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    by_id = {product.id: product for product in products}
    try:
        state = load_case_state(session, by_id)
    except core.PharmonlinePublicAPIIdentityError as exc:
        raise changed() from exc
    pending = state.pending_by_product
    proof = dict(
        tenant_id=tenant_id,
        proof_version=PROOF_VERSION,
        source_manifest_sha256=source_manifest_sha256,
        catalog_fingerprint_sha256=catalog_fingerprint_sha256,
        source_transport=source_transport,
        preflight_run_ref=preflight_run_ref,
    )

    def record(action, case, decision, *, product, current, legacy_external_id, evidence, **extra):
        session.add(
            _Resolution(
                conflict_id=case.id,
                product_id=product.id,
                case_last_run_id=case.last_run_id,
                decision=decision,
                current_product_id=current.id,
                legacy_external_id=legacy_external_id,
                public_api_external_id=action.public_api_external_id,
                public_api_canonical_url=action.public_api_canonical_url,
                variant_set_sha256=variant_digest(state.variants[case.id]),
                evidence_sha256=evidence,
                **proof,
                **extra,
            )
        )
        product.identity_conflicted = False
        counts[decision] += 1

    closed: set[int] = set()
    for action in actions:
        pair = frozenset({(action.public_api_external_id, action.public_api_canonical_url)})
        legacy = by_id.get(action.legacy_product_id)
        case = pending.get(action.legacy_product_id)
        if (
            legacy is None
            or case is None
            or case.id in closed
            or legacy.tenant_id != tenant_id
            or state.variants[case.id] != pair
        ):
            raise changed()
        if action.retirement_kind in (recovery.URL_CHANGED, recovery.ID_REPLACED):
            retirement = session.scalar(
                select(storage.PharmonlinePublicAPIIdentityRetirement).where(
                    storage.PharmonlinePublicAPIIdentityRetirement.legacy_product_id == legacy.id
                )
            )
            evidence = evidence_sha256(state, [(legacy, action.legacy_external_id)])
            if (
                retirement is None
                or evidence != action.conflict_evidence_sha256
                or str(retirement.public_api_external_id) != action.public_api_external_id
                or _canonical(retirement.public_api_canonical_url)
                != action.public_api_canonical_url
            ):
                raise changed()
            replacement = by_id.get(retirement.replacement_product_id) or session.get(
                storage.Product, retirement.replacement_product_id
            )
            record(
                action,
                case,
                RETIRED,
                product=legacy,
                current=replacement,
                legacy_external_id=action.legacy_external_id,
                evidence=evidence,
                retirement_id=retirement.id,
            )
            closed.add(case.id)
            continue
        if action.retirement_kind != SHADOW_KIND:
            raise changed()
        current = [
            p
            for p in products
            if str(p.external_id) == action.public_api_external_id
            and _canonical(p.url) == action.public_api_canonical_url
        ]
        if (
            len(current) != 1
            or str(legacy.external_id) != action.legacy_external_id
            or _canonical(legacy.url) != action.public_api_canonical_url
            or _has_lineage(session, legacy.id)
            or evidence_sha256(state, [(legacy, None), (current[0], None)])
            != action.conflict_evidence_sha256
        ):
            raise changed()
        current = current[0]
        archived = shadow_archived_id(legacy.id, action.legacy_external_id)
        legacy.external_id = archived
        legacy.offer_availability_status = "unknown"
        legacy.offer_quantity = None
        legacy.availability_source = recovery.AVAILABILITY_SOURCE
        legacy.availability_observed_at = utcnow()
        legacy.availability_run_id = None
        session.flush()
        record(
            action,
            case,
            SHADOW,
            product=legacy,
            current=current,
            legacy_external_id=action.legacy_external_id,
            evidence=action.conflict_evidence_sha256,
            archived_external_id=archived,
        )
        closed.add(case.id)
        current_case = pending.get(current.id)
        if current_case is not None:
            if current_case.id in closed or state.variants[current_case.id] != pair:
                raise changed()
            record(
                action,
                current_case,
                CONFIRMED,
                product=current,
                current=current,
                legacy_external_id=action.public_api_external_id,
                evidence=action.conflict_evidence_sha256,
            )
            closed.add(current_case.id)
    session.flush()
    # The written decisions must pass the very validation every later scrape
    # applies before it trusts a closed case.
    try:
        after = load_case_state(session, by_id)
    except core.PharmonlinePublicAPIIdentityError as exc:
        raise changed() from exc
    if not closed <= set(after.current):
        raise changed()
    return counts
