#!/usr/bin/env python3
"""Plan or apply the isolated Pharmonline legacy-ID reconciliation.

The default mode is read-only.  It proves two fresh, identical full public API
catalogs, then reports only aggregate candidate counts.  ``--apply`` is for
the exact-SHA gated recovery workflow only: it repeats the same source proof,
locks the relevant Product rows, updates eligible IDs in place, records the
immutable evidence, establishes a non-ratcheting catalog floor, and verifies
the resulting map before committing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import NoReturn

from sqlalchemy import text

from src import storage
from src.main import (
    _apply_pharmonline_public_api_reconciliation,
    _diagnose_pharmonline_public_api_reconciliation,
    _ensure_pharmonline_public_api_catalog_baseline,
    _pharmonline_public_api_catalog_fingerprint,
    _pharmonline_public_api_reconciliation_is_safe,
    _verify_pharmonline_public_api_identities,
)
from src.run_lock import try_exclusive_scrape_lock
from src.scrapers.pharmonline_public_api import (
    PUBLIC_CATALOG_ROUTE,
    PharmonlinePublicAPIScraper,
    is_retryable_full_catalog_abort_reason,
)


_MAX_FRESH_CATALOG_READ_ATTEMPTS = 3


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
            print(
                "Pharmonline public API reconciliation discarded an inconsistent "
                f"{pass_name} read; retrying from a fresh session "
                f"attempt={attempt}, reason={reason}"
            )
            await asyncio.sleep(attempt)
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


async def main(*, apply: bool) -> None:
    transport = _transport()
    verified_catalog = await read_two_identical_catalogs()
    results = [verified_catalog]
    fingerprint = _pharmonline_public_api_catalog_fingerprint(results)
    Session = storage.make_session()

    if not apply:
        with Session() as session:
            try:
                with try_exclusive_scrape_lock(session) as acquired:
                    if not acquired:
                        fail("an active scrape or recovery holds the production run lock")
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
                    )
            finally:
                session.rollback()
        diagnostic_text = ", ".join(f"{key}={value}" for key, value in sorted(diagnostics.items()))
        print(
            "Pharmonline public API reconciliation plan "
            f"(read-only, aggregate-only): transport={transport}, "
            f"products={len(verified_catalog.products)}, "
            f"catalog_fingerprint_sha256={fingerprint}, {diagnostic_text}"
        )
        if not _pharmonline_public_api_reconciliation_is_safe(diagnostics):
            fail("one or more legacy identity transitions require manual proof")
        return

    source_manifest_sha256, preflight_run_ref = _workflow_evidence()
    with Session() as session:
        try:
            with try_exclusive_scrape_lock(session) as acquired:
                if not acquired:
                    fail("an active scrape or recovery holds the production run lock")
                session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                metrics = _apply_pharmonline_public_api_reconciliation(
                    session,
                    results,
                    tenant_id=1,
                    source_manifest_sha256=source_manifest_sha256,
                    catalog_fingerprint_sha256=fingerprint,
                    source_transport=transport,
                    preflight_run_ref=preflight_run_ref,
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
