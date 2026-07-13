#!/usr/bin/env python3
"""Dry-run-first country/offer audit and trusted legacy backfill.

Examples:
  python scripts/audit_product_policy.py
  python scripts/audit_product_policy.py --apply --legacy-sites aptekonline,pharmonline
  python scripts/audit_product_policy.py --apply --fetch-aloe --matched-only --limit 100
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import matcher, storage
from src._time import utcnow
from src.product_observations import apply_product_observation
from src.product_policy import country_resolution
from src.scrapers.aloe import aloe_product_detail_signals
from src.scrapers.base import ScrapedProduct


def _scraped(product: storage.Product, *, raw: str | None, source: str) -> ScrapedProduct:
    return ScrapedProduct(
        site=product.site,
        external_id=product.external_id,
        url=product.url,
        name=product.name,
        manufacturer_country_raw=raw,
        country_source=source,
    )


def _legacy_country(product: storage.Product) -> tuple[str | None, str | None]:
    if product.site == "aptekonline":
        return product.manufacturer, "legacy_aptek_olke"
    if product.site == "pharmonline":
        raw = product.manufacturer
        if country_resolution(raw)[0]:
            return raw, "legacy_pharmonline_manufacturer"
        # A country-looking URL suffix is not an authoritative SKU attribute.
        # Leave the value unresolved until a fresh DDP observation supplies the
        # website's explicit manufacturer-country field.
        return None, None
    return None, None


def _fetch_aloe(product: storage.Product, client: httpx.Client) -> ScrapedProduct | None:
    for attempt in range(3):
        try:
            response = client.get(product.url)
            response.raise_for_status()
            country, availability = aloe_product_detail_signals(response.text)
            return ScrapedProduct(
                site=product.site,
                external_id=product.external_id,
                url=product.url,
                name=product.name,
                manufacturer_country_raw=country,
                country_source="aloe_detail_country_label" if country else None,
                offer_availability_status=availability,
                availability_source="aloe_detail_in_stock",
            )
        except (httpx.HTTPError, ValueError):
            if attempt == 2:
                return None
            time.sleep(0.5 * (attempt + 1))
    return None


def _report(session) -> dict:
    products = session.scalars(select(storage.Product)).all()
    per_site: dict[str, Counter] = {}
    for product in products:
        counters = per_site.setdefault(product.site, Counter())
        counters["products"] += 1
        counters[f"country_{product.country_resolution_status or 'unknown'}"] += 1
        counters[f"offer_{product.offer_availability_status or 'unknown'}"] += 1

    conflicts = []
    matches = session.scalars(
        select(storage.Match).options(selectinload(storage.Match.products))
    ).all()
    matched_oos = 0
    for match in matches:
        members = list(match.products)
        if any(p.offer_availability_status == "out_of_stock" for p in members):
            matched_oos += 1
        for index, left in enumerate(members):
            for right in members[index + 1 :]:
                if left.site != right.site and matcher._has_conflicting_country(left, right):
                    conflicts.append(
                        {
                            "match_id": match.id,
                            "left": left.id,
                            "right": right.id,
                            "left_country": matcher._country_of(left),
                            "right_country": matcher._country_of(right),
                        }
                    )
                    break
            else:
                continue
            break
    return {
        "sites": {site: dict(counts) for site, counts in sorted(per_site.items())},
        "country_conflict_matches": len(conflicts),
        "country_conflict_sample": conflicts[:50],
        "matches_with_explicit_oos": matched_oos,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--legacy-sites", default="aptekonline,pharmonline")
    parser.add_argument("--fetch-aloe", action="store_true")
    parser.add_argument("--matched-only", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    Session = storage.make_session()
    with Session() as session:
        audit_run = storage.Run(
            tenant_id=1,
            status="running",
            started_at=utcnow(),
            catalog_scope="partial",
            catalog_verified=False,
            catalog_verification_reason="product_policy_audit_backfill",
        )
        session.add(audit_run)
        session.flush()
        query = select(storage.Product).order_by(storage.Product.id)
        if args.matched_only:
            query = query.where(storage.Product.canonical_id.is_not(None))
        products = list(session.scalars(query).all())
        if args.limit > 0:
            products = products[: args.limit]

        sites = {site.strip() for site in args.legacy_sites.split(",") if site.strip()}
        changed = Counter()
        observed_at = utcnow()
        observation_count = 0
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            for product in products:
                scraped = None
                if product.site in sites:
                    raw, source = _legacy_country(product)
                    if raw and source:
                        scraped = _scraped(product, raw=raw, source=source)
                if scraped is None:
                    continue
                before = (
                    product.manufacturer_country_code,
                    product.country_resolution_status,
                    product.offer_availability_status,
                )
                session.add(
                    apply_product_observation(
                        product,
                        scraped,
                        run_id=audit_run.id,
                        observed_at=observed_at,
                    )
                )
                observation_count += 1
                after = (
                    product.manufacturer_country_code,
                    product.country_resolution_status,
                    product.offer_availability_status,
                )
                if before != after:
                    changed[product.site] += 1

            # Aloe listing exposes a stable manufacturer_country dictionary ID.
            # Verify each ID against up to two different detail pages, then apply
            # the consensus mapping to the whole group: O(distinct countries),
            # not one network request per SKU.
            if args.fetch_aloe:
                aloe_groups: dict[str, list[storage.Product]] = {}
                for product in products:
                    raw = product.manufacturer_country_raw
                    if product.site == "aloe" and raw and str(raw).isdigit():
                        aloe_groups.setdefault(str(raw), []).append(product)
                for country_id, group in aloe_groups.items():
                    samples = [_fetch_aloe(product, client) for product in group[:2]]
                    samples = [sample for sample in samples if sample is not None]
                    codes = {
                        country_resolution(sample.manufacturer_country_raw)[0]
                        for sample in samples
                    }
                    codes.discard(None)
                    required = min(2, len(group))
                    if len(samples) < required or len(codes) != 1:
                        continue
                    country_raw = next(
                        sample.manufacturer_country_raw
                        for sample in samples
                        if country_resolution(sample.manufacturer_country_raw)[0]
                        in codes
                    )
                    country_code = country_resolution(country_raw)[0]
                    mapping = session.scalar(
                        select(storage.AloeCountryMapping).where(
                            storage.AloeCountryMapping.tenant_id == 1,
                            storage.AloeCountryMapping.country_id == country_id,
                        )
                    )
                    source_url = samples[0].url
                    if mapping is None:
                        session.add(
                            storage.AloeCountryMapping(
                                tenant_id=1,
                                country_id=country_id,
                                country_code=country_code,
                                country_raw=country_raw,
                                source_url=source_url,
                                sample_count=len(samples),
                                version=1,
                                verified_at=observed_at,
                            )
                        )
                    elif (
                        mapping.country_code != country_code
                        or mapping.country_raw != country_raw
                    ):
                        mapping.country_code = country_code
                        mapping.country_raw = country_raw
                        mapping.source_url = source_url
                        mapping.sample_count = len(samples)
                        mapping.version += 1
                        mapping.verified_at = observed_at
                    for product in group:
                        scraped = _scraped(
                            product,
                            raw=country_raw,
                            source="aloe_country_id_verified_detail",
                        )
                        before = (
                            product.manufacturer_country_code,
                            product.country_resolution_status,
                        )
                        session.add(
                            apply_product_observation(
                                product,
                                scraped,
                                run_id=audit_run.id,
                                observed_at=observed_at,
                            )
                        )
                        observation_count += 1
                        after = (
                            product.manufacturer_country_code,
                            product.country_resolution_status,
                        )
                        if before != after:
                            changed[product.site] += 1

        report = _report(session)
        report["mode"] = "apply" if args.apply else "dry_run"
        report["would_change"] = dict(changed)
        report["immutable_observations"] = observation_count
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        if args.apply:
            audit_run.status = "ok"
            audit_run.finished_at = utcnow()
            audit_run.products_scraped = observation_count
            session.commit()
        else:
            session.rollback()


if __name__ == "__main__":
    main()
