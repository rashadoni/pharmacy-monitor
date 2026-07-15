"""Apply trusted scrape observations to persistent product state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src import storage
from src.product_policy import (
    COUNTRY_AMBIGUOUS,
    COUNTRY_RESOLVED,
    OFFER_IN_STOCK,
    OFFER_OUT_OF_STOCK,
    country_resolution,
)

_PROMOTE_COUNTRY_AFTER_DISTINCT_RUNS = 2


def apply_country_observation(
    product: storage.Product,
    scraped: Any,
    *,
    run_id: int,
    observed_at: datetime,
) -> tuple[str | None, str]:
    """Apply country signal with a two-distinct-run conflict quarantine."""
    raw = getattr(scraped, "manufacturer_country_raw", None)
    code, status = country_resolution(raw)
    source = getattr(scraped, "country_source", None)

    if raw is not None:
        product.manufacturer_country_raw = str(raw)[:160]

    current = product.manufacturer_country_code
    if status != COUNTRY_RESOLVED or code is None:
        if current is None:
            product.country_resolution_status = status
            product.country_source = source
            product.country_observed_at = observed_at
        return code, status

    if current is None or current == code:
        product.manufacturer_country_code = code
        product.country_resolution_status = COUNTRY_RESOLVED
        product.country_source = source
        product.country_observed_at = observed_at
        product.country_candidate_code = None
        product.country_candidate_seen_count = 0
        product.country_candidate_observed_at = None
        product.country_candidate_run_id = None
        return code, COUNTRY_RESOLVED

    if product.country_candidate_code != code:
        product.country_candidate_code = code
        product.country_candidate_seen_count = 1
    elif product.country_candidate_run_id != run_id:
        product.country_candidate_seen_count += 1
    product.country_candidate_observed_at = observed_at
    product.country_candidate_run_id = run_id
    product.country_resolution_status = COUNTRY_AMBIGUOUS

    if product.country_candidate_seen_count >= _PROMOTE_COUNTRY_AFTER_DISTINCT_RUNS:
        product.manufacturer_country_code = code
        product.country_resolution_status = COUNTRY_RESOLVED
        product.country_source = source
        product.country_observed_at = observed_at
        product.country_candidate_code = None
        product.country_candidate_seen_count = 0
        product.country_candidate_observed_at = None
        product.country_candidate_run_id = None
        return code, COUNTRY_RESOLVED
    return code, COUNTRY_AMBIGUOUS


def apply_offer_observation(
    product: storage.Product,
    scraped: Any,
    *,
    run_id: int,
    observed_at: datetime,
) -> str:
    """Update current offer only from an explicit in/out-of-stock signal."""
    status = getattr(scraped, "offer_availability_status", "unknown") or "unknown"
    if status in {OFFER_IN_STOCK, OFFER_OUT_OF_STOCK}:
        product.offer_availability_status = status
        product.offer_quantity = getattr(scraped, "offer_quantity", None)
        product.availability_source = getattr(scraped, "availability_source", None)
        product.availability_observed_at = observed_at
        product.availability_run_id = run_id
    return status


def observation_row(
    product: storage.Product,
    scraped: Any,
    *,
    run_id: int,
    observed_at: datetime,
) -> storage.OfferObservation:
    """Build immutable history row from the raw signal, not inferred state."""
    code, country_status = country_resolution(
        getattr(scraped, "manufacturer_country_raw", None)
    )
    return storage.OfferObservation(
        tenant_id=product.tenant_id,
        run_id=run_id,
        product_id=product.id,
        country_code=code,
        country_raw=getattr(scraped, "manufacturer_country_raw", None),
        country_resolution_status=country_status,
        country_source=getattr(scraped, "country_source", None),
        availability_status=getattr(scraped, "offer_availability_status", "unknown")
        or "unknown",
        quantity=getattr(scraped, "offer_quantity", None),
        availability_source=getattr(scraped, "availability_source", None),
        observed_at=observed_at,
    )


def apply_product_observation(
    product: storage.Product,
    scraped: Any,
    *,
    run_id: int,
    observed_at: datetime,
) -> storage.OfferObservation:
    apply_country_observation(product, scraped, run_id=run_id, observed_at=observed_at)
    apply_offer_observation(product, scraped, run_id=run_id, observed_at=observed_at)
    return observation_row(product, scraped, run_id=run_id, observed_at=observed_at)
