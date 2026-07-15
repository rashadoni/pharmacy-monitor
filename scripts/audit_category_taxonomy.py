"""Audit canonical category coverage and cross-site agreement.

Read-only.  The report distinguishes categories that all classifiable source
sites agree on from conflicts and broad/unclassifiable source buckets.  It is
intended to be run before expanding taxonomy rules.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict

import click
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.category_taxonomy import classify_source_category, source_category_labels
from src.storage import Match, make_session


def build_category_taxonomy_audit(session, *, tenant_id: int = 1) -> dict:
    labels = source_category_labels(session)
    matches = session.scalars(
        select(Match).where(Match.tenant_id == tenant_id).options(selectinload(Match.products))
    ).all()

    raw_client_categories: set[str] = set()
    mapped_client_categories: set[str] = set()
    unclassified_client: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    aligned: Counter[str] = Counter()
    unverifiable: Counter[str] = Counter()
    conflicting: Counter[str] = Counter()
    raw_by_canonical: dict[str, set[str]] = defaultdict(set)
    conflict_samples: list[dict] = []

    def mapped_key(product) -> str | None:
        label_ru, label_az = labels.get((product.site, product.category or ""), (None, None))
        category = classify_source_category(
            product.site,
            product.category,
            label_ru=label_ru,
            label_az=label_az,
        )
        return category.key if category else None

    for match in matches:
        live = [product for product in match.products if product.url_dead_at is None]
        clients = [product for product in live if product.site == "pharmonline"]
        competitors = [product for product in live if product.site != "pharmonline"]
        if not clients or not competitors:
            continue

        client = clients[0]
        raw_category = client.category or "(uncategorized)"
        raw_client_categories.add(raw_category)
        client_key = mapped_key(client)
        if client_key is None:
            unclassified_client[raw_category] += 1
            continue

        mapped_client_categories.add(raw_category)
        raw_by_canonical[client_key].add(raw_category)
        totals[client_key] += 1

        competitor_keys = {key for product in competitors if (key := mapped_key(product))}
        if not competitor_keys:
            unverifiable[client_key] += 1
        elif competitor_keys == {client_key}:
            aligned[client_key] += 1
        else:
            conflicting[client_key] += 1
            if len(conflict_samples) < 30:
                conflict_samples.append(
                    {
                        "match_id": match.id,
                        "name": match.canonical_name,
                        "client_category": raw_category,
                        "client_canonical": client_key,
                        "competitor_canonical": sorted(competitor_keys),
                    }
                )

    category_rows = []
    for key, total in totals.most_common():
        checked = aligned[key] + conflicting[key]
        category_rows.append(
            {
                "key": key,
                "matched_skus": total,
                "source_categories": len(raw_by_canonical[key]),
                "aligned_skus": aligned[key],
                "conflicting_skus": conflicting[key],
                "unverifiable_skus": unverifiable[key],
                "alignment_pct": round(aligned[key] / checked * 100, 1) if checked else None,
            }
        )

    return {
        "summary": {
            "cross_site_matches": sum(totals.values()) + sum(unclassified_client.values()),
            "raw_client_categories": len(raw_client_categories),
            "mapped_raw_client_categories": len(mapped_client_categories),
            "canonical_categories": len(totals),
            "mapped_matches": sum(totals.values()),
            "unclassified_matches": sum(unclassified_client.values()),
            "aligned_matches": sum(aligned.values()),
            "conflicting_matches": sum(conflicting.values()),
            "unverifiable_matches": sum(unverifiable.values()),
        },
        "categories": category_rows,
        "top_unclassified_client_categories": [
            {"category": category, "matched_skus": count}
            for category, count in unclassified_client.most_common(30)
        ],
        "conflict_samples": conflict_samples,
    }


@click.command()
@click.option("--tenant-id", default=1, type=int, show_default=True)
def main(tenant_id: int) -> None:
    """Print a read-only JSON audit to stdout."""
    session = make_session()()
    try:
        report = build_category_taxonomy_audit(session, tenant_id=tenant_id)
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        session.close()


if __name__ == "__main__":
    main()
