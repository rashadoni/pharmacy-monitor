#!/usr/bin/env python3
"""Plan or apply the isolated Pharmonline legacy-ID reconciliation.

The default mode is read-only.  It proves two fresh, identical full public API
catalogs, then reports only aggregate candidate counts.  ``--apply`` is for
the exact-SHA gated recovery workflow only: it repeats the same source proof,
locks the relevant Product rows, applies only proven rekeys or quarantined
identity splits, records immutable evidence, establishes a non-ratcheting
catalog floor, and verifies the resulting map before committing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import NoReturn

from sqlalchemy import text

from src import storage
from src.main import (
    _PharmonlinePublicAPILegacySelfRedirectProof,
    _apply_pharmonline_public_api_reconciliation,
    _diagnose_pharmonline_public_api_reconciliation,
    _ensure_pharmonline_public_api_catalog_baseline,
    _pharmonline_public_api_catalog_fingerprint,
    _pharmonline_public_api_reconciliation_plan,
    _pharmonline_public_api_reconciliation_plan_manifest_sha256,
    _pharmonline_public_api_reconciliation_is_safe,
    _pharmonline_public_api_redirect_challenges,
    _verify_pharmonline_public_api_identities,
)
from src.run_lock import wait_for_exclusive_scrape_lock
from src.scrapers.pharmonline_public_api import (
    PUBLIC_CATALOG_ROUTE,
    PharmonlinePublicAPIScraper,
    is_retryable_full_catalog_abort_reason,
)


_MAX_FRESH_CATALOG_READ_ATTEMPTS = 3
_MAX_PRODUCTION_LOCK_WAIT_SECONDS = 45 * 60
_PRODUCTION_LOCK_POLL_SECONDS = 15


def fail(message: str) -> NoReturn:
    raise SystemExit(f"Pharmonline public API reconciliation failed: {message}")


async def read_catalog_pass(pass_name: str):
    """Return one complete catalog or discard the entire inconsistent attempt."""
    for attempt in range(1, _MAX_FRESH_CATALOG_READ_ATTEMPTS + 1):
        async with PharmonlinePublicAPIScraper() as scraper:
            result = await scraper.scrape([PUBLIC_CATALOG_ROUTE])

        route = result.route_statuses.get(PUBLIC_CATALOG_ROUTE)
        complete = (
            not result.site_fatal
            and not result.errors
            and route is not None
            and route.complete
            and len(result.products) > 0
        )
        if complete:
            if route.expected_items != len(result.products):
                fail(
                    f"{pass_name} route coverage mismatch: "
                    f"expected_items={route.expected_items}, products={len(result.products)}"
                )
            return result

        reason = route.abort_reason if route is not None else None
        if (
            is_retryable_full_catalog_abort_reason(reason)
            and attempt < _MAX_FRESH_CATALOG_READ_ATTEMPTS
        ):
            retry_delay = (
                300
                if reason
                in {
                    "decodo_http_429",
                    "decodo_request_failed",
                    "decodo_transient_request_failed",
                    "product_sitemap_empty",
                }
                else attempt
            )
            print(
                "Pharmonline public API reconciliation discarded an inconsistent "
                f"{pass_name} read; retrying from a fresh session "
                f"attempt={attempt}, reason={reason}, delay_seconds={retry_delay}"
            )
            await asyncio.sleep(retry_delay)
            continue
        fail(
            f"{pass_name} did not produce one verified full catalog: "
            f"site_fatal={result.site_fatal}, errors={len(result.errors)}, "
            f"products={len(result.products)}, route_complete={bool(route and route.complete)}, "
            f"abort_reason={reason or 'none'}, attempts={attempt}"
        )
    raise AssertionError("unreachable")


def identity_sequence(result) -> tuple[tuple[str, str], ...]:
    return tuple((str(product.external_id), product.url) for product in result.products)


async def read_two_identical_catalogs():
    first_pass = await read_catalog_pass("first pass")
    second_pass = await read_catalog_pass("second pass")
    if identity_sequence(first_pass) != identity_sequence(second_pass):
        fail("two complete source passes are not identical identity-by-identity")
    return first_pass


def _transport() -> str:
    transport = os.environ.get("PHARMONLINE_PUBLIC_API_TRANSPORT", "").strip().lower()
    if not transport:
        fail("PHARMONLINE_PUBLIC_API_TRANSPORT must be explicit")
    return transport


def _workflow_evidence() -> tuple[str, str]:
    source_manifest_sha256 = os.environ.get(
        "PHARMONLINE_PUBLIC_API_SOURCE_MANIFEST_SHA256", ""
    ).strip()
    preflight_run_ref = os.environ.get("PHARMONLINE_PUBLIC_API_PREFLIGHT_RUN_REF", "").strip()
    if not source_manifest_sha256 or not preflight_run_ref:
        fail("--apply requires immutable exact-SHA workflow evidence")
    return source_manifest_sha256, preflight_run_ref


def _expected_plan_evidence() -> tuple[str, str, int]:
    """Read only the non-sensitive immutable output of the successful plan."""
    fingerprint = os.environ.get(
        "PHARMONLINE_PUBLIC_API_EXPECTED_PLAN_CATALOG_FINGERPRINT_SHA256", ""
    ).strip()
    manifest = os.environ.get("PHARMONLINE_PUBLIC_API_EXPECTED_PLAN_MANIFEST_SHA256", "").strip()
    raw_count = os.environ.get("PHARMONLINE_PUBLIC_API_EXPECTED_PLAN_PRODUCT_COUNT", "").strip()
    if len(fingerprint) != 64 or len(manifest) != 64 or not raw_count.isdigit():
        fail("--apply requires the exact successful plan evidence artifact")
    return fingerprint, manifest, int(raw_count)


def _production_lock_wait_seconds() -> int:
    """Read the bounded workflow wait without allowing an unbounded job."""
    raw = os.environ.get("PHARMONLINE_PUBLIC_API_LOCK_WAIT_SECONDS", "0").strip()
    if not raw:
        return 0
    if not raw.isdigit():
        fail("PHARMONLINE_PUBLIC_API_LOCK_WAIT_SECONDS must be a whole number")
    seconds = int(raw)
    if seconds > _MAX_PRODUCTION_LOCK_WAIT_SECONDS:
        fail(
            "PHARMONLINE_PUBLIC_API_LOCK_WAIT_SECONDS exceeds the safe "
            f"maximum of {_MAX_PRODUCTION_LOCK_WAIT_SECONDS} seconds"
        )
    return seconds


async def read_two_redirect_proofs(challenges, *, transport: str):
    """Classify two fresh direct first-party redirect observations per move.

    A redirect to the current public URL proves safe continuity. A permanent
    redirect back to the old canonical URL proves the opposite: it is admitted
    only as a separately typed legacy-self witness for a quarantined split.
    Every other outcome remains unproven.
    """
    if not challenges:
        return (), (), {}
    if transport != "decodo":
        fail("native URL redirect proof requires the explicit Decodo transport")

    async def read_pass() -> tuple[tuple, tuple, tuple[str, ...], dict[str, int]]:
        async with PharmonlinePublicAPIScraper() as scraper:
            proven = []
            legacy_self = []
            verdicts = []
            reason_counts: dict[str, int] = {}
            for challenge in challenges:
                reason = await scraper.product_url_redirect_proof_reason(
                    legacy_url=challenge.legacy_canonical_url,
                    public_api_url=challenge.public_api_canonical_url,
                    public_api_external_id=challenge.public_api_external_id,
                )
                verdicts.append(reason)
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                if reason == "verified":
                    proven.append(challenge)
                elif reason == "redirect_target_legacy_product":
                    legacy_self.append(
                        _PharmonlinePublicAPILegacySelfRedirectProof(
                            product_id=challenge.product_id,
                            public_api_external_id=challenge.public_api_external_id,
                            legacy_canonical_url=challenge.legacy_canonical_url,
                            public_api_canonical_url=challenge.public_api_canonical_url,
                        )
                    )
            return tuple(proven), tuple(legacy_self), tuple(verdicts), reason_counts

    first_proven, first_legacy_self, first_verdicts, first_reasons = await read_pass()
    second_proven, second_legacy_self, second_verdicts, second_reasons = await read_pass()
    if (
        first_proven != second_proven
        or first_legacy_self != second_legacy_self
        or first_verdicts != second_verdicts
        or first_reasons != second_reasons
    ):
        fail("two fresh permanent-redirect proof passes disagree")
    return first_proven, first_legacy_self, first_reasons


def write_plan_evidence(
    *,
    transport: str,
    product_count: int,
    catalog_fingerprint_sha256: str,
    candidate_manifest_sha256: str,
    metrics: dict[str, int],
) -> None:
    """Persist aggregate-only evidence for the exact gated apply workflow."""
    raw_path = os.environ.get("PHARMONLINE_PUBLIC_API_PLAN_EVIDENCE_PATH", "").strip()
    if not raw_path:
        return
    path = Path(raw_path)
    payload = {
        "version": 3,
        "transport": transport,
        "product_count": product_count,
        "catalog_fingerprint_sha256": catalog_fingerprint_sha256,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "metrics": metrics,
    }
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


async def main(*, apply: bool) -> None:
    transport = _transport()
    lock_wait_seconds = _production_lock_wait_seconds()
    source_manifest_sha256 = ""
    preflight_run_ref = ""
    expected_plan_fingerprint = ""
    expected_plan_manifest = ""
    expected_plan_product_count = 0
    if apply:
        source_manifest_sha256, preflight_run_ref = _workflow_evidence()
        (
            expected_plan_fingerprint,
            expected_plan_manifest,
            expected_plan_product_count,
        ) = _expected_plan_evidence()

    # Take the producer lock before the first source read.  Both the plan and
    # apply paths prove a catalog sequence against mutable product state, so a
    # concurrent scrape must not be able to change that state halfway through
    # the proof. The bounded workflow wait permits an already-running producer
    # to finish; it never overlaps or bypasses the lock. The lock itself uses
    # a dedicated connection and does not turn the later
    # read-only/SERIALIZABLE Session transaction into a write.
    Session = storage.make_session()
    with Session() as session:
        try:
            if lock_wait_seconds:
                print(
                    "Pharmonline public API reconciliation will wait for the "
                    f"production run lock for at most {lock_wait_seconds} seconds"
                )
            async with wait_for_exclusive_scrape_lock(
                session,
                timeout_seconds=lock_wait_seconds,
                poll_seconds=_PRODUCTION_LOCK_POLL_SECONDS,
            ) as acquired:
                if not acquired:
                    fail(
                        "an active scrape or recovery held the production run lock "
                        f"for {lock_wait_seconds} seconds"
                    )

                verified_catalog = await read_two_identical_catalogs()
                results = [verified_catalog]
                fingerprint = _pharmonline_public_api_catalog_fingerprint(results)
                redirect_challenges = _pharmonline_public_api_redirect_challenges(
                    session,
                    results,
                    tenant_id=1,
                )
                # The challenge lookup starts an ORM transaction.  End it
                # before any network I/O; the dedicated advisory lock remains
                # held for the full source proof and apply sequence.
                session.rollback()
                (
                    redirect_proofs,
                    legacy_self_redirect_proofs,
                    redirect_reason_counts,
                ) = await read_two_redirect_proofs(
                    redirect_challenges,
                    transport=transport,
                )
                if redirect_challenges:
                    reason_text = ", ".join(
                        f"{reason}={count}"
                        for reason, count in sorted(redirect_reason_counts.items())
                    )
                    print(
                        "Pharmonline native URL redirect proof "
                        f"(aggregate-only): candidates={len(redirect_challenges)}, "
                        f"verified={len(redirect_proofs)}, "
                        f"legacy_self={len(legacy_self_redirect_proofs)}, {reason_text}"
                    )

                if apply and (
                    fingerprint != expected_plan_fingerprint
                    or len(verified_catalog.products) != expected_plan_product_count
                ):
                    fail("fresh catalog differs from the approved read-only plan")

                if not apply:
                    session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    )
                    transaction_read_only = str(
                        session.scalar(text("SHOW transaction_read_only"))
                    ).lower()
                    if transaction_read_only not in {"on", "true", "1"}:
                        fail("database transaction did not enter read-only mode")
                    diagnostics = _diagnose_pharmonline_public_api_reconciliation(
                        session,
                        results,
                        tenant_id=1,
                        redirect_proofs=redirect_proofs,
                        legacy_self_redirect_proofs=legacy_self_redirect_proofs,
                    )
                    (
                        actions,
                        admissions,
                        quarantines,
                        plan_metrics,
                    ) = _pharmonline_public_api_reconciliation_plan(
                        session,
                        results,
                        tenant_id=1,
                        redirect_proofs=redirect_proofs,
                        legacy_self_redirect_proofs=legacy_self_redirect_proofs,
                    )
                    plan_manifest_sha256 = (
                        _pharmonline_public_api_reconciliation_plan_manifest_sha256(
                            actions,
                            admissions,
                            quarantines,
                            plan_metrics,
                        )
                    )
                    session.rollback()
                else:
                    session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                    metrics = _apply_pharmonline_public_api_reconciliation(
                        session,
                        results,
                        tenant_id=1,
                        source_manifest_sha256=source_manifest_sha256,
                        catalog_fingerprint_sha256=fingerprint,
                        source_transport=transport,
                        preflight_run_ref=preflight_run_ref,
                        redirect_proofs=redirect_proofs,
                        legacy_self_redirect_proofs=legacy_self_redirect_proofs,
                        expected_plan_manifest_sha256=expected_plan_manifest,
                    )
                    catalog_floor = _ensure_pharmonline_public_api_catalog_baseline(
                        session,
                        results,
                        tenant_id=1,
                        verified_identity_count=len(verified_catalog.products),
                        trusted_ddp_item_count=metrics["trusted_ddp_identities"],
                        retired_ddp_item_count=metrics["retired_ddp_identities"],
                        reconciled_item_count=(
                            metrics["reconciled_identities"]
                            + metrics["legacy_rekeys_ready"]
                            + metrics["native_id_url_rebind_ready"]
                            + metrics["native_id_url_rebind_redirect_ready"]
                            + metrics["identity_splits_ready"]
                            + metrics["existing_native_admissions_ready"]
                            + metrics["new_public_product_admissions_ready"]
                        ),
                        source_manifest_sha256=source_manifest_sha256,
                        catalog_fingerprint_sha256=fingerprint,
                        source_transport=transport,
                        preflight_run_ref=preflight_run_ref,
                    )
                    verified_identities = _verify_pharmonline_public_api_identities(
                        session,
                        results,
                        tenant_id=1,
                    )
                    session.commit()
        except Exception:
            session.rollback()
            raise

    if not apply:
        diagnostic_text = ", ".join(f"{key}={value}" for key, value in sorted(diagnostics.items()))
        print(
            "Pharmonline public API reconciliation plan "
            f"(read-only, aggregate-only): transport={transport}, "
            f"products={len(verified_catalog.products)}, "
            f"catalog_fingerprint_sha256={fingerprint}, {diagnostic_text}"
        )
        if not _pharmonline_public_api_reconciliation_is_safe(diagnostics):
            fail("one or more legacy identity transitions require manual proof")
        write_plan_evidence(
            transport=transport,
            product_count=len(verified_catalog.products),
            catalog_fingerprint_sha256=fingerprint,
            candidate_manifest_sha256=plan_manifest_sha256,
            metrics=diagnostics,
        )
        return

    metric_text = ", ".join(f"{key}={value}" for key, value in sorted(metrics.items()))
    print(
        "Pharmonline public API reconciliation applied: "
        f"transport={transport}, products={len(verified_catalog.products)}, "
        f"verified_identities={verified_identities}, catalog_floor={catalog_floor}, "
        f"catalog_fingerprint_sha256={fingerprint}, {metric_text}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply only the fully proven batch supplied by the recovery workflow",
    )
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
