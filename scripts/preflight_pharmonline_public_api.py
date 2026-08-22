#!/usr/bin/env python3
"""Prove a complete Pharmonline public-API catalog without persistence.

This operational entry point deliberately uses the same guarded scraper that
the recovery command uses. It executes two fresh full passes, each of which
validates every advertised page and the product sitemap before it yields a
product. Only after both identity sequences match does it query production
Postgres in a read-only transaction for the trusted DDP lineage proof.

Run it only with ``PHARMONLINE_PUBLIC_API=required`` and an explicitly selected
transport. It never creates a Run, Product, OfferObservation, or snapshot.
"""

from __future__ import annotations

import asyncio
import os
from typing import NoReturn

from sqlalchemy import text

from src import storage
from src.main import (
    PharmonlinePublicAPIIdentityError,
    _diagnose_pharmonline_public_api_identities,
    _verify_pharmonline_public_api_identities,
)
from src.scrapers.pharmonline_public_api import (
    PUBLIC_CATALOG_ROUTE,
    PharmonlinePublicAPIScraper,
)


def fail(message: str) -> NoReturn:
    raise SystemExit(f"Pharmonline public API full preflight failed: {message}")


async def read_catalog_pass(pass_name: str):
    async with PharmonlinePublicAPIScraper() as scraper:
        result = await scraper.scrape([PUBLIC_CATALOG_ROUTE])

    route = result.route_statuses.get(PUBLIC_CATALOG_ROUTE)
    if (
        result.site_fatal
        or result.errors
        or route is None
        or not route.complete
        or len(result.products) < 1
    ):
        fail(
            f"{pass_name} did not produce one verified full catalog: "
            f"site_fatal={result.site_fatal}, errors={len(result.errors)}, "
            f"products={len(result.products)}, route_complete={bool(route and route.complete)}"
        )
    if route.expected_items != len(result.products):
        fail(
            f"{pass_name} route coverage mismatch: "
            f"expected_items={route.expected_items}, products={len(result.products)}"
        )
    return result


def identity_sequence(result) -> tuple[tuple[str, str], ...]:
    return tuple((str(product.external_id), product.url) for product in result.products)


async def main() -> None:
    transport = os.environ.get("PHARMONLINE_PUBLIC_API_TRANSPORT", "").strip().lower()
    if not transport:
        fail("PHARMONLINE_PUBLIC_API_TRANSPORT must be explicit")

    first_pass = await read_catalog_pass("first pass")
    second_pass = await read_catalog_pass("second pass")
    first_identities = identity_sequence(first_pass)
    second_identities = identity_sequence(second_pass)
    if first_identities != second_identities:
        fail("two complete source passes are not identical identity-by-identity")

    Session = storage.make_session()
    with Session() as session:
        try:
            session.execute(text("SET TRANSACTION READ ONLY"))
            transaction_read_only = str(
                session.scalar(text("SHOW transaction_read_only"))
            ).lower()
            if transaction_read_only not in {"on", "true", "1"}:
                fail("database transaction did not enter read-only mode")
            try:
                verified_identities = _verify_pharmonline_public_api_identities(
                    session,
                    [first_pass],
                    tenant_id=1,
                )
            except PharmonlinePublicAPIIdentityError as exc:
                diagnostics = _diagnose_pharmonline_public_api_identities(
                    session,
                    [first_pass],
                    tenant_id=1,
                )
                diagnostic_text = ", ".join(
                    f"{key}={value}" for key, value in sorted(diagnostics.items())
                )
                print(
                    "Pharmonline public API identity diagnostics "
                    f"(read-only, aggregate-only): {diagnostic_text}"
                )
                fail(str(exc))
        finally:
            session.rollback()

    route = first_pass.route_statuses[PUBLIC_CATALOG_ROUTE]
    print(
        "Pharmonline public API full preflight OK: "
        f"transport={transport}, products={len(first_pass.products)}, "
        f"pages={route.expected_pages}, two_passes=identical, "
        f"trusted_ddp_identities={verified_identities}, "
        "database_transaction=read_only, persistence=none"
    )


if __name__ == "__main__":
    asyncio.run(main())
